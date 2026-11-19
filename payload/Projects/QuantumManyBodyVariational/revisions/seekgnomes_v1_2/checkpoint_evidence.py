"""Reload only host-owned handoff evidence after a controller interruption."""

from __future__ import annotations

import json
import os
import re

from revisions.seekgnomes_v1_1_1.scoring import verify_stored_result
from revisions.seekgnomes_v1_1_1.tools import (
    ISSUE_KINDS, MAX_FILE_BYTES, _directory, _regular_bytes, _replace, _sha,
)


_INVOCATION = re.compile(r"([0-9]{4,})-(prepare|run_lean|score|report-issue)\Z")
_HANDOFF = re.compile(r"handoff-([0-9]{4,})\Z")
_RETURN_FILES = frozenset({
    "Candidate.lean", "GeneratedState.lean", "f.state", "f.py", "formalization_map.md",
})


def _directories(root, pattern):
    try:
        descriptor = _directory(root)
    except FileNotFoundError:
        return []
    try:
        with os.scandir(descriptor) as entries:
            found = []
            for entry in entries:
                if match := pattern.fullmatch(entry.name):
                    if not entry.is_dir(follow_symlinks=False):
                        raise ValueError("Checkpoint path must be a directory")
                    found.append((int(match[1]), root / entry.name, match.groups()[1:]))
            return sorted(found, reverse=True)
    finally:
        os.close(descriptor)


def _read_record(path, *, interrupted_write=False):
    try:
        value = json.loads(_regular_bytes(path, MAX_FILE_BYTES))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Historical invocation files are write-once. A controller killed in
        # the middle of a write can leave an incomplete receipt.
        if interrupted_write:
            return None
        raise
    if not isinstance(value, dict):
        raise ValueError("Expected a checkpoint record object")
    return value


def _read_bound(path, digest):
    contents = _regular_bytes(path, MAX_FILE_BYTES)
    if _sha(contents) != digest:
        raise ValueError(f"Checkpoint digest changed: {path.name}")
    return contents


def _binding_matches(record, handoff):
    return (record.get("handoff_id") == handoff.index
            and record.get("definition_sha256") == handoff.definition_sha256)


def _generated_digest(result):
    digest = result.get("generated_state_sha256")
    if digest is None and isinstance(result.get("certificate"), dict):
        digest = result["certificate"].get("generated_state_sha256")
    return digest


def _restore_invocation(trial, broker, invocation, result):
    state = trial.candidate_format == "state"
    source_name = "f.state" if state else "Source.py"
    source = _read_bound(invocation / source_name, result.get("source_sha256"))
    generated = _read_bound(invocation / "GeneratedState.lean", _generated_digest(result))
    if state and result["source_sha256"] != broker._handoff.definition_sha256:
        raise ValueError("Recovered source differs from the frozen handoff")
    if not state:
        _read_bound(invocation / "f.md", broker._handoff.definition_sha256)

    prefix = f"generated/{result['source_sha256']}/"
    broker.generated.setdefault(prefix + "GeneratedState.lean", generated.decode())
    # Templates are created by the prepare callback inside the host invocation
    # directory. They are editing aids and do not enter proof acceptance.
    template = _regular_bytes(invocation / "Candidate.lean.txt", MAX_FILE_BYTES)
    broker.generated.setdefault(prefix + "Candidate.lean.txt", template.decode())
    files = {"f.state" if state else "f.py": source, "GeneratedState.lean": generated}
    if result.get("proof_sha256") is not None:
        files["Candidate.lean"] = _read_bound(invocation / "Candidate.lean", result["proof_sha256"])
        if not state:
            files["formalization_map.md"] = _read_bound(invocation / "formalization_map.md",
                                                         result.get("formalization_map_sha256"))
    return files


