"""Tests for the per-user endpoint store: totality, permissions, atomicity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mak.core.exceptions import ConfigError
from mak.endpoints.parse import parse_endpoint
from mak.endpoints.store import (
    SCHEMA_VERSION,
    endpoints_path,
    load_user_endpoints,
    merge_endpoints,
    save_user_endpoints,
    to_dict,
)
from mak.endpoints.types import (
    EndpointConfig,
    EndpointHeaderConfig,
    Location,
    StructuredOutput,
    Transport,
)


def _endpoint(endpoint_id: str = "gw", **kw: object) -> EndpointConfig:
    base: dict[str, object] = {
        "id": endpoint_id,
        "transport": Transport.OPENAI_CHAT,
        "base_url": "https://gw.example/v1",
        "api_key_env": "GW_KEY",
    }
    base.update(kw)
    return EndpointConfig(**base)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_an_endpoint_survives_a_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        save_user_endpoints((_endpoint(),), path)
        loaded, diagnostic = load_user_endpoints(path)
        assert diagnostic is None
        assert loaded == (_endpoint(),)

    def test_every_field_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        original = _endpoint(
            "nv",
            profile="nvidia",
            display_name="NVIDIA Build",
            location=Location.HOSTED,
            structured_output=StructuredOutput.JSON_OBJECT,
            headers=(
                EndpointHeaderConfig(name="X-Title", value="MAK"),
                EndpointHeaderConfig(name="X-Token", value_env="GW_TOK"),
            ),
        )
        save_user_endpoints((original,), path)
        loaded, _ = load_user_endpoints(path)
        assert loaded == (original,)

    def test_an_unset_capability_is_omitted_not_nulled(self) -> None:
        """Writing the resolved default would freeze it into the user's file.

        A later correction to the profile would then never reach them.
        """
        data = to_dict(_endpoint())
        assert "structured_output" not in data
        assert "health_check" not in data

    def test_no_credential_value_is_ever_written(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        os.environ["GW_KEY"] = "sk-sentinel-value"
        try:
            save_user_endpoints((_endpoint(),), path)
            body = path.read_text(encoding="utf-8")
        finally:
            del os.environ["GW_KEY"]
        assert "sk-sentinel-value" not in body
        assert "GW_KEY" in body  # the NAME is what is stored

    def test_several_endpoints_keep_their_order(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        save_user_endpoints(
            (_endpoint("a"), _endpoint("b"), _endpoint("c")), path
        )
        loaded, _ = load_user_endpoints(path)
        assert [e.id for e in loaded] == ["a", "b", "c"]


class TestReadsAreTotal:
    def test_a_missing_file_is_an_empty_store_with_no_complaint(
        self, tmp_path: Path
    ) -> None:
        loaded, diagnostic = load_user_endpoints(tmp_path / "absent.json")
        assert loaded == ()
        assert diagnostic is None

    def test_corrupt_json_degrades_with_a_diagnostic(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        path.write_text("{not json", encoding="utf-8")
        loaded, diagnostic = load_user_endpoints(path)
        assert loaded == ()
        assert diagnostic is not None
        assert "not valid JSON" in diagnostic.reason
        assert str(path) in diagnostic.message()

    def test_a_future_schema_degrades_rather_than_guessing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "endpoints.json"
        path.write_text(
            json.dumps({"schema_version": 99, "endpoints": []}), encoding="utf-8"
        )
        loaded, diagnostic = load_user_endpoints(path)
        assert loaded == ()
        assert diagnostic is not None
        assert "different version of MAK" in diagnostic.reason

    def test_a_non_object_file_degrades(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        path.write_text("[]", encoding="utf-8")
        loaded, diagnostic = load_user_endpoints(path)
        assert loaded == () and diagnostic is not None

    def test_one_invalid_entry_does_not_hide_the_others(
        self, tmp_path: Path
    ) -> None:
        """A hand-edited endpoint must not cost the user their whole store."""
        path = tmp_path / "endpoints.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "endpoints": [
                        to_dict(_endpoint("good")),
                        {"id": "bad", "transport": "nonsense"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        loaded, diagnostic = load_user_endpoints(path)
        assert [e.id for e in loaded] == ["good"]
        assert diagnostic is not None
        assert "bad" in diagnostic.reason

    def test_a_duplicate_id_in_the_file_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "endpoints": [to_dict(_endpoint("a")), to_dict(_endpoint("a"))],
                }
            ),
            encoding="utf-8",
        )
        loaded, diagnostic = load_user_endpoints(path)
        assert len(loaded) == 1
        assert diagnostic is not None and "duplicate id" in diagnostic.reason

    def test_no_read_path_raises(self, tmp_path: Path) -> None:
        """Startup must survive every shape of broken file."""
        for body in ("", "null", "3", '{"schema_version": 1}', "{}"):
            path = tmp_path / "e.json"
            path.write_text(body, encoding="utf-8")
            loaded, _ = load_user_endpoints(path)
            assert loaded == ()


class TestWriteSafety:
    def test_the_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        save_user_endpoints((_endpoint(),), path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_a_rewrite_keeps_the_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        save_user_endpoints((_endpoint("a"),), path)
        save_user_endpoints((_endpoint("b"),), path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_no_temp_file_is_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints.json"
        save_user_endpoints((_endpoint(),), path)
        assert not (tmp_path / "endpoints.json.tmp").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["endpoints.json"]

    def test_an_unwritable_location_raises_rather_than_lying(
        self, tmp_path: Path
    ) -> None:
        """The wizard has just told the user it saved; silence would be a lie."""
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        with pytest.raises(ConfigError, match="cannot write saved endpoints"):
            save_user_endpoints((_endpoint(),), blocked / "endpoints.json")

    def test_the_previous_file_survives_a_failed_write(
        self, tmp_path: Path
    ) -> None:
        """Atomicity: a crash mid-write must not destroy the saved endpoints.

        ``os.replace`` is restored by hand rather than through
        ``monkeypatch.undo()``: the suite's autouse isolation fixture shares one
        ``monkeypatch`` instance with the test, so an ``undo()`` here would also
        revert ``XDG_CONFIG_HOME``.
        """
        path = tmp_path / "endpoints.json"
        save_user_endpoints((_endpoint("original"),), path)
        real_replace = os.replace

        def boom(src: object, dst: object) -> None:
            raise OSError("disk full")

        os.replace = boom  # type: ignore[assignment]
        try:
            with pytest.raises(ConfigError):
                save_user_endpoints((_endpoint("replacement"),), path)
        finally:
            os.replace = real_replace  # type: ignore[assignment]
        loaded, _ = load_user_endpoints(path)
        assert [e.id for e in loaded] == ["original"]


class TestMerge:
    def test_project_and_user_endpoints_combine(self) -> None:
        merged = merge_endpoints((_endpoint("proj"),), (_endpoint("user"),))
        assert [e.id for e in merged] == ["proj", "user"]

    def test_a_clash_names_both_sources_and_refuses(self) -> None:
        """No implicit precedence: the two may point at different hosts."""
        with pytest.raises(ConfigError) as exc:
            merge_endpoints((_endpoint("gw"),), (_endpoint("gw"),))
        message = str(exc.value)
        assert "project's config" in message
        assert "saved endpoints" in message
        assert "/endpoint remove gw" in message

    def test_empty_sources_are_fine(self) -> None:
        assert merge_endpoints((), ()) == ()


class TestDefaultLocation:
    def test_the_store_lives_beside_the_other_user_state(self) -> None:
        assert endpoints_path().name == "endpoints.json"
        assert endpoints_path().parent.name == "mak"

    def test_a_saved_endpoint_reparses_through_the_yaml_parser(self) -> None:
        """One parser for both sources, so validation cannot diverge."""
        assert parse_endpoint(to_dict(_endpoint())) == _endpoint()
