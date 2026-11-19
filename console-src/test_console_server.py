"""Local-only regression tests for console run state, credentials, and HTTP guards."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

import server
import runner

HERE = Path(__file__).resolve().parent
TASKS = [{"id": tid, "name": "Fixture " + tid, "dimension": 1, "group": "fixture"}
         for tid in sorted(server.TASK_IDS)]
COMPANIES = [{"id": "deepseek", "label": "DeepSeek", "harness": "dsh",
              "protocols": ["chat_completions"], "default_protocol": "chat_completions"}]
KEY = "synthetic-test-key-no-provider-use"
REQUEST = {"company": "deepseek", "model": "example/model", "api_base_url": "https://example.invalid/v1",
           "api_key": KEY, "api_protocol": "chat_completions", "model_context_tokens": 8192,
           "task_ids": ["01", "02", "03", "04", "05"], "budget_mode": "tokens", "budget_limit": 1024,
           "cpus": 2, "web": False, "share": False}


class FixtureBackend:
    def __init__(self):
        self.release = threading.Event()
        self.recovered = []
        self.keys = []

    def execute(self, run_id, output_dir, config, secret, stop, update):
        self.keys.append(secret)
        update("preflight", "Checking fixture.", engine="podman")
        root = output_dir / "one-task"
        (root / "results").mkdir(parents=True)
        ids = config["task_ids"]
        queue = {"schema": "seekgnomes-queue/v1.2", "status": "running", "model": config["model"],
                 "task_ids": ids, "tasks": {tid: {"status": "pending"} for tid in ids}}
        for tid in ids[:config["parallel_tasks"]]:
            queue["tasks"][tid]["status"] = "running"
        server.write_json(root / "queue.json", queue)
        while not self.release.wait(0.01):
            if stop.is_set():
                raise runner.RunStopped
        queue["status"] = "finished_with_technical_gaps"
        for tid in ids:
            queue["tasks"][tid]["status"] = "scientific_failure"
        queue["tasks"][ids[-1]]["status"] = "technical_gap"
        report = {"schema": "seekgnomes-scores/v1.1", "rows": [{"task_id": tid} for tid in ids]}
        server.write_json(root / "queue.json", queue)
        server.write_json(root / "results/scores.json", report)
        return 1

    def recover(self, run_id, engine):
        self.recovered.append((run_id, engine))


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=HERE)
        self.results = Path(self.temp.name)
        self.backend = FixtureBackend()
        self.manager = self.make_manager()
        self.http = server.ConsoleServer(self.manager, HERE / "ui")
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()

    def make_manager(self):
        return server.RunManager(self.results, TASKS, COMPANIES, self.backend, "http://127.0.0.1:9/v1/submissions")

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.manager.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, **headers):
        default = {"Host": self.http.host, "Content-Type": "application/json",
                   "Origin": self.http.origin, "X-SeekGnomes-Token": self.http.csrf}
        default.update(headers)
        connection = http.client.HTTPConnection(self.http.host, timeout=3)
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body), headers=default)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def wait_run(self, state=None, sharing=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            runs = self.manager.list()["runs"]
            if runs and (state is None or runs[0]["status"] == state) and (sharing is None or runs[0]["share_status"] == sharing):
                return runs[0]
            time.sleep(0.01)
        self.fail(f"Run did not settle on {state!r}, sharing {sharing!r}. Last observations: {runs!r}")

    def test_http_rejects_host_origin_token_and_invalid_selection(self):
        self.assertEqual(self.request("GET", "/api/config", Host="other.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", REQUEST, Origin="https://other.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", REQUEST, **{"X-SeekGnomes-Token": "bad"})[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", {**REQUEST, "task_ids": ["01"] * 5})[0], 400)
        self.assertEqual(self.request("POST", "/api/runs", {**REQUEST, "model_context_tokens": None})[0], 400)
        self.assertEqual(self.request("POST", "/api/runs", {**REQUEST, "api_key": "秘密"})[0], 400)
        self.assertEqual(self.request("GET", "/../server.py")[0], 404)
        _, _, config_body = self.request("GET", "/api/config")
        config = json.loads(config_body)
        self.assertEqual(len(config["tasks"]), 50)
        self.assertEqual(config["companies"][0]["default_protocol"], "chat_completions")
        _, _, page = self.request("GET", "/")
        self.assertIn(self.http.csrf.encode(), page)
        self.assertNotIn(b"__SG_CSRF__", page)

    def test_parallel_tasks_require_integer_within_cpu_and_selection_limits(self):
        for value in (0, -1, 1.0, 1.5, True, "1", None, 3):
            with self.subTest(value=value):
                code, _, body = self.request("POST", "/api/runs", {**REQUEST, "parallel_tasks": value})
                self.assertEqual(code, 400)
                self.assertIn("from 1 to 2", json.loads(body)["error"])
        code, _, body = self.request("POST", "/api/runs", {**REQUEST, "cpus": 9, "parallel_tasks": 6})
        self.assertEqual(code, 400)
        self.assertIn("from 1 to 5", json.loads(body)["error"])
        self.assertEqual(self.backend.keys, [])

    def test_parallel_run_reports_every_running_task_and_persists_setting(self):
        code, _, body = self.request("POST", "/api/runs", {**REQUEST, "parallel_tasks": 2})
        self.assertEqual(code, 201)
        started = json.loads(body)
        self.assertEqual(started["parallel_tasks"], 2)
        self.wait_run("running")
        code, _, body = self.request("GET", "/api/runs")
        self.assertEqual(code, 200)
        run, = json.loads(body)["runs"]
        self.assertEqual(run["running_task_ids"], ["01", "02"])
        self.assertEqual(run["message"], "Running tasks 01, 02.")
        self.assertEqual(self.request("POST", f"/api/runs/{started['id']}/stop", {})[0], 202)
        self.assertEqual(self.wait_run("stopped")["running_task_ids"], [])
        self.manager.close()
        self.manager = self.make_manager()
        self.assertEqual(self.wait_run("stopped")["parallel_tasks"], 2)

    def test_result_and_no_default_share_persist_across_restart(self):
        with patch.object(server, "sharing_functions") as sharing:
            code, _, body = self.request("POST", "/api/runs", REQUEST)
            self.assertEqual(code, 201)
            run_id = json.loads(body)["id"]
            running = self.wait_run("running")
            self.assertEqual(running["parallel_tasks"], 1)
            self.assertEqual(running["running_task_ids"], ["01"])
            self.assertEqual(self.request("POST", "/api/runs", REQUEST)[0], 409)
            self.backend.release.set()
            run = self.wait_run("finished_with_technical_gaps")
            self.assertEqual((run["completed"], run["selected"]), (4, 5))
            sharing.assert_not_called()
        self.assertEqual(self.backend.keys, [KEY])
        self.manager.close()
        path = self.results / run_id / "state.json"
        legacy_state = json.loads(path.read_bytes())
        self.assertEqual(legacy_state.pop("parallel_tasks"), 1)
        server.write_json(path, legacy_state)
        self.manager = self.make_manager()
        self.http.manager = self.manager
        run = self.wait_run("finished_with_technical_gaps", "not_requested")
        self.assertTrue(run["results_available"])
        self.assertEqual(run["parallel_tasks"], 1)
        code, headers, data = self.request("GET", f"/api/runs/{run_id}/results")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="scores.json"')
        self.assertEqual(data, (self.manager.campaign_dir(run_id) / "results/scores.json").read_bytes())
        for path in self.results.rglob("*.json"):
            self.assertNotIn(KEY.encode(), path.read_bytes())

    def test_opt_in_failed_send_retry_keeps_same_payload(self):
        envelope = {"sha256": "a" * 64}
        payloads = []

        def build(run, settings, secret):
            self.assertEqual(secret, KEY)
            self.assertEqual(settings["task_ids"], REQUEST["task_ids"])
            self.assertTrue((run / "results/scores.json").is_file())
            return envelope

        def send(endpoint, payload):
            payloads.append((endpoint, payload))
            if len(payloads) == 1:
                raise RuntimeError("Fixture network failure")
            return {"submission_id": envelope["sha256"], "status": "pending", "duplicate": True}

        with patch.object(server, "sharing_functions", return_value=(build, send)):
            code, _, body = self.request("POST", "/api/runs", {**REQUEST, "share": True})
            self.assertEqual(code, 201)
            run_id = json.loads(body)["id"]
            self.backend.release.set()
            self.wait_run("finished_with_technical_gaps", "failed")
            self.manager.close()
            self.manager = self.make_manager()
            self.http.manager = self.manager
            self.assertEqual(len(payloads), 1)
            self.assertEqual(self.request("POST", f"/api/runs/{run_id}/retry-share", {})[0], 202)
            receipt = self.wait_run("finished_with_technical_gaps", "sent")["receipt"]
            self.assertTrue(receipt["duplicate"])
            self.assertEqual(payloads[0], payloads[1])
            self.assertEqual(self.request("POST", f"/api/runs/{run_id}/retry-share", {})[0], 409)

    def test_stop_and_restart_do_not_dispatch_again(self):
        started = self.manager.start(REQUEST)
        run_id = started["id"]
        self.wait_run("running")
        self.assertEqual(self.request("POST", f"/api/runs/{run_id}/stop", {})[0], 202)
        self.wait_run("stopped")
        self.manager.close()
        path = self.results / run_id / "state.json"
        stale = json.loads(path.read_bytes())
        stale.update(status="preparing", engine="podman")
        server.write_json(path, stale)
        self.manager = self.make_manager()
        self.wait_run("interrupted")
        self.assertEqual(self.backend.recovered, [(run_id, "podman")])
        self.assertEqual(self.backend.keys, [KEY])

    def test_container_flags_and_args_keep_credential_out(self):
        settings, key = server.normalize_request({**REQUEST, "parallel_tasks": 2}, self.manager.company_by_id)
        (self.results / "source-manifest.json").write_text("{}")
        backend = runner.ContainerRunner(self.results, HERE / "runtime")
        argv = backend._argv("docker", "/usr/bin/docker", "sha256:" + "a" * 64,
                             "f" * 32, self.results, settings)
        self.assertIn("--read-api-key-stdin", argv)
        self.assertIn("--no-web", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("--pull=never", argv)
        self.assertIn("none", argv)
        self.assertEqual(argv[argv.index("--cpus") + 1], "2")
        self.assertIn("--cpus=2", argv)
        self.assertIn("--parallel-tasks=2", argv)
        self.assertTrue(any(value.startswith("--source-manifest-sha256=") for value in argv))
        self.assertTrue(all(key not in value and "SEEKGNOMES_API_KEY" not in value for value in argv))
        with patch.dict(os.environ, {"SEEKGNOMES_API_KEY": key}):
            self.assertNotIn("SEEKGNOMES_API_KEY", runner.command_env())


if __name__ == "__main__":
    unittest.main()
