"""Offline tests of subprocess isolation, dispatch, shutdown and score projection."""

from functools import partial
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
if HERE.name == "console-src":
    PROJECT = HERE.parent / "payload/Projects/QuantumManyBodyVariational"
sys.path[:0] = [str(PROJECT), str(HERE / "runtime")]


KEY = "offline-private-test-key"


def load(path):
    return json.loads(path.read_text())


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Offline workers did not reach their coordination point")
        time.sleep(0.02)


def fake_trial(scenario, *, out, task_id, model, budget_multiplier, api_key, cpus, **settings):
    from revisions.seekgnomes_v1_1_1.campaign import atomic_json
    from revisions.seekgnomes_v1_2.task_export import STATE_PROTOCOL
    from src.benchmark50_models import load_task

    assert api_key == KEY and api_key not in sys.argv and api_key not in os.environ.values()
    out = Path(out)
    out.mkdir()
    task = load_task(task_id)
    task_text = json.dumps(task, ensure_ascii=False, sort_keys=True, allow_nan=False)
    atomic_json(out / "task.json", task)
    lead = os.getpid()
    atomic_json(out / "offline-worker.json", {"pid": lead, "pgid": os.getpgid(0),
                "affinity": sorted(os.sched_getaffinity(0)), "cpus": cpus, "started": time.monotonic()})
    if scenario == "crash" and task_id == 1:
        # This imitates a gateway subprocess still running at controller death.
        helper = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
        (out / "helper.pid").write_text(str(helper.pid))
        os._exit(7)
    if scenario in ("overlap", "account", "stop") and task_id in (1, 2):
        shards = out.parents[3]
        partner = f"{3 - task_id:02d}"
        marker = shards / partner / "tasks" / partner / "attempt-1" / "offline-worker.json"
        wait_for(marker.exists)
    if scenario == "stop" or (scenario == "account" and task_id == 2):
        time.sleep(60)
    if scenario == "account":
        status = "account_budget_exceeded"
    elif scenario == "overlap" and task_id == 2 and out.name == "attempt-1":
        status = "infrastructure_failure"
    else:
        status = "final_submission_not_verified"
    manifest = {"status": status, "model": model, "budget_multiplier": budget_multiplier,
                "protocol": STATE_PROTOCOL,
                "task_sha256": hashlib.sha256(task_text.encode()).hexdigest(), "best_verified_result": None,
                "constructor_usage": {"output_tokens_charged": 2}}
    atomic_json(out / "manifest.json", manifest)
    (out / "finished").write_text(str(time.monotonic()))
    return manifest


def fixture(mode, scenario, source, out=None):
    sys.path.insert(0, source)
    from parallel_campaign import run_parallel, run_worker

    # Preserve atomic rename and exercise the persisted schema without
    # competing for physical disk flushes across concurrently running tests.
    os.fsync = lambda fd: None
    launcher = None
    if mode == "campaign" and scenario != "serial":
        worker = [sys.executable, "-B", __file__, "worker", scenario, source]
        launcher = partial(run_parallel, parallel_tasks=2, worker_argv=worker)
    from revisions.seekgnomes_v1_2 import campaign

    campaign.release_checks = lambda: {"failures": []}
    campaign.run_harbor_trial = partial(fake_trial, scenario)
    if mode == "worker":
        key = sys.stdin.buffer.readline().decode("ascii").rstrip("\n")
        return run_worker(sys.stdin.buffer, key)
    campaign.run_campaign(Path(out), model="gpt-offline", endpoint="https://example.invalid",
                          api_key=KEY, task_ids=(1, 2, 3), cpus=2, preflight_record={"ok": True},
                          constructor_output_tokens=71, constructor_active_seconds=None,
                          _parallel_launcher=launcher)


@unittest.skipUnless(hasattr(os, "sched_getaffinity") and len(os.sched_getaffinity(0)) >= 2,
                     "Two Linux CPUs are required for the parallel fixture")
class ParallelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="console-parallel-test-", dir=HERE)
        self.addCleanup(temporary.cleanup)
        # Stage a source snapshot so the campaign's file outputs remain private.
        self.source = Path(temporary.name) / "payload"
        from revisions.seekgnomes_v1_2.build_bundle import source_closure
        for name in source_closure():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((PROJECT / name).read_bytes())
        self.out = self.source / "runs/campaign"

    def run_campaign(self, scenario, sig=None):
        with subprocess.Popen([sys.executable, "-B", __file__, "campaign", scenario,
                               str(self.source), str(self.out)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
            try:
                if sig:
                    wait_for(lambda: all(self.artifact(task, "offline-worker.json").is_file() for task in ("01", "02")))
                    process.send_signal(sig)
                _, err = process.communicate(timeout=15)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=18)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            self.assertEqual(process.returncode, 0, err.decode())
        return load(self.out / "queue.json"), load(self.out / "results/scores.json")

    def artifact(self, task, file, attempt=1):
        return self.out / "shards" / task / "tasks" / task / f"attempt-{attempt}" / file

    def test_overlap_and_scoring_of_retried_attempt(self):
        queue, scores = self.run_campaign("overlap")
        plan = load(self.out / "plan.json")
        self.assertEqual((plan["parallel_trials"], plan["resources"]["cpus"],
                          plan["resources"]["cpus_per_task"]), (2, 2, 1))
        self.assertEqual(plan["task_ids"], ["01", "02", "03"])
        worker1, worker2, worker3 = (load(self.artifact(task, "offline-worker.json")) for task in plan["task_ids"])
        self.assertLess(worker2["started"], float(self.artifact("01", "finished").read_text()))
        self.assertFalse(set(worker1["affinity"]) & set(worker2["affinity"]))
        for worker in (worker1, worker2, worker3):
            self.assertEqual(worker["pid"], worker["pgid"])
            self.assertEqual(worker["cpus"], len(worker["affinity"]))
        self.assertEqual(queue["status"], "finished")
        selected = "shards/02/tasks/02/attempt-2"
        self.assertEqual(queue["tasks"]["02"]["selected_trial"], selected)
        self.assertEqual(scores["rows"][1]["selected_trial"], selected)
        self.assertEqual(scores["total_meters"]["output_tokens_charged"], 8)
        self.assertEqual(load(self.out / "shards/02/queue.json")["tasks"]["02"]["selected_trial"],
                         "tasks/02/attempt-2")
        self.assertEqual(load(self.out / "shards/02/plan.json")["task_sha256"]["02"], plan["task_sha256"]["02"])
        self.assertEqual(load(self.out / "shards/02/plan.json")["resources"]["budget_mode"], "tokens")

    def test_serial_leaves_existing_layout(self):
        queue, scores = self.run_campaign("serial")
        plan = load(self.out / "plan.json")
        self.assertEqual(plan["parallel_trials"], 1)
        self.assertNotIn("cpus_per_task", plan["resources"])
        self.assertFalse((self.out / "shards").exists())
        self.assertEqual(queue["tasks"]["01"]["selected_trial"], "tasks/01/attempt-1")
        self.assertEqual(scores["total_meters"]["output_tokens_charged"], 6)

    def test_account_error_stops_dispatch(self):
        queue, scores = self.run_campaign("account")
        self.assertEqual(queue["status"], "account_budget_exceeded")
        self.assertEqual([row["status"] for row in scores["rows"]],
                         ["technical_gap", "cancelled", "cancelled_before_start"])
        self.assertFalse((self.out / "shards/03").exists())
        self.assertTrue(all(row["score"] is None for row in scores["rows"]))

    def test_operator_signal_cancels_running_and_pending_workers(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                self.out = self.out.with_name(f"campaign-{sig}")
                queue, scores = self.run_campaign("stop", sig=sig)
                self.assertEqual((queue["status"], queue["stop_signal"]), ("cancelled", sig))
                self.assertEqual([row["status"] for row in scores["rows"]],
                                 ["cancelled", "cancelled", "cancelled_before_start"])
                for task in ("01", "02"):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(load(self.artifact(task, "offline-worker.json"))["pid"], 0)

    def test_worker_crash_closes_group_and_is_not_retried(self):
        queue, scores = self.run_campaign("crash")
        self.assertEqual(queue["status"], "finished_with_technical_gaps")
        failed = queue["tasks"]["01"]
        self.assertEqual((failed["status"], failed["worker_returncode"]), ("technical_gap", 7))
        self.assertEqual(len(failed["attempts"]), 1)
        self.assertIsNone(scores["rows"][0]["score"])
        self.assertEqual(scores["rows"][1]["status"], "scientific_failure")
        helper = Path("/proc") / self.artifact("01", "helper.pid").read_text() / "stat"
        wait_for(lambda: not helper.exists() or ") Z " in helper.read_text())


if __name__ == "__main__":
    raise SystemExit(fixture(*sys.argv[1:]))
