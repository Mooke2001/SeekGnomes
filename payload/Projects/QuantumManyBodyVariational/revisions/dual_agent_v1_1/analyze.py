"""Read-only dual-agent usage, price and stored-proof analysis.

Use --partial for an explicitly unfinished snapshot. This program performs no
API call, candidate execution, Lean replay, or mutation of campaign artifacts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

PROJECT = Path(__file__).resolve().parents[2]
EUREKA = PROJECT.parents[1]
sys.path.insert(0, str(PROJECT))

from revisions.hybrid_web_v1.compare_campaigns import (
    Evidence, MODEL, PRICING, api_metrics, finite_number, token_count,
)
from src.correlator_energy import normalize_task

IDS = ("01", "13", "25")
ISSUES = ("math_error", "missing_argument", "representation_error", "infrastructure")
SAFE_MODULES = ("LocalPauli", "StateLimits", "PauliRecovery", "CorrelatorStates",
                "StateClosure", "StateExamples", "TrustedStateSpec", "TrustedStateSpecV2",
                "TrustedSpecExamplesV2", "RationalMath", "HybridMath")
LOCK_NAME = "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json"
SCIENCE = frozenset({"completed", "final_submission_not_verified"})
ATTEMPT_SCHEMA = "dual-agent-handoff-attempt/v1"
ATTEMPT_NAME = re.compile(r"handoff-([0-9]{4,})\Z")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, **kwargs).encode()


def inside(root, value):
    path = Path(os.path.abspath(value))
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("Expected an unaliased path inside " + str(root) + ": " + str(path))
    return path


class Snapshot(Evidence):
    """Read each path once, retaining exact hashes and explicit missing data."""

    def __init__(self):
        super().__init__()
        self.data, self.values = {}, {}

    def read_bytes(self, path):
        path, key = Path(path), str(path)
        if key not in self.data:
            try:
                inside(EUREKA, path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Evidence must be a single-link regular file")
                data = path.read_bytes()
                self.files[key] = {"path": key, "sha256": sha(data), "bytes": len(data)}
            except (OSError, ValueError) as error:
                data = None
                self.files[key] = {"path": key, "sha256": None, "error": type(error).__name__}
            self.data[key] = data
        return self.data[key]

    def read_json(self, path):
        key = str(path)
        if key not in self.values:
            data = self.read_bytes(path)
            try:
                value = json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
            except (ValueError, TypeError, UnicodeError) as error:
                value = None
                self.files[key]["error"] = type(error).__name__
            self.values[key] = value
        return self.values[key]

    def digest(self, path):
        self.read_bytes(path)
        return self.files[str(path)].get("sha256")


def check(name, expected, observed):
    unknown = expected is None or observed is None
    equal = expected == observed and (not isinstance(expected, bool) or type(observed) is bool)
    return {"check": name, "status": "unknown" if unknown else "passed" if equal else "failed",
            "expected": expected, "observed": observed}


def verdict(checks):
    return ("failed" if any(row["status"] == "failed" for row in checks) else
            "unknown" if any(row["status"] == "unknown" for row in checks) else "passed")


def artifact(root, value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("Stored artifact paths must be explicit absolute paths")
    return inside(root, value)


def _present(path):
    """Count a marker itself, including an unreadable or dangling link."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _transaction_record(evidence, path):
    """Transaction records are objects even with a different evidence reader."""
    value = evidence.read_json(path)
    if value is not None and not isinstance(value, dict):
        raise ValueError("Transaction record must be a JSON object")
    return value


