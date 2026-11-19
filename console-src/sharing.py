"""Build and send a private, evidence-bound SeekGnomes community submission."""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack
from datetime import datetime, timedelta
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urlsplit

SCHEMA = "seekgnomes-community-submission/v1"
STATE_PROTOCOL = "seekgnomes-harbor-state/v1"
ROUTE = "/v1/submissions"
CAMPAIGN_TERMINALS = {"finished", "finished_with_technical_gaps"}
TASK_TERMINALS = {"completed", "scientific_failure", "technical_gap"}
TASK_IDS = {f"{index:02d}" for index in range(1, 51)}
FILE_LIMITS = {"f.py": 65536, "f.state": 1024**2, "Candidate.lean": 2 * 1024**2}
MAX_SUBMISSION_BYTES = 50 * sum(4 * ((FILE_LIMITS[name] + 2) // 3)
                                for name in ("f.state", "Candidate.lean")) + 1024**2
METADATA_LIMIT = 16 * 1024**2
HARNESS_FIELDS = ("name", "company", "version", "api_protocol", "harbor_agent")
REQUIRED_LIMITS = ("cpus", "closing_output_tokens", "closing_allocations")
PARALLEL_LIMITS = ("parallel_tasks", "cpus_per_task")
OPTIONAL_LIMITS = ("constructor_output_tokens", "constructor_active_seconds", "closing_active_seconds",
                   "constructor_input_tokens", "api_requests_limit", "closing_input_tokens",
                   "model_context_tokens")
ROW_NUMBERS = ("energy_density", "censored_verified_energy", "pass_rate", "improvement_rate",
               "amplitude", "score")
GROUP_FIELDS = ("catalogue_tasks", "selected_tasks", "completed_tasks", "scored_tasks",
                "score", "selected_subset_score")
CERT_HASHES = ("lock_sha256", "source_sha256", "candidate_source_sha256",
               "generated_state_sha256", "candidate_export_sha256")
CERT_INTS = ("dimension", "qubits_per_cell", "sqrt_bits", "series_terms")
CERT_NAMES = {"kind": "hybrid-fixed-goal-kernel-v1", "theorem": "Submission.accepted",
              "target": "GeneratedState.ProofObligation"}
STATE_CERT_NAMES = {**CERT_NAMES, "kind": "state-format-fixed-goal-kernel-v1"}
STATE_CERT_HASHES = (*CERT_HASHES, "runtime_sha256")
STATE_CERT_INTS = (*CERT_INTS, "precision_bits")
FLAGS = ("ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified")
STAGES = ("lock-before", "compile-generated", "compile-challenge", "export-challenge",
          "compile-candidate", "export-candidate", "judge", "lock-after")
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"\w[\w.+@\[\]/-]{0,255}")


