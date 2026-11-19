"""Real Harbor Job/Trial integration around the native two-role runtime.

Harbor's custom environment below holds trusted orchestration artifacts. It is
not the native agent's execution sandbox: runtime.run_trial creates the actual
per-role bubblewrap namespaces. Model-controlled commands are never delegated
to this environment. Harbor's custom verifier consumes a host-sealed result,
not a model-written reward file.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import errno
import fcntl
import hashlib
import hmac
import importlib.metadata
import harbor
import inspect
import json
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import secrets
import signal
import stat
import sys
from typing import Any
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.agent.context import AgentContext
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

PROJECT = Path(__file__).resolve().parents[2]
EUREKA = PROJECT.parents[1]
PREFIX = EUREKA / "Environment/Python/miniconda3"
HARBOR_VERSION = "0.22.0"
VERSION = "1.3.0"
_CALLS: dict[str, dict[str, Any]] = {}
_SCIENTIFIC = frozenset({"completed", "final_submission_not_verified", "scientific_failure"})


class SeekGnomesTechnicalError(RuntimeError):
    """A missing technical result must not become a scientific zero reward."""


class SeekGnomesRecoveryError(SeekGnomesTechnicalError):
    """An unusable checkpoint must not trigger a replacement allocation."""


def _canonical(path, *, existing=False):
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(PROJECT) or path.resolve() != path:
        raise ValueError("Harbor output must remain in a canonical project-local path")
    if existing and not path.exists():
        raise FileNotFoundError("Required host artifact is unavailable")
    return path


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()


def _write(path, value):
    path = _canonical(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _regular(path):
    path = _canonical(path, existing=True)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024**2:
        raise ValueError("Expected a bounded single-link host artifact")
    with path.open("rb") as stream:
        return stream.read()


def _controller_worker(writer, settings, key, parent_pid):
    """Launch a host-only controller with credentials passed over spawn IPC."""
    try:
        from .gateway import require_live_parent
        require_live_parent(parent_pid)
        # The supervisor records operator cancellation; a worker signal on its
        # own is an interruption and leaves the checkpoint resumable.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        from .runtime import run_trial
        writer.send(("ready", None))
        writer.send(("result", run_trial(**settings, api_key=key)))
    except BaseException as error:
        # Never serialize provider exception text or traceback.
        writer.send(("error", type(error).__name__))
    finally:
        writer.close()


async def _controller_attempt(settings, key, worker):
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(writer, settings, key, os.getpid()))
    ready = False
    reply = None
    try:
        process.start()
        writer.close()
        while True:
            if reader.poll():
                try:
                    kind, value = reader.recv()
                except EOFError:
                    break
                if kind == "ready":
                    ready = True
                    continue
                reply = kind, value
                break
            if not process.is_alive():
                # A worker's last write can arrive between poll and exit.
                if not reader.poll():
                    break
                continue
            await asyncio.sleep(0.05)
    finally:
        writer.close()
        reader.close()
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()
        exit_code = process.exitcode
        process.close()
    if reply is not None:
        return reply
    if not ready and exit_code is not None and 0 <= exit_code < 128:
        return "error", "ControllerBootstrapFailure"
    return "interrupted", exit_code


async def _supervise_native(settings, key, *, resume=False, worker=_controller_worker,
                            restart_log=None):
    """Restart a killed controller inside the same Harbor trial and budget."""
    out = _canonical(settings["out"])
    lock = out.parent / ("." + out.name + ".supervisor.lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Unsafe native controller supervisor lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cancelled = out / "controller-cancelled.json"
        if cancelled.exists() or cancelled.is_symlink():
            raise SeekGnomesTechnicalError("An operator cancelled this trial")
        interruptions = []
        try:
            while True:
                if out.exists() and not resume:
                    raise ValueError("A fresh native trial output path is required")
                options = {**settings, "resume": resume and out.exists()}
                try:
                    kind, value = await _controller_attempt(options, key, worker)
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.ENOMEM):
                        raise
                    kind, value = "interrupted", type(error).__name__
                if kind == "result":
                    return value
                if kind != "interrupted":
                    error_type = SeekGnomesRecoveryError if options["resume"] else SeekGnomesTechnicalError
                    raise error_type("Native controller raised " + str(value))
                interruptions.append({"exit": value, "checkpoint_exists": out.exists()})
                if restart_log is not None:
                    _write(restart_log, {"controller_interruptions": interruptions})
                resume = True
                await asyncio.sleep(2 ** min(len(interruptions) - 1, 5))
        except asyncio.CancelledError:
            if out.is_dir():
                _write(cancelled, {"status": "cancelled_by_operator"})
            raise
    finally:
        os.close(fd)


def _seal(payload, key):
    return {"schema": "seekgnomes-harbor-host-receipt/v1", "payload": payload,
            "hmac_sha256": hmac.new(key, _json(payload), hashlib.sha256).hexdigest()}


def _open_seal(envelope, key):
    if not isinstance(envelope, dict) or envelope.get("schema") != "seekgnomes-harbor-host-receipt/v1":
        raise ValueError("A host-sealed SeekGnomes receipt is required")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Malformed host receipt")
    expected = hmac.new(key, _json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(envelope.get("hmac_sha256", "")), expected):
        raise ValueError("Host receipt authentication failed")
    return payload


def _rewards(payload):
    from src.benchmark50_models import load_task
    from revisions.seekgnomes_v1_1_1.scoring import score_parameters, task_metrics
    from revisions.seekgnomes_v1_2.state_tools import is_verified

    result = payload["result"]
    task = load_task(int(payload["task_id"]))
    if result.get("task_id") != task["id"] or result.get("model") != payload["model"]:
        raise SeekGnomesTechnicalError("Host result task/model binding mismatch")
    best = result.get("best_verified_result")
    if best is not None:
        if not is_verified(best):
            raise SeekGnomesTechnicalError("Host result has an invalid strict receipt")
        certificate = best["certificate"]
        if (certificate.get("dimension") != task["dimension"]
                or certificate.get("qubits_per_cell") != task["qubits_per_cell"]):
            raise SeekGnomesTechnicalError("Strict receipt is for another lattice")
        energy = best["energy_density"]
    elif result.get("status") in _SCIENTIFIC:
        energy = None
    else:
        raise SeekGnomesTechnicalError("Technical termination has no scientific reward")
    parameters = score_parameters(task)
    metrics = task_metrics(energy, baseline=parameters["baseline"], lower=parameters["lower"],
                           group=parameters["group"], scale=parameters["scale"],
                           reference=parameters["reference"])
    rewards = {"verified": metrics["pass_rate"]}
    if metrics.get("improvement_rate") is not None:
        rewards["improved"] = metrics["improvement_rate"]
    if metrics.get("amplitude") is not None:
        rewards["amplitude"] = metrics["amplitude"]
    if metrics.get("score") is not None:
        rewards["reward"] = metrics["score"] / 100.0
    else:
        rewards["score_pending"] = 1.0
    return rewards, metrics


class SeekGnomesEnvironment(BaseEnvironment):
    """Private host artifact environment; actual role isolation is in runtime.

    Even trusted bootstrap commands execute in a networkless bubblewrap
    namespace. This object is never passed to the model's native shell tools.
    """
    def __init__(self, *args, **kwargs):
        trial_paths = kwargs.get("trial_paths")
        if trial_paths is None:
            raise ValueError("Harbor trial paths are required")
        self.root = _canonical(trial_paths.trial_dir / "host-environment")
        self._receipt_key = secrets.token_bytes(32)
        self._receipt_path = self.root / "app" / "host-receipt.json"
        self._receipt_digest = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def type():
        return "seekgnomes-host-artifacts"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(disable_internet=True, mounted=False)

    def _validate_definition(self):
        if not (self.environment_dir / "seekgnomes-task.json").is_file():
            raise ValueError("The fixed SeekGnomes task definition is missing")

    async def start(self, force_build=False):
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        for relative in ("app", "tests", "solution", "logs/agent", "logs/verifier", "logs/artifacts", "harbor"):
            (self.root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        _write(self.root / "environment-state.json", {
            "status": "started", "native_role_isolation": "runtime-owned-bubblewrap",
            "model_commands_use_environment_exec": False, "network": False})

    async def stop(self, delete=True):
        # Failed and completed evidence is retained. There is no daemon here.
        if self.root.exists():
            _write(self.root / "environment-state.json", {"status": "stopped",
                "delete_requested": bool(delete), "host_evidence_retained": True})

    def _path(self, path):
        virtual = PurePosixPath(path)
        if not virtual.is_absolute() or ".." in virtual.parts:
            raise ValueError("Environment paths must be absolute and traversal-free")
        relative = virtual.relative_to("/")
        if relative.parts and relative.parts[0] not in {"app", "tests", "solution", "logs", "harbor", "tmp"}:
            raise ValueError("Path is outside the artifact environment")
        physical = self.root / relative
        if physical.resolve() != physical or not physical.is_relative_to(self.root):
            raise ValueError("Artifact paths may not traverse symbolic links")
        return physical

    @staticmethod
    def _copy_file(source, destination):
        source = _canonical(source, existing=True)
        contents = _regular(source)
        destination = _canonical(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() and (destination.is_symlink() or not destination.is_file()):
            raise ValueError("Unsafe artifact destination")
        destination.write_bytes(contents)
        destination.chmod(stat.S_IMODE(source.stat().st_mode))

    @classmethod
    def _copy_dir(cls, source, destination):
        source = _canonical(source, existing=True)
        destination = _canonical(destination)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in source.iterdir():
            if child.is_symlink():
                raise ValueError("Artifact directories may not contain symbolic links")
            if child.is_dir():
                cls._copy_dir(child, destination / child.name)
            else:
                cls._copy_file(child, destination / child.name)

    async def upload_file(self, source_path, target_path):
        self._copy_file(source_path, self._path(target_path))

    async def upload_dir(self, source_dir, target_dir):
        self._copy_dir(source_dir, self._path(target_dir))

    async def download_file(self, source_path, target_path):
        self._copy_file(self._path(source_path), target_path)

    async def download_dir(self, source_dir, target_dir):
        self._copy_dir(self._path(source_dir), target_dir)

    async def is_dir(self, path, user=None):
        return self._path(path).is_dir()

    async def is_file(self, path, user=None):
        return self._path(path).is_file()

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        if not Path("/usr/bin/bwrap").is_file():
            raise RuntimeError("bubblewrap is required; refusing host shell fallback")
        workdir = str(PurePosixPath(cwd or "/app"))
        self._path(workdir)
        child = ["/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-pid",
                 "--unshare-ipc", "--unshare-net", "--unshare-uts", "--clearenv",
                 "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr"]
        for directory in ("/lib", "/lib64"):
            if Path(directory).exists():
                child += ["--ro-bind", directory, directory]
        child += ["--dir", "/etc"]
        if Path('/etc/ld.so.cache').is_file():
            child += ['--ro-bind','/etc/ld.so.cache','/etc/ld.so.cache']
        child += ["--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
                  "--ro-bind", str(PROJECT), str(PROJECT),
                  "--ro-bind", str(PREFIX), str(PREFIX)]
        dependency_root = Path(harbor.__file__).resolve().parents[1]
        if not dependency_root.is_relative_to(PROJECT) and not dependency_root.is_relative_to(PREFIX):
            child += ["--ro-bind", str(dependency_root), str(dependency_root)]
        for name in ("app", "tests", "solution", "logs", "harbor"):
            child += ["--bind", str(self.root / name), "/" + name]
        child += ["--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc",
                  "--setenv", "PATH", f"{PREFIX}/bin:/usr/local/bin:/usr/bin:/bin",
                  "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8"]
        for key, value in (env or {}).items():
            if not isinstance(key, str) or "=" in key or "\0" in key or not isinstance(value, str):
                raise ValueError("Invalid trusted verifier environment")
            child += ["--setenv", key, value]
        child += ["--chdir", workdir, "--", "/bin/bash", "-c", command]
        process = await asyncio.create_subprocess_exec(*child, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, env={})
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except BaseException:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        return ExecResult(stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"),
                          return_code=process.returncode)

    def commit_result(self, payload):
        envelope = _seal(payload, self._receipt_key)
        _write(self._receipt_path, envelope)
        self._receipt_digest = hashlib.sha256(_regular(self._receipt_path)).hexdigest()

    def read_host_result(self):
        contents = _regular(self._receipt_path)
        if not self._receipt_digest or hashlib.sha256(contents).hexdigest() != self._receipt_digest:
            raise SeekGnomesTechnicalError("Trusted host receipt changed after commit")
        return _open_seal(json.loads(contents), self._receipt_key)


class SeekGnomesAgent(BaseAgent):
    """Harbor's external agent is the fixed two-native-session coordinator."""
    def __init__(self, *args, run_config=None, call_id=None, **kwargs):
        self.run_config = deepcopy(run_config or {})
        self.call_id = call_id
        if "api_key" in self.run_config:
            raise ValueError("API credentials must not appear in Harbor configuration")
        super().__init__(*args, **kwargs)

    @staticmethod
    def name():
        return "seekgnomes-dual-native"

    def version(self):
        return VERSION

    def _init_model_info(self):
        # A relay route's prefix need not be the model's company. Keep its
        # exact requested ID instead of silently stripping the route prefix.
        self._parsed_model_provider = self.run_config.get("company")
        self._parsed_model_name = self.model_name

    async def setup(self, environment):
        if not isinstance(environment, SeekGnomesEnvironment):
            raise ValueError("SeekGnomes requires its trusted artifact environment")
        if importlib.metadata.version("harbor") != HARBOR_VERSION:
            raise ValueError("This protocol requires Harbor 0.22.0")

    async def run(self, instruction, environment, context: AgentContext):
        from revisions.seekgnomes_v1_2.task_export import STATE_PROTOCOL

        settings = deepcopy(self.run_config)
        settings["out"] = Path(settings["out"])
        resume = settings.pop("resume", False)
        call = _CALLS.get(self.call_id, {})
        key = call.get("api_key") or os.environ.get("SEEKGNOMES_API_KEY", "")
        if not key:
            raise ValueError("SEEKGNOMES_API_KEY is required")
        runner = call.get("runner")
        try:
            if runner is None:
                result = await _supervise_native(settings, key, resume=resume,
                    restart_log=environment.root / "logs/artifacts/controller-restarts.json")
            else:
                result = await asyncio.to_thread(runner, **settings, api_key=key)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, SeekGnomesRecoveryError):
                call["recovery_failed"] = True
            if isinstance(exc, SeekGnomesTechnicalError):
                raise
            # Provider exception text may contain an upstream credential.
            raise SeekGnomesTechnicalError("Native runtime raised " + type(exc).__name__) from None
        if not isinstance(result, dict):
            raise SeekGnomesTechnicalError("The native runtime returned no host manifest")
        result = json.loads(json.dumps(result, allow_nan=False).replace(key, "[REDACTED]"))
        if call:
            call["result"] = result
        payload = {"task_id": str(settings["task_id"]).zfill(2), "model": settings["model"],
                   "scientific_trial": call.get("runner") is None,
                   "result": result}
        environment.commit_result(payload)
        context.metadata = {"protocol": STATE_PROTOCOL,
            "requested_model": settings["model"], "scientific_status": result.get("status"),
            "host_manifest": str(settings["out"] / "manifest.json"),
            "scientific_trial": payload["scientific_trial"],
            "native_role_isolation": "runtime-owned-bubblewrap"}
        # These are operational meters, not billing or physical-token claims.
        context.metadata["usage_is_operational_meter"] = True
        for field, source in (("n_input_tokens", "input_tokens_charged"),
                              ("n_output_tokens", "output_tokens_charged")):
            value = result.get(source)
            if type(value) is int and value >= 0:
                setattr(context, field, value)
        best = result.get("best_verified_result")
        if result.get("status") not in _SCIENTIFIC and best is None:
            raise SeekGnomesTechnicalError("Native runtime ended with a technical gap: " +
                                          str(result.get("status", "unknown")))


