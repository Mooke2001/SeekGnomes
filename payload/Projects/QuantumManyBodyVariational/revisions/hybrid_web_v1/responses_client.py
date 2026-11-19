"""Stateless Responses transport with the hosted web-search ablation enabled.

The frozen native action broker still receives only benchmark_action function
calls. Hosted web results and encrypted reasoning are preserved as Responses
items, then replayed exactly once without fabricating local tool outputs.
"""
from __future__ import annotations

from copy import deepcopy

from revisions.hybrid_agent_v1.runtime.api_client import RelayClient, inconsistent_output_usage


BRIDGE_VERSION = "openai-responses-native-broker-web/v1"
OUTPUT_ITEMS_FIELD = "responses_output_items"
WEB_ACTION_TYPES = ("search", "open_page", "find_in_page")
OUTPUT_ITEM_TYPES = ("reasoning", "message", "function_call", "web_search_call")


def _validate_web_item(item: dict, *, partial: bool = False) -> None:
    """Validate known web fields without discarding provider extension fields."""
    identifier = item.get("id")
    if (identifier is not None or not partial) and (not isinstance(identifier, str) or not identifier):
        raise ValueError("Invalid Responses web-search identifier")
    if "status" in item and (not isinstance(item["status"], str) or not item["status"]):
        raise ValueError("Invalid Responses web-search status")
    action = item.get("action")
    if action is None and partial:
        return
    if not isinstance(action, dict):
        raise ValueError("Responses web-search action must be an object")
    kind = action.get("type")
    if kind is None and partial:
        return
    if kind not in WEB_ACTION_TYPES:
        raise ValueError("Unexpected Responses web-search action type")
    for field in ("query", "url", "pattern"):
        if action.get(field) is not None and not isinstance(action[field], str):
            raise ValueError("Invalid Responses web-search action field")
    if action.get("queries") is not None and (not isinstance(action["queries"], list)
            or any(not isinstance(query, str) for query in action["queries"])):
        raise ValueError("Invalid Responses web-search queries")
    if not partial:
        required = ("url",) if kind == "open_page" else ("url", "pattern") if kind == "find_in_page" else ()
        if any(not isinstance(action.get(field), str) or not action[field] for field in required):
            raise ValueError("Missing Responses web-search action field")
    sources = action.get("sources")
    if sources is not None and (not isinstance(sources, list) or any(
            not isinstance(source, dict) or source.get("type") != "url"
            or not isinstance(source.get("url"), str) or not source["url"] for source in sources)):
        raise ValueError("Invalid Responses web-search sources")


def _web_search_metadata(raw) -> dict:
    """Describe current-response calls only; historical input is never counted."""
    output = raw.get("output") if isinstance(raw, dict) else None
    items = [deepcopy(item) for item in output if isinstance(item, dict)
             and item.get("type") == "web_search_call"] if isinstance(output, list) else []
    actions, sources, action_counts = [], [], {}
    for item in items:
        action = item.get("action")
        actions.append({"call_id": item.get("id"), "status": item.get("status"),
                        "action": deepcopy(action)})
        if isinstance(action, dict):
            kind = action.get("type")
            if isinstance(kind, str):
                action_counts[kind] = action_counts.get(kind, 0) + 1
            if isinstance(action.get("sources"), list):
                sources.extend({"call_id": item.get("id"), "action_type": kind,
                                "source": deepcopy(source)} for source in action["sources"])
    return {"web_search_call_count": len(items), "web_search_calls": items,
            "web_search_actions": actions, "web_search_action_counts": action_counts,
            "web_search_sources": sources, "web_search_source_count": len(sources)}


