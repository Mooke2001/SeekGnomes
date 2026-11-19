"""Definition-only Constructor handoffs and host-bound Formalist checks.

The prose definition is the Constructor's immutable intent. The Formalist may
implement it, but has no physical optimization or numerical-execution tool.
Only a frozen Python/proof pair checked by the fixed verifier yields energy.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from revisions.dual_agent_v1_1 import callbacks
from revisions.dual_agent_v1_1.tools import (
    RoleToolBroker as FileBroker, _directory, _inside, _mkdir, _new, _json,
    _sha, _relative, _replace, _diagnostic, MAX_FILE_BYTES, SOURCE_LIMIT,
)
from revisions.hybrid_agent_v1.local_numeric import _regular_bytes
from revisions.kimi_adaptive50_web_v1.tools import NetworkRoleBroker
from revisions.kimi_adaptive50_web_v1.web_tools import WebBroker

PROJECT = Path(__file__).resolve().parents[2]
SCHEMA = "seekgnomes-definition-handoff/v1"
CONSTRUCTOR_ACTIONS = ("list_files", "read", "write", "server", "create_handoff", "finish")
FORMALIZER_ACTIONS = ("list_files", "read", "write", "prepare", "run_lean", "score", "report_issue", "finish")
ISSUE_KINDS = frozenset({"unresolved", "definition_ambiguous", "definition_inconsistent",
                         "representation_error", "infrastructure"})
_NAME = re.compile(r"handoff-([0-9]{4,})\Z")


@dataclass(frozen=True)
class Definition:
    index: int
    definition_path: Path
    definition_sha256: str
    notes_path: Path
    notes_sha256: str
    manifest_path: Path


def is_verified(value):
    """Accept only the host's exact fixed-goal, fixed-source receipt."""
    if not isinstance(value, dict) or not all(value.get(k) is True for k in (
            "ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified")):
        return False
    c = value.get("certificate") or {}
    return (type(value.get("energy_density")) in (int, float)
            and math.isfinite(value["energy_density"])
            and c.get("kind") == "hybrid-fixed-goal-kernel-v1"
            and c.get("theorem") == "Submission.accepted"
            and c.get("target") == "GeneratedState.ProofObligation"
            and c.get("source_sha256") == value.get("source_sha256")
            and c.get("candidate_source_sha256") == value.get("proof_sha256")
            and c.get("lock_sha256") == _sha(callbacks.LOCK.read_bytes()))


