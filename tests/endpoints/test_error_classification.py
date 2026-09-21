"""Wave 24: the incident, frozen as fixtures, plus everything that must not move.

Every envelope in ``_LIVE`` below was captured from the real OpenRouter API on
2026-09-21 against ``inclusionai/ling-3.0-flash-vl:free`` and
``google/gemma-4-31b-it:free``. They are verbatim, including the nested
JSON-encoded ``metadata.raw``, because the whole bug was that MAK read the
outer envelope and the sentence that mattered was two levels in.

The negative cases matter as much as the positive ones. A classifier that
descends the ladder on an auth failure, a quota breach or a schema MAK authored
wrong turns one clear provider error into a confusing second failure one rung
down — which is exactly what the pre-Wave-22 substring matching did.
"""

from __future__ import annotations

import json

import pytest

from mak.endpoints.error_classification import (
    RejectionKind,
    classify_rejection,
    normalize,
)


class ApiError(Exception):
    """An SDK-shaped error: message, HTTP status, and the parsed error body.

    Mirrors ``openai.APIStatusError`` closely enough for classification —
    ``status_code`` and ``body`` are the two attributes the openai SDK actually
    attaches, and both were confirmed present on the live exceptions.
    """

    def __init__(
        self, message: str, status_code: int, body: object | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _openrouter(status: int, message: str, metadata: dict[str, object]) -> ApiError:
    """Build an error in OpenRouter's real envelope shape.

    ``exc.body`` is the error object itself (no ``error`` wrapper), which is
    what the live SDK exception carried, and ``str(exc)`` renders the SDK's
    ``Error code: N - {...}`` form so the ``str``-only fallback path is
    exercised too.
    """
    body = {"message": message, "code": status, "metadata": metadata}
    return ApiError(f"Error code: {status} - {{'error': {body!r}}}", status, body)


# ---------------------------------------------------------------------------
# The live envelopes. Verbatim from the incident and the Wave 24 probes.
# ---------------------------------------------------------------------------

# response_format={"type": "json_object"} -> Novita, hyphenated spelling.
# This is the one 0.8.1 missed.
LING_JSON_OBJECT = _openrouter(
    400,
    "Provider returned error",
    {
        "raw": json.dumps(
            {
                "code": 400,
                "reason": "INVALID_REQUEST_BODY",
                "message": (
                    "model: inclusionai/ling-3.0-flash-vl does not support "
                    "feature: structured-outputs"
                ),
                "metadata": {},
            }
        ),
        "provider_name": "Novita",
        "is_byok": False,
    },
)

# response_format json_schema -> Novita, space-separated spelling.
LING_JSON_SCHEMA = _openrouter(
    400,
    "Provider returned error",
    {
        "raw": json.dumps(
            {
                "code": 400,
                "reason": "INVALID_REQUEST_BODY",
                "message": "model features structured outputs not support",
                "metadata": {},
            }
        ),
        "provider_name": "Novita",
        "is_byok": False,
    },
)

# json_schema + provider.require_parameters -> 404, MAK's own guard filtered
# every route away. Not a 400, which is why the Wave 24 plan could not reuse
# the format gate for it.
ROUTING_GUARD_404 = _openrouter(
    404,
    "No endpoints found that can handle the requested parameters. To learn "
    "more about provider routing, visit: "
    "https://openrouter.ai/docs/guides/routing/provider-selection",
    {
        "routing_funnel": [{"step": "Initial Endpoints", "endpoint_count": 1}],
        "failed_routing_step": "Filter by Parameters",
    },
)

UNKNOWN_MODEL = ApiError(
    "Error code: 400 - nope/does-not-exist-xyz is not a valid model ID",
    400,
    {"message": "nope/does-not-exist-xyz is not a valid model ID", "code": 400},
)

UPSTREAM_QUOTA = _openrouter(
    429,
    "Provider returned error",
    {
        "raw": (
            "google/gemma-4-31b-it:free is temporarily rate-limited upstream. "
            "Please retry shortly, or add your own key to accumulate your "
            "rate limits: https://openrouter.ai/settings/integrations"
        ),
        "provider_name": "Google AI Studio",
        "is_byok": False,
        "provider_error_code": "429",
    },
)

BAD_CREDENTIAL = ApiError(
    "Error code: 401 - User not found.",
    401,
    {"message": "User not found.", "code": 401},
)


class TestNormalization:
    """Separator runs collapse, so one marker matches every real spelling."""

    @pytest.mark.parametrize(
        "text",
        [
            "structured outputs",
            "structured-outputs",
            "structured_outputs",
            "Structured Outputs",
            "STRUCTURED--OUTPUTS",
        ],
    )
    def test_every_spelling_normalizes_alike(self, text: str) -> None:
        """The five spellings observed across providers become one token pair."""
        assert normalize(text) == "structured outputs"

    def test_response_format_field_name_normalizes(self) -> None:
        """``response_format`` and ``response format`` are one marker."""
        assert normalize("response_format") == normalize("Response Format")

    def test_unicode_is_folded_before_matching(self) -> None:
        """A provider echoing full-width user content still classifies."""
        assert normalize("ｓｔｒｕｃｔｕｒｅｄ　ｏｕｔｐｕｔｓ") == "structured outputs"


class TestTheIncident:
    """Both observed Novita spellings must descend the ladder."""

    def test_hyphenated_spelling_is_a_format_rejection(self) -> None:
        """The 0.8.1 bug: this exact envelope was not recognized.

        0.8.1 matched the literal ``"structured outputs"`` with a space, so the
        hyphenated ``structured-outputs`` fell through, the adapter never
        descended, and the scheduler re-dispatched the task until it failed.
        """
        analysis = classify_rejection(LING_JSON_OBJECT)
        assert analysis.kind is RejectionKind.FORMAT_UNSUPPORTED
        assert analysis.status == 400

    def test_spaced_spelling_is_a_format_rejection(self) -> None:
        """The spelling 0.8.1 did handle keeps working."""
        assert classify_rejection(LING_JSON_SCHEMA).is_format_rejection is True

    def test_the_literal_0_8_1_marker_really_did_miss_one(self) -> None:
        """Pins *why* a new design was needed, not just that one exists.

        Without this the regression is invisible: both envelopes now pass, so
        nothing would show that the old rule could only ever catch one of them.
        """
        old_marker = "structured outputs"
        assert old_marker in str(LING_JSON_SCHEMA).lower()
        assert old_marker not in str(LING_JSON_OBJECT).lower()

    def test_the_upstream_provider_is_reported(self) -> None:
        """Novita is named, because that is what makes the failure explicable."""
        assert classify_rejection(LING_JSON_OBJECT).provider_name == "Novita"

    def test_the_reason_is_the_upstream_sentence_not_the_wrapper(self) -> None:
        """"Provider returned error" tells a user nothing; the inner one does."""
        reason = classify_rejection(LING_JSON_OBJECT).reason
        assert "structured-outputs" in reason
        assert reason != "Provider returned error"


class TestRoutingGuard:
    """The guard's own failure is a distinct outcome with a distinct recovery."""

    def test_the_404_is_a_routing_rejection(self) -> None:
        """It is not a format rejection: the provider never saw the request."""
        analysis = classify_rejection(ROUTING_GUARD_404)
        assert analysis.kind is RejectionKind.ROUTING_PARAMETERS
        assert analysis.status == 404

    def test_a_routing_rejection_is_not_a_format_rejection(self) -> None:
        """The two must not be conflated — dropping the guard is the fix."""
        analysis = classify_rejection(ROUTING_GUARD_404)
        assert analysis.is_routing_rejection is True
        assert analysis.is_format_rejection is False

    def test_the_machine_readable_step_alone_is_enough(self) -> None:
        """Wording is OpenRouter's to change; ``failed_routing_step`` is data."""
        exc = _openrouter(
            404, "reworded entirely", {"failed_routing_step": "Filter by Parameters"}
        )
        assert classify_rejection(exc).is_routing_rejection is True

    def test_an_unrelated_404_propagates(self) -> None:
        """A plain not-found has nothing to do with parameter routing."""
        exc = ApiError("Error code: 404 - not found", 404, {"message": "not found"})
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION


class TestNegativeCases:
    """Everything that is not a capability claim keeps its own failure."""

    @pytest.mark.parametrize(
        ("label", "exc"),
        [
            ("unknown model", UNKNOWN_MODEL),
            ("upstream quota", UPSTREAM_QUOTA),
            ("bad credential", BAD_CREDENTIAL),
        ],
    )
    def test_live_negatives_propagate(self, label: str, exc: ApiError) -> None:
        """All three captured live. None is answered by a weaker reply shape."""
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION, label

    def test_an_invalid_schema_mak_authored_propagates(self) -> None:
        """A MAK defect must surface, not be hidden behind a quieter rung.

        This body names ``response_format`` *and* is a 400, so the format gate
        alone would match it. The invalid-schema veto is what keeps it visible.
        """
        exc = ApiError(
            "Error code: 400 - Invalid schema for response_format "
            "'task_result': 'additionalProperties' is required to be supplied "
            "and to be false.",
            400,
            {
                "error": {
                    "message": (
                        "Invalid schema for response_format 'task_result': "
                        "'additionalProperties' is required to be supplied and "
                        "to be false."
                    )
                }
            },
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION

    def test_a_statusless_error_is_never_a_rejection(self) -> None:
        """A transport failure has no opinion about the request body.

        Even when its message happens to name the format — a proxy can echo the
        request into a connection-error string.
        """
        assert (
            classify_rejection(
                RuntimeError("connection reset while sending response_format")
            ).kind
            is RejectionKind.NOT_A_REJECTION
        )

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_5xx_propagates_even_naming_the_format(self, status: int) -> None:
        """The server failed. Asking it more quietly does not help."""
        exc = ApiError(
            f"Error code: {status} - response_format is not supported", status
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION

    def test_a_context_limit_propagates(self) -> None:
        """A 400 about length is not a 400 about capability."""
        exc = ApiError(
            "Error code: 400 - This model's maximum context length is 8192 "
            "tokens, however you requested 9000.",
            400,
            {"error": {"message": "maximum context length is 8192 tokens"}},
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION

    def test_a_safety_rejection_propagates(self) -> None:
        """Content moderation is not a capability gap."""
        exc = ApiError(
            "Error code: 400 - Your request was rejected by our safety system",
            400,
            {"error": {"message": "rejected by our safety system"}},
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION

    def test_naming_the_format_without_a_capability_claim_propagates(self) -> None:
        """Both halves of the gate are required, not either one."""
        exc = ApiError(
            "Error code: 400 - response_format must be an object", 400
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION

    def test_an_unsupported_parameter_that_is_not_the_format_propagates(self) -> None:
        """"unsupported" alone used to match this and drop schema enforcement."""
        exc = ApiError(
            "Error code: 400 - Unsupported parameter: 'logit_bias'",
            400,
            {"error": {"message": "Unsupported parameter: 'logit_bias'"}},
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION

    def test_an_unsupported_model_propagates(self) -> None:
        """An unsupported *model* is not an unsupported *format*."""
        exc = ApiError(
            "Error code: 400 - The model 'gpt-9' is not supported",
            400,
            {"error": {"message": "The model 'gpt-9' is not supported"}},
        )
        assert classify_rejection(exc).kind is RejectionKind.NOT_A_REJECTION


class TestBodyShapes:
    """The parse must survive every envelope shape the SDKs actually produce."""

    def test_the_error_wrapper_form_is_read(self) -> None:
        """Cloud OpenAI nests under ``error``; OpenRouter does not."""
        exc = ApiError(
            "Error code: 400",
            400,
            {"error": {"message": "json_schema is not supported by this model"}},
        )
        assert classify_rejection(exc).is_format_rejection is True

    def test_metadata_raw_as_a_nested_object_is_read(self) -> None:
        """``raw`` is usually a JSON string, but an object form must work too."""
        exc = _openrouter(
            400,
            "Provider returned error",
            {"raw": {"message": "model does not support structured outputs"}},
        )
        assert classify_rejection(exc).is_format_rejection is True

    def test_malformed_metadata_raw_degrades_to_the_outer_text(self) -> None:
        """A truncated JSON string must not crash the classifier."""
        exc = _openrouter(
            400,
            "response_format is not supported",
            {"raw": '{"message": "truncated'},
        )
        assert classify_rejection(exc).is_format_rejection is True

    def test_a_status_on_the_response_rather_than_the_exception(self) -> None:
        """Some SDKs put the status only on the response object."""

        class Response:
            status_code = 400

        class Exc(Exception):
            response = Response()
            body = {"error": {"message": "structured outputs not supported"}}

        assert classify_rejection(Exc("boom")).is_format_rejection is True

    def test_a_body_that_will_not_parse_is_simply_no_evidence(self) -> None:
        """``response.json()`` raising must not become a classification error."""

        class Response:
            status_code = 400

            def json(self) -> object:
                raise ValueError("not json")

        class Exc(Exception):
            response = Response()

        exc = Exc("Error code: 400 - json mode is not supported here")
        assert classify_rejection(exc).is_format_rejection is True

    def test_str_only_exceptions_still_classify(self) -> None:
        """The compatibility fallback: no body at all, message only.

        This is the shape every existing adapter test double uses, so the path
        has to keep working or Wave 22's suite would be rewritten for nothing.
        """
        exc = ApiError("model features structured outputs not support", 400)
        assert classify_rejection(exc).is_format_rejection is True

    def test_a_deeply_nested_message_is_still_found(self) -> None:
        """Bounded-depth walk, because this envelope has been reshaped before."""
        exc = ApiError(
            "Error code: 400",
            400,
            {"error": {"metadata": {"upstream": {"detail": {
                "message": "response_format is unsupported"
            }}}}},
        )
        assert classify_rejection(exc).is_format_rejection is True

    def test_a_self_referential_body_terminates(self) -> None:
        """A looping structure costs bounded work, not a hang."""
        loop: dict[str, object] = {"message": "structured outputs not supported"}
        loop["self"] = loop
        exc = ApiError("Error code: 400", 400, loop)
        assert classify_rejection(exc).kind in set(RejectionKind)

    def test_no_body_and_no_status_is_not_a_rejection(self) -> None:
        """The most degenerate input still yields a defined verdict."""
        assert (
            classify_rejection(Exception("something went wrong")).kind
            is RejectionKind.NOT_A_REJECTION
        )


class TestReasonIsSafeToLog:
    """The raw body is read in memory and never handed to a log line."""

    def test_a_bearer_token_echoed_in_the_body_is_redacted(self) -> None:
        """Provider errors do echo request headers; this one goes to a log."""
        exc = _openrouter(
            400,
            "Provider returned error",
            {
                "raw": json.dumps(
                    {
                        "message": (
                            "structured outputs not supported (sent "
                            "sk-or-v1-abcdef123456)"
                        )
                    }
                )
            },
        )
        analysis = classify_rejection(exc)
        assert analysis.is_format_rejection is True
        assert "sk-or-v1-abcdef123456" not in analysis.reason
        assert "[redacted]" in analysis.reason

    def test_the_reason_is_bounded(self) -> None:
        """A provider echoing the whole prompt must not fill the log file."""
        exc = _openrouter(
            400,
            "Provider returned error",
            {
                "raw": json.dumps(
                    {"message": "structured outputs not supported " + "x" * 5000}
                )
            },
        )
        assert len(classify_rejection(exc).reason) <= 200