def collect_handoffs(trial_dir, task, evidence):
    """Project committed transactions, retaining every other attempt for audit.

    A valid failed marker takes precedence over a residual commit marker. This
    includes faults after a partial commit write. No uncommitted manifest can
    establish a handoff or advance the current successful handoff.
    """
    handoffs, attempts, checks, unresolved_commits = {}, [], [], []
    parent = trial_dir / "handoffs"
    try:
        inside(PROJECT, parent)
        paths = sorted(parent.iterdir()) if _present(parent) else []
    except (OSError, ValueError) as error:
        return {"handoffs": {}, "attempts": [], "unresolved_commits": [str(parent)],
                "checks": [{"check": "handoff_directory", "status": "failed",
                            "error": type(error).__name__}]}
    for path in paths:
        if not path.name.startswith("handoff-"):
            continue
        match = ATTEMPT_NAME.fullmatch(path.name)
        index = int(match[1]) if match else None
        attempt = {"handoff_id": index, "directory": str(path), "status": "uncommitted",
                   "committed": False, "checks": []}
        attempts.append(attempt)
        local = attempt["checks"]
        try:
            inside(PROJECT, path)
            if (not stat.S_ISDIR(path.lstat().st_mode) or index is None or index < 1
                    or path.name != f"handoff-{index:04d}"):
                raise ValueError("Attempt requires a canonical numbered directory")
            failed_path, commit_path = path / "failed.json", path / "committed.json"
            if _present(failed_path):
                failed = _transaction_record(evidence, failed_path)
                local.extend([check(f"handoff.{index}.failed.readable_json", True, failed is not None),
                              check(f"handoff.{index}.failed.schema", ATTEMPT_SCHEMA, (failed or {}).get("schema")),
                              check(f"handoff.{index}.failed.status", "failed", (failed or {}).get("status")),
                              check(f"handoff.{index}.failed.id", True,
                                    type((failed or {}).get("handoff_id")) is int
                                    and failed["handoff_id"] == index)])
                attempt.update(status="failed", failure_record=str(failed_path),
                               record_status=verdict(local),
                               technical_diagnostic=(failed or {}).get("technical_diagnostic"))
                if verdict(local) != "passed" and _present(commit_path):
                    unresolved_commits.append(str(path))
                continue
            if not _present(commit_path):
                # Missing/partial manifests here are expected failed-write
                # residues. They are evidence, never implicit successful commits.
                preparing_path, manifest_path = path / "preparing.json", path / "manifest.json"
                if _present(preparing_path):
                    attempt["preparing_record"] = _transaction_record(evidence, preparing_path)
                if _present(manifest_path):
                    _transaction_record(evidence, manifest_path)
                    attempt["uncommitted_manifest"] = str(manifest_path)
                attempt["reason"] = "No committed marker; excluded from successful handoffs."
                continue
            commit = _transaction_record(evidence, commit_path) or {}
            manifest_path = path / "manifest.json"
            manifest = _transaction_record(evidence, manifest_path) or {}
            local.extend([
                check(f"handoff.{index}.commit.readable_json", True, bool(commit)),
                check(f"handoff.{index}.commit.schema", ATTEMPT_SCHEMA, commit.get("schema")),
                check(f"handoff.{index}.commit.status", "committed", commit.get("status")),
                check(f"handoff.{index}.commit.id", True,
                      type(commit.get("handoff_id")) is int and commit["handoff_id"] == index),
                check(f"handoff.{index}.commit.manifest_sha256", evidence.digest(manifest_path),
                      commit.get("manifest_sha256")),
                check(f"handoff.{index}.manifest.readable_json", True, bool(manifest)),
                check(f"handoff.{index}.manifest.schema", "dual-agent-handoff/v1", manifest.get("schema")),
                check(f"handoff.{index}.manifest.status", "committed", manifest.get("status")),
                check(f"handoff.{index}.manifest.id", True,
                      type(manifest.get("handoff_id")) is int and manifest["handoff_id"] == index),
                check(f"handoff.{index}.source_path", str(path / "source.py"), manifest.get("source_path")),
                check(f"handoff.{index}.proof_path", str(path / "proof.md"), manifest.get("proof_path")),
                check(f"handoff.{index}.source", manifest.get("source_sha256"), evidence.digest(path / "source.py")),
                check(f"handoff.{index}.natural_proof", manifest.get("proof_sha256"), evidence.digest(path / "proof.md")),
                check(f"handoff.{index}.task", sha(encoded(task, indent=2) + b"\n") if task else None,
                      manifest.get("task_sha256")),
            ])
            if verdict(local) == "passed":
                handoffs[index] = manifest
                attempt.update(status="committed", committed=True, manifest_path=str(manifest_path))
            else:
                attempt.update(status="invalid_commit", reason="Committed transaction bindings are unresolved.")
                unresolved_commits.append(str(path))
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            attempt.update(status="invalid_attempt", error=type(error).__name__)
            local.append({"check": f"handoff.{index}.transaction_artifacts", "status": "failed",
                          "error": type(error).__name__})
            unresolved_commits.append(str(path))
        finally:
            checks.extend(local)
    return {"handoffs": handoffs, "attempts": attempts, "checks": checks,
            "unresolved_commits": unresolved_commits}


def collect_role_directories(trial_dir, handoffs):
    """Drop uncommitted broker shells; retain anomalous session/API evidence."""
    directories, audit, checks = [], [], []
    constructor = trial_dir / "constructor"
    if constructor.is_dir():
        directories.append(constructor)
    parent = trial_dir / "formalizer"
    for path in sorted(parent.glob("handoff-*")):
        row = {"directory": str(path), "included": False}
        audit.append(row)
        try:
            inside(PROJECT, path)
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("Formalizer evidence requires a regular directory")
            match = ATTEMPT_NAME.fullmatch(path.name)
            index = int(match[1]) if match else None
            committed = index in handoffs and path.name == f"handoff-{index:04d}"
            session = _present(path / "manifest.json")
            api_artifacts = [item for name in ("api", "API")
                             for item in (path / name).glob("*.json")]
            row.update(handoff_id=index, committed_handoff=committed,
                       session_manifest_present=session, api_artifact_count=len(api_artifacts))
            if committed or session or api_artifacts:
                directories.append(path)
                row.update(included=True, reason="committed_handoff" if committed else "uncommitted_session_evidence")
                if not committed:
                    checks.append(check("role_uncommitted_session." + path.name, True, False))
            else:
                row["reason"] = "uncommitted_broker_shell_without_session_or_API"
        except (OSError, ValueError) as error:
            row.update(reason="unsafe_role_directory", error=type(error).__name__)
            checks.append({"check": "role_directory." + path.name, "status": "failed",
                           "error": type(error).__name__})
    return {"directories": directories, "audit": audit, "checks": checks}


