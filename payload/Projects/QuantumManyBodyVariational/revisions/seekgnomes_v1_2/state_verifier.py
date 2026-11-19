"""Independent Lean verification of the shared-source state format."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any

from revisions.dual_agent_v1_1.verifier import (
    AXIOMS, ALLOWED_IMPORTS, BASE_LOCK, BIN, COMPILER_REVISION, EXPORT_ROOTS,
    LEAN, LOCK as LEGACY_LOCK, REQUIRED_SOURCES as LEGACY_SOURCES, RUNTIME,
    SAFE_MODULES, TARGET, THEOREM, V1_LOCK, _json_bytes, _new_evidence,
    _old, _project_source, _read,
)
from src.state_format import LANGUAGE_VERSION, StateProgram

LOCK = Path(__file__).resolve().with_name("STATE_PROOF_SPEC.lock.json")
SPEC = "state-format-proof/v1"
KIND = "state-format-fixed-goal-kernel-v1"
BITS, TERMS, PRECISION_BITS = 40, 32, 40
PRECISION = {"sqrt_bits": BITS, "series_terms": TERMS, "precision_bits": PRECISION_BITS}
SOURCE_LIMIT = 1024**2
PROOF_LIMIT = 2 * 1024**2
MPMATH_VERSION = "1.3.0"
REQUIRED_SOURCES = {
    "revisions/seekgnomes_v1_2/state_callbacks.py",
    "revisions/seekgnomes_v1_2/state_verifier.py",
    "src/correlator_energy.py",
    "src/correlator_float_guard.py",
    "src/correlator_hybrid_branch.py",
    "src/correlator_rational_math.py",
    "src/rational_text.py",
    "src/state_format.py",
    "src/state_format_eval.py",
    "src/state_format_ir.py",
    "src/state_format_lean.py",
    "src/state_format_math.py",
    "src/state_format_numeric.py",
    "src/state_format_parse.py",
}


def validate_lock(program_manifest: dict, precision: dict, lock_sha256: str) -> dict:
    """Verify the format's source pins and the inherited Lean environment."""
    lock_bytes = _read(LOCK, PROOF_LIMIT)
    if hashlib.sha256(lock_bytes).hexdigest() != lock_sha256:
        raise ValueError("State proof lock changed during verification")
    lock = json.loads(lock_bytes)
    expected = {
        "spec": SPEC,
        "kind": KIND,
        "language_version": LANGUAGE_VERSION,
        "generated_namespace": "GeneratedState",
        "required_theorem": THEOREM,
        "fixed_target": TARGET,
        "candidate_definition_holes": [],
        "permitted_axioms": AXIOMS,
        "precision": PRECISION,
        "mpmath_version": MPMATH_VERSION,
    }
    failures = [key for key, value in expected.items() if lock.get(key) != value]
    if (program_manifest["language_version"], program_manifest["mpmath_version"], precision) != (
        LANGUAGE_VERSION, MPMATH_VERSION, PRECISION
    ):
        failures.append("program language, numerical environment, or precision")

    legacy_bytes, base_bytes = _read(LEGACY_LOCK, PROOF_LIMIT), _read(BASE_LOCK, PROOF_LIMIT)
    legacy, base = json.loads(legacy_bytes), json.loads(base_bytes)
    if lock.get("legacy_lock_sha256") != hashlib.sha256(legacy_bytes).hexdigest():
        failures.append("inherited proof lock")
    if legacy.get("base_lock_sha256") != hashlib.sha256(base_bytes).hexdigest():
        failures.append("base v2 lock")
    if base.get("v1_lock_sha256") != _old.digest(V1_LOCK):
        failures.append("base v1 lock")
    if (legacy.get("compiler_revision"), legacy.get("required_theorem"), legacy.get("fixed_target"),
            legacy.get("permitted_axioms"), legacy.get("candidate_definition_holes")) != (
        COMPILER_REVISION, THEOREM, TARGET, AXIOMS, []
    ):
        failures.append("inherited theorem and checker")
    binaries = base["toolchain_binary_sha256"]
    if legacy.get("mathlib_revision") != base.get("mathlib_revision"):
        failures.append("mathlib revision binding")
    if legacy.get("toolchain_binary_sha256") != binaries:
        failures.append("toolchain binding")
    if set(binaries) != {"lean", "lake", "leanexport", "leanchecker"}:
        raise ValueError("Missing or unexpected frozen toolchain binary name")

    sources = lock["source_sha256"]
    legacy_sources = legacy["source_sha256"]
    missing = (REQUIRED_SOURCES - set(sources)) | (LEGACY_SOURCES - set(legacy_sources))
    failures.extend("missing source binding: " + name for name in sorted(missing))
    for catalog in (base["source_sha256"], legacy_sources, sources):
        for name, digest in catalog.items():
            if _old.digest(_project_source(name)) != digest:
                failures.append(name)
    for name, digest in binaries.items():
        if _old.digest(BIN / name) != digest:
            failures.append("toolchain/" + name)
    if failures:
        raise ValueError("Frozen state specification mismatch: " + ", ".join(failures))
    return legacy


