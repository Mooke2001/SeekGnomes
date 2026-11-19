"""Launch only the pinned Linux image with the original role sandbox protections."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import shutil
import subprocess
import time

PROJECT = "/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational"
PYTHON = "/home/ubuntu/Mooke/Eureka/Environment/Python/miniconda3/bin/python"
OVERLAY = "/opt/seekgnomes/console-runtime"
LABEL = "org.seekgnomes.console.run"
ARCHIVE = "seekgnomes-image.tar.gz"
IMAGE_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


class RunnerError(RuntimeError):
    """Known infrastructure failure safe to return without subprocess output."""


class RunStopped(Exception):
    pass


class RecoveryRequired(RunnerError):
    """The engine could not confirm that the detached container exited."""


def container_name(run_id):
    return "seekgnomes-console-" + run_id


def command_env():
    env = os.environ.copy()
    env.pop("SEEKGNOMES_API_KEY", None)
    return env


def command(argv, *, stop=None, timeout=20):
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=command_env(), start_new_session=True) as process:
            deadline = time.monotonic() + timeout
            while True:
                stopped = stop is not None and stop.is_set()
                if stopped or time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    if stopped:
                        raise RunStopped
                    return None, b""
                try:
                    output, _ = process.communicate(timeout=0.25)
                    return process.returncode, output
                except subprocess.TimeoutExpired:
                    continue
    except OSError:
        return None, b""


def select_engine(stop):
    requested = os.environ.get("SEEKGNOMES_ENGINE", "")
    if requested and requested not in ("docker", "podman"):
        raise RunnerError("SEEKGNOMES_ENGINE must be docker or podman.")
    for name in ((requested,) if requested else ("docker", "podman")):
        engine = shutil.which(name)
        if engine is None:
            continue
        if name == "docker":
            code, security = command([engine, "info", "--format", "{{json .SecurityOptions}}"], stop=stop)
            if code == 0 and b"rootless" in security.lower():
                if requested:
                    raise RunnerError("Use rootful Docker or rootless Podman for the required result ownership.")
                continue
        else:
            code, _ = command([engine, "info", "--format", "{{.Host.Arch}}"], stop=stop)
        if code == 0:
            return name, engine
    raise RunnerError("No working rootful Docker or Podman engine was found. Check the engine and DOCKER_HOST.")


def image_hash(raw):
    match = IMAGE_RE.fullmatch(raw)
    return match.group(1) if match else None


def valid_image(engine, ref, expected, stop):
    code, info = command([engine, "image", "inspect", "--format", "{{.Id}} {{.Os}} {{.Architecture}}", ref], stop=stop)
    values = info.decode("ascii", errors="replace").split()
    return code == 0 and len(values) == 3 and image_hash(values[0]) == expected and values[1:] == ["linux", "amd64"]


def verify_archive(package_dir, stop):
    archive = package_dir / ARCHIVE
    try:
        manifest = (package_dir / "image.sha256").read_text(encoding="ascii").split()
        if len(manifest) != 2 or manifest[1].lstrip("*") != ARCHIVE or not image_hash(manifest[0]):
            raise RunnerError("The bundled image checksum file is invalid.")
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                if stop.is_set():
                    raise RunStopped
                digest.update(chunk)
    except (OSError, UnicodeError):
        raise RunnerError("The bundled image archive or its checksum could not be read.") from None
    if digest.hexdigest() != manifest[0]:
        raise RunnerError("The bundled image failed SHA-256 verification.")
    return archive


def inspect_container(engine, run_id):
    fmt = '{{.Id}} {{index .Config.Labels "' + LABEL + '"}} {{.State.Running}}'
    code, data = command([engine, "container", "inspect", "--format", fmt, container_name(run_id)], timeout=10)
    if code is None:
        raise RunnerError("The container engine could not confirm that the run stopped.")
    if code != 0:
        info_code, _ = command([engine, "info"], timeout=10)
        if info_code != 0:
            raise RunnerError("The container engine could not confirm that the run stopped.")
        return None
    values = data.decode("ascii", errors="replace").split()
    if len(values) != 3 or not image_hash(values[0]) or values[1] != run_id or values[2] not in ("true", "false"):
        raise RunnerError("The engine returned an unexpected container identity.")
    return values[0] if values[2] == "true" else None


def stop_container(engine, run_id):
    target = inspect_container(engine, run_id)
    if target is None:
        return False
    code, _ = command([engine, "stop", "--time", "25", target], timeout=35)
    if code != 0 and inspect_container(engine, run_id) is not None:
        raise RunnerError("The engine could not stop the active run.")
    return True


def preflight_error(output, secret):
    """Extract frozen preflight errors without retaining container stdout."""
    try:
        record, _ = json.JSONDecoder().raw_decode(output.decode("utf-8", errors="replace").lstrip())
        if not isinstance(record, dict) or record.get("schema") != "seekgnomes-harbor-preflight/v1.2":
            return None
        failures = record.get("failures", [])
        if record.get("ok") is False and isinstance(failures, list):
            messages = [" ".join(item.split())[:240] for item in failures[:4] if isinstance(item, str)]
            if messages:
                return "Container checks failed. " + " ".join(messages).replace(secret, "[REDACTED]")
    except (ValueError, RecursionError):
        pass
    return None


class ContainerRunner:
    def __init__(self, package_dir: Path, overlay_dir: Path):
        self.package_dir = package_dir.resolve()
        self.overlay_dir = overlay_dir.resolve()

    def _image(self, name, engine, stop, update):
        bundled = (self.package_dir / "image-id.txt").is_file()
        pin_name = "image-id.txt" if bundled else "build-image-id.txt"
        try:
            expected = image_hash((self.package_dir / pin_name).read_text(encoding="ascii").strip())
        except (OSError, UnicodeError):
            expected = None
        if not expected:
            raise RunnerError("The image ID is missing or invalid. Build the runtime with build-image.sh, or use a verified image archive.")
        ref = "sha256:" + expected if name == "docker" else expected
        if not valid_image(engine, ref, expected, stop):
            if not bundled:
                raise RunnerError("The runtime image built with build-image.sh is absent from this container engine.")
            update("image", "Verifying the bundled container image.")
            archive = verify_archive(self.package_dir, stop)
            update("image", "Importing the verified container image. This can take several minutes.")
            code, _ = command([engine, "load", "-i", str(archive)], stop=stop, timeout=1800)
            if code != 0:
                raise RunnerError("The container engine could not import the verified image. Check available disk space.")
        if not valid_image(engine, ref, expected, stop):
            raise RunnerError("The imported image does not match the pinned Linux amd64 image ID.")
        return ref

    def _argv(self, name, engine, ref, run_id, output_dir, config):
        if any(":" in str(path) for path in (output_dir, self.overlay_dir)):
            raise RunnerError("Container bind paths must not contain colons.")
        if not (self.overlay_dir / "runtime_entry.py").is_file():
            raise RunnerError("The bundled console runtime overlay is incomplete.")
        manifest = self.package_dir / "source-manifest.json"
        try:
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            raise RunnerError("The release source manifest is missing or unreadable.") from None
        argv = [engine, "run", "--rm", "--init", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
                "--pids-limit", "256", "--memory", os.environ.get("SEEKGNOMES_MEMORY", "16g"),
                "--cpus", str(config["cpus"]), "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
                "--user", f"{os.getuid()}:{os.getgid()}", "--name", container_name(run_id),
                "--label", f"{LABEL}={run_id}", "--pull=never", "--log-driver", "none", "-i",
                "-v", f"{output_dir}:{PROJECT}/runs:rw", "-v", f"{self.overlay_dir}:{OVERLAY}:ro",
                "--entrypoint", PYTHON]
        if name == "podman":
            argv.extend(["--userns=keep-id", "--security-opt", "unmask=ALL"])
        else:
            argv.extend(["--security-opt", "systempaths=unconfined"])
        argv.extend([ref, "-B", f"{OVERLAY}/runtime_entry.py", "run", "--read-api-key-stdin",
                     "--budget-mode", config["budget_mode"], "--budget-limit", str(config["budget_limit"]),
                     "--source-manifest-sha256=" + manifest_digest,
                     "--company=" + config["company"], "--model=" + config["model"],
                     "--api-base-url=" + config["api_base_url"], "--api-protocol=" + config["api_protocol"],
                     "--tasks=" + ",".join(config["task_ids"]), "--cpus=" + str(config["cpus"]),
                     "--parallel-tasks=" + str(config.get("parallel_tasks", 1)),
                     "--out=" + PROJECT + "/runs/one-task"])
        if config["model_context_tokens"] is not None:
            argv.append("--model-context-tokens=" + str(config["model_context_tokens"]))
        if not config["web"]:
            argv.append("--no-web")
        if config["api_base_url"].lower().startswith("http://"):
            argv.append("--allow-local-http")
        return argv

    def _monitor(self, process, engine, run_id, secret, stop):
        output = bytearray()
        fd = process.stdout.fileno()
        stream_open = True
        stop_sent = False
        stop_after = 0.0
        while process.poll() is None or stream_open:
            if stop.is_set() and not stop_sent and time.monotonic() >= stop_after:
                try:
                    stop_sent = stop_container(engine, run_id)
                except RunnerError:
                    pass
                stop_after = time.monotonic() + 1
            if not stream_open:
                time.sleep(0.2)
                continue
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            chunk = os.read(fd, 8192)
            stream_open = bool(chunk)
            if len(output) < 128 * 1024:
                output.extend(chunk[:128 * 1024 - len(output)])
        code = process.wait()
        try:
            stop_container(engine, run_id)
        except RunnerError:
            raise RecoveryRequired("Restore the container engine and retry Stop to confirm the interrupted run ended.") from None
        if stop.is_set():
            raise RunStopped
        problem = preflight_error(output, secret)
        if problem:
            raise RunnerError(problem)
        return code

    def execute(self, run_id, output_dir, config, api_key, stop, update):
        if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
            raise RunnerError("The bundled image requires Linux x86_64/amd64, including an amd64 WSL distribution.")
        if os.getuid() == 0:
            raise RunnerError("Run the Console as a non-root host user for the required role sandbox.")
        update("engine", "Checking the local container engine.")
        name, engine = select_engine(stop)
        ref = self._image(name, engine, stop, update)
        argv = self._argv(name, engine, ref, run_id, output_dir, config)
        if stop.is_set():
            raise RunStopped
        update("preflight", "Checking the frozen benchmark and selected harness inside the container.", engine=name)
        if stop.is_set():
            raise RunStopped
        try:
            with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=command_env(), start_new_session=True) as process:
                try:
                    if not stop.is_set():
                        process.stdin.write(api_key.encode("ascii") + b"\n")
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                return self._monitor(process, engine, run_id, api_key, stop)
        except OSError:
            raise RunnerError("Could not launch the benchmark container. Check the engine and bind paths.") from None

    def recover(self, run_id, engine_name):
        if engine_name is None:
            return
        engine = shutil.which(engine_name) if engine_name in ("docker", "podman") else None
        if engine is None:
            raise RunnerError("Restore the container engine and retry Stop to confirm the interrupted run ended.")
        stop_container(engine, run_id)
