"""Read-only comparison of a frozen local control and hosted-web campaign.

Run with Environment/Python/run.sh and explicit --control, --web and fresh --out
directories. No API calls, model code, proof jobs or campaign mutations occur.
Prices describe observed token usage and observed search actions, not a bill.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
EUREKA = PROJECT.parents[1]
MODEL = "gpt-6.1-terra"
SCIENTIFIC_TERMINALS = frozenset({
    "completed", "final_submission_not_verified", "turn_budget_exhausted",
    "wall_budget_exhausted", "context_budget_exhausted", "output_budget_exhausted",
    "input_budget_exhausted",
})
# Additions, not permission to alter any source frozen by the control.
ALLOWED_WEB_SOURCES = frozenset({
    "revisions/hybrid_web_v1/run_gpt_campaign.py",
    "revisions/hybrid_web_v1/run_gpt_trial.py",
    "revisions/hybrid_web_v1/responses_client.py",
    "revisions/hybrid_web_v1/agent.py",
    "revisions/hybrid_web_v1/system_prompt.md",
    "revisions/hybrid_web_v1/GPT61_WEB_FIVE_PROTOCOL.md",
})
PRICING = {
    "model": MODEL, "currency": "USD", "unit": "per_million_tokens",
    "ordinary_input": 1.0, "cached_input": 0.05,
    "cache_write_30m": 1.25, "cache_write_12h": 2.5, "output": 5.0,
    "long_input_threshold_exclusive": 272000,
    "long_input_multiplier": 2.0, "long_output_multiplier": 1.5,
    "per_observed_search_action": 0.01,
    "sources": ["https://developers.openai.com/api/docs/pricing",
                "https://developers.openai.com/api/docs/models/gpt-6.1-terra"],
    "as_of": "2026-11-15",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any, *, compact: bool = True) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      **({"separators": (",", ":")} if compact else {}))


def finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def token_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def estimate_token_price(raw: dict | None) -> dict:
    """Price one actual response. Output details are subsets, never added twice.

    A missing optional write field means no *observed* cache writes. A positive
    write total with no complete TTL split cannot be priced unambiguously.
    """
    unknown: list[str] = []
    result: dict[str, Any] = {"usd": None, "unknown": unknown, "tokens": None}
    if not isinstance(raw, dict):
        unknown.append("raw_response_missing")
        return result
    result["service_tier"] = raw.get("service_tier")
    if raw.get("service_tier") != "default":
        unknown.append("service_tier_not_explicit_default")
    if raw.get("model") != MODEL:
        unknown.append("price_model_not_exact_gpt_6_1_terra")
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        unknown.append("usage_missing")
        return result
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if not token_count(inp) or not token_count(out):
        unknown.append("input_or_output_usage_missing_or_invalid")
        return result
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict) or not token_count(details.get("cached_tokens")):
        unknown.append("cached_input_usage_missing_or_invalid")
        return result
    cached = details["cached_tokens"]
    writes = details.get("cache_write_tokens", 0)
    ttl = details.get("cache_write_tokens_details", {})
    if not token_count(writes) or not isinstance(ttl, dict):
        unknown.append("cache_write_usage_invalid")
        return result
    w30, w12 = ttl.get("ttl_30m_tokens", 0), ttl.get("ttl_12h_tokens", 0)
    if (not token_count(w30) or not token_count(w12) or w30 + w12 != writes
            or cached + writes > inp):
        unknown.append("cache_write_ttl_split_or_input_partition_invalid")
        return result
    ordinary = inp - cached - writes
    long = inp > PRICING["long_input_threshold_exclusive"]
    im = PRICING["long_input_multiplier"] if long else 1.0
    om = PRICING["long_output_multiplier"] if long else 1.0
    result.update(tokens={"input": inp, "output": out, "ordinary_input": ordinary,
                          "cached_input": cached, "cache_write_30m": w30,
                          "cache_write_12h": w12},
                  long_context_request=long, input_multiplier=im, output_multiplier=om)
    if not unknown:
        result["usd"] = (im * (ordinary * PRICING["ordinary_input"]
                               + cached * PRICING["cached_input"]
                               + w30 * PRICING["cache_write_30m"]
                               + w12 * PRICING["cache_write_12h"])
                         + om * out * PRICING["output"]) / 1_000_000
    return result


class Evidence:
    """Hash the exact bytes used; retain missing/unreadable paths as evidence."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}

    def read_json(self, path: Path) -> dict | None:
        key = str(path)
        try:
            data = path.read_bytes()
            self.files[key] = {"path": key, "sha256": sha256(data), "bytes": len(data)}
            value = json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("Nonfinite JSON constant: " + value)))
            if not isinstance(value, dict):
                raise ValueError("JSON root must be an object")
            return value
        except (OSError, ValueError, UnicodeError) as error:
            self.files.setdefault(key, {"path": key, "sha256": None})["error"] = type(error).__name__
            return None


