"""A local, credential-free numerical aid for the hybrid state agent.

Only this trusted launcher, one frozen Python source, the request and the
scientific Python installation enter the sandbox. Candidate stdout is data,
never a state-legality proof or an energy certificate.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import runpy
import selectors
import signal
import stat
import subprocess
import sys
import time


_MODULE = Path(__file__).resolve()
# The same frozen file runs as /launcher.py in the minimal namespace. Those
# host-only broker paths are deliberately unavailable to the candidate worker.
PROJECT = _MODULE.parents[2] if len(_MODULE.parents) > 2 else Path("/unmounted-project")
EUREKA = PROJECT.parents[1] if len(PROJECT.parents) > 1 else Path("/unmounted-eureka")
PREFIX = EUREKA / "Environment/Python/miniconda3"
SOURCE_LIMIT = 2 * 1024**2
OUTPUT_LIMIT = 2 * 1024**2
RETURN_LIMIT = 64 * 1024


def _regular_bytes(path: Path, limit: int) -> bytes:
    """Open every component without following links and reject hardlinks."""
    path = Path(os.path.abspath(path))
    if ".." in path.parts or not path.is_relative_to(PROJECT):
        raise ValueError("Input must be a canonical path inside this project")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
            os.close(fd)
            fd = following
        incoming = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                           dir_fd=fd)
        try:
            before = os.fstat(incoming)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("Input must be a regular single-link file")
            if before.st_size > limit:
                raise ValueError("Input exceeds its byte limit")
            chunks, length = [], 0
            while chunk := os.read(incoming, min(65536, limit + 1 - length)):
                chunks.append(chunk)
                length += len(chunk)
                if length > limit:
                    raise ValueError("Input exceeds its byte limit")
            after = os.fstat(incoming)
            attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if any(getattr(before, name) != getattr(after, name) for name in attributes):
                raise ValueError("Input changed while it was read")
            return b"".join(chunks)
        finally:
            os.close(incoming)
    finally:
        os.close(fd)


def _new_file(path: Path, contents: bytes, mode: int = 0o400) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(contents)


def _seccomp() -> None:
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)
    if not context:
        raise RuntimeError("Could not allocate the numerical sandbox filter")
    try:
        # Numerics are preloaded with one BLAS thread. All subsequent process
        # and thread creation is denied; existing native numerical code runs.
        for name in (
            "execve", "execveat", "fork", "vfork", "clone", "clone3", "socket", "socketpair",
            "connect", "bind", "listen", "accept", "accept4", "ptrace", "process_vm_readv",
            "process_vm_writev", "unshare", "setns", "mount", "umount2", "pivot_root",
            "open_by_handle_at", "bpf", "perf_event_open", "keyctl", "add_key", "request_key",
            "userfaultfd", "io_uring_setup", "io_uring_enter", "io_uring_register", "mknod",
            "mknodat", "sched_setaffinity", "reboot", "kexec_load", "init_module", "finit_module",
            "delete_module",
        ):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and library.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 0):
                raise RuntimeError("Could not configure the numerical sandbox filter")
        if library.seccomp_load(context):
            raise RuntimeError("Could not install the numerical sandbox filter")
    finally:
        library.seccomp_release(context)


def _inside() -> None:
    request = json.loads(Path("/request.json").read_text())
    wall = request["wall_seconds"]
    for kind, value in (
        (resource.RLIMIT_CPU, math.ceil(wall) + 2),
        (resource.RLIMIT_AS, 4 * 1024**3),
        (resource.RLIMIT_FSIZE, 8 * 1024**2),
        (resource.RLIMIT_NOFILE, 128),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (value, value))
    # Trusted installed packages load before process/thread creation is denied.
    import numpy  # noqa: F401
    import scipy.linalg  # noqa: F401
    import scipy.sparse.linalg  # noqa: F401
    import scipy.optimize  # noqa: F401
    import tenpy  # Constructor exploration only
    import qutip  # Constructor exploration only
    _seccomp()
    sys.argv = ["/candidate.py", *request["arguments"]]
    runpy.run_path("/candidate.py", run_name="__main__")


def _sandbox_command(job: Path, cpu: int) -> list[str]:
    if not Path("/usr/bin/bwrap").is_file() or not Path("/usr/bin/taskset").is_file():
        raise RuntimeError("bubblewrap and taskset are required; refusing unisolated execution")
    if PREFIX.resolve(strict=True) != PREFIX or not (PREFIX / "bin/python").is_file():
        raise RuntimeError("The project scientific Python runtime is unavailable")
    command = [
        "/usr/bin/taskset", "-c", str(cpu), "/usr/bin/bwrap", "--die-with-parent", "--new-session",
        "--unshare-pid", "--unshare-ipc", "--unshare-net", "--unshare-uts", "--clearenv",
        "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr",
    ]
    for directory in ("/lib", "/lib64"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    command += [
        "--ro-bind", str(PREFIX), str(PREFIX),
        "--ro-bind", str(job / "launcher.py"), "/launcher.py",
        "--ro-bind", str(job / "candidate.py"), "/candidate.py",
        "--ro-bind", str(job / "request.json"), "/request.json",
        "--tmpfs", "/work", "--dir", "/work/tmp", "--dev", "/dev", "--proc", "/proc",
        "--dir", "/etc",
    ]
    if Path("/etc/ld.so.cache").is_file():
        command += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    settings = {
        "PATH": f"{PREFIX}/bin:/usr/bin", "HOME": "/work", "TMPDIR": "/work/tmp",
        "XDG_CACHE_HOME": "/work/cache", "MPLCONFIGDIR": "/work/mpl", "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1",
    }
    for name, value in settings.items():
        command += ["--setenv", name, value]
    command += [
        "--remount-ro", "/dev", "--remount-ro", "/",
        "--chdir", "/work", "--", str(PREFIX / "bin/python"), "-I", "-B", "/launcher.py", "--inside",
    ]
    return command


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _collect(command: list[str], wall: float) -> tuple[int, str, dict[str, bytes], float]:
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={}, close_fds=True, start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    status = "completed"
    with selectors.DefaultSelector() as selector:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = wall - (time.monotonic() - started)
            if remaining <= 0 and status == "completed":
                status = "wall_time_limit"
                _kill(process)
            for key, _ in selector.select(timeout=max(0.01, min(0.1, remaining))):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = buffers[key.data]
                room = OUTPUT_LIMIT - len(target)
                target.extend(data[:max(0, room)])
                if len(data) > room and status == "completed":
                    status = "output_limit"
                    _kill(process)
        process.wait()
    if status == "completed" and process.returncode:
        status = "worker_failed"
    return process.returncode, status, {name: bytes(data) for name, data in buffers.items()}, time.monotonic() - started


class LocalNumericalBroker:
    """Local callback for the existing ``server`` numerical-aid action.

    The action name is retained for the shared budget ledger. No remote server
    is contacted. The requested wall allowance is charged conservatively at one
    CPU; stdout is not interpreted as a verification receipt.
    """

    def __init__(self, *, cpu: int | None = None):
        available = sorted(os.sched_getaffinity(0))
        if not available or (cpu is not None and (type(cpu) is not int or cpu not in available)):
            raise ValueError("Requested CPU is not in the current process affinity")
        self.cpu = available[0] if cpu is None else cpu

    def __call__(self, *, candidate_path: Path, invocation_dir: Path, cpus: int = 1,
                 wall_seconds: float = 60, arguments: list[str] | None = None) -> dict:
        if type(cpus) is not int or cpus != 1:
            raise ValueError("Each local numerical call uses exactly one CPU")
        if (type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds)
                or not 1 <= wall_seconds <= 300):
            raise ValueError("wall_seconds must be in 1..300")
        arguments = [] if arguments is None else arguments
        if (not isinstance(arguments, list) or len(arguments) > 64
                or any(not isinstance(value, str) or len(value) > 4096 or "\0" in value for value in arguments)):
            raise ValueError("arguments must be at most 64 bounded strings")
        candidate = Path(os.path.abspath(candidate_path))
        if candidate.suffix != ".py":
            raise ValueError("Numerical exploration requires a Python source file")
        source = _regular_bytes(candidate, SOURCE_LIMIT)
        invocation = Path(os.path.abspath(invocation_dir))
        if not invocation.is_relative_to(PROJECT) or invocation.resolve() != invocation:
            raise ValueError("Invocation must remain inside the project without aliases")
        invocation.mkdir(parents=True, exist_ok=True)
        job = invocation / "local-numerical"
        job.mkdir(mode=0o700, exist_ok=False)
        request = {"wall_seconds": wall_seconds, "cpu": self.cpu, "arguments": arguments}
        _new_file(job / "candidate.py", source)
        _new_file(job / "launcher.py", _regular_bytes(Path(__file__), SOURCE_LIMIT))
        _new_file(job / "request.json", (json.dumps(request, indent=2) + "\n").encode())
        result = {
            "ok": False, "status": "not_started", "execution_location": "local",
            "formal_proof": False, "infinite_state_verified": False, "output_trusted": False,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_sha256": hashlib.sha256(source).hexdigest(), "cpu_affinity": [self.cpu],
            "threads": 1, "cpu_seconds_charged": wall_seconds,
            "cpu_charging_policy": "requested_wall_seconds_at_one_CPU_not_measured_usage",
            "network": False, "filesystem": "read_only_inputs_private_ephemeral_work",
            "environment": "credential_free_allowlist", "process_creation": "denied",
            "output_limit_per_stream": OUTPUT_LIMIT, "return_limit_per_stream": RETURN_LIMIT,
            "address_space_bytes": 4 * 1024**3,
        }
        try:
            command = _sandbox_command(job, self.cpu)
            result["command"] = command
            code, status, output, elapsed = _collect(command, float(wall_seconds))
            result.update(ok=code == 0 and status == "completed", status=status,
                          returncode=code, wall_seconds=elapsed)
            for name, data in output.items():
                _new_file(job / (name + ".log"), data)
                result[name] = data[:RETURN_LIMIT].decode(errors="replace")
                result[name + "_truncated"] = len(data) > RETURN_LIMIT
                result[name + "_sha256"] = hashlib.sha256(data).hexdigest()
                result[name + "_bytes_preserved"] = len(data)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result.update(ok=False, status="broker_error", error=str(error)[:2048])
        manifest = job / "manifest.json"
        result["manifest"] = str(manifest)
        _new_file(manifest, (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode())
        return result


if __name__ == "__main__":
    if sys.argv[1:] != ["--inside"]:
        raise SystemExit("This file is a broker module, not an unrestricted execution interface")
    _inside()