class DefinitionTools:
    """Own one isolated trial, durable definitions and separate role workspaces."""

    handoff_schema = SCHEMA
    definition_filename = "f.md"
    private_return_filenames = ("f.py", "Candidate.lean", "formalization_map.md")

    def __init__(self, task, trial_dir, *, constructor_references=None,
                 formalizer_references=None, server=None, timeout=300,
                 web=True, web_broker=None, callback_factory=None):
        self.task = json.loads(json.dumps(task, allow_nan=False))
        self.trial_dir = _inside(Path(trial_dir))
        _mkdir(self.trial_dir)
        self.prepare_callback, self.lean_callback, self.score_callback = (
            callback_factory or callbacks.callbacks)(self.task, timeout=timeout)
        # No implicit numerical interpreter: the launcher injects an isolated one.
        self.server = server
        self.web = (web_broker or WebBroker()) if web else None
        self.formalizer_references = dict(formalizer_references or {})
        self._handoffs = []
        self.handoffs, self.handoff_attempts = [], []
        self.handoff, self.formalizer, self.last_verified = None, None, None
        self.constructor_base = self._role_broker("constructor",
            self.trial_dir / "constructor", references=constructor_references)
        self.constructor = self._network(self.constructor_base)
        with self._lock():
            self._recover()

    def _network(self, broker):
        return NetworkRoleBroker(broker, self.web) if self.web is not None else broker

    def _role_broker(self, role, path, **kwargs):
        return DefinitionRoleBroker(self, role, path, **kwargs)

    def _validate_definition(self, definition):
        if not definition.decode().strip():
            raise ValueError("The complete definition cannot be empty")

    @contextmanager
    def _lock(self):
        root = self.trial_dir / "handoffs"
        fd = _directory(root, create=True)
        lock = None
        try:
            lock = os.open(".handoff.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                | os.O_CLOEXEC, 0o600, dir_fd=fd)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unsafe handoff lock")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield root
        finally:
            if lock is not None:
                os.close(lock)
            os.close(fd)

    def _failed(self, root, index, diagnostic):
        result = {"schema": self.handoff_schema, "status": "failed", "handoff_id": index,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "technical_diagnostic": diagnostic, "retryable": True, "proof_verified": False}
        try:
            _new(root / "failed.json", _json(result))
        except Exception:
            pass  # Reserved directory still prevents ID reuse after a full disk.
        return result

    def _read_json(self, path):
        value = json.loads(_regular_bytes(path, MAX_FILE_BYTES))
        if not isinstance(value, dict):
            raise ValueError("Expected a host record object")
        return value

    def _recover(self):
        root = self.trial_dir / "handoffs"
        fd = _directory(root)
        try:
            with os.scandir(fd) as stream:
                entries = [(int(m[1]), e.name,
                    stat.S_ISDIR(e.stat(follow_symlinks=False).st_mode))
                    for e in stream if (m := _NAME.fullmatch(e.name))]
        finally:
            os.close(fd)
        attempts, frozen, records = [], [], []
        for index, name, safe in sorted(entries):
            path = root / name
            if not safe:
                attempts.append({"handoff_id": index, "status": "failed", "error": "unsafe_entry"})
                continue
            if (path / "failed.json").exists():
                attempts.append({"handoff_id": index, "status": "failed"})
                continue
            try:
                commit = self._read_json(path / "committed.json")
            except (FileNotFoundError, json.JSONDecodeError):
                attempts.append(self._failed(path, index, {"kind": "interrupted_attempt"}))
                continue
            raw = _regular_bytes(path / "manifest.json", MAX_FILE_BYTES)
            record = json.loads(raw)
            definition = _regular_bytes(path / self.definition_filename, MAX_FILE_BYTES)
            notes = _regular_bytes(path / "notes.md", MAX_FILE_BYTES)
            if (commit.get("schema") != self.handoff_schema or commit.get("status") != "committed"
                    or commit.get("handoff_id") != index
                    or commit.get("manifest_sha256") != _sha(raw)
                    or record.get("task_sha256") != _sha(_json(self.task))
                    or record.get("definition_sha256") != _sha(definition)
                    or record.get("notes_sha256") != _sha(notes)):
                raise ValueError("Immutable definition handoff binding changed")
            f = Definition(index, path / self.definition_filename, _sha(definition), path / "notes.md",
                _sha(notes), path / "manifest.json")
            frozen.append(f)
            records.append({**record, "manifest_path": str(f.manifest_path)})
            attempts.append(commit)
        current = frozen[-1] if frozen else None
        existing = getattr(self.formalizer, "_handoff", None)
        if current is not None and current != existing:
            refs = {**self.formalizer_references,
                f"handoff/{self.definition_filename}": _regular_bytes(current.definition_path, MAX_FILE_BYTES).decode(),
                "handoff/notes.md": _regular_bytes(current.notes_path, MAX_FILE_BYTES).decode(),
                "handoff/manifest.json": _regular_bytes(current.manifest_path, MAX_FILE_BYTES).decode()}
            self.formalizer = self._network(self._role_broker("formalizer",
                self.trial_dir / "formalizer" / f"handoff-{current.index:04d}",
                references=refs, handoff=current))
        self._handoffs, self.handoffs, self.handoff_attempts = frozen, records, attempts
        self.handoff = records[-1] if records else None
        return max((x[0] for x in entries), default=0)

    def create_handoff(self, definition_path, notes=""):
        suffix = Path(self.definition_filename).suffix
        if _relative(definition_path).suffix != suffix:
            raise ValueError(f"Constructor submits the complete definition as a {suffix} file")
        definition = self.constructor_base._read_private(definition_path)
        self._validate_definition(definition)
        if not isinstance(notes, str) or len(notes.encode()) > 32768:
            raise ValueError("Optional implementation notes must be text up to 32 KiB")
        with self._lock() as parent:
            index = self._recover() + 1
            root = parent / f"handoff-{index:04d}"
            os.mkdir(root, 0o700)
            descriptor = _directory(parent)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                _new(root / "preparing.json", _json({"schema": self.handoff_schema,
                    "status": "preparing", "handoff_id": index}))
                _new(root / self.definition_filename, definition)
                _new(root / "notes.md", notes.encode())
                record = {"schema": self.handoff_schema, "status": "committed", "handoff_id": index,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "task_sha256": _sha(_json(self.task)), "definition_sha256": _sha(definition),
                    "notes_sha256": _sha(notes.encode()), "original_definition_path": definition_path,
                    "proof_verified": False,
                    "definition_reference": f"reference/handoff/{self.definition_filename}",
                    "notes_reference": "reference/handoff/notes.md"}
                _new(root / "manifest.json", _json(record))
                _new(root / "committed.json", _json({"schema": self.handoff_schema,
                    "status": "committed", "handoff_id": index,
                    "manifest_sha256": _sha(_json(record))}))
                descriptor = _directory(root)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._recover()
                return {"ok": True, "action": "handoff", **self.handoff}
            except Exception as error:
                failure = self._failed(root, index, _diagnostic(error))
                self.handoff_attempts.append(failure)
                return {"ok": False, "error": "host_exception", "recoverable": True, **failure}

    def _assert_frozen(self, handoff):
        if not self._handoffs or self._handoffs[-1] != handoff:
            raise ValueError("Stale Formalist handoff")
        definition = _regular_bytes(handoff.definition_path, MAX_FILE_BYTES)
        notes = _regular_bytes(handoff.notes_path, MAX_FILE_BYTES)
        if _sha(definition) != handoff.definition_sha256 or _sha(notes) != handoff.notes_sha256:
            raise ValueError("Immutable Constructor definition changed")
        return definition, notes

    def return_view(self, *, write_snapshot=_new):
        """Expose only this handoff's snapshots, never another role's workspace."""
        broker = getattr(self.formalizer, "base", self.formalizer)
        if broker is None:
            return {}
        self._assert_frozen(broker._handoff)
        index = broker._handoff.index
        root = self.trial_dir / "returns" / f"handoff-{index:04d}"
        _mkdir(root)
        paths = {}
        values = dict(broker.return_files)
        for filename in self.private_return_filenames:
            if filename not in values:
                try:
                    values[filename] = broker._read_private(filename,
                        SOURCE_LIMIT if filename == "f.py" else MAX_FILE_BYTES)
                except FileNotFoundError:
                    continue
        for filename, contents in values.items():
            name = f"generated/return-{index:04d}/{filename}"
            write_snapshot(root / filename, contents)
            self.constructor_base.generated[name] = contents.decode()
            paths[filename] = {"path": name, "sha256": _sha(contents)}
        write_snapshot(root / "index.json", _json(paths))
        return paths