def audit_endpoint(raw, trial_dir, task, handoffs, locked_sources, evidence, *, require_latest=False,
                   active_snapshot=False, unresolved_commits=()):
    """Confirm stored strict-kernel evidence and byte bindings; do not rerun Lean."""
    if raw is None:
        return {"status": "not_submitted", "confirmed": None, "checks": []}
    checks = []
    audit = {"status": "unknown", "confirmed": None, "checks": checks}
    if not isinstance(raw, dict):
        audit["unsupported"] = "endpoint_not_an_object"
        return audit
    try:
        for flag in ("ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified"):
            checks.append(check("receipt." + flag, True, raw.get(flag)))
        checks.append(check("receipt.finite_energy", True, finite_number(raw.get("energy_density"))))
        handoff_id = raw.get("handoff_id")
        checks.append(check("receipt.integer_handoff_id", True, type(handoff_id) is int and handoff_id > 0))
        handoff = handoffs.get(handoff_id, {})
        checks.append(check("handoff.exists", True, bool(handoff)))
        if require_latest:
            checks.append(check("latest_committed_handoff_evidence_complete", True, not unresolved_commits))
            latest = check("final_is_latest_handoff", max(handoffs) if handoffs else None, handoff_id)
            if active_snapshot and latest["status"] == "failed":
                latest.update(status="unknown", reason="New handoff may precede the updated top-level endpoint snapshot")
            checks.append(latest)
        handoff_dir = trial_dir / "handoffs" / f"handoff-{handoff_id:04d}"
        formalizer_dir = trial_dir / "formalizer" / f"handoff-{handoff_id:04d}"
        proof_path = artifact(formalizer_dir, raw.get("proof_manifest"))
        verification, invocation = proof_path.parent, proof_path.parent.parent
        checks.append(check("proof_manifest.layout", True,
            proof_path.name == "manifest.json" and verification.name == "verification"
            and invocation.parent == formalizer_dir / "invocations"
            and invocation.name.endswith(("-score", "-run_lean"))))
        energy_path = artifact(invocation, raw.get("energy_report"))
        checks.append(check("energy_report.layout", invocation / "energy.json", energy_path))
        # Convert Path values before writing this check to JSON.
        checks[-1].update(expected=str(invocation / "energy.json"), observed=str(energy_path))
        proof = evidence.read_json(proof_path) or {}
        energy = evidence.read_json(energy_path) or {}
        certificate, stored_certificate = raw.get("certificate") or {}, proof.get("certificate") or {}
        audit.update(proof_manifest=str(proof_path), energy_report=str(energy_path), handoff_id=handoff_id)
        for flag in ("passed", "compile_passed", "goal_matched", "kernel_verified"):
            checks.append(check("stored_proof." + flag, True, proof.get(flag)))
        for field, expected in (("problems", []), ("candidate_definition_holes", []),
                                ("spec", "hybrid-state-proof/v1"),
                                ("target", "GeneratedState.ProofObligation"),
                                ("theorem", "Submission.accepted"),
                                ("dimension", task.get("dimension")),
                                ("qubits_per_cell", task.get("qubits_per_cell")),
                                ("precision", {"sqrt_bits": 40, "series_terms": 32}),
                                ("lock_sha256", locked_sources.get(LOCK_NAME))):
            checks.append(check("stored_proof." + field, expected, proof.get(field)))
        checks.append(check("certificate.equals_stored", stored_certificate, certificate))
        for field, expected in (("kind", "hybrid-fixed-goal-kernel-v1"),
                                ("theorem", "Submission.accepted"),
                                ("target", "GeneratedState.ProofObligation"),
                                ("lock_sha256", locked_sources.get(LOCK_NAME)),
                                ("permitted_axioms", ["propext", "Quot.sound", "Classical.choice"]),
                                ("dimension", task.get("dimension")),
                                ("qubits_per_cell", task.get("qubits_per_cell")),
                                ("sqrt_bits", 40), ("series_terms", 32)):
            checks.append(check("certificate." + field, expected, certificate.get(field)))
        source_sha, lean_sha = raw.get("source_sha256"), raw.get("proof_sha256")
        bindings = [
            ("handoff.source", source_sha, handoff.get("source_sha256")),
            ("handoff.natural_proof", raw.get("mathematical_proof_sha256"), handoff.get("proof_sha256")),
            ("stored_proof.source", source_sha, proof.get("source_sha256")),
            ("stored_proof.lean_source", lean_sha, proof.get("candidate_source_sha256")),
            ("certificate.source", source_sha, certificate.get("source_sha256")),
            ("certificate.lean_source", lean_sha, certificate.get("candidate_source_sha256")),
            ("energy.source", source_sha, (energy.get("f_manifest") or {}).get("python_source_sha256")),
        ]
        checks.extend(check(name, expected, observed) for name, expected, observed in bindings)
        files = [(source_sha, handoff_dir / "source.py"), (source_sha, invocation / "Source.py"),
                 (source_sha, verification / "trusted/source.py"),
                 (raw.get("mathematical_proof_sha256"), handoff_dir / "proof.md"),
                 (raw.get("mathematical_proof_sha256"), invocation / "proof.md"),
                 (lean_sha, invocation / "Candidate.lean"), (lean_sha, verification / "candidate/Candidate.lean"),
                 (lean_sha, verification / "trusted/Submitted.lean"),
                 (raw.get("formalization_map_sha256"), invocation / "formalization_map.md"),
                 (proof.get("generated_state_sha256"), verification / "trusted/GeneratedState.lean"),
                 (proof.get("candidate_export_sha256"), verification / "export-output/Candidate.ndjson"),
                 (proof.get("candidate_export_sha256"), verification / "trusted/judge-input.ndjson"),
                 (proof.get("candidate_module_sha256"), verification / "trusted/candidate-module/Candidate.olean"),
                 (proof.get("challenge_source_sha256"), verification / "trusted/Challenge.lean"),
                 (proof.get("challenge_export_sha256"), verification / "trusted/Challenge.ndjson")]
        for expected, path in files:
            checks.append(check("file_sha256." + str(path.relative_to(trial_dir)), expected, evidence.digest(path)))
        fixed_challenge = ("module\npublic import GeneratedState\n@[expose] public section\n"
            "namespace Submission\ntheorem accepted : GeneratedState.ProofObligation := by sorry\n"
            "end Submission\n").encode()
        checks.append(check("fixed_challenge_source", sha(fixed_challenge),
                            evidence.digest(verification / "trusted/Challenge.lean")))
        for name in SAFE_MODULES:
            path = verification / "trusted/safe-lib/QuantumManyBodyVariational" / (name + ".olean")
            checks.append(check("generic_module." + name,
                                (proof.get("generic_module_sha256") or {}).get(name), evidence.digest(path)))
        for field in ("generated_state_sha256", "candidate_export_sha256"):
            checks.append(check("certificate." + field, proof.get(field), certificate.get(field)))
        checks.append(check("generated_program_binding", proof.get("generated_state_sha256"),
                            (proof.get("program") or {}).get("lean_definition_sha256")))
        required = ("lock-before", "lock-after", "mathlib-revision", "mathlib-clean", "lean-path",
                    "compile-generated", "compile-challenge", "export-challenge", "compile-candidate",
                    "export-candidate", "judge", *("compile-generic-" + name for name in SAFE_MODULES))
        for label in required:
            stored = (proof.get("checks") or {}).get(label) or {}
            checks.append(check("checker." + label + ".passed", True, stored.get("passed")))
            if label in ("lock-before", "lock-after"):
                checks.append(check("checker." + label + ".mismatches", [], stored.get("mismatches")))
            elif label != "lean-path":
                checks.extend([check("checker." + label + ".exit_code", 0, stored.get("exit_code")),
                               check("checker." + label + ".timed_out", False, stored.get("timed_out")),
                               check("checker." + label + ".log_path", str(verification / (label + ".log")), stored.get("log")),
                               check("checker." + label + ".log_sha256", stored.get("log_sha256"),
                                     evidence.digest(verification / (label + ".log")))])
        for label, field in (("export-candidate", "candidate_export_sha256"),
                             ("export-challenge", "challenge_export_sha256")):
            checks.append(check("checker." + label + ".export", proof.get(field),
                                ((proof.get("checks") or {}).get(label) or {}).get("export_sha256")))
        judge = evidence.read_bytes(verification / "judge.log")
        for text in (b"Lean default kernel accepts the solution", b"Your solution is okay!"):
            checks.append(check("judge_diagnostic." + text.decode(), True,
                                text in judge if judge is not None else None))
        physical_sha = sha(encoded(normalize_task(task), separators=(",", ":")))
        for field, expected in (("energy_density", raw.get("energy_density")), ("mode", "approximate_float"),
                                ("energy_normalization", "per_qubit"), ("floating_point_certified", False),
                                ("energy_is_llm_reported", False), ("dimension", task.get("dimension")),
                                ("qubits_per_cell", task.get("qubits_per_cell")),
                                ("task_physics_sha256", physical_sha)):
            checks.append(check("stored_energy." + field, expected, energy.get(field)))
        checks.append(check("receipt.task_physics_sha256", physical_sha, raw.get("task_physics_sha256")))
        preconditions = energy.get("preconditions") or {}
        for field in ("legality", "translation_invariance", "numerical_preconditions"):
            checks.append(check("energy_precondition." + field, "verified_by_fixed_goal_kernel_replay", preconditions.get(field)))
        checks.append(check("energy_precondition.proof_checked_here", True, preconditions.get("proof_checked_here")))
        # Compare common compiler fields, excluding the historical namespace-specific definition hash.
        program = proof.get("program") or {}
        for field in sorted(set(program) - {"lean_definition_sha256", "lean_namespace"}):
            checks.append(check("compiler_binding." + field, (proof.get("program") or {}).get(field),
                                (energy.get("f_manifest") or {}).get(field)))
        terms = energy.get("terms")
        if isinstance(terms, list) and len(terms) == len(task.get("terms", [])):
            contributions = []
            for index, term in enumerate(terms):
                expected_term = normalize_task(task)["terms"][index]
                checks.append(check(f"energy_term.{index}.word", expected_term["word"], term.get("word")))
                checks.append(check(f"energy_term.{index}.coefficient", expected_term["coefficient"], term.get("coefficient")))
                coefficient, expectation = float(Fraction(expected_term["coefficient"])), term.get("expectation")
                if not finite_number(expectation):
                    raise ValueError("Nonfinite or unsupported stored expectation")
                contribution = coefficient * expectation
                checks.append(check(f"energy_term.{index}.product", contribution, term.get("contribution_per_cell")))
                contributions.append(contribution)
            checks.append(check("stored_energy.fsum", raw.get("energy_density"),
                                math.fsum(contributions) / task["qubits_per_cell"]))
        else:
            checks.append(check("stored_energy.complete_terms", True, False))
    except (ValueError, TypeError, KeyError, OSError, ArithmeticError, AttributeError, IndexError) as error:
        checks.append({"check": "artifact_validation", "status": "failed", "error": type(error).__name__ + ": " + str(error)})
    audit["status"] = verdict(checks)
    if audit["status"] == "passed":
        audit["confirmed"] = {key: raw.get(key) for key in ("energy_density", "source_sha256", "proof_sha256",
            "mathematical_proof_sha256", "handoff_id", "proof_manifest", "energy_report", "task_physics_sha256")}
        audit["confirmed"].update(state_legality_kernel_verified=True, energy_certified=False,
                                  ground_state_proven=False, energy_mode="approximate_float")
    return audit


