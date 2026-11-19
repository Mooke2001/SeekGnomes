"""Auditable non-stream Chat Completions client for both Kimi roles.

Keep the complete provider message, including reasoning_content required by
some tool-using Kimi deployments. No hosted web or implicit tools are exposed.
"""
from __future__ import annotations

from copy import deepcopy

from revisions.hybrid_agent_v1.runtime.api_client import RelayClient


VERSION = "kimi-adaptive50-chat-client/v1"


class RoleChatClient(RelayClient):
    allow_web = False

    def __init__(self, *args, allow_web=False, **kwargs):
        if allow_web is not False:
            raise ValueError("Kimi benchmark roles do not expose hosted web")
        super().__init__(*args, **kwargs)
        if self.reasoning_usage_policy != "provider_reported_completion":
            raise ValueError("Kimi roles require provider_reported_completion accounting")

    def _request(self, method, endpoint, payload=None, *, stream=False,
                 request_timeout_seconds=None):
        wire = deepcopy(payload)
        if method == "POST":
            if endpoint != "/chat/completions" or not isinstance(wire, dict):
                raise ValueError("Kimi roles only support Chat Completions POST requests")
            tools = wire.get("tools", [])
            if not isinstance(tools, list) or any(
                not isinstance(item, dict) or item.get("type") != "function"
                or not isinstance(item.get("function"), dict)
                or item["function"].get("name") != "benchmark_action" for item in tools
            ):
                raise ValueError("Only the benchmark_action native function may be exposed")
            # The frozen RelayClient's streaming collector retains raw tool
            # fragments; non-streaming preserves complete native calls and the
            # full reasoning field without inventing an SSE normalization.
            wire["stream"] = False
            wire.pop("stream_options", None)
        record = super()._request(method, endpoint, wire, stream=False,
                                 request_timeout_seconds=request_timeout_seconds)
        raw = deepcopy(record.get("response"))
        record.update(api_protocol="chat_completions", client_revision=VERSION,
                      broker_requested_stream=bool(stream), actual_response_stream=False,
                      hosted_web_enabled=False, raw_response=raw,
                      raw_usage=deepcopy(raw.get("usage")) if isinstance(raw, dict) else None)
        if not record.get("ok") or method != "POST":
            return self.redact(record)
        try:
            if not isinstance(raw, dict) or raw.get("error"):
                raise ValueError("Expected a successful Chat Completions response object")
            choices = raw.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected exactly one Chat Completions choice")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise ValueError("Expected an assistant message")
            if "responses_output_items" in message:
                raise ValueError("Responses output items are not Chat Completions messages")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError("Assistant content must be text or null")
            if message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str):
                raise ValueError("Assistant reasoning_content must be text or null")
            finish = choice.get("finish_reason")
            if finish not in ("stop", "tool_calls", "length", "content_filter", "function_call"):
                raise ValueError("Missing or unsupported completion termination")
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ValueError("Native tool_calls must be a list")
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ValueError("Expected native function tool calls")
                if (call.get("function") or {}).get("name") != "benchmark_action":
                    raise ValueError("Unexpected provider tool call")
            record.update(returned_model=raw.get("model"), usage=raw.get("usage"),
                          content=message.get("content"), finish_reason=finish,
                          response_status="incomplete" if finish in ("length", "content_filter") else "completed",
                          tool_calls_observed=bool(calls), normalization_applied=False)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            record.update(ok=False, error="InvalidChatCompletionsSchema", error_detail=str(error))
        return self.redact(record)


RoleRelayClient = RoleChatClient
