"""Small auditable OpenAI-compatible client; credentials never enter artifacts."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from src.relay_session import PersistentSession


RESPONSE_HEADER_ALLOWLIST = (
    "x-request-id", "x-litellm-call-id", "x-litellm-model-id", "x-litellm-response-cost",
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
COMPLETION_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "both")
REASONING_USAGE_POLICIES = ("included", "conservative_union_bound", "provider_reported_completion")


def completion_token_fields(count, selected="max_tokens"):
    """Explicit request spelling, without changing provider usage accounting."""
    if type(count) is not int or count <= 0 or selected not in COMPLETION_TOKEN_FIELDS:
        raise ValueError("invalid completion-token request configuration")
    if selected == "both":
        return {"max_tokens": count, "max_completion_tokens": count}
    return {selected: count}


def inconsistent_output_usage(usage):
    if not isinstance(usage, dict):
        return False
    completion = usage.get("completion_tokens")
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return (type(completion) is int and completion >= 0 and type(reasoning) is int
            and reasoning >= 0 and reasoning > completion)


def _safe_http_error_detail(error):
    # Protocol exceptions may quote raw response header lines. Persist only
    # fixed classifications, never arbitrary exception str/repr or its causes.
    return {
        "ConnectError": "connection establishment failed",
        "ConnectTimeout": "connection establishment timed out",
        "RemoteProtocolError": "remote HTTP protocol error",
        "LocalProtocolError": "local HTTP protocol error",
        "ReadError": "response transport read failed",
        "ReadTimeout": "response read timed out",
        "WriteError": "request transport write failed",
        "WriteTimeout": "request write timed out",
        "PoolTimeout": "connection pool wait timed out",
        "ProxyError": "proxy connection failed",
    }.get(type(error).__name__, "HTTP transport failed")


class _StreamFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _SSEAccumulator:
    """Preserve bounded raw frames, extracting only choice-zero delta.content."""

    def __init__(self):
        self.buffer = b""
        self.raw_lines = []
        self.data_lines = []
        self.event_name = None
        self.events = []
        self.content_parts = []
        self.model = None
        self.usage = None
        self.finish_reason = None
        self.done = False
        self.pending_raw = ""
        self.error_response = None
        self.tool_call_events = []

    def _dispatch(self):
        raw = b"".join(self.raw_lines).decode("utf-8")
        data = "\n".join(self.data_lines) if self.data_lines else None
        event = {"raw": raw, "data": data, "event": self.event_name}
        self.events.append(event)
        self.raw_lines, self.data_lines, self.event_name = [], [], None
        if data is None:
            event["kind"] = "comment_or_empty"
            return
        if data.strip() == "[DONE]":
            event["kind"] = "done"
            self.done = True
            return
        try:
            value = strict_json(data)
        except (ValueError, TypeError, RecursionError):
            event["kind"] = "invalid_json"
            raise _StreamFailure("SSEInvalidJSON") from None
        event.update(kind="json", json=value)
        if not isinstance(value, dict):
            raise _StreamFailure("SSEInvalidSchema")
        if "error" in value:
            self.error_response = value
            raise _StreamFailure("SSEErrorEvent")
        if isinstance(value.get("model"), str):
            self.model = value["model"]
        if isinstance(value.get("usage"), dict):
            self.usage = value["usage"]
        choices = value.get("choices", [])
        if not isinstance(choices, list):
            raise _StreamFailure("SSEInvalidSchema")
        for choice in choices:
            if not isinstance(choice, dict):
                raise _StreamFailure("SSEInvalidSchema")
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise _StreamFailure("SSEInvalidSchema")
            content = delta.get("content")
            if delta.get("tool_calls") or delta.get("function_call"):
                self.tool_call_events.append(delta.get("tool_calls") or delta["function_call"])
            if content is not None:
                if not isinstance(content, str):
                    raise _StreamFailure("SSEInvalidSchema")
                self.content_parts.append(content)
            finish = choice.get("finish_reason")
            if finish is not None:
                if not isinstance(finish, str):
                    raise _StreamFailure("SSEInvalidSchema")
                self.finish_reason = finish

    def feed(self, chunk, *, final=False):
        self.buffer += chunk
        while not self.done:
            found = re.search(rb"[\r\n]", self.buffer)
            if found is None:
                break
            index = found.start()
            if self.buffer[index:index + 1] == b"\r" and index + 1 == len(self.buffer) and not final:
                break  # A CRLF pair may cross transport chunks.
            end = index + 1
            if self.buffer[index:end] == b"\r" and self.buffer[end:end + 1] == b"\n":
                end += 1
            raw, self.buffer = self.buffer[:end], self.buffer[end:]
            self.raw_lines.append(raw)
            try:
                line = raw[:index].decode("utf-8")
            except UnicodeDecodeError:
                raise _StreamFailure("SSEInvalidUTF8") from None
            if not line:
                self._dispatch()
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    self.data_lines.append(value)
                elif field == "event":
                    self.event_name = value
        if final:
            self.pending_raw = (b"".join(self.raw_lines) + self.buffer).decode("utf-8", errors="replace")

    def response(self):
        return {"model": self.model, "usage": self.usage,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(self.content_parts),
                             **({"tool_calls": self.tool_call_events} if self.tool_call_events else {})},
                             "finish_reason": self.finish_reason}]}


def strict_json(text: str):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out
    def constant(value):
        raise ValueError("nonfinite JSON number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def parse_submission(content: str) -> dict:
    if not isinstance(content, str) or len(content.encode()) > 250_000:
        raise ValueError("submission absent or larger than 250000 bytes")
    content = content.strip()
    if content.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.S)
        if not match:
            raise ValueError("expected one JSON object or one JSON code fence")
        content = match.group(1)
    try:
        value = strict_json(content)
    except RecursionError:
        raise ValueError("JSON nesting exceeds parser limits") from None
    if not isinstance(value, dict) or "candidate" not in value:
        raise ValueError("submission must be an object with a candidate")
    if set(value) - {"candidate", "derivation", "lower_bound_certificate"}:
        raise ValueError("unknown submission fields")
    if not isinstance(value["candidate"], dict):
        raise ValueError("candidate must be an object")
    if "derivation" in value and not isinstance(value["derivation"], str):
        raise ValueError("derivation must be a string")
    return value


@dataclass
class RelayClient:
    base_url: str
    api_key: str = field(repr=False)
    timeout: float = 180
    transport: object = field(default=None, repr=False)
    connect_timeout: float = 12
    persistent_session: bool = False
    max_connect_retries: int = 0
    response_body_limit_bytes: int = MAX_RESPONSE_BYTES
    completion_token_field: str = "max_tokens"
    reasoning_usage_policy: str = "included"
    _session: object = field(default=None, init=False, repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)
    _lifecycle_lock: object = field(default_factory=threading.RLock, init=False,
                                    repr=False, compare=False)

    def __post_init__(self):
        parts = urlsplit(self.base_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment
                or any(c.isspace() for c in self.base_url)):
            raise ValueError("expected a credential-free HTTPS base URL")
        self.base_url = self.base_url.rstrip("/")
        if not self.api_key:
            raise ValueError("missing API key; launch through Environment/API/run.sh relay")
        if not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be a finite positive number of seconds")
        if (isinstance(self.connect_timeout, bool)
                or not isinstance(self.connect_timeout, (int, float))
                or not math.isfinite(self.connect_timeout) or self.connect_timeout <= 0):
            raise ValueError("connect_timeout must be a finite positive number of seconds")
        if type(self.persistent_session) is not bool:
            raise ValueError("persistent_session must be boolean")
        if type(self.max_connect_retries) is not int or not 0 <= self.max_connect_retries <= 2:
            raise ValueError("max_connect_retries must be an integer in 0..2")
        if (type(self.response_body_limit_bytes) is not int
                or not 1 <= self.response_body_limit_bytes <= 64 * 1024 * 1024):
            raise ValueError("response_body_limit_bytes must be an integer in 1..64MiB")
        if self.completion_token_field not in COMPLETION_TOKEN_FIELDS:
            raise ValueError("unknown completion_token_field")
        if self.reasoning_usage_policy not in REASONING_USAGE_POLICIES:
            raise ValueError("unknown reasoning_usage_policy")

    def close(self):
        """Close an optional persistent pool; subsequent requests are rejected."""
        if self._session is not None and self._session.in_session_thread:
            raise RuntimeError("cannot close the client from its HTTP event loop")
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            if self._session is not None:
                self._session.close()

    def reset_connection(self):
        """Discard an existing keep-alive pool without issuing any request.

        This explicit host action never enables automatic POST retries. The
        caller owns any later request, its raw record, deadline and token cost.
        """
        if self._session is not None and self._session.in_session_thread:
            raise RuntimeError("cannot reset the client from its HTTP event loop")
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("RelayClient is closed")
            if self._session is None:
                return False
            return self._session.reset_connection()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("RelayClient is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @classmethod
    def from_environment(cls, *, base_url=None, **kwargs):
        if os.environ.get("EUREKA_API_PROVIDER") != "relay":
            raise ValueError("this experiment requires Environment/API provider relay")
        endpoint = os.environ.get("EUREKA_API_BASE_URL", "") if base_url is None else base_url
        return cls(endpoint,
                   os.environ.get("EUREKA_API_KEY", ""), **kwargs)

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self.api_key, "[REDACTED]")
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, dict):
            return {self.redact(k): self.redact(v) for k, v in value.items()}
        return value

    def _metadata(self, headers):
        metadata = self.redact({name: headers[name] for name in RESPONSE_HEADER_ALLOWLIST if name in headers})
        try:
            cost = float(metadata.get("x-litellm-response-cost", ""))
        except (ValueError, TypeError, OverflowError):
            cost = None
        metadata["cost_usd"] = cost if cost is not None and math.isfinite(cost) and cost >= 0 else None
        return metadata

    async def _request_async(self, method, endpoint, payload, *, stream,
                             session_client=None, request_timeout_seconds=None):
        start = time.monotonic()
        request_timeout = (self.timeout if request_timeout_seconds is None
                           else min(self.timeout, request_timeout_seconds))
        connect_timeout = min(self.connect_timeout, request_timeout)
        connect_retries = self.max_connect_retries
        record = {"endpoint": endpoint, "method": method, "base_url": self.base_url,
                  "request": payload, "automatic_retries": 0,
                  "automatic_post_retries": 0, "automatic_connection_retries": 0,
                  "connection_attempts": [],
                  "request_timeout_seconds": request_timeout,
                  "connect_timeout_seconds": connect_timeout,
                  "max_connect_retries": connect_retries,
                  "connection_reuse_enabled": session_client is not None,
                  "http_body_drained": False,
                  "http_events": [], "http_events_truncated": False,
                  "response_metadata": {"cost_usd": None}, "http_status": None,
                  "ok": False, "response": None,
                  "response_body_limit_bytes": self.response_body_limit_bytes,
                  "completion_token_field": self.completion_token_field,
                  "reasoning_usage_policy": self.reasoning_usage_policy,
                  "runtime_version": "transport_v3_2"}
        body = bytearray()
        accumulator = _SSEAccumulator()
        is_sse = False
        phase = {}

        async def trace(name, _info):
            # HTTP core supplies event names. Never persist its info dictionary:
            # that can contain headers, request/response objects and sockets.
            if (isinstance(name, str) and len(name) <= 120
                    and re.fullmatch(r"(?:connection|http11|http2)\.[a-z0-9_.]+\.(?:started|complete|failed)", name)):
                if name in ("connection.connect_tcp.failed", "connection.start_tls.failed"):
                    phase["connect_failure_observed"] = True
                if any(stage in name for stage in (
                        ".send_request_headers.", ".send_request_body.",
                        ".receive_response_headers.", ".receive_response_body.")):
                    phase["http_started"] = True
                if len(record["http_events"]) < 128:
                    record["http_events"].append({"name": name,
                        "elapsed_seconds": time.monotonic() - start})
                else:
                    record["http_events_truncated"] = True

        async def send(client):
            nonlocal is_sse
            async with client.stream(method, self.base_url + endpoint,
                        headers={"Authorization": "Bearer " + self.api_key}, json=payload,
                        timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
                        extensions={"trace": trace}) as response:
                record["http_status"] = response.status_code
                record["response_metadata"] = self._metadata(response.headers)
                is_sse = stream and response.is_success and "text/event-stream" in response.headers.get("content-type", "").lower()
                async for chunk in response.aiter_bytes():
                    remaining = self.response_body_limit_bytes - len(body)
                    if len(chunk) > remaining:
                        body.extend(chunk[:remaining])
                        raise _StreamFailure("ResponseBodyLimitExceeded")
                    body.extend(chunk)
                    if is_sse:
                        accumulator.feed(chunk)
                        if accumulator.done and session_client is None:
                            break
                else:
                    # A persistent SSE response must consume HTTP EndOfMessage
                    # after [DONE], or httpcore has to discard its connection.
                    # Draining still obeys this request's deadline and body cap.
                    record["http_body_drained"] = True
                if is_sse:
                    accumulator.feed(b"", final=True)
                    if accumulator.done:
                        record.update(ok=True, stream_termination="done")
                    elif accumulator.finish_reason and not accumulator.pending_raw:
                        record.update(ok=True, stream_termination="eof_after_finish")
                    else:
                        raise _StreamFailure("SSETruncatedEOF")
                else:
                    try:
                        record["response"] = strict_json(body.decode("utf-8"))
                    except (ValueError, TypeError, RecursionError, UnicodeError):
                        record["response"] = None
                    record["ok"] = response.is_success and isinstance(record["response"], dict)
                    if not record["ok"]:
                        record["error"] = "HTTP_or_JSON_response_failure"
                        record["error_detail"] = "HTTP status or JSON response validation failed"
                    if stream:
                        record["stream_termination"] = "non_sse_response"

        async def attempts(client):
            for number in range(1, connect_retries + 2):
                phase.clear()
                attempt = {"number": number, "start_elapsed_seconds": time.monotonic() - start,
                           "event_start_index": len(record["http_events"])}
                record["connection_attempts"].append(attempt)
                try:
                    await send(client)
                except httpx.HTTPError as exc:
                    # Local httpcore tracing precedes HTTP header writes. A
                    # missing, truncated or post-header trace cannot authorize
                    # another attempt. No ambiguous POST is ever replayed.
                    no_http_sent = bool(
                        phase.get("connect_failure_observed")
                        and not phase.get("http_started")
                        and not record["http_events_truncated"]
                        and record["http_status"] is None and not body)
                    retry = (isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                             and no_http_sent and number <= connect_retries)
                    attempt.update(error=type(exc).__name__,
                                   error_detail=self.redact(_safe_http_error_detail(exc))[:1000],
                                   no_http_sent_proved_by_trace=no_http_sent,
                                   retried_before_http=retry)
                    if not retry:
                        raise
                    record["automatic_connection_retries"] += 1
                    record["automatic_retries"] += 1
                finally:
                    attempt.update(end_elapsed_seconds=time.monotonic() - start,
                                   event_end_index=len(record["http_events"]))
                if not attempt.get("retried_before_http"):
                    return

        async def transfer():
            if session_client is not None:
                await attempts(session_client)
            else:
                async with httpx.AsyncClient(
                        timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
                        transport=self.transport, follow_redirects=False,
                        trust_env=False) as client:
                    await attempts(client)

        try:
            await asyncio.wait_for(transfer(), timeout=request_timeout)
        except asyncio.TimeoutError:
            record.update(ok=False, error="TotalDeadlineExceeded",
                          error_detail="request total deadline exceeded",
                          ambiguous_post_completion=method == "POST")
            if stream:
                record["stream_termination"] = "deadline_exceeded"
        except _StreamFailure as exc:
            record.update(ok=False, error=exc.code,
                          error_detail=self.redact(exc.code)[:1000],
                          ambiguous_post_completion=method == "POST")
            if stream:
                record["stream_termination"] = "truncated_eof" if exc.code == "SSETruncatedEOF" else exc.code
        except httpx.HTTPError as exc:
            record.update(ok=False, error=type(exc).__name__,
                          error_detail=self.redact(_safe_http_error_detail(exc))[:1000],
                          ambiguous_post_completion=method == "POST")
            if stream:
                record["stream_termination"] = "transport_error"
        record["response_text"] = body.decode("utf-8", errors="replace")
        record["response_bytes_captured"] = len(body)
        if stream:
            record.update(stream_requested=True, raw_sse_events=accumulator.events,
                          tool_calls_observed=bool(accumulator.tool_call_events),
                          stream_done=accumulator.done, stream_finish_received=bool(accumulator.finish_reason),
                          stream_complete=bool(is_sse and record["ok"]),
                          incomplete_sse_event=(b"".join(accumulator.raw_lines) + accumulator.buffer).decode("utf-8", errors="replace"))
            if is_sse:
                aggregate = accumulator.response()
                record.update(response=accumulator.error_response or aggregate,
                              content=aggregate["choices"][0]["message"]["content"],
                              returned_model=accumulator.model, usage=accumulator.usage,
                              finish_reason=accumulator.finish_reason)
        record["latency_seconds"] = time.monotonic() - start
        reported_usage = record.get("usage")
        if reported_usage is None and isinstance(record.get("response"), dict):
            reported_usage = record["response"].get("usage")
        if (record.get("ok") and self.reasoning_usage_policy == "included"
                and inconsistent_output_usage(reported_usage)):
            record.update(ok=False, error="InconsistentUsage",
                          error_detail="provider reasoning_tokens exceeds completion_tokens",
                          usage=reported_usage, usage_accounting_ambiguous=True)
        if (self.reasoning_usage_policy == "provider_reported_completion"
                and inconsistent_output_usage(reported_usage)):
            # Diagnostic only: this policy meters the provider's completion
            # field, without claiming the detail field is contained in it.
            record.update(usage_accounting_ambiguous=True,
                          usage_accounting_warning="reasoning_detail_exceeds_reported_completion",
                          usage_budget_measure="provider_reported_completion")
        return self.redact(record)

    def _request(self, method, endpoint, payload=None, *, stream=False,
                 request_timeout_seconds=None):
        # A host reconnect loop can share one logical-step deadline while each
        # actual request retains its own raw record. This never starts a retry.
        if (request_timeout_seconds is not None
                and (isinstance(request_timeout_seconds, bool)
                     or not isinstance(request_timeout_seconds, (int, float))
                     or not math.isfinite(request_timeout_seconds)
                     or request_timeout_seconds <= 0)):
            raise ValueError("request_timeout_seconds must be a finite positive number")
        if self.persistent_session:
            if self._session is not None and self._session.in_session_thread:
                raise RuntimeError("cannot synchronously request from the HTTP event loop")
            with self._lifecycle_lock:
                if self._closed:
                    raise RuntimeError("RelayClient is closed")
                if self._session is None:
                    self._session = PersistentSession(self.transport)
                return self._session.run(lambda client: self._request_async(
                    method, endpoint, payload, stream=stream, session_client=client,
                    request_timeout_seconds=request_timeout_seconds))
        if self._closed:
            raise RuntimeError("RelayClient is closed")
        def run():
            return asyncio.run(self._request_async(
                method, endpoint, payload, stream=stream,
                request_timeout_seconds=request_timeout_seconds))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        # Keep the public API synchronous when embedded in an existing event loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(run).result()

    def catalogue(self):
        result = self._request("GET", "/models")
        if result["ok"]:
            data = result["response"].get("data")
            if not isinstance(data, list) or any(not isinstance(v, dict)
                    or not isinstance(v.get("id"), str) for v in data):
                result.update(ok=False, error="invalid_model_catalogue")
            else:
                result["model_ids"] = sorted({v["id"] for v in data})
        return result

    def complete(self, model, messages, *, max_tokens=2000, stream=False, forbid_tools=False):
        # No temperature or reasoning knobs unsupported by some families.
        payload = {"model": model, "messages": messages,
                   **completion_token_fields(max_tokens, self.completion_token_field), "stream": stream}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if forbid_tools:
            payload.update(tools=[], tool_choice="none")
        result = self._request("POST", "/chat/completions", payload, stream=stream)
        if result["ok"]:
            try:
                response = result["response"]
                choice = response["choices"][0]
                content = choice["message"].get("content")
                result.update(content=content, returned_model=response.get("model"),
                              usage=response.get("usage"),
                              finish_reason=choice.get("finish_reason"))
                tool_calls = choice["message"].get("tool_calls") or choice["message"].get("function_call")
                result["tool_calls_observed"] = bool(tool_calls)
                if forbid_tools and (tool_calls or choice.get("finish_reason") in ("tool_calls", "function_call")):
                    result.update(ok=False, error="forbidden_tool_call_response")
                if not isinstance(content, str):
                    result.update(ok=False, error="no_text_content")
            except (KeyError, IndexError, TypeError, AttributeError):
                result.update(ok=False, error="invalid_completion_schema")
        return result