class SharingError(ValueError):
    """A sharing error suitable for display in the local console."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def decode_json(raw: bytes):
    def object_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError
            obj[key] = value
        return obj

    def reject_constant(_):
        raise ValueError

    return json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=reject_constant)


def _require(ok, message="Invalid community submission format."):
    if not ok:
        raise SharingError(message)


def _object(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields))


def _digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def _identifier(value):
    return (isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None
            and all(part not in ("", ".", "..") for part in value.split("/")))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def _groups(groups, rows):
    _object(groups, ("exactly_solvable", "generic"))
    for name, group in groups.items():
        _object(group, GROUP_FIELDS)
        selected = [row for row in rows if row["group"] == name]
        counts = [group[field] for field in GROUP_FIELDS[:4]]
        _require(all(type(count) is int and 0 <= count <= 50 for count in counts))
        catalogue, selected_count, complete, scored = counts
        _require(scored <= complete <= selected_count <= catalogue)
        _require(selected_count == len(selected))
        _require(complete == sum(row["scientific_complete"] for row in selected))
        _require(scored == sum(row["scientific_complete"] and row["score_complete"] for row in selected))
        for field in GROUP_FIELDS[4:]:
            value = group[field]
            _require(value is None or (_number(value) and 0 <= value <= 100))
    _require(sum(group["catalogue_tasks"] for group in groups.values()) == 50)


def _rows(rows, ids, campaign_status):
    _require(isinstance(rows, list) and len(rows) == len(ids))
    strict = {}
    for task_id, row in zip(ids, rows):
        _object(row, ("task_id", "group", "status", "censored", "scientific_complete",
                      "score_complete", *ROW_NUMBERS))
        _require(row["task_id"] == task_id and row["group"] in ("exactly_solvable", "generic"))
        _require(row["status"] in TASK_TERMINALS)
        _require(all(type(row[field]) is bool for field in ("censored", "scientific_complete", "score_complete")))
        _require(all(row[field] is None or _number(row[field]) for field in ROW_NUMBERS))
        for field in ("pass_rate", "improvement_rate", "amplitude", "score"):
            _require(row[field] is None or 0 <= row[field] <= (100 if field == "score" else 1))
        energy = row["energy_density"]
        censored_energy = row["censored_verified_energy"]
        _require(row["score_complete"] == (row["score"] is not None)
                 and (row["scientific_complete"] or not row["score_complete"]))
        if row["status"] == "completed":
            _require((energy is not None) == row["scientific_complete"]
                     and (censored_energy is not None) != row["scientific_complete"])
            strict[task_id] = energy if energy is not None else censored_energy
        else:
            _require(energy is None and censored_energy is None and row["censored"] is False)
        if row["status"] == "technical_gap":
            _require(row["scientific_complete"] is False and row["score_complete"] is False
                     and all(row[field] is None for field in ROW_NUMBERS))
    _require((campaign_status == "finished_with_technical_gaps")
             == any(row["status"] == "technical_gap" for row in rows))
    return strict


def _validate_payload(payload):
    _object(payload, ("run", "summary", "strict_results"))
    run = payload["run"]
    _object(run, ("protocol", "framework", "framework_version", "model", "harness", "limits",
                  "task_ids", "task_sha256", "proof_lock_sha256", "created_utc", "ended_utc",
                  "status", "web_enabled", "completion_token_field"))
    _require(all(_identifier(run[name]) for name in ("protocol", "framework", "framework_version",
                                                     "model", "completion_token_field")))
    _require(run["status"] in CAMPAIGN_TERMINALS and type(run["web_enabled"]) is bool)
    for field in ("created_utc", "ended_utc"):
        _require(isinstance(run[field], str) and len(run[field]) <= 35)
        _require(datetime.fromisoformat(run[field]).utcoffset() == timedelta(0))
    _require(datetime.fromisoformat(run["created_utc"]) <= datetime.fromisoformat(run["ended_utc"]))
    _object(run["harness"], HARNESS_FIELDS)
    _require(all(_identifier(value) for value in run["harness"].values()))
    limits = run["limits"]
    fields = (*REQUIRED_LIMITS, *OPTIONAL_LIMITS, "budget_expansion", "budget_mode")
    layout = isinstance(limits, dict) and any(name in limits for name in PARALLEL_LIMITS)
    _object(limits, (*fields, *PARALLEL_LIMITS) if layout else fields)
    _require(all(_positive(limits[name]) for name in REQUIRED_LIMITS))
    _require(all(limits[name] is None or _positive(limits[name]) for name in OPTIONAL_LIMITS))
    _require(type(limits["budget_expansion"]) is bool)
    output_cap, time_cap = limits["constructor_output_tokens"], limits["constructor_active_seconds"]
    _require(limits["budget_mode"] in ("combined", "tokens", "time")
             and (output_cap is not None) == (limits["budget_mode"] in ("combined", "tokens"))
             and (time_cap is not None) == (limits["budget_mode"] in ("combined", "time"))
             and limits["closing_active_seconds"] == time_cap)
    ids = run["task_ids"]
    _require(isinstance(ids, list) and 0 < len(ids) <= 50
             and all(isinstance(value, str) and value in TASK_IDS for value in ids)
             and len(set(ids)) == len(ids))
    if layout:
        workers, cores = (limits[name] for name in PARALLEL_LIMITS)
        _require(_positive(workers) and _positive(cores) and 2 <= workers <= min(len(ids), limits["cpus"])
                 and cores == limits["cpus"] // workers)
    _object(run["task_sha256"], ids)
    _require(all(_digest(value) for value in run["task_sha256"].values()) and _digest(run["proof_lock_sha256"]))

    summary = payload["summary"]
    _object(summary, ("schema", "rules_sha256", "task_groups_sha256", "rows", "groups"))
    _require(_identifier(summary["schema"]))
    _require(_digest(summary["rules_sha256"]) and _digest(summary["task_groups_sha256"]))
    strict = _rows(summary["rows"], ids, run["status"])
    _groups(summary["groups"], summary["rows"])
    results = payload["strict_results"]
    state = run["protocol"] == STATE_PROTOCOL
    cert_names = STATE_CERT_NAMES if state else CERT_NAMES
    cert_hashes = STATE_CERT_HASHES if state else CERT_HASHES
    cert_ints = STATE_CERT_INTS if state else CERT_INTS
    source_name = "f.state" if state else "f.py"
    _require(isinstance(results, list) and len(results) == len(strict))
    for (task_id, energy), result in zip(strict.items(), results):
        _object(result, ("task_id", "energy_density", "checker_manifest_sha256", "certificate", "files"))
        _require(result["task_id"] == task_id and _number(result["energy_density"])
                 and result["energy_density"] == energy and _digest(result["checker_manifest_sha256"]))
        cert = result["certificate"]
        _object(cert, (*cert_names, *cert_hashes, *cert_ints))
        _require(all(cert[key] == value for key, value in cert_names.items()))
        _require(all(_digest(cert[key]) for key in cert_hashes)
                 and all(_positive(cert[key]) for key in cert_ints)
                 and cert["lock_sha256"] == run["proof_lock_sha256"])
        _object(result["files"], (source_name, "Candidate.lean"))
        for name, cert_field in ((source_name, "source_sha256"), ("Candidate.lean", "candidate_source_sha256")):
            contents = result["files"][name]
            _require(isinstance(contents, str) and len(contents) <= 4 * ((FILE_LIMITS[name] + 2) // 3))
            raw = base64.b64decode(contents, validate=True)
            _require(0 < len(raw) <= FILE_LIMITS[name] and raw.decode("utf-8")
                     and base64.b64encode(raw).decode("ascii") == contents and sha256(raw) == cert[cert_field])


def validated_submission_id(envelope: dict) -> str:
    """Check both the bounded wire format and the content-addressed submission ID."""
    try:
        _object(envelope, ("schema", "sha256", "payload"))
        _require(envelope["schema"] == SCHEMA and _digest(envelope["sha256"]))
        _validate_payload(envelope["payload"])
        expected = sha256(canonical_json(envelope["payload"]))
        _require(hmac.compare_digest(expected, envelope["sha256"]), "Community submission digest mismatch.")
        return expected
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError, binascii.Error) as error:
        if isinstance(error, SharingError):
            raise
        raise SharingError("Invalid community submission format.") from None


def _path(value):
    _require(isinstance(value, str) and 0 < len(value) < 2048)
    path = PurePosixPath(value)
    _require(not path.is_absolute() and ".." not in path.parts and path.as_posix() == value
             and value != "." and "\\" not in value)
    return path


class _RunFiles:
    def __init__(self, root_fd):
        self.root_fd = root_fd

    def read(self, path, limit=METADATA_LIMIT):
        path = _path(str(path))
        with ExitStack() as descriptors:
            current = self.root_fd
            for part in path.parts[:-1]:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                descriptors.callback(os.close, current)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= limit)
                data = stream.read(limit + 1)
                _require(0 < len(data) <= limit)
                return data

    def json(self, path):
        value = decode_json(self.read(path))
        _require(isinstance(value, dict))
        return value


def _strict_result(files, run, task_id, entry, row, energy, private_literals):
    state = run["protocol"] == STATE_PROTOCOL
    trial = _path(entry["selected_trial"])
    prefix = f"shards/{task_id}/" if run["limits"].get("parallel_tasks", 1) > 1 else ""
    _require(re.fullmatch(rf"{prefix}tasks/{task_id}/attempt-[1-9][0-9]*", str(trial)) is not None)
    manifest = files.json(trial / "manifest.json")
    task = files.json(trial / "task.json")
    task_digest = sha256(json.dumps(task, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
    _require(manifest["model"] == run["model"] and manifest["task_id"] == task_id
             and manifest["protocol"] == run["protocol"] and manifest["budget_multiplier"] == 1
             and task["id"] == task_id and manifest["task_sha256"] == task_digest == run["task_sha256"][task_id])
    receipt = manifest["best_verified_result"]
    _require(all(receipt.get(flag) is True for flag in FLAGS))
    _require(_number(receipt["energy_density"]) and math.isclose(receipt["energy_density"], energy,
                                                               rel_tol=1e-12, abs_tol=1e-12))
    certificate = receipt["certificate"]
    _require(certificate["dimension"] == task["dimension"] and certificate["qubits_per_cell"] == task["qubits_per_cell"]
             and certificate["lock_sha256"] == run["proof_lock_sha256"]
             and certificate["source_sha256"] == receipt["source_sha256"]
             and certificate["candidate_source_sha256"] == receipt["proof_sha256"])
    checker = trial / _path(receipt["proof_manifest_relative"])
    _require(checker.name == "manifest.json" and checker != trial / "manifest.json"
             and row["proof_manifest_relative"] == receipt["proof_manifest_relative"])
    checker_bytes = files.read(checker)
    report = decode_json(checker_bytes)
    _require(report["passed"] is True and report["certificate"] == certificate
             and all(report.get(flag) is True for flag in ("compile_passed", "goal_matched", "kernel_verified"))
             and report["candidate_definition_holes"] == certificate["candidate_definition_holes"] == []
             and set(certificate["permitted_axioms"]) == {"propext", "Quot.sound", "Classical.choice"}
             and all(report["checks"][stage]["passed"] is True for stage in STAGES))
    if state:
        invocation = checker.parent.parent
        _require(receipt["energy_report_relative"] == str((invocation / "energy.json").relative_to(trial)))
        _require((trial / _path(receipt["source_path_relative"])) == invocation / "f.state"
                 and (trial / _path(receipt["proof_path_relative"])) == invocation / "Candidate.lean")
        energy_bytes = files.read(invocation / "energy.json")
        energy_report = decode_json(energy_bytes)
        _require(sha256(energy_bytes) == receipt["energy_report_sha256"]
                 and receipt["energy_density"] == energy_report["energy_density"]
                 and all(receipt[name] == report[name] == certificate[name]
                         for name in ("runtime_sha256", "generated_state_sha256", "lock_sha256"))
                 and all(energy_report["verification"][name] == certificate[name]
                         for name in ("runtime_sha256", "generated_state_sha256", "source_sha256",
                                      "lock_sha256", "candidate_source_sha256")))
    source = {}
    for name, receipt_path, checker_filename, digest_key in (
        (("f.state" if state else "f.py"), "source_path_relative",
         ("source.state" if state else "source.py"), "source_sha256"),
        ("Candidate.lean", "proof_path_relative", "Submitted.lean", "candidate_source_sha256"),
    ):
        data = files.read(trial / _path(receipt[receipt_path]), FILE_LIMITS[name])
        trusted = files.read(checker.parent / "trusted" / checker_filename, FILE_LIMITS[name])
        _require(data == trusted and sha256(data) == certificate[digest_key] == report[digest_key])
        _require(all(literal not in data for literal in private_literals),
                 "A private value was found in a selected artifact. Nothing was sent.")
        source[name] = base64.b64encode(data).decode("ascii")
    return {"task_id": task_id, "energy_density": energy, "checker_manifest_sha256": sha256(checker_bytes),
            "certificate": {name: certificate[name] for name in (
                (*STATE_CERT_NAMES, *STATE_CERT_HASHES, *STATE_CERT_INTS) if state else
                (*CERT_NAMES, *CERT_HASHES, *CERT_INTS))}, "files": source}


def _build(files, config, secret, root):
    plan, queue, scores = (files.json(path) for path in ("plan.json", "queue.json", "results/scores.json"))
    _require(queue["status"] in CAMPAIGN_TERMINALS, "Only a normally finished campaign can be shared.")
    ids = plan["task_ids"]
    model = plan["model"]
    _require(queue["model"] == model and queue["task_ids"] == ids
             and config.get("model", model) == model and list(config.get("task_ids", ids)) == ids)
    _object(queue["tasks"], ids)
    run = {
        "protocol": plan["protocol"], "framework": plan["execution_framework"],
        "framework_version": plan["harbor_version"], "model": model,
        "harness": {field: plan["harness_selection"][field] for field in HARNESS_FIELDS},
        "limits": {**{field: plan["resources"][field] for field in (*REQUIRED_LIMITS, *OPTIONAL_LIMITS[:-1],
                                                                   "budget_expansion")},
                   "budget_mode": plan["resources"].get("budget_mode", "combined"),
                   "model_context_tokens": plan["model_context_tokens"]},
        "task_ids": ids, "task_sha256": plan["task_sha256"], "proof_lock_sha256": plan["proof_lock_sha256"],
        "created_utc": plan["created_utc"], "ended_utc": queue["ended_utc"], "status": queue["status"],
        "web_enabled": plan["web_enabled"], "completion_token_field": plan["completion_token_field"],
    }
    workers = plan.get("parallel_trials", 1)
    _require(_positive(workers) and config.get("parallel_tasks", workers) == workers)
    if workers > 1:
        run["limits"].update(parallel_tasks=workers, cpus_per_task=plan["resources"]["cpus_per_task"])
    _require(scores["floating_point_certified"] is False and scores["energy_mode"] == "approximate_float")
    score_rows = {row["task_id"]: row for row in scores["rows"]}
    _object(score_rows, ids)
    _require(len(score_rows) == len(scores["rows"]))
    rows = []
    for task_id in ids:
        stored = score_rows[task_id]
        entry = queue["tasks"][task_id]
        _require(stored["status"] == entry["status"] and stored["selected_trial"] == entry["selected_trial"])
        rows.append({"task_id": task_id, "group": stored["group"], "status": stored["status"],
                     "censored": entry.get("censored", False), "scientific_complete": stored["scientific_complete"],
                     "score_complete": stored["score_complete"], **{name: stored[name] for name in ROW_NUMBERS}})
    summary = {"schema": scores["schema"], "rules_sha256": scores["rules_sha256"],
               "task_groups_sha256": scores["task_groups_sha256"], "rows": rows,
               "groups": {name: {field: group[field] for field in GROUP_FIELDS}
                          for name, group in scores["groups"].items()}}
    private_literals = tuple(value.encode("utf-8") for value in
                             (secret, plan.get("api_base_url"), config.get("api_base_url"), str(root))
                             if isinstance(value, str) and value)
    strict = _rows(rows, ids, run["status"])
    metadata = canonical_json({"run": run, "summary": summary})
    _require(all(literal not in metadata for literal in private_literals),
             "A private value was found in the sharing metadata. Nothing was sent.")
    results = [_strict_result(files, run, task, queue["tasks"][task], score_rows[task], energy, private_literals)
               for task, energy in strict.items()]
    payload = {"run": run, "summary": summary, "strict_results": results}
    payload_bytes = canonical_json(payload)
    _require(not secret or secret.encode("utf-8") not in payload_bytes,
             "A private value was found in the submission. Nothing was sent.")
    envelope = {"schema": SCHEMA, "sha256": sha256(payload_bytes), "payload": payload}
    validated_submission_id(envelope)
    return envelope


def build_submission(run_dir: Path, config: dict, secret: str | None = None) -> dict:
    """Read only in-campaign regular files and export the approved share fields."""
    try:
        _require(isinstance(config, dict) and (secret is None or isinstance(secret, str)))
        root = Path(os.path.abspath(run_dir))
        _require(root != Path(root.anchor))
        with ExitStack() as descriptors:
            fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.callback(os.close, fd)
            for part in root.parts[1:]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.callback(os.close, fd)
            return _build(_RunFiles(fd), config, secret, root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as error:
        if isinstance(error, SharingError):
            raise
        raise SharingError("The local benchmark evidence could not be read or did not match. Nothing was sent.") from None


def _destination(endpoint):
    try:
        _require(isinstance(endpoint, str) and endpoint.isascii()
                 and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in endpoint)
                 and not any(char in endpoint for char in "\\?#@%"),
                 "Invalid community intake address.")
        url = urlsplit(endpoint)
        _require(url.hostname is not None and (url.port is None or url.port > 0)
                 and re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/v1/submissions", url.path),
                 "Invalid community intake address.")
        if url.scheme == "https":
            return http.client.HTTPSConnection(url.hostname, url.port, timeout=20), url.path
        _require(url.scheme == "http" and ipaddress.ip_address(url.hostname).is_loopback,
                 "Community intake requires HTTPS.")
        return http.client.HTTPConnection(url.hostname, url.port, timeout=20), url.path
    except ValueError as error:
        if isinstance(error, SharingError):
            raise
        raise SharingError("Invalid community intake address.") from None


def send_submission(endpoint: str, envelope: dict) -> dict:
    """Send to the publisher's configured recipient and require a matching receipt."""
    submission_id = validated_submission_id(envelope)
    body = canonical_json(envelope)
    _require(len(body) <= MAX_SUBMISSION_BYTES, "Community submission exceeds the upload limit.")
    connection, path = _destination(endpoint)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json",
                           "Accept": "application/json", "Idempotency-Key": submission_id})
        with connection.getresponse() as response:
            _require(response.status in (200, 201), "The community intake did not accept the submission.")
            raw = response.read(4097)
            _require(len(raw) <= 4096, "The community intake returned an invalid receipt.")
            receipt = decode_json(raw)
            expected = {"status": "pending", "submission_id": submission_id, "duplicate": response.status == 200}
            _require(isinstance(receipt, dict) and type(receipt.get("duplicate")) is bool and receipt == expected,
                     "The community intake returned an invalid receipt.")
            return expected
    except (http.client.HTTPException, OSError, ValueError, RecursionError) as error:
        if isinstance(error, SharingError):
            raise
        raise SharingError("A community intake receipt could not be obtained. The same submission can be retried.") from None
    finally:
        connection.close()
