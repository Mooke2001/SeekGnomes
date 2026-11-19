"""Offline role-controller and Harbor regression using real Lean proofs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from revisions.seekgnomes_v1_1_1.scoring import score_run, verify_stored_result
from revisions.seekgnomes_v1_2 import runtime
from revisions.seekgnomes_v1_2.harbor_adapter import run_harbor_trial
from revisions.seekgnomes_v1_2.state_tools import is_verified
from src.benchmark50_models import load_task

PROJECT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.environ.get("SEEKGNOMES_RUN_LEAN_HANDOFF_TEST") == "1",
                     "Opt in to real Lean kernel and Harbor subprocesses")
class StateHarborTests(unittest.TestCase):
    def test_frozen_state_feedback_and_archived_score(self):
        examples = PROJECT / "examples/state_format"
        source = (examples / "tracial.state").read_text()
        proof = (examples / "TracialProof.lean").read_text()
        source_sha = hashlib.sha256(source.encode()).hexdigest()
        wrong_proof = """module
public import GeneratedState
@[expose] public section
namespace Submission
theorem accepted : True := True.intro
end Submission
"""
        task = load_task(11)
        feedback = []
        fixture_errors = []

        def action(role, name, **arguments):
            return role.dispatch({"method": "call_tool", "name": "benchmark_action",
                                  "arguments": {"tool": name, "arguments": arguments}})

        def fixture(role):
            role.ledger.resume()
            try:
                if role.role == "constructor":
                    self.assertTrue(action(role, "write", path="f.state", content=source)["ok"])
                    result = action(role, "create_handoff", definition_path="f.state")
                    self.assertEqual(result["kind"], "verified_state_energy", result)
                    self.assertEqual(result["definition_sha256"], source_sha)
                    self.assertEqual(result["result"]["energy_density"], -0.375)
                    self.assertTrue(is_verified(result["result"]))
                    feedback.append(result)
                    self.assertTrue(action(role, "finish")["ok"])
                else:
                    self.assertEqual(role.base.references["handoff/f.state"], source)
                    generated = action(role, "prepare")
                    self.assertTrue(generated["ok"], generated)
                    self.assertEqual(generated["source_sha256"], source_sha)
                    self.assertFalse(generated["proof_verified"])
                    self.assertIsNone(generated.get("energy_density"))
                    self.assertTrue(action(role, "write", path="Wrong.lean", content=wrong_proof)["ok"])
                    invalid = action(role, "score", proof_path="Wrong.lean")
                    self.assertTrue(invalid.get("compile_passed"), invalid)
                    self.assertFalse(invalid.get("goal_matched"), invalid)
                    self.assertFalse(invalid["proof_verified"], invalid)
                    self.assertIsNone(invalid.get("energy_density"))
                    self.assertIsNone(role.owner.top["best_verified_result"])
                    self.assertTrue(action(role, "write", path="Candidate.lean", content=proof)["ok"])
                    accepted = action(role, "score", proof_path="Candidate.lean")
                    self.assertTrue(is_verified(accepted), accepted)
                    self.assertEqual(accepted["generated_state_sha256"], generated["generated_state_sha256"])
                return role.receipt
            except AssertionError as error:
                fixture_errors.append(str(error))
                raise
            finally:
                role.active = False
                role.closed = True
                role.ledger.pause()
                role.gateway.close()
                role.save()

        with nullcontext(tempfile.mkdtemp(prefix="state-harbor-fixture-", dir=PROJECT / "runs")) as folder:
            root = Path(folder)
            trial = root / "trial"
            with patch.object(runtime.NativeRole, "run", new=fixture):
                result = run_harbor_trial(task_id=11, out=trial, model="gpt-host-fixture",
                    company="openai", endpoint="https://unused.invalid/v1", api_key="offline-fixture-key",
                    web=False, cpus=1, fixture_runner=runtime.run_trial)
            self.assertEqual(result["status"], "completed", {
                "runtime_error": result.get("error_type"), "terminal": result.get("constructor_terminal"),
                "fixture_errors": fixture_errors,
                "harbor_error": (result["harbor"]["trial_result"] or {}).get("exception_info"),
            })
            self.assertEqual(len(feedback), 1)
            self.assertIsNone(result["harbor"]["trial_result"]["exception_info"])
            self.assertEqual(result["harbor"]["trial_result"]["verifier_result"]["rewards"]["verified"], 1)
            self.assertFalse(result["harbor"]["scientific_trial"])

            best = result["best_verified_result"]
            replayed = verify_stored_result(best, task, trial)
            self.assertEqual(replayed["energy_density"], -0.375)
            (root / "queue.json").write_text(json.dumps({"task_ids": ["11"], "tasks": {
                "11": {"status": "completed", "selected_trial": "trial"}}}))
            row = score_run(root)["rows"][0]
            self.assertEqual(row["energy_density"], -0.375)
            self.assertTrue(row["scientific_complete"])
            bad_result = {**best, "energy_density": 100.0}
            with self.assertRaises(ValueError):
                verify_stored_result(bad_result, task, trial)
