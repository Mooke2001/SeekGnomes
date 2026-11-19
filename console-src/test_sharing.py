"""Offline fixtures for export bindings and a loopback pending intake."""
import base64
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from .receiver import IntakeServer, PendingStore
from .sharing import (CERT_NAMES, FLAGS, HARNESS_FIELDS, MAX_SUBMISSION_BYTES, ROUTE, ROW_NUMBERS, SCHEMA,
                      STAGES, STATE_CERT_NAMES, STATE_PROTOCOL, SharingError, build_submission,
                      canonical_json, send_submission, sha256, validated_submission_id)


PRIVATE_URL = "https://private-model.example.invalid/v1"
SECRET = "fixture-credential-with-special-characters!"
TRIAL = "tasks/01/attempt-1"
PROOF_MANIFEST = "formalizer/handoff-0001/invocations/0001-score/verification/manifest.json"
SOURCE = b"def f(word):\r\n    return 1 if not word else 0\r\n"
PROOF = b"import GeneratedState\n-- example proof bytes, never evaluated by this test\n"


class SharingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "run"

    def write(self, relative, value):
        path = self.run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else canonical_json(value))

    def fixture(self, *, strict=True, technical_gap=False, budget_mode="tokens", source=SOURCE, state=False):
        task = {"id": "01", "dimension": 1, "qubits_per_cell": 1}
        digest = sha256(json.dumps(task, sort_keys=True).encode())
        tag = sha256(b"lock")
        protocol = STATE_PROTOCOL if state else "seekgnomes-harbor/v1.2.0+console-budget-v1"
        ids = ["01", "02"] if technical_gap else ["01"]
        model = "fixture/model-1[1m]"
        active = 5 if budget_mode == "time" else None
        self.write("plan.json", {
            "protocol": protocol, "execution_framework": "Harbor", "harbor_version": "0.22.0",
            "model": model, "api_base_url": PRIVATE_URL, "created_utc": "2026-11-18T00:00:00+00:00",
            "task_ids": ids, "task_sha256": {task_id: digest for task_id in ids},
            "proof_lock_sha256": tag, "web_enabled": False, "completion_token_field": "max_tokens",
            "model_context_tokens": None, "harness_selection": {field: "fixture" for field in HARNESS_FIELDS},
            "resources": {"budget_mode": budget_mode, "constructor_output_tokens": 10 if active is None else None,
                          "constructor_input_tokens": None, "constructor_active_seconds": active, "cpus": 2,
                          "closing_input_tokens": None, "closing_output_tokens": 50000, "closing_allocations": 1,
                          "closing_active_seconds": active, "api_requests_limit": None, "budget_expansion": False},
        })
        status = "completed" if strict else "scientific_failure"
        other_status = "finished_with_technical_gaps" if technical_gap else "finished"
        queue_tasks = {"01": {"status": status, "selected_trial": TRIAL}}
        if technical_gap:
            queue_tasks["02"] = {"status": "technical_gap", "selected_trial": None}
        self.write("queue.json", {"model": model, "task_ids": ids, "status": other_status, "tasks": queue_tasks,
                                  "ended_utc": "2026-11-18T00:02:00+00:00"})
        energy = -1.5 if strict else None
        first = {"task_id": "01", "group": "generic", "status": status, "selected_trial": TRIAL,
                 "scientific_complete": True, "score_complete": True, "energy_density": energy,
                 "censored_verified_energy": None, "pass_rate": 1.0 if strict else 0.0,
                 "improvement_rate": 0.0, "amplitude": 0.0, "score": 30.0 if strict else 0.0}
        rows = [first]
        if technical_gap:
            rows.append({"task_id": "02", "group": "generic", "status": "technical_gap", "selected_trial": None,
                         "scientific_complete": False, "score_complete": False, **dict.fromkeys(ROW_NUMBERS)})
        if strict:
            first["proof_manifest_relative"] = PROOF_MANIFEST
        groups = {"generic": {"catalogue_tasks": 33, "selected_tasks": len(ids), "completed_tasks": 1,
                              "scored_tasks": 1, "score": None,
                              "selected_subset_score": None if technical_gap else first["score"]},
                  "exactly_solvable": {"catalogue_tasks": 17, "selected_tasks": 0, "completed_tasks": 0,
                                       "scored_tasks": 0, "score": None, "selected_subset_score": None}}
        self.write("results/scores.json", {"schema": "seekgnomes-scores/v1.1", "energy_mode": "approximate_float",
                                          "floating_point_certified": False, "rules_sha256": sha256(b"rules"),
                                          "task_groups_sha256": sha256(b"groups"), "rows": rows, "groups": groups})
        if strict:
            checker = Path(PROOF_MANIFEST).parent
            source_path = checker.parent / ("f.state" if state else "Source.py")
            proof_path = checker.parent / "Candidate.lean"
            self.write(Path(TRIAL) / source_path, source)
            self.write(Path(TRIAL) / proof_path, PROOF)
            self.write(Path(TRIAL) / checker / "trusted" / ("source.state" if state else "source.py"), source)
            self.write(Path(TRIAL) / checker / "trusted/Submitted.lean", PROOF)
            certificate = {**(STATE_CERT_NAMES if state else CERT_NAMES),
                           "lock_sha256": tag, "source_sha256": sha256(source),
                           "candidate_source_sha256": sha256(PROOF), "generated_state_sha256": sha256(b"generated"),
                           "candidate_export_sha256": sha256(b"export"), "dimension": 1, "qubits_per_cell": 1,
                           "series_terms": 12, "sqrt_bits": 16, "candidate_definition_holes": [],
                           "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"]}
            receipt = {**dict.fromkeys(FLAGS, True), "certificate": certificate, "source_sha256": sha256(source),
                       "proof_sha256": sha256(PROOF), "energy_density": energy,
                       "source_path_relative": str(source_path), "proof_path_relative": str(proof_path),
                       "proof_manifest_relative": PROOF_MANIFEST}
            state_hashes = {}
            if state:
                certificate.update(runtime_sha256=sha256(b"state evaluator"), precision_bits=40)
                state_hashes = {name: certificate[name] for name in
                                ("lock_sha256", "generated_state_sha256", "runtime_sha256")}
                energy_bytes = canonical_json({"energy_density": energy, "verification": {
                    **state_hashes, "source_sha256": sha256(source),
                    "candidate_source_sha256": sha256(PROOF),
                }})
                energy_path = checker.parent / "energy.json"
                self.write(Path(TRIAL) / energy_path, energy_bytes)
                receipt.update(**state_hashes, energy_report_relative=str(energy_path),
                               energy_report_sha256=sha256(energy_bytes))
            self.write(f"{TRIAL}/task.json", task)
            self.write(f"{TRIAL}/manifest.json", {"task_id": "01", "task_sha256": digest, "model": model,
                                                  "protocol": protocol, "budget_multiplier": 1,
                                                  "best_verified_result": receipt})
            self.write(f"{TRIAL}/{PROOF_MANIFEST}", {**dict.fromkeys(FLAGS, True), "passed": True,
                                                    "certificate": certificate, "candidate_definition_holes": [],
                                                    "source_sha256": sha256(source),
                                                    "candidate_source_sha256": sha256(PROOF),
                                                    **state_hashes,
                                                    "checks": {stage: {"passed": True} for stage in STAGES}})

    def test_export_exact_bytes_and_allowlisted_metadata(self):
        self.fixture()
        envelope = build_submission(self.run, {"api_base_url": PRIVATE_URL}, secret=SECRET)
        self.assertEqual(envelope["schema"], SCHEMA)
        self.assertEqual(envelope, build_submission(self.run, {}, secret=SECRET))
        result = envelope["payload"]["strict_results"][0]
        self.assertEqual(base64.b64decode(result["files"]["f.py"]), SOURCE)
        self.assertEqual(base64.b64decode(result["files"]["Candidate.lean"]), PROOF)
        published = canonical_json(envelope).decode()
        for private in (SECRET, PRIVATE_URL, str(self.root), TRIAL, PROOF_MANIFEST):
            self.assertNotIn(private, published)

    def test_no_results_and_technical_gap_respect_null_scores(self):
        self.fixture(strict=False, technical_gap=True, budget_mode="time")
        payload = build_submission(self.run, {}, secret=SECRET)["payload"]
        self.assertEqual(payload["strict_results"], [])
        self.assertEqual(payload["run"]["limits"]["budget_mode"], "time")
        self.assertIsNone(payload["run"]["limits"]["constructor_output_tokens"])
        self.assertIsNone(payload["summary"]["rows"][1]["score"])

    def test_state_certificate_exports_frozen_source_and_proof_and_binds_energy(self):
        source = b"def f(P: Word) -> Real:\n    return 1 if len(factors(P)) == 0 else 0\n"
        self.fixture(state=True, source=source)
        envelope = build_submission(self.run, {}, secret=SECRET)
        self.assertEqual(validated_submission_id(envelope), envelope["sha256"])
        record = envelope["payload"]["strict_results"][0]
        self.assertEqual(set(record["files"]), {"f.state", "Candidate.lean"})
        self.assertEqual(base64.b64decode(record["files"]["f.state"]), source)
        self.assertEqual(record["certificate"]["runtime_sha256"], sha256(b"state evaluator"))
        energy = Path(TRIAL) / Path(PROOF_MANIFEST).parents[1] / "energy.json"
        self.write(energy, b'{"energy_density": 0}')
        with self.assertRaises(SharingError):
            build_submission(self.run, {}, secret=SECRET)

    def test_parallel_shard_export_binds_limits_and_certified_artifacts(self):
        self.fixture(technical_gap=True)
        parallel_trial = Path("shards/01") / TRIAL
        (self.run / parallel_trial).parent.mkdir(parents=True)
        (self.run / TRIAL).rename(self.run / parallel_trial)
        plan = json.loads((self.run / "plan.json").read_text())
        plan["parallel_trials"] = 2
        plan["resources"]["cpus_per_task"] = 1
        self.write("plan.json", plan)
        queue = json.loads((self.run / "queue.json").read_text())
        queue["tasks"]["01"]["selected_trial"] = str(parallel_trial)
        self.write("queue.json", queue)
        scores = json.loads((self.run / "results/scores.json").read_text())
        scores["rows"][0]["selected_trial"] = str(parallel_trial)
        self.write("results/scores.json", scores)

        payload = build_submission(self.run, {"parallel_tasks": 2})["payload"]
        self.assertEqual(payload["run"]["limits"]["parallel_tasks"], 2)
        self.assertEqual(payload["run"]["limits"]["cpus_per_task"], 1)
        self.assertEqual(base64.b64decode(payload["strict_results"][0]["files"]["f.py"]), SOURCE)
        self.assertIsNone(payload["summary"]["rows"][1]["score"])

        with self.assertRaises(SharingError):
            build_submission(self.run, {"parallel_tasks": 1})
        plan["resources"]["cpus_per_task"] = 2
        self.write("plan.json", plan)
        with self.assertRaises(SharingError):
            build_submission(self.run, {"parallel_tasks": 2})

    def test_false_or_cancelled_selection_is_not_exportable(self):
        self.fixture()
        queue = json.loads((self.run / "queue.json").read_text())
        queue["status"] = "cancelled"
        self.write("queue.json", queue)
        with self.assertRaisesRegex(SharingError, "normally finished"):
            build_submission(self.run, {})

    def test_altered_and_symlinked_source_are_not_exported(self):
        self.fixture()
        path = Path(TRIAL) / Path(PROOF_MANIFEST).parents[1] / "Source.py"
        self.write(path, SOURCE + b"\n")
        with self.assertRaises(SharingError):
            build_submission(self.run, {})
        (self.run / path).unlink()
        elsewhere = self.root / "different.py"
        elsewhere.write_bytes(SOURCE)
        (self.run / path).symlink_to(elsewhere)
        with self.assertRaises(SharingError):
            build_submission(self.run, {})

    def test_credential_and_model_url_inside_artifact_are_rejected(self):
        for value in (SECRET, PRIVATE_URL):
            with self.subTest(value=value):
                self.fixture(source=SOURCE + ("# " + value + "\n").encode())
                with self.assertRaisesRegex(SharingError, "private value"):
                    build_submission(self.run, {}, secret=SECRET)

    def test_loopback_intake_persists_raw_once_even_on_retry(self):
        self.fixture(technical_gap=True)
        envelope = build_submission(self.run, {}, secret=SECRET)
        database = self.root / "pending.sqlite3"
        with IntakeServer(("127.0.0.1", 0), PendingStore(database)) as server:
            serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            serving.start()
            host, port = server.server_address
            try:
                address = f"http://{host}:{port}{ROUTE}"
                first = send_submission(address, envelope)
                duplicate = send_submission(address, envelope)
                self.assertEqual(first, {"status": "pending", "submission_id": envelope["sha256"], "duplicate": False})
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(duplicate["submission_id"], first["submission_id"])
                forged = copy.deepcopy(envelope)
                forged["payload"]["strict_results"][0]["files"]["f.py"] = base64.b64encode(b"change").decode()
                self.assertEqual(self.post_raw(host, port, canonical_json(forged), envelope["sha256"]), 400)
                self.assertEqual(self.post_raw(host, port, b"{}", envelope["sha256"],
                                              length=MAX_SUBMISSION_BYTES + 1), 413)
                self.assertEqual(self.post_raw(host, port, b'{"same":0,"same":1}', envelope["sha256"]), 400)
            finally:
                server.shutdown()
                serving.join()
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT status, raw_payload FROM submissions").fetchall(),
                             [("pending", canonical_json(envelope))])
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)

    def test_non_loopback_http_and_embedded_credentials_are_rejected_without_network(self):
        self.fixture(strict=False)
        envelope = build_submission(self.run, {})
        for address in ("http://example.invalid/v1/submissions",
                        "https://user:secret@example.invalid/v1/submissions",
                        "https://example.invalid/v1/submissions?api_key=secret"):
            with self.subTest(address=address), self.assertRaises(SharingError):
                send_submission(address, envelope)

    def post_raw(self, host, port, raw, key, *, length=None):
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("POST", ROUTE, body=raw, headers={"Content-Type": "application/json", "Idempotency-Key": key,
                                                         "Content-Length": str(len(raw) if length is None else length)})
            with conn.getresponse() as response:
                response.read()
                return response.status
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
