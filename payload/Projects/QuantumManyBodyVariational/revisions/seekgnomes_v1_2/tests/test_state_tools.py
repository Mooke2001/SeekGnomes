"""Regression coverage for the role handoff and score receipt boundaries."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from revisions.seekgnomes_v1_2 import state_tools
from src.benchmark50_models import load_task
from src.state_format import runtime_sha256


PROJECT = Path(__file__).resolve().parents[3]
SOURCE = "def f(P: Word) -> Real:\n    return prod([1 if p == Z else 0 for p in labels(P)])\n"
PROOF = "-- simulated proof source\n"
DEFINITION = "-- simulated generated module\n"
TEMPLATE = "-- simulated proof template\n"
THREAD_STAGE = "compile-generic-StateExamples"
THREAD_ERROR = (b"libc++abi: terminating due to uncaught exception of type lean::exception: "
                b"failed to create thread: Resource temporarily unavailable\n")


def action(tool, **arguments):
    return {"tool": tool, "arguments": arguments}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class StateToolsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="seekgnomes-state-tools-", dir=PROJECT / "tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.task = load_task(13)
        self.calls = []
        self.receipt_mismatch = False
        self.energy_mismatch = False
        self.failed_verification = None
        self.other_report_path = False
        self.verifier_log = None
        self.after_failure = None

    def owner(self):
        return state_tools.StateDefinitionTools(self.task, self.root / "trial", web=False,
                                                callback_factory=self.callbacks)

    def callbacks(self, task, timeout):
        def bindings(source_path):
            self.assertEqual(source_path.name, "f.state")
            self.assertEqual(source_path.read_bytes(), SOURCE.encode())
            return {"source_sha256": sha(source_path.read_bytes()), "runtime_sha256": runtime_sha256(),
                    "generated_state_sha256": sha(DEFINITION.encode()),
                    "lock_sha256": sha(state_tools.LOCK.read_bytes())}

        def prepare(*, source_path, invocation_dir):
            self.calls.append("prepare")
            return {"ok": True, "generated_state": DEFINITION, "proof_template": TEMPLATE, **bindings(source_path)}

        def check(*, source_path, proof_path, invocation_dir, score):
            self.calls.append("score" if score else "lean")
            self.assertEqual(proof_path.read_bytes(), PROOF.encode())
            if self.failed_verification is not None:
                name = "other.json" if self.other_report_path else "manifest.json"
                path = invocation_dir / "verification" / name
                path.parent.mkdir()
                report = {"spec": state_tools.SPEC, "passed": False, "manifest": str(path),
                          **deepcopy(self.failed_verification)}
                if self.verifier_log is not None:
                    stage, log = self.verifier_log
                    log_path = path.parent / f"{stage}.log"
                    log_path.write_bytes(log)
                    report["checks"][stage].setdefault("log", str(log_path))
                    report["checks"][stage].setdefault("log_sha256", sha(log))
                path.write_text(json.dumps(report))
                if self.after_failure is not None:
                    self.after_failure()
                return {"ok": False, "proof_manifest": str(path), "problems": report["problems"]}
            digests = {**bindings(source_path), "candidate_source_sha256": sha(PROOF.encode()),
                       "candidate_export_sha256": sha(b"fake export")}
            if self.receipt_mismatch:
                digests["source_sha256"] = sha(b"wrong source")
            certificate = {**digests, "kind": state_tools.KIND, "theorem": state_tools.THEOREM,
                           "target": state_tools.TARGET, "dimension": task["dimension"],
                           "qubits_per_cell": task["qubits_per_cell"]}
            result = {**digests, "certificate": certificate, "ok": True, "verified": True,
                      "proof_verified": True, "compile_passed": True, "goal_matched": True,
                      "kernel_verified": True}
            if score:
                path = invocation_dir / "energy.json"
                energy = -0.5
                physics_digest = sha(b"physical task")
                result.update(energy_density=energy, energy_normalization="per_qubit",
                              energy_mode="approximate_float", energy_report=str(path),
                              task_physics_sha256=physics_digest)
                path.write_text(json.dumps({"energy_density": 70 if self.energy_mismatch else energy,
                    "energy_normalization": "per_qubit", "mode": "approximate_float",
                    "energy_is_llm_reported": False, "preconditions": {"proof_checked_here": True},
                    "verification": digests, "task_physics_sha256": physics_digest}))
            return result

        def lean(**kwargs):
            return check(**kwargs, score=False)

        def score(**kwargs):
            return check(**kwargs, score=True)

        return prepare, lean, score

    def handoff(self, owner):
        constructor = owner.constructor
        self.assertTrue(constructor.dispatch(action("write", path="f.state", content=SOURCE))["ok"])
        result = constructor.dispatch(action("create_handoff", definition_path="f.state", notes="A product state."))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["definition_sha256"], sha(SOURCE.encode()))
        return result

    def write_proof(self, owner):
        self.assertTrue(owner.formalizer.dispatch(action("write", path="Candidate.lean", content=PROOF))["ok"])

    def test_unfinished_proof_cannot_be_returned_as_an_issue(self):
        owner = self.owner()
        self.handoff(owner)
        for request in (
            action("report_issue", kind="unresolved", detail="A library lemma is missing."),
            action("report_issue", kind="infrastructure", detail="I did not finish the proof."),
            action("finish", reason="Leave the proof incomplete."),
        ):
            with self.subTest(request=request):
                result = owner.formalizer.dispatch(request)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "formalist_proof_required")
                self.assertNotIn("action", result)
        self.assertFalse(list(self.root.rglob("issue.json")))
        self.assertIsNone(owner.last_verified)

    def test_prepare_and_check_use_the_committed_state(self):
        first = self.owner()
        result = self.handoff(first)
        first.constructor.dispatch(action("write", path="f.state", content="Replaced Constructor file"))
        owner = self.owner()  # Recover the committed transaction from disk.
        self.assertEqual(result["definition_reference"], "reference/handoff/f.state")
        frozen = owner.formalizer.dispatch(action("read", path=result["definition_reference"]))
        self.assertEqual(frozen["content"], SOURCE)
        self.assertFalse(owner.formalizer.dispatch(action("write", path="f.state", content=SOURCE))["ok"])
        self.assertFalse(owner.formalizer.dispatch(action("write", path="f.py", content="x"))["ok"])
        self.assertFalse(owner.formalizer.dispatch(action("prepare", source_path="f.state"))["ok"])
        prepared = owner.formalizer.dispatch(action("prepare"))
        self.assertTrue(prepared["ok"], prepared)
        self.assertNotIn("energy_density", prepared)
        generated = owner.formalizer.dispatch(action("read", path=prepared["generated_paths"]["generated_state"]))
        self.assertEqual(generated["content"], DEFINITION)
        self.write_proof(owner)
        checked = owner.formalizer.dispatch(action("run_lean", proof_path="Candidate.lean"))
        self.assertTrue(checked["proof_verified"], checked)
        self.assertNotIn("energy_density", checked)
        self.assertIsNone(owner.last_verified)
        scored = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertTrue(state_tools.is_verified(scored), scored)
        self.assertEqual(scored["action"], "feedback")
        self.assertEqual(scored["energy_density"], -0.5)
        self.assertEqual(owner.last_verified, scored)
        self.assertEqual(self.calls, ["prepare", "prepare", "lean", "prepare", "score"])
        self.assertEqual(set(owner.return_view()), {"f.state", "GeneratedState.lean", "Candidate.lean"})

    def test_invalid_state_does_not_commit(self):
        owner = self.owner()
        constructor = owner.constructor
        constructor.dispatch(action("write", path="f.state", content="def f(P: Word) -> Real:\n    return open('file')\n"))
        result = constructor.dispatch(action("create_handoff", definition_path="f.state"))
        self.assertFalse(result["ok"])
        self.assertFalse(owner.handoffs)
        self.assertFalse(self.calls)

    def test_mismatched_proof_or_energy_has_no_score_feedback(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        self.receipt_mismatch = True
        mismatch = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertEqual(mismatch["error"], "proof_binding_mismatch")
        self.assertNotIn("energy_density", mismatch)
        self.receipt_mismatch = False
        self.energy_mismatch = True
        mismatch = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertFalse(mismatch["ok"])
        self.assertNotIn("energy_density", mismatch)
        self.assertIsNone(owner.last_verified)

    def test_result_predicate_rejects_malformed_validation(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        result = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertTrue(state_tools.is_verified(result), result)
        self.assertFalse(state_tools.is_verified(None))
        self.assertFalse(state_tools.is_verified({**result, "certificate": []}))
        self.assertFalse(state_tools.is_verified({**result, "energy_density": float("nan")}))
        self.assertFalse(state_tools.is_verified({**result, "energy_report_sha256": "a"}))
        for key in ("runtime_sha256", "lock_sha256"):
            changed = deepcopy(result)
            changed[key] = changed["certificate"][key] = "0" * 64
            self.assertFalse(state_tools.is_verified(changed))

    def test_only_trusted_environment_failures_receive_technical_diagnostics(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        cases = (
            ("lock-before", False, False, "lock mismatch", True),
            ("lock-after", False, False, "lock changed", True),
            ("mathlib-revision", False, False, "revision command failed", True),
            ("mathlib-revision", True, False, "mathlib revision differs from frozen revision", True),
            ("mathlib-clean", False, False, "dirty Mathlib", True),
            ("compile-generic-LocalPauli", False, True, "deadline exceeded", True),
            ("compile-generated", False, True, "generated target timeout", False),
            ("compile-candidate", False, False, "proof syntax error", False),
            ("judge", False, False, "wrong target", False),
        )
        for stage, passed, timed_out, reason, technical in cases:
            for tool in ("run_lean", "score"):
                with self.subTest(stage=stage, tool=tool):
                    self.failed_verification = {
                        "checks": {stage: {"passed": passed, "timed_out": timed_out}},
                        "problems": [reason],
                    }
                    self.calls.clear()
                    answer = owner.formalizer.dispatch(action(tool, proof_path="Candidate.lean"))
                    self.assertEqual(self.calls, ["prepare", "lean" if tool == "run_lean" else "score"])
                    self.assertFalse(answer["proof_verified"])
                    self.assertNotIn("energy_density", answer)
                    if technical:
                        self.assertEqual(answer["technical_diagnostic"], {
                            "kind": "strict_verifier_environment_failure", "stage": stage,
                            "reason": reason, "timed_out": timed_out,
                        })
                    else:
                        self.assertNotIn("technical_diagnostic", answer)
        self.assertIsNone(owner.last_verified)

    def thread_creation_failure(self):
        self.failed_verification = {
            "checks": {THREAD_STAGE: {"passed": False, "timed_out": False, "exit_code": 134}},
            "problems": ["Public Lean module failed to compile."],
        }
        self.verifier_log = (THREAD_STAGE, THREAD_ERROR)

    def test_retry_scores_the_same_frozen_submission_after_thread_creation_failure(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        self.thread_creation_failure()

        def after_failure():
            self.assertIsNone(owner.last_verified)
            self.failed_verification = None
            owner.formalizer.dispatch(action("write", path="Candidate.lean", content="-- later edit\n"))

        self.after_failure = after_failure
        answer = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertTrue(state_tools.is_verified(answer), answer)
        self.assertEqual(self.calls, ["prepare", "score", "prepare", "score"])
        replay = Path(answer["invocation_dir"])
        original = Path(answer["retry_of_invocation"])
        self.assertNotEqual(original, replay)
        for filename, frozen in (("f.state", SOURCE), ("Candidate.lean", PROOF)):
            self.assertEqual((original / filename).read_bytes(), frozen.encode())
            self.assertEqual((replay / filename).read_bytes(), frozen.encode())
        failed = json.loads((original / "result.json").read_text())
        self.assertFalse(failed["proof_verified"])
        self.assertTrue(failed["technical_diagnostic"]["retryable"])
        self.assertNotIn("energy_density", failed)
        self.assertEqual(failed["definition_sha256"], answer["definition_sha256"])
        self.assertEqual(failed["proof_sha256"], answer["proof_sha256"])
        self.assertEqual(owner.last_verified, answer)

    def test_persistent_thread_creation_failure_returns_after_one_replay(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        self.thread_creation_failure()
        answer = owner.formalizer.dispatch(action("run_lean", proof_path="Candidate.lean"))
        self.assertEqual(self.calls, ["prepare", "lean", "prepare", "lean"])
        self.assertFalse(answer["proof_verified"])
        self.assertTrue(answer["technical_diagnostic"]["retryable"])
        self.assertNotIn("energy_density", answer)
        self.assertNotIn("action", answer)
        self.assertIsNone(owner.last_verified)

    def test_thread_failure_with_unbound_log_digest_is_not_retried(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        self.thread_creation_failure()
        self.failed_verification["checks"][THREAD_STAGE]["log_sha256"] = "0" * 64
        answer = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertEqual(self.calls, ["prepare", "score"])
        self.assertFalse(answer["proof_verified"])
        self.assertNotIn("retryable", answer["technical_diagnostic"])
        self.assertNotIn("action", answer)

    def test_failure_diagnostic_ignores_evidence_outside_the_fixed_manifest_path(self):
        owner = self.owner()
        self.handoff(owner)
        self.write_proof(owner)
        self.other_report_path = True
        self.failed_verification = {"checks": {"lock-before": {"passed": False}},
                                    "problems": ["lock mismatch"]}
        answer = owner.formalizer.dispatch(action("score", proof_path="Candidate.lean"))
        self.assertFalse(answer["proof_verified"])
        self.assertNotIn("technical_diagnostic", answer)


if __name__ == "__main__":
    unittest.main()