def local_path(root: Path, value: Any, fallback: Path) -> Path:
    """Do not follow an artifact's path to an unrelated host file."""
    path = Path(value) if isinstance(value, str) and value else fallback
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError("Campaign artifact points outside its campaign: " + str(path))
    return path


def check_record(name: str, control: Any, web: Any) -> dict:
    status = "unknown" if control is None or web is None else "passed" if control == web else "failed"
    return {"check": name, "status": status, "control": control, "web": web}


def index_rows(rows: Any) -> dict[str, dict]:
    if not isinstance(rows, list):
        return {}
    result = {}
    for row in rows:
        if isinstance(row, dict) and row.get("task_id") is not None:
            task_id = str(row["task_id"]).zfill(2)
            if task_id in result:
                raise ValueError("Duplicate task row: " + task_id)
            result[task_id] = row
    return result


def web_calls(raw: dict | None, path: Path) -> tuple[list[dict], bool]:
    """Only raw_response.output belongs to this response; never count replay."""
    output = raw.get("output") if isinstance(raw, dict) else None
    if not isinstance(output, list):
        return [], False
    calls = []
    for index, item in enumerate(output):
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            action = item.get("action")
            calls.append({"api_file": str(path), "json_pointer": f"/raw_response/output/{index}",
                          "call_id": item.get("id"), "status": item.get("status"),
                          "action_type": action.get("type") if isinstance(action, dict) else None,
                          "action": action})
    return calls, True


def api_metrics(trial_dir: Path, manifest: dict | None, evidence: Evidence) -> dict:
    directories = [trial_dir / name for name in ("api", "API") if (trial_dir / name).is_dir()]
    paths = sorted(path for directory in directories for path in directory.glob("*.json")
                   if not path.name.endswith(".request.json"))
    rows, calls = [], []
    unknown_outputs = 0
    for path in paths:
        record = evidence.read_json(path)
        raw = record.get("raw_response") if isinstance(record, dict) else None
        raw = raw if isinstance(raw, dict) else None
        observed_calls, output_known = web_calls(raw, path)
        calls.extend(observed_calls)
        unknown_outputs += not output_known
        price = estimate_token_price(raw)
        usage = raw.get("usage") if raw else None
        rows.append({"path": str(path), "sha256": evidence.files[str(path)].get("sha256"),
                     "raw_response_id": raw.get("id") if raw else None,
                     "response_status": raw.get("status") if raw else None,
                     "http_status": record.get("http_status") if record else None,
                     "logical_step": record.get("logical_step") if record else None,
                     "transport_attempt": record.get("transport_attempt") if record else None,
                     "latency_seconds": record.get("latency_seconds") if record else None,
                     "usage": usage, "token_price_estimate": price,
                     "web_output_known": output_known, "web_item_count": len(observed_calls)})
    available = bool(directories) and (bool(paths) or manifest is not None)
    action_counts = Counter(call["action_type"] or "unknown" for call in calls)
    statuses: dict[str, Counter] = {}
    for call in calls:
        statuses.setdefault(call["action_type"] or "unknown", Counter())[call["status"] or "unknown"] += 1
    totals: dict[str, int | None] = {}
    for field in ("input_tokens", "output_tokens"):
        values = [row["usage"].get(field) for row in rows if isinstance(row["usage"], dict)]
        known = [value for value in values if token_count(value)]
        totals[field] = sum(known) if known else None
        totals[field + "_missing_records"] = len(rows) - len(known) if available else None
    prices = [row["token_price_estimate"]["usd"] for row in rows]
    known_prices = [price for price in prices if price is not None]
    search_count = action_counts["search"] if available else None
    unknown_actions = sum(call["action_type"] not in ("search", "open_page", "find_in_page") for call in calls)
    search_price = search_count * PRICING["per_observed_search_action"] if search_count is not None else None
    known_subtotal = sum(known_prices) if known_prices else None
    all_priced = bool(rows) and len(known_prices) == len(rows) and unknown_outputs == 0 and unknown_actions == 0
    return {
        "api_record_count": len(rows) if available else None,
        "raw_response_count": sum(row["raw_response_id"] is not None for row in rows) if available else None,
        "tokens_observed": totals,
        "manifest_turns": manifest.get("turns") if manifest else None,
        "input_tokens_charged": manifest.get("input_tokens_charged") if manifest else None,
        "output_tokens_charged": manifest.get("output_tokens_charged") if manifest else None,
        "verifier_calls": manifest.get("verifier_calls") if manifest else None,
        "elapsed_seconds": manifest.get("elapsed_seconds") if manifest else None,
        "web_item_count": len(calls) if available else None,
        "web_action_counts": dict(action_counts) if available else None,
        "web_action_status_counts": {key: dict(value) for key, value in statuses.items()} if available else None,
        "search_action_count": search_count,
        "web_output_unknown_records": unknown_outputs if available else None,
        "web_unknown_action_items": unknown_actions if available else None,
        "web_calls": calls, "api_records": rows,
        "price": {"known_token_subtotal_usd": known_subtotal,
                  "unpriced_api_records": len(rows) - len(known_prices) if available else None,
                  "observed_search_action_estimate_usd": search_price,
                  "observed_token_plus_search_estimate_usd": known_subtotal + search_price if all_priced else None,
                  "complete_for_observed_records": all_priced,
                  "billing_total_usd": None,
                  "search_content_tokens_complete_in_usage": None,
                  "scope": "Observed token usage plus observed search actions; no manual extra input-token charge. Not an account bill. Unobserved/disconnected usage and search-content accounting remain unknown. Calibration excluded."},
    }


