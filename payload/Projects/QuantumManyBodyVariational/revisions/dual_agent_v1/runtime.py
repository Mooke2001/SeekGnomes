"""Independent, persistent role conversations for the dual-agent experiment.

The constructor owns a cumulative budget across every proof handoff. The
formalizer has no cumulative token, request-count, or active-wall allowance;
its per-request timeout is solely a protection against a hung connection.
Neither this loop nor a model-supplied action establishes mathematical truth.
Only the host tool handler may return a verified receipt or a role handoff.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import time

from revisions.hybrid_agent_v1.runtime.api_client import (
    RelayClient, completion_token_fields, inconsistent_output_usage,
)
from revisions.hybrid_web_v1.agent import (
    MAX_TOOL_RESULT_BYTES, _atomic_json, _inside_eureka, _json, _sha,
    account_budget_error, input_token_reservation, output_budget_reservation,
    parse_action, transport_failure,
)
from revisions.hybrid_web_v1.responses_client import (
    OUTPUT_ITEMS_FIELD, ResponsesClient, _normalized_response,
    _web_search_metadata, responses_payload,
)


VERSION = "dual-agent-role-runtime/v1"
FORMALIZER_CONTEXT_THRESHOLD = 400000
ROLE_TOOLS = {
    "constructor": ("list_files", "read", "write", "server", "create_handoff", "finish"),
    "formalizer": ("list_files", "read", "write", "prepare", "run_lean", "score", "report_issue", "finish"),
}
HANDOFF_ACTIONS = frozenset({"handoff", "feedback", "finish"})


class RoleResponsesClient(ResponsesClient):
    """The existing Responses bridge with a host-enforced hosted-web switch."""

    def __init__(self, *args, allow_web=True, **kwargs):
        if type(allow_web) is not bool:
            raise ValueError("allow_web must be boolean")
        self.allow_web = allow_web
        super().__init__(*args, **kwargs)

    def _request(self, method, endpoint, payload=None, *, stream=False,
                 request_timeout_seconds=None):
        if self.allow_web or method != "POST" or endpoint != "/chat/completions":
            return super()._request(method, endpoint, payload, stream=stream,
                                    request_timeout_seconds=request_timeout_seconds)
        wire = responses_payload(payload)
        wire["tools"] = [item for item in wire["tools"] if item.get("type") != "web_search"]
        wire["include"] = ["reasoning.encrypted_content"]
        record = RelayClient._request(self, "POST", "/responses", wire, stream=False,
                                     request_timeout_seconds=request_timeout_seconds)
        raw = deepcopy(record.get("response"))
        record.update(api_protocol="responses", bridge_version=VERSION,
            normalization_applied=False, broker_requested_stream=bool(stream),
            actual_response_stream=False, raw_response=raw,
            raw_usage=deepcopy(raw.get("usage")) if isinstance(raw, dict) else None,
            hosted_web_enabled=False, **_web_search_metadata(raw))
        if not record.get("ok"):
            return self.redact(record)
        if isinstance(raw, dict) and (raw.get("status") == "failed" or raw.get("error")):
            record.update(ok=False, error="ResponsesFailure",
                          error_detail="The provider returned a failed Responses result")
            return self.redact(record)
        try:
            if not isinstance(raw, dict):
                raise ValueError("Responses result must be an object")
            if any(item.get("type") == "web_search_call" for item in raw.get("output", [])
                   if isinstance(item, dict)):
                raise ValueError("hosted web is forbidden for the formalizer")
            response, content, calls, finish = _normalized_response(raw)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            record.update(ok=False, error="InvalidResponsesSchema", error_detail=str(error))
            return self.redact(record)
        record.update(response=response, content=content, tool_calls_observed=bool(calls),
            returned_model=raw.get("model"), usage=response["usage"], finish_reason=finish,
            normalization_applied=True, response_status=raw["status"],
            responses_output_items=deepcopy(raw["output"]))
        if inconsistent_output_usage(response["usage"]):
            record.update(usage_accounting_ambiguous=True,
                usage_accounting_warning="reasoning_detail_exceeds_reported_completion")
            if self.reasoning_usage_policy == "included":
                record.update(ok=False, error="InconsistentUsage",
                    error_detail="provider reasoning_tokens exceeds completion_tokens")
        return self.redact(record)


@dataclass(frozen=True)
class RoleLimits:
    max_turns: int | None = 64
    max_tokens_per_turn: int = 16000
    max_total_output_tokens: int | None = 128000
    max_total_input_tokens: int | None = 2000000
    wall_seconds: float | None = 14400
    max_server_cpu_seconds: float | None = 3600
    request_seconds: float = 600
    max_transport_reconnects: int = 2

    @classmethod
    def constructor(cls, **overrides):
        return cls(**overrides)

    @classmethod
    def formalizer(cls, **overrides):
        values = dict(max_turns=None, max_total_output_tokens=None,
            max_total_input_tokens=None, wall_seconds=None, max_server_cpu_seconds=None)
        values.update(overrides)
        return cls(**values)


def _limits(value, role):
    if value is None:
        value = getattr(RoleLimits, role)()
    elif isinstance(value, dict):
        value = RoleLimits(**value)
    if not isinstance(value, RoleLimits):
        raise TypeError("limits must be RoleLimits or a dictionary")
    hard = RoleLimits.constructor()
    for name in ("max_turns", "max_total_output_tokens", "max_total_input_tokens"):
        number = getattr(value, name)
        if number is not None and (type(number) is not int or number <= 0):
            raise ValueError(name + " must be positive or None")
        if role == "constructor" and (number is None or number > getattr(hard, name)):
            raise ValueError(name + " exceeds the constructor cap")
        if role == "formalizer" and number is not None:
            raise ValueError("formalizer cumulative request/token limits must be None")
    for name in ("wall_seconds", "max_server_cpu_seconds", "request_seconds"):
        number = getattr(value, name)
        if number is not None and (type(number) not in (int, float)
                or not math.isfinite(number) or number <= 0):
            raise ValueError(name + " must be positive and finite or None")
        if name == "request_seconds" and (number is None or number > hard.request_seconds):
            raise ValueError("request_seconds exceeds the single-request timeout")
        if role == "constructor" and name != "request_seconds" and (
                number is None or number > getattr(hard, name)):
            raise ValueError(name + " exceeds the constructor cap")
        if role == "formalizer" and name != "request_seconds" and number is not None:
            raise ValueError("formalizer has no cumulative wall or numerical budget")
    if type(value.max_tokens_per_turn) is not int or not 1 <= value.max_tokens_per_turn <= 16000:
        raise ValueError("max_tokens_per_turn must be in 1..16000")
    if type(value.max_transport_reconnects) is not int or not 0 <= value.max_transport_reconnects <= 2:
        raise ValueError("max_transport_reconnects must be in 0..2")
    return value


def _new_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class RoleSession:
    """One role's conversation, preserved across arbitrarily many handoffs.

    ``workspace`` is the role's host run directory. Its candidate file mount is
    ``workspace/workspace``, matching RoleToolBroker. The trusted handler is the
    sole file/computation capability provider; absent handlers fail closed.
    ``step`` performs one logical API request, including bounded same-payload
    transport reconnections. ``run_until_handoff`` loops until a host receipt
    requests handoff/feedback/finish, a budget ends, or infrastructure fails.
    """

    def __init__(self, client, model, role, workspace, references=None,
                 system_prompt=None, limits=None, tools=None, tool_handler=None, progress=None):
        if role not in ROLE_TOOLS:
            raise ValueError("role must be constructor or formalizer")
        if not isinstance(model, str) or not model:
            raise ValueError("a model ID is required")
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ValueError("an explicit system prompt is required")
        if getattr(client, "reasoning_usage_policy", None) != "provider_reported_completion":
            raise ValueError("dual-agent sessions require provider_reported_completion accounting")
        if role == "formalizer" and getattr(client, "allow_web", None) is not False:
            raise ValueError("formalizer requires a client with hosted web disabled")
        self.client, self.model, self.role = client, model, role
        self.limits = _limits(limits, role)
        self.tools = tuple(ROLE_TOOLS[role] if tools is None else tools)
        if not self.tools or len(set(self.tools)) != len(self.tools) or any(
                name not in ROLE_TOOLS[role] for name in self.tools):
            raise ValueError("tools must be a nonempty subset of the role whitelist")
        self.tool_handler, self.progress = tool_handler, progress
        self.broker = getattr(tool_handler, "__self__", None)
        directory = Path(os.path.abspath(workspace))
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise ValueError("role paths cannot traverse symlinks")
        self.workspace = _inside_eureka(directory)
        self.run_dir = self.workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        if any((self.workspace / name).exists() for name in
               ("manifest.json", "messages.json", "checkpoint.json", "api", "trace.jsonl")):
            raise ValueError("role session control files already exist; budget reset is forbidden")
        (self.workspace / "api").mkdir()
        self.file_workspace = self.workspace / "workspace"
        if self.file_workspace.is_symlink():
            raise ValueError("candidate workspace cannot be a symlink")
        self.file_workspace.mkdir(exist_ok=True)
        self.references = client.redact(dict(references or {}))
        if any(not isinstance(name, str) or not isinstance(content, str)
               for name, content in self.references.items()):
            raise ValueError("references must contain explicit public text only")
        prompt = system_prompt + "\nHost role resource limits:\n" + _json(asdict(self.limits))
        prompt += ("\nUse benchmark_action for one complete registered local action per response. "
                   "Only host tool receipts establish handoffs or verification. "
                   "Cumulative output accounting uses provider-reported completion once; "
                   "missing completion is reserved at the request cap. This is not a billing or physical-generation claim.")
        self.messages = [{"role": "system", "content": client.redact(prompt)}]
        self.manifest = {"schema": VERSION, "role": role, "model": model,
            "created_utc": datetime.now(timezone.utc).isoformat(), "limits": asdict(self.limits),
            "tools": list(self.tools), "status": "ready", "turns": 0, "logical_steps": 0,
            "input_tokens_charged": 0, "output_tokens_charged": 0, "requested_output_tokens": 0,
            "server_cpu_seconds_charged": 0, "active_wall_seconds": 0.0,
            "agent_transport_reconnects": 0, "usage": [], "budget_ledger": [],
            "returned_models": [], "model_identity_policy": "exact_requested_model; missing identity is a technical failure",
            "closing_notice_sent": False, "context_rotation_count": 0, "context_rotations": [],
            "hosted_web_enabled": role == "constructor" and getattr(client, "allow_web", True),
            "output_budget_measure": "provider_reported_completion; missing completion reserved at request cap",
            "physical_output_upper_bound_established": False, "resume_available": False,
            "system_prompt_sha256": _sha(client.redact(prompt).encode()),
            "references_sha256": _sha(_json(client.redact(self.references)).encode())}
        self._active_started = None
        self._terminal = None
        self._initial_user = None
        _new_json(self.workspace / "references.json", client.redact(self.references))
        _new_json(self.workspace / "system_prompt.json", {"content": client.redact(prompt)})
        self.persist()

    def _active_wall(self):
        return self.manifest["active_wall_seconds"] + (
            time.monotonic() - self._active_started if self._active_started is not None else 0)

    def persist(self):
        manifest = deepcopy(self.manifest)
        manifest["active_wall_seconds"] = self._active_wall()
        _atomic_json(self.workspace / "manifest.json", self.client.redact(manifest))
        _atomic_json(self.workspace / "messages.json", self.client.redact(self.messages))
        _atomic_json(self.workspace / "checkpoint.json", self.client.redact({
            "manifest": manifest, "messages": self.messages}))

    def _event(self, kind, data):
        safe = self.client.redact(data)
        with (self.workspace / "trace.jsonl").open("a") as stream:
            stream.write(_json({"kind": kind, "active_wall_seconds": self._active_wall(), "data": safe}) + "\n")
        if self.progress:
            self.progress(kind, deepcopy(safe))

    def add_user_message(self, content):
        if not isinstance(content, str) or not content:
            raise ValueError("role feedback must be nonempty text")
        if self._terminal is not None:
            raise ValueError("a terminated role session cannot be restarted")
        message = {"role": "user", "content": self.client.redact(content)}
        self.messages.append(message)
        if self._initial_user is None:
            self._initial_user = deepcopy(message)
        self.manifest["status"] = "ready"
        self.persist()

    def _remaining(self, field, used):
        maximum = getattr(self.limits, field)
        return None if maximum is None else max(0, maximum - used)

    def remaining_budgets(self):
        return {"api_responses_remaining": self._remaining("max_turns", self.manifest["turns"]),
            "output_tokens_remaining": self._remaining("max_total_output_tokens", self.manifest["output_tokens_charged"]),
            "input_tokens_remaining": self._remaining("max_total_input_tokens", self.manifest["input_tokens_charged"]),
            "active_wall_seconds_remaining": self._remaining("wall_seconds", self._active_wall()),
            "server_cpu_seconds_remaining": self._remaining("max_server_cpu_seconds", self.manifest["server_cpu_seconds_charged"])}

    def _stop(self, status, reason):
        self.manifest.update(status=status, stop_reason=reason,
            ended_utc=datetime.now(timezone.utc).isoformat())
        self._terminal = {"action": "finish", "ok": False, "session_status": status,
            "stop_reason": reason, "budget_exhausted": status == "budget_exhausted",
            "manifest_path": str(self.workspace / "manifest.json")}
        self.persist()
        return deepcopy(self._terminal)

    def _admission(self, estimate, cap):
        checks = (("max_turns", self.manifest["turns"] + 1, "api_budget_exhausted"),
            ("max_total_input_tokens", self.manifest["input_tokens_charged"] + estimate, "input_budget_exhausted"),
            ("max_total_output_tokens", self.manifest["output_tokens_charged"] + cap, "output_budget_exhausted"),
            ("wall_seconds", self._active_wall(), "active_wall_budget_exhausted"))
        for field, next_value, reason in checks:
            maximum = getattr(self.limits, field)
            if maximum is not None and (next_value >= maximum if field == "wall_seconds" else next_value > maximum):
                return reason
        return None

    def _payload(self, cap):
        return {"model": self.model, "messages": deepcopy(self.messages),
            **completion_token_fields(cap, self.client.completion_token_field),
            "stream": True, "stream_options": {"include_usage": True}, "tool_choice": "auto",
            "tools": [{"type": "function", "function": {"name": "benchmark_action",
                "description": "Request one documented action for the current role.",
                "parameters": {"type": "object", "properties": {
                    "tool": {"type": "string", "enum": list(self.tools)},
                    "arguments": {"type": "object"}}, "required": ["tool", "arguments"],
                    "additionalProperties": False}}}]}

    def _maybe_closing_notice(self):
        """Warn while the exact current handoff request can still be admitted.

        This adds no large hypothetical Lean-log reserve and does not end the
        scientific search early. The same original request/input/output caps
        admit every subsequent call, including handoff and source-note writes.
        """
        if self.role != "constructor" or self.manifest["closing_notice_sent"]:
            return
        estimate = input_token_reservation(self.messages)
        remaining = self.remaining_budgets()
        near_end = (remaining["api_responses_remaining"] <= 4
            or remaining["output_tokens_remaining"] <= 8000
            or self.manifest["input_tokens_charged"] + 2 * estimate > self.limits.max_total_input_tokens)
        if not near_end:
            return
        notice = {"role": "user", "content":
            "Host budget notice: the original cumulative constructor allowance is nearing its end. "
            "Save the exact f and its complete mathematical proof in private files and call create_handoff "
            "while a response remains. You may use the remaining original allowance; this notice does not "
            "add or reset tokens, API calls, active wall, or numerical CPU. Only a host-verified handoff "
            "can return energy. If no further construction is ready, finish explicitly. Remaining budgets:\n"
            + _json(remaining)}
        if input_token_reservation(self.messages + [notice]) + self.manifest["input_tokens_charged"] <= self.limits.max_total_input_tokens:
            self.messages.append(notice)
            self.manifest["closing_notice_sent"] = True
            self._event("constructor_closing_notice", remaining)

    def _maybe_rotate_context(self):
        """Bound one formalizer context without capping its cumulative work.

        The complete old conversation is archived, never summarized by a new
        model. The same immutable submission and on-disk proof work survive.
        Historical tool results are supplied as plain text, avoiding orphaned
        Responses call items and invented function outputs in the new context.
        """
        if self.role != "formalizer" or input_token_reservation(self.messages) < FORMALIZER_CONTEXT_THRESHOLD:
            return
        previous = deepcopy(self.messages)
        index = self.manifest["context_rotation_count"] + 1
        archive = self.workspace / f"history-{index:04d}.messages.json"
        _new_json(archive, self.client.redact(previous))
        receipts = [message["content"] for message in previous
            if message.get("role") == "tool" and isinstance(message.get("content"), str)][-4:]
        receipts = [text[:16000] for text in receipts]
        notice = ("Host context continuation for the SAME frozen constructor submission. "
            "The complete earlier conversation is retained in the host audit archive. Your current private "
            "Lean files, formalization_map.md, and generated references remain available through list_files/read. "
            "Inspect them and continue translating the supplied proof; do not alter f, invent a different "
            "construction, or optimize its parameters. Constructor mathematics remains authoritative. "
            "No cumulative token, API-call, or active-wall allowance has been imposed or reset. "
            "The following recent host receipts are historical data, rendered as plain text:\n" + _json(receipts))
        self.messages = [deepcopy(previous[0])]
        if self._initial_user is not None:
            self.messages.append(deepcopy(self._initial_user))
        self.messages.append({"role": "user", "content": notice})
        rotation = {"archive": archive.name, "archive_sha256": _sha(archive.read_bytes()),
            "before_input_reservation": input_token_reservation(previous),
            "after_input_reservation": input_token_reservation(self.messages),
            "api_turn": self.manifest["turns"], "mathematical_summary_generated": False}
        self.manifest["context_rotation_count"] = index
        self.manifest["context_rotations"].append(rotation)
        self._event("formalizer_context_rotation", rotation)
        self.persist()

    def _dispatch(self, action):
        tool, args = action["tool"], action["arguments"]
        if tool not in self.tools:
            return {"ok": False, "error": "role_tool_forbidden", "tool": tool}
        if tool == "server":
            wall, cpus = args.get("wall_seconds", 120), args.get("cpus", 1)
            if (type(wall) not in (int, float) or not math.isfinite(wall)
                    or not 1 <= wall <= 300 or type(cpus) is not int or cpus != 1):
                return {"ok": False, "error": "invalid_server_resource_request"}
            limit = self.limits.max_server_cpu_seconds
            if limit is not None and self.manifest["server_cpu_seconds_charged"] + wall > limit:
                return {"ok": False, "error": "server_cpu_budget_exhausted"}
            if self.limits.wall_seconds is not None and self._active_wall() + wall > self.limits.wall_seconds:
                return {"ok": False, "error": "server_wall_budget_exhausted"}
            self.manifest["server_cpu_seconds_charged"] += wall
            self.manifest["budget_ledger"].append({"kind": "server_cpu_reservation", "seconds": wall})
            self.persist()
        if self.tool_handler is None:
            return {"ok": False, "error": "host_tool_unavailable", "tool": tool}
        try:
            result = self.tool_handler(deepcopy(action), self)
            if not isinstance(result, dict):
                raise TypeError("tool handler must return an object")
            return self.client.redact(result)
        except Exception as error:
            # An implementation exception is a tool problem, never evidence of
            # a flaw in the constructor's mathematics or of verification.
            self._event("tool_handler_exception", {"tool": tool, "type": type(error).__name__})
            return {"ok": False, "error": "host_tool_failure", "tool": tool,
                    "exception_type": type(error).__name__, "mathematical_error_established": False}

    def _handle_record(self, record):
        try:
            message = deepcopy(record["response"]["choices"][0]["message"])
            if not isinstance(message, dict):
                raise ValueError("invalid assistant message")
            message["role"] = "assistant"
            if OUTPUT_ITEMS_FIELD not in message and isinstance(record.get(OUTPUT_ITEMS_FIELD), list):
                message[OUTPUT_ITEMS_FIELD] = deepcopy(record[OUTPUT_ITEMS_FIELD])
            incomplete = (record.get("response_status") == "incomplete"
                or record.get("finish_reason") in ("length", "content_filter"))
            if incomplete:
                # Keep the exact provider output in api/*.json, but never replay
                # an unexecuted partial function as an outstanding complete
                # call. This permits protocol repair without fabricating a
                # function result for an action that never ran.
                message["tool_calls"] = []
                if isinstance(message.get(OUTPUT_ITEMS_FIELD), list):
                    message[OUTPUT_ITEMS_FIELD] = [item for item in message[OUTPUT_ITEMS_FIELD]
                        if item.get("type") != "function_call"]
            self.messages.append(self.client.redact(message))
            calls = message.get("tool_calls") or []
            if incomplete:
                raise ValueError("an incomplete response cannot execute tools")
            if not calls and any(item.get("type") == "web_search_call" for item in
                    message.get(OUTPUT_ITEMS_FIELD, []) if isinstance(item, dict)):
                if self.role != "constructor":
                    return self._stop("infrastructure_failure", "forbidden_hosted_web_output")
                self._event("hosted_web_only", {"no_local_action_executed": True})
                return None
            if not isinstance(calls, list) or len(calls) != 1:
                raise ValueError("exactly one complete native action required")
            call = calls[0]
            if (not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]
                    or call.get("type") != "function"
                    or (call.get("function") or {}).get("name") != "benchmark_action"):
                raise ValueError("unregistered function call")
            action = self.client.redact(parse_action(call["function"]["arguments"]))
            self._event("tool_request", action)
            result = self._dispatch(action)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            result = {"ok": False, "error": "protocol_error",
                "detail": "Call benchmark_action exactly once with complete top-level tool and arguments. Incomplete calls are never executed."}
            calls = message.get("tool_calls", []) if isinstance(locals().get("message"), dict) else []
        self._event("tool_result", result)
        output = _json(self.client.redact({**result, "remaining_budgets": self.remaining_budgets()}))
        if len(output.encode()) > MAX_TOOL_RESULT_BYTES:
            output = _json({"ok": result.get("ok", False), "output_truncated": True,
                "prefix": output[:MAX_TOOL_RESULT_BYTES // 2]})
        acknowledged = False
        for call in calls if isinstance(calls, list) else []:
            if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]:
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
                acknowledged = True
        if not acknowledged:
            self.messages.append({"role": "user", "content": "Host protocol feedback:\n" + output})
        if result.get("action") in HANDOFF_ACTIONS:
            self.manifest["status"] = "finished" if result["action"] == "finish" else "waiting_for_peer"
            receipt = {**result, "session_status": self.manifest["status"],
                       "manifest_path": str(self.workspace / "manifest.json")}
            if result["action"] == "finish":
                self._terminal = deepcopy(receipt)
            return receipt
        return None

    def step(self):
        if self._terminal is not None:
            return deepcopy(self._terminal)
        if self._active_started is not None:
            raise RuntimeError("role session cannot run concurrent or recursive requests")
        self._active_started = time.monotonic()
        try:
            self._maybe_rotate_context()
            self._maybe_closing_notice()
            estimate = input_token_reservation(self.messages)
            remaining = self._remaining("max_total_output_tokens", self.manifest["output_tokens_charged"])
            cap = min(self.limits.max_tokens_per_turn, remaining) if remaining is not None else self.limits.max_tokens_per_turn
            if cap <= 0:
                return self._stop("budget_exhausted", "output_budget_exhausted")
            reason = self._admission(estimate, cap)
            if reason:
                return self._stop("budget_exhausted", reason)
            planned = self._payload(cap)
            message_hash = _sha(_json(planned["messages"]).encode())
            logical = self.manifest["logical_steps"]
            self.manifest["logical_steps"] += 1
            deadline = time.monotonic() + min(self.limits.request_seconds, self.client.timeout)
            for attempt in range(self.limits.max_transport_reconnects + 1):
                # Reconnection repeats exactly the same logical payload/cap.
                # Failed attempts retain reservations and count against every
                # cumulative constructor allowance before a retry is admitted.
                reason = self._admission(estimate, cap)
                if reason:
                    return self._stop("budget_exhausted", reason)
                seconds = deadline - time.monotonic()
                if self.limits.wall_seconds is not None:
                    seconds = min(seconds, self.limits.wall_seconds - self._active_wall())
                if seconds <= 0:
                    return self._stop("infrastructure_failure", "logical_request_deadline_exhausted")
                turn = self.manifest["turns"]
                metadata = {"logical_step": logical, "transport_attempt": attempt,
                    "request_messages_sha256": message_hash, "request_seconds": seconds}
                _new_json(self.workspace / "api" / f"{turn:04d}.request.json", self.client.redact({
                    "request": planned, "started_utc": datetime.now(timezone.utc).isoformat(), **metadata}))
                row = {"kind": "api", "turn": turn, "input_reservation": estimate,
                    "input_charged": estimate, "output_requested": cap,
                    **output_budget_reservation(None, cap, "provider_reported_completion"),
                    "in_flight": True, **metadata}
                self.manifest["turns"] += 1
                self.manifest["input_tokens_charged"] += estimate
                self.manifest["output_tokens_charged"] += cap
                self.manifest["requested_output_tokens"] += cap
                self.manifest["budget_ledger"].append(row)
                self.manifest["status"] = "running"
                self.persist()
                original_timeout = self.client.timeout
                self.client.timeout = min(original_timeout, seconds)
                try:
                    try:
                        record = self.client._request("POST", "/chat/completions", deepcopy(planned),
                            stream=True, request_timeout_seconds=seconds)
                        if not isinstance(record, dict):
                            raise TypeError("client response must be an object")
                    except Exception as error:
                        record = {"ok": False, "error": type(error).__name__, "http_status": None,
                            "usage": None, "error_detail": "client request raised; reservations retained"}
                finally:
                    self.client.timeout = original_timeout
                usage = record.get("usage")
                observed = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                input_charge = observed if type(observed) is int and observed >= 0 else estimate
                reservation = output_budget_reservation(usage, cap, "provider_reported_completion")
                self.manifest["input_tokens_charged"] += input_charge - estimate
                self.manifest["output_tokens_charged"] += reservation["output_charged"] - cap
                row.update(input_charged=input_charge, input_provider_reported=observed,
                    **reservation, in_flight=False)
                self.manifest["usage"].append(deepcopy(usage))
                record.update(**metadata, runtime_version=VERSION)
                _new_json(self.workspace / "api" / f"{turn:04d}.json", self.client.redact(record))
                self._event("api", {"turn": turn, "ok": record.get("ok"), "record": f"api/{turn:04d}.json"})
                self.persist()
                if not record.get("ok"):
                    status = record.get("http_status")
                    refused = type(status) is int and 400 <= status < 500
                    if (not refused and account_budget_error(record) is None and transport_failure(record)
                            and attempt < self.limits.max_transport_reconnects and time.monotonic() < deadline):
                        try:
                            self.client.reset_connection()
                        except Exception:
                            return self._stop("infrastructure_failure", "connection_reset_failed")
                        self.manifest["agent_transport_reconnects"] += 1
                        self._event("transport_reconnect", metadata)
                        continue
                    return self._stop("infrastructure_failure", "http_refusal" if refused else "request_failure")
                returned_model = record.get("returned_model")
                if returned_model not in self.manifest["returned_models"]:
                    self.manifest["returned_models"].append(returned_model)
                if returned_model != self.model:
                    self.manifest["model_identity_error"] = {
                        "requested": self.model, "returned": returned_model}
                    return self._stop("infrastructure_failure", "returned_model_missing_or_mismatched")
                if ((self.limits.max_total_output_tokens is not None and
                        self.manifest["output_tokens_charged"] > self.limits.max_total_output_tokens)
                        or (self.limits.max_total_input_tokens is not None and
                        self.manifest["input_tokens_charged"] > self.limits.max_total_input_tokens)):
                    return self._stop("infrastructure_failure", "provider_usage_exceeded_budget")
                return self._handle_record(record)
            return self._stop("infrastructure_failure", "transport_reconnects_exhausted")
        finally:
            self.manifest["active_wall_seconds"] += time.monotonic() - self._active_started
            self._active_started = None
            self.persist()

    def run_until_handoff(self):
        while True:
            result = self.step()
            if result is not None:
                return result


__all__ = ["RoleSession", "RoleLimits", "RoleResponsesClient", "ROLE_TOOLS", "VERSION"]
