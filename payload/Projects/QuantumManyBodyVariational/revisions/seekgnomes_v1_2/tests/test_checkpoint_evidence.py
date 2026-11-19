"""Check recovery of frozen host evidence without calling a model or Lean."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2 import checkpoint_evidence, state_tools
from src.benchmark50_models import load_task


PROJECT = Path(__file__).resolve().parents[3]
SOURCE = "def f(P: Word) -> Real:\n    return prod([1 if p == Z else 0 for p in labels(P)])\n"


def action(tool, **arguments):
    return {"tool": tool, "arguments": arguments}


class CheckpointEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="checkpoint-evidence-", dir=PROJECT / "tmp")
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        self.task = load_task(13)
        self.trial = self.reopen()
        constructor = self.trial.tools.constructor
        self.assertTrue(constructor.dispatch(action("write", path="f.state", content=SOURCE))["ok"])
        self.handoff = constructor.dispatch(action("create_handoff", definition_path="f.state"))
        self.assertTrue(self.handoff["ok"], self.handoff)

    def reopen(self):
        tools = state_tools.StateDefinitionTools(self.task, self.out, web=False)
        return SimpleNamespace(out=self.out, task=self.task, tools=tools,
                               candidate_format="state", is_verified=state_tools.is_verified)

    def prepare(self):
        result = self.trial.tools.formalizer.dispatch(action("prepare"))
        self.assertTrue(result["ok"], result)
        return result

    def test_mid_invocation_restores_reference_and_serial(self):
        prepared = self.prepare()
        base = self.trial.tools.formalizer
        interrupted = base._invocation("score")
        (interrupted / "result.json").write_text('{"proof_verified":')
        restored = self.reopen()
        formalist = restored.tools.formalizer
        self.assertIsNone(checkpoint_evidence.restore_formalist(restored, formalist))
        generated = formalist.dispatch(action("read", path=prepared["generated_paths"]["generated_state"]))
        self.assertTrue(generated["ok"], generated)
        self.assertEqual(formalist._invocation("prepare").name, "0003-prepare")

    def test_committed_issue_and_partial_return_view(self):
        self.prepare()
        note = "The definition omits what happens to an empty word."
        self.assertTrue(self.trial.tools.formalizer.dispatch(
            action("report_issue", kind="definition_ambiguous", detail=note))["ok"])
        directory = self.out / "returns/handoff-0001"
        directory.mkdir(parents=True)
        (directory / "f.state").write_text(SOURCE)
        restored = self.reopen()
        receipt = checkpoint_evidence.restore_formalist(restored, restored.tools.formalizer)
        self.assertEqual(receipt["session_status"], "completed")
        self.assertEqual(receipt["detail"], note)
        self.assertFalse(receipt["proof_verified"])
        index = checkpoint_evidence.return_view(restored)
        self.assertEqual(set(index), {"f.state", "GeneratedState.lean"})
        self.assertEqual(checkpoint_evidence.return_view(restored), index)
        restored = self.reopen()
        checkpoint_evidence.restore_constructor_returns(restored)
        answer = restored.tools.constructor.dispatch(action("read", path=index["f.state"]["path"]))
        self.assertEqual(answer["content"], SOURCE)
        (directory / "f.state").write_text("changed")
        with self.assertRaisesRegex(ValueError, "digest changed"):
            checkpoint_evidence.restore_constructor_returns(restored)

    def test_committed_score_uses_frozen_proof_and_full_stored_verification(self):
        prepared = self.prepare()
        base = self.trial.tools.formalizer
        snapshot = base._invocation("score")
        frozen_proof = b"-- checkpoint proof fixture\n"
        source = snapshot / "f.state"
        source.write_text(SOURCE)
        for name in ("GeneratedState.lean", "Candidate.lean.txt"):
            data = base.generated[f"generated/{self.handoff['definition_sha256']}/{name}"]
            (snapshot / name).write_text(data)
        proof = snapshot / "Candidate.lean"
        proof.write_bytes(frozen_proof)
        digests = {key: prepared[key] for key in ("source_sha256", "generated_state_sha256",
                                                  "lock_sha256", "runtime_sha256")}
        digests.update(candidate_source_sha256=state_tools._sha(frozen_proof),
                       candidate_export_sha256=state_tools._sha(b"checkpoint export fixture"))
        certificate = {**digests, "kind": state_tools.KIND, "theorem": state_tools.THEOREM,
                       "target": state_tools.TARGET}
        result = {**digests, **dict.fromkeys(state_tools._ACCEPTANCE_FLAGS, True),
                  "certificate": certificate, "action": "feedback", "handoff_id": 1,
                  "definition_sha256": self.handoff["definition_sha256"],
                  "proof_sha256": digests["candidate_source_sha256"],
                  "invocation_dir": str(snapshot), "source_path": str(source), "proof_path": str(proof),
                  "proof_manifest": str(snapshot / "verification/manifest.json"),
                  "energy_report": str(snapshot / "energy.json"),
                  "energy_report_sha256": state_tools._sha(b"checkpoint energy fixture"),
                  "energy_density": -0.5, "energy_mode": "approximate_float",
                  "energy_normalization": "per_qubit"}
        (snapshot / "result.json").write_text(json.dumps(result))
        base.dispatch(action("write", path="Candidate.lean", content="-- lost work\n"))
        restored = self.reopen()
        formalist = restored.tools.formalizer
        with patch.object(checkpoint_evidence, "verify_stored_result") as verify:
            receipt = checkpoint_evidence.restore_formalist(restored, formalist)
            checked = {**result, "proof_manifest_relative": str(
                (snapshot / "verification/manifest.json").relative_to(self.out))}
            verify.assert_called_once_with(checked, self.task, self.out)
        self.assertEqual(receipt["session_status"], "completed")
        self.assertEqual(formalist.last_verified, result)
        self.assertEqual(formalist.return_files["Candidate.lean"], frozen_proof)
        with patch.object(checkpoint_evidence, "verify_stored_result", side_effect=ValueError("bad certificate")):
            with self.assertRaisesRegex(ValueError, "bad certificate"):
                checkpoint_evidence.restore_formalist(restored, formalist)


if __name__ == "__main__":
    unittest.main()