def result_value(result: Any, baseline: Any) -> dict | None:
    if (not isinstance(result, dict) or result.get("proof_verified") is not True
            or not finite_number(result.get("energy_density"))):
        return None
    energy = float(result["energy_density"])
    gain = baseline - energy if finite_number(baseline) else None
    return {**result, "energy_density": energy, "energy_certified": False,
            "energy_normalization": "per_qubit", "ground_state_proven": False,
            "absolute_baseline_improvement": gain,
            "improvement_is_certified": False}


def load_campaign(root: Path, evidence: Evidence) -> dict:
    root = root.resolve(strict=True)
    plan = evidence.read_json(root / "plan.json") or {}
    queue = evidence.read_json(root / "queue.json") or {}
    summary = evidence.read_json(root / "summary.json") or {}
    queue_rows, summary_rows = index_rows(queue.get("tasks")), index_rows(summary.get("rows"))
    definitions = index_rows(plan.get("task_definitions"))
    ids = sorted(set(str(value).zfill(2) for value in plan.get("task_ids", []))
                 | set(queue_rows) | set(summary_rows) | set(definitions))
    rows = {}
    for task_id in ids:
        qrow, srow, definition = queue_rows.get(task_id, {}), summary_rows.get(task_id, {}), definitions.get(task_id, {})
        trial_dir = local_path(root, qrow.get("trial_dir") or srow.get("trial_dir"), root / f"gpt-task{task_id}-trial1")
        manifest_path = local_path(root, qrow.get("manifest") or srow.get("manifest"), trial_dir / "manifest.json")
        manifest = evidence.read_json(manifest_path)
        task_path = local_path(root, definition.get("task_file") or qrow.get("task_file"), root / "tasks" / f"{task_id}.json")
        task = evidence.read_json(task_path)
        runtime_task = evidence.read_json(trial_dir / "task.json")
        references = evidence.read_json(trial_dir / "references.json")
        reference_hashes = ({name: sha256(content.encode()) for name, content in references.items()
                             if isinstance(content, str)} if references is not None else None)
        baseline = srow.get("baseline_energy")
        scientific = (srow.get("category") == "scientific_terminal" and manifest is not None
                      and manifest.get("status") in SCIENTIFIC_TERMINALS)
        best, final = result_value(srow.get("best"), baseline), result_value(srow.get("final"), baseline)
        proof_artifacts = []
        for value in (best, final):
            if value:
                for key in ("proof_manifest", "energy_report"):
                    if value.get(key):
                        artifact_path = local_path(root, value[key], trial_dir / "missing")
                        evidence.read_json(artifact_path)
                        proof_artifacts.append({"role": key, **evidence.files[str(artifact_path)]})
        checks = [check_record(f"task{task_id}.manifest_model", plan.get("model"), manifest.get("model") if manifest else None),
                  check_record(f"task{task_id}.manifest_runtime_hash", definition.get("runtime_task_sha256"), manifest.get("task_sha256") if manifest else None),
                  check_record(f"task{task_id}.task_file_hash", definition.get("task_file_sha256"), evidence.files[str(task_path)].get("sha256")),
                  check_record(f"task{task_id}.task_canonical_hash", definition.get("task_canonical_sha256"), task.get("canonical_sha256") if task else None),
                  check_record(f"task{task_id}.canonical_recompute", task.get("canonical_sha256") if task else None,
                               sha256(canonical({key: value for key, value in task.items() if key != "canonical_sha256"}).encode()) if task else None),
                  check_record(f"task{task_id}.runtime_task_recompute", definition.get("runtime_task_sha256"),
                               sha256(canonical(runtime_task, compact=False).encode()) if runtime_task else None),
                  check_record(f"task{task_id}.summary_manifest_hash", srow.get("manifest_sha256"), evidence.files[str(manifest_path)].get("sha256"))]
        if manifest is not None:
            for endpoint, manifest_key in (("best", "best_verified_result"), ("final", "verified_result")):
                original = manifest.get(manifest_key)
                accepted = (isinstance(original, dict) and original.get("ok") is True
                            and original.get("verified") is True and original.get("proof_verified") is True
                            and finite_number(original.get("energy_density")))
                expected = original["energy_density"] if accepted else None
                observed = (best if endpoint == "best" else final)
                observed = observed["energy_density"] if observed else None
                # Here two absent endpoints agree; check_record treats missing
                # bindings as unknown, so handle this known absence explicitly.
                checks.append({"check": f"task{task_id}.summary_manifest_{endpoint}_energy",
                               "status": "passed" if expected == observed else "failed",
                               "manifest": expected, "summary": observed})
        if any(check["status"] == "failed" for check in checks):
            scientific = False
        rows[task_id] = {
            "task_id": task_id, "name": srow.get("name") or qrow.get("name"),
            "coverage": "scientific_terminal" if scientific else "missing",
            "runtime_status": manifest.get("status") if manifest else None,
            "category": srow.get("category"), "queue_state": qrow.get("state"),
            "baseline_energy": baseline, "best": best, "final": final,
            "best_verified": best is not None if scientific else None,
            "final_verified": final is not None if scientific else None,
            "partial_best_censored": best is not None and not scientific,
            "definition": definition, "reference_content_sha256": reference_hashes,
            "effective_settings": {key: manifest.get(key) if manifest else None for key in (
                "limits", "runtime_session_policy", "transport_config", "recovery_policy", "tool_protocol", "base_url")},
            "trial_dir": str(trial_dir), "manifest": str(manifest_path),
            "checks": checks, "proof_artifacts": proof_artifacts,
            "metrics": api_metrics(trial_dir, manifest, evidence),
        }
    return {"root": str(root), "plan": plan, "queue": queue, "summary": summary, "rows": rows}


