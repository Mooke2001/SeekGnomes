"""One scientific allocation per task; fresh retries only for technical gaps.

Every dispatched attempt is a real Harbor Job/Trial. A normal scientific
failure settles the task immediately. Technical attempts repeat the identical
configuration at most twice, with new role workspaces, sessions and artifacts.
The multiplier field remains 1 only for the existing receipt/scorer format.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from .harnesses import resolve_harness
from .state_tools import is_verified
from .state_verifier import LOCK
from .task_export import STATE_PROTOCOL
from revisions.seekgnomes_v1_1_1.campaign import (
    CampaignInterrupted, _interruptions, _manifest_bound, _usage,
    atomic_json, canonical_output, utc,
)
from revisions.seekgnomes_v1_1_1.scoring import write_results

PROJECT = Path(__file__).resolve().parents[2]
MAX_TECHNICAL_RETRIES = 2
SCIENTIFIC_TERMINALS = frozenset({"completed", "final_submission_not_verified", "scientific_failure"})
CANCELLATION_TERMINALS = frozenset({"cancelled", "cancelled_by_operator", "cancelled_before_start"})


def run_harbor_trial(**kwargs):
    # Inspection does not require Harbor; production always uses its real adapter.
    from .harbor_adapter import run_harbor_trial as execute
    return execute(**kwargs)


def cancel_active_trials():
    from .runtime import cancel_active_trials as cancel
    cancel()


def release_checks():
    from revisions.seekgnomes_v1_1_1.preflight import release_checks as inspect
    return inspect()


def _positive(value, label, *, optional=False):
    if value is None and optional:
        return
    if type(value) is not int or value < 1:
        raise ValueError(label + " must be a positive integer" + (" or None" if optional else ""))


def _checkpoint_record(path):
    path = canonical_output(path)
    if not path.is_file() or path.stat().st_size > 16 * 1024**2:
        raise ValueError("Expected a bounded campaign checkpoint record")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid campaign checkpoint record")
    return value


def _restore_queue(out, plan):
    saved = _checkpoint_record(out / "plan.json")
    if json.dumps(saved, sort_keys=True) != json.dumps({**plan, "created_utc": saved.get("created_utc")}, sort_keys=True):
        raise ValueError("Campaign options differ from the saved checkpoint")
    queue = _checkpoint_record(out / "queue.json")
    if (queue.get("schema") != "seekgnomes-queue/v1.2" or queue.get("model") != plan["model"]
            or queue.get("task_ids") != plan["task_ids"] or not isinstance(queue.get("tasks"), dict)
            or set(queue["tasks"]) != set(plan["task_ids"])):
        raise ValueError("Campaign queue differs from the saved plan")
    if queue.get("status") not in ("running", "finished", "finished_with_technical_gaps"):
        raise ValueError("This campaign cannot be resumed after its terminal status")
    for task_id, entry in queue["tasks"].items():
        attempts = entry.get("attempts") if isinstance(entry, dict) else None
        if not isinstance(attempts, list) or len(attempts) > MAX_TECHNICAL_RETRIES + 1:
            raise ValueError("Invalid saved campaign attempts")
        if entry.get("status") not in ("pending", "running", "completed", "scientific_failure", "technical_gap"):
            raise ValueError("Invalid saved campaign task status")
        for retry, attempt in enumerate(attempts):
            if (not isinstance(attempt, dict) or attempt.get("path") != f"tasks/{task_id}/attempt-{retry + 1}"
                    or attempt.get("technical_retry") != retry or attempt.get("budget_multiplier") != 1
                    or (attempt.get("status") == "running" and retry != len(attempts) - 1)
                    or attempt.get("cancellation_requested")):
                raise ValueError("Invalid saved campaign attempt")
    return queue


def _execute(config, api_key, log_dir, *, resume=False):
    log_dir.mkdir(parents=True, exist_ok=resume)
    request = log_dir / "harbor-request.json"
    if resume and request.exists():
        if _checkpoint_record(request) != config:
            raise ValueError("Saved Harbor request differs from the campaign checkpoint")
    else:
        atomic_json(request, config)
    try:
        result = run_harbor_trial(**config, api_key=api_key, **({"resume": True} if resume else {}))
    except BaseException:
        cancel_active_trials()
        raise
    code = 0 if isinstance(result, dict) and result.get("status") in SCIENTIFIC_TERMINALS else 1
    atomic_json(log_dir / "execution.json", {
        "framework": "Harbor", "returncode": code,
        "status": result.get("status") if isinstance(result, dict) else None,
        "harbor": result.get("harbor") if isinstance(result, dict) else None,
    })
    return result, code


def run_campaign(
    out, *, model, endpoint, api_key, task_ids, company=None, harness=None,
    api_protocol="auto", web=True, completion_token_field="max_tokens",
    preflight_record=None, model_context_tokens=None,
    constructor_output_tokens=128000, constructor_input_tokens=None,
    constructor_active_seconds=14400, cpus=2, resume=False, _parallel_launcher=None,
):
    """The first normal scientific terminal settles a task, without expansion.

    Main and optional closing allocations live inside runtime's single attempt.
    No candidate/history crosses a fresh technical attempt.
    """
    selection = resolve_harness(model, company=company, harness=harness, api_protocol=api_protocol)
    if not selection.supported:
        raise ValueError(selection.unsupported_reason)
    for value, label, optional in (
        (constructor_output_tokens, "Constructor output budget", True),
        (constructor_input_tokens, "Constructor input budget", True),
        (constructor_active_seconds, "Constructor activity budget", True),
        (cpus, "CPU count", False), (model_context_tokens, "Model context", True),
    ):
        _positive(value, label, optional=optional)
    if constructor_output_tokens is None and constructor_active_seconds is None:
        raise ValueError("Choose a Constructor output or activity budget")
    if resume and _parallel_launcher is not None:
        raise ValueError("Resume a parallel campaign through its shard checkpoints")
    ids = tuple(str(int(value)).zfill(2) for value in task_ids)
    if not ids or len(set(ids)) != len(ids) or any(not 1 <= int(value) <= 50 for value in ids):
        raise ValueError("Expected distinct task IDs between 1 and 50")
    if not isinstance(api_key, str) or not api_key or not model or api_key in model or api_key in endpoint:
        raise ValueError("An API key and exact public model/endpoint configuration are required")
    out = canonical_output(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not resume:
        out.mkdir(mode=0o700, exist_ok=False)
    lock_fd = os.open(out / ".campaign.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(lock_fd)
        raise

    lock_sha = hashlib.sha256(LOCK.read_bytes()).hexdigest()
    from src.benchmark50_models import load_task
    task_hashes = {tid: hashlib.sha256(json.dumps(load_task(int(tid)), ensure_ascii=False,
        sort_keys=True, allow_nan=False).encode()).hexdigest() for tid in ids}
    resources = {
        "constructor_output_tokens": constructor_output_tokens,
        "constructor_input_tokens": constructor_input_tokens,
        "constructor_active_seconds": constructor_active_seconds,
        "cpus": cpus, "api_requests_limit": None,
        "closing_output_tokens": 50000, "closing_allocations": 1,
        "closing_active_seconds": constructor_active_seconds,
        "closing_input_tokens": constructor_input_tokens,
        "budget_expansion": False,
    }
    if constructor_active_seconds is None:
        resources["budget_mode"] = "tokens"
    elif constructor_output_tokens is None:
        resources["budget_mode"] = "time"
    plan = {
        "schema": "seekgnomes-plan/v1.2", "created_utc": utc(), "experiment_id": out.name,
        "protocol": STATE_PROTOCOL, "candidate_format": "state",
        "execution_framework": "Harbor", "harbor_version": "0.22.0",
        "model": model, "api_base_url": endpoint, "task_ids": list(ids), "task_sha256": task_hashes,
        "harness_selection": selection.to_dict(), "model_context_tokens": model_context_tokens,
        "requested_harness": {"company": company, "harness": harness, "api_protocol": api_protocol},
        "native_context_management": True, "host_context_trimming": False,
        "resources": resources, "budget_multiplier": 1,
        "budget_multiplier_semantics": "legacy receipt/scorer compatibility only; no tier expansion",
        "max_technical_retries": MAX_TECHNICAL_RETRIES,
        "scientific_attempt_policy": "first normal scientific terminal settles task; retry only technical gaps",
        "parallel_trials": 1, "web_enabled": bool(web), "completion_token_field": completion_token_field,
        "controller_recovery": True,
        "proof_lock_sha256": lock_sha, "api_key_recorded": False,
        "success": "at_least_one_strict_fixed_goal_kernel_verified_finite_energy_state",
        "constructor_definition_only": True, "formalist_cumulative_budget": None,
        "independence": "fresh roles per technical attempt; no peer/task/previous-attempt artifacts exposed",
    }
    try:
        if _parallel_launcher is not None:
            result = _parallel_launcher(out, plan, preflight_record, api_key=api_key, options={
                "model": model, "endpoint": endpoint, "company": company, "harness": harness,
                "api_protocol": api_protocol, "web": web, "completion_token_field": completion_token_field,
                "model_context_tokens": model_context_tokens,
                "constructor_input_tokens": constructor_input_tokens,
                "constructor_output_tokens": constructor_output_tokens,
                "constructor_active_seconds": constructor_active_seconds,
            })
            os.close(lock_fd)
            return result
        if resume:
            queue = _restore_queue(out, plan)
            if queue["status"] != "running":
                os.close(lock_fd)
                return queue
        else:
            atomic_json(out / "plan.json", plan)
            if preflight_record is not None:
                atomic_json(out / "preflight.json", preflight_record)
            queue = {
                "schema": "seekgnomes-queue/v1.2", "created_utc": utc(), "status": "running", "model": model,
                "task_ids": list(ids), "tasks": {tid: {"status": "pending", "selected_trial": None, "attempts": []} for tid in ids},
            }
    except BaseException:
        os.close(lock_fd)
        raise
    current = current_attempt = None

    def save():
        queue["updated_utc"] = utc()
        atomic_json(out / "queue.json", queue)

    save()
    print(json.dumps({"event": "campaign_resumed" if resume else "campaign_started", "out": str(out), "task_ids": list(ids),
                      "parallel_trials": 1, "budget_expansion": False, "controller_recovery": True}), flush=True)
    try:
        with _interruptions():
            for tid in ids:
                current = queue["tasks"][tid]
                if current["status"] in ("completed", "scientific_failure", "technical_gap"):
                    continue
                attempts = current["attempts"]
                unfinished = bool(attempts and attempts[-1]["status"] == "running")
                start_index = len(attempts) - int(unfinished)
                for retry in range(start_index, MAX_TECHNICAL_RETRIES + 1):
                    name = f"tasks/{tid}/attempt-{retry + 1}"
                    trial_dir = out / name
                    trial_dir.parent.mkdir(parents=True, exist_ok=True)
                    resume_attempt = unfinished and retry == start_index
                    if resume_attempt:
                        current_attempt = attempts[retry]
                    else:
                        current_attempt = {"path": name, "budget_multiplier": 1, "technical_retry": retry,
                                           "status": "running", "started_utc": utc()}
                        attempts.append(current_attempt)
                    current.update(status="running", budget_multiplier=1)
                    save()
                    if release_checks()["failures"]:
                        raise RuntimeError("Release source changed during campaign; no further API requests dispatched")
                    config = {
                        "task_id": int(tid), "out": str(trial_dir), "experiment_id": out.name,
                        "budget_multiplier": 1, "model": model, "endpoint": endpoint,
                        "company": company, "harness": harness, "api_protocol": api_protocol,
                        "completion_token_field": completion_token_field, "web": bool(web),
                        "model_context_tokens": model_context_tokens,
                        "constructor_output_tokens": constructor_output_tokens,
                        "constructor_input_tokens": constructor_input_tokens,
                        "constructor_active_seconds": constructor_active_seconds, "cpus": cpus,
                    }
                    try:
                        manifest, code = _execute(config, api_key,
                            out / "attempt-logs" / f"task-{tid}-attempt-{retry + 1}", resume=resume_attempt)
                    except Exception as error:
                        current_attempt["host_error_type"] = type(error).__name__
                        if resume_attempt:
                            raise
                        manifest, code = {}, None
                    if not isinstance(manifest, dict):
                        manifest = {}
                    current_attempt.update(ended_utc=utc(), returncode=code,
                        runtime_status=manifest.get("status"), usage=_usage(manifest))
                    bound = manifest.get("protocol") == STATE_PROTOCOL and _manifest_bound(
                        manifest, model=model, multiplier=1, task_sha256=task_hashes[tid])
                    current_attempt["manifest_binding_matched"] = bound
                    verified = bound and is_verified(manifest.get("best_verified_result"))
                    recovery_failed = (manifest.get("controller_recovery_failure") is True
                                       or manifest.get("error_type") == "controller_recovery_failure")
                    if (resume_attempt and not bound) or (recovery_failed and not verified):
                        raise RuntimeError("Interrupted attempt could not validate its saved checkpoint")
                    account = bool(manifest.get("account_error")) or manifest.get("status") == "account_budget_exceeded"
                    cancelled = manifest.get("status") in CANCELLATION_TERMINALS
                    if verified:
                        censored = manifest.get("status") not in SCIENTIFIC_TERMINALS or code != 0
                        current_attempt.update(status="completed", censored=censored)
                        current.update(status="completed", selected_trial=name, censored=censored)
                    elif not account and not cancelled and bound and code == 0 and manifest.get("status") in SCIENTIFIC_TERMINALS and manifest.get("best_verified_result") is None:
                        current_attempt["status"] = "scientific_not_verified"
                        current.update(status="scientific_failure", selected_trial=name)
                    else:
                        current_attempt["status"] = "cancelled" if cancelled else "technical_gap"
                        current["status"] = "cancelled" if cancelled else "technical_gap" if retry == MAX_TECHNICAL_RETRIES or account else "pending"
                        if bound and manifest.get("best_verified_result") is not None:
                            current_attempt["technical_reason"] = "invalid_strict_receipt"
                    save()
                    print(json.dumps({"event": "trial_settled", "task_id": tid, "technical_retry": retry,
                                      "status": current["status"], "runtime_status": manifest.get("status")}), flush=True)
                    if account or cancelled:
                        queue.update(status="account_budget_exceeded" if account else "cancelled", automatic_resume=False)
                        return queue
                    if current["status"] in ("completed", "scientific_failure", "technical_gap"):
                        break
                write_results(out)
            queue["status"] = "finished" if all(entry["status"] in ("completed", "scientific_failure")
                for entry in queue["tasks"].values()) else "finished_with_technical_gaps"
    except (CampaignInterrupted, KeyboardInterrupt) as error:
        cancel_active_trials()
        queue.update(status="cancelled", automatic_resume=False)
        queue["stop_signal"] = error.args[0] if isinstance(error, CampaignInterrupted) and error.args else None
        if current_attempt is not None and current is not None and (current_attempt["status"] == "running" or current["status"] == "running"):
            current_attempt.update(status="cancelled", ended_utc=utc(), cancellation_requested=True)
            try:
                stored = json.loads((out / current_attempt["path"] / "manifest.json").read_text())
                if isinstance(stored, dict):
                    bound = stored.get("protocol") == STATE_PROTOCOL and _manifest_bound(
                        stored, model=model, multiplier=1, task_sha256=task_hashes[tid])
                    current_attempt.update(usage=_usage(stored), runtime_status=stored.get("status"), manifest_binding_matched=bound)
                    if bound and is_verified(stored.get("best_verified_result")):
                        current_attempt.update(status="completed", censored=True)
                        current.update(status="completed", selected_trial=current_attempt["path"],
                                       censored=True, cancellation_requested=True)
            except (OSError, ValueError):
                pass
        if current is not None and current["status"] == "running":
            current["status"] = "cancelled"
    except Exception as error:
        cancel_active_trials()
        queue.update(status="infrastructure_failure", host_error_type=type(error).__name__, automatic_resume=False)
        if current_attempt is not None and current_attempt["status"] == "running":
            current_attempt.update(status="technical_gap", ended_utc=utc())
        if current is not None and current["status"] == "running":
            current["status"] = "technical_gap"
    finally:
        queue["ended_utc"] = utc()
        save()
        try:
            write_results(out)
        finally:
            os.close(lock_fd)
    return queue


def resume_campaign(out, *, api_key):
    """Continue the saved campaign after its outermost invocation was restarted."""
    out = canonical_output(out)
    plan = _checkpoint_record(out / "plan.json")
    options = plan.get("requested_harness")
    resources = plan.get("resources")
    if not isinstance(options, dict) or not isinstance(resources, dict):
        raise ValueError("This campaign has no controller recovery checkpoint")
    if plan.get("parallel_trials", 1) != 1:
        raise ValueError("Resume the individual shard checkpoints of a parallel campaign")
    from .cli import validate_endpoint
    endpoint = plan.get("api_base_url")
    validate_endpoint(endpoint, allow_local_http=True)
    return run_campaign(out, model=plan.get("model"),
        endpoint=endpoint, api_key=api_key,
        task_ids=plan.get("task_ids", ()), company=options.get("company"), harness=options.get("harness"),
        api_protocol=options.get("api_protocol"), web=plan.get("web_enabled"),
        model_context_tokens=plan.get("model_context_tokens"),
        constructor_output_tokens=resources.get("constructor_output_tokens"),
        constructor_active_seconds=resources.get("constructor_active_seconds"),
        constructor_input_tokens=resources.get("constructor_input_tokens"), cpus=resources.get("cpus"),
        completion_token_field=plan.get("completion_token_field"), resume=True)
