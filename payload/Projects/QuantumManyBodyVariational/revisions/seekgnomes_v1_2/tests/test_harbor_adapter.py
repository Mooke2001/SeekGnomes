import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from revisions.seekgnomes_v1_2.gateway import RoleGateway, require_live_parent
from revisions.seekgnomes_v1_2.harbor_adapter import (
    PROJECT, SeekGnomesAgent, SeekGnomesTechnicalError, _open_seal, _rewards, _seal,
    _supervise_native, run_fixture_smoke,
)


def _killed_controller(writer, settings, key, parent_pid):
    require_live_parent(parent_pid)
    writer.send(("ready", None))
    out = settings["out"]
    if not settings["resume"]:
        out.mkdir()
        (out / "checkpoint.txt").write_text("same-context")
        os.kill(os.getpid(), signal.SIGKILL)
    writer.send(("result", {"status": "completed", "checkpoint": (out / "checkpoint.txt").read_text()}))


def _waiting_controller(writer, settings, key, parent_pid):
    require_live_parent(parent_pid)
    writer.send(("ready", None))
    out = settings["out"]
    out.mkdir()
    (out / "worker-pid.txt").write_text(str(os.getpid()))
    while True:
        signal.pause()


def _rejected_controller(writer, settings, key, parent_pid):
    writer.send(("ready", None))
    writer.send(("error", "ValueError"))


class HostReceiptTests(unittest.TestCase):
    def test_host_seal_rejects_modified_state(self):
        key = secrets.token_bytes(32)
        original = {"task_id": "02", "model": "fixture", "result": {"status": "completed"}}
        sealed = _seal(original, key)
        self.assertEqual(_open_seal(sealed, key), original)
        sealed["payload"]["result"]["status"] = "scientific_failure"
        with self.assertRaises(ValueError):
            _open_seal(sealed, key)

    def test_technical_missing_is_not_zero(self):
        with self.assertRaises(SeekGnomesTechnicalError):
            _rewards({"task_id": "02", "model": "fixture", "result": {
                "task_id": "02", "model": "fixture", "status": "infrastructure_failure",
                "best_verified_result": None}})

    def test_claimed_verified_without_fixed_goal_is_not_accepted(self):
        with self.assertRaises(SeekGnomesTechnicalError):
            _rewards({"task_id": "02", "model": "fixture", "result": {
                "task_id": "02", "model": "fixture", "status": "completed",
                "best_verified_result": {"ok": True, "verified": True,
                                         "energy_density": -100.0}}})

    def test_actual_harbor_job_pipeline_and_no_secret_in_configs(self):
        result = run_fixture_smoke()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["scientific_trial"])
        self.assertEqual(result["external_api_calls"], 0)
        self.assertTrue(result["checks"]["exported_verifier_script_executed"])
        self.assertTrue(result["checks"]["full_model_route_preserved"])
        root = Path(result["evidence"])
        configs = list(root.rglob("config.json"))
        self.assertTrue(configs)
        for path in configs:
            self.assertNotIn("offline-not-an-api-key", path.read_text())


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_harbor_cancellation_reaches_controller_supervisor(self):
        agent = SeekGnomesAgent.__new__(SeekGnomesAgent)
        agent.run_config = {"out": str(PROJECT / "runs/unused-cancellation-fixture")}
        agent.call_id = None
        work = AsyncMock(side_effect=asyncio.CancelledError)
        with patch.dict(os.environ, {"SEEKGNOMES_API_KEY": "test-private-key"}), patch(
                "revisions.seekgnomes_v1_2.harbor_adapter._supervise_native", new=work):
            with self.assertRaises(asyncio.CancelledError):
                await agent.run("unused", SimpleNamespace(root=PROJECT), None)
        work.assert_awaited_once()


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="v12-controller-test-", dir=PROJECT / "runs")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "trial"
        self.key = "offline-private-controller-capability"

    async def test_sigkill_restarts_existing_trial_instead_of_allocating_again(self):
        log = self.root / "restarts.json"
        result = await asyncio.wait_for(_supervise_native({"out": self.out}, self.key,
            worker=_killed_controller, restart_log=log), timeout=15)
        self.assertEqual(result, {"status": "completed", "checkpoint": "same-context"})
        self.assertEqual(json.loads(log.read_text())["controller_interruptions"],
                         [{"exit": -signal.SIGKILL, "checkpoint_exists": True}])

    async def test_cancellation_kills_managed_controller_and_blocks_reattachment(self):
        running = asyncio.create_task(_supervise_native({"out": self.out}, self.key, worker=_waiting_controller))
        started = self.out / "worker-pid.txt"

        async def wait_for_launch():
            while not started.exists() or not started.read_text():
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(wait_for_launch(), timeout=10)
        finally:
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        with self.assertRaises(ProcessLookupError):
            os.kill(int(started.read_text()), 0)
        self.assertEqual(json.loads((self.out / "controller-cancelled.json").read_text())["status"],
                         "cancelled_by_operator")
        with self.assertRaisesRegex(SeekGnomesTechnicalError, "cancelled"):
            await _supervise_native({"out": self.out}, self.key, resume=True, worker=_waiting_controller)

    async def test_controller_typed_exception_is_terminal(self):
        log = self.root / "restarts.json"
        with self.assertRaisesRegex(SeekGnomesTechnicalError, "ValueError"):
            await asyncio.wait_for(_supervise_native({"out": self.out}, self.key,
                worker=_rejected_controller, restart_log=log), timeout=10)
        self.assertFalse(log.exists())

    def test_gateway_resume_replaces_only_a_socket(self):
        self.out.mkdir()
        path = self.out / "broker.sock"
        fd = os.open(self.out, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(f"/proc/self/fd/{fd}/broker.sock")
        finally:
            os.close(fd)
        kwargs = {"role": "constructor", "model": "fixture", "endpoint": "http://localhost",
                  "api_key": self.key, "protocol": "responses", "ledger": None, "tool_dispatch": None}
        with self.assertRaises(OSError):
            RoleGateway(self.out, **kwargs)
        active = RoleGateway(self.out, resume=True, **kwargs).start()
        active.close()
        self.assertFalse(path.exists())
        path.write_text("not a socket")
        with self.assertRaisesRegex(ValueError, "Unsafe checkpoint broker socket"):
            RoleGateway(self.out, resume=True, **kwargs)
        self.assertEqual(path.read_text(), "not a socket")


if __name__ == "__main__":
    unittest.main()