def consistency_checks(control: dict, web: dict, evidence: Evidence) -> list[dict]:
    checks = []
    for field in ("model", "provider", "api_base_url", "api_protocol", "compute_location",
                  "task_ids", "trials_per_task", "parallel", "limits", "recovery_policy",
                  "between_trial_isolation", "test_worker_override"):
        checks.append(check_record("plan." + field, control["plan"].get(field), web["plan"].get(field)))
    checks.append(check_record("exact_price_model", MODEL, web["plan"].get("model")))
    checks.append(check_record("n_per_task", 1, web["plan"].get("trials_per_task")))
    checks.append(check_record("network_control_disabled", False, control["plan"].get("network_tools_exposed_to_model")))
    checks.append(check_record("network_web_enabled", True, web["plan"].get("network_tools_exposed_to_model")))
    old, new = control["plan"].get("source_sha256"), web["plan"].get("source_sha256")
    if not isinstance(old, dict) or not isinstance(new, dict):
        checks.append(check_record("source_bindings", old, new))
    else:
        for path in sorted(old):
            checks.append(check_record("frozen_source." + path, old[path], new.get(path)))
        added = sorted(set(new) - set(old))
        checks.append({"check": "web_source_addition_whitelist", "status": "passed" if set(added) <= ALLOWED_WEB_SOURCES else "failed",
                       "additions": added, "allowed": sorted(ALLOWED_WEB_SOURCES)})
        checks.append(check_record("core_lock", old.get("HYBRID_PROOF_SPEC.lock.json"), new.get("HYBRID_PROOF_SPEC.lock.json")))
        for name, expected in sorted(new.items()):
            path = (EUREKA if name.startswith("Environment/") else PROJECT) / name
            resolved = path.resolve()
            actual = sha256(resolved.read_bytes()) if resolved.is_relative_to(EUREKA) and resolved.is_file() else None
            if actual is not None:
                evidence.files[str(resolved)] = {"path": str(resolved), "sha256": actual, "role": "live_bound_source"}
            checks.append(check_record("live_source." + name, expected, actual))
    for campaign in (control, web):
        label, root = campaign["plan"].get("experiment_id", campaign["root"]), campaign["root"]
        plan_hash = evidence.files[str(Path(root) / "plan.json")].get("sha256")
        for kind in ("queue", "summary"):
            checks.append(check_record(f"{label}.{kind}.plan_hash", plan_hash, campaign[kind].get("plan_sha256")))
        drift = campaign["summary"].get("source_drift")
        checks.append(check_record(f"{label}.source_drift", [], drift))
        for row in campaign["rows"].values():
            checks.extend({**check, "campaign": label} for check in row["checks"])
    for task_id in sorted(set(control["rows"]) | set(web["rows"])):
        c, w = control["rows"].get(task_id, {}), web["rows"].get(task_id, {})
        for field in ("task_file_sha256", "task_canonical_sha256", "runtime_task_sha256"):
            checks.append(check_record(f"task{task_id}.paired.{field}", c.get("definition", {}).get(field), w.get("definition", {}).get(field)))
        checks.append(check_record(f"task{task_id}.reference_content_hashes", c.get("reference_content_sha256"), w.get("reference_content_sha256")))
        checks.append(check_record(f"task{task_id}.baseline", c.get("baseline_energy"), w.get("baseline_energy")))
        for field in ("limits", "runtime_session_policy", "transport_config", "recovery_policy", "tool_protocol", "base_url"):
            checks.append(check_record(f"task{task_id}.effective.{field}", c.get("effective_settings", {}).get(field),
                                       w.get("effective_settings", {}).get(field)))
    return checks


