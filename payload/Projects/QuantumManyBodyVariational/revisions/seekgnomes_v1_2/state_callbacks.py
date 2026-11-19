"""Prepare, prove, and score the same typed correlator submission."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
from pathlib import Path

from src.state_format import StateProgram

from .state_verifier import (
    BITS, KIND, LOCK, PRECISION, PRECISION_BITS, PROOF_LIMIT, SOURCE_LIMIT,
    TARGET, TERMS, THEOREM, _old, _read, validate_lock, verify,
)


_BINDINGS = (
    "source_sha256", "generated_state_sha256", "runtime_sha256", "lock_sha256",
)
_PROOF_BINDINGS = (*_BINDINGS, "candidate_source_sha256")
_PROOF_TEMPLATE = """module
public import GeneratedState
public import QuantumManyBodyVariational.TrustedSpecExamplesV2
@[expose] public section
noncomputable section
namespace Submission

theorem accepted : GeneratedState.ProofObligation := by
  -- Replace skip with the proof of the generated obligation.
  skip

end Submission
"""


def callbacks(task, *, timeout=300):
    """Return the live broker callbacks for the supplied Hamiltonian."""
    dimension, qubits_per_cell = task["dimension"], task["qubits_per_cell"]

    def read_program(source_path):
        source = _read(Path(source_path), SOURCE_LIMIT).decode("utf-8")
        return StateProgram(source, dimension, qubits_per_cell, bits=BITS,
                            terms=TERMS, precision_bits=PRECISION_BITS)

    def describe(program):
        definition = program.lean_source()
        manifest = program.manifest()
        bindings = {
            "source_sha256": program.source_sha256,
            "generated_state_sha256": hashlib.sha256(definition.encode("utf-8")).hexdigest(),
            "runtime_sha256": manifest["runtime_sha256"],
            "lock_sha256": _old.digest(LOCK),
        }
        return definition, manifest, bindings

    def prepare(*, source_path, invocation_dir):
        program = read_program(source_path)
        definition, manifest, bindings = describe(program)
        validate_lock(manifest, PRECISION, bindings["lock_sha256"])
        return {
            "ok": True, "proof_verified": False, "generated_state": definition,
            "proof_template": _PROOF_TEMPLATE, **bindings,
            "dimension": dimension, "qubits_per_cell": qubits_per_cell, **PRECISION,
            "target": f"{THEOREM} : {TARGET}",
            "meaning": "Generated the fixed correlator and its Lean proof obligation.",
        }

    def check(*, source_path, proof_path, invocation_dir, energy=False):
        program = read_program(source_path)
        _, _, bindings = describe(program)
        bindings["candidate_source_sha256"] = hashlib.sha256(
            _read(Path(proof_path), PROOF_LIMIT)
        ).hexdigest()
        result = verify(program, proof_path, timeout=timeout,
                        evidence_dir=invocation_dir / "verification")
        certificate = result.get("certificate", {})
        passed = result["passed"] is True and all(
            result.get(key) is True for key in ("compile_passed", "goal_matched", "kernel_verified")
        )
        bound = (certificate.get("kind"), certificate.get("theorem"), certificate.get("target")) == (
            KIND, THEOREM, TARGET
        ) and all(result.get(key) == certificate.get(key) == expected
                  for key, expected in bindings.items())
        problems = list(result["problems"])
        if passed and not bound:
            problems.append("The Lean receipt differs from the submitted state, proof, or runtime.")
        passed = passed and bound
        response = {
            key: result.get(key) for key in
            (*_PROOF_BINDINGS, "candidate_export_sha256", "compile_passed", "goal_matched", "kernel_verified")
        }
        response.update(ok=passed, verified=passed, proof_verified=passed,
                        problems=problems, certificate=certificate if passed else None,
                        proof_manifest=result["manifest"], energy_certified=False,
                        energy_mode="approximate_float", energy_density=None)
        evidence = Path(result["manifest"]).parent
        diagnostics = {}
        for label in ("compile-generated", "compile-candidate", "export-candidate", "judge"):
            log = result["checks"].get(label, {}).get("log")
            if log and Path(log).parent == evidence:
                with _old.regular_input(Path(log)) as stream:
                    diagnostics[label] = stream.read(24000).decode(errors="replace")
        response["diagnostics"] = diagnostics
        if energy and passed:
            try:
                numeric = program.energy(task)
            except (ValueError, ArithmeticError, RuntimeError) as error:
                response.update(ok=False, numerical_failure=str(error))
                return response
            numeric["preconditions"] = {
                "legality": "verified_by_fixed_goal_kernel_replay",
                "translation_invariance": "verified_by_fixed_goal_kernel_replay",
                "numerical_preconditions": "verified_by_fixed_goal_kernel_replay",
                "proof_checked_here": True,
            }
            if program.program.limit:
                numeric["f_manifest"]["limit"]["tail_proof_checked_here"] = True
            numeric["verification"] = {**bindings, "kind": KIND, "theorem": THEOREM, "target": TARGET}
            report_path = invocation_dir / "energy.json"
            _old.write_new(report_path, (json.dumps(
                numeric, indent=2, ensure_ascii=False, allow_nan=False,
            ) + "\n").encode("utf-8"))
            response.update(energy_density=numeric["energy_density"],
                            energy_normalization="per_qubit", terms=numeric["terms"],
                            task_physics_sha256=numeric["task_physics_sha256"],
                            energy_report=str(report_path), improvement_is_certified=False)
            rule = _legacy_score_rule(task)
            if rule is not None:
                baseline, scale = rule
                response.update(baseline_energy=baseline, numerical_normalized_gain=max(
                    0.0, (baseline - numeric["energy_density"]) / scale,
                ))
        return response

    def lean(*, source_path, proof_path, invocation_dir):
        return check(source_path=source_path, proof_path=proof_path, invocation_dir=invocation_dir)

    def score(*, source_path, proof_path, invocation_dir):
        return check(source_path=source_path, proof_path=proof_path,
                     invocation_dir=invocation_dir, energy=True)

    return prepare, lean, score


def _legacy_score_rule(task):
    path = LOCK.parents[2] / "config/benchmark50_score_rules.json"
    rule = json.loads(path.read_text())["tasks"].get(task.get("id"))
    if rule is not None:
        return float(Fraction(rule["baseline"])), float(Fraction(rule["scale"]))
