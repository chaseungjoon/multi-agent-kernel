"""Wave 22.8: any credential variable name, and a .env that survives a save."""

from __future__ import annotations

import os

import cli.core.api_keys as api_keys
import pytest

from mak.config import user_config_dir
from mak.core.exceptions import ConfigError
from mak.endpoints.types import EndpointConfig, EndpointHeaderConfig, Transport


def _write(body: str) -> None:
    path = user_config_dir() / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _read() -> str:
    return (user_config_dir() / ".env").read_text(encoding="utf-8")


class TestPreservation:
    def test_a_comment_survives(self) -> None:
        _write("# my notes\nOPENAI_API_KEY=old\n")
        api_keys.save_keys({"OPENAI_API_KEY": "new"})
        assert "# my notes" in _read()

    def test_an_unrelated_variable_survives(self) -> None:
        """The bug that per-endpoint credentials would have made fatal."""
        _write("SOME_OTHER_KEY=keep-me\n")
        api_keys.save_keys({"OPENAI_API_KEY": "sk-new"})
        body = _read()
        assert "SOME_OTHER_KEY=keep-me" in body
        assert "OPENAI_API_KEY=sk-new" in body

    def test_two_endpoint_keys_coexist_across_separate_saves(self) -> None:
        api_keys.save_keys({"SERVICE_A_KEY": "a"})
        api_keys.save_keys({"SERVICE_B_KEY": "b"})
        body = _read()
        assert "SERVICE_A_KEY=a" in body
        assert "SERVICE_B_KEY=b" in body

    def test_a_known_name_is_updated_in_place_not_appended(self) -> None:
        _write("# head\nOPENAI_API_KEY=old\n# tail\n")
        api_keys.save_keys({"OPENAI_API_KEY": "new"})
        lines = [ln for ln in _read().splitlines() if ln]
        assert lines == ["# head", "OPENAI_API_KEY=new", "# tail"]

    def test_blank_lines_are_preserved(self) -> None:
        _write("A_KEY=1\n\nB_KEY=2\n")
        api_keys.save_keys({"A_KEY": "9"})
        assert _read() == "A_KEY=9\n\nB_KEY=2\n"


class TestRemoval:
    def test_an_empty_value_removes_the_line(self) -> None:
        _write("OPENAI_API_KEY=sk-old\nOTHER=x\n")
        api_keys.save_keys({"OPENAI_API_KEY": ""})
        body = _read()
        assert "OPENAI_API_KEY" not in body
        assert "OTHER=x" in body

    def test_removal_also_clears_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-old")
        api_keys.save_keys({"OPENAI_API_KEY": ""})
        assert "OPENAI_API_KEY" not in os.environ

    def test_a_removed_key_is_not_stored_as_blank(self) -> None:
        """'No key' and 'an empty key' must stay distinguishable."""
        api_keys.save_keys({"SOME_KEY": "v"})
        api_keys.save_keys({"SOME_KEY": ""})
        assert "SOME_KEY=" not in _read()


class TestArbitraryNames:
    def test_any_valid_name_round_trips(self) -> None:
        api_keys.save_keys({"MY_GATEWAY_TOKEN": "tok"})
        assert api_keys.load_keys(("MY_GATEWAY_TOKEN",))["MY_GATEWAY_TOKEN"] == "tok"

    def test_an_invalid_name_is_refused_before_any_write(self) -> None:
        _write("OPENAI_API_KEY=untouched\n")
        with pytest.raises(ConfigError, match="environment variable NAME"):
            api_keys.save_keys({"not a name": "x"})
        assert _read() == "OPENAI_API_KEY=untouched\n"

    def test_a_pasted_key_as_a_name_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="never stores the key"):
            api_keys.save_keys({"sk-live-abc": "x"})

    def test_load_all_stored_returns_names_mak_was_never_told_about(self) -> None:
        _write("# c\nWEIRD_NAME=value\nBLANK=\n")
        stored = api_keys.load_all_stored()
        assert stored == {"WEIRD_NAME": "value"}

    def test_key_names_for_includes_every_endpoint_secret(self) -> None:
        endpoint = EndpointConfig(
            id="gw",
            transport=Transport.OPENAI_CHAT,
            base_url="https://gw/v1",
            api_key_env="GW_KEY",
            headers=(
                EndpointHeaderConfig(name="X-Public", value="mak"),
                EndpointHeaderConfig(name="X-Token", value_env="GW_TOKEN"),
            ),
        )
        names = api_keys.key_names_for([endpoint])
        assert names[:3] == api_keys.KEY_NAMES
        assert "GW_KEY" in names
        assert "GW_TOKEN" in names

    def test_key_names_for_does_not_duplicate(self) -> None:
        endpoint = EndpointConfig(
            id="gw",
            transport=Transport.OPENAI_CHAT,
            base_url="https://gw/v1",
            api_key_env="OPENAI_API_KEY",
        )
        names = api_keys.key_names_for([endpoint])
        assert names.count("OPENAI_API_KEY") == 1


class TestSafety:
    def test_the_file_stays_owner_only(self) -> None:
        api_keys.save_keys({"OPENAI_API_KEY": "sk"})
        path = user_config_dir() / ".env"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_a_rewrite_keeps_the_mode(self) -> None:
        api_keys.save_keys({"OPENAI_API_KEY": "sk"})
        api_keys.save_keys({"OPENAI_API_KEY": "sk2"})
        path = user_config_dir() / ".env"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_no_temp_file_is_left_behind(self) -> None:
        api_keys.save_keys({"OPENAI_API_KEY": "sk"})
        assert not (user_config_dir() / ".env.tmp").exists()

    def test_the_previous_file_survives_a_failed_write(self) -> None:
        """Atomicity: a crash mid-write must not destroy the stored keys.

        ``os.replace`` is patched and restored by hand rather than through
        ``monkeypatch.undo()``: the suite's autouse isolation fixture shares one
        ``monkeypatch`` instance with the test, so an ``undo()`` here would also
        revert ``XDG_CONFIG_HOME`` and point the next read at the developer's
        real key file.
        """
        api_keys.save_keys({"OPENAI_API_KEY": "original"})
        real_replace = os.replace

        def boom(src: object, dst: object) -> None:
            raise OSError("disk full")

        os.replace = boom  # type: ignore[assignment]
        try:
            with pytest.raises(ConfigError, match="cannot write API keys"):
                api_keys.save_keys({"OPENAI_API_KEY": "replacement"})
        finally:
            os.replace = real_replace  # type: ignore[assignment]
        assert "OPENAI_API_KEY=original" in _read()


class TestPrecedence:
    def test_an_exported_variable_beats_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api_keys.save_keys({"OPENAI_API_KEY": "from-file"})
        monkeypatch.setenv("OPENAI_API_KEY", "from-env")
        assert api_keys.load_keys()["OPENAI_API_KEY"] == "from-env"

    def test_load_keys_defaults_to_the_built_in_three(self) -> None:
        assert set(api_keys.load_keys()) == set(api_keys.KEY_NAMES)

    def test_an_unset_requested_name_reads_as_empty(self) -> None:
        assert api_keys.load_keys(("NEVER_SET",))["NEVER_SET"] == ""
