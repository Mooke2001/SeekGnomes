"""Isolated text-tool agent loop for the infinite-state benchmark.

The API key exists only in RelayClient. Candidate text is data, never imported
or executed here. Host callbacks must independently sandbox Lean/server jobs
and validate proof exports; a model cannot supply a trusted score receipt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import fcntl
import tempfile
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Callable

from revisions.hybrid_agent_v1.runtime.api_client import RelayClient, strict_json, completion_token_fields, inconsistent_output_usage


PROJECT = Path("/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational")
EUREKA = PROJECT.parents[1]
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOOL_RESULT_BYTES = 160_000
ToolCallback = Callable[..., dict]
ACTION_NAMES = ("list_files", "read", "write", "prepare", "run_lean", "score", "server", "finish", "final_submit")
ACCOUNT_BUDGET_ERROR_CODES = frozenset({
    "budget_exceeded", "insufficient_quota", "billing_hard_limit_reached",
})
TRANSPORT_FAILURES = frozenset({"ConnectError", "ConnectTimeout", "RemoteProtocolError",
    "ReadError", "ReadTimeout", "WriteError", "WriteTimeout", "PoolTimeout",
    "SSETruncatedEOF", "TotalDeadlineExceeded"})
FINAL_MAX_RESPONSES = 3
RUNTIME_VERSION = "hybrid_web_v1"


def runtime_revision():
    """Bind same-version resume to the executable bytes, not a mutable label."""
    names = ("revisions/hybrid_agent_v1/runtime/__init__.py",
             "revisions/hybrid_agent_v1/runtime/api_client.py",
             "revisions/hybrid_agent_v1/runtime/benchmark50_agent.py", "src/relay_session.py",
             "revisions/hybrid_web_v1/agent.py")
    return {"version": RUNTIME_VERSION, "source_sha256": {
        name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() for name in names}}


def transport_config(client, normal_request_tokens=8000, normal_output_safety_margin=4096,
                     closing_request_tokens=2048):
    for name, value in (("normal_request_tokens", normal_request_tokens),
                        ("closing_request_tokens", closing_request_tokens)):
        if type(value) is not int or value <= 0:
            raise ValueError(name + " must be a positive integer")
    if type(normal_output_safety_margin) is not int or normal_output_safety_margin < 0:
        raise ValueError("normal_output_safety_margin must be a nonnegative integer")
    return {"schema": "transport-v3-config/v1",
            "response_body_limit_bytes": client.response_body_limit_bytes,
            "completion_token_field": client.completion_token_field,
            "reasoning_usage_policy": client.reasoning_usage_policy,
            "normal_request_tokens": normal_request_tokens,
            "normal_output_safety_margin": normal_output_safety_margin,
            "closing_request_tokens": closing_request_tokens,
            "body_limit_host_resume": True}


def output_budget_reservation(usage, requested, policy):
    """Reserve budget without inventing actual tokens or component inclusion.

    provider_reported_completion meters the reported completion field once,
    without asserting a physical-generation or billing upper bound.
    For unknown overlap, c+r bounds the union of two reported nonnegative
    components. Missing components have no established upper bound; their
    reservation remains explicitly distinct from that observable union bound.
    """
    usage = usage if isinstance(usage, dict) else {}
    completion = usage.get("completion_tokens")
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    c_valid = type(completion) is int and completion >= 0
    r_valid = type(reasoning) is int and reasoning >= 0
    upper = None
    invalid_included = policy == "included" and inconsistent_output_usage(usage)
    partial = False
    if policy == "conservative_union_bound":
        if c_valid and r_valid:
            charged = upper = completion + reasoning
            source = "reported_components_union_bound"
        elif c_valid or r_valid:
            charged = max(requested, completion if c_valid else 0, reasoning if r_valid else 0)
            source = "incomplete_usage_request_reservation"
            partial = True
        else:
            charged = requested
            source = "request_cap_missing_usage"
    elif policy == "provider_reported_completion":
        # Empirical provider-reported completion budget, matching the legacy
        # agent. Neither c nor the missing-c reservation is asserted to bound
        # all physical generation, hidden reasoning, or billing.
        charged = completion if c_valid else requested
        source = "provider_reported_completion" if c_valid else "request_cap_missing_completion"
        partial = not c_valid
    elif policy == "included":
        charged = completion if c_valid and not invalid_included else requested
        source = ("request_cap_inconsistent_usage" if invalid_included else
                  "provider_completion_tokens" if c_valid else "request_cap_missing_usage")
    else:
        raise ValueError("unknown reasoning_usage_policy")
    return {"output_charged": charged, "output_provider_reported": completion,
            "reasoning_provider_reported": reasoning, "runtime_budget_upper_bound": upper,
            "usage_policy": policy, "output_accounting_source": source,
            "output_usage_ambiguous": invalid_included or (policy == "provider_reported_completion" and inconsistent_output_usage(usage)),
            "reasoning_detail_exceeds_completion": inconsistent_output_usage(usage),
            "budget_measure": "provider_reported_completion" if policy == "provider_reported_completion" else policy,
            "physical_output_upper_bound_established": False,
            "reported_component_overlap_unknown": policy == "conservative_union_bound" and c_valid and r_valid,
            "usage_partial": partial, "reasoning_unreported": not r_valid}


def _session_policy(client, kwargs):
    return {"request_seconds": client.timeout, "connect_seconds": client.connect_timeout,
            "persistent_session": client.persistent_session,
            "max_connect_retries": client.max_connect_retries,
            "transport_reconnect": kwargs.get("transport_reconnect", False),
            "max_transport_reconnects": kwargs.get("max_transport_reconnects", 2),
            "checker_seconds": (kwargs.get("provenance") or {}).get("transport_limits", {}).get("checker_seconds")}


def _body_limit_host_resumable(record):
    status = record.get("http_status")
    return (record.get("ok") is False and record.get("error") == "ResponseBodyLimitExceeded"
            and type(status) is int and 200 <= status < 300)


def transport_failure(record: dict) -> bool:
    status = record.get("http_status")
    return (record.get("ok") is False and record.get("error") in TRANSPORT_FAILURES
            and (status is None or type(status) is int and 200 <= status < 300))


def _atomic_json(path: Path, value):
    fd, name = tempfile.mkstemp(prefix="checkpoint-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(_json(value) + "\n"); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if Path(name).exists(): Path(name).unlink()


def _workspace_hashes(run_dir):
    result = {}
    for path in sorted((run_dir / "workspace").rglob("*")):
        if path.is_symlink(): raise ValueError("resume workspace contains a symlink")
        if path.is_file(): result[str(path.relative_to(run_dir))] = _sha(path.read_bytes())
    return result


def _generated_index_hash(run_dir):
    path = run_dir / "host-generated.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_nlink != 1:
        raise ValueError("unsafe generated reference index")
    return _sha(path.read_bytes())


def verified_energy(result):
    """Only a host-verified source/proof pair can enter float energy tracking."""
    if (not isinstance(result, dict) or result.get("ok") is not True
            or result.get("verified") is not True or result.get("proof_verified") is not True):
        return None
    value = result.get("energy_density")
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def run_agent(client, model, task, run_dir, **kwargs):
    """One exclusive host controller owns a fresh or explicitly resumed run."""
    run_dir = Path(run_dir).absolute()
    if any(p.is_symlink() for p in (run_dir, *run_dir.parents)):
        raise ValueError("run paths cannot traverse symlinks")
    run_dir = _inside_eureka(run_dir)
    if kwargs.get("resume", False):
        if not run_dir.is_dir(): raise ValueError("resume run is missing")
        old = json.loads((run_dir / "manifest.json").read_text())
        expected = transport_config(client, **{name: kwargs[name] for name in (
            "normal_request_tokens", "normal_output_safety_margin", "closing_request_tokens") if name in kwargs})
        if (old.get("runtime_revision") != runtime_revision()
                or old.get("transport_config") != expected
                or old.get("runtime_session_policy") != _session_policy(client, kwargs)):
            raise ValueError("resume requires the exact hybrid_agent_v1 executable and configuration; legacy runs are separate")
    else:
        run_dir.mkdir(parents=True, exist_ok=False)
    fd = os.open(run_dir / "host-controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1: raise ValueError("controller lock must have a single link")
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError("another controller owns this run") from None
        return _run_agent(client, model, task, run_dir, **kwargs)
    finally:
        os.close(fd)


def account_budget_error(record: dict) -> dict | None:
    """Only structured failed HTTP error codes establish a hard account limit.

    Assistant content, error messages and unparsed response text are never
    scanned. A generic 429 or quota_exceeded can still be a transient limit.
    """
    status = record.get("http_status")
    if (record.get("ok") is not False or type(status) is not int
            or not 400 <= status < 600):
        return None
    response = record.get("response")
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return None
    for field in ("type", "code"):
        code = error.get(field)
        if isinstance(code, str) and code in ACCOUNT_BUDGET_ERROR_CODES:
            return {"field": "response.error." + field, "code": code}
    return None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inside_eureka(path: Path) -> Path:
    result = path.resolve()
    if not result.is_relative_to(EUREKA):
        raise ValueError("benchmark paths must remain inside Eureka")
    return result


def parse_action(content: str) -> dict:
    """Exactly one JSON action. Prose/code fences cannot execute host commands."""
    if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
        raise ValueError("action missing or too large")
    value = strict_json(content.strip())
    if (not isinstance(value, dict) or set(value) != {"tool", "arguments"}
            or not isinstance(value["tool"], str)
            or not isinstance(value["arguments"], dict)):
        raise ValueError("expected exactly tool and arguments")
    return value


def native_tools() -> list[dict]:
    """One local capability broker; the transport additionally exposes hosted web search."""
    return [{"type": "function", "function": {
        "name": "benchmark_action", "description": "Request exactly one documented host benchmark action.",
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string", "enum": list(ACTION_NAMES)},
            "arguments": {"type": "object"}},
            "required": ["tool", "arguments"], "additionalProperties": False}}}]


def native_payload(model: str, messages: list[dict], max_tokens: int,
                   completion_token_field: str = "max_tokens") -> dict:
    return {"model": model, "messages": messages,
            **completion_token_fields(max_tokens, completion_token_field),
            "stream": True, "stream_options": {"include_usage": True},
            "tools": native_tools(), "tool_choice": "auto"}


def native_assistant_message(record: dict) -> dict:
    """Reassemble choice-zero SSE function fragments and provider thinking.

    Provider-exposed thinking blocks are round-tripped unchanged when required
    by the provider's tool protocol. They never constitute host tool results.
    """
    if not record.get("raw_sse_events"):
        try:
            response = record["response"]
            choice = response["choices"][0]
            message = dict(choice["message"])
            message["role"] = "assistant"
            record.update(usage=response.get("usage"), returned_model=response.get("model"),
                          finish_reason=choice.get("finish_reason"))
        except (KeyError, TypeError, IndexError):
            raise ValueError("invalid native response schema") from None
    else:
        calls, blocks, reasoning = {}, [], []
        for event in record["raw_sse_events"]:
            for choice in (event.get("json") or {}).get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                for item in delta.get("tool_calls") or []:
                    if not isinstance(item, dict) or type(item.get("index", 0)) is not int:
                        raise ValueError("invalid native tool fragment")
                    call = calls.setdefault(item.get("index", 0), {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    value = item.get("id")
                    if value:
                        if not isinstance(value, str):
                            raise ValueError("invalid native tool identifier")
                        if call["id"] != value:
                            call["id"] += value
                    function = item.get("function") or {}
                    for field in ("name", "arguments"):
                        value = function.get(field)
                        if value is not None:
                            if not isinstance(value, str):
                                raise ValueError("invalid native function fragment")
                            call["function"][field] += value
                for block in (delta.get("thinking_blocks") or
                              (delta.get("provider_specific_fields") or {}).get("thinking_blocks") or []):
                    if block not in blocks:
                        blocks.append(block)
                if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                    reasoning.append(delta["reasoning_content"])
        message = {"role": "assistant", "content": record.get("content") or None,
                   "tool_calls": [calls[i] for i in sorted(calls)]}
        if blocks:
            message["thinking_blocks"] = blocks
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("exactly one native benchmark_action call is required")
    call = calls[0]
    if (not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]
            or call.get("type") != "function"
            or (call.get("function") or {}).get("name") != "benchmark_action"):
        raise ValueError("unregistered native tool or missing identifier")
    return message


class ToolBroker:
    """Capabilities for exactly one model/task pair.

    ``reference`` is an explicit host-provided public reference map, not a
    recursive project mount. Candidate reads cannot inspect API traces,
    other runs, answer files, environment variables or credentials. There is
    no shell tool. Computational callbacks receive immutable invocation copies.
    """

    def __init__(self, run_dir: Path, *, references: dict[str, str] | None = None,
                 prepare: ToolCallback | None = None,
                 run_lean: ToolCallback | None = None,
                 score: ToolCallback | None = None,
                 server: ToolCallback | None = None):
        self.run_dir = _inside_eureka(Path(run_dir))
        self.workspace = self.run_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.invocations = self.run_dir / "invocations"
        self.invocations.mkdir(exist_ok=True)
        self.callbacks = {"prepare": prepare, "run_lean": run_lean, "score": score, "server": server}
        self.references = {}
        for name, content in (references or {}).items():
            key = self._relative(name)
            if key.parts[0] == "reference" or not isinstance(content, str):
                raise ValueError("reference names are relative to reference/")
            if len(content.encode()) > MAX_FILE_BYTES:
                raise ValueError("reference file is too large")
            self.references[key.as_posix()] = content
        self.serial = 0
        self.last_verified = None
        generated_file = self.run_dir / "host-generated.json"
        self.generated = {}
        if generated_file.exists():
            if generated_file.is_symlink() or generated_file.stat().st_nlink != 1:
                raise ValueError("unsafe host-generated reference index")
            self.generated = json.loads(generated_file.read_text())
            if not isinstance(self.generated, dict):
                raise ValueError("invalid generated reference index")
            for name, item in self.generated.items():
                if (self._relative(name).parts[0] != "generated" or not isinstance(item, dict)
                        or not isinstance(item.get("content"), str)
                        or len(item["content"].encode()) > MAX_FILE_BYTES
                        or _sha(item["content"].encode()) != item.get("sha256")):
                    raise ValueError("invalid generated reference binding")

    @staticmethod
    def _relative(name) -> PurePosixPath:
        if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
                or len(name) > 240):
            raise ValueError("invalid relative workspace path")
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or any(part in (".", "..") or part.startswith(".")
                                     for part in path.parts):
            raise ValueError("absolute, hidden or parent paths are forbidden")
        return path

    def _path(self, name, *, write=False) -> Path:
        relative = self._relative(name)
        if relative.parts[0] in ("reference", "generated"):
            raise ValueError("reference and generated files are read-only virtual files")
        path = self.workspace.joinpath(*relative.parts)
        for parent in (path, *path.parents):
            if parent == self.workspace.parent:
                break
            if parent.is_symlink():
                raise ValueError("workspace symlinks are forbidden")
        if not path.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("workspace path escaped")
        if not write and (not path.is_file() or path.stat().st_size > MAX_FILE_BYTES):
            raise ValueError("candidate file missing or too large")
        return path

    @staticmethod
    def _keys(args: dict, required: set[str], optional: set[str] | None = None):
        if not required <= set(args) or set(args) - required - (optional or set()):
            raise ValueError("invalid tool arguments")

    def _compute(self, tool: str, args: dict) -> dict:
        if tool in ("score", "run_lean"):
            self._keys(args, {"source", "proof"})
        elif tool == "prepare":
            self._keys(args, {"source"})
        elif tool == "server":
            self._keys(args, {"candidate"}, {"cpus", "wall_seconds", "arguments"})
            cpus = args.get("cpus", 1)
            wall = args.get("wall_seconds", 120)
            if type(cpus) is not int or cpus != 1:
                raise ValueError("each local numerical call must use exactly one CPU")
            if (type(wall) not in (int, float) or not math.isfinite(wall)
                    or not 1 <= wall <= 300):
                raise ValueError("server wall_seconds must be in 1..300")
            if not isinstance(args.get("arguments", []), list) or any(
                    not isinstance(x, str) or len(x) > 1000 for x in args.get("arguments", [])):
                raise ValueError("server arguments must be bounded strings")
        callback = self.callbacks[tool]
        if callback is None:
            return {"ok": False, "error": "host_tool_unavailable", "tool": tool}
        self.serial += 1
        invocation = self.invocations / f"{self.serial:04d}-{tool}"
        invocation.mkdir()
        def freeze(name: str, target: str, limit: int, suffix: str):
            original = self._path(name)
            if original.suffix != suffix:
                raise ValueError("expected " + suffix + " source")
            fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as incoming:
                before = os.fstat(incoming.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_size > limit):
                    raise ValueError("unsafe or oversized candidate source")
                content = incoming.read(limit + 1)
                after = os.fstat(incoming.fileno())
                if (len(content) > limit or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or before.st_ino != after.st_ino or after.st_nlink != 1):
                    raise ValueError("candidate changed while freezing")
            frozen = invocation / target
            frozen.write_bytes(content)
            frozen.chmod(0o444)
            return frozen, _sha(content)

        binding = {}
        kwargs = {"invocation_dir": invocation}
        if tool == "server":
            frozen, hashed = freeze(args["candidate"], "Experiment.py", MAX_FILE_BYTES, ".py")
            kwargs["candidate_path"] = frozen
            binding["candidate_sha256"] = hashed
            kwargs.update(cpus=args.get("cpus", 1), wall_seconds=args.get("wall_seconds", 120),
                          arguments=args.get("arguments", []))
        else:
            frozen, hashed = freeze(args["source"], "Source.py", 65536, ".py")
            kwargs["source_path"] = frozen
            binding["source_sha256"] = hashed
            if tool in ("run_lean", "score"):
                proof, proof_sha = freeze(args["proof"], "Candidate.lean", MAX_FILE_BYTES, ".lean")
                kwargs["proof_path"] = proof
                binding["proof_sha256"] = proof_sha
        result = callback(**kwargs)
        if not isinstance(result, dict):
            raise TypeError("host callback must return a dict")
        result = {**result, **binding}
        saved_result = result
        if tool == "prepare" and result.get("ok") is True:
            definition = result.get("generated_state")
            if not isinstance(definition, str) or len(definition.encode()) > MAX_FILE_BYTES:
                raise ValueError("prepare must return a bounded generated_state definition")
            paths = {}
            for field, filename in (("generated_state", "GeneratedState.lean"),
                                    ("proof_template", "Candidate.lean.txt")):
                text = result.get(field)
                if text is None:
                    continue
                if not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES:
                    raise ValueError("invalid generated reference text")
                name = "generated/" + binding["source_sha256"] + "/" + filename
                self.generated[name] = {"content": text, "sha256": _sha(text.encode()),
                                        "source_sha256": binding["source_sha256"]}
                paths[field] = name
            _atomic_json(self.run_dir / "host-generated.json", self.generated)
            result = {key: value for key, value in result.items()
                      if key not in ("generated_state", "proof_template")}
            result["generated_paths"] = paths
            saved_result = {**saved_result, "generated_paths": paths}
        # Preserve complete host result outside the candidate's read capability.
        (invocation / "result.json").write_text(_json(saved_result) + "\n")
        return result

    def dispatch(self, action: dict) -> dict:
        tool, args = action["tool"], action["arguments"]
        try:
            if tool == "list_files":
                self._keys(args, set())
                files = [p.relative_to(self.workspace).as_posix()
                         for p in self.workspace.rglob("*") if p.is_file() and not p.is_symlink()]
                return {"ok": True, "files": sorted(files),
                        "references": ["reference/" + name for name in sorted(self.references)],
                        "generated": sorted(self.generated)}
            if tool == "read":
                self._keys(args, {"path"}, {"start_line", "max_lines"})
                name = self._relative(args["path"])
                if name.parts[0] == "reference":
                    content = self.references[PurePosixPath(*name.parts[1:]).as_posix()]
                elif name.parts[0] == "generated":
                    content = self.generated[name.as_posix()]["content"]
                else:
                    content = self._path(args["path"]).read_text()
                start, count = args.get("start_line", 1), args.get("max_lines", 240)
                if type(start) is not int or start < 1 or type(count) is not int or not 1 <= count <= 1000:
                    raise ValueError("read line range must start at 1 or later and contain 1..1000 lines")
                lines = content.splitlines(keepends=True)
                return {"ok": True, "content": "".join(lines[start-1:start-1+count]),
                        "sha256": _sha(content.encode()), "start_line": start,
                        "total_lines": len(lines), "next_line": start+count if start-1+count < len(lines) else None}
            if tool == "write":
                self._keys(args, {"path", "content"}, {"append"})
                content = args["content"]
                if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
                    raise ValueError("content must be bounded UTF-8 text")
                append = args.get("append", False)
                if type(append) is not bool:
                    raise ValueError("append must be boolean")
                path = self._path(args["path"], write=True)
                if append and path.exists():
                    if not path.is_file() or path.stat().st_size + len(content.encode()) > MAX_FILE_BYTES:
                        raise ValueError("combined candidate file exceeds max_file_bytes")
                    content = path.read_text() + content
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                return {"ok": True, "bytes": len(content.encode()), "sha256": _sha(content.encode())}
            if tool in ("prepare", "run_lean", "score", "server"):
                return self._compute(tool, args)
            if tool == "finish":
                self._keys(args, {"source", "proof"}, {"summary"})
                if "summary" in args and not isinstance(args["summary"], str):
                    raise ValueError("summary must be text")
                result = self._compute("score", {k: args[k] for k in ("source", "proof")})
                # Only host-verified scores can finish. Self-reported receipts,
                # success flags and energies have no authority.
                if verified_energy(result) is not None:
                    self.last_verified = result
                    return {**result, "finished": True}
                return {**result, "finished": False}
            if tool == "final_submit":
                self._keys(args, {"python_source", "lean_proof"}, {"summary"})
                if not isinstance(args["python_source"], str) or len(args["python_source"].encode()) > 65536:
                    raise ValueError("inline Python source must be at most 64 KiB")
                for field, name in (("python_source", "FinalState.py"), ("lean_proof", "FinalCandidate.lean")):
                    written = self.dispatch({"tool": "write", "arguments": {
                        "path": name, "content": args[field]}})
                    if not written.get("ok"):
                        return written
                return self.dispatch({"tool": "finish", "arguments": {
                    "source": "FinalState.py", "proof": "FinalCandidate.lean",
                    "summary": args.get("summary", "")}})
            return {"ok": False, "error": "unknown_tool"}
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as error:
            # No arbitrary exception repr or inherited environment is returned.
            return {"ok": False, "error": type(error).__name__, "detail": str(error)[:1000]}


@dataclass(frozen=True)
class AgentLimits:
    max_turns: int = 40
    max_tokens_per_turn: int = 16000
    wall_seconds: float = 14400
    max_context_characters: int = 1_500_000
    max_total_output_tokens: int = 128000
    max_total_input_tokens: int | None = None
    max_verifier_calls: int | None = None
    max_server_cpu_seconds: float | None = None
    final_output_reserve: int = 0


def input_token_reservation(messages: list[dict]) -> int:
    """Conservative application estimate, not a provider tokenizer count."""
    return len(_json(messages).encode()) + 128 * len(messages) + 2048


def _final_usage(manifest):
    rows = [row for row in manifest.get("budget_ledger", [])
            if row.get("kind") == "api" and row.get("phase") == "final"]
    if any(type(row.get("output_charged")) is not int or row["output_charged"] < 0 for row in rows):
        raise ValueError("invalid closing output ledger")
    return len(rows), sum(row["output_charged"] for row in rows)


def _closing_notice(manifest, limits, protocol, reason):
    responses, charged = _final_usage(manifest)
    text = ("Host closing policy supersedes any earlier ONE-final-response instruction. " + reason +
            ". At most " + str(FINAL_MAX_RESPONSES) + " closing API responses in total, including failed "
            "transport attempts, share the original " + str(limits.final_output_reserve) +
            " output-token reserve. Remaining closing responses: " + str(max(0, FINAL_MAX_RESPONSES-responses)) +
            "; remaining closing output tokens: " + str(max(0, limits.final_output_reserve-charged)) +
            ". All original total request, input, output, wall and verifier budgets still apply. ")
    request_cap = manifest.get("transport_config", {}).get("closing_request_tokens", 2048)
    text += ("This transport revision requests at most " + str(request_cap) +
             " output tokens per closing response, within those unchanged cumulative limits. "
             "A short finish call can submit a proof already saved in the workspace. ")
    if protocol == "native_function":
        text += ("Call the registered benchmark_action API function exactly once. Its TOP-LEVEL function "
                 'arguments must be {"tool":"final_submit","arguments":{"python_source":"<complete Python f>",'
                 '"lean_proof":"<complete public Lean module>"}} or {"tool":"finish","arguments":{"source":"<workspace .py path>",'
                 '"proof":"<workspace .lean path>"}}. The inner arguments contains python_source/lean_proof (and optional summary) '
                 "for final_submit, or source/proof for finish. Never nest another tool/arguments object inside "
                 "that inner arguments. Do not emit prose JSON, XML calls or simulated tool results. ")
    else:
        text += ("Return exactly one JSON action with top-level tool and arguments, choosing final_submit "
                 "with python_source/lean_proof (and optional summary), or finish with source/proof. ")
    return text + ("Only a failed final_submit/finish or a protocol error can be repaired. "
                   "No read, write, list_files, server, scoring exploration or other action is allowed. "
                   "The public module MUST include @[expose] public section after imports. "
                   "The host independently verifies submissions; do not invent a verification result.")


def _last_closing_failure_repairable(last, messages, protocol):
    """Recognize an old host-recorded closing failure without rewriting it."""
    try:
        feedback = messages[-1].get("content", "")
        if feedback.startswith("Host tool result:\n"):
            feedback = feedback.split("\n", 1)[1]
        result = strict_json(feedback)
        if not isinstance(result, dict) or result.get("finished") is True:
            return False
        if result.get("error") == "protocol_error":
            return True
        if protocol == "native_function":
            message = native_assistant_message(last)
            content = message["tool_calls"][0]["function"]["arguments"]
        else:
            content = last.get("content") or ""
        return parse_action(content)["tool"] in ("final_submit", "finish")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def _run_agent(client: RelayClient, model: str, task: dict, run_dir: Path, *,
              references: dict[str, str] | None = None,
              prepare: ToolCallback | None = None,
              run_lean: ToolCallback | None = None,
              score: ToolCallback | None = None,
              server: ToolCallback | None = None,
              limits: AgentLimits | None = None,
              system_prompt: str | None = None,
              require_tools: tuple[str, ...] = (),
              provenance: dict | None = None,
              progress: Callable[[str, dict], None] | None = None,
              tool_protocol: str = "text_json", transport_reconnect: bool = False,
              max_transport_reconnects: int = 2, resume: bool = False,
              normal_request_tokens: int = 8000, normal_output_safety_margin: int = 4096,
              closing_request_tokens: int = 2048) -> dict:
    """Isolated conversation, with explicitly authorized transport recovery."""
    limits = limits or AgentLimits()
    revision = runtime_revision()
    config = transport_config(client, normal_request_tokens, normal_output_safety_margin,
                              closing_request_tokens)
    session_policy = _session_policy(client, {"transport_reconnect": transport_reconnect,
        "max_transport_reconnects": max_transport_reconnects, "provenance": provenance})
    if (type(transport_reconnect) is not bool or type(resume) is not bool
            or type(max_transport_reconnects) is not int or not 0 <= max_transport_reconnects <= 2):
        raise ValueError("invalid explicit transport recovery policy")
    if tool_protocol not in ("text_json", "native_function"):
        raise ValueError("unknown tool protocol")
    if any(name not in ("prepare", "run_lean", "score", "server") for name in require_tools):
        raise ValueError("required tools must be computational broker tools")
    if any(callback is None for name, callback in
           (("prepare", prepare), ("run_lean", run_lean), ("score", score), ("server", server)) if name in require_tools):
        raise ValueError("a required computational callback is unavailable")
    for name in ("max_turns", "max_tokens_per_turn", "max_context_characters", "max_total_output_tokens"):
        if type(getattr(limits, name)) is not int or getattr(limits, name) <= 0:
            raise ValueError("agent integer limits must be positive")
    if not math.isfinite(limits.wall_seconds) or limits.wall_seconds <= 0:
        raise ValueError("wall_seconds must be positive and finite")
    for name in ("max_total_input_tokens", "max_verifier_calls"):
        value = getattr(limits, name)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"{name} must be a positive integer or None")
    if limits.max_server_cpu_seconds is not None and (
            type(limits.max_server_cpu_seconds) not in (int, float)
            or not math.isfinite(limits.max_server_cpu_seconds)
            or limits.max_server_cpu_seconds <= 0):
        raise ValueError("max_server_cpu_seconds must be positive and finite or None")
    if (type(limits.final_output_reserve) is not int or limits.final_output_reserve < 0
            or limits.final_output_reserve >= limits.max_total_output_tokens):
        raise ValueError("final_output_reserve must be in 0..max_total_output_tokens-1")
    run_dir = _inside_eureka(Path(run_dir))
    api_dir = run_dir / "api"
    api_dir.mkdir(exist_ok=resume)
    broker = ToolBroker(run_dir, references=references, prepare=prepare,
                        run_lean=run_lean, score=score, server=server)
    if system_prompt is None:
        raise ValueError("hybrid trials require an explicit source/proof system prompt")
    if tool_protocol == "native_function":
        system_prompt = system_prompt.replace(
            'Each response must be exactly ONE JSON object, with no markdown fence or prose:\n',
            'Call the supplied benchmark_action API function exactly ONCE per response. '
            'Its arguments have the following shape. Do not write text JSON, XML invokes, '
            'or simulated tool results in assistant prose:\n')
    if require_tools:
        system_prompt += ("\nThis is an infrastructure pilot, separate from formal scores. Before finish, "
                          "successfully exercise these tools at least once: " + ", ".join(require_tools) +
                          ". Use the server for a small numerical exploration relevant to the task; "
                          "then submit your infinite-state Lean proof and obtain a verified score.\n")
    system_prompt += ("\nHost resource limits for this trial (output tokens include reasoning when the provider reports it):\n" +
                      _json(asdict(limits)) + "\nThe host reports remaining budgets after each tool result.\n")
    public_task = _json(task)
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": "Fixed benchmark task:\n" + public_task +
                 "\nUse list_files to inspect the public API references. Find a legal infinite state and minimize its energy per qubit."}]
    if not resume:
        (run_dir / "task.json").write_text(public_task + "\n")
        (run_dir / "system_prompt.md").write_text(system_prompt)
        (run_dir / "references.json").write_text(_json(references or {}) + "\n")
    manifest = {"schema": "hybrid-correlator-agent/v1", "created_utc": datetime.now(timezone.utc).isoformat(),
                "model": model, "limits": asdict(limits), "task_sha256": _sha(public_task.encode()),
                "system_prompt_sha256": _sha(system_prompt.encode()), "status": "running",
                "isolation": "fresh conversation; capability file broker; networkless host tools",
                "automatic_post_retries": 0, "turns": 0, "usage": [], "cost_usd": [],
                "verified_result": None, "best_verified_result": None,
                "energy_score_semantics": "approximate_float_energy_of_formally_verified_state_not_certified_rounding_bound",
                "output_tokens_charged": 0, "requested_output_tokens": 0,
                "input_tokens_charged": 0, "input_tokens_observed": 0,
                "input_reservation_scheme": "serialized UTF-8 bytes + 128 per message + 2048 overhead; actual provider usage when supplied",
                "verifier_calls": 0, "server_cpu_seconds_charged": 0.0,
                "budget_ledger": [], "final_phase_entered": False,
                "required_tools": list(require_tools), "successful_tools": [],
                "base_url": client.base_url, "provenance": provenance or {}}
    manifest["tool_protocol"] = tool_protocol
    manifest["runtime_revision"] = revision
    manifest["transport_config"] = config
    manifest["runtime_session_policy"] = session_policy
    recovery_policy = {"transport_reconnect": transport_reconnect,
                       "resumable_session": True, "max_transport_reconnects": max_transport_reconnects}
    if resume:
        old = json.loads((run_dir / "manifest.json").read_text())
        saved_messages = json.loads((run_dir / "messages.json").read_text())
        closing_resume = old.get("status") == "final_submission_not_verified"
        if (old.get("status") not in ("infrastructure_failure", "infrastructure_interrupted", "final_submission_not_verified")
                or old.get("model") != model or old.get("limits") != asdict(limits)
                or old.get("task_sha256") != _sha(public_task.encode())
                or old.get("system_prompt_sha256") != _sha(system_prompt.encode())
                or old.get("tool_protocol", "text_json") != tool_protocol
                or old.get("base_url") != client.base_url
                or old.get("required_tools") != list(require_tools)
                or old.get("provenance", {}).get("runtime_sources") != (provenance or {}).get("runtime_sources")
                or json.loads((run_dir / "task.json").read_text()) != task
                or (run_dir / "system_prompt.md").read_text() != system_prompt
                or json.loads((run_dir / "references.json").read_text()) != (references or {})):
            raise ValueError("resume requires the same interrupted model, task, prompt, references and budgets")
        count = old.get("turns")
        if type(count) is not int or count <= 0:
            raise ValueError("resume has no accounted terminal request")
        expected = {f"{i:04d}.json" for i in range(count)}
        if {x.name for x in api_dir.glob("*.json") if not x.name.endswith(".request.json")} != expected:
            raise ValueError("resume API records are incomplete")
        if {x.name for x in api_dir.glob("*.request.json")} != {f"{i:04d}.request.json" for i in range(count)}:
            raise ValueError("resume has an unknown in-flight request")
        last = json.loads((api_dir / f"{count-1:04d}.json").read_text())
        if closing_resume:
            if (last.get("ok") is not True or not old.get("final_phase_entered")
                    or not _last_closing_failure_repairable(last, saved_messages, tool_protocol)):
                raise ValueError("resume requires a repairable failed closing submission")
        elif last.get("ok") is not False or (old.get("infrastructure_retryable") is False and account_budget_error(last) is None):
            raise ValueError("resume cannot bypass a completed response or explicit refusal")
        if old.get("final_phase_entered"):
            final_count, final_charged = _final_usage(old)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(old["created_utc"])).total_seconds()
            if (final_count >= FINAL_MAX_RESPONSES or final_charged >= limits.final_output_reserve
                    or count >= limits.max_turns
                    or old.get("output_tokens_charged", 0) >= limits.max_total_output_tokens
                    or max(old.get("elapsed_seconds", 0), age) >= limits.wall_seconds
                    or (limits.max_total_input_tokens is not None
                        and old.get("input_tokens_charged", 0) >= limits.max_total_input_tokens)
                    or (limits.max_verifier_calls is not None
                        and old.get("verifier_calls", 0) >= limits.max_verifier_calls)):
                raise ValueError("resume has no remaining closing or total budget")
        checkpoint = run_dir / "checkpoint.json"
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text())
            if (state.get("manifest") != old or state.get("messages") != saved_messages
                    or state.get("workspace_sha256") != _workspace_hashes(run_dir)
                    or state.get("generated_index_sha256") != _generated_index_hash(run_dir)):
                raise ValueError("resume checkpoint binding changed")
        manifest = old
        messages = saved_messages
        history = manifest.setdefault("resume_history", [])
        snapshot = run_dir / "resume-history" / f"{len(history)+1:04d}"
        snapshot.mkdir(parents=True, exist_ok=False)
        hashes = {}
        for name in ("manifest.json", "messages.json", "task.json", "references.json", "system_prompt.md"):
            source = run_dir / name
            if source.is_symlink() or source.stat().st_nlink != 1: raise ValueError("unsafe resume input")
            data = source.read_bytes(); (snapshot / name).write_bytes(data); (snapshot / name).chmod(0o444)
            hashes[name] = _sha(data)
        raw_hashes = {str(x.relative_to(run_dir)): _sha(x.read_bytes()) for x in sorted(api_dir.glob("*.json"))}
        history.append({"index": len(history)+1, "starting_turn": count, "snapshot_dir": str(snapshot),
                        "snapshot_sha256": hashes, "raw_prefix_sha256": raw_hashes,
                        "previous_status": old["status"], "resumed_utc": datetime.now(timezone.utc).isoformat(),
                        "previous_transport_limits": old.get("provenance", {}).get("transport_limits", {}),
                        "new_transport_limits": (provenance or {}).get("transport_limits", {})})
        manifest.setdefault("legacy_raw_prefix_turns", count if "logical_step" not in last else 0)
        manifest["provenance"] = provenance or old.get("provenance", {})
        manifest.update(status="running", resume_available=False)
        for field in ("infrastructure_error", "infrastructure_http_status", "infrastructure_retryable",
                      "infrastructure_nonretryable_reason", "infrastructure_account_error_field", "infrastructure_account_error_code"):
            manifest.pop(field, None)
        broker.serial = max([0] + [int(x.name.split("-", 1)[0]) for x in broker.invocations.iterdir()
                                  if x.is_dir() and re.fullmatch(r"\d+-(?:prepare|run_lean|score|server)", x.name)])
    manifest["recovery_policy"] = recovery_policy
    manifest.setdefault("agent_transport_reconnects", 0)
    base_elapsed = manifest.get("elapsed_seconds", 0.0)
    if resume:
        base_elapsed = max(base_elapsed, (datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_utc"])).total_seconds())
    start = time.monotonic() - base_elapsed

    def persist():
        final_count, final_charged = _final_usage(manifest)
        manifest.update(final_repair_policy={"max_responses": FINAL_MAX_RESPONSES,
                                            "total_output_cap": limits.final_output_reserve},
                        final_responses=final_count, final_output_tokens_charged=final_charged)
        manifest["elapsed_seconds"] = time.monotonic() - start
        saved_manifest = client.redact(manifest); saved_messages = client.redact(messages)
        _atomic_json(run_dir / "manifest.json", saved_manifest)
        _atomic_json(run_dir / "messages.json", saved_messages)
        _atomic_json(run_dir / "checkpoint.json", {"schema": "hybrid-correlator-agent-checkpoint/v1",
            "manifest": saved_manifest, "messages": saved_messages, "workspace_sha256": _workspace_hashes(run_dir),
            "generated_index_sha256": _generated_index_hash(run_dir)})

    def event(kind, value):
        with (run_dir / "trace.jsonl").open("a") as stream:
            stream.write(_json(client.redact({"kind": kind, "elapsed_seconds": time.monotonic() - start,
                                             "value": value})) + "\n")
        if progress is not None:
            progress(kind, value)

    if resume and manifest.get("final_phase_entered"):
        messages.append({"role": "user", "content": _closing_notice(
            manifest, limits, tool_protocol, "Continue the existing closing phase under this uniform policy")})
    persist()
    final_phase = manifest.get("final_phase_entered", False)
    recovery = None
    logical_step = manifest["turns"]
    for turn in range(manifest["turns"], limits.max_turns):
        if time.monotonic() - start >= limits.wall_seconds:
            manifest["status"] = "wall_budget_exhausted"
            break
        if sum(len(_json(m)) for m in messages) > limits.max_context_characters:
            manifest["status"] = "context_budget_exhausted"
            break
        remaining = limits.max_total_output_tokens - manifest["output_tokens_charged"]
        if remaining <= 0:
            manifest["status"] = "output_budget_exhausted"
            break
        estimated_input = input_token_reservation(messages)
        normal_remaining = remaining - limits.final_output_reserve
        closing_reason = None
        if limits.final_output_reserve:
            if turn >= limits.max_turns - 1:
                closing_reason = "normal_turn_budget_exhausted"
            elif normal_remaining <= normal_output_safety_margin:
                closing_reason = "normal_output_safety_margin_reserved_for_final"
            elif (limits.max_verifier_calls is not None
                  and manifest["verifier_calls"] >= limits.max_verifier_calls - 1):
                closing_reason = "last_verifier_call_reserved_for_final"
            elif (limits.max_total_input_tokens is not None
                  and manifest["input_tokens_charged"] + 2 * estimated_input
                      + 4 * limits.max_tokens_per_turn + 2 * MAX_TOOL_RESULT_BYTES + 1024
                      > limits.max_total_input_tokens):
                closing_reason = "input_budget_reserved_for_final"
        if closing_reason and not final_phase:
            final_phase = True
            manifest["final_phase_entered"] = True
            messages.append({"role": "user", "content": _closing_notice(
                manifest, limits, tool_protocol, "The normal search phase has ended: " + closing_reason)})
            event("final_phase", {"reason": closing_reason})
            estimated_input = input_token_reservation(messages)
        if (limits.max_total_input_tokens is not None
                and manifest["input_tokens_charged"] + estimated_input > limits.max_total_input_tokens):
            manifest["status"] = "input_budget_exhausted"
            break
        final_count, final_charged = _final_usage(manifest)
        if final_phase and (final_count >= FINAL_MAX_RESPONSES
                            or final_charged >= limits.final_output_reserve
                            or (limits.max_verifier_calls is not None
                                and manifest["verifier_calls"] >= limits.max_verifier_calls)):
            manifest.update(status="final_submission_not_verified", resume_available=False)
            break
        turn_tokens = (min(closing_request_tokens, limits.max_tokens_per_turn,
                          limits.final_output_reserve-final_charged, remaining) if final_phase
                       else min(normal_request_tokens, limits.max_tokens_per_turn,
                           normal_remaining-normal_output_safety_margin if limits.final_output_reserve else remaining))
        # Persist the outbound request before network I/O so a host-level
        # termination cannot make an in-flight paid request disappear.
        planned = {"model": model, "messages": messages,
                   **completion_token_fields(turn_tokens, client.completion_token_field),
                   "stream": True, "stream_options": {"include_usage": True},
                   "tools": [], "tool_choice": "none"}
        if tool_protocol == "native_function":
            planned = native_payload(model, messages, turn_tokens, client.completion_token_field)
        message_hash = _sha(_json(planned["messages"]).encode())
        if recovery is None or recovery["messages_sha256"] != message_hash:
            recovery = {"logical_step": logical_step, "transport_attempt": 0,
                        "messages_sha256": message_hash, "deadline": time.monotonic() + client.timeout}
            logical_step += 1
        request_seconds = min(recovery["deadline"] - time.monotonic(),
                              limits.wall_seconds - (time.monotonic() - start))
        if request_seconds <= 0:
            manifest.update(status="infrastructure_failure", infrastructure_error="recovery_deadline_exhausted",
                            infrastructure_retryable=True, resume_available=transport_reconnect)
            break
        (api_dir / f"{turn:04d}.request.json").write_text(_json(client.redact({
            "started_utc": datetime.now(timezone.utc).isoformat(), "request": planned,
            "runtime_revision": revision, "transport_config": config})) + "\n")
        # One RelayClient belongs to one run; bounding this request timeout also
        # prevents starting a full-length request at the end of the run budget.
        original_timeout = client.timeout
        client.timeout = min(original_timeout, max(.001, request_seconds))
        try:
            if tool_protocol == "native_function":
                record = client._request("POST", "/chat/completions", planned, stream=True,
                                         request_timeout_seconds=request_seconds)
                native_message = None
                if record.get("ok"):
                    try:
                        native_message = native_assistant_message(record)
                        # A truncated function argument is not a valid tool
                        # message for the next provider request. Preserve it in
                        # raw API evidence, never fabricate a host execution.
                        parse_action(native_message["tool_calls"][0]["function"]["arguments"])
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        record["native_protocol_error"] = type(error).__name__ + ": " + str(error)[:1000]
                        native_message = None
            else:
                record = client.complete(model, messages, max_tokens=turn_tokens,
                                         stream=True, forbid_tools=True)
        finally:
            client.timeout = original_timeout
        ambiguous_usage = (client.reasoning_usage_policy == "included"
                           and inconsistent_output_usage(record.get("usage")))
        if ambiguous_usage:
            record.update(ok=False, error="InconsistentUsage",
                          error_detail="provider reasoning_tokens exceeds completion_tokens",
                          usage_accounting_ambiguous=True)
        record.update(logical_step=recovery["logical_step"], transport_attempt=recovery["transport_attempt"],
                      request_messages_sha256=message_hash, agent_post_retry=recovery["transport_attempt"] > 0,
                      runtime_revision=revision, transport_config=config)
        (api_dir / f"{turn:04d}.json").write_text(_json(record) + "\n")
        manifest["turns"] = turn + 1
        manifest["usage"].append(record.get("usage"))
        manifest["cost_usd"].append(record.get("response_metadata", {}).get("cost_usd"))
        used = (record.get("usage") or {}).get("completion_tokens")
        reservation = output_budget_reservation(record.get("usage"), turn_tokens, client.reasoning_usage_policy)
        charged = reservation["output_charged"]
        manifest["output_tokens_charged"] += charged
        manifest["requested_output_tokens"] += turn_tokens
        observed_input = (record.get("usage") or {}).get("prompt_tokens")
        input_charge = observed_input if type(observed_input) is int and observed_input >= 0 else estimated_input
        manifest["input_tokens_charged"] += input_charge
        if type(observed_input) is int and observed_input >= 0:
            manifest["input_tokens_observed"] += observed_input
        manifest["budget_ledger"].append({"kind": "api", "turn": turn,
            "input_reservation": estimated_input, "input_charged": input_charge,
            "input_provider_reported": observed_input, "output_requested": turn_tokens,
            **reservation,
            "phase": "final" if final_phase else "normal"})
        final_count, final_charged = _final_usage(manifest)
        closing_budget_available = (not final_phase or
            final_count < FINAL_MAX_RESPONSES and final_charged < limits.final_output_reserve)
        event("api", {"turn": turn, "record": f"api/{turn:04d}.json",
                      "ok": record.get("ok"), "latency_seconds": record.get("latency_seconds")})
        if not record.get("ok"):
            http_status = record.get("http_status")
            retryable = not (type(http_status) is int and 400 <= http_status < 500
                             and http_status not in (408, 429))
            if ambiguous_usage:
                retryable = False
                manifest["infrastructure_nonretryable_reason"] = "inconsistent_usage"
            hard_budget = account_budget_error(record)
            if hard_budget is not None:
                retryable = False
                manifest.update(infrastructure_nonretryable_reason="account_budget_exhausted",
                                infrastructure_account_error_field=hard_budget["field"],
                                infrastructure_account_error_code=hard_budget["code"])
            manifest.update(status="infrastructure_failure", infrastructure_error=record.get("error"),
                            infrastructure_http_status=http_status,
                            infrastructure_retryable=retryable,
                            resume_available=transport_reconnect and retryable
                                and (transport_failure(record) or _body_limit_host_resumable(record))
                                and closing_budget_available and turn+1 < limits.max_turns
                                and manifest["output_tokens_charged"] < limits.max_total_output_tokens
                                and (limits.max_total_input_tokens is None
                                     or manifest["input_tokens_charged"] < limits.max_total_input_tokens))
            if (transport_reconnect and transport_failure(record)
                    and recovery["transport_attempt"] < max_transport_reconnects
                    and recovery["deadline"] > time.monotonic() and turn+1 < limits.max_turns
                    and manifest["output_tokens_charged"] < limits.max_total_output_tokens
                    and closing_budget_available):
                try:
                    client.reset_connection()
                except RuntimeError:
                    manifest.update(infrastructure_error="connection_reset_failed", resume_available=True)
                    persist()
                    break
                recovery["transport_attempt"] += 1
                manifest["agent_transport_reconnects"] += 1
                manifest.update(status="running", resume_available=False)
                event("transport_reconnect", {"logical_step": recovery["logical_step"],
                    "next_transport_attempt": recovery["transport_attempt"], "failed_record": f"api/{turn:04d}.json",
                    "request_messages_sha256": message_hash})
                persist()
                continue
            persist()
            break
        manifest["resume_available"] = False
        for field in ("infrastructure_error", "infrastructure_http_status", "infrastructure_retryable"):
            manifest.pop(field, None)
        if (manifest["output_tokens_charged"] > limits.max_total_output_tokens
                or final_phase and final_charged > limits.final_output_reserve
                or (limits.max_total_input_tokens is not None
                    and manifest["input_tokens_charged"] > limits.max_total_input_tokens)):
            manifest["status"] = "provider_usage_exceeded_budget"
            persist()
            break
        content = record.get("content") or ""
        native_call_id = None
        if tool_protocol == "native_function" and native_message is not None:
            messages.append(native_message)
            call = native_message["tool_calls"][0]
            native_call_id = call["id"]
            content = call["function"]["arguments"]
        else:
            # Hosted search may finish without a local benchmark_action. Keep
            # every original output item for stateless replay even when the
            # local action protocol needs repair. No hosted call is executed
            # or acknowledged by the local capability broker.
            normalized_message = (((record.get("response") or {}).get("choices") or [{}])[0]
                                  .get("message") or {})
            output_items = normalized_message.get("responses_output_items")
            replay_hosted_only = (
                tool_protocol == "native_function"
                and record.get("response_status") == "completed"
                and isinstance(output_items, list)
                and any(item.get("type") == "web_search_call" for item in output_items)
                and not any(item.get("type") == "function_call" for item in output_items)
            )
            if replay_hosted_only:
                from copy import deepcopy
                messages.append(deepcopy(normalized_message))
            elif content or tool_protocol == "text_json":
                messages.append({"role": "assistant", "content": content})
        action = None
        try:
            if tool_protocol == "native_function" and native_message is None:
                raise ValueError("missing native function call")
            action = parse_action(content)
            event("tool_request", action)
            missing = sorted(set(require_tools) - set(manifest["successful_tools"]))
            tool = action["tool"]
            if final_phase and tool in ("final_submit", "finish"):
                # Invalid nested wrappers are protocol errors, not checker
                # invocations. The model receives feedback and must repair it.
                broker._keys(action["arguments"],
                             {"python_source", "lean_proof"} if tool == "final_submit" else {"source", "proof"},
                             {"summary"})
            verifier_tool = tool in ("run_lean", "score", "finish", "final_submit")
            server_request = action["arguments"].get("wall_seconds", 120) if tool == "server" else None
            server_valid = (type(server_request) in (int, float) and math.isfinite(server_request)
                            and 1 <= server_request <= 300)
            if final_phase and tool not in ("finish", "final_submit"):
                result = {"ok": False, "error": "final_action_required"}
            elif tool in ("finish", "final_submit") and missing:
                result = {"ok": False, "error": "pilot_tools_not_exercised", "missing": missing}
            elif (verifier_tool and limits.max_verifier_calls is not None
                  and manifest["verifier_calls"] >= limits.max_verifier_calls):
                result = {"ok": False, "error": "verifier_call_budget_exhausted"}
            elif (tool == "server" and server_valid and limits.max_server_cpu_seconds is not None
                  and manifest["server_cpu_seconds_charged"] + server_request > limits.max_server_cpu_seconds):
                result = {"ok": False, "error": "server_cpu_budget_exhausted"}
            else:
                if verifier_tool:
                    manifest["verifier_calls"] += 1
                    manifest["budget_ledger"].append({"kind": "verifier", "turn": turn, "tool": tool})
                if tool == "server" and server_valid:
                    # Single-CPU jobs reserve requested wall seconds. No actual
                    # CPU measurement is fabricated from a numerical payload.
                    manifest["server_cpu_seconds_charged"] += server_request
                    manifest["budget_ledger"].append({"kind": "server_cpu_reservation", "turn": turn,
                                                       "seconds": server_request})
                result = broker.dispatch(action)
            if (result.get("ok") is True and action["tool"] in ("prepare", "run_lean", "score", "server")
                    and action["tool"] not in manifest["successful_tools"]):
                manifest["successful_tools"].append(action["tool"])
        except (ValueError, TypeError, RecursionError):
            result = {"ok": False, "error": "protocol_error",
                      "detail": ("Call the registered benchmark_action API function exactly once with TOP-LEVEL tool and arguments. "
                                 "For final_submit the inner arguments contains python_source and lean_proof, never another tool/arguments wrapper. "
                                 "Do not emit prose JSON, XML calls, simulated calls or simulated tool results."
                                 if tool_protocol == "native_function" else
                                 "Reply with exactly one JSON object having tool and arguments keys. No prose or markdown fences.")}
        event("tool_result", result)
        energy = verified_energy(result)
        if energy is not None:
            best = manifest["best_verified_result"]
            best_energy = verified_energy(best)
            if best_energy is None or energy < best_energy:
                manifest["best_verified_result"] = result
        budget = {"output_tokens_remaining": max(0, limits.max_total_output_tokens - manifest["output_tokens_charged"]),
                  "normal_turns_remaining": max(0, limits.max_turns - turn - 1 - bool(limits.final_output_reserve)),
                  "input_tokens_remaining": None if limits.max_total_input_tokens is None else max(0, limits.max_total_input_tokens-manifest["input_tokens_charged"]),
                  "verifier_calls_remaining": None if limits.max_verifier_calls is None else max(0, limits.max_verifier_calls-manifest["verifier_calls"]),
                  "server_cpu_seconds_remaining": None if limits.max_server_cpu_seconds is None else max(0, limits.max_server_cpu_seconds-manifest["server_cpu_seconds_charged"]),
                  "final_output_reserve": limits.final_output_reserve,
                  "final_responses_remaining": max(0, FINAL_MAX_RESPONSES-final_count),
                  "final_output_tokens_remaining": max(0, limits.final_output_reserve-final_charged)}
        result_text = _json(client.redact({**result, "remaining_budgets": budget}))
        if len(result_text.encode()) > MAX_TOOL_RESULT_BYTES:
            result_text = _json({"ok": result.get("ok", False), "output_truncated": True,
                                 "prefix": result_text[:MAX_TOOL_RESULT_BYTES // 2]})
        if native_call_id is not None:
            messages.append({"role": "tool", "tool_call_id": native_call_id, "content": result_text})
        else:
            messages.append({"role": "user", "content": "Host tool result:\n" + result_text})
        if result.get("finished") is True:
            manifest.update(status="completed", verified_result=broker.last_verified)
            persist()
            break
        if final_phase:
            repairable = (result.get("error") == "protocol_error"
                          or action is not None and action["tool"] in ("final_submit", "finish"))
            budget_available = (closing_budget_available and turn+1 < limits.max_turns
                and manifest["output_tokens_charged"] < limits.max_total_output_tokens
                and (limits.max_total_input_tokens is None
                     or manifest["input_tokens_charged"] < limits.max_total_input_tokens)
                and (limits.max_verifier_calls is None
                     or manifest["verifier_calls"] < limits.max_verifier_calls))
            manifest.update(status="final_submission_not_verified",
                            final_repair_eligible=repairable, resume_available=False)
            if repairable and budget_available:
                messages.append({"role": "user", "content": _closing_notice(
                    manifest, limits, tool_protocol, "The previous closing response failed; repair only its submission or protocol")})
                manifest["status"] = "running"
                recovery = None
                persist()
                continue
            persist()
            break
        recovery = None
        persist()
    else:
        manifest["status"] = "turn_budget_exhausted"
    persist()
    return manifest