def role_metrics(directory, expected_role, evidence, *, active_snapshot=False):
    directory = inside(PROJECT, directory)
    manifest = evidence.read_json(directory / "manifest.json")
    metrics = api_metrics(directory, manifest, evidence)
    usage = manifest or {}
    rows = usage.get("budget_ledger") or []
    metrics.update(role=expected_role, directory=str(directory), status=usage.get("status"),
        context_rotation_count=usage.get("context_rotation_count"),
        active_wall_seconds=usage.get("active_wall_seconds"),
        server_cpu_seconds_reserved=usage.get("server_cpu_seconds_charged"),
        cpu_measure="one-CPU numerical wall reservation; measured CPU and Lean CPU unavailable",
        in_flight_api_reservations=[row for row in rows if row.get("kind") == "api" and row.get("in_flight")],
        limits=usage.get("limits"), context_rotations=usage.get("context_rotations"))
    checks = [check("role", expected_role, usage.get("role")), check("model", MODEL, usage.get("model"))]
    for rotation in usage.get("context_rotations", []):
        path = artifact(directory, str(directory / rotation.get("archive", "missing-history")))
        checks.append(check("context_archive." + path.name, rotation.get("archive_sha256"), evidence.digest(path)))
    turns, count = usage.get("turns"), metrics["api_record_count"]
    if token_count(turns) and token_count(count):
        coverage = check("recorded_attempts_plus_in_flight", turns,
                         count + len(metrics["in_flight_api_reservations"]))
        if active_snapshot and coverage["status"] == "failed":
            coverage.update(status="unknown", reason="Active files may advance between the manifest and API-directory snapshots")
        checks.append(coverage)
    metrics["checks"] = checks
    return metrics