def _restore_score(trial, broker, invocation, result):
    proof_manifest = invocation / "verification/manifest.json"
    source_name = "f.state" if trial.candidate_format == "state" else "Source.py"
    if (not trial.is_verified(result)
            or result.get("invocation_dir") != str(invocation)
            or result.get("source_path") != str(invocation / source_name)
            or result.get("proof_path") != str(invocation / "Candidate.lean")
            or result.get("energy_report") != str(invocation / "energy.json")
            or result.get("proof_manifest") != str(proof_manifest)):
        raise ValueError("Archived score differs from the fixed invocation")
    checked = {**result, "proof_manifest_relative": str(proof_manifest.relative_to(trial.out))}
    verify_stored_result(checked, trial.task, trial.out)
    broker.return_files = _restore_invocation(trial, broker, invocation, result)
    broker.last_verified = dict(result)
    previous = trial.tools.last_verified
    if previous is None or result["energy_density"] < previous["energy_density"]:
        trial.tools.last_verified = dict(result)
    return {**result, "session_status": "completed"}


def restore_formalist(trial, broker):
    """Restore the latest host snapshot and return a committed terminal result."""
    base = getattr(broker, "base", broker)
    handoff = base._handoff
    trial.tools._assert_frozen(handoff)
    completed_issue = None
    for serial, invocation, (tool,) in _directories(base.invocations, _INVOCATION):
        base.serial = max(base.serial, serial)
        if tool == "report-issue":
            issue = _read_record(invocation / "issue.json", interrupted_write=True)
            if issue is None:
                continue
            if (not _binding_matches(issue, handoff)
                    or issue.get("ok") is not True or issue.get("action") != "feedback"
                    or issue.get("proof_verified") is not False
                    or issue.get("issue_kind") not in ISSUE_KINDS
                    or not isinstance(issue.get("detail"), str) or not issue["detail"].strip()
                    or (trial.candidate_format == "state"
                        and issue["issue_kind"] in {"unresolved", "infrastructure"})):
                raise ValueError("Archived issue is not a completed handoff result")
            if completed_issue is None:
                completed_issue = {**issue, "session_status": "completed"}
            continue

        result = _read_record(invocation / "result.json", interrupted_write=True)
        if result is None:
            continue
        if not _binding_matches(result, handoff):
            raise ValueError("Archived invocation belongs to another handoff")
        if result.get("action") == "feedback":
            if tool != "score" and (trial.candidate_format == "state" or tool != "run_lean"):
                raise ValueError("Archived score came from an unexpected operation")
            return _restore_score(trial, base, invocation, result)
        # A failed proof can still carry the latest generated module and proof
        # snapshot, but a failure in prepare may not have written either file.
        if _generated_digest(result) is not None:
            prefix = f"generated/{result['source_sha256']}/"
            if prefix + "GeneratedState.lean" not in base.generated:
                files = _restore_invocation(trial, base, invocation, result)
                if not base.return_files:
                    base.return_files = files
    return completed_issue


def restore_constructor_returns(trial):
    """Restore only completed return views, including older handoffs."""
    handoffs = {entry["handoff_id"]: entry for entry in trial.tools.handoffs}
    generated = trial.tools.constructor_base.generated
    for index, directory, _ in _directories(trial.out / "returns", _HANDOFF):
        files = _read_record(directory / "index.json")
        if files is None:
            continue
        if index not in handoffs:
            raise ValueError("Return view belongs to an unknown handoff")
        for filename, entry in files.items():
            if filename not in _RETURN_FILES:
                raise ValueError("Unexpected file in handoff return view")
            path = f"generated/return-{index:04d}/{filename}"
            if not isinstance(entry, dict) or entry.get("path") != path:
                raise ValueError("Return view path differs from the handoff")
            contents = _read_bound(directory / filename, entry.get("sha256"))
            if filename == "f.state" and _sha(contents) != handoffs[index]["definition_sha256"]:
                raise ValueError("Return view differs from the frozen state")
            generated[path] = contents.decode()


def _write_snapshot(path, contents):
    try:
        previous = _regular_bytes(path, MAX_FILE_BYTES)
    except FileNotFoundError:
        _replace(path, contents)
    else:
        if previous != contents:
            raise ValueError(f"Committed handoff snapshot changed: {path.name}")


def return_view(trial):
    """Finish writing the return view without recreating an existing snapshot."""
    return trial.tools.return_view(write_snapshot=_write_snapshot)
