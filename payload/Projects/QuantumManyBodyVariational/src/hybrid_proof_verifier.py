"""Verify a proof of the exact host-generated hybrid correlator specification.

The candidate elaborator is untrusted.  Its frozen export is compared with a
host-owned theorem target, with no definition holes, and independently replayed
by Lean's kernel.  A successful source compilation alone is never acceptance.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any
from uuid import uuid4

from .python_correlator_hybrid import CorrelatorProgram


PROJECT = Path(__file__).resolve().parents[1]
LEAN = PROJECT / "lean"
RUNTIME = PROJECT / "tmp/lean-runtime/lean-4.35.0-linux"
BIN = RUNTIME / "bin"
LOCK = PROJECT / "HYBRID_PROOF_SPEC.lock.json"
BASE_LOCK = PROJECT / "STATE_SPEC_V2.lock.json"
V1_LOCK = PROJECT / "STATE_SPEC_V1.lock.json"
SPEC = "hybrid-state-proof/v1"
THEOREM = "Submission.accepted"
TARGET = "GeneratedState.ProofObligation"
AXIOMS = ["propext", "Quot.sound", "Classical.choice"]

# The frozen v2 checker supplies the already reviewed safe-file, process, and
# resource primitives.  Its old CLI is not run and its sources are not edited.
_helper_spec = importlib.util.spec_from_file_location(
    "_hybrid_frozen_v2_checker", PROJECT / "scripts/check_state_spec_v2.py"
)
assert _helper_spec is not None and _helper_spec.loader is not None
_old = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(_old)

SAFE_MODULES = [*_old.SAFE_PROJECT_MODULES, "RationalMath", "HybridMath"]
ALLOWED_IMPORTS = {"GeneratedState", "Mathlib"} | {
    "QuantumManyBodyVariational." + name for name in SAFE_MODULES
}
EXPORT_ROOTS = [
    name for name in _old.PRIMITIVES
    if name not in {"Submission.f", "Submission.bound"}
] + ["GeneratedState.f", TARGET]
REQUIRED_SOURCES = {
    "src/hybrid_proof_verifier.py", "scripts/check_hybrid_state.py",
    "src/python_correlator_hybrid.py", "src/python_correlator_v2.py",
    "src/python_correlator_compiler.py", "src/correlator_float_guard.py",
    "src/correlator_hybrid_branch.py", "src/correlator_rational_math.py",
    "scripts/check_state_spec_v2.py", "lean/lake-manifest.json",
} | {f"lean/QuantumManyBodyVariational/{name}.lean" for name in SAFE_MODULES}


def _read(path: Path, limit: int = 512 * 1024**2) -> bytes:
    with _old.regular_input(path, limit) as incoming:
        return incoming.read()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _project_source(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError("Noncanonical frozen source path: " + name)
    full = PROJECT / path
    if full.resolve(strict=True) != full or not full.is_relative_to(PROJECT):
        raise ValueError("Frozen source path crosses an alias: " + name)
    return full


def _new_evidence(requested: Path | None) -> Path:
    if requested is None:
        parent = PROJECT / "tmp"
        if parent.resolve(strict=True) != parent:
            raise ValueError("Evidence parent may not be a symbolic link")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        result = parent / ("hybrid-proof-check-" + stamp + "-" + uuid4().hex[:8])
    else:
        result = Path(os.path.abspath(requested))
        parent = result.parent.resolve(strict=True)
        if parent != result.parent:
            raise ValueError("Evidence directory may not cross a symbolic link")
    if not result.is_relative_to(PROJECT):
        raise ValueError("Evidence directory must be a new directory inside the project")
    result.mkdir(mode=0o700, exist_ok=False)
    return result


def verify(source: str, proof_path: Path, dimension: int, qubits_per_cell: int,
           *, bits: int = 40, terms: int = 32, timeout: int = 180,
           evidence_dir: Path | None = None) -> dict[str, Any]:
    """Return an evidence-backed verdict for the exact ``f`` and configuration.

    ``proof_path`` is a complete Lean module importing ``GeneratedState`` and
    defining ``Submission.accepted : GeneratedState.ProofObligation``.  The
    release lock is mandatory, including this verifier's own source hash.
    Each compilation/export subprocess has the supplied deadline; the judge
    has a minimum deadline of 180 seconds.  The complete pipeline can take more
    than one deadline.  No untrusted Python source is executed.
    """
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("timeout must be an integer in 1..3600 seconds")
    evidence = _new_evidence(evidence_dir)
    trusted, candidate = evidence / "trusted", evidence / "candidate"
    export_scratch, export_output = evidence / "export-scratch", evidence / "export-output"
    for directory in (trusted, candidate, export_scratch, export_output):
        directory.mkdir()
    report: dict[str, Any] = {
        "spec": SPEC, "created_utc": datetime.now(timezone.utc).isoformat(),
        "passed": False, "compile_passed": False, "goal_matched": False,
        "kernel_verified": False, "checks": {}, "problems": [],
        "dimension": dimension, "qubits_per_cell": qubits_per_cell,
        "precision": {"sqrt_bits": bits, "series_terms": terms},
        "theorem": THEOREM, "target": TARGET, "candidate_definition_holes": [],
        "resource_policy": {
            "local_cpu_affinity": sorted(os.sched_getaffinity(0))[:2],
            "candidate_process_creation": "threads_only",
            "address_space_bytes": 12 * 1024**3,
            "per_subprocess_timeout_seconds": timeout,
            "judge_timeout_seconds": max(timeout, 180),
            "network": "unshared", "candidate_environment": "credential_free",
        },
        "trust_scope": {
            "compiler_correctness": "trusted_implementation_not_formally_proved",
            "numerical_error_certified": False,
            "claims": "exact_generated_Lean_state_condition_and_numerical_preconditions",
        },
    }
    filter_fd: int | None = None
    env = {
        "PATH": f"{BIN}:/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
        "LEAN_SYSROOT": str(RUNTIME), "MATHLIB_NO_CACHE_ON_UPDATE": "1",
        "GIT_TERMINAL_PROMPT": "0", "LEAN_NUM_THREADS": "2",
    }

    def run(label: str, command: list[str], *, cwd: Path = LEAN,
            process_env: dict[str, str] | None = None,
            export: Path | None = None, deadline: int | None = None) -> str:
        log = evidence / (label + ".log")
        timed_out = False
        with ExitStack() as files:
            out = files.enter_context(_old.new_output(export or log))
            err = files.enter_context(_old.new_output(log)) if export else subprocess.STDOUT
            inherited = ()
            if filter_fd is not None:
                os.lseek(filter_fd, 0, os.SEEK_SET)
                inherited = (filter_fd,)
            process = subprocess.Popen(
                command, cwd=cwd, env=process_env or env, stdout=out, stderr=err,
                pass_fds=inherited, preexec_fn=_old._limits,
            )
            try:
                process.communicate(timeout=deadline or timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        item = {
            "passed": process.returncode == 0 and not timed_out,
            "exit_code": process.returncode, "timed_out": timed_out,
            "log": str(log), "log_sha256": _old.digest(log), "command": command,
        }
        if export is not None:
            item["export_sha256"] = _old.digest(export)
        report["checks"][label] = item
        if timed_out:
            raise ValueError(label + ": deadline exceeded; see " + str(log))
        if process.returncode:
            raise ValueError(f"{label}: exit {process.returncode}; see {log}")
        return _read(log).decode(errors="replace")

    def check_lock(stage: str) -> dict[str, Any]:
        lock = json.loads(_read(LOCK, 2 * 1024**2))
        base = json.loads(_read(BASE_LOCK, 2 * 1024**2))
        failures = []
        if lock.get("spec") != SPEC:
            failures.append("hybrid lock spec")
        if lock.get("required_theorem") != THEOREM:
            failures.append("required theorem")
        if lock.get("generated_namespace") != "GeneratedState" or lock.get("fixed_target") != TARGET:
            failures.append("fixed generated namespace or target")
        if lock.get("candidate_definition_holes") != []:
            failures.append("candidate definition holes must be empty")
        if set(lock.get("permitted_axioms", [])) != set(AXIOMS):
            failures.append("permitted axioms")
        if lock.get("base_lock_sha256") != _old.digest(BASE_LOCK):
            failures.append("base v2 lock")
        if base.get("v1_lock_sha256") != _old.digest(V1_LOCK):
            failures.append("base v1 lock")
        if lock.get("mathlib_revision") != base.get("mathlib_revision"):
            failures.append("mathlib revision binding")
        if lock.get("toolchain_binary_sha256") != base.get("toolchain_binary_sha256"):
            failures.append("toolchain binding")
        source_hashes = lock.get("source_sha256", {})
        missing = REQUIRED_SOURCES - set(source_hashes)
        failures.extend("missing source binding: " + name for name in sorted(missing))
        for catalog in (base.get("source_sha256", {}), source_hashes):
            for name, expected in catalog.items():
                if _old.digest(_project_source(name)) != expected:
                    failures.append(name)
        for name, expected in base["toolchain_binary_sha256"].items():
            if name not in {"lean", "lake", "leanexport", "leanchecker"}:
                raise ValueError("Unexpected toolchain binary name")
            if _old.digest(BIN / name) != expected:
                failures.append("toolchain/" + name)
        if _old.digest(LOCK) != report["lock_sha256"]:
            failures.append("hybrid lock changed during verification")
        report["checks"][stage] = {"passed": not failures, "mismatches": failures}
        if failures:
            raise ValueError("Frozen hybrid specification mismatch: " + ", ".join(failures))
        return lock

    try:
        report["lock_sha256"] = _old.digest(LOCK)
        lock = check_lock("lock-before")
        program = CorrelatorProgram.from_source(source, dimension, qubits_per_cell,
                                                bits=bits, terms=terms)
        generated = program.lean_definition(namespace="GeneratedState").encode()
        _old.write_new(trusted / "source.py", source.encode())
        _old.write_new(trusted / "GeneratedState.lean", generated)
        report["source_sha256"] = program.source_sha256
        report["generated_state_sha256"] = hashlib.sha256(generated).hexdigest()
        report["program"] = program.manifest()
        report["program"]["lean_definition_sha256"] = report["generated_state_sha256"]
        report["program"]["lean_namespace"] = "GeneratedState"
        _old.write_new(trusted / "program.json", _json_bytes(report["program"]))
        original_proof = _read(Path(os.path.abspath(proof_path)), 2 * 1024**2)
        proof_text = original_proof.decode()
        imports = re.findall(r"^\s*(?:public\s+)?import\s+([^\n]+)", proof_text, re.M)
        names = [name for line in imports for name in line.split("--", 1)[0].split()]
        if "GeneratedState" not in names or any(
            name not in ALLOWED_IMPORTS and not name.startswith("Mathlib.") for name in names
        ):
            raise ValueError("Proof imports must include GeneratedState and use only the generic API or Mathlib")
        _old.write_new(candidate / "Candidate.lean", original_proof)
        _old.write_new(trusted / "Submitted.lean", original_proof)
        report["candidate_source_sha256"] = hashlib.sha256(original_proof).hexdigest()

        process_filter = trusted / "candidate-process-filter.bpf"
        _old._sandbox_process_filter(process_filter)
        filter_fd = os.open(process_filter, os.O_RDONLY | os.O_NOFOLLOW)
        revision = run("mathlib-revision", ["/usr/bin/git", "-C", str(LEAN / ".lake/packages/mathlib"), "rev-parse", "HEAD"]).strip()
        if revision != lock["mathlib_revision"]:
            raise ValueError("mathlib revision differs from frozen revision")
        run("mathlib-clean", ["/usr/bin/git", "-C", str(LEAN / ".lake/packages/mathlib"), "diff", "--exit-code", "HEAD", "--"])
        # Read the frozen manifest without evaluating lake package configuration
        # or building anything in the workspace.  Installed dependency caches
        # are read-only; missing prerequisites fail at the trusted compile.
        package_manifest = json.loads(_read(LEAN / "lake-manifest.json", 2 * 1024**2))
        package_root = LEAN / ".lake/packages"
        lean_path = []
        for package in package_manifest["packages"]:
            name = package["name"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                raise ValueError("Unexpected frozen package name")
            directory = package_root / name / ".lake/build/lib/lean"
            if directory.exists():
                if directory.resolve(strict=True) != directory:
                    raise ValueError("Package cache path crosses an alias: " + name)
                lean_path.append(str(directory))
        if str(package_root / "mathlib/.lake/build/lib/lean") not in lean_path:
            raise ValueError("Frozen mathlib dependency cache is missing")
        lean_path.append(str(RUNTIME / "lib/lean"))
        safe_lib = trusted / "safe-lib"
        safe_sources = safe_lib / "QuantumManyBodyVariational"
        safe_sources.mkdir(parents=True)
        for name in SAFE_MODULES:
            _old.freeze_regular(
                _project_source("lean/QuantumManyBodyVariational/" + name + ".lean"),
                safe_sources / (name + ".lean"),
            )
        lean_path.insert(0, str(safe_lib))
        report["checks"]["lean-path"] = {"passed": True, "paths": lean_path,
            "source": "frozen_lake_manifest_and_pinned_runtime"}

        def sandbox(work: Path, writable: Path) -> list[str]:
            command = [
                "/usr/bin/bwrap", "--seccomp", str(filter_fd), "--tmpfs", "/",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session",
            ]
            for path in dict.fromkeys(["/usr", "/lib", "/lib64", str(RUNTIME), *lean_path]):
                if Path(path).exists():
                    command += ["--ro-bind", path, path]
            command += ["--ro-bind", str(trusted), str(trusted)]
            command += ["--bind", str(writable), str(writable), "--chdir", str(work), "--"]
            return command

        sandbox_env = dict(env, LEAN_PATH=":".join([str(trusted), *lean_path, str(candidate)]))
        trusted_command = sandbox(trusted, trusted)
        for name in SAFE_MODULES:
            relative = "QuantumManyBodyVariational/" + name
            run("compile-generic-" + name,
                sandbox(safe_lib, trusted) + [str(BIN / "lean"), "-j2", "-M8192",
                    relative + ".lean", "-o", relative + ".olean"],
                cwd=safe_lib, process_env=sandbox_env)
        report["generic_module_sha256"] = {
            name: _old.digest(safe_sources / (name + ".olean")) for name in SAFE_MODULES
        }
        run("compile-generated", trusted_command + [str(BIN / "lean"), "-j2", "-M8192",
            "GeneratedState.lean", "-o", "GeneratedState.olean"], cwd=trusted, process_env=sandbox_env)
        challenge = (
            "module\npublic import GeneratedState\n@[expose] public section\n"
            "namespace Submission\ntheorem accepted : GeneratedState.ProofObligation := by sorry\n"
            "end Submission\n"
        )
        _old.write_new(trusted / "Challenge.lean", challenge.encode())
        report["challenge_source_sha256"] = _old.digest(trusted / "Challenge.lean")
        run("compile-challenge", trusted_command + [str(BIN / "lean"), "-j2", "-M8192",
            "Challenge.lean", "-o", "Challenge.olean"], cwd=trusted, process_env=sandbox_env)
        run("export-challenge", trusted_command + [str(BIN / "leanexport"), "Challenge", "--", *EXPORT_ROOTS],
            cwd=trusted, process_env=sandbox_env, export=trusted / "Challenge.ndjson")
        report["challenge_export_sha256"] = _old.digest(trusted / "Challenge.ndjson")

        run("compile-candidate", sandbox(candidate, candidate) + [str(BIN / "lean"), "-j2", "-M8192",
            "Candidate.lean", "-o", "Candidate.olean"], cwd=candidate, process_env=sandbox_env)
        report["compile_passed"] = True
        _old.check_candidate_tree(candidate)
        if _read(candidate / "Candidate.lean", 2 * 1024**2) != original_proof:
            raise ValueError("Candidate changed its submitted source during compilation")
        if (candidate / "Candidate.ndjson").exists():
            raise ValueError("Candidate precreated the reserved export output")
        frozen = trusted / "candidate-module"
        frozen.mkdir()
        for name in ("Candidate.olean", "Candidate.olean.private", "Candidate.olean.server", "Candidate.ir", "Candidate.ir.sig"):
            artifact = candidate / name
            if name == "Candidate.olean" or artifact.exists():
                _old.freeze_regular(artifact, frozen / name)
        report["candidate_module_sha256"] = _old.digest(frozen / "Candidate.olean")
        export_env = dict(env, LEAN_PATH=":".join([str(trusted), *lean_path, str(frozen)]))
        raw_export = export_output / "Candidate.ndjson"
        run("export-candidate", sandbox(export_scratch, export_scratch) + [str(BIN / "leanexport"), "Candidate", "--", *EXPORT_ROOTS],
            cwd=export_scratch, process_env=export_env, export=raw_export)
        _old.check_candidate_tree(export_scratch)
        _old.freeze_regular(raw_export, trusted / "judge-input.ndjson")
        report["candidate_export_sha256"] = _old.digest(trusted / "judge-input.ndjson")
        config = {
            "challenge_module": "Challenge", "solution_module": "Candidate",
            "theorem_names": [THEOREM], "definition_names": [],
            "permitted_axioms": lock["permitted_axioms"],
        }
        _old.write_new(trusted / "comparator.json", _json_bytes(config))
        # The judge consumes only frozen data exports; it never imports or
        # executes Candidate.olean.  This step is separate from elaboration.
        command = [
            "/usr/bin/bwrap", "--ro-bind", "/", "/", "--bind", str(evidence), str(evidence),
            "--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc", "--unshare-net",
            "--die-with-parent", "--", str(BIN / "lake"), "comparator", "--config",
            str(trusted / "comparator.json"), "--challenge-from-export", str(trusted / "Challenge.ndjson"),
            "--solution-from-export", str(trusted / "judge-input.ndjson"),
        ]
        judge = run("judge", command, deadline=max(timeout, 180))
        if "Lean default kernel accepts the solution" not in judge or "Your solution is okay!" not in judge:
            raise ValueError("Comparator omitted the required goal-match and independent-kernel diagnostics")
        report["goal_matched"] = True
        report["kernel_verified"] = True
        check_lock("lock-after")
        if _old.digest(trusted / "GeneratedState.lean") != report["generated_state_sha256"]:
            raise ValueError("Host-generated fixed module changed during verification")
        report["passed"] = True
        report["certificate"] = {
            "kind": "hybrid-fixed-goal-kernel-v1", "theorem": THEOREM, "target": TARGET,
            "source_sha256": report["source_sha256"],
            "generated_state_sha256": report["generated_state_sha256"],
            "candidate_source_sha256": report["candidate_source_sha256"],
            "candidate_export_sha256": report["candidate_export_sha256"],
            "lock_sha256": report["lock_sha256"], "dimension": dimension,
            "qubits_per_cell": qubits_per_cell, "sqrt_bits": bits, "series_terms": terms,
            "candidate_definition_holes": [], "permitted_axioms": AXIOMS,
        }
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError,
            UnicodeError, SyntaxError, subprocess.SubprocessError) as error:
        report["problems"].append(str(error))
    finally:
        if filter_fd is not None:
            os.close(filter_fd)
    manifest = evidence / "manifest.json"
    report["manifest"] = str(manifest)
    _old.write_new(manifest, _json_bytes(report))
    return report


__all__ = ["verify", "SPEC", "TARGET", "THEOREM", "LOCK"]
