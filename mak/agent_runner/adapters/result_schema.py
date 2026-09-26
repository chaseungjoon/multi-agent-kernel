"""The ``TaskResult`` wire schema, rendered in each provider's dialect.

Every structured-output backend has to describe the same contract — the five
``TaskResult`` keys an agent may return — and every one of them spells it
slightly differently. Before this module the schema was written out twice
verbatim (Anthropic's ``input_schema``, Gemini's ``parameters``), differing only
in how a nullable ``error`` is expressed. The local transports need two more
copies, and four hand-maintained copies of one contract is where a
contract drifts: a field description improved in one place and not the other
teaches two different things to two models.

So the property names, their descriptions, and the required set live here once,
and :func:`result_schema` renders them per dialect. The dialects differ only
where the consuming validator genuinely differs:

- **anthropic** — JSON Schema; nullable via a ``type`` union.
- **gemini** — the OpenAPI subset the GenAI SDK accepts; nullable via
  ``nullable: true``, which is not JSON Schema at all.
- **openai** — *strict* JSON Schema: ``additionalProperties: false`` on every
  object and **every** property listed in ``required``. That is what OpenAI
  strict mode demands, and also what vLLM's and llama.cpp's guided decoding
  demand, so optional fields become nullable types rather than omissions.
- **ollama** — plain JSON Schema handed to llama.cpp's grammar converter, the
  narrowest of the four: no ``type`` unions and no ``anyOf`` survive it, so
  ``error`` is a plain optional string and only the two genuinely-required
  fields are listed.
"""

from __future__ import annotations

from typing import Any

RESULT_TOOL_NAME = "submit_task_result"

# A template, not a constant: Anthropic declares a "tool" and Gemini a
# "function", and the sentence reads to the model either way. The noun is the
# only word the two dialects disagree on.
RESULT_TOOL_DESCRIPTION = (
    "Report the structured outcome of the assigned MAK task. You MUST call "
    "this {noun} exactly once as your final action."
)

TASK_ID_DESCRIPTION = "The task_id from the received task bundle."

SUCCESS_DESCRIPTION = "True if the task was completed successfully."

FRAGMENTS_DESCRIPTION = (
    "For every node you changed, an object with its node_id and the FULL "
    "rewritten source of that node (not a diff)."
)

NODE_ID_DESCRIPTION = (
    "A node id copied verbatim from the bundle's target_nodes. Never a "
    "narrower or invented id."
)

NEW_SOURCE_DESCRIPTION = "The complete rewritten source of the node."

NO_CHANGES_DESCRIPTION = (
    "True only when you inspected every target and found nothing to change. "
    "Never true alongside modified_fragments."
)

ERROR_DESCRIPTION = "Failure reason when success is false, else null."

# The two fields a result is meaningless without: which task this answers, and
# whether it worked. Everything else is optional by design — a successful no-op
# carries no fragments and no error.
REQUIRED_FIELDS: tuple[str, ...] = ("task_id", "success")

# Every property name in the contract, in wire order. Exposed so a test can
# assert the four dialects agree without re-listing them.
PROPERTY_NAMES: tuple[str, ...] = (
    "task_id",
    "success",
    "modified_fragments",
    "no_changes_required",
    "error",
)

DIALECTS: tuple[str, ...] = ("anthropic", "gemini", "openai", "ollama")


def _fragment_item(*, strict: bool) -> dict[str, Any]:
    """Return the schema for one ``modified_fragments`` entry."""
    item: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": NODE_ID_DESCRIPTION},
            "new_source": {
                "type": "string",
                "description": NEW_SOURCE_DESCRIPTION,
            },
        },
        "required": ["node_id", "new_source"],
    }
    if strict:
        item["additionalProperties"] = False
    return item


def _error_property(dialect: str) -> dict[str, Any]:
    """Return the ``error`` property in ``dialect``'s spelling of 'nullable'."""
    if dialect == "gemini":
        return {
            "type": "string",
            "nullable": True,
            "description": ERROR_DESCRIPTION,
        }
    if dialect == "ollama":
        # llama.cpp's grammar converter rejects a type union, and an optional
        # field is expressed by leaving it out of ``required`` — which it is.
        return {"type": "string", "description": ERROR_DESCRIPTION}
    return {"type": ["string", "null"], "description": ERROR_DESCRIPTION}


def result_schema(dialect: str) -> dict[str, Any]:
    """Return the ``TaskResult`` schema in one provider's dialect.

    ``dialect`` is one of :data:`DIALECTS`; anything else raises ``ValueError``,
    because a silently-wrong schema is a model that answers in a shape MAK
    cannot decode.
    """
    if dialect not in DIALECTS:
        raise ValueError(
            f"unknown result-schema dialect {dialect!r}; "
            f"known dialects: {', '.join(DIALECTS)}"
        )
    strict = dialect == "openai"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": TASK_ID_DESCRIPTION},
            "success": {"type": "boolean", "description": SUCCESS_DESCRIPTION},
            "modified_fragments": {
                "type": "array",
                "description": FRAGMENTS_DESCRIPTION,
                "items": _fragment_item(strict=strict),
            },
            "no_changes_required": {
                "type": "boolean",
                "description": NO_CHANGES_DESCRIPTION,
            },
            "error": _error_property(dialect),
        },
        # Strict mode requires *every* property to be required, so optionality
        # is carried by the nullable types above instead of by omission.
        "required": list(PROPERTY_NAMES) if strict else list(REQUIRED_FIELDS),
    }
    if strict:
        schema["additionalProperties"] = False
    return schema