class HostReceiptVerifier(BaseVerifier):
    """Project host-certified state evidence into Harbor rewards."""
    async def verify(self):
        if not isinstance(self.environment, SeekGnomesEnvironment):
            raise ValueError("The trusted SeekGnomes environment is required")
        payload = self.environment.read_host_result()
        rewards, metrics = _rewards(payload)
        # Execute the exported task's actual verifier entry point after the
        # model phase, using an ephemeral host capability. The model does not
        # receive this environment, its key, or these private artifact paths.
        await self.environment.upload_dir(self.task.paths.tests_dir, "/tests")
        dependency_root = Path(harbor.__file__).resolve().parents[1]
        executed = await self.environment.exec("bash /tests/test.sh", timeout_sec=300,
            env={"SEEKGNOMES_HOST_RECEIPT": "/app/host-receipt.json",
                 "SEEKGNOMES_RECEIPT_VERIFY_KEY": self.environment._receipt_key.hex(),
                 "PYTHONPATH": str(dependency_root) + os.pathsep + str(PROJECT)})
        _write(self.trial_paths.verifier_dir / "script-result.json", {
            "return_code": executed.return_code,
            "stdout": executed.stdout, "stderr": executed.stderr})
        if executed.return_code != 0:
            raise SeekGnomesTechnicalError("The fixed host receipt verifier could not complete")
        emitted = json.loads(_regular(self.environment._path("/logs/verifier/reward.json")))
        if emitted != rewards:
            raise SeekGnomesTechnicalError("The verifier reward differs from its authenticated host receipt")
        self.trial_paths.verifier_dir.mkdir(parents=True, exist_ok=True)
        _write(self.trial_paths.reward_json_path, rewards)
        _write(self.trial_paths.verifier_dir / "seekgnomes-score.json", {
            "metrics": metrics, "scientific_trial": payload["scientific_trial"],
            "authority": "host strict fixed-goal/kernel receipt",
            "verifier_script_executed": True,
            "independent_lean_rerun_in_harbor_projection": False})
        return VerifierResult(rewards=rewards)


