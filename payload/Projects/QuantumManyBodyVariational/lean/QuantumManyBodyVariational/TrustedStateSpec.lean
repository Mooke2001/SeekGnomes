module

public import QuantumManyBodyVariational.CorrelatorStates
public import Mathlib.Tactic.Linarith

/-!
Fixed semantic interface for an infinite-qubit variational candidate.

The only variable state object is `f`. The lattice, local energy terms and
acceptance predicate belong to the problem specification, not the candidate.
`value_eq_trace` ties the energy expression to the actual Pauli matrices used
to reconstruct the state. This module does not assert a thermodynamic-limit
or ground-energy theorem, and does not require a finite-system sequence.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState.TrustedStateSpec

open scoped BigOperators
open LocalPauli

/-- The d-dimensional cubic lattice, infinite when `d > 0`.
The evaluator fixes `d` (in particular 2 or 3); it is not candidate-controlled. -/
abbrev Lattice (d : ℕ) := Fin d → ℤ

/-- The fixed, all-size physical condition on the sole state object `f`. -/
def StateCondition (d : ℕ) (f : Correlator (Lattice d)) : Prop :=
  IsTranslationInvariantState f

/-- A finite, evaluator-owned local energy density term. Every Pauli term is
placed in one common region, so relative positions cannot be independently
translated. Rational coefficients are part of the problem, not the state. -/
structure LocalEnergy (d n m : ℕ) where
  placement : Fin n ↪ Lattice d
  coeff : Fin m → ℚ
  word : Fin m → (Fin n → Pauli)

namespace LocalEnergy

variable {d n m : ℕ}

theorem pauli_isHermitian (p : Pauli) : (pauli p).IsHermitian := by
  apply Matrix.IsHermitian.ext
  intro i j
  fin_cases p <;> fin_cases i <;> fin_cases j <;> norm_num [pauli]

theorem pauliTensor_isHermitian {ι : Type} [Fintype ι] (p : ι → Pauli) :
    (pauliTensor p).IsHermitian := by
  apply Matrix.IsHermitian.ext
  intro i j
  simp only [pauliTensor, star_prod]
  exact Finset.prod_congr rfl fun x _ => (pauli_isHermitian (p x)).apply (i x) (j x)

/-- Exact energy functional on the global correlator. -/
def value (h : LocalEnergy d n m) (f : Correlator (Lattice d)) : ℝ :=
  ∑ j, (h.coeff j : ℝ) * f (place h.placement (h.word j))

/-- The actual finite matrix of the evaluator-owned local energy term. -/
def matrix (h : LocalEnergy d n m) :
    Matrix (Fin n → Fin 2) (Fin n → Fin 2) ℂ :=
  ∑ j, (h.coeff j : ℂ) • pauliTensor (h.word j)

/-- Rational Pauli coefficients give a genuine Hermitian observable. -/
theorem matrix_isHermitian (h : LocalEnergy d n m) : h.matrix.IsHermitian := by
  apply Matrix.IsHermitian.ext
  intro i j
  simp only [matrix, Matrix.sum_apply, Matrix.smul_apply, smul_eq_mul,
    star_sum, star_mul', star_ratCast]
  exact Finset.sum_congr rfl fun k _ => congrArg ((h.coeff k : ℂ) * ·)
    ((pauliTensor_isHermitian (h.word k)).apply i j)

/-- This is a matrix expectation, not an unrelated score bearing an energy name.
The identity is algebraic and does not need a legality assumption. -/
theorem value_eq_trace (h : LocalEnergy d n m) (f : Correlator (Lattice d)) :
    Matrix.trace (localDensity f h.placement * h.matrix) = (h.value f : ℂ) := by
  classical
  simp only [matrix, Matrix.mul_sum, Matrix.mul_smul, Matrix.trace_sum,
    Matrix.trace_smul, localDensity,
    trace_reconstruct_mul_pauliTensor, smul_eq_mul, value]
  push_cast
  rfl

theorem value_eq_trace_re (h : LocalEnergy d n m) (f : Correlator (Lattice d)) :
    (Matrix.trace (localDensity f h.placement * h.matrix)).re = h.value f := by
  rw [value_eq_trace]
  rfl

/-- A finite list of rational query results, with no independent state meaning. -/
def estimate (h : LocalEnergy d n m) (q : Fin m → ℚ) : ℚ :=
  ∑ j, h.coeff j * q j

/-- Propagate individual certified query errors to a certified energy error. -/
theorem value_error_bound (h : LocalEnergy d n m)
    (f : Correlator (Lattice d)) (q : Fin m → ℚ) (ε : Fin m → ℝ)
    (hq : ∀ j, |f (place h.placement (h.word j)) - (q j : ℝ)| ≤ ε j) :
    |h.value f - (h.estimate q : ℝ)| ≤ ∑ j, |(h.coeff j : ℝ)| * ε j := by
  classical
  have heq : h.value f - (h.estimate q : ℝ) =
      ∑ j, (h.coeff j : ℝ) *
        (f (place h.placement (h.word j)) - (q j : ℝ)) := by
    simp only [value, estimate, Rat.cast_sum, Rat.cast_mul, mul_sub,
      Finset.sum_sub_distrib]
  rw [heq]
  exact (Finset.abs_sum_le_sum_abs _ _).trans (Finset.sum_le_sum fun j _ => by
    rw [abs_mul]
    exact mul_le_mul_of_nonneg_left (hq j) (abs_nonneg _))

/-- A certified numerical upper endpoint bounds the exact candidate energy. -/
theorem value_le_upper (h : LocalEnergy d n m)
    (f : Correlator (Lattice d)) (q : Fin m → ℚ) (ε : Fin m → ℝ)
    (upper : ℚ)
    (hq : ∀ j, |f (place h.placement (h.word j)) - (q j : ℝ)| ≤ ε j)
    (hu : (h.estimate q : ℝ) + ∑ j, |(h.coeff j : ℝ)| * ε j ≤ (upper : ℝ)) :
    h.value f ≤ (upper : ℝ) := by
  have herr := h.value_error_bound f q ε hq
  have hle := (le_abs_self (h.value f - (h.estimate q : ℝ))).trans herr
  linarith

end LocalEnergy

/-- The fixed target theorem required of a submission. The evaluator supplies
`h`; the candidate supplies `f`, its claimed `upper`, and a proof of this proposition. -/
def CandidateAccepted {d n m : ℕ} (h : LocalEnergy d n m) (upper : ℚ)
    (f : Correlator (Lattice d)) : Prop :=
  StateCondition d f ∧ h.value f ≤ (upper : ℝ)

/-- Theorems about an accepted candidate retain the very same correlator. -/
theorem accepted_trace_bound {d n m : ℕ} {h : LocalEnergy d n m} {upper : ℚ}
    {f : Correlator (Lattice d)} (hf : CandidateAccepted h upper f) :
    (Matrix.trace (localDensity f h.placement * h.matrix)).re ≤ (upper : ℝ) := by
  rw [h.value_eq_trace_re]
  exact hf.2

end QuantumManyBodyVariational.InfiniteState.TrustedStateSpec
