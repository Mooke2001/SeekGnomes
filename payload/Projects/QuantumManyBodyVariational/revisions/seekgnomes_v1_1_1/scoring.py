"""Evidence-bound, two-group SeekGnomes reports, with no API calls.

Reporting checks stored strict-checker evidence and recomputes the floating
energy. It does not replace independently rerunning the Lean kernel when
accepting an untrusted third-party result archive.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from src.benchmark50_models import load_catalog

GROUPS = ("exactly_solvable", "generic")
FLAGS = ("ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified")
METER_FIELDS = ("turns", "input_tokens_charged", "output_tokens_charged",
                "server_cpu_seconds_charged", "active_wall_seconds")
COMPLETE = frozenset({"completed", "scientific_failure"})
STAGES = ("lock-before", "compile-generated", "compile-challenge", "export-challenge",
          "compile-candidate", "export-candidate", "judge", "lock-after")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text())


def _write_text(path, content):
    """Atomically replace a report file without following a destination link."""
    descriptor, temp = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _inside(path, root):
    path, root = Path(path), Path(root).resolve(strict=True)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Report input may not traverse symbolic links")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Report input leaves this run: " + str(path))
    return resolved


def _trial_path(value, root):
    if not isinstance(value, str) or not value:
        raise ValueError("A relative selected_trial path is required")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Queue trial paths must be relative and unaliased")
    return _inside(root / path, root)


def _catalogues():
    groups = _read(HERE / "task_groups.json")
    if groups["catalogue_sha256"] != digest(PROJECT / "config/benchmark50_tasks.json"):
        raise ValueError("Scoring task catalogue drift")
    tasks = {t["id"]: t for t in load_catalog()["tasks"]}
    rules = _read(PROJECT / "config/benchmark50_score_rules.json")["tasks"]
    if set(tasks) != set(groups["tasks"]) or set(tasks) != set(rules):
        raise ValueError("Grouping/rule coverage does not equal the fifty tasks")
    for key, task in tasks.items():
        if (groups["tasks"][key]["task_sha256"] != task["canonical_sha256"]
                or rules[key]["task_sha256"] != task["canonical_sha256"]
                or groups["tasks"][key]["group"] not in GROUPS):
            raise ValueError("Task/rule/group identity mismatch: " + key)
    return tasks, rules, groups


def _evidence_path(result, trial):
    relative = result.get("proof_manifest_relative")
    if relative:
        return _trial_path(relative, trial)
    # Old absolute paths can still be read in place. For a moved archive,
    # require one unique stored certificate match, never an external path.
    value = result.get("proof_manifest")
    if isinstance(value, str):
        path = Path(value)
        if not path.is_absolute():
            return _trial_path(value, trial)
        if path.is_relative_to(trial):
            return _inside(path, trial)
    matches = []
    for path in trial.rglob("manifest.json"):
        if path.is_symlink() or path == trial / "manifest.json":
            continue
        report = _read(_inside(path, trial))
        if report.get("certificate") == result.get("certificate") and report.get("passed") is True:
            matches.append(path)
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous in-trial strict verification manifest")
    return matches[0]


def verify_stored_result(result, task, trial):
    """Validate archived checker bindings and recompute the host float energy.

    Self-consistent evidence is not a signature by a trusted external operator.
    A community submission still needs fresh verification of its source/proof.
    """
    if not isinstance(result, dict) or not all(result.get(k) is True for k in FLAGS):
        raise ValueError("A passing set of strict checker flags is required")
    cert = result.get("certificate")
    if not isinstance(cert, dict):
        raise ValueError("Missing fixed-goal certificate")
    if cert.get("kind") == "state-format-fixed-goal-kernel-v1":
        from revisions.seekgnomes_v1_2.state_verifier import BITS, LOCK, PRECISION_BITS, TERMS
        from src.state_format import StateProgram, runtime_sha256
        is_state = True
        lock_path, source_name = LOCK, "source.state"
        if cert.get("runtime_sha256") != runtime_sha256():
            raise ValueError("State evaluator runtime differs from the checked submission")
    elif cert.get("kind") == "hybrid-fixed-goal-kernel-v1":
        is_state = False
        lock_path = PROJECT / "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json"
        source_name = "source.py"
    else:
        raise ValueError("Unknown fixed-goal certificate kind")
    expected = {"theorem": "Submission.accepted",
                "target": "GeneratedState.ProofObligation",
                "dimension": task["dimension"], "qubits_per_cell": task["qubits_per_cell"],
                "lock_sha256": digest(lock_path)}
    if any(cert.get(k) != v for k, v in expected.items()):
        raise ValueError("Certificate is not bound to this task and current fixed goal")
    manifest = _evidence_path(result, trial)
    report, evidence = _read(manifest), manifest.parent
    if (report.get("passed") is not True or report.get("certificate") != cert
            or any(report.get(k) is not True for k in ("compile_passed", "goal_matched", "kernel_verified"))
            or report.get("candidate_definition_holes") != []
            or cert.get("candidate_definition_holes") != []
            or set(cert.get("permitted_axioms", [])) != {"propext", "Quot.sound", "Classical.choice"}):
        raise ValueError("Stored checker report does not establish the fixed proof obligation")
    for name in STAGES:
        item = report.get("checks", {}).get(name, {})
        if item.get("passed") is not True:
            raise ValueError("Missing passing strict verification stage: " + name)
    # Check every retained subprocess log against the manifest. Log paths in
    # the original container are deliberately resolved by fixed local names.
    for name, item in report.get("checks", {}).items():
        if item.get("log_sha256"):
            local = _inside(evidence / (name + ".log"), trial)
            if digest(local) != item["log_sha256"]:
                raise ValueError("Stored verifier log changed: " + name)
    if is_state and report.get("runtime_sha256") != cert["runtime_sha256"]:
        raise ValueError("Stored checker report has a different state evaluator")
    for name, key in ((source_name, "source_sha256"), ("Submitted.lean", "candidate_source_sha256"),
                      ("GeneratedState.lean", "generated_state_sha256"),
                      ("judge-input.ndjson", "candidate_export_sha256")):
        local = _inside(evidence / "trusted" / name, trial)
        if digest(local) != cert.get(key) or report.get(key) != cert.get(key):
            raise ValueError("Stored source/proof/export binding changed: " + name)
    if result.get("source_sha256") != cert["source_sha256"]:
        raise ValueError("Result belongs to another correlator source")
    source_path = evidence / "trusted" / source_name
    lattice = (task["dimension"], task["qubits_per_cell"])
    if is_state:
        program = StateProgram(source_path.read_bytes().decode("utf-8"), *lattice,
                               bits=BITS, terms=TERMS, precision_bits=PRECISION_BITS)
        generated = program.lean_source().encode()
    else:
        from revisions.dual_agent_v1_1.compiler import CorrelatorProgram
        program = CorrelatorProgram.from_source(source_path.read_text(), *lattice,
            bits=cert.get("sqrt_bits"), terms=cert.get("series_terms"))
        generated = program.lean_definition(namespace="GeneratedState").encode()
    if hashlib.sha256(generated).hexdigest() != cert["generated_state_sha256"]:
        raise ValueError("The current compiler generates a different Lean definition")
    if is_state:
        numeric = program.energy(task)
        energy_report = _inside(evidence.parent / "energy.json", trial)
        stored_bytes = energy_report.read_bytes()
        archived = json.loads(stored_bytes)
        bindings = archived.get("verification", {})
        keys = ("kind", "theorem", "target", "source_sha256", "candidate_source_sha256",
                "generated_state_sha256", "runtime_sha256", "lock_sha256")
        if (hashlib.sha256(stored_bytes).hexdigest() != result.get("energy_report_sha256")
                or any(bindings.get(key) != cert.get(key) for key in keys)
                or archived.get("energy_density") != result.get("energy_density")
                or archived.get("energy_normalization") != "per_qubit"
                or archived.get("energy_is_llm_reported") is not False
                or archived.get("preconditions", {}).get("proof_checked_here") is not True
                or archived.get("task_physics_sha256") != numeric["task_physics_sha256"]):
            raise ValueError("Stored energy report is not bound to the checked state and task")
    else:
        from src.correlator_energy import evaluate_energy
        numeric = evaluate_energy(task, program.evaluate, f_manifest=program.manifest())
    energy = result.get("energy_density")
    if (type(energy) not in (int, float) or not math.isfinite(energy)
            or not math.isclose(energy, numeric["energy_density"], rel_tol=1e-12, abs_tol=1e-12)):
        raise ValueError("Stored energy does not equal the recomputed host energy")
    return {"energy_density": numeric["energy_density"],
            "proof_manifest_relative": str(manifest.relative_to(trial)),
            "certificate": cert, "floating_point_certified": False}


def task_reference(task, group):
    """Get a host-owned exact reference; missing references never use a baseline."""
    if group != "exactly_solvable":
        return None
    try:
        from .exact_references import energy_reference
        result = energy_reference(task)
    except (ImportError, OSError, ValueError, KeyError, ArithmeticError) as error:
        return {"status": "reference_pending", "value": None,
                "reason": "Exact reference provider unavailable: " + type(error).__name__}
    if not isinstance(result, dict):
        return {"status": "reference_pending", "value": None,
                "reason": "No verified exact ground-energy reference is configured"}
    return result


def score_parameters(task):
    """Current-task parameters shared by Constructor feedback and reporting."""
    rule = _read(PROJECT / "config/benchmark50_score_rules.json")["tasks"][task["id"]]
    group = _read(HERE / "task_groups.json")["tasks"][task["id"]]
    if (rule["task_sha256"] != task["canonical_sha256"]
            or group["task_sha256"] != task["canonical_sha256"]):
        raise ValueError("Task/rule/group identity mismatch in score feedback")
    return {"baseline": float(Fraction(rule["baseline"])),
            "lower": float(Fraction(rule["algebraic_lower"])),
            "scale": float(Fraction(rule["scale"])), "group": group["group"],
            "reference": task_reference(task, group["group"])}


def public_reference_metadata(reference):
    """Only scoring target scalars, never a host reference solver or derivation."""
    if not isinstance(reference, dict):
        return None
    return {name: reference[name] for name in
            ("value", "status", "normalization", "numerical_tolerance") if name in reference}


def task_metrics(energy, *, baseline, lower, threshold=0.01,
                 group="generic", reference=None, scale=None):
    """Keep P/R fixed; exact A uses ground error, generic A uses baseline gain."""
    baseline, lower = float(baseline), float(lower)
    if not (math.isfinite(baseline) and math.isfinite(lower) and baseline > lower):
        raise ValueError("A finite strictly positive baseline-to-lower scale is required")
    if group not in GROUPS:
        raise ValueError("Unknown scoring group")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("The baseline-improvement threshold must lie in [0,1]")
    if scale is not None:
        scale = float(scale)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("A finite strictly positive interaction scale is required")
    if group == "exactly_solvable" and scale is None:
        raise ValueError("Exact-group scoring requires the interaction scale")
    common = {"baseline_gain": 0.0, "interaction_scale": scale,
        "ground_energy_reference": reference, "ground_energy_reference_value": None,
        "ground_energy_gap": None, "raw_signed_gap": None,
        "normalized_ground_energy_gap": None, "effective_normalized_ground_energy_gap": None,
        "reference_status": "not_required" if group == "generic" else "reference_pending",
        "score_status": "ready", "score_complete": True, "scoring_error": None,
        "amplitude_definition": ("clamp(1-(E-e0)/s,0,1)" if group == "exactly_solvable"
                                 else "clamp((B-E)/(B-L),0,1)")}
    if energy is None:
        return {**common, "pass_rate": 0.0, "improvement_rate": 0.0,
                "amplitude": 0.0, "score": 0.0, "reference_status": "not_needed_for_scientific_failure"}
    if type(energy) not in (int, float) or not math.isfinite(energy):
        raise ValueError("Energy must be finite or None")
    # Keep the generic lower-bound diagnostic. Exact-group diagnostics below
    # first retain the reference and signed gap, including when e0 equals L.
    if group == "generic" and energy < lower - 1e-9 * max(1, abs(lower)):
        raise ValueError("Recomputed energy lies below the algebraic lower bound")
    baseline_gain = min(1.0, max(0.0, (baseline - energy) / (baseline - lower)))
    improved = float(baseline_gain >= threshold)
    result = {**common, "pass_rate": 1.0, "improvement_rate": improved,
              "baseline_gain": baseline_gain}
    if group == "generic":
        amplitude = baseline_gain
    else:
        value = reference.get("value") if isinstance(reference, dict) else None
        if (type(value) not in (int, float) or not math.isfinite(value)
                or reference.get("status") in {"reference_pending", "pending", "unverified", "unsupported"}):
            return {**result, "amplitude": None, "score": None,
                    "score_status": "reference_pending", "score_complete": False,
                    "scoring_error": "A verified exact ground-energy reference is required"}
        tolerance = reference.get("numerical_tolerance", 0.0)
        if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Invalid exact-reference numerical tolerance")
        # This is a numerical anomaly tolerance, not a certified error bound.
        tolerance = max(float(tolerance), 1e-9 * scale)
        result.update(reference_status="available", ground_energy_reference_value=value,
                      reference_numerical_tolerance=tolerance)
        if reference.get("normalization", "per_qubit") != "per_qubit":
            return {**result, "amplitude": None, "score": None,
                    "score_status": "reference_invalid", "score_complete": False,
                    "scoring_error": "The exact reference must use per-qubit normalization"}
        if value < lower - tolerance or value > baseline + tolerance:
            return {**result, "amplitude": None, "score": None,
                    "score_status": "reference_invalid", "score_complete": False,
                    "scoring_error": "The exact reference lies outside the known [L,B] bounds"}
        gap = energy - value
        result.update(ground_energy_gap=gap, raw_signed_gap=gap,
                      normalized_ground_energy_gap=gap / scale)
        if gap < -tolerance:
            return {**result, "improvement_rate": None, "amplitude": None, "score": None,
                    "score_status": "numerical_anomaly", "score_complete": False,
                    "scoring_error": "The computed energy is below the exact reference beyond tolerance"}
        normalized = max(0.0, gap / scale)
        result["effective_normalized_ground_energy_gap"] = normalized
        result["negative_gap_within_tolerance"] = gap < 0
        amplitude = min(1.0, max(0.0, 1.0 - normalized))
    return {**result, "amplitude": amplitude,
            "score": 30.0 + 30.0 * improved + 40.0 * amplitude}


def _meters(trial, manifest):
    values = {name: 0 for name in METER_FIELDS}
    # Prefer role manifests: these retain CPU and active time across reconnects.
    role_paths = []
    for name in ("constructor/manifest.json",):
        if (trial / name).is_file():
            role_paths.append(trial / name)
    for path in trial.rglob("manifest.json"):
        if path == trial / "manifest.json" or path in role_paths or path.is_symlink():
            continue
        data = _read(_inside(path, trial))
        if data.get("role") == "formalizer" and "budget_ledger" in data:
            role_paths.append(path)
    if role_paths:
        for path in role_paths:
            data = _read(_inside(path, trial))
            for name in METER_FIELDS:
                value = data.get(name, 0)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid resource meter in " + str(path))
                values[name] += value
    else:
        for role in ("constructor_usage", "formalizer_usage"):
            data = manifest.get(role) or {}
            for name in METER_FIELDS:
                value = data.get(name, 0)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    values[name] += value
    return values


def score_run(run_dir):
    root = Path(run_dir).resolve(strict=True)
    tasks, rules, grouping = _catalogues()
    queue = _read(root / "queue.json")
    entries = queue.get("tasks")
    if not isinstance(entries, dict) or any(k not in tasks for k in entries):
        raise ValueError("Queue must map canonical task IDs to task entries")
    expected_ids = queue.get("task_ids", list(entries))
    if (not isinstance(expected_ids, list) or len(expected_ids) != len(set(expected_ids))
            or any(k not in tasks for k in expected_ids) or set(expected_ids) != set(entries)):
        raise ValueError("Queue task_ids must exactly equal the selected task entries")
    rows, total_meter = [], {name: 0 for name in METER_FIELDS}
    for key in expected_ids:
        entry, task, rule, group = entries[key], tasks[key], rules[key], grouping["tasks"][key]
        status = entry.get("status", "pending")
        row = {"task_id": key, "name": task["name"], "group": group["group"],
               "subclass": group["subclass"], "status": status, "energy_density": None,
               "baseline": float(Fraction(rule["baseline"])),
               "algebraic_lower": float(Fraction(rule["algebraic_lower"])),
               "selected_trial": entry.get("selected_trial"), "selected_budget_multiplier": None,
               "error": None, "scientific_complete": status in COMPLETE,
               "censored_verified_energy": None, "ground_energy_gap": None,
               "interaction_scale": float(Fraction(rule["scale"])),
               "meters": {name: 0 for name in METER_FIELDS}}
        all_paths = []
        for value in [entry.get("selected_trial"), *(entry.get("attempts") or [])]:
            if isinstance(value, dict):
                value = value.get("path") or value.get("trial_path") or value.get("selected_trial")
            if value and value not in all_paths:
                all_paths.append(value)
        for value in all_paths:
            try:
                trial = _trial_path(value, root)
                m = _read(trial / "manifest.json")
                for name, quantity in _meters(trial, m).items():
                    row["meters"][name] += quantity
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row["error"] = "Resource evidence problem: " + str(exc)
                row["scientific_complete"] = False
        selected = entry.get("selected_trial")
        if selected:
            try:
                trial = _trial_path(selected, root)
                m = _read(trial / "manifest.json")
                if _read(_inside(trial / "task.json", trial)) != task:
                    raise ValueError("Selected trial was run for a different task")
                row["selected_budget_multiplier"] = m.get("budget_multiplier")
                result = m.get("best_verified_result")
                if result is not None:
                    verified = verify_stored_result(result, task, trial)
                    row["proof_manifest_relative"] = verified["proof_manifest_relative"]
                    if row["scientific_complete"]:
                        row["energy_density"] = verified["energy_density"]
                    else:
                        row["censored_verified_energy"] = verified["energy_density"]
                elif status == "completed":
                    raise ValueError("completed requires at least one strictly verified state")
                if status == "scientific_failure" and result is not None:
                    raise ValueError("scientific_failure cannot contain a verified accepted state")
            except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
                row["error"] = "Selected evidence problem: " + str(exc)
                row["scientific_complete"] = False
                row["energy_density"] = None
        elif status in COMPLETE:
            row["error"] = "A scientific terminal requires a selected trial manifest"
            row["scientific_complete"] = False
        reference = task_reference(task, group["group"])
        try:
            metrics = task_metrics(row["energy_density"], baseline=row["baseline"],
                lower=row["algebraic_lower"], scale=row["interaction_scale"],
                group=group["group"], reference=reference)
            row.update(metrics)
            if not row["scientific_complete"]:
                row.update(pass_rate=None, improvement_rate=None, amplitude=None,
                    score=None, baseline_gain=None, score_complete=False,
                    score_status="missing_scientific_result")
        except ValueError as exc:
            # Scoring/reference trouble must not erase an already checked state.
            row.update(pass_rate=(1.0 if row["scientific_complete"] and row["energy_density"] is not None else None),
                       improvement_rate=None, baseline_gain=None, amplitude=None, score=None,
                       score_complete=False, score_status="scoring_error", scoring_error=str(exc))
        for name, value in row["meters"].items():
            total_meter[name] += value
        rows.append(row)
    groups = {}
    for name in GROUPS:
        selected = [r for r in rows if r["group"] == name]
        completed = [r for r in selected if r["scientific_complete"]]
        total_group_ids = [k for k, g in grouping["tasks"].items() if g["group"] == name]
        complete_selected = len(completed) == len(selected) and bool(selected)
        full = complete_selected and len(selected) == len(total_group_ids)
        scored = [r for r in completed if r.get("score_complete") is True]
        score_complete_selected = complete_selected and len(scored) == len(completed)
        full_score = full and score_complete_selected
        means = {key: (math.fsum(r[key] for r in completed) / len(completed)
                       if completed and all(r.get(key) is not None for r in completed) else None)
                 for key in ("pass_rate", "improvement_rate", "amplitude", "score")}
        groups[name] = {"label": grouping["groups"][name]["label"],
            "catalogue_tasks": len(total_group_ids), "selected_tasks": len(selected),
            "completed_tasks": len(completed), "coverage_selected": len(completed) / len(selected) if selected else None,
            "selection_complete": complete_selected, "full_group_complete": full,
            "score_selection_complete": score_complete_selected, "full_group_score_complete": full_score,
            "scored_tasks": len(scored),
            "score": means["score"] if full_score else None,
            "selected_subset_score": means["score"] if score_complete_selected else None,
            "reference_pending_task_ids": [r["task_id"] for r in selected if r.get("score_status") == "reference_pending"],
            "scoring_issue_task_ids": [r["task_id"] for r in completed if not r.get("score_complete")],
            "provisional_observed_mean": means, "missing_task_ids": [r["task_id"] for r in selected if not r["scientific_complete"]]}
    return {"schema": "seekgnomes-scores/v1.1", "created_utc": datetime.now(timezone.utc).isoformat(),
        "score_definition": "30*P+30*R+40*A; exact a=clamp(1-(E-e0)/s,0,1); generic a=clamp((B-E)/(B-L),0,1); R=baseline_gain>=0.01",
        "score_interpretation": "Exact-group A measures ground-energy error in interaction-scale units; generic A measures baseline-relative gain. Neither is ground-state fidelity.",
        "proof_reporting_scope": "Stored evidence/hash audit and independent float energy recomputation; no fresh kernel replay here. Untrusted community archives require independent proof verification.",
        "energy_mode": "approximate_float", "floating_point_certified": False,
        "resource_meter_scope": "Operational counters, including all listed attempts and failed requests; no billing claim. Formalist is uncapped.",
        "task_groups_sha256": digest(HERE / "task_groups.json"),
        "rules_sha256": digest(PROJECT / "config/benchmark50_score_rules.json"),
        "groups": groups, "rows": rows, "total_meters": total_meter}


def write_results(run_dir):
    root = Path(run_dir).resolve(strict=True)
    report = score_run(root)
    out = root / "results"
    if out.is_symlink():
        raise ValueError("Result directory may not be a symbolic link")
    out.mkdir(exist_ok=True)
    _write_text(out / "scores.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    fields = ["task_id", "name", "group", "subclass", "status", "scientific_complete", "energy_density",
              "baseline", "algebraic_lower", "pass_rate", "improvement_rate", "amplitude", "score",
              "baseline_gain", "interaction_scale", "ground_energy_reference_value", "ground_energy_gap",
              "normalized_ground_energy_gap", "score_status", "score_complete", "reference_status",
              "selected_budget_multiplier", "selected_trial", "censored_verified_energy", "error", "scoring_error"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: row.get(key) for key in fields} for row in report["rows"])
    _write_text(out / "tasks.csv", stream.getvalue())
    lines = ["# SeekGnomes测评结果", "", "主评分按严格可解组与一般变分组分别统计. 技术缺失不填0; 精确参考缺失时保留已核验态的P/R, 不以基线替代A, 不发布该组总分.", "",
             "| 分组 | 完成/本次选题 | 全题库组分数 | 完整选题子集分数 | 当前已完成题的暂定均分 |", "| --- | --- | --- | --- | --- |"]
    number = lambda value: "未发布" if value is None else f"{value:.4f}"
    for group in report["groups"].values():
        lines.append(f"| {group['label']} | {group['completed_tasks']}/{group['selected_tasks']} | {number(group['score'])} | {number(group['selected_subset_score'])} | {number(group['provisional_observed_mean']['score'])} |")
    lines += ["", "已完成科学结果的分项均值(不把缺失题或缺失参考填0):", "",
              "| 分组 | P | R | A | 评分待查题号 |", "| --- | --- | --- | --- | --- |"]
    for group in report["groups"].values():
        means = group["provisional_observed_mean"]
        issues = ", ".join(group["scoring_issue_task_ids"]) or "--"
        lines.append(f"| {group['label']} | {number(means['pass_rate'])} | {number(means['improvement_rate'])} | {number(means['amplitude'])} | {issues} |")
    lines += ["", "分数为30P+30R+40A. P为严格核验通过率, R沿用baseline_gain=clamp((B-E)/(B-L),0,1)至少0.01的比例. 精确组A平均clamp(1-(E-e0)/s,0,1), 一般组A平均baseline_gain. s是非恒等Pauli系数绝对值之和除以每晶胞qubit数, e0是已核对的精确解理论参考的数值. L仅为代数下界.",
              "", "能量由接口以float复算. Lean核验的是态合法性, 平移不变性和数值定义域义务, 不认证float误差. 本报告只复核已存证据与哈希, 未再次运行Lean内核. 接收不受信社区提交时需要重新核验源文件和证明.", "",
              "| 题号 | 模型 | 分组 | 状态 | 能量/每qubit | 精确参考e0 | E-e0 | (E-e0)/s | 分数 |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["rows"]:
        energy = "--" if row["energy_density"] is None else f"{row['energy_density']:.12g}"
        precise = lambda value: "--" if value is None else f"{value:.12g}"
        lines.append(f"| {row['task_id']} | {row['name']} | {row['group']} | {row['status']} | {energy} | {precise(row.get('ground_energy_reference_value'))} | {precise(row.get('raw_signed_gap'))} | {precise(row.get('normalized_ground_energy_gap'))} | {number(row['score'])} |")
        if row["error"]:
            lines.append("\n证据问题(" + row["task_id"] + "): " + row["error"].replace("\n", " ") + "\n")
        if row.get("scoring_error"):
            lines.append("\n评分参考问题(" + row["task_id"] + "): " + row["scoring_error"].replace("\n", " ") + "\n")
    lines += ["", "所有档位和技术重试的资源计数:", "", "```json", json.dumps(report["total_meters"], indent=2), "```", "",
              "原始对话, API响应, 工具轨迹, f.md/f.py/Lean证明和逐次核验记录保存在运行目录中. 资源计数是操作预算计量, 不是服务商账单."]
    _write_text(out / "REPORT.md", "\n".join(lines) + "\n")
    return report