def combined(metrics):
    """Aggregate actual response rows once, retaining missing usage and prices."""
    records = [row for item in metrics for row in item["api_records"]]
    result = {"role_session_count": len(metrics), "api_record_count": len(records),
              "roles_with_unknown_manifest": sum(item.get("manifest_turns") is None for item in metrics)}
    for name in ("manifest_turns", "input_tokens_charged", "output_tokens_charged",
                 "server_cpu_seconds_reserved", "context_rotation_count", "active_wall_seconds"):
        values = [item.get(name) for item in metrics]
        known = [value for value in values if finite_number(value)]
        result[name] = sum(known) if known else None
        result[name + "_unknown_roles"] = len(values) - len(known)
    result["raw_usage"] = {}
    for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
        values = []
        for row in records:
            usage = row.get("usage") or {}
            value = (usage.get("output_tokens_details") or {}).get(field) if field == "reasoning_tokens" else usage.get(field)
            if token_count(value):
                values.append(value)
        result["raw_usage"][field + "_known_subtotal"] = sum(values) if values else None
        result["raw_usage"][field + "_unknown_records"] = len(records) - len(values)
    result["raw_usage"]["reasoning_is_output_subset_not_added_again"] = True
    known_prices = [row["token_price_estimate"]["usd"] for row in records if row["token_price_estimate"]["usd"] is not None]
    searches = [item.get("search_action_count") for item in metrics]
    known_searches = [value for value in searches if token_count(value)]
    search_count = sum(known_searches) if known_searches else None
    search_usd = search_count * PRICING["per_observed_search_action"] if search_count is not None else None
    token_usd = sum(known_prices) if known_prices else None
    complete = bool(records) and all(item["price"]["complete_for_observed_records"] for item in metrics)
    result["price"] = {"known_token_subtotal_usd": token_usd, "observed_search_actions": search_count,
        "observed_search_subtotal_usd": search_usd, "unpriced_api_records": len(records) - len(known_prices),
        "complete_for_observed_records": complete,
        "observed_token_plus_search_estimate_usd": token_usd + search_usd if complete else None,
        "known_token_plus_observed_search_subtotal_usd":
            (token_usd or 0) + (search_usd or 0) if token_usd is not None or search_usd is not None else None,
        "billing_total_usd": None, "unobserved_or_disconnected_usage_usd": None,
        "scope": "Frozen PRICING snapshot; observed usage and searches only, not an account bill."}
    result["in_flight_api_reservation_count"] = sum(len(item["in_flight_api_reservations"]) for item in metrics)
    return result


