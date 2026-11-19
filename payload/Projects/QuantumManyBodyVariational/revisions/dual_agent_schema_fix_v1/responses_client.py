"""Independent compatibility fix for observed named API web-search sources.

The official web-search guide documents named live feeds in ``sources`` while
the public Responses reference still enumerates URL sources only. An actual
official HTTP-200 response also supplies {"type":"api","name":"oai-calculator"}.
This adapter accepts that precise source variant without inventing a URL or
changing any other frozen web/function validation rule. It never mutates the
legacy module, a saved record, or the raw provider output.

Documentation checked 2026-11-16:
https://developers.openai.com/api/docs/guides/tools-web-search
https://developers.openai.com/api/reference/resources/responses/methods/create
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from revisions.dual_agent_v1.runtime import RoleResponsesClient as _LegacyRoleResponsesClient
from revisions.hybrid_agent_v1.runtime.api_client import RelayClient, inconsistent_output_usage
from revisions.hybrid_web_v1 import responses_client as _legacy


VERSION = "dual-agent-api-source-schema-fix/v1"
OUTPUT_ITEMS_FIELD = _legacy.OUTPUT_ITEMS_FIELD


def _validation_web_copy(item, *, partial=False):
    """Delegate every old restriction after validating the one new variant."""
    copied = deepcopy(item)
    action = copied.get("action")
    count = 0
    if isinstance(action, dict) and action.get("sources") is not None:
        sources = action["sources"]
        if not isinstance(sources, list):
            raise ValueError("Invalid Responses web-search sources")
        legacy_sources = []
        for source in sources:
            if isinstance(source, dict) and source.get("type") == "api":
                name = source.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("An API web-search source requires a nonempty name")
                count += 1
            else:
                legacy_sources.append(source)
        # This validation-only copy is never sent or retained as provider data.
        # API entries have their own strict name check above; URL entries and
        # every other web/action field still pass the complete frozen checker.
        action["sources"] = legacy_sources
    _legacy._validate_web_item(copied, partial=partial)
    return copied, count


def _input_items(messages):
    if not isinstance(messages, list):
        raise ValueError("Broker messages must be a list")
    result = []
    for message in messages:
        if (isinstance(message, dict) and message.get("role") == "assistant"
                and OUTPUT_ITEMS_FIELD in message):
            original = message[OUTPUT_ITEMS_FIELD]
            if not isinstance(original, list):
                raise ValueError("Invalid replayable Responses output items")
            copied = deepcopy(message)
            for index, item in enumerate(original):
                if isinstance(item, dict) and item.get("type") == "web_search_call":
                    copied[OUTPUT_ITEMS_FIELD][index], _ = _validation_web_copy(item, partial=True)
            # Verify item kinds, registered function names, and all non-web
            # constraints. Then replay the original items exactly once.
            _legacy._input_items([copied])
            result.extend(deepcopy(original))
        else:
            result.extend(_legacy._input_items([message]))
    return result


def responses_payload(payload, *, allow_web=True):
    if type(allow_web) is not bool:
        raise ValueError("allow_web must be boolean")
    # The frozen builder continues to validate model, token cap and registered
    # native function. Only history validation requires the new source variant.
    if not isinstance(payload, dict):
        raise ValueError("A native request object is required")
    wire = _legacy.responses_payload({**payload, "messages": []})
    wire["input"] = _input_items(payload.get("messages"))
    if not allow_web:
        if any(item.get("type") == "web_search_call" for item in wire["input"]):
            raise ValueError("hosted web history is forbidden for the formalizer")
        wire["tools"] = [item for item in wire["tools"] if item.get("type") != "web_search"]
        wire["include"] = ["reasoning.encrypted_content"]
    return wire


def _normalized_response(raw, *, allow_web):
    if not isinstance(raw, dict):
        raise ValueError("Responses result must be an object")
    if raw.get("status") not in ("completed", "incomplete"):
        raise ValueError("Responses result is not a completed or incomplete final response")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses output must be a list")
    copied = deepcopy(raw)
    count = 0
    for index, item in enumerate(output):
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            if not allow_web:
                raise ValueError("hosted web is forbidden for the formalizer")
            copied["output"][index], observed = _validation_web_copy(
                item, partial=raw["status"] == "incomplete")
            count += observed
    response, content, calls, finish = _legacy._normalized_response(copied)
    response["choices"][0]["message"][OUTPUT_ITEMS_FIELD] = deepcopy(output)
    return response, content, calls, finish, count


def _record_sha256(record):
    data = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def normalize_saved_record(record, *, allow_web=True,
                           reasoning_usage_policy="provider_reported_completion"):
    """Return a validated deep copy of one already-received HTTP response.

    No API call, file access, budget modification, or local tool execution occurs.
    Invalid source variants and all unrelated schema problems raise ValueError.
    A transport/refusal failure cannot be promoted to success. Raw output and
    raw usage are retained exactly; normalized completion counts output once,
    with reasoning retained only as the provider's detail field.
    """
    if not isinstance(record, dict) or type(allow_web) is not bool:
        raise ValueError("A record object and boolean allow_web are required")
    if reasoning_usage_policy not in ("included", "conservative_union_bound", "provider_reported_completion"):
        raise ValueError("unknown reasoning_usage_policy")
    status = record.get("http_status")
    if type(status) is not int or not 200 <= status < 300:
        raise ValueError("Only a completed HTTP-2xx record may be normalized")
    if record.get("ok") is not True and record.get("error") != "InvalidResponsesSchema":
        raise ValueError("Only a schema normalization failure may be recovered")
    if record.get("error") not in (None, "InvalidResponsesSchema"):
        raise ValueError("An unrelated transport/provider failure cannot be recovered")
    result = deepcopy(record)
    raw = result.get("raw_response")
    if not isinstance(raw, dict):
        raise ValueError("The exact raw_response object is required")
    if raw.get("error"):
        raise ValueError("A provider failure cannot be normalized as success")
    raw_usage = raw.get("usage")
    saved_usage = result.get("raw_usage")
    if raw_usage is not None and saved_usage is not None and raw_usage != saved_usage:
        raise ValueError("raw_response usage conflicts with saved raw_usage")
    usage = raw_usage if raw_usage is not None else saved_usage
    normalization_input = deepcopy(raw)
    if raw_usage is None and saved_usage is not None:
        normalization_input["usage"] = deepcopy(saved_usage)
    response, content, calls, finish, api_sources = _normalized_response(
        normalization_input, allow_web=allow_web)
    # Normalization may use the independently retained raw_usage when needed,
    # but must not insert that fallback into the immutable raw provider object.
    previous_fix = result.get("schema_fix")
    if not isinstance(previous_fix, dict) or previous_fix.get("version") != VERSION:
        result["schema_fix"] = {"version": VERSION,
            "source_record_sha256": _record_sha256(record),
            "original_ok": record.get("ok"), "original_error": record.get("error"),
            "original_error_detail": record.get("error_detail"),
            "original_normalization_applied": record.get("normalization_applied"),
            "usage_recovered_from_raw": record.get("usage") != response["usage"],
            "api_source_count": api_sources,
            "accounting_policy": "provider completion counted once; reasoning remains a detail; driver records any old-reservation correction separately",
            "budget_modified": False, "api_request_issued": False}
    result.pop("error", None)
    result.pop("error_detail", None)
    result.update(ok=True, api_protocol="responses", bridge_version=VERSION,
        normalization_applied=True, raw_response=deepcopy(raw), raw_usage=deepcopy(usage),
        response=response, content=content, tool_calls_observed=bool(calls),
        returned_model=raw.get("model"), usage=response["usage"], finish_reason=finish,
        response_status=raw["status"], responses_output_items=deepcopy(raw["output"]),
        hosted_web_enabled=allow_web, **_legacy._web_search_metadata(raw))
    if inconsistent_output_usage(response["usage"]):
        result.update(usage_accounting_ambiguous=True,
            usage_accounting_warning="reasoning_detail_exceeds_reported_completion")
        if reasoning_usage_policy == "included":
            result.update(ok=False, error="InconsistentUsage",
                error_detail="provider reasoning_tokens exceeds completion_tokens")
    return result


class SchemaFixedRoleResponsesClient(_LegacyRoleResponsesClient):
    """Drop-in role client; only this independent version accepts API sources."""

    def _request(self, method, endpoint, payload=None, *, stream=False,
                 request_timeout_seconds=None):
        if method != "POST" or endpoint != "/chat/completions":
            return super()._request(method, endpoint, payload, stream=stream,
                request_timeout_seconds=request_timeout_seconds)
        wire = responses_payload(payload, allow_web=self.allow_web)
        record = RelayClient._request(self, "POST", "/responses", wire, stream=False,
            request_timeout_seconds=request_timeout_seconds)
        raw = deepcopy(record.get("response"))
        record.update(api_protocol="responses", bridge_version=VERSION,
            normalization_applied=False, broker_requested_stream=bool(stream),
            actual_response_stream=False, raw_response=raw,
            raw_usage=deepcopy(raw.get("usage")) if isinstance(raw, dict) else None,
            hosted_web_enabled=self.allow_web, **_legacy._web_search_metadata(raw))
        if not record.get("ok"):
            return self.redact(record)
        if isinstance(raw, dict) and (raw.get("status") == "failed" or raw.get("error")):
            record.update(ok=False, error="ResponsesFailure",
                error_detail="The provider returned a failed Responses result")
            return self.redact(record)
        try:
            record = normalize_saved_record(record, allow_web=self.allow_web,
                reasoning_usage_policy=self.reasoning_usage_policy)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            record.update(ok=False, error="InvalidResponsesSchema", error_detail=str(error))
        return self.redact(record)


# A convenient local import name; the frozen original class remains untouched.
RoleResponsesClient = SchemaFixedRoleResponsesClient

__all__ = ["SchemaFixedRoleResponsesClient", "RoleResponsesClient", "normalize_saved_record",
           "responses_payload", "VERSION", "OUTPUT_ITEMS_FIELD"]