async def _job(settings, harbor_dir, api_key, fixture_runner):
    from harbor.job import Job
    from harbor.models.job.config import JobConfig, RetryConfig
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig
    from revisions.seekgnomes_v1_2.task_export import export_task

    harbor_dir = _canonical(harbor_dir)
    harbor_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    task_dir = harbor_dir / "task"
    export_task(settings["task_id"], task_dir, web=settings["web"], cpus=settings["cpus"])
    call_id = uuid4().hex
    call = {"api_key": api_key, "runner": fixture_runner}
    _CALLS[call_id] = call
    public = {**settings, "out": str(settings["out"])}
    config = JobConfig(job_name="trial", jobs_dir=harbor_dir / "jobs", n_attempts=1,
        n_concurrent_trials=1, quiet=True, retry=RetryConfig(max_retries=0),
        tasks=[TaskConfig(path=task_dir)],
        agents=[AgentConfig(import_path=SeekGnomesAgent.import_path(), model_name=settings["model"],
                            kwargs={"run_config": public, "call_id": call_id})],
        environment=EnvironmentConfig(import_path=__name__ + ":SeekGnomesEnvironment", delete=False),
        verifier=VerifierConfig(import_path=__name__ + ":HostReceiptVerifier"))
    previous_telemetry = os.environ.get("HARBOR_TELEMETRY")
    os.environ["HARBOR_TELEMETRY"] = "off"
    try:
        job = await Job.create(config)
        completed = await job.run()
        trials = completed.trial_results or []
        harbor = {"version": HARBOR_VERSION, "job_dir": str(job.job_dir),
            "trial_count": len(trials), "scientific_trial": fixture_runner is None,
            "trial_result": trials[0].model_dump(mode="json") if len(trials) == 1 else None}
        result = deepcopy(call.get("result"))
        if result is None:
            result = {"status": "infrastructure_failure", "task_id": str(settings["task_id"]).zfill(2),
                "model": settings["model"], "budget_multiplier": settings["budget_multiplier"],
                "best_verified_result": None,
                "error_type": "controller_recovery_failure" if call.get("recovery_failed") else "harbor_trial_failure"}
        if len(trials) != 1:
            result.update(status="infrastructure_failure", error_type="harbor_trial_cardinality")
        elif trials[0].exception_info is not None and result.get("status") in _SCIENTIFIC:
            # A verifier/environment failure cannot silently become a scored
            # scientific terminal simply because the model loop returned.
            result.update(status="infrastructure_failure", error_type="harbor_verification_failure")
        result["harbor"] = harbor
        _write(harbor_dir / "seekgnomes-harbor-result.json", result)
        return result
    finally:
        _CALLS.pop(call_id, None)
        if previous_telemetry is None:
            os.environ.pop("HARBOR_TELEMETRY", None)
        else:
            os.environ["HARBOR_TELEMETRY"] = previous_telemetry


