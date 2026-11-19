"""Same-trial constructor/formalizer capabilities and immutable proof handoffs.

Candidate text is data. Only the frozen physics-first strict verifier can
authorize an energy evaluation. Formalizers have neither Python execution nor
source editing capabilities, and every check is bound to the current handoff.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import traceback
from uuid import uuid4

from revisions.hybrid_agent_v1.local_numeric import LocalNumericalBroker, _regular_bytes
from revisions.hybrid_web_v1.agent import ToolBroker as _FileBroker
from revisions.dual_agent_v1_1 import callbacks as _callbacks
from revisions.physics_first_v1_1.verifier import _old

PROJECT = Path(__file__).resolve().parents[2]
MAX_FILE_BYTES = 2 * 1024**2
SOURCE_LIMIT = 65536
ISSUE_KINDS = frozenset({"math_error", "missing_argument", "representation_error", "infrastructure"})
CONSTRUCTOR_ACTIONS = ("list_files", "read", "write", "server", "create_handoff", "finish")
FORMALIZER_ACTIONS = ("list_files", "read", "write", "prepare", "run_lean", "score", "report_issue", "finish")
ATTEMPT_SCHEMA = "dual-agent-handoff-attempt/v1"
_ATTEMPT_NAME = re.compile(r"handoff-([0-9]{4,})\Z")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode()


def _inside(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(PROJECT) or path.resolve() != path:
        raise ValueError("Tool paths must remain inside this project without aliases")
    return path


def _directory(path: Path, *, create: bool = False) -> int:
    """Open/create a directory without following a link in any component."""
    path = _inside(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir(path: Path) -> None:
    os.close(_directory(path, create=True))


def _relative(name) -> PurePosixPath:
    # Reuse the reviewed broker's restrictions and additionally reject aliases
    # such as a//b, whose spelling PurePosixPath otherwise silently normalizes.
    result = _FileBroker._relative(name)
    if result.as_posix() != name:
        raise ValueError("Workspace paths must use their canonical spelling")
    return result


def _new(path: Path, contents: bytes) -> None:
    _inside(path)
    with _old.new_output(path) as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o400)


def _diagnostic(error: Exception, *, kind="host_exception") -> dict:
    """Keep type and stack, never exception text, source lines or local values.

    Host exceptions can include API credentials or source contents in their
    messages. RepresentationError is the compiler's explicit bounded-input
    rejection, and its detail remains useful to the formalizer.
    """
    frames = traceback.extract_tb(error.__traceback__)
    return {"kind": kind, "exception_type": type(error).__name__,
            "detail": (str(error)[:2000] if kind == "representation_error" else
                       "Host operation failed; the diagnostic preserves its type and stack without exception text."),
            "traceback": [{"file": frame.filename, "line": frame.lineno,
                           "function": frame.name} for frame in frames[-32:]]}


def _replace(path: Path, contents: bytes) -> None:
    """Replace one private file, refusing links and nonregular existing leaves."""
    descriptor = _directory(path.parent, create=True)
    temporary = ".write-" + uuid4().hex
    try:
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Workspace writes require a single-link regular file")
        outgoing = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                           | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=descriptor)
        with os.fdopen(outgoing, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


@dataclass(frozen=True)
class _Handoff:
    index: int
    source_path: Path
    proof_path: Path
    source_sha256: str
    proof_sha256: str
    manifest_path: Path


class DualAgentTools:
    """Capability owner for one task and one trial, with no other-trial mounts.

    The orchestrator passes ``constructor.dispatch`` and then the current
    ``formalizer.dispatch`` to separate role sessions. Each handoff starts a
    fresh formalizer workspace. ``handoff``/``handoffs`` are public records;
    immutable internal records, rather than mutable returned dicts, authorize IO.
    """

    def __init__(self, task: dict, trial_dir: Path, *, constructor_references=None,
                 formalizer_references=None, server=None, timeout: int = 300):
        self.task = json.loads(json.dumps(task, allow_nan=False))
        self.trial_dir = _inside(trial_dir)
        _mkdir(self.trial_dir)
        self.prepare_callback, self.lean_callback, self.score_callback = _callbacks.callbacks(
            self.task, timeout=timeout)
        self.server = LocalNumericalBroker() if server is None else server
        self.formalizer_references = dict(formalizer_references or {})
        self._handoffs: list[_Handoff] = []
        self.handoffs: list[dict] = []
        self.handoff_attempts: list[dict] = []
        self.handoff: dict | None = None
        self.formalizer: RoleToolBroker | None = None
        self.last_verified: dict | None = None
        self.constructor = RoleToolBroker(self, "constructor", self.trial_dir / "constructor",
                                          references=constructor_references)
        with self._handoff_lock():
            self._recover_locked()

    @contextmanager
    def _handoff_lock(self):
        """Serialize allocation, recovery and commit across broker processes.

        This is a local POSIX-file lock, not a lease or a budget checkpoint.
        Role sessions and their cumulative budgets remain the driver's concern.
        """
        parent = self.trial_dir / "handoffs"
        descriptor = _directory(parent, create=True)
        lock = None
        try:
            lock = os.open(".handoff.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                           | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=descriptor)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Handoff lock must be a single-link regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield parent
        finally:
            if lock is not None:
                os.close(lock)
            os.close(descriptor)

    def _failed_attempt(self, root: Path, index: int, diagnostic: dict) -> dict:
        failure = {"schema": ATTEMPT_SCHEMA, "handoff_id": index,
                   "status": "failed", "finished_utc": datetime.now(timezone.utc).isoformat(),
                   "proof_verified": False, "technical_diagnostic": diagnostic,
                   "retryable": True}
        # Even a full disk or a fault while writing this evidence cannot release
        # the already reserved directory/number. Recovery never reuses it.
        try:
            _new(root / "failed.json", _json(failure))
            failure["diagnostic_path"] = str(root / "failed.json")
        except Exception as evidence_error:
            failure["evidence_write_error"] = _diagnostic(evidence_error)
        return failure

    def _read_record(self, path: Path) -> dict:
        value = json.loads(_regular_bytes(path, MAX_FILE_BYTES))
        if not isinstance(value, dict):
            raise ValueError("Handoff record must be a JSON object")
        return value

    def _load_committed(self, root: Path, index: int, commit: dict) -> tuple[_Handoff, dict]:
        manifest_path = root / "manifest.json"
        manifest_bytes = _regular_bytes(manifest_path, MAX_FILE_BYTES)
        manifest = json.loads(manifest_bytes)
        source_path, proof_path = root / "source.py", root / "proof.md"
        if (commit.get("schema") != ATTEMPT_SCHEMA or commit.get("status") != "committed"
                or commit.get("handoff_id") != index
                or commit.get("manifest_sha256") != _sha(manifest_bytes)
                or not isinstance(manifest, dict)
                or manifest.get("schema") != "dual-agent-handoff/v1"
                or manifest.get("status") != "committed" or manifest.get("handoff_id") != index
                or manifest.get("task_sha256") != _sha(_json(self.task))
                or manifest.get("source_path") != str(source_path)
                or manifest.get("proof_path") != str(proof_path)):
            raise ValueError("Committed handoff manifest binding changed")
        source = _regular_bytes(source_path, SOURCE_LIMIT)
        proof = _regular_bytes(proof_path, MAX_FILE_BYTES)
        if (_sha(source) != manifest.get("source_sha256")
                or _sha(proof) != manifest.get("proof_sha256")):
            raise ValueError("Frozen handoff source or mathematical proof changed")
        frozen = _Handoff(index, source_path, proof_path, _sha(source), _sha(proof), manifest_path)
        return frozen, {**manifest, "manifest_path": str(manifest_path)}

    def _formalizer_for(self, frozen: _Handoff) -> RoleToolBroker:
        references = {**self.formalizer_references,
                      "handoff/source.py": _regular_bytes(frozen.source_path, SOURCE_LIMIT).decode(),
                      "handoff/proof.md": _regular_bytes(frozen.proof_path, MAX_FILE_BYTES).decode(),
                      "handoff/manifest.json": _regular_bytes(frozen.manifest_path, MAX_FILE_BYTES).decode()}
        return RoleToolBroker(self, "formalizer",
            self.trial_dir / "formalizer" / f"handoff-{frozen.index:04d}",
            references=references, handoff=frozen)

    def _recover_locked(self) -> int:
        """Read only single-link records; incomplete attempts become failed.

        The highest *reserved* ID determines the next ID, while only a complete
        committed marker can change the current handoff. Prior source/proof
        records are never deleted, repaired or overwritten.
        """
        parent = self.trial_dir / "handoffs"
        descriptor = _directory(parent)
        entries = []
        try:
            with os.scandir(descriptor) as incoming:
                for entry in incoming:
                    match = _ATTEMPT_NAME.fullmatch(entry.name)
                    if match:
                        entries.append((int(match[1]), entry.name,
                                        stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)))
        finally:
            os.close(descriptor)
        attempts, committed, records = [], [], []
        for index, name, safe_directory in sorted(entries):
            root = parent / name
            if not safe_directory:
                attempts.append({"handoff_id": index, "status": "failed",
                                 "technical_diagnostic": {"kind": "unsafe_attempt_entry"}})
                continue
            # A failed marker always wins, including a partially written one.
            try:
                failed = self._read_record(root / "failed.json")
            except FileNotFoundError:
                pass
            except json.JSONDecodeError:
                attempts.append({"handoff_id": index, "status": "failed",
                                 "technical_diagnostic": {"kind": "unreadable_failure_record"}})
                continue
            else:
                attempts.append({**failed, "handoff_id": index, "status": "failed"})
                continue
            try:
                commit = self._read_record(root / "committed.json")
            except FileNotFoundError:
                commit = None
            except json.JSONDecodeError:
                commit = None
            if commit is not None:
                frozen, record = self._load_committed(root, index, commit)
                committed.append(frozen)
                records.append(record)
                attempts.append({**commit, "manifest_path": str(frozen.manifest_path)})
            else:
                attempts.append(self._failed_attempt(root, index, {
                    "kind": "interrupted_attempt", "exception_type": None,
                    "detail": "Reserved attempt has no complete commit marker; preserved for audit.",
                    "traceback": []}))
        current = committed[-1] if committed else None
        if current is not None and (self.formalizer is None or self.formalizer._handoff != current):
            formalizer = self._formalizer_for(current)
        else:
            formalizer = self.formalizer if current is not None else None
        self._handoffs, self.handoffs, self.handoff_attempts = committed, records, attempts
        self.handoff = dict(records[-1]) if records else None
        self.formalizer = formalizer
        return max((index for index, _, _ in entries), default=0)

    def create_handoff(self, source_path: str, proof_path: str) -> dict:
        source_name, proof_name = _relative(source_path), _relative(proof_path)
        if source_name.suffix != ".py" or proof_name.suffix != ".md":
            raise ValueError("A handoff requires a Python .py source and a separate .md proof")
        source = self.constructor._read_private(source_path, SOURCE_LIMIT)
        proof = self.constructor._read_private(proof_path, MAX_FILE_BYTES)
        source_text, proof_text = source.decode(), proof.decode()
        if not source_text.strip() or not proof_text.strip():
            raise ValueError("The exact source and its complete mathematical proof must be nonempty")
        with self._handoff_lock() as parent:
            index = self._recover_locked() + 1
            root = parent / f"handoff-{index:04d}"
            try:
                descriptor = _directory(parent)
                try:
                    os.mkdir(root.name, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return self._commit_handoff(root, index, source_path, proof_path,
                                            source, proof)
            except Exception as error:
                failure = self._failed_attempt(root, index, _diagnostic(error))
                self.handoff_attempts.append(failure)
                return {"ok": False, "error": "host_exception", "recoverable": True,
                        "retryable": True, **failure}

    def _commit_handoff(self, root: Path, index: int, original_source: str,
                        original_proof: str, source: bytes, proof: bytes) -> dict:
        preparing = {"schema": ATTEMPT_SCHEMA, "status": "preparing", "handoff_id": index,
                     "created_utc": datetime.now(timezone.utc).isoformat(),
                     "task_sha256": _sha(_json(self.task)), "source_sha256": _sha(source),
                     "proof_sha256": _sha(proof), "proof_verified": False}
        _new(root / "preparing.json", _json(preparing))
        source_snapshot, proof_snapshot = root / "source.py", root / "proof.md"
        _new(source_snapshot, source)
        _new(proof_snapshot, proof)
        technical = None
        try:
            self.prepare_callback(source_path=source_snapshot, invocation_dir=root)
        except _callbacks.RepresentationError as error:
            technical = {**_diagnostic(error, kind="representation_error"),
                         "policy": "Preserve the exact source; formalizer must report, not repair f."}
        manifest = {
            "schema": "dual-agent-handoff/v1", "handoff_id": index, "status": "committed",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "task_sha256": _sha(_json(self.task)), "source_path": str(source_snapshot),
            "proof_path": str(proof_snapshot), "source_sha256": _sha(source),
            "proof_sha256": _sha(proof), "original_source_path": original_source,
            "original_proof_path": original_proof, "proof_verified": False,
            "technical_diagnostic": technical,
            "source_policy": "Exact f and proof frozen; only constructor may propose a new handoff.",
            "origin": "Only this trial's constructor; no previous trials or peer answers.",
        }
        manifest_path = root / "manifest.json"
        _new(manifest_path, _json(manifest))
        frozen = _Handoff(index, source_snapshot, proof_snapshot, _sha(source), _sha(proof), manifest_path)
        formalizer = self._formalizer_for(frozen)
        # Recheck after the compiler and broker setup, before publishing success.
        if (_regular_bytes(source_snapshot, SOURCE_LIMIT) != source
                or _regular_bytes(proof_snapshot, MAX_FILE_BYTES) != proof):
            raise ValueError("Frozen handoff changed during preparation")
        commit = {"schema": ATTEMPT_SCHEMA, "status": "committed", "handoff_id": index,
                  "finished_utc": datetime.now(timezone.utc).isoformat(),
                  "manifest_sha256": _sha(_json(manifest)), "proof_verified": False}
        _new(root / "committed.json", _json(commit))
        self._handoffs.append(frozen)
        self.handoff = {**manifest, "manifest_path": str(manifest_path)}
        self.handoffs.append(dict(self.handoff))
        self.handoff_attempts.append(commit)
        self.formalizer = formalizer
        return {"ok": True, "action": "handoff", **self.handoff,
                "source_reference": "reference/handoff/source.py",
                "proof_reference": "reference/handoff/proof.md"}

    def _assert_frozen(self, handoff: _Handoff) -> tuple[bytes, bytes]:
        with self._handoff_lock():
            self._recover_locked()
        if not self._handoffs or self._handoffs[-1] != handoff:
            raise ValueError("Formalizer belongs to a stale handoff")
        source = _regular_bytes(handoff.source_path, SOURCE_LIMIT)
        proof = _regular_bytes(handoff.proof_path, MAX_FILE_BYTES)
        if _sha(source) != handoff.source_sha256 or _sha(proof) != handoff.proof_sha256:
            raise ValueError("Frozen handoff source or mathematical proof changed")
        return source, proof


class RoleToolBroker:
    """Private files and an explicit in-memory reference map for one role."""

    def __init__(self, owner: DualAgentTools, role: str, run_dir: Path, *, references=None,
                 handoff: _Handoff | None = None):
        if role not in {"constructor", "formalizer"}:
            raise ValueError("Unknown dual-agent role")
        self.owner, self.role, self._handoff = owner, role, handoff
        self.run_dir = _inside(run_dir)
        self.workspace, self.invocations = self.run_dir / "workspace", self.run_dir / "invocations"
        _mkdir(self.workspace)
        _mkdir(self.invocations)
        self.references, self.generated = {}, {}
        for name, content in (references or {}).items():
            key = _relative(name)
            if (key.parts[0] in {"reference", "generated"} or not isinstance(content, str)
                    or len(content.encode()) > MAX_FILE_BYTES):
                raise ValueError("References must be bounded, explicitly named public text")
            self.references[key.as_posix()] = content
        self.serial = 0
        self.last_verified = None

    _keys = staticmethod(_FileBroker._keys)

    def _path(self, name: str) -> Path:
        relative = _relative(name)
        if relative.parts[0] in {"reference", "generated"}:
            raise ValueError("Public references and generated modules are read-only")
        return self.workspace.joinpath(*relative.parts)

    def _read_private(self, name: str, limit=MAX_FILE_BYTES) -> bytes:
        return _regular_bytes(self._path(name), limit)

    def _read(self, args: dict) -> dict:
        self._keys(args, {"path"}, {"start_line", "max_lines"})
        name = _relative(args["path"])
        if name.parts[0] == "reference":
            content = self.references[PurePosixPath(*name.parts[1:]).as_posix()]
        elif name.parts[0] == "generated":
            content = self.generated[name.as_posix()]
        else:
            content = self._read_private(args["path"]).decode()
        start, count = args.get("start_line", 1), args.get("max_lines", 240)
        if type(start) is not int or start < 1 or type(count) is not int or not 1 <= count <= 1000:
            raise ValueError("read requires start_line>=1 and max_lines in 1..1000")
        lines = content.splitlines(keepends=True)
        return {"ok": True, "content": "".join(lines[start-1:start-1+count]),
                "sha256": _sha(content.encode()), "start_line": start, "total_lines": len(lines),
                "next_line": start + count if start - 1 + count < len(lines) else None}

    def _write(self, args: dict) -> dict:
        self._keys(args, {"path", "content"}, {"append"})
        path = self._path(args["path"])
        if self.role == "formalizer" and path.suffix not in {".lean", ".md"}:
            raise ValueError("Formalizer may write only Lean proofs and textual issue reports")
        content, append = args["content"], args.get("append", False)
        if not isinstance(content, str) or type(append) is not bool:
            raise ValueError("write requires text content and a boolean append flag")
        data = content.encode()
        if append:
            try:
                data = self._read_private(args["path"]) + data
            except FileNotFoundError:
                pass
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Workspace file exceeds its byte limit")
        _replace(path, data)
        return {"ok": True, "bytes": len(data), "sha256": _sha(data)}

    def _list(self) -> dict:
        files = []
        entries_seen = 0
        def visit(descriptor: int, prefix: PurePosixPath, depth=0):
            nonlocal entries_seen
            if depth > 16:
                raise ValueError("Workspace directory depth limit exceeded")
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > 4096:
                        raise ValueError("Workspace entry limit exceeded")
                    relative = prefix / entry.name
                    _relative(relative.as_posix())
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                        | os.O_CLOEXEC, dir_fd=descriptor)
                        try:
                            visit(child, relative, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        files.append(relative.as_posix())
                    else:
                        raise ValueError("Workspace contains a link or nonregular entry")
        descriptor = _directory(self.workspace)
        try:
            visit(descriptor, PurePosixPath())
        finally:
            os.close(descriptor)
        return {"ok": True, "files": sorted(files),
                "references": ["reference/" + name for name in sorted(self.references)],
                "generated": sorted(self.generated)}

    def _invocation(self, tool: str) -> Path:
        descriptor = _directory(self.invocations)
        try:
            while True:
                self.serial += 1
                path = self.invocations / f"{self.serial:04d}-{tool}"
                try:
                    os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    continue
                return path
        finally:
            os.close(descriptor)

    def _source_argument(self, args: dict, expected: bytes) -> None:
        source = args.get("source_path", args.get("source", "reference/handoff/source.py"))
        if source == "reference/handoff/source.py":
            actual = self.references["handoff/source.py"].encode()
        else:
            if _relative(source).suffix != ".py":
                raise ValueError("Formal checks require the frozen Python source")
            actual = self._read_private(source, SOURCE_LIMIT)
        if actual != expected:
            raise ValueError("Formalizer source must exactly match the current frozen f")

    def _callback(self, callback, invocation: Path, **kwargs) -> dict:
        try:
            return callback(invocation_dir=invocation, **kwargs)
        except _callbacks.RepresentationError:
            raise
        except Exception as error:
            diagnostic = _diagnostic(error)
            record = {"ok": False, "error": "host_exception", "proof_verified": False,
                      "verified": False, "retryable": True, "recoverable": True,
                      "technical_diagnostic": diagnostic}
            try:
                _new(invocation / "host_failure.json", _json(record))
                record["diagnostic_path"] = str(invocation / "host_failure.json")
            except Exception as evidence_error:
                record["evidence_write_error"] = _diagnostic(evidence_error)
            return record

    def _formal(self, tool: str, args: dict) -> dict:
        proof_fields = {"proof", "proof_path"} & set(args)
        allowed = {"source", "source_path"} | ({"proof", "proof_path"} if tool != "prepare" else set())
        if set(args) - allowed or {"source", "source_path"} <= set(args):
            raise ValueError("Invalid or duplicate formal-check arguments")
        if tool != "prepare" and len(proof_fields) != 1:
            raise ValueError("run_lean/score requires one Lean proof_path")
        source, mathematical_proof = self.owner._assert_frozen(self._handoff)
        self._source_argument(args, source)
        invocation = self._invocation(tool)
        source_path = invocation / "Source.py"
        _new(source_path, source)
        _new(invocation / "proof.md", mathematical_proof)
        binding = {"handoff_id": self._handoff.index, "source_sha256": _sha(source),
                   "mathematical_proof_sha256": _sha(mathematical_proof)}
        if tool == "prepare":
            result = self._callback(self.owner.prepare_callback, invocation, source_path=source_path)
            if result.get("ok") is True:
                paths = {}
                for field, filename in (("generated_state", "GeneratedState.lean"),
                                        ("proof_template", "Candidate.lean.txt")):
                    text = result.get(field)
                    if not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES:
                        raise ValueError("Invalid bounded host-generated Lean reference")
                    name = f"generated/{binding['source_sha256']}/{filename}"
                    self.generated[name] = text
                    _new(invocation / filename, text.encode())
                    paths[field] = name
                result = {key: value for key, value in result.items()
                          if key not in {"generated_state", "proof_template"}}
                result["generated_paths"] = paths
        else:
            formalization_map = self._read_private("formalization_map.md")
            if not formalization_map.decode().strip():
                raise ValueError("Write a nonempty formalization_map.md before checking the translation")
            _new(invocation / "formalization_map.md", formalization_map)
            binding["formalization_map_sha256"] = _sha(formalization_map)
            binding["formalization_map_path"] = str(invocation / "formalization_map.md")
            proof_name = args[next(iter(proof_fields))]
            if _relative(proof_name).suffix != ".lean":
                raise ValueError("Formalizer must submit a .lean proof file")
            proof = self._read_private(proof_name)
            proof_path = invocation / "Candidate.lean"
            _new(proof_path, proof)
            binding["proof_sha256"] = _sha(proof)
            # Both actions use the unchanged strict score callback: it runs the
            # fixed-goal checker before it evaluates any Hamiltonian energy.
            result = self._callback(self.owner.score_callback, invocation,
                                    source_path=source_path, proof_path=proof_path)
            certificate = result.get("certificate") or {}
            accepted = (all(result.get(key) is True for key in (
                "ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified"))
                and certificate.get("kind") == "hybrid-fixed-goal-kernel-v1"
                and certificate.get("theorem") == "Submission.accepted"
                and certificate.get("target") == "GeneratedState.ProofObligation"
                and certificate.get("source_sha256") == binding["source_sha256"]
                and certificate.get("candidate_source_sha256") == binding["proof_sha256"])
            if not accepted:
                result = {key: value for key, value in result.items()
                          if key not in {"energy_density", "energy_report", "terms", "baseline_energy",
                                         "numerical_normalized_gain", "energy_normalization"}}
                result.update(ok=False, verified=False, proof_verified=False)
            else:
                energy = result.get("energy_density")
                if type(energy) not in (int, float) or not math.isfinite(energy):
                    raise ValueError("Strict verification returned no finite energy")
                result = {**result, **binding, "action": "feedback", "issue_kind": None}
        self.owner._assert_frozen(self._handoff)
        if _regular_bytes(source_path, SOURCE_LIMIT) != source:
            raise ValueError("Invocation source changed during verification")
        if tool != "prepare" and _regular_bytes(proof_path, MAX_FILE_BYTES) != proof:
            raise ValueError("Invocation Lean proof changed during verification")
        result = {**result, **binding}
        _new(invocation / "result.json", _json(result))
        if result.get("action") == "feedback" and result.get("proof_verified") is True:
            self.last_verified = dict(result)
            previous = self.owner.last_verified
            if previous is None or result["energy_density"] < previous["energy_density"]:
                self.owner.last_verified = dict(result)
        return result

    def _server(self, args: dict) -> dict:
        self._keys(args, {"candidate"}, {"cpus", "wall_seconds", "arguments"})
        cpus, wall, arguments = args.get("cpus", 1), args.get("wall_seconds", 120), args.get("arguments", [])
        if type(cpus) is not int or cpus != 1:
            raise ValueError("Each local numerical call uses exactly one CPU")
        if type(wall) not in (int, float) or not math.isfinite(wall) or not 1 <= wall <= 300:
            raise ValueError("Numerical wall_seconds must be in 1..300")
        if (not isinstance(arguments, list) or len(arguments) > 64
                or any(not isinstance(value, str) or len(value) > 4096 or "\0" in value
                       for value in arguments)):
            raise ValueError("Numerical arguments must be at most 64 bounded strings")
        if _relative(args["candidate"]).suffix != ".py":
            raise ValueError("Numerical experiments require a Python .py file")
        source = self._read_private(args["candidate"])
        invocation = self._invocation("server")
        candidate = invocation / "Experiment.py"
        _new(candidate, source)
        result = self.owner.server(candidate_path=candidate, invocation_dir=invocation,
            cpus=cpus, wall_seconds=wall, arguments=arguments)
        # Sandbox stdout is user code, never a host energy result or proof.
        result = {key: value for key, value in result.items()
                  if key not in {"energy_density", "energy_report", "terms", "baseline_energy",
                                 "numerical_normalized_gain", "energy_normalization"}}
        result.update(source_sha256=_sha(source), proof_verified=False, verified=False,
                      output_trusted=False)
        _new(invocation / "result.json", _json(result))
        return result

    def dispatch(self, action: dict, session=None) -> dict:
        del session  # Runtime owns API/CPU budgets; this broker owns capabilities.
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
                return self._server(args)
            if tool == "create_handoff":
                self._keys(args, {"source_path", "proof_path"})
                return self.owner.create_handoff(**args)
            if tool in {"prepare", "run_lean", "score"}:
                return self._formal(tool, args)
            if tool == "report_issue":
                self._keys(args, {"kind", "detail"})
                if args["kind"] not in ISSUE_KINDS or not isinstance(args["detail"], str):
                    raise ValueError("Issue requires a supported kind and text detail")
                if not args["detail"].strip() or len(args["detail"].encode()) > 32768:
                    raise ValueError("Issue detail must be nonempty and at most 32 KiB")
                result = {"ok": True, "action": "feedback", "issue_kind": args["kind"],
                          "detail": args["detail"], "proof_verified": False,
                          "handoff_id": self._handoff.index,
                          "source_sha256": self._handoff.source_sha256,
                          "mathematical_proof_sha256": self._handoff.proof_sha256}
                invocation = self._invocation("report-issue")
                _new(invocation / "issue.json", _json(result))
                return result
            if tool == "finish":
                self._keys(args, set(), {"summary", "reason", "kind"})
                if {"reason", "summary"} <= set(args):
                    raise ValueError("Finish accepts reason or summary, not both")
                summary = args.get("reason", args.get("summary", ""))
                if not isinstance(summary, str) or len(summary.encode()) > 32768:
                    raise ValueError("Finish summary must be text")
                if self.role == "formalizer" and self.last_verified is None:
                    return {"ok": False, "error": "formalizer_cannot_finish_unverified",
                            "detail": "Continue repairing the Lean translation or use report_issue with a concrete mathematical error, missing argument, representation error or infrastructure problem."}
                if "kind" in args:
                    raise ValueError("kind is only meaningful for an unverified formalizer finish")
                return {"ok": True, "action": "finish", "finished": True,
                        "summary": summary,
                        "proof_verified": self.last_verified is not None}
            raise ValueError("Unknown role action")
        except _callbacks.RepresentationError as error:
            return {"ok": False, "error": "representation_error", "proof_verified": False,
                    "technical_diagnostic": _diagnostic(error, kind="representation_error")}
        except (ValueError, TypeError, KeyError, OSError, UnicodeError, ArithmeticError,
                SyntaxError, RecursionError) as error:
            return {"ok": False, "error": type(error).__name__, "detail": str(error)[:2000]}
        except Exception as error:
            return {"ok": False, "error": "host_exception", "proof_verified": False,
                    "recoverable": True, "retryable": True,
                    "technical_diagnostic": _diagnostic(error)}


__all__ = ["DualAgentTools", "RoleToolBroker", "CONSTRUCTOR_ACTIONS", "FORMALIZER_ACTIONS", "ISSUE_KINDS"]
