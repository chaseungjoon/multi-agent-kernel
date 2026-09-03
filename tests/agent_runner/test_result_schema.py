"""Tests for the one TaskResult schema rendered in four provider dialects."""

from typing import Any

import pytest

from mak.agent_runner.adapters.result_schema import (
    DIALECTS,
    PROPERTY_NAMES,
    REQUIRED_FIELDS,
    result_schema,
)


def _walk(schema: Any) -> list[Any]:
    """Yield every nested mapping/sequence node of a schema, itself included."""
    found = [schema]
    if isinstance(schema, dict):
        for value in schema.values():
            found.extend(_walk(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_walk(value))
    return found


class TestDialectsAgree:
    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_property_names_are_the_same_everywhere(self, dialect: str) -> None:
        schema = result_schema(dialect)
        assert tuple(schema["properties"]) == PROPERTY_NAMES

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_task_id_and_success_are_always_required(self, dialect: str) -> None:
        assert set(REQUIRED_FIELDS) <= set(result_schema(dialect)["required"])

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_fragments_ask_for_the_full_new_source(self, dialect: str) -> None:
        # The whole point of the schema: without new_source an agent's edit can
        # never reach the node store.
        items = result_schema(dialect)["properties"]["modified_fragments"]["items"]
        assert items["properties"]["new_source"]["type"] == "string"
        assert set(items["required"]) == {"node_id", "new_source"}

    def test_descriptions_are_shared_across_dialects(self) -> None:
        descriptions = {
            dialect: {
                name: prop["description"]
                for name, prop in result_schema(dialect)["properties"].items()
            }
            for dialect in DIALECTS
        }
        first = descriptions["anthropic"]
        for dialect in DIALECTS:
            assert descriptions[dialect] == first


class TestDialectSpecifics:
    def test_anthropic_marks_error_nullable_with_a_type_union(self) -> None:
        error = result_schema("anthropic")["properties"]["error"]
        assert error["type"] == ["string", "null"]

    def test_gemini_marks_error_nullable_with_the_openapi_flag(self) -> None:
        error = result_schema("gemini")["properties"]["error"]
        assert error == {
            "type": "string",
            "nullable": True,
            "description": error["description"],
        }

    def test_openai_dialect_satisfies_strict_mode(self) -> None:
        schema = result_schema("openai")
        # Strict mode: every property required, and every object closed.
        assert list(schema["required"]) == list(PROPERTY_NAMES)
        for node in _walk(schema):
            if isinstance(node, dict) and node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])

    def test_ollama_dialect_has_no_type_unions_and_no_anyof(self) -> None:
        # llama.cpp's grammar converter is the narrowest of the four.
        schema = result_schema("ollama")
        for node in _walk(schema):
            if isinstance(node, dict):
                assert not isinstance(node.get("type"), list)
                assert "anyOf" not in node
                assert "nullable" not in node

    def test_ollama_requires_only_task_id_and_success(self) -> None:
        assert result_schema("ollama")["required"] == list(REQUIRED_FIELDS)


class TestUnknownDialect:
    def test_unknown_dialect_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown result-schema dialect"):
            result_schema("llamacpp")