def run_harbor_trial(*, task_id, out, model, endpoint, api_key=None, budget_multiplier=1,
                     company=None, harness=None, api_protocol="auto", web=True,
                     completion_token_field="max_tokens", experiment_id="",
                     model_context_tokens=None,
                     constructor_output_tokens=128000,
                     constructor_input_tokens=None, constructor_active_seconds=14400,
                     cpus=2,
                     harbor_dir=None, fixture_runner=None, resume=False):
    """Execute one real Harbor Job containing one native two-role Trial.

    Unexpected worker exits restart inside this trial. Resume explicitly to
    recreate a Harbor job over an existing trial if its supervisor also exited.
    ``fixture_runner`` is an in-process, host-owned offline test callback, never
    a CLI option or a model tool. It cannot be serialized into a saved job.
    """
    if importlib.metadata.version("harbor") != HARBOR_VERSION:
        raise ValueError("This protocol requires Harbor 0.22.0")
    out = _canonical(out)
    if type(resume) is not bool or (resume and fixture_runner is not None):
        raise ValueError("Native resume is only available for the normal runtime")
    if not out.is_relative_to(PROJECT / "runs") or (out.exists() and not resume):
        raise ValueError("A fresh unaliased runs output path is required")
    if type(budget_multiplier) is not int or budget_multiplier != 1:
        raise ValueError("This protocol has one configurable budget and no multiplier expansion")
    if model_context_tokens is not None and (type(model_context_tokens) is not int or model_context_tokens < 1):
        raise ValueError("The model context window must be a positive integer")
    if type(cpus) is not int or cpus < 1:
        raise ValueError("CPU count must be a positive integer")
    if constructor_output_tokens is None and constructor_active_seconds is None:
        raise ValueError("Choose a Constructor output or activity budget")
    for name, value in (("Constructor output-token budget", constructor_output_tokens),
                        ("Constructor activity budget", constructor_active_seconds)):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(name + " must be a positive integer or None")
    if constructor_input_tokens is not None and (type(constructor_input_tokens) is not int or constructor_input_tokens < 1):
        raise ValueError("The optional Constructor input-token budget must be a positive integer")
    key = api_key or os.environ.get("SEEKGNOMES_API_KEY", "")
    if not key:
        raise ValueError("SEEKGNOMES_API_KEY is required")
    if not model or any(ord(c) < 32 for c in model) or key in model or key in endpoint:
        raise ValueError("Invalid public model/endpoint configuration")
    settings = {"task_id": str(int(task_id)).zfill(2), "out": out, "model": model,
        "endpoint": endpoint, "budget_multiplier": budget_multiplier, "company": company,
        "harness": harness, "api_protocol": api_protocol, "web": bool(web),
        "completion_token_field": completion_token_field, "experiment_id": experiment_id,
        "model_context_tokens": model_context_tokens,
        "constructor_output_tokens": constructor_output_tokens,
        "constructor_input_tokens": constructor_input_tokens,
        "constructor_active_seconds": constructor_active_seconds, "cpus": cpus}
    root = _canonical(harbor_dir or out.parent / ".harbor" / out.name)
    if resume:
        settings["resume"] = True
        root = root / "wrapper-resumes" / uuid4().hex
    try:
        return asyncio.run(_job(settings, root, key, fixture_runner))
    except BaseException:
        if fixture_runner is None:
            from revisions.seekgnomes_v1_2.runtime import cancel_active_trials
            cancel_active_trials()
        raise