def _input_items(messages: list[dict]) -> list[dict]:
    if not isinstance(messages, list):
        raise ValueError("Broker messages must be a list")
    result: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Broker message must be an object")
        role = message.get("role")
        if role == "assistant" and OUTPUT_ITEMS_FIELD in message:
            items = message[OUTPUT_ITEMS_FIELD]
            if (not isinstance(items, list) or any(not isinstance(item, dict)
                    or item.get("type") not in OUTPUT_ITEM_TYPES for item in items)):
                raise ValueError("Invalid replayable Responses output items")
            for item in items:
                if item["type"] == "web_search_call":
                    _validate_web_item(item, partial=True)
                elif item["type"] == "function_call" and item.get("name") != "benchmark_action":
                    raise ValueError("Unregistered Responses function in history")
            # Includes every encrypted reasoning, hosted result and local call.
            # Normalized content/tool calls must not be appended a second time.
            result.extend(deepcopy(items))
            continue
        content = message.get("content")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id or not isinstance(content, str):
                raise ValueError("Tool messages need a call ID and string output")
            result.append({"type": "function_call_output", "call_id": call_id, "output": content})
            continue
        if role not in ("system", "developer", "user", "assistant"):
            raise ValueError("Unsupported broker message role")
        if content is not None and not isinstance(content, str):
            raise ValueError("This text benchmark requires string message content")
        if content:
            result.append({"role": role, "content": content})
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if (not isinstance(function, dict) or not isinstance(call.get("id"), str)
                        or not call["id"] or function.get("name") != "benchmark_action"
                        or not isinstance(function.get("arguments"), str)):
                    raise ValueError("Invalid native broker call in history")
                result.append({"type": "function_call", "call_id": call["id"],
                               "name": function["name"], "arguments": function["arguments"]})
        # OpenAI rejects empty text; preserve the frozen empty-fallback behavior.
    return result


def responses_payload(payload: dict) -> dict:
    """Keep the native plan and default reasoning; add only hosted web_search."""
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        raise ValueError("A model ID and native request object are required")
    caps = [payload[name] for name in ("max_completion_tokens", "max_tokens") if name in payload]
    if (not caps or any(type(value) is not int or value <= 0 for value in caps)
            or len(set(caps)) != 1):
        raise ValueError("One unambiguous positive output cap is required")
    tools = []
    for item in payload.get("tools") or []:
        function = item.get("function") if isinstance(item, dict) else None
        if (not isinstance(function, dict) or item.get("type") != "function"
                or function.get("name") != "benchmark_action"
                or not isinstance(function.get("parameters"), dict)):
            raise ValueError("Only the registered benchmark_action client function is allowed")
        tools.append({"type": "function", "name": function["name"],
                      "description": function.get("description", ""),
                      "parameters": deepcopy(function["parameters"]), "strict": False})
    if len(tools) > 1:
        raise ValueError("Exactly one registered broker function is supported")
    choice = payload.get("tool_choice", "auto" if tools else "none")
    if choice not in ("auto", "none"):
        raise ValueError("Unsupported broker tool choice")
    tools.append({"type": "web_search", "external_web_access": True})
    return {"model": payload["model"], "input": _input_items(payload.get("messages")),
            "tools": tools, "tool_choice": choice, "max_output_tokens": caps[0],
            "parallel_tool_calls": False, "store": False, "stream": False,
            "include": ["reasoning.encrypted_content", "web_search_call.action.sources"]}


def normalized_usage(usage):
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("Invalid Responses usage object")
    result = {}
    for source, target in (("input_tokens", "prompt_tokens"),
                           ("output_tokens", "completion_tokens"),
                           ("total_tokens", "total_tokens"),
                           ("input_tokens_details", "prompt_tokens_details"),
                           ("output_tokens_details", "completion_tokens_details")):
        if source in usage:
            result[target] = deepcopy(usage[source])
    return result