class DefinitionRoleBroker(FileBroker):
    """Reuse the reviewed private file surface; replace only role policy/checks."""

    formalizer_suffixes = {".py", ".lean", ".md"}
    formalizer_write_error = "Formalist may write restricted Python, Lean and Markdown only"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.return_files = {}

    def _write(self, args):
        self._keys(args, {"path", "content"}, {"append"})
        path = self._path(args["path"])
        if self.role == "formalizer" and path.suffix not in self.formalizer_suffixes:
            raise ValueError(self.formalizer_write_error)
        text, append = args["content"], args.get("append", False)
        if not isinstance(text, str) or type(append) is not bool:
            raise ValueError("write requires text and a boolean append flag")
        data = text.encode()
        if append:
            try:
                data = self._read_private(args["path"]) + data
            except FileNotFoundError:
                pass
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Workspace file exceeds its byte limit")
        _replace(path, data)
        return {"ok": True, "bytes": len(data), "sha256": _sha(data)}

    def _formal(self, tool, args):
        proof_fields = {"proof", "proof_path"} & set(args)
        allowed = {"source", "source_path"} | ({"proof", "proof_path"} if tool != "prepare" else set())
        if set(args) - allowed or {"source", "source_path"} <= set(args):
            raise ValueError("Invalid formal-check arguments")
        if tool != "prepare" and len(proof_fields) != 1:
            raise ValueError("A check requires exactly one Lean proof path")
        definition, notes = self.owner._assert_frozen(self._handoff)
        source_name = args.get("source_path", args.get("source", "f.py"))
        if _relative(source_name).suffix != ".py":
            raise ValueError("Expected a restricted Python source")
        source = self._read_private(source_name, SOURCE_LIMIT)
        invocation = self._invocation(tool)
        source_path = invocation / "Source.py"
        _new(source_path, source)
        _new(invocation / "f.md", definition)
        _new(invocation / "notes.md", notes)
        binding = {"handoff_id": self._handoff.index,
            "definition_sha256": self._handoff.definition_sha256,
            "source_sha256": _sha(source)}
        prepared = self._callback(self.owner.prepare_callback, invocation, source_path=source_path)
        if prepared.get("ok") is not True:
            _new(invocation / "result.json", _json({**prepared, **binding}))
            return {**prepared, **binding}
        generated_paths = {}
        for field, filename in (("generated_state", "GeneratedState.lean"),
                                ("proof_template", "Candidate.lean.txt")):
            text = prepared.get(field)
            if not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES:
                raise ValueError("Invalid host-generated reference")
            name = f"generated/{binding['source_sha256']}/{filename}"
            self.generated[name] = text
            _new(invocation / filename, text.encode())
            generated_paths[field] = name
        current_files = {"f.py": source,
            "GeneratedState.lean": prepared["generated_state"].encode()}
        if tool == "prepare":
            result = {k: v for k, v in prepared.items()
                if k not in {"generated_state", "proof_template"}}
            result["generated_paths"] = generated_paths
        else:
            mapping = self._read_private("formalization_map.md")
            if not mapping.decode().strip():
                raise ValueError("Write a definition-to-code/proof map in formalization_map.md first")
            proof_name = args[next(iter(proof_fields))]
            if _relative(proof_name).suffix != ".lean":
                raise ValueError("Expected a .lean proof file")
            proof = self._read_private(proof_name)
            proof_path = invocation / "Candidate.lean"
            _new(proof_path, proof)
            _new(invocation / "formalization_map.md", mapping)
            binding.update(proof_sha256=_sha(proof),
                formalization_map_sha256=_sha(mapping), invocation_dir=str(invocation),
                source_path=str(source_path), proof_path=str(proof_path))
            result = self._callback(self.owner.score_callback, invocation,
                source_path=source_path, proof_path=proof_path)
            result = {**result, **binding}
            current_files.update({"Candidate.lean": proof, "formalization_map.md": mapping})
            if is_verified(result):
                # Immutable callbacks retain their historical /scale metric.
                # Expose it only under an explicit diagnostic name, and use
                # the group-aware release scorer for current feedback.
                if "numerical_normalized_gain" in result:
                    result["legacy_scale_normalized_gain"] = result.pop("numerical_normalized_gain")
                    result["legacy_scale_normalized_gain_meaning"] = (
                        "max(0,(B-E)/scale); legacy diagnostic, not current score amplitude")
                from revisions.seekgnomes_v1_1_1.scoring import task_metrics, score_parameters, public_reference_metadata
                try:
                    result["score_components"] = task_metrics(result["energy_density"],
                        **score_parameters(self.owner.task))
                except (ValueError, KeyError, OSError, ArithmeticError) as error:
                    # The proof receipt remains accepted if a host scoring
                    # reference/configuration has a problem.
                    result["score_components"] = {"pass_rate": 1.0,
                        "improvement_rate": None, "amplitude": None, "score": None,
                        "score_complete": False, "score_status": "scoring_error",
                        "scoring_error": str(error)}
                _new(invocation / "score-components.json", _json(result["score_components"]))
                # Host reports retain complete exact-reference provenance. The
                # role only receives the scalar target, not a supplied solver.
                result["score_components"] = dict(result["score_components"])
                result["score_components"]["ground_energy_reference"] = public_reference_metadata(
                    result["score_components"].get("ground_energy_reference"))
                result["score_rule"] = "30*P+30*R+40*A; exact a=clamp(1-(E-e0)/s,0,1); generic a=baseline_gain=clamp((B-E)/(B-L),0,1); R=mean(baseline_gain>=0.01); groups scored separately"
                result.update(action="feedback", issue_kind=None)
            else:
                result = {k: v for k, v in result.items() if k not in {
                    "energy_density", "energy_report", "terms", "baseline_energy",
                    "numerical_normalized_gain", "energy_normalization"}}
                result.update(ok=False, verified=False, proof_verified=False)
            if _regular_bytes(proof_path, MAX_FILE_BYTES) != proof:
                raise ValueError("Frozen proof changed during verification")
        self.owner._assert_frozen(self._handoff)
        if _regular_bytes(source_path, SOURCE_LIMIT) != source:
            raise ValueError("Frozen Python changed during verification")
        result = {**result, **binding}
        self.return_files = current_files
        _new(invocation / "result.json", _json(result))
        if result.get("action") == "feedback" and is_verified(result):
            self.last_verified = dict(result)
            previous = self.owner.last_verified
            if previous is None or result["energy_density"] < previous["energy_density"]:
                self.owner.last_verified = dict(result)
        return result

    def dispatch(self, action, session=None):
        del session
        try:
            if not isinstance(action, dict) or set(action) != {"tool", "arguments"}:
                raise ValueError("Expected exactly tool and arguments")
            tool, args = action["tool"], action["arguments"]
            if not isinstance(tool, str) or not isinstance(args, dict):
                raise ValueError("Invalid action fields")
            allowed = CONSTRUCTOR_ACTIONS if self.role == "constructor" else FORMALIZER_ACTIONS
            if tool not in allowed:
                return {"ok": False, "error": "role_tool_forbidden", "role": self.role, "tool": tool}
            if self.role == "formalizer":
                self.owner._assert_frozen(self._handoff)
            if tool == "list_files":
                self._keys(args, set())
                return self._list()
            if tool == "read":
                return self._read(args)
            if tool == "write":
                return self._write(args)
            if tool == "server":
                if self.owner.server is None:
                    return {"ok": False, "error": "numerical_sandbox_unavailable"}
                return self._server(args)
            if tool == "create_handoff":
                self._keys(args, {"definition_path"}, {"notes"})
                return self.owner.create_handoff(**args)
            if tool in {"prepare", "run_lean", "score"}:
                return self._formal(tool, args)
            if tool == "report_issue":
                self._keys(args, {"kind", "detail"})
                if (args["kind"] not in ISSUE_KINDS or not isinstance(args["detail"], str)
                        or not args["detail"].strip() or len(args["detail"].encode()) > 32768):
                    raise ValueError("A specific bounded obstacle is required")
                result = {"ok": True, "action": "feedback", "issue_kind": args["kind"],
                    "detail": args["detail"], "proof_verified": False,
                    "handoff_id": self._handoff.index,
                    "definition_sha256": self._handoff.definition_sha256}
                _new(self._invocation("report-issue") / "issue.json", _json(result))
                return result
            if tool == "finish":
                self._keys(args, set(), {"reason", "summary"})
                if {"reason", "summary"} <= set(args):
                    raise ValueError("Use reason or summary, not both")
                summary = args.get("reason", args.get("summary", ""))
                if not isinstance(summary, str) or len(summary.encode()) > 32768:
                    raise ValueError("Finish reason must be bounded text")
                if self.role == "formalizer" and self.last_verified is None:
                    return {"ok": False, "error": "formalist_cannot_finish_unverified",
                        "detail": "Continue implementing/proving, or report_issue with a specific obstacle; use kind=unresolved when no proof has been obtained."}
                return {"ok": True, "action": "finish", "finished": True,
                    "summary": summary, "proof_verified": self.last_verified is not None}
        except callbacks.RepresentationError as error:
            return {"ok": False, "error": "representation_error", "proof_verified": False,
                "technical_diagnostic": _diagnostic(error, kind="representation_error")}
        except (ValueError, TypeError, KeyError, OSError, UnicodeError, ArithmeticError,
                SyntaxError, RecursionError) as error:
            return {"ok": False, "error": type(error).__name__, "detail": str(error)[:2000],
                "proof_verified": False}
        except Exception as error:
            return {"ok": False, "error": "host_exception", "proof_verified": False,
                "recoverable": True, "technical_diagnostic": _diagnostic(error)}


__all__ = ["DefinitionTools", "DefinitionRoleBroker", "is_verified",
           "CONSTRUCTOR_ACTIONS", "FORMALIZER_ACTIONS"]
