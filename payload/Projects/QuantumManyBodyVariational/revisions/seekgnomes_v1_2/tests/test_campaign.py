"""Host-only scheduler fixtures, never synthetic successful proof receipts."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
from revisions.seekgnomes_v1_2 import campaign
from src.benchmark50_models import load_task


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        (PROJECT / "runs").mkdir(exist_ok=True)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="v12-campaign-unit-", dir=PROJECT / "runs"))
        self.root, self.out = Path(folder), Path(folder) / "campaign"
        self.key = "offline-private-fixture-capability"
        self.calls, self.responses = [], []
        self.runner = self.stack.enter_context(patch.object(campaign, "run_harbor_trial", side_effect=self.fake_harbor))
        self.scorer = self.stack.enter_context(patch.object(campaign, "write_results"))
        self.cancel = self.stack.enter_context(patch.object(campaign, "cancel_active_trials"))
        self.release = self.stack.enter_context(patch.object(campaign, "release_checks", return_value={"failures": []}))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.addCleanup(self.stack.close)

    def fake_harbor(self, **kwargs):
        self.assertEqual(kwargs.pop("api_key"), self.key)
        self.calls.append(dict(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        task = load_task(kwargs["task_id"])
        manifest = {
            "model": kwargs["model"], "budget_multiplier": 1, "protocol": campaign.STATE_PROTOCOL,
            "task_sha256": hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
            "status": response if isinstance(response, str) else "infrastructure_failure",
            "best_verified_result": None,
            "turns": 1, "input_tokens_charged": 11, "output_tokens_charged": 7,
            "server_cpu_seconds_charged": 0,
            "harbor": {"version": "0.22.0", "scientific_trial": False, "fixture": True},
        }
        if isinstance(response, dict):
            manifest.update(response)
        trial = Path(kwargs["out"])
        trial.mkdir(parents=True, exist_ok=kwargs.get("resume", False))
        campaign.atomic_json(trial / "manifest.json", manifest)
        campaign.atomic_json(trial / "task.json", task)
        return manifest

    def run_campaign(self, task_ids=(1,), **kwargs):
        return campaign.run_campaign(self.out, model="gpt-6.1-terra", endpoint="http://localhost:9999/v1",
                                     api_key=self.key, task_ids=task_ids, **kwargs)

    def test_three_technical_attempts_are_fresh_and_share_identical_scientific_settings(self):
        self.responses = ["infrastructure_failure"] * 3
        queue = self.run_campaign(constructor_output_tokens=250000, constructor_input_tokens=900000,
                                  constructor_active_seconds=3600, cpus=4)
        self.assertEqual(queue["status"], "finished_with_technical_gaps")
        entry = queue["tasks"]["01"]
        self.assertEqual(entry["status"], "technical_gap")
        self.assertIsNone(entry["selected_trial"])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({item["out"] for item in self.calls}), 3)
        comparable = [{key: value for key, value in item.items() if key != "out"} for item in self.calls]
        self.assertEqual(comparable[0], comparable[1])
        self.assertEqual(comparable[1], comparable[2])
        self.assertTrue(all(item["budget_multiplier"] == 1 for item in self.calls))
        self.assertEqual([item["path"] for item in entry["attempts"]],
                         ["tasks/01/attempt-1", "tasks/01/attempt-2", "tasks/01/attempt-3"])

    def test_first_normal_scientific_failure_settles_without_another_allocation(self):
        self.responses = ["final_submission_not_verified", "infrastructure_failure"]
        queue = self.run_campaign()
        self.assertEqual(queue["status"], "finished")
        self.assertEqual(queue["tasks"]["01"]["status"], "scientific_failure")
        self.assertEqual(queue["tasks"]["01"]["selected_trial"], "tasks/01/attempt-1")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(len(self.responses), 1)
        plan = json.loads((self.out / "plan.json").read_text())
        self.assertEqual(plan["protocol"], campaign.STATE_PROTOCOL)
        self.assertEqual(plan["proof_lock_sha256"], campaign.hashlib.sha256(campaign.LOCK.read_bytes()).hexdigest())
        self.assertNotIn("budget_multipliers", plan)
        self.assertFalse(plan["resources"]["budget_expansion"])
        self.assertEqual(plan["resources"]["closing_allocations"], 1)
        self.assertEqual(plan["resources"]["closing_output_tokens"], 50000)
        self.assertEqual(plan["resources"]["closing_active_seconds"], 14400)
        self.assertIsNone(plan["resources"]["closing_input_tokens"])
        self.assertIsNone(plan["resources"]["api_requests_limit"])
        self.assertIsNone(plan["resources"]["constructor_input_tokens"])

    def test_scientific_failure_after_two_technical_gaps_selects_third_attempt(self):
        self.responses = [RuntimeError("offline fixture outage"), "infrastructure_failure", "scientific_failure"]
        queue = self.run_campaign()
        self.assertEqual(self.runner.call_count, 3)
        self.assertEqual(queue["tasks"]["01"]["selected_trial"], "tasks/01/attempt-3")
        self.assertEqual(queue["tasks"]["01"]["status"], "scientific_failure")

    def test_all_configurable_parameters_reach_the_harbor_adapter_without_credentials_on_disk(self):
        self.responses = ["final_submission_not_verified"]
        self.run_campaign(company="OpenAI", harness="codex", api_protocol="responses", web=False,
                          completion_token_field="max_completion_tokens", model_context_tokens=500000,
                          constructor_output_tokens=321000, constructor_input_tokens=765000,
                          constructor_active_seconds=4321, cpus=3)
        config = self.calls[0]
        for key, expected in {"constructor_output_tokens": 321000, "constructor_input_tokens": 765000,
                "constructor_active_seconds": 4321, "cpus": 3, "model_context_tokens": 500000,
                "company": "OpenAI", "harness": "codex", "api_protocol": "responses", "web": False,
                "completion_token_field": "max_completion_tokens"}.items():
            self.assertEqual(config[key], expected)
        for path in self.out.rglob("*.json"):
            self.assertNotIn(self.key, path.read_text())

    def test_each_task_stops_at_first_scientific_terminal(self):
        self.responses = ["final_submission_not_verified", "scientific_failure"]
        queue = self.run_campaign(task_ids=(1, 7))
        self.assertEqual([item["task_id"] for item in self.calls], [1, 7])
        self.assertTrue(all(item["status"] == "scientific_failure" for item in queue["tasks"].values()))
        for config in self.calls:
            self.assertFalse(any("previous" in key or "candidate" in key or "history" in key for key in config))

    def test_resume_reuses_interrupted_attempt_and_skips_settled_tasks(self):
        self.responses = ["scientific_failure", "scientific_failure"]
        queue = self.run_campaign(task_ids=(1, 7), constructor_output_tokens=54321)
        settled = self.out / queue["tasks"]["01"]["selected_trial"] / "manifest.json"
        recorded = settled.read_bytes()
        queue["status"] = "running"
        queue.pop("ended_utc")
        interrupted = queue["tasks"]["07"]
        interrupted.update(status="running", selected_trial=None)
        interrupted["attempts"][0]["status"] = "running"
        campaign.atomic_json(self.out / "queue.json", queue)

        self.responses = ["scientific_failure"]
        resumed = campaign.resume_campaign(self.out, api_key=self.key)
        self.assertEqual(resumed["status"], "finished")
        self.assertEqual(settled.read_bytes(), recorded)
        self.assertEqual(len(resumed["tasks"]["07"]["attempts"]), 1)
        self.assertEqual(self.calls[-1], {**self.calls[1], "resume": True})
        self.assertEqual(len(self.calls), 3)

    def test_explicit_cancellation_cannot_resume(self):
        self.responses = [KeyboardInterrupt()]
        self.run_campaign()
        with self.assertRaisesRegex(ValueError, "terminal status"):
            campaign.resume_campaign(self.out, api_key=self.key)
        self.assertEqual(len(self.calls), 1)

    def test_rejected_controller_checkpoint_does_not_open_a_second_attempt(self):
        self.responses = [{"status": "infrastructure_failure", "controller_recovery_failure": True}]
        queue = self.run_campaign()
        self.assertEqual(queue["status"], "infrastructure_failure")
        self.assertEqual(len(queue["tasks"]["01"]["attempts"]), 1)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_claimed_receipt_is_a_technical_gap_not_a_scientific_success(self):
        # Deliberately invalid data tests rejection, never simulates a valid proof.
        self.responses = [{"status": "completed", "best_verified_result": {"ok": True, "energy_density": -1}}] * 3
        queue = self.run_campaign()
        self.assertEqual(queue["tasks"]["01"]["status"], "technical_gap")
        self.assertIsNone(queue["tasks"]["01"]["selected_trial"])
        self.assertTrue(all(item["technical_reason"] == "invalid_strict_receipt"
                            for item in queue["tasks"]["01"]["attempts"]))

    def test_wrong_task_or_model_binding_never_settles_scientific_failure(self):
        self.responses = [{"status": "final_submission_not_verified", "model": "another-model"}] * 3
        queue = self.run_campaign()
        self.assertEqual(queue["tasks"]["01"]["status"], "technical_gap")
        self.assertTrue(all(not item["manifest_binding_matched"] for item in queue["tasks"]["01"]["attempts"]))

    def test_account_stop_never_retries_or_dispatches_the_next_task(self):
        self.responses = ["account_budget_exceeded"]
        queue = self.run_campaign(task_ids=(1, 7))
        self.assertEqual(queue["status"], "account_budget_exceeded")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(queue["tasks"]["07"]["status"], "pending")
        self.assertFalse(queue["automatic_resume"])

    def test_user_interrupt_stops_active_harbor_execution_and_does_not_retry(self):
        self.responses = [KeyboardInterrupt()]
        queue = self.run_campaign(task_ids=(1, 7))
        self.assertEqual(queue["status"], "cancelled")
        self.assertEqual(queue["tasks"]["01"]["status"], "cancelled")
        self.assertEqual(self.runner.call_count, 1)
        self.assertGreaterEqual(self.cancel.call_count, 1)
        self.assertFalse(queue["automatic_resume"])

    def test_cancelled_native_result_is_not_retried_as_a_technical_outage(self):
        self.responses = ["cancelled"]
        queue = self.run_campaign()
        self.assertEqual(queue["status"], "cancelled")
        self.assertEqual(self.runner.call_count, 1)

    def test_operator_and_before_start_cancellations_never_launch_another_attempt(self):
        for index, status in enumerate(("cancelled_by_operator", "cancelled_before_start"), 1):
            with self.subTest(status=status):
                self.out = self.root / status
                self.responses = [status]
                queue = self.run_campaign(task_ids=(1, 7))
                self.assertEqual(queue["status"], "cancelled")
                self.assertEqual(self.runner.call_count, index)
                self.assertEqual(queue["tasks"]["07"]["status"], "pending")

    def test_changed_sealed_release_stops_before_a_model_request(self):
        self.release.return_value = {"failures": ["offline fixture source mismatch"]}
        queue = self.run_campaign()
        self.assertEqual(queue["status"], "infrastructure_failure")
        self.assertEqual(self.runner.call_count, 0)

    def test_invalid_budget_is_rejected_before_creating_a_campaign(self):
        for kwargs in ({"constructor_output_tokens": 0}, {"constructor_output_tokens": True},
                       {"constructor_input_tokens": 0}, {"constructor_active_seconds": -1}, {"cpus": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_campaign(**kwargs)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.runner.call_count, 0)


if __name__ == "__main__":
    unittest.main()
