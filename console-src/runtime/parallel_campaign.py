"""Run isolated pinned Harbor campaigns inside one container and project their evidence."""

from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from revisions.seekgnomes_v1_1_1.campaign import atomic_json, utc
from revisions.seekgnomes_v1_1_1.scoring import write_results


POLL_SECONDS = 0.1
STOP_GRACE_SECONDS = 15
WORKER_REQUEST_LIMIT = 65536
WORKER_OPTIONS = frozenset({
    "model", "endpoint", "company", "harness", "api_protocol", "web", "completion_token_field",
    "model_context_tokens", "constructor_input_tokens", "constructor_output_tokens",
    "constructor_active_seconds",
})
SCIENCE = frozenset({"completed", "scientific_failure"})
TERMINAL_TASKS = SCIENCE | {"technical_gap", "cancelled"}


def validate_parallelism(parallel_tasks, ids, cpus):
    if type(parallel_tasks) is not int or not 1 <= parallel_tasks <= min(len(ids), cpus):
        raise ValueError("Parallel tasks must be between 1 and both the selected task count and total CPUs")


def run_worker(stream, key):
    """Read the request on the same anonymous pipe as the credential."""
    from revisions.seekgnomes_v1_2.campaign import run_campaign

    body = stream.readline(WORKER_REQUEST_LIMIT + 1)
    if len(body) > WORKER_REQUEST_LIMIT or not body.endswith(b"\n"):
        raise ValueError("Invalid bounded worker request")
    config = json.loads(body)
    affinity = config["affinity"]
    task = config["task_id"]
    options = config["options"]
    if (not isinstance(affinity, list) or not affinity or any(type(cpu) is not int for cpu in affinity)
            or len(set(affinity)) != len(affinity) or not set(affinity) <= os.sched_getaffinity(0)
            or not isinstance(task, str) or not re.fullmatch(r"0[1-9]|[1-4][0-9]|50", task)
            or not isinstance(options, dict) or set(options) != WORKER_OPTIONS):
        raise ValueError("Invalid worker allocation")
    os.sched_setaffinity(0, affinity)
    queue = run_campaign(Path(config["out"]), task_ids=(task,), api_key=key, cpus=len(affinity), **options)
    return 0 if queue["status"] == "finished" else 1


def _start_worker(argv, out, task, affinity, key, options):
    config = {"out": str(out / "shards" / task), "task_id": task, "affinity": affinity, "options": options}
    request = json.dumps(config).encode("ascii") + b"\n"
    if len(request) > WORKER_REQUEST_LIMIT:
        raise ValueError("Worker request exceeds its size limit")
    env = {name: value for name, value in os.environ.items() if name != "SEEKGNOMES_API_KEY"}
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    try:
        with process.stdin as stream:
            stream.write(key.encode("ascii") + b"\n")
            stream.write(request)
    except BrokenPipeError:
        # The scheduler will record the exited process and any persisted evidence.
        pass
    except BaseException:
        _signal_workers((process,), signal.SIGKILL)
        process.wait()
        raise
    return process


def _worker_evidence(out, task):
    prefix = f"shards/{task}"
    try:
        snapshot = json.loads((out / prefix / "queue.json").read_text())
        if snapshot["schema"] != "seekgnomes-queue/v1.2" or snapshot["task_ids"] != [task]:
            return None, None
        entry = snapshot["tasks"][task]
        selected = entry["selected_trial"]
        attempts = entry["attempts"]
        paths = [attempt["path"] for attempt in attempts]
        if (selected is not None and selected not in paths) or not all(
            re.fullmatch(rf"tasks/{task}/attempt-[1-9][0-9]*", path) for path in paths
        ):
            return None, None
        projected = {**entry, "selected_trial": f"{prefix}/{selected}" if selected else None,
                     "attempts": [{**attempt, "path": f"{prefix}/{path}"}
                                  for attempt, path in zip(attempts, paths)]}
        status = snapshot["status"]
        if any(attempt.get("runtime_status") == "account_budget_exceeded" for attempt in attempts):
            status = "account_budget_exceeded"
        return status, projected
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