def verify(program: StateProgram, proof_path: Path, *, timeout: int = 180,
           evidence_dir: Path | None = None) -> dict[str, Any]:
    """Compare the submitted proof with the exact generated theorem and replay it.

    Candidate elaboration, the challenge, and kernel replay use the same
    frozen generic modules, toolchain, and evidence layout as the earlier
    verifier. The candidate module and the program's numerical evaluator are
    never loaded into the judge.
    """
    dimension, qubits_per_cell = program.program.dimension, program.program.qubits_per_cell
    bits, terms = program.program.bits, program.program.terms
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
        "precision": {"sqrt_bits": bits, "series_terms": terms, "precision_bits": program.precision_bits},
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
        try:
            lock = validate_lock(report["program"], report["precision"], report["lock_sha256"])
        except (ValueError, OSError, KeyError, TypeError) as error:
            report["checks"][stage] = {"passed": False, "mismatches": [str(error)]}
            raise
        report["checks"][stage] = {"passed": True, "mismatches": []}
        return lock

    try:
        report["lock_sha256"] = _old.digest(LOCK)
        report["program"] = program.manifest()
        report["runtime_sha256"] = report["program"]["runtime_sha256"]
        lock = check_lock("lock-before")
        generated = program.lean_source().encode("utf-8")
        _old.write_new(trusted / "source.state", program.source.encode("utf-8"))
        _old.write_new(trusted / "GeneratedState.lean", generated)
        report["source_sha256"] = program.source_sha256
        report["generated_state_sha256"] = hashlib.sha256(generated).hexdigest()
        report["program"]["lean_definition_sha256"] = report["generated_state_sha256"]
        report["program"]["lean_namespace"] = "GeneratedState"
        _old.write_new(trusted / "program.json", _json_bytes(report["program"]))
        original_proof = _read(Path(os.path.abspath(proof_path)), PROOF_LIMIT)
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
        package_manifest = json.loads(_read(LEAN / "lake-manifest.json", PROOF_LIMIT))
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
        if _read(candidate / "Candidate.lean", PROOF_LIMIT) != original_proof:
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
        for name, digest in (
            ("source.state", report["source_sha256"]),
            ("GeneratedState.lean", report["generated_state_sha256"]),
            ("Submitted.lean", report["candidate_source_sha256"]),
        ):
            if _old.digest(trusted / name) != digest:
                raise ValueError("Frozen evidence changed during verification: " + name)
        report["passed"] = True
        report["certificate"] = {
            "kind": KIND, "theorem": THEOREM, "target": TARGET,
            "source_sha256": report["source_sha256"],
            "generated_state_sha256": report["generated_state_sha256"],
            "runtime_sha256": report["runtime_sha256"],
            "candidate_source_sha256": report["candidate_source_sha256"],
            "candidate_export_sha256": report["candidate_export_sha256"],
            "lock_sha256": report["lock_sha256"], "dimension": dimension,
            "qubits_per_cell": qubits_per_cell, "sqrt_bits": bits, "series_terms": terms,
            "precision_bits": program.precision_bits,
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