def aggregate(campaign: dict) -> dict:
    rows = list(campaign["rows"].values())
    completed = [row for row in rows if row["coverage"] == "scientific_terminal"]
    result = {"requested_trials": len(rows), "scientific_terminal_count": len(completed),
              "missing_count": len(rows) - len(completed), "replicates_per_task": 1,
              "complete_coverage": bool(rows) and len(completed) == len(rows),
              "confidence_interval": None}
    for key in ("best_verified", "final_verified"):
        count = sum(row[key] is True for row in completed) if completed else None
        result[key + "_count"] = count
        result[key + "_rate_among_scientific_terminals"] = count / len(completed) if result["complete_coverage"] else None
    for key in ("api_record_count", "verifier_calls", "elapsed_seconds", "search_action_count"):
        values = [row["metrics"][key] for row in rows]
        known = [value for value in values if finite_number(value)]
        result[key + "_observed_sum"] = sum(known) if known else None
        result[key + "_missing_trials"] = len(rows) - len(known)
    for token in ("input_tokens", "output_tokens"):
        values = [row["metrics"]["tokens_observed"][token] for row in rows]
        known = [value for value in values if token_count(value)]
        result[token + "_observed_sum"] = sum(known) if known else None
        missing_records = [row["metrics"]["tokens_observed"][token + "_missing_records"] for row in rows]
        result[token + "_unknown_records_observed_sum"] = sum(value for value in missing_records if value is not None)
        result[token + "_missing_trials"] = len(rows) - len(known)
    for key in ("known_token_subtotal_usd", "observed_search_action_estimate_usd", "observed_token_plus_search_estimate_usd"):
        values = [row["metrics"]["price"][key] for row in rows]
        known = [value for value in values if finite_number(value)]
        result[key + "_observed_sum"] = sum(known) if known else None
        result[key + "_missing_trials"] = len(rows) - len(known)
    result["billing_total_usd"] = None
    return result


