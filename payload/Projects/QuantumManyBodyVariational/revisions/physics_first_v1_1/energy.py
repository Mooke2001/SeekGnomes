"""Conditional energy estimates for the physics exploration stage.

Candidate text is interpreted by the existing restricted AST language. It is
never imported or executed as Python, and this module neither generates Lean
nor invokes a verifier. The returned number is only sum_j c_j f(P_j) / q. Until
legality, cell translation invariance, and numerical obligations are proved,
even a very low value carries no variational upper-bound meaning.
"""
from __future__ import annotations

import hashlib
import json
import math

from src.correlator_energy import evaluate_energy, normalize_task
from src.correlator_hybrid_branch import LANGUAGE_POLICY
from revisions.physics_first_v1_1.compiler import CorrelatorProgram, LANGUAGE_VERSION, COMPILER_REVISION


SCHEMA_VERSION = "physics-first-exploration-energy-v1"
SQRT_BITS = 40
SERIES_TERMS = 32
ELIGIBILITY = "exploration_only_pending_legality_and_TI_proof"


def _sha256_json(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unproved_status() -> dict:
    return {
        "verified": False,
        "proof_verified": False,
        "energy_certified": False,
        "floating_point_certified": False,
        "is_variational_upper_bound": False,
        "eligibility": ELIGIBILITY,
        "preconditions": {
            "legality": "not_checked",
            "translation_invariance": "not_checked",
            "numerical_preconditions": "not_proved",
            "proof_checked_here": False,
        },
        "interpretation": (
            "Conditional numerical functional value only. Without a legal, "
            "cell-translation-invariant state and its required numerical "
            "proofs, this value is not a variational upper bound."
        ),
    }


def explore_energy(source: str, task: dict) -> dict:
    """Return a safe, explicitly unverified exploration result.

    Task normalization and numeric evaluation reuse the frozen implementation.
    No energy threshold determines legality or eligibility. Input, restricted
    language, numerical-domain, and resource errors become JSON-safe error
    records with ``ok=False`` and ``evaluated=False``; they do not escape to
    the calling tool. A successful estimate always remains unverified.
    """
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "evaluated": False,
        "status": "invalid_input_or_evaluation_error",
        "energy_density": None,
        "energy_normalization": "per_qubit",
        "source_sha256": None,
        "task_physics_sha256": None,
        "terms_sha256": None,
        "terms": [],
        **_unproved_status(),
    }
    try:
        if type(source) is not str:
            raise ValueError("source must be a string in the restricted correlator language")
        result["source_sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        physical = normalize_task(task)
        result["task_physics_sha256"] = _sha256_json(physical)
        result["terms_sha256"] = _sha256_json(physical["terms"])
        program = CorrelatorProgram.from_source(
            source, physical["dimension"], physical["qubits_per_cell"],
            bits=SQRT_BITS, terms=SERIES_TERMS,
        )
        numeric = evaluate_energy(task, program.evaluate)

        # Do not call program.manifest(): it generates a Lean definition.
        provenance = {
            "language_version": LANGUAGE_VERSION,
            "compiler_revision": COMPILER_REVISION,
            "branch_semantics_version": LANGUAGE_POLICY,
            "python_source_sha256": program.source_sha256,
            "bits": SQRT_BITS,
            "terms": SERIES_TERMS,
            "execution": "restricted_ast_interpreter",
            "value_semantics": "approximate_float_values",
            "numerical_error_certified": False,
        }
        result.update(numeric)
        result.update({
            "schema_version": SCHEMA_VERSION,
            "energy_evaluator_schema_version": numeric["schema_version"],
            "ok": True,
            "evaluated": True,
            "status": "evaluated",
            "source_sha256": program.source_sha256,
            "f_manifest": provenance,
            "ast_semantics": provenance.copy(),
            **_unproved_status(),
        })

        # These are necessary conditions on finitely many queried values only.
        # A failure is informative; a pass cannot establish state legality.
        diagnostics = {
            "scope": "identity_and_hamiltonian_words_only",
            "is_legality_check": False,
            "proves_legality": False,
            "hamiltonian_expectations_within_unit_interval": all(
                abs(row["expectation"]) <= 1.0 for row in numeric["terms"]
            ),
        }
        try:
            identity = float(program.evaluate(()))
            if not math.isfinite(identity):
                raise ValueError("identity expectation must be finite")
            diagnostics.update({"identity_expectation": identity,
                                "identity_equals_one": identity == 1.0})
        except Exception as exc:
            # The energy can be evaluated even when an additional diagnostic
            # query fails. Preserve that distinction without certifying either.
            diagnostics.update({"identity_expectation": None,
                                "identity_equals_one": None,
                                "identity_error": str(exc)[:500]})
        result["necessary_condition_diagnostics"] = diagnostics
    except Exception as exc:
        result["error"] = str(exc)[:1000]
        result["error_type"] = type(exc).__name__
    return result