def run_fixture_smoke(out=None):
    """Exercise real Harbor trials offline, without claiming a model solution."""
    parent = _canonical(out or PROJECT / "runs" / ("harbor-self-test-" + uuid4().hex))
    parent.mkdir(parents=True, exist_ok=False, mode=0o700)

    def fixture(*, task_id, out, model, budget_multiplier, **kwargs):
        out.mkdir(mode=0o700, exist_ok=False)
        result = {"status": "final_submission_not_verified", "task_id": task_id,
            "model": model, "budget_multiplier": budget_multiplier,
            "best_verified_result": None, "handoffs": [], "turns": 0,
            "input_tokens_charged": 0, "output_tokens_charged": 0,
            "scientific_trial": False, "external_api_calls": 0}
        _write(out / "manifest.json", result)
        return result

    def technical(**kwargs):
        value = fixture(**kwargs)
        value["status"] = "infrastructure_failure"
        _write(kwargs["out"] / "manifest.json", value)
        return value

    arguments = {"task_id": "02", "model": "offline/seekgnomes-fixture", "company": "fixture",
                 "endpoint": "https://unused.invalid/v1", "api_key": "offline-not-an-api-key",
                 "web": False}
    scientific = run_harbor_trial(**arguments, out=parent / "scientific", fixture_runner=fixture)
    missing = run_harbor_trial(**arguments, out=parent / "technical", fixture_runner=technical)
    a = scientific["harbor"].get("trial_result") or {}
    b = missing["harbor"].get("trial_result") or {}
    score_files = list((parent / ".harbor" / "scientific").rglob("seekgnomes-score.json"))
    checks = {
        "real_harbor_job_and_trial": scientific["harbor"]["trial_count"] == 1,
        "scientific_no_certificate_is_zero": (a.get("verifier_result") or {}).get("rewards", {}).get("reward") == 0,
        "technical_failure_has_no_reward": b.get("verifier_result") is None,
        "technical_failure_remains_missing": missing.get("status") == "infrastructure_failure" and b.get("exception_info") is not None,
        "fixture_not_a_model_score": a.get("agent_result", {}).get("metadata", {}).get("scientific_trial") is False,
        "full_model_route_preserved": (a.get("agent_info") or {}).get("model_info") == {
            "name": arguments["model"], "provider": "fixture"},
        "exported_verifier_script_executed": len(score_files) == 1 and json.loads(
            _regular(score_files[0])).get("verifier_script_executed") is True,
    }
    return {"schema": "seekgnomes-harbor-offline-smoke/v1", "ok": all(checks.values()),
            "checks": checks, "external_api_calls": 0, "scientific_trial": False,
            "host_fixture_only": True, "evidence": str(parent)}


