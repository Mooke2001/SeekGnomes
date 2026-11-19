"""Exact energy of the rationally defined, externally preverified state."""
from fractions import Fraction
import hashlib
import json
import math

from .correlator_energy import normalize_task, normalize_word
from .rational_text import rational_text


MAX_BITS = 262144


def bounded(value):
    if type(value) is int:
        value = Fraction(value)
    if type(value) is not Fraction:
        raise ValueError("Rational f must return Fraction or int; float and bool are not accepted")
    if max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > MAX_BITS:
        raise ValueError("Exact energy arithmetic exceeds rational size limit")
    return value


def display(value):
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def evaluate_energy(task, f, *, f_manifest=None):
    """Compute sum c_j f(P_j)/q exactly for rationalized physical inputs.

    Legality and translation invariance remain externally proved premises.
    The answer concerns the rational f, not an ideal real formula that its
    rational approximation primitives were intended to approximate.
    """
    physical = normalize_task(task)
    rows = []
    total = Fraction(0)
    for index, term in enumerate(physical["terms"]):
        # Rational strings remain exact; floating JSON coefficients are given
        # their repr/decimal interpretation, never binary Fraction(float).
        coefficient = bounded(Fraction(str(term["coefficient"])))
        word = normalize_word(term["word"], physical["dimension"], physical["qubits_per_cell"])
        try:
            expectation = bounded(f(word))
        except Exception as exc:
            raise ValueError(f"Rational f failed for normalized term {index}: {str(exc)[:240]}") from exc
        contribution = bounded(coefficient * expectation)
        total = bounded(total + contribution)
        rows.append({"term_index": index, "coefficient": rational_text(coefficient), "word": term["word"],
                     "expectation_exact": rational_text(expectation), "expectation": display(expectation),
                     "contribution_per_cell_exact": rational_text(contribution),
                     "contribution_per_cell": display(contribution)})
    energy = bounded(total / physical["qubits_per_cell"])
    physical = {**physical, "terms": [{"coefficient": row["coefficient"], "word": row["word"]} for row in rows]}
    manifest = json.loads(json.dumps(f_manifest, allow_nan=False)) if f_manifest is not None else None
    return {
        "schema_version": "python-correlator-rational-energy-v1",
        "mode": "exact_rational", "energy_exact": rational_text(energy), "energy_density": display(energy),
        "energy_display": display(energy), "per_cell_exact": rational_text(total),
        "energy_normalization": "per_qubit", "dimension": physical["dimension"],
        "qubits_per_cell": physical["qubits_per_cell"],
        "formula": "e(f_Q) = sum_j coefficient_j * f_Q(P_j) / qubits_per_cell",
        "exactness_scope": "exact_for_rationally_defined_state_and_rationalized_coefficients",
        "numerical_display_only": True, "f_manifest": manifest,
        "task_physics_sha256": hashlib.sha256(json.dumps(physical, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "preconditions": {"legality": "assumed_preverified", "translation_invariance": "assumed_preverified",
                          "proof_checked_here": False},
        "energy_is_llm_reported": False, "terms": rows,
    }