def compare_campaigns(control_path: Path, web_path: Path) -> dict:
    evidence = Evidence()
    control, web = load_campaign(control_path, evidence), load_campaign(web_path, evidence)
    checks = consistency_checks(control, web, evidence)
    failed = [check for check in checks if check["status"] == "failed"]
    unknown = [check for check in checks if check["status"] == "unknown"]
    status = "failed" if failed else "unknown" if unknown else "passed"
    rows = []
    for task_id in sorted(set(control["rows"]) | set(web["rows"])):
        c, w = control["rows"].get(task_id), web["rows"].get(task_id)
        pair = {"task_id": task_id, "name": (c or w).get("name"), "control": c, "web": w,
                "scientific_pair_observed": bool(c and w and c["coverage"] == w["coverage"] == "scientific_terminal"),
                "conditions_verified_unchanged_except_web": status == "passed"}
        for endpoint in ("best", "final"):
            observed = pair["scientific_pair_observed"] and c.get(endpoint) is not None and w.get(endpoint) is not None
            pair[endpoint + "_energy_delta_web_minus_control"] = w[endpoint]["energy_density"] - c[endpoint]["energy_density"] if observed else None
            cg = c[endpoint].get("numerical_normalized_gain") if observed else None
            wg = w[endpoint].get("numerical_normalized_gain") if observed else None
            pair[endpoint + "_normalized_gain_delta_web_minus_control"] = wg - cg if finite_number(cg) and finite_number(wg) else None
        rows.append(pair)
    return {
        "schema": "hybrid-hosted-web-paired-comparison/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "control": {"root": control["root"], "experiment_id": control["plan"].get("experiment_id"), "aggregate": aggregate(control)},
        "web": {"root": web["root"], "experiment_id": web["plan"].get("experiment_id"), "aggregate": aggregate(web)},
        "consistency": {"status": status, "failed_count": len(failed), "unknown_count": len(unknown), "checks": checks},
        "rows": rows, "pricing": PRICING, "evidence": list(evidence.files.values()),
        "interpretation": [
            "Each task has one independent control and one independent web trial; no confidence interval or stable causal-effect claim is justified.",
            "The endpoint is host-computed floating-point energy per qubit of a kernel-verified legal cell-translation-invariant state. Rounding is not certified and ground-state optimality is not proven.",
            "Technical/incomplete trials are missing coverage, never capability zero. Any verified partial best is retained as censored evidence and excluded from paired energy deltas.",
            "A negative energy delta means the observed web energy is lower. Absolute baseline improvement is B-E; saved numerical_normalized_gain uses max(0,(B-E)/frozen_scale), not fraction of true ground-state progress. Overall verification rates are withheld until coverage is complete.",
            "Prices apply public default-tier rates per actual response, with >272000 input tokens multiplying all input/cache rates by 2 and output by 1.5. Search/open/find statuses are separate; only observed search actions receive the $0.01 action estimate.",
            "Observed token-plus-search estimates are not account bills. Search-content token completeness cannot be established from this local schema, and no extra input tokens are manually added. Calibration and unobserved disconnected usage are excluded or unknown.",
        ],
    }


