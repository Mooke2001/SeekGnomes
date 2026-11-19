"""Floating-point energy of a preverified TI infinite-qubit correlator.

The contract is conditional: f already denotes a legal, cell-translation-
invariant state. This module does not prove that contract or sample translated
words to establish it. For h0 = sum(c_j P_j), the per-qubit energy is computed
as math.fsum(c_j * f(P_j)) / qubits_per_cell. No infinite sum is evaluated.

f receives a canonical tuple of (cell_tuple, qubit_index, Pauli_letter)
triples with X/Y/Z factors only. The identity is (). Callback and coefficient
values are converted to finite Python floats. These are numerical estimates,
not certified error bounds or rigorous variational upper bounds. This callback
API executes trusted code; untrusted source belongs in the AST compiler.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
import re
from typing import Callable


SCHEMA_VERSION = "python-correlator-energy-v1"
MAX_DIMENSION = 32
MAX_QUBITS_PER_CELL = 65536
MAX_TERMS = 10000
MAX_WORD_FACTORS = 4096
MAX_TOTAL_FACTORS = 100000
MAX_INPUT_BITS = 16384
MAX_NUMERIC_TEXT = 4096
_RATIONAL_TEXT = re.compile(r"[+-]?[0-9]+(?:/[+-]?[0-9]+)?\Z")

Word = tuple[tuple[tuple[int, ...], int, str], ...]


class CorrelatorEnergyError(ValueError):
    """Invalid task, callback result, or numerical overflow."""


def _as_float(value, label: str) -> float:
    if type(value) not in (int, float, Fraction):
        raise CorrelatorEnergyError(f"{label} must be an int, float, or Fraction (not bool)")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise CorrelatorEnergyError(f"{label} is outside finite floating-point range") from exc
    if not math.isfinite(result):
        raise CorrelatorEnergyError(f"{label} must be finite")
    return result


def _coefficient(value) -> str | float:
    # Preserve a rational input in the normalized task/provenance. Conversion
    # to float occurs in evaluation, not silently in the physical input hash.
    if type(value) is str:
        if not value or len(value) > MAX_NUMERIC_TEXT:
            raise CorrelatorEnergyError("coefficient text is empty or too long")
        try:
            if _RATIONAL_TEXT.fullmatch(value):
                rational = Fraction(value)
                _as_float(rational, "coefficient")
                return str(rational)
            return _as_float(float(value), "coefficient")
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            raise CorrelatorEnergyError("invalid or nonfinite coefficient") from exc
    _as_float(value, "coefficient")
    return str(value) if type(value) in (int, Fraction) else value


def _shape(d: int, q: int) -> None:
    if type(d) is not int or not 1 <= d <= MAX_DIMENSION:
        raise CorrelatorEnergyError(f"dimension must be an integer in 1..{MAX_DIMENSION}")
    if type(q) is not int or not 1 <= q <= MAX_QUBITS_PER_CELL:
        raise CorrelatorEnergyError(f"qubits_per_cell must be an integer in 1..{MAX_QUBITS_PER_CELL}")


def normalize_word(word, d: int, q: int) -> Word:
    """Validate/canonicalize a finite Hermitian Pauli word.

    Accepts JSON factor dictionaries or Python factor triples. Repeated sites
    are rejected instead of multiplying Pauli matrices with untracked phases.
    """
    _shape(d, q)
    if type(word) not in (list, tuple) or len(word) > MAX_WORD_FACTORS:
        raise CorrelatorEnergyError(f"word needs at most {MAX_WORD_FACTORS} factors")
    result, seen = [], set()
    for factor in word:
        if type(factor) is dict:
            try:
                cell, qubit, pauli = factor["cell"], factor["qubit"], factor["pauli"]
            except KeyError as exc:
                raise CorrelatorEnergyError("each factor needs cell, qubit, and pauli") from exc
        elif type(factor) in (tuple, list) and len(factor) == 3:
            cell, qubit, pauli = factor
        else:
            raise CorrelatorEnergyError("each factor must be a dictionary or a (cell, qubit, pauli) triple")
        if type(cell) not in (list, tuple) or len(cell) != d:
            raise CorrelatorEnergyError("a cell needs exactly dimension coordinates")
        if any(type(x) is not int or abs(x).bit_length() > MAX_INPUT_BITS for x in cell):
            raise CorrelatorEnergyError("cell coordinates must be bounded integers")
        if type(qubit) is not int or not 0 <= qubit < q:
            raise CorrelatorEnergyError("qubit index is outside the cell")
        if type(pauli) is not str or pauli not in ("X", "Y", "Z"):
            raise CorrelatorEnergyError("Pauli factors must be X/Y/Z; use an empty word for identity")
        cell = tuple(cell)
        site = (cell, qubit)
        if site in seen:
            raise CorrelatorEnergyError("repeated site in one Hermitian Pauli word")
        seen.add(site)
        result.append((cell, qubit, pauli))
    return tuple(sorted(result, key=lambda factor: (factor[0], factor[1])))


def _word_json(word: Word) -> list[dict]:
    return [{"cell": list(cell), "qubit": qubit, "pauli": pauli} for cell, qubit, pauli in word]


def normalize_task(task: dict) -> dict:
    """Return canonical relevant physical input, without trusting stored hashes.

    Every listed term belongs to h0, allocated once per cell. Duplicate terms
    remain separate summands; only ordering is normalized. An empty terms list
    represents the zero Hamiltonian. The final normalization is per qubit.
    """
    if type(task) is not dict:
        raise CorrelatorEnergyError("task must be a dictionary")
    try:
        d, q, terms = task["dimension"], task["qubits_per_cell"], task["terms"]
    except KeyError as exc:
        raise CorrelatorEnergyError("task needs dimension, qubits_per_cell, and terms") from exc
    _shape(d, q)
    if task.get("energy_normalization", "per_qubit") != "per_qubit":
        raise CorrelatorEnergyError("this interface requires per_qubit normalization")
    if type(terms) is not list or len(terms) > MAX_TERMS:
        raise CorrelatorEnergyError(f"terms must be a list with at most {MAX_TERMS} entries")
    normalized, factors = [], 0
    for term in terms:
        if type(term) is not dict or "coefficient" not in term or "word" not in term:
            raise CorrelatorEnergyError("each term needs coefficient and word")
        word = normalize_word(term["word"], d, q)
        factors += len(word)
        if factors > MAX_TOTAL_FACTORS:
            raise CorrelatorEnergyError("task exceeds the total factor limit")
        normalized.append({"coefficient": _coefficient(term["coefficient"]), "word": _word_json(word)})
    normalized.sort(key=lambda term: (
        tuple((tuple(x["cell"]), x["qubit"], x["pauli"]) for x in term["word"]),
        str(term["coefficient"]),
    ))
    return {"dimension": d, "qubits_per_cell": q,
            "energy_normalization": "per_qubit", "terms": normalized}


def evaluate_energy(task: dict, f: Callable[[Word], object], *, f_manifest: dict | None = None) -> dict:
    """Numerically evaluate sum(c_j f(P_j))/q under the preverified-state contract.

    This function does not check f(identity), positivity, or TI. It also does
    not reconstruct the state from a fixed ansatz. All contributions are
    independently obtained by calling f on the Hamiltonian's finite words.
    """
    if not callable(f):
        raise CorrelatorEnergyError("f must be callable")
    physical = normalize_task(task)
    serialized = json.dumps(physical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest = None
    if f_manifest is not None:
        if type(f_manifest) is not dict:
            raise CorrelatorEnergyError("f_manifest must be a JSON dictionary")
        try:
            manifest_text = json.dumps(f_manifest, allow_nan=False)
            if len(manifest_text) > 1000000:
                raise CorrelatorEnergyError("f_manifest exceeds the size limit")
            manifest = json.loads(manifest_text)
        except (TypeError, ValueError) as exc:
            raise CorrelatorEnergyError("f_manifest must be finite JSON data") from exc
    q, d = physical["qubits_per_cell"], physical["dimension"]
    contributions, rows = [], []
    for index, term in enumerate(physical["terms"]):
        raw_coefficient = term["coefficient"]
        coefficient = _as_float(Fraction(raw_coefficient) if type(raw_coefficient) is str else raw_coefficient,
                                "coefficient")
        word = normalize_word(term["word"], d, q)
        try:
            value = _as_float(f(word), f"f(P) for term {index}")
        except CorrelatorEnergyError:
            raise
        except Exception as exc:
            raise CorrelatorEnergyError(f"f failed for normalized term {index}: {str(exc)[:240]}") from exc
        contribution = coefficient * value
        if not math.isfinite(contribution):
            raise CorrelatorEnergyError(f"nonfinite contribution for term {index}")
        contributions.append(contribution)
        rows.append({"term_index": index, **term, "coefficient_numeric": coefficient,
                     "expectation": value, "contribution_per_cell": contribution})
    try:
        per_cell = math.fsum(contributions)
        energy = per_cell / q
    except (OverflowError, ValueError) as exc:
        raise CorrelatorEnergyError("energy accumulation overflowed") from exc
    if not math.isfinite(energy) or not math.isfinite(per_cell):
        raise CorrelatorEnergyError("energy accumulation must be finite")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "approximate_float",
        "energy_density": energy,
        "energy_display": energy,
        "per_cell_energy": per_cell,
        "per_cell_display": per_cell,
        "energy_normalization": "per_qubit",
        "formula": "e(f) = sum_j coefficient_j * f(P_j) / qubits_per_cell",
        "dimension": d,
        "qubits_per_cell": q,
        "numeric_method": "math.fsum",
        "floating_point_certified": False,
        "task_physics_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "f_manifest": manifest,
        "preconditions": {"legality": "assumed_preverified",
                          "translation_invariance": "assumed_preverified",
                          "proof_checked_here": False},
        "energy_is_llm_reported": False,
        "terms": rows,
    }