def verify_receipt_file(receipt, reward, key, *, artifact_namespace=False):
    """Trusted verifier-script entry point. The signer key is host-only."""
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("A host-only receipt verification capability is required")
    if artifact_namespace:
        # These two fixed names live in the trusted artifact namespace created
        # by SeekGnomesEnvironment.exec. Arbitrary output paths are forbidden.
        if Path(receipt) != Path("/app/host-receipt.json") or Path(reward) != Path("/logs/verifier/reward.json"):
            raise ValueError("Unexpected artifact namespace paths")
        descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024**2:
                raise ValueError("Invalid namespaced host receipt")
            contents = stream.read()
    else:
        contents = _regular(receipt)
    payload = _open_seal(json.loads(contents), key)
    rewards, _ = _rewards(payload)
    if artifact_namespace:
        parent_fd = os.open("/logs/verifier", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = "reward-" + uuid4().hex + ".tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent_fd)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_json(rewards) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "reward.json", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
    else:
        _write(reward, rewards)
    return rewards


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify-receipt")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--reward", type=Path, required=True)
    verify.add_argument("--artifact-namespace", action="store_true")
    args = parser.parse_args(argv)
    try:
        key = bytes.fromhex(os.environ.get("SEEKGNOMES_RECEIPT_VERIFY_KEY", ""))
        verify_receipt_file(args.receipt, args.reward, key, artifact_namespace=args.artifact_namespace)
    except (ValueError, OSError, SeekGnomesTechnicalError):
        print("A valid host-owned strict receipt is required; no reward was accepted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