def format_value(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "通过" if value else "未通过"
    return f"{value:.10g}" if finite_number(value) else str(value).replace("|", "\\|").replace("\n", " ")


def markdown_report(report: dict) -> str:
    lines = ["# GPT-6.1 Terra联网五题对照", "", f"对照: `{report['control']['root']}`.",
             f"联网: `{report['web']['root']}`.", "",
             f"冻结条件检查: `{report['consistency']['status']}`. 不一致{report['consistency']['failed_count']}项, 未知{report['consistency']['unknown_count']}项.",
             "", "每题各1次独立试验. 能量为通过内核核验的合法晶胞平移不变态的每qubit宿主浮点值, 无认证舍入误差界, 无真实基态最优性证明. 技术缺失保留为missing. 负能量差表示本次联网结果更低.",
             "", "| 题号 | 对照覆盖 | 联网覆盖 | 对照best | 联网best | best差(web-control) | 对照final | 联网final | final差 |", "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in report["rows"]:
        c, w = row["control"] or {}, row["web"] or {}
        get = lambda side, key: side.get(key, {}).get("energy_density") if side.get(key) else None
        values = [row["task_id"], c.get("coverage"), w.get("coverage"), get(c, "best"), get(w, "best"), row["best_energy_delta_web_minus_control"], get(c, "final"), get(w, "final"), row["final_energy_delta_web_minus_control"]]
        lines.append("| " + " | ".join(format_value(value) for value in values) + " |")
    lines += ["", "| 题号/条件 | best/final核验 | best/final基线改善B-E | 原best numerical_normalized_gain | API记录/核验 | input/output已观测token | search/open/find | 耗时s | 已观测费用估算USD |", "|---|---|---|---:|---|---|---|---:|---:|"]
    for row in report["rows"]:
        for label in ("control", "web"):
            side = row[label]
            if side is None:
                continue
            m, best = side["metrics"], side.get("best") or {}
            actions = m["web_action_counts"]
            counts = "/".join(str(actions.get(key, 0)) for key in ("search", "open_page", "find_in_page")) if actions is not None else "missing"
            values = [f"{row['task_id']}/{label}", f"{format_value(side['best_verified'])}/{format_value(side['final_verified'])}",
                      f"{format_value(best.get('absolute_baseline_improvement'))}/{format_value((side.get('final') or {}).get('absolute_baseline_improvement'))}", best.get("numerical_normalized_gain"),
                      f"{format_value(m['api_record_count'])}/{format_value(m['verifier_calls'])}",
                      f"{format_value(m['tokens_observed']['input_tokens'])}/{format_value(m['tokens_observed']['output_tokens'])}",
                      counts, m["elapsed_seconds"], m["price"]["observed_token_plus_search_estimate_usd"]]
            lines.append("| " + " | ".join(format_value(value) for value in values) + " |")
    lines += ["", "基线改善量为B-E, 原numerical_normalized_gain为max(0,(B-E)/scale), 后者只保留供核对, 不另建本轮正式综合分数, 也不是真实基态完成比例. 缺失技术试次若保留best, 该值仅为删失观测, 不纳入配对能量差. 整体核验率待覆盖完整后才提供.", ""]
    for label in ("control", "web"):
        a = report[label]["aggregate"]
        lines.append(f"{label}: 科学终态{a['scientific_terminal_count']}/{a['requested_trials']}, 技术/未完成缺失{a['missing_count']}. 科学终态中best核验率{format_value(a['best_verified_rate_among_scientific_terminals'])}, final核验率{format_value(a['final_verified_rate_among_scientific_terminals'])}. 已知token费用小计USD {format_value(a['known_token_subtotal_usd_observed_sum'])}, 已观测search动作估算USD {format_value(a['observed_search_action_estimate_usd_observed_sum'])}.")
        lines.append("")
    lines += ["价格仅按已观测usage和本期raw_response.output中的search动作估算. 每次search估算USD0.01, open_page/find_in_page不额外加费. 非default service_tier, usage缺失或cache TTL拆分不完整时保留未知. 逐请求input超过272000时全部input/cache费率乘2, output乘1.5. 无法由本地schema确认搜索内容token是否完全包含在usage中, 不人工重复加入input. 本报告不是账单, 不含单独联网校准, 不可见断线消耗也不能填0.",
              "", "价格来源: [官方定价](https://developers.openai.com/api/docs/pricing), [GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra). 定价快照: 2026-11-15.",
              "", "每题只有一对试次, 不给重复试验置信区间, 不把观测差异解释为稳健的联网因果效应. 完整逐响应价格, 搜索动作及状态, 来源URL, task/source/reference绑定检查和读取文件SHA-256均在comparison.json中."]
    problems = [check for check in report["consistency"]["checks"] if check["status"] != "passed"]
    if problems:
        lines += ["", "冻结条件中的不一致或未知:", ""]
        lines.extend(f"- `{check['check']}`: {check['status']}." for check in problems)
    return "\n".join(lines) + "\n"


def write_report(report: dict, out: Path) -> Path:
    out = Path(os.path.abspath(out))
    if not out.is_relative_to(EUREKA) or out.parent.resolve(strict=True) != out.parent:
        raise ValueError("Output parent must be non-aliased and inside Eureka")
    for label in ("control", "web"):
        if out.is_relative_to(Path(report[label]["root"])):
            raise ValueError("Comparison output cannot be inside an input campaign")
    out.mkdir(mode=0o700, exist_ok=False)
    for name, content in (("comparison.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"),
                          ("comparison.md", markdown_report(report))):
        with (out / name).open("x") as stream:
            stream.write(content)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--web", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = compare_campaigns(args.control, args.web)
    out = write_report(report, args.out)
    print(json.dumps({"comparison": str(out / "comparison.json"), "report": str(out / "comparison.md"),
                      "consistency": report["consistency"]["status"]}, ensure_ascii=False))
    return 2 if report["consistency"]["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
