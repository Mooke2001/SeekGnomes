"""Offline, read-only resource and strict-checker prerequisites for the release."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[2]
GIB = 1024 ** 3
MIN_CPUS = 2
MIN_AVAILABLE_MEMORY = 12 * GIB
REQUIRED_MODULES = ("httpx", "mpmath", "numpy", "scipy", "tenpy", "qutip")


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpu_set_count(value):
    if not value:
        return None
    cpus = set()
    try:
        for part in value.split(","):
            bounds = part.split("-")
            first, last = int(bounds[0]), int(bounds[-1])
            if len(bounds) > 2 or first < 0 or last < first or last - first > 100000:
                return None
            cpus.update(range(first, last + 1))
        return len(cpus) or None
    except ValueError:
        return None


def _ancestors(node, root):
    if not node.is_relative_to(root):
        return
    yield node
    while node != root:
        node = node.parent
        yield node


def cgroup_limits(root=Path("/sys/fs/cgroup"), membership=None):
    """Read accessible cgroup v1/v2 quotas, including ancestor restrictions.

    Containers can expose their own cgroup as the filesystem root. Both that
    root and the membership-relative location are inspected; unavailable
    controls are recorded as unknown, never treated as a zero-byte allowance.
    """
    root = Path(root)
    membership = _read("/proc/self/cgroup") if membership is None else membership
    nodes = {root}
    for line in (membership or "").splitlines():
        pieces = line.split(":", 2)
        if len(pieces) != 3:
            continue
        controllers, relative = pieces[1].split(","), pieces[2].lstrip("/")
        if ".." in Path(relative).parts:
            continue
        nodes.add(root / relative)
        for controller in controllers:
            if controller:
                nodes.add(root / controller / relative)
                nodes.add(root / controller)
        if "cpu" in controllers:
            nodes.add(root / "cpu,cpuacct" / relative)
    directories = {item for node in nodes for item in _ancestors(node, root)}
    quotas, memories, available, cpus = [], [], [], []
    for directory in sorted(directories):
        value = _read(directory / "cpu.max")
        if value:
            fields = value.split()
            if len(fields) == 2 and fields[0] != "max":
                try:
                    quota, period = int(fields[0]), int(fields[1])
                    if quota > 0 and period > 0:
                        quotas.append(quota / period)
                except ValueError:
                    pass
        quota = _read(directory / "cpu.cfs_quota_us")
        period = _read(directory / "cpu.cfs_period_us")
        if quota and period:
            try:
                if int(quota) > 0 and int(period) > 0:
                    quotas.append(int(quota) / int(period))
            except ValueError:
                pass
        for limit_name, usage_name in (("memory.max", "memory.current"),
                                       ("memory.limit_in_bytes", "memory.usage_in_bytes")):
            limit = _read(directory / limit_name)
            if limit and limit != "max":
                try:
                    number = int(limit)
                    if 0 < number < 2 ** 60:
                        memories.append(number)
                        used = int(_read(directory / usage_name) or "0")
                        available.append(max(0, number - used))
                except ValueError:
                    pass
        for name in ("cpuset.cpus.effective", "cpuset.effective_cpus", "cpuset.cpus"):
            count = cpu_set_count(_read(directory / name))
            if count:
                cpus.append(count)
    return {"cpu_quota": min(quotas) if quotas else None,
            "cpuset_count": min(cpus) if cpus else None,
            "memory_limit_bytes": min(memories) if memories else None,
            "memory_available_bytes": min(available) if available else None}


def resources():
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    cg = cgroup_limits()
    counts = [float(v) for v in (affinity, cg["cpu_quota"], cg["cpuset_count"]) if v is not None]
    meminfo = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable):\s+(\d+) kB", line)
        if match:
            meminfo[match[1]] = int(match[2]) * 1024
    available = [v for v in (meminfo.get("MemAvailable"), cg["memory_available_bytes"]) if v is not None]
    return {"architecture": platform.machine(), "system": platform.system(),
            "affinity_cpu_count": affinity, "effective_cpu_count": min(counts) if counts else None,
            "physical_memory_bytes": meminfo.get("MemTotal"),
            "available_memory_bytes": min(available) if available else None, "cgroup": cg}


def strict_lock_checks(project=PROJECT):
    project = Path(project)
    failures = []
    runtime = project / "tmp/lean-runtime/lean-4.35.0-linux/bin"
    try:
        for name in ("STATE_SPEC_V1.lock.json", "STATE_SPEC_V2.lock.json",
                     "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json"):
            lock = json.loads((project / name).read_text())
            for relative, expected in lock.get("source_sha256", {}).items():
                file = project / relative
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    failures.append("noncanonical lock path")
                elif not file.is_file() or file.resolve() != file:
                    failures.append("missing or aliased locked file: " + relative)
                elif hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                    failures.append("locked source mismatch: " + relative)
            for binary, expected in lock.get("toolchain_binary_sha256", {}).items():
                file = runtime / binary
                if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                    failures.append("locked Lean binary mismatch: " + binary)
        manifest = json.loads((project / "lean/lake-manifest.json").read_text())
        if not any(package.get("name") == "mathlib" for package in manifest["packages"]):
            failures.append("mathlib missing from frozen package manifest")
        # Match the strict verifier: optional package caches enter LEAN_PATH
        # when present, while Mathlib's compiled cache is mandatory. Cli, for
        # example, can occur in Lake's manifest without an imported Lean cache.
        cache = project / "lean/.lake/packages/mathlib/.lake/build/lib/lean"
        if not cache.is_dir():
            failures.append("missing Lean cache: mathlib")
        if not (project / "lean/.lake/packages/mathlib/.git").exists():
            failures.append("mathlib revision metadata unavailable")
    except (OSError, ValueError, KeyError, TypeError) as error:
        failures.append("cannot inspect fixed proof specification: " + type(error).__name__)
    return failures


def release_checks(project=PROJECT):
    """The source checkout is not yet a sealed portable artifact."""
    project = Path(project)
    path = project / "SEEKGNOMES_RELEASE.json"
    if not path.exists():
        return {"source_checkout": True, "sealed_release": False, "failures": []}
    failures = []
    try:
        lock = json.loads(path.read_text())
        if not isinstance(lock.get("files"), dict) or not lock["files"]:
            raise ValueError("missing files")
        for relative, expected in lock["files"].items():
            file = project / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or file.resolve() != file:
                failures.append("noncanonical release source path: " + relative)
            elif not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                failures.append("release source mismatch: " + relative)
        proof_lock = project / "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json"
        if hashlib.sha256(proof_lock.read_bytes()).hexdigest() != lock.get("proof_lock_sha256"):
            failures.append("release proof lock binding mismatch")
        if "state_proof_lock_sha256" in lock:
            state_lock = project / "revisions/seekgnomes_v1_2/STATE_PROOF_SPEC.lock.json"
            if hashlib.sha256(state_lock.read_bytes()).hexdigest() != lock["state_proof_lock_sha256"]:
                failures.append("release state proof lock binding mismatch")
    except (OSError, ValueError, KeyError, TypeError):
        failures.append("cannot inspect release source binding")
    return {"source_checkout": False, "sealed_release": not failures, "failures": failures}


def inspect(*, check_resources=True, check_locks=True, check_sandbox=True):
    """Never contact an API and never fall back to unisolated candidate code."""
    info = resources()
    failures = []
    modules = {}
    for name in REQUIRED_MODULES:
        try:
            modules[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            modules[name] = False
        if not modules[name]:
            failures.append("missing Python module: " + name)
    if info["system"] != "Linux" or info["architecture"] not in ("x86_64", "amd64"):
        failures.append("This pinned release requires Linux x86_64/amd64 inside the container")
    if not Path('/run/user').is_dir():
        failures.append('Missing /run/user directory required by the pinned Lean kernel sandbox')
    if check_resources:
        if info["effective_cpu_count"] is None or info["effective_cpu_count"] < MIN_CPUS:
            failures.append("At least 2 effective CPUs are required (including cgroup quota)")
        if info["available_memory_bytes"] is None or info["available_memory_bytes"] < MIN_AVAILABLE_MEMORY:
            failures.append("At least 12 GiB available RAM is required; allocate 16 GiB or more")
    for name in ("bwrap", "taskset", "git", "pdftotext"):
        if not shutil.which(name):
            failures.append("missing executable: " + name)
    try:
        ctypes.CDLL("libseccomp.so.2")
    except OSError:
        failures.append("missing libseccomp.so.2")
    if check_locks:
        failures.extend(strict_lock_checks())
    release = release_checks()
    failures.extend(release["failures"])
    sandbox = None
    if check_sandbox and shutil.which("bwrap"):
        try:
            command = ["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                       "--tmpfs", "/tmp", "--unshare-net", "--unshare-pid", "--die-with-parent",
                       "--", "/bin/true"]
            completed = subprocess.run(command, capture_output=True, timeout=20)
            sandbox = completed.returncode == 0
            if not sandbox:
                failures.append("nested bubblewrap namespaces unavailable; check container security flags")
        except (OSError, subprocess.SubprocessError):
            sandbox = False
            failures.append("bubblewrap namespace probe failed")
    return {"schema": "seekgnomes-preflight/v1", "ok": not failures, "resources": info,
            "requirements": {"min_effective_cpus": MIN_CPUS,
                             "min_available_memory_bytes": MIN_AVAILABLE_MEMORY,
                             "recommended_memory_bytes": 16 * GIB,
                             "parallel_trials": 1},
            "sandbox_probe_passed": sandbox, "failures": failures,
            "release": release, "python_modules": modules,
            "external_api_calls": 0, "isolation_fallback": False}
