"""Fresh, sequential 1x/2x/4x trials, with durable independent attempt records."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[2]
MULTIPLIERS = (1, 2, 4)
MAX_TECHNICAL_RETRIES = 2
SCIENTIFIC_TERMINALS = frozenset({"completed", "final_submission_not_verified"})


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def canonical_output(path):
    path = Path(os.path.abspath(path))
    if path.resolve() != path or not path.is_relative_to(PROJECT / "runs"):
        raise ValueError("Output must be a new, unaliased directory under the mounted runs directory")
    return path


def _verified(value, lock_sha256):
    if not isinstance(value, dict):
        return False
    cert = value.get("certificate") or {}
    energy = value.get("energy_density")
    source = value.get("source_sha256")
    return (all(value.get(k) is True for k in
                ("ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified"))
            and type(energy) in (int, float) and math.isfinite(energy)
            and isinstance(cert, dict) and cert.get("kind") == "hybrid-fixed-goal-kernel-v1"
            and cert.get("theorem") == "Submission.accepted"
            and cert.get("target") == "GeneratedState.ProofObligation"
            and isinstance(source, str) and len(source) == 64
            and cert.get("source_sha256") == source and cert.get("lock_sha256") == lock_sha256)


def _manifest_bound(manifest, *, model, multiplier, task_sha256):
    return (isinstance(manifest, dict) and manifest.get("model") == model
            and manifest.get("budget_multiplier") == multiplier
            and manifest.get("task_sha256") == task_sha256)


def _usage(manifest):
    names = ("turns", "input_tokens_charged", "output_tokens_charged", "server_cpu_seconds_charged")
    result = {name: manifest.get(name) for name in names}
    # The manifest explicitly separates the two role totals. Preserve both,
    # because Formalist work is uncapped but its API consumption still matters.
    result.update(constructor=manifest.get("constructor_usage"), formalizer=manifest.get("formalizer_usage"))
    result["not_a_billing_estimate"] = True
    return result


class CampaignInterrupted(BaseException):
    pass


@contextmanager
def _interruptions():
    saved = {}
    def handler(signum, frame):
        raise CampaignInterrupted(signum)
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            saved[signum] = signal.signal(signum, handler)
        yield
    finally:
        for signum, previous in saved.items():
            signal.signal(signum, previous)


def subprocess_trial(*, config, api_key, log_dir):
    """The key is inherited through an environment entry, never an argv/file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    config_path = log_dir / "request.json"
    atomic_json(config_path, config)
    process = None
    with (log_dir / "stdout.log").open("xb") as stdout, (log_dir / "stderr.log").open("xb") as stderr:
        environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                       "LANG": "C.UTF-8", "HOME": "/nonexistent", "TMPDIR": "/tmp",
                       "SEEKGNOMES_API_KEY": api_key, "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONNOUSERSITE": "1", "OMP_NUM_THREADS": "1",
                       "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        try:
            process = subprocess.Popen([sys.executable, "-B", "-m", "revisions.seekgnomes_v1_1_1.cli",
                "_trial", "--request", str(config_path)], cwd=PROJECT, env=environment,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            atomic_json(log_dir / "process.json", {"pid": process.pid, "started_utc": utc(),
                                                   "key_in_argv": False})
            code = process.wait()
            path = Path(config["out"]) / "manifest.json"
            manifest = json.loads(path.read_text()) if path.is_file() else {}
            return manifest, code
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    pass


def run_campaign(out, *, model, endpoint, api_key, task_ids, completion_token_field="max_tokens",
                 web=True, trial_runner=None, results_writer=None, preflight_record=None):
    """No previous answers enter a fresh role session or a higher budget tier."""
    out = canonical_output(out)
    ids = tuple(str(int(value)).zfill(2) for value in task_ids)
    if not ids or len(set(ids)) != len(ids) or any(not 1 <= int(value) <= 50 for value in ids):
        raise ValueError("Expected distinct task IDs between 1 and 50")
    if not api_key or not model:
        raise ValueError("An API key and exact model ID are required")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(mode=0o700, exist_ok=False)
    lock_fd = os.open(out / ".campaign.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = subprocess_trial if trial_runner is None else trial_runner
    if results_writer is None:
        from .scoring import write_results
        results_writer = write_results
    fixed_lock = PROJECT / "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json"
    lock_sha = hashlib.sha256(fixed_lock.read_bytes()).hexdigest()
    from src.benchmark50_models import load_task
    task_hashes = {tid: hashlib.sha256(json.dumps(load_task(int(tid)), ensure_ascii=False,
                   sort_keys=True, allow_nan=False).encode()).hexdigest() for tid in ids}
    plan = {"schema": "seekgnomes-plan/v1", "created_utc": utc(), "experiment_id": out.name,
        "model": model, "api_base_url": endpoint, "task_ids": list(ids), "task_sha256": task_hashes,
        "budget_multipliers": list(MULTIPLIERS), "max_technical_retries_per_tier": MAX_TECHNICAL_RETRIES,
        "parallel_trials": 1, "web_enabled": web, "completion_token_field": completion_token_field,
        "proof_lock_sha256": lock_sha, "api_key_recorded": False,
        "success": "at_least_one_strict_fixed_goal_kernel_verified_finite_energy_state",
        "constructor_definition_only": True, "formalist_cumulative_budget": None,
        "independence": "fresh roles per attempt; no peer/task/previous-tier artifacts exposed"}
    atomic_json(out / "plan.json", plan)
    if preflight_record is not None:
        atomic_json(out / "preflight.json", preflight_record)
    queue = {"schema": "seekgnomes-queue/v1", "created_utc": utc(), "status": "running",
        "model": model, "task_ids": list(ids), "tasks": {tid: {"status": "pending",
         "selected_trial": None, "attempts": []} for tid in ids}}
    current = None
    current_attempt = None
    def save():
        queue["updated_utc"] = utc()
        atomic_json(out / "queue.json", queue)
    save()
    print(json.dumps({"event": "campaign_started", "out": str(out), "task_ids": list(ids),
                      "parallel_trials": 1, "automatic_resume": False}, ensure_ascii=False), flush=True)
    try:
        with _interruptions():
            for tid in ids:
                current = queue["tasks"][tid]
                stop_task = False
                for tier in MULTIPLIERS:
                    for retry in range(MAX_TECHNICAL_RETRIES + 1):
                        name = f"tasks/{tid}/tier-{tier}-attempt-{retry + 1}"
                        trial_dir = out / name
                        trial_dir.parent.mkdir(parents=True, exist_ok=True)
                        current_attempt = {"path": name, "budget_multiplier": tier, "tier": tier,
                            "technical_retry": retry, "status": "running", "started_utc": utc()}
                        current["attempts"].append(current_attempt)
                        current.update(status="running", budget_multiplier=tier)
                        save()
                        from .preflight import release_checks
                        if release_checks()["failures"]:
                            raise RuntimeError("Release source changed during campaign; no further API requests dispatched")
                        config = {"task_id": int(tid), "out": str(trial_dir), "experiment_id": out.name,
                            "budget_multiplier": tier, "model": model, "endpoint": endpoint,
                            "completion_token_field": completion_token_field, "web": web}
                        try:
                            manifest, code = runner(config=config, api_key=api_key,
                                log_dir=out / "attempt-logs" / f"task-{tid}-tier-{tier}-retry-{retry}")
                        except Exception as error:
                            manifest, code = {}, None
                            current_attempt["host_error_type"] = type(error).__name__
                        if not isinstance(manifest, dict):
                            manifest = {}
                        current_attempt.update(ended_utc=utc(), returncode=code,
                            runtime_status=manifest.get("status"), usage=_usage(manifest))
                        bound = _manifest_bound(manifest, model=model, multiplier=tier,
                                                task_sha256=task_hashes[tid])
                        current_attempt["manifest_binding_matched"] = bound
                        account = bool(manifest.get("account_error")) or manifest.get("status") == "account_budget_exceeded"
                        if bound and _verified(manifest.get("best_verified_result"), lock_sha):
                            current_attempt["status"] = "completed"
                            censored = manifest.get("status") not in SCIENTIFIC_TERMINALS or code != 0
                            current.update(status="completed", selected_trial=name, censored=censored)
                            stop_task = True
                        elif account:
                            current_attempt["status"] = "technical_gap"
                            current["status"] = "technical_gap"
                        elif bound and code == 0 and manifest.get("status") in SCIENTIFIC_TERMINALS:
                            current_attempt["status"] = "scientific_not_verified"
                            if tier == MULTIPLIERS[-1]:
                                current.update(status="scientific_failure", selected_trial=name)
                                stop_task = True
                            else:
                                current["status"] = "pending"
                            save()
                            break
                        else:
                            current_attempt["status"] = "technical_gap"
                            current["status"] = "technical_gap" if retry == MAX_TECHNICAL_RETRIES else "pending"
                            if retry == MAX_TECHNICAL_RETRIES:
                                stop_task = True
                        save()
                        print(json.dumps({"event": "trial_settled", "task_id": tid, "tier": tier,
                            "retry": retry, "status": current["status"], "runtime_status": manifest.get("status")},
                            ensure_ascii=False), flush=True)
                        if account:
                            queue["status"] = "account_budget_exceeded"
                            save()
                            return queue
                        if stop_task:
                            break
                    if stop_task:
                        break
                results_writer(out)
            queue["status"] = "finished" if all(e["status"] in ("completed", "scientific_failure")
                for e in queue["tasks"].values()) else "finished_with_technical_gaps"
    except (CampaignInterrupted, KeyboardInterrupt) as error:
        queue["status"] = "cancelled"
        queue["stop_signal"] = error.args[0] if isinstance(error, CampaignInterrupted) and error.args else None
        queue["automatic_resume"] = False
        if current_attempt is not None and (current_attempt["status"] == "running"
                or current is not None and current["status"] == "running"):
            current_attempt.update(status="cancelled", ended_utc=utc(), cancellation_requested=True)
            trial_path = out / current_attempt["path"] / "manifest.json"
            try:
                stored = json.loads(trial_path.read_text())
                if isinstance(stored, dict):
                    bound = _manifest_bound(stored, model=model,
                        multiplier=current_attempt["budget_multiplier"], task_sha256=task_hashes[tid])
                    current_attempt.update(usage=_usage(stored), runtime_status=stored.get("status"),
                                           manifest_binding_matched=bound)
                    if bound and _verified(stored.get("best_verified_result"), lock_sha):
                        # User cancellation stops the campaign, not a certificate
                        # already committed by the fixed host checker. Keep the
                        # selected evidence available to scoring and export.
                        current_attempt.update(status="completed", censored=True)
                        current.update(status="completed", selected_trial=current_attempt["path"],
                                       censored=True, cancellation_requested=True)
            except (OSError, ValueError):
                pass
        if current is not None and current["status"] == "running":
            current["status"] = "cancelled"
    except Exception as error:
        queue["status"] = "infrastructure_failure"
        queue["host_error_type"] = type(error).__name__
        queue["automatic_resume"] = False
        if current_attempt is not None and current_attempt["status"] == "running":
            current_attempt.update(status="technical_gap", ended_utc=utc())
        if current is not None and current["status"] == "running":
            current["status"] = "technical_gap"
    finally:
        queue["ended_utc"] = utc()
        save()
        try:
            results_writer(out)
        finally:
            os.close(lock_fd)
    return queue