def endpoint_status(audit, scientific):
    if audit["confirmed"] is not None:
        return True
    return False if scientific and audit["status"] == "not_submitted" else None


def analyze(run, out, *, partial=False):
    if not Path(run).is_absolute() or not Path(out).is_absolute():
        raise ValueError("--run and --out must be absolute paths")
    run, out = inside(PROJECT, run), inside(EUREKA, out)
    if not run.is_dir() or not out.parent.is_dir() or out.exists() or out.is_relative_to(run):
        raise ValueError("Require an existing campaign and a fresh output outside that campaign")
    evidence = Snapshot()
    plan, queue = evidence.read_json(run / "plan.json"), evidence.read_json(run / "queue.json")
    if not plan or not queue or plan.get("schema") != "dual-agent-plan/v1" or queue.get("schema") != "dual-agent-queue/v1":
        raise ValueError("Missing or unsupported dual-agent plan/queue")
    if queue.get("status") != "finished" and not partial:
        raise ValueError("Campaign queue is not finished; use --partial for an unfinished snapshot")
    entries = queue.get("entries")
    if (not isinstance(entries, list) or len(entries) != len(IDS)
            or sorted(str(row.get("task_id")).zfill(2) for row in entries) != list(IDS)):
        raise ValueError("Expected exactly one queued trial for each of 01/13/25")
    if tuple(plan.get("task_ids", [])) != IDS:
        raise ValueError("Unsupported campaign task selection")
    existing_summary = evidence.read_json(run / "summary.json")
    locked_sources = plan.get("source_sha256") or {}
    rule_path = PROJECT / "config/benchmark50_score_rules.json"
    rules = evidence.read_json(rule_path) or {}
    rules_check = check("frozen_score_rules", locked_sources.get("config/benchmark50_score_rules.json"), evidence.digest(rule_path))
    lock_check = check("frozen_proof_lock", locked_sources.get(LOCK_NAME), evidence.digest(PROJECT / LOCK_NAME))
    campaign_checks = [rules_check, lock_check,
        check("queue.plan_sha256", evidence.digest(run / "plan.json"), queue.get("plan_sha256"))]
    rows, all_roles = [], []
    for task_id in IDS:
        entry = next(row for row in entries if str(row["task_id"]).zfill(2) == task_id)
        directory = artifact(run, entry.get("trial_dir"))
        manifest = evidence.read_json(directory / "manifest.json") or {}
        task = evidence.read_json(directory / "task.json") or {}
        checks = []
        if task:
            task_hash = sha(encoded(task))
            checks.extend([check("queue.task_sha256", task_hash, entry.get("task_sha256")),
                           check("manifest.task_sha256", task_hash, manifest.get("task_sha256")),
                           check("manifest.model", plan.get("model"), manifest.get("model")),
                           check("manifest.runtime_sources", locked_sources, manifest.get("runtime_sources"))])
        transactions = collect_handoffs(directory, task, evidence)
        handoffs = transactions["handoffs"]
        checks.extend(transactions["checks"])
        role_projection = collect_role_directories(directory, handoffs)
        role_dirs = role_projection["directories"]
        checks.extend(role_projection["checks"])
        roles = [role_metrics(path, "constructor" if path.name == "constructor" else "formalizer", evidence,
                              active_snapshot=queue.get("status") != "finished")
                 for path in role_dirs]
        all_roles.extend(roles)
        issues = []
        for role in roles:
            for path in sorted(Path(role["directory"]).glob("invocations/*-report-issue/issue.json")):
                issue = evidence.read_json(path) or {}
                issues.append({"path": str(path), "handoff_id": issue.get("handoff_id"),
                               "kind": issue.get("issue_kind"), "detail": issue.get("detail")})
        terminal = manifest.get("formalizer_terminal") or {}
        if terminal.get("session_status") == "infrastructure_failure" and terminal.get("issue_kind") != "infrastructure":
            issues.append({"path": str(directory / "manifest.json"), "kind": "infrastructure",
                           "detail": terminal.get("stop_reason"), "origin": "formalizer_transport_terminal"})
        distribution = Counter(row.get("kind") or "unknown" for row in issues)
        endpoint_audits = {name: audit_endpoint(manifest.get(key), directory, task, handoffs,
                                             locked_sources, evidence, require_latest=name == "final",
                                             unresolved_commits=transactions["unresolved_commits"],
                                             active_snapshot=queue.get("status") != "finished")
                           for name, key in (("best", "best_verified_result"), ("final", "verified_result"))}
        best, final = (endpoint_audits[key]["confirmed"] for key in ("best", "final"))
        if best is not None and final is not None:
            best_order = check("best_energy_not_above_final", True, best["energy_density"] <= final["energy_density"])
            endpoint_audits["best"]["checks"].append(best_order)
            if best_order["status"] != "passed":
                endpoint_audits["best"].update(status="failed", confirmed=None)
                best = None
        baseline = None
        if rules_check["status"] == "passed":
            try:
                baseline = float(Fraction(rules["tasks"][task_id]["baseline"]))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
        scientific = entry.get("status") == "scientific_terminal" and manifest.get("status") in SCIENCE
        best_status, final_status = (endpoint_status(endpoint_audits[name], scientific) for name in ("best", "final"))
        row = {"task_id": task_id, "task_name": task.get("name"), "trial_dir": str(directory),
            "process_status": entry.get("status"), "runtime_status": manifest.get("status"),
            "scientific_terminal": scientific, "incomplete": not scientific,
            "evidence_unresolved": (verdict(transactions["checks"] + role_projection["checks"]) != "passed"
                                    or any(audit["status"] in ("unknown", "failed")
                                           for audit in endpoint_audits.values())),
            "baseline_energy_per_qubit": baseline, "best": best, "final": final,
            "best_verified": best_status, "final_verified": final_status,
            "best_energy_per_qubit": best["energy_density"] if best else None,
            "final_energy_per_qubit": final["energy_density"] if final else None,
            "baseline_improved": best["energy_density"] < baseline - 1e-10 if best and baseline is not None else False if best_status is False and baseline is not None else None,
            "absolute_baseline_improvement": baseline - best["energy_density"] if best and baseline is not None else None,
            "improvement_is_certified": False, "handoff_count": len(handoffs),
            "handoff_attempts": transactions["attempts"],
            "unresolved_committed_attempts": transactions["unresolved_commits"],
            "formalizer_directory_audit": role_projection["audit"],
            "feedback_distribution": {name: distribution[name] for name in ISSUES},
            "unsupported_feedback_kinds": {name: count for name, count in distribution.items() if name not in ISSUES},
            "feedback_issues": issues, "roles": roles, "endpoint_audits": endpoint_audits,
            "usage_by_role": {name: combined([role for role in roles if role["role"] == name])
                              for name in ("constructor", "formalizer")},
            "end_to_end_usage": combined(roles), "checks": checks}
        rows.append(row)
    analysis_paths = [Path(__file__).resolve(), PROJECT / "revisions/hybrid_web_v1/compare_campaigns.py",
                      PROJECT / "src/correlator_energy.py"]
    analysis_sources = {str(path): evidence.digest(path) for path in analysis_paths}
    all_checks = campaign_checks + [item for row in rows for item in row["checks"]]
    all_checks += [item for row in rows for role in row["roles"] for item in role["checks"]]
    all_checks += [item for row in rows for audit in row["endpoint_audits"].values() for item in audit["checks"]]
    result = {"schema": "dual-agent-analysis/v1", "created_utc": datetime.now(timezone.utc).isoformat(),
        "run": str(run), "experiment_id": plan.get("experiment_id"), "model": plan.get("model"),
        "queue_status": queue.get("status"), "partial_requested": partial,
        "unfinished_snapshot": queue.get("status") != "finished", "requested_trials": len(IDS),
        "scientific_terminal_count": sum(row["scientific_terminal"] for row in rows),
        "incomplete_or_technical_trial_count": sum(row["incomplete"] for row in rows),
        "best_verified_count_observed": sum(row["best"] is not None for row in rows),
        "final_verified_count_observed": sum(row["final"] is not None for row in rows),
        "baseline_improvement_count_observed": sum(row["baseline_improved"] is True for row in rows),
        "rows": rows, "usage_by_role": {name: combined([role for role in all_roles if role["role"] == name])
                                          for name in ("constructor", "formalizer")},
        "end_to_end_usage": combined(all_roles), "pricing": PRICING,
        "checks": campaign_checks, "integrity_status": verdict(all_checks),
        "analysis_sources": analysis_sources, "frozen_runtime_sources": locked_sources,
        "source_summary_created_utc": (existing_summary or {}).get("created_utc"),
        "evidence": list(evidence.files.values()),
        "interpretation": {"verification": "Stored strict-kernel evidence and archive consistency checked; no new Lean replay.",
            "energy": "Approximate float for a kernel-verified legal state; neither energy error nor improvement certified.",
            "missing_results": "Unfinished and technical trials retain null endpoints; no zero score is imputed.",
            "usage": "Each actual API record counted once; reasoning is an output subset; in-flight reservations separate.",
            "handoffs": "Only transactions with valid committed markers and byte bindings count as handoffs; failed and uncommitted attempts remain in each trial's audit.",
            "roles": "Uncommitted broker shells with no session/API records are excluded; anomalous uncommitted session/API evidence is retained and flagged incomplete.",
            "partial": "Each file read once; an active campaign snapshot is not atomic across files."}}
    out.mkdir(mode=0o700)
    (out / "analysis.json").write_bytes(encoded(result, indent=2) + b"\n")
    write_markdown(out / "SUMMARY.md", result)
    return result