@contextmanager
def _stop_signal():
    received = []

    def handler(signum, frame):
        if not received:
            received.append(signum)

    originals = {signum: signal.signal(signum, handler) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield received
    finally:
        for signum, original in originals.items():
            signal.signal(signum, original)


def _signal_workers(workers, signum):
    for process in workers:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass


def _worker_returncode(process):
    # Keep the exited leader unreaped while cleaning up any surviving gateway
    # I/O helpers in its group. The process group ID cannot be reused yet.
    if process.returncode is None:
        exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if exited is not None:
            _signal_workers((process,), signal.SIGKILL)
            return process.wait()
    return process.returncode


def run_parallel(out, plan, preflight_record, *, api_key, options, parallel_tasks, worker_argv):
    ids = plan["task_ids"]
    cpus = plan["resources"]["cpus"]
    validate_parallelism(parallel_tasks, ids, cpus)
    available = sorted(os.sched_getaffinity(0))
    if len(available) < cpus:
        raise ValueError("Total CPUs exceed the available affinity")
    per_task = cpus // parallel_tasks
    slots = deque(tuple(available[index:index + per_task])
                  for index in range(0, per_task * parallel_tasks, per_task))
    plan["parallel_trials"] = parallel_tasks
    plan["resources"]["cpus_per_task"] = per_task
    pending = deque(ids)
    active = {}
    entries = {task: {"status": "pending", "selected_trial": None, "attempts": []} for task in ids}
    queue = {"schema": "seekgnomes-queue/v1.2", "created_utc": utc(), "status": "running",
             "model": plan["model"], "task_ids": ids, "tasks": entries}
    stop_status = None

    def save():
        queue["updated_utc"] = utc()
        atomic_json(out / "queue.json", queue)

    def refresh():
        nonlocal stop_status
        changed = False
        for task, (process, affinity) in list(active.items()):
            code = _worker_returncode(process)
            worker_status, observed = _worker_evidence(out, task)
            if worker_status == "account_budget_exceeded":
                stop_status = "account_budget_exceeded"
            elif worker_status == "cancelled" and stop_status is None:
                stop_status = "cancelled"
            entry = observed if observed is not None else entries[task].copy()
            if code is None:
                if entry["status"] == "pending":
                    entry["status"] = "running"
            else:
                slots.append(affinity)
                del active[task]
                if entry["status"] not in TERMINAL_TASKS:
                    status = "cancelled" if stop_status else "technical_gap"
                    entry.update(status=status, worker_returncode=code)
                    if entry["attempts"] and entry["attempts"][-1]["status"] == "running":
                        entry["attempts"][-1] = {**entry["attempts"][-1], "status": status, "ended_utc": utc()}
            if entry != entries[task]:
                entries[task] = entry
                changed = True
        return changed

    with _stop_signal() as received:
        (out / "shards").mkdir(mode=0o700)
        atomic_json(out / "plan.json", plan)
        if preflight_record is not None:
            atomic_json(out / "preflight.json", preflight_record)
        save()
        print(json.dumps({"event": "campaign_started", "out": str(out), "task_ids": ids,
                          "parallel_trials": parallel_tasks, "budget_expansion": False,
                          "automatic_resume": False}), flush=True)
        try:
            while pending or active:
                changed = refresh()
                if received and stop_status is None:
                    stop_status = "cancelled"
                if stop_status:
                    break
                while slots and pending and not received:
                    affinity, task = slots.popleft(), pending.popleft()
                    try:
                        process = _start_worker(worker_argv, out, task, affinity, api_key, options)
                    except OSError as error:
                        entries[task].update(status="technical_gap", host_error_type=type(error).__name__)
                        slots.append(affinity)
                    else:
                        entries[task]["status"] = "running"
                        active[task] = (process, affinity)
                    changed = True
                if changed:
                    save()
                if active:
                    time.sleep(POLL_SECONDS)
        except Exception as error:
            stop_status = stop_status or "infrastructure_failure"
            queue["host_error_type"] = type(error).__name__
        finally:
            if received and stop_status is None:
                stop_status = "cancelled"
            if stop_status:
                for entry in entries.values():
                    if entry["status"] == "pending":
                        entry["status"] = "cancelled_before_start"
                queue.update(status=stop_status, automatic_resume=False)
                if received:
                    queue["stop_signal"] = received[0]
                for task, (process, _) in active.items():
                    # A settled worker can finish committing its existing reports.
                    if _worker_evidence(out, task)[0] in (None, "running"):
                        _signal_workers((process,), signal.SIGTERM)
                deadline = time.monotonic() + STOP_GRACE_SECONDS
                try:
                    save()
                    while active and time.monotonic() < deadline:
                        if refresh():
                            save()
                        if active:
                            time.sleep(POLL_SECONDS)
                finally:
                    _signal_workers((process for process, _ in active.values()), signal.SIGKILL)
                    for process, _ in active.values():
                        process.wait()
                    refresh()
                queue["status"] = stop_status
            else:
                queue["status"] = ("finished" if all(entry["status"] in SCIENCE for entry in entries.values())
                                   else "finished_with_technical_gaps")
            queue["ended_utc"] = utc()
            save()
            write_results(out)
    return queue