def _normalized_response(raw: dict) -> tuple[dict, str, list[dict], str]:
    status = raw.get("status")
    if status not in ("completed", "incomplete"):
        raise ValueError("Responses result is not a completed or incomplete final response")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses output must be a list")
    text, calls = [], []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Responses output item must be an object")
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "web_search_call":
            _validate_web_item(item, partial=status == "incomplete")
        elif kind == "message":
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("Responses message content must be a list")
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("Responses message block must be an object")
                value = block.get("text") if block.get("type") == "output_text" else block.get("refusal")
                if isinstance(value, str):
                    text.append(value)
        elif kind == "function_call":
            if status != "completed":
                continue
            if (not isinstance(item.get("call_id"), str) or not item["call_id"]
                    or item.get("name") != "benchmark_action"
                    or not isinstance(item.get("arguments"), str)
                    or item.get("status", "completed") != "completed"):
                raise ValueError("Invalid complete Responses function call")
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
        else:
            raise ValueError("Unexpected Responses output item type")
    if status == "incomplete":
        reason = (raw.get("incomplete_details") or {}).get("reason")
        finish = "length" if reason == "max_output_tokens" else "content_filter" if reason == "content_filter" else "stop"
        calls = []
        # A complete-looking action or partial text must never run locally.
        content = ""
    else:
        finish, content = ("tool_calls" if calls else "stop"), "".join(text)
    message = {"role": "assistant", "content": content or None, "tool_calls": calls,
               OUTPUT_ITEMS_FIELD: deepcopy(output)}
    normalized = {"id": raw.get("id"), "object": "chat.completion.bridge",
                  "model": raw.get("model"), "usage": normalized_usage(raw.get("usage")),
                  "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    return normalized, content, calls, finish


class ResponsesClient(RelayClient):
    """RelayClient-compatible lifecycle with non-stream Responses and web I/O."""

    def _metadata(self, headers):
        metadata = super()._metadata(headers)
        for kind in ("limit", "remaining", "reset"):
            for unit in ("requests", "tokens"):
                name = f"x-ratelimit-{kind}-{unit}"
                if name in headers:
                    metadata[name] = self.redact(str(headers[name]))[:200]
        return metadata

    def _request(self, method, endpoint, payload=None, *, stream=False,
                 request_timeout_seconds=None):
        if method != "POST" or endpoint != "/chat/completions":
            return super()._request(method, endpoint, payload, stream=stream,
                                    request_timeout_seconds=request_timeout_seconds)
        wire = responses_payload(payload)
        record = super()._request("POST", "/responses", wire, stream=False,
                                  request_timeout_seconds=request_timeout_seconds)
        raw = deepcopy(record.get("response"))
        record.update(api_protocol="responses", bridge_version=BRIDGE_VERSION,
                      normalization_applied=False, broker_requested_stream=bool(stream),
                      actual_response_stream=False, raw_response=raw,
                      raw_usage=deepcopy(raw.get("usage")) if isinstance(raw, dict) else None,
                      **_web_search_metadata(raw))
        if not record.get("ok"):
            return record
        if isinstance(raw, dict) and (raw.get("status") == "failed" or raw.get("error")):
            record.update(ok=False, error="ResponsesFailure",
                          error_detail="The provider returned a failed Responses result")
            return record
        try:
            if not isinstance(raw, dict):
                raise ValueError("Responses result must be an object")
            response, content, calls, finish = _normalized_response(raw)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            record.update(ok=False, error="InvalidResponsesSchema", error_detail=str(error))
            return record
        record.update(response=response, content=content, tool_calls_observed=bool(calls),
                      returned_model=raw.get("model"), usage=response["usage"],
                      finish_reason=finish, normalization_applied=True,
                      response_status=raw["status"], responses_output_items=deepcopy(raw["output"]))
        if inconsistent_output_usage(response["usage"]):
            record.update(usage_accounting_ambiguous=True,
                          usage_accounting_warning="reasoning_detail_exceeds_reported_completion")
            if self.reasoning_usage_policy == "included":
                record.update(ok=False, error="InconsistentUsage",
                              error_detail="provider reasoning_tokens exceeds completion_tokens")
        return self.redact(record)


__all__ = ["ResponsesClient", "responses_payload", "normalized_usage", "BRIDGE_VERSION", "OUTPUT_ITEMS_FIELD"]