def display(value):
    return "未提供/无法判断" if value is None else str(value)


def write_markdown(path, result):
    lines = ["# 双agent三题分析", "", f"来源: `{result['run']}`", "",
        f"快照UTC: {result['created_utc']}. 队列: {result['queue_status']}. "
        f"未完成快照: {result['unfinished_snapshot']}. 科学终态: {result['scientific_terminal_count']}/3.", "",
        "| 题号 | 进程/阶段 | best能量/qubit | final能量/qubit | 基线 | best改善 | 交接数 |",
        "|---|---|---:|---:|---:|---|---:|"]
    for row in result["rows"]:
        lines.append(f"| {row['task_id']} | {row['process_status']}/{row['runtime_status']} | "
            f"{display(row['best_energy_per_qubit'])} | {display(row['final_energy_per_qubit'])} | "
            f"{display(row['baseline_energy_per_qubit'])} | {display(row['baseline_improved'])} | {row['handoff_count']} |")
    lines += ["", "| 范围 | API响应记录 | 原始输入token已知小计 | 原始输出token已知小计 | CPU秒预留 | 上下文轮换 | 已知费用小计/USD |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for label, usage in [*result["usage_by_role"].items(), ("端到端", result["end_to_end_usage"])]:
        lines.append(f"| {label} | {usage['api_record_count']} | "
            f"{display(usage['raw_usage']['input_tokens_known_subtotal'])} | "
            f"{display(usage['raw_usage']['output_tokens_known_subtotal'])} | "
            f"{display(usage['server_cpu_seconds_reserved'])} | {display(usage['context_rotation_count'])} | "
            f"{display(usage['price']['known_token_plus_observed_search_subtotal_usd'])} |")
    lines += ["", "| 题号 | math_error | missing_argument | representation_error | infrastructure |",
              "|---|---:|---:|---:|---:|"]
    for row in result["rows"]:
        lines.append("| " + row["task_id"] + " | " + " | ".join(str(row["feedback_distribution"][name]) for name in ISSUES) + " |")
    usage = result["end_to_end_usage"]
    lines += ["", f"归档一致性检查: {result['integrity_status']}. 在途API预留: {usage['in_flight_api_reservation_count']}.", "",
        "未完成或技术缺失保留为空, 不填0. 表内费用是已知token及已观察搜索动作的小计, "
        f"采用{PRICING['as_of']}冻结价表; 无法定价的API记录为{usage['price']['unpriced_api_records']}条, "
        "不视为账单. reasoning token包含于output, 不重复计费. CPU为数值工具预留, 不包含实测CPU或Lean CPU.", "",
        "认证端点已核对保存的源文件, Lean源码, 固定目标与内核回执, 日志及energy.json. "
        "本分析未重新运行Lean. 能量仍为浮点计算, 改善幅度不带严格数值误差证书.", "",
        "完整逐请求usage, 分角色计账, 核验检查和来源SHA见`analysis.json`. 运行中快照按文件读取, "
        "跨文件不保证同一瞬间. 本分析不向被测试次注入任何材料.", ""]
    path.write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    try:
        result = analyze(args.run, args.out, partial=args.partial)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"out": str(args.out), "queue_status": result["queue_status"],
                      "integrity_status": result["integrity_status"],
                      "scientific_terminal_count": result["scientific_terminal_count"]}, ensure_ascii=False))
    return 1 if result["integrity_status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
