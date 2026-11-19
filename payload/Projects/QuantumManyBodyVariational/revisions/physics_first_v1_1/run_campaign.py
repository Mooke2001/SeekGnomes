"""Supervise five independent GPT physics-first, then proof trials.

Only the trusted API launcher receives credentials. This process records child
lifetimes, immutable task/source bindings and the distinction between scientific
terminal results and infrastructure gaps. It never executes model source.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[2]
EUREKA = PROJECT.parents[1]
sys.path.insert(0, str(PROJECT))
from src.benchmark50_models import load_task

WRAPPER = PROJECT / "revisions/physics_first_v1_1/run_trial.py"
SCIENTIFIC_TERMINALS = frozenset({
    "completed", "final_submission_not_verified", "turn_budget_exhausted",
    "wall_budget_exhausted", "context_budget_exhausted", "output_budget_exhausted",
    "input_budget_exhausted",
})
LIMITS = {
    "max_total_output_tokens": 128000, "max_total_input_tokens": 2000000,
    "max_api_responses": 64, "max_verifier_calls": 64,
    "max_local_numeric_cpu_seconds_reserved": 3600, "wall_seconds": 14400,
    "request_seconds": 600, "connect_seconds": 60, "checker_seconds": 300,
    "normal_request_tokens": 16000, "normal_output_safety_margin": 4096,
    "closing_request_tokens": 8192, "closing_total_tokens": 16000,
    "closing_max_responses": 3,
}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="host-checkpoint-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def source_bindings() -> dict[str, str]:
    lock = json.loads((PROJECT / "HYBRID_PROOF_SPEC.lock.json").read_text())
    bindings = dict(lock["source_sha256"])
    required = [
        "HYBRID_PROOF_SPEC.lock.json", "config/benchmark50_tasks.json",
        "config/benchmark50_score_rules.json", "src/benchmark50_models.py",
        "src/relay_session.py", "revisions/hybrid_agent_v1/run_gpt_campaign.py",
        "revisions/hybrid_agent_v1/run_gpt_trial.py",
        "revisions/hybrid_agent_v1/responses_client.py",
        "revisions/hybrid_agent_v1/run_trial.py", "revisions/hybrid_agent_v1/local_numeric.py",
        "revisions/hybrid_agent_v1/system_prompt.md",
        "revisions/hybrid_agent_v1/runtime/__init__.py",
        "revisions/hybrid_agent_v1/runtime/api_client.py",
        "revisions/hybrid_agent_v1/runtime/benchmark50_agent.py",
        "revisions/hybrid_web_v1/run_gpt_campaign.py",
        "revisions/hybrid_web_v1/run_gpt_trial.py", "revisions/hybrid_web_v1/responses_client.py",
        "revisions/hybrid_web_v1/agent.py", "revisions/hybrid_web_v1/system_prompt.md",
        "revisions/hybrid_web_v1/GPT61_WEB_FIVE_PROTOCOL.md",
        "revisions/physics_first_v1/run_campaign.py", "revisions/physics_first_v1/run_trial.py",
        "revisions/physics_first_v1/exploration.py", "revisions/physics_first_v1/energy.py",
        "revisions/physics_first_v1/references.py", "revisions/physics_first_v1/physics_prompt.md",
        "revisions/physics_first_v1/proof_prompt.md", "revisions/physics_first_v1/PROTOCOL.md",
        "revisions/physics_first_v1_1/run_campaign.py", "revisions/physics_first_v1_1/run_trial.py",
        "revisions/physics_first_v1_1/exploration.py", "revisions/physics_first_v1_1/energy.py",
        "revisions/physics_first_v1_1/references.py", "revisions/physics_first_v1_1/physics_prompt.md",
        "revisions/physics_first_v1_1/proof_prompt.md", "revisions/physics_first_v1_1/PROTOCOL.md",
        "revisions/physics_first_v1_1/compiler.py", "revisions/physics_first_v1_1/verifier.py",
        "revisions/physics_first_v1_1/callbacks.py", "revisions/physics_first_v1_1/PROOF_SPEC.lock.json",
        "Environment/API/run.sh", "Environment/API/credentials.py",
        "Environment/API/providers.json",
        "Environment/Python/run.sh", "Environment/Python/sandbox.sh",
    ]
    for name, expected in bindings.items():
        if digest(PROJECT / name) != expected:
            raise ValueError("Frozen hybrid source mismatch: " + name)
    for name in required:
        path = EUREKA / name if name.startswith("Environment/") else PROJECT / name
        if path.resolve(strict=True) != path:
            raise ValueError("Source binding crosses a path alias: " + name)
        bindings[name] = digest(path)
    return bindings


def source_drift(bindings: dict[str, str]) -> list[str]:
    differences = []
    for name, expected in bindings.items():
        path = EUREKA / name if name.startswith("Environment/") else PROJECT / name
        try:
            if path.resolve(strict=True) != path or digest(path) != expected:
                differences.append(name)
        except OSError:
            differences.append(name)
    return differences


def normalize_ids(task_ids) -> list[str]:
    if not isinstance(task_ids, (list, tuple)) or len(task_ids) != 5:
        raise ValueError("Exactly five task IDs must be supplied explicitly")
    ids = []
    for item in task_ids:
        if isinstance(item, bool) or not str(item).isdigit() or not 1 <= int(item) <= 50:
            raise ValueError("Task IDs must be integers from 1 to 50")
        ids.append(f"{int(item):02d}")
    if len(set(ids)) != 5:
        raise ValueError("Five task IDs must be distinct")
    return ids


def validate_endpoint(endpoint: str) -> None:
    """Keep the GPT provider on the configured official v1 endpoint."""
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.hostname != "api.openai.com"
            or parsed.port not in (None, 443) or parsed.path.rstrip("/") != "/v1"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(character.isspace() for character in endpoint)):
        raise ValueError("The openai provider must use https://api.openai.com/v1")


def standard_command(entry: dict, plan: dict) -> list[str]:
    return [
        "bash", str(EUREKA / "Environment/API/run.sh"), "openai", "--",
        str(EUREKA / "Environment/Python/run.sh"), str(WRAPPER),
        "--task-id", str(int(entry["task_id"])), "--model", plan["model"],
        "--experiment-id", plan["experiment_id"], "--out", entry["trial_dir"],
    ]


def classify_manifest(manifest: dict | None) -> str:
    if not isinstance(manifest, dict):
        return "technical_gap"
    if manifest.get("status") in SCIENTIFIC_TERMINALS:
        return "scientific_terminal"
    return "technical_gap"


def valid_result(result) -> bool:
    if not isinstance(result, dict):
        return False
    value = result.get("energy_density")
    return (result.get("ok") is True and result.get("verified") is True
            and result.get("proof_verified") is True and type(value) in (int, float)
            and math.isfinite(value))


def result_view(result, baseline: float, scale: float) -> dict | None:
    if not valid_result(result):
        return None
    energy = float(result["energy_density"])
    fields = ("source_sha256", "proof_sha256", "candidate_sha256", "proof_manifest",
              "energy_report", "certificate", "task_physics_sha256")
    return {
        "energy_density": energy, "energy_normalization": "per_qubit",
        "proof_verified": True, "energy_certified": False,
        "numerical_normalized_gain": max(0.0, (baseline - energy) / scale),
        "improvement_is_certified": False,
        **{name: result[name] for name in fields if name in result},
    }


def load_manifest(path: Path) -> tuple[dict | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            return None, "manifest_missing_or_unsafe"
        if path.stat().st_size > 32 * 1024**2:
            return None, "manifest_oversized"
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            return None, "manifest_not_object"
        return value, None
    except (OSError, ValueError, UnicodeError):
        return None, "manifest_unreadable"


def account_refused(manifest: dict | None) -> bool:
    return (isinstance(manifest, dict)
            and manifest.get("infrastructure_nonretryable_reason") == "account_budget_exhausted")


def manifest_binding_error(manifest: dict | None, entry: dict, plan: dict) -> str | None:
    if manifest is None:
        return None
    if manifest.get("model") != plan["model"]:
        return "manifest_model_mismatch"
    if manifest.get("task_sha256") != entry["runtime_task_sha256"]:
        return "manifest_task_mismatch"
    return None


def summarize(out: Path, plan: dict, queue: dict) -> dict:
    rules = json.loads((PROJECT / "config/benchmark50_score_rules.json").read_text())["tasks"]
    rows = []
    for entry in queue["tasks"]:
        rule = rules[entry["task_id"]]
        baseline, scale = float(Fraction(rule["baseline"])), float(Fraction(rule["scale"]))
        manifest, error = load_manifest(Path(entry["manifest"]))
        binding_error = manifest_binding_error(manifest, entry, plan)
        category = "technical_gap" if binding_error else classify_manifest(manifest)
        row = {key: entry.get(key) for key in (
            "task_id", "name", "trial", "state", "trial_dir", "manifest", "pid",
            "started_utc", "ended_utc", "returncode", "stdout", "stderr")}
        row.update(category=category, manifest_error=error or binding_error, baseline_energy=baseline,
                   best=None, final=None, capability_zero_imputed=False)
        if manifest is not None:
            row.update(runtime_status=manifest.get("status"), manifest_sha256=digest(Path(entry["manifest"])),
                       model=manifest.get("model"), turns=manifest.get("turns"),
                       output_tokens_charged=manifest.get("output_tokens_charged"),
                       input_tokens_charged=manifest.get("input_tokens_charged"),
                       verifier_calls=manifest.get("verifier_calls"),
                       best=None if binding_error else result_view(manifest.get("best_verified_result"), baseline, scale),
                       final=None if binding_error else result_view(manifest.get("verified_result"), baseline, scale),
                       source_provenance=manifest.get("provenance"),
                       runtime_revision=manifest.get("runtime_revision"),
                       infrastructure_error=manifest.get("infrastructure_error"),
                       physics_status=manifest.get("physics_status"),
                       proof_status=manifest.get("proof_status"),
                       physics_candidates_unverified=manifest.get("physics_candidates", []),
                       physics_manifest=manifest.get("physics_manifest"),
                       physics_manifest_sha256=manifest.get("physics_manifest_sha256"),
                       proof_manifest=manifest.get("proof_manifest"),
                       proof_manifest_sha256=manifest.get("proof_manifest_sha256"),
                       proof_transition_sha256=manifest.get("proof_transition_sha256"),
                       resume_available=manifest.get("resume_available") is True,
                       account_budget_exhausted=account_refused(manifest))
        row["partial_best_censored"] = row["category"] == "technical_gap" and row["best"] is not None
        rows.append(row)
    scientific = sum(row["category"] == "scientific_terminal" for row in rows)
    result = {
        "schema": "physics-first-campaign-summary/v1", "created_utc": utc(),
        "experimental_change": "separate physics exploration and frozen-source formal proof; shared total limits",
        "experiment_id": plan["experiment_id"], "model": plan["model"],
        "provider": "openai", "plan_sha256": digest(out / "plan.json"),
        "campaign_status": queue["status"], "scientific_terminal_count": scientific,
        "requested_trials": 5, "all_scientific_terminal": scientific == 5,
        "best_verified_count": sum(row["best"] is not None for row in rows),
        "final_verified_count": sum(row["final"] is not None for row in rows),
        "rows": rows, "source_drift": source_drift(plan["source_sha256"]),
        "endpoint": "Lowest host-computed float energy of a kernel-verified legal TI state",
        "energy_is_certified_upper_bound": False,
        "replicates_per_task": 1, "confidence_interval": None,
        "interpretation": "Technical gaps are missing coverage, never capability zero. Partial best states are retained as censored observations; no cross-protocol legacy ranking.",
    }
    new_json(out / "summary.json", result)
    lines = ["# GPT先物理探索再证明的五题测试", "", f"实际模型ID: `{plan['model']}`. 每题1次独立试验, 宿主数值与Lean计算在本地执行, GPT与搜索由官方API提供.",
             "", f"科学终态: {scientific}/5. 通过核验的best态: {result['best_verified_count']}/5. 最终提交通过: {result['final_verified_count']}/5.",
             "", "能量为宿主计算的每qubit float值, 合法性与平移不变性经固定目标核验; 数值本身没有认证舍入上界. 技术缺失不填0, 单次重复不提供置信区间.",
             "", "| 题号 | 模型 | 运行状态 | best能量/qubit | final能量/qubit |", "| --- | --- | --- | ---: | ---: |"]
    for row in rows:
        best = "未获得" if row["best"] is None else f"{row['best']['energy_density']:.12g}"
        final = "未获得" if row["final"] is None else f"{row['final']['energy_density']:.12g}"
        lines.append(f"| {row['task_id']} | {row['name']} | {row.get('runtime_status') or row['state']} | {best} | {final} |")
    with (out / "summary.md").open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    return result


def run_campaign(out: Path, model: str, task_ids, *, parallel: int = 1,
                 command_factory: Callable[[dict, dict], list[str]] | None = None,
                 poll_seconds: float = 1.0) -> dict:
    """Run a fresh campaign. ``command_factory`` is for offline tests only.

    A technical terminal result is retained without automatic retry or resume.
    An authorized host can later decide whether a same-budget runtime resume is
    justified; this supervisor never resets a trial to obtain another answer.
    """
    if model != "gpt-6.1-terra":
        raise ValueError("This campaign requires the exact requested gpt-6.1-terra model")
    ids = normalize_ids(task_ids)
    if type(parallel) is not int or parallel not in (1, 2):
        raise ValueError("parallel must be 1 or 2")
    if (not isinstance(model, str) or not model or len(model) > 200
            or any(ord(char) < 32 for char in model)):
        raise ValueError("An exact bounded model ID is required")
    out = Path(os.path.abspath(out))
    if not out.is_relative_to(PROJECT) or out.parent.resolve(strict=True) != out.parent:
        raise ValueError("Campaign parent must be non-aliased and inside the project")
    if type(poll_seconds) not in (int, float) or not 0.01 <= poll_seconds <= 10:
        raise ValueError("poll_seconds must be in 0.01..10")
    bindings = source_bindings()
    endpoint = json.loads((EUREKA / "Environment/API/providers.json").read_text())["providers"]["openai"]["base_url"]
    validate_endpoint(endpoint)
    out.mkdir(mode=0o700, exist_ok=False)
    (out / "tasks").mkdir()
    entries = []
    for task_id in ids:
        task = load_task(int(task_id))
        task_path = out / "tasks" / (task_id + ".json")
        new_json(task_path, task)
        trial_dir = out / ("gpt-task" + task_id + "-trial1")
        entries.append({"task_id": task_id, "name": task["name"], "trial": 1,
                        "state": "pending", "trial_dir": str(trial_dir),
                        "manifest": str(trial_dir / "manifest.json"),
                        "task_file": str(task_path), "task_file_sha256": digest(task_path),
                        "task_canonical_sha256": task["canonical_sha256"],
                        "runtime_task_sha256": hashlib.sha256(json.dumps(task, ensure_ascii=False,
                            sort_keys=True, allow_nan=False).encode()).hexdigest(),
                        "stdout": str(out / ("task" + task_id + ".stdout.log")),
                        "stderr": str(out / ("task" + task_id + ".stderr.log")),
                        "host_resume_attempts": 0})
    plan = {"schema": "local-hybrid-campaign-plan/v1", "created_utc": utc(),
            "experiment_id": out.name, "provider": "openai", "model": model,
            "api_base_url": endpoint,
            "api_protocol": "responses", "actual_output_token_field": "max_output_tokens",
            "compute_location": "local_no_ssh", "task_ids": ids, "trials_per_task": 1,
            "parallel": parallel, "source_sha256": bindings, "limits": LIMITS,
            "task_definitions": [{key: entry[key] for key in ("task_id", "task_file", "task_file_sha256", "task_canonical_sha256", "runtime_task_sha256")} for entry in entries],
            "recovery_policy": "No automatic fresh retry or host resume; preserve technical gaps and cumulative runtime state",
            "selection_basis": "Reuse the same five task definitions as the prior local hybrid test; no prior model answers or scores are supplied to this model",
            "test_worker_override": command_factory is not None,
            "credentials_in_plan": False, "network_tools_exposed_to_model": True,
            "network_policy": "hosted_web_search_only; local_tools_remain_networkless",
            "comparison_control": "gpt61-terra-hybrid-web-five-local-20261115T103249Z",
            "experimental_change": "physics-only exploration then fresh proof conversation with a frozen same-trial candidate pool",
            "host_fix_revision": "v1_1: stage-1-specific closing and generic Lean fold projection fix",
            "excluded_diagnostic_pilot": "gpt61-terra-physics-first-five-local-20261115T114829Z",
            "physics_subcaps": {"api_responses":24,"output_tokens":48000,"input_tokens":600000,
                                "local_numeric_cpu_reserved":1800,"wall_seconds":5400},
            "proof_limits": "total minus actual charged exploration usage; original proof closing reserve retained",
            "proof_context": "fresh; same-trial frozen sources and notes only, no raw stage-1 conversation",
            "proof_source_policy": "exact physics candidate SHA allowlist",
            "between_trial_isolation": "Independent messages, references and private workspace; no peer answer mounts"}
    new_json(out / "plan.json", plan)
    queue = {"schema": "local-hybrid-campaign-queue/v1", "created_utc": utc(),
             "updated_utc": utc(), "status": "running", "plan_sha256": digest(out / "plan.json"),
             "pid": os.getpid(), "tasks": entries, "stop_reason": None}
    queue_path = out / "queue.json"
    atomic_json(queue_path, queue)
    active = {}
    interrupted = []
    old_handlers = {}
    def request_stop(number, _frame):
        interrupted.append(number)
    if __import__("threading").current_thread() is __import__("threading").main_thread():
        for number in (signal.SIGTERM, signal.SIGINT):
            old_handlers[number] = signal.signal(number, request_stop)
    factory = command_factory or standard_command
    try:
        while True:
            drift = source_drift(bindings)
            if drift and queue["stop_reason"] is None:
                queue.update(stop_reason="source_drift", source_drift=drift)
            if interrupted and queue["stop_reason"] is None:
                queue.update(stop_reason="host_interrupted", host_signal=interrupted[-1])
            for task_id, item in list(active.items()):
                process, entry = item["process"], item["entry"]
                if interrupted and not item.get("stop_sent"):
                    os.killpg(process.pid, signal.SIGTERM)
                    item.update(stop_sent=time.monotonic())
                if item.get("stop_sent") and time.monotonic() - item["stop_sent"] > 30 and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                code = process.poll()
                if code is None:
                    continue
                item["stdout"].close(); item["stderr"].close()
                manifest, error = load_manifest(Path(entry["manifest"]))
                binding_error = manifest_binding_error(manifest, entry, plan)
                category = "technical_gap" if binding_error else classify_manifest(manifest)
                entry.update(state=category, ended_utc=utc(), returncode=code,
                             runtime_status=None if manifest is None else manifest.get("status"),
                             manifest_error=error or binding_error, host_resume_attempts=0,
                             needs_host=category == "technical_gap")
                if manifest is not None:
                    entry["manifest_sha256"] = digest(Path(entry["manifest"]))
                if account_refused(manifest):
                    queue["stop_reason"] = "account_budget_exhausted"
                elif binding_error:
                    queue["stop_reason"] = "manifest_binding_mismatch"
                del active[task_id]
            if queue["stop_reason"] is not None:
                for entry in entries:
                    if entry["state"] == "pending":
                        entry.update(state="not_started_host_stop", host_stop_reason=queue["stop_reason"])
            while queue["stop_reason"] is None and len(active) < parallel:
                entry = next((value for value in entries if value["state"] == "pending"), None)
                if entry is None:
                    break
                command = factory(entry, plan)
                if not isinstance(command, list) or not command or any(not isinstance(value, str) for value in command):
                    raise ValueError("Worker command must be a nonempty argv list")
                stdout = Path(entry["stdout"]).open("xb")
                stderr = Path(entry["stderr"]).open("xb")
                try:
                    process = subprocess.Popen(command, cwd=EUREKA, stdin=subprocess.DEVNULL,
                                               stdout=stdout, stderr=stderr, start_new_session=True)
                except Exception as error:
                    stdout.close(); stderr.close()
                    entry.update(state="technical_gap", ended_utc=utc(), needs_host=True,
                                 launch_error=type(error).__name__)
                    continue
                entry.update(state="running", started_utc=utc(), pid=process.pid, command=command)
                active[entry["task_id"]] = {"process": process, "entry": entry,
                                           "stdout": stdout, "stderr": stderr}
            queue["updated_utc"] = utc()
            atomic_json(queue_path, queue)
            if not active and not any(entry["state"] == "pending" for entry in entries):
                break
            time.sleep(poll_seconds)
    except Exception as error:
        queue.update(stop_reason="supervisor_exception", supervisor_error=type(error).__name__)
        for entry in entries:
            if entry["state"] == "pending":
                entry.update(state="not_started_host_stop", host_stop_reason=queue["stop_reason"])
    finally:
        # Unexpected supervisor errors must not orphan private paid sessions.
        for item in active.values():
            process = item["process"]
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=15)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            item["stdout"].close(); item["stderr"].close()
            entry = item["entry"]
            manifest, error = load_manifest(Path(entry["manifest"]))
            binding_error = manifest_binding_error(manifest, entry, plan)
            entry.update(state="technical_gap", ended_utc=utc(), returncode=process.returncode,
                         runtime_status=None if manifest is None else manifest.get("status"),
                         manifest_error=error or binding_error, needs_host=True,
                         host_stop_reason=queue["stop_reason"])
            if manifest is not None:
                entry["manifest_sha256"] = digest(Path(entry["manifest"]))
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
    queue["status"] = ("stopped" if queue["stop_reason"] else "finished" if all(
        entry["state"] == "scientific_terminal" for entry in entries) else "finished_with_technical_gaps")
    queue["updated_utc"] = utc()
    atomic_json(queue_path, queue)
    summary = summarize(out, plan, queue)
    print(json.dumps({"event": "campaign_finished", "status": queue["status"],
                      "scientific_terminal_count": summary["scientific_terminal_count"],
                      "summary": str(out / "summary.json")}, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--task-ids", required=True, nargs=5)
    parser.add_argument("--parallel", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    result = run_campaign(args.out, args.model, args.task_ids, parallel=args.parallel)
    return 0 if result["all_scientific_terminal"] and not result["source_drift"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
