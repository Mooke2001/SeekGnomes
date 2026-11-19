module

public import QuantumManyBodyVariational.TrustedStateSpec

/-! Finite-qubit cells over an infinite lattice. Translation acts on the
lattice coordinate only, never on the internal qubit label. -/
@[expose] public section
noncomputable section
namespace QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2
open scoped BigOperators
open LocalPauli

abbrev Lattice (d : ℕ) := Fin d → ℤ
abbrev Site (d q : ℕ) := Lattice d × Fin q

def cellShift {d q : ℕ} (a : Lattice d) : Site d q ↪ Site d q where
  toFun s := (a + s.1, s.2)
  inj' := by
    intro s t h
    have heq : a + s.1 = a + t.1 ∧ s.2 = t.2 := by
      simpa only [Prod.mk.injEq] using h
    exact Prod.ext (add_left_cancel heq.1) heq.2

def cellTranslate {d q : ℕ} (a : Lattice d) (p : Word (Site d q)) : Word (Site d q) :=
  Finsupp.embDomain (cellShift a) p

def IsCellTranslationInvariant {d q : ℕ} (f : Correlator (Site d q)) : Prop :=
  ∀ a p, f (cellTranslate a p) = f p

def StateCondition (d q : ℕ) (f : Correlator (Site d q)) : Prop :=
  IsLegal f ∧ IsCellTranslationInvariant f

/-- One cell's interaction allocation. q_pos prevents empty-cell normalization.
All terms use the same placement and are divided by q exactly once. -/
structure LocalEnergy (d q n m : ℕ) where
  q_pos : 0 < q
  placement : Fin n ↪ Site d q
  coeff : Fin m → ℚ
  word : Fin m → (Fin n → Pauli)

namespace LocalEnergy
variable {d q n m : ℕ}
def cellValue (h : LocalEnergy d q n m) (f : Correlator (Site d q)) : ℝ :=
  ∑ j, (h.coeff j : ℝ) * f (place h.placement (h.word j))
def value (h : LocalEnergy d q n m) (f : Correlator (Site d q)) : ℝ :=
  h.cellValue f / (q : ℝ)
def matrix (h : LocalEnergy d q n m) : Matrix (Fin n → Fin 2) (Fin n → Fin 2) ℂ :=
  ∑ j, (h.coeff j : ℂ) • pauliTensor (h.word j)
theorem matrix_isHermitian (h : LocalEnergy d q n m) : h.matrix.IsHermitian := by
  apply Matrix.IsHermitian.ext
  intro i j
  simp only [matrix, Matrix.sum_apply, Matrix.smul_apply, smul_eq_mul,
    star_sum, star_mul', star_ratCast]
  exact Finset.sum_congr rfl fun k _ => congrArg ((h.coeff k : ℂ) * ·)
    ((TrustedStateSpec.LocalEnergy.pauliTensor_isHermitian (h.word k)).apply i j)
theorem cellValue_eq_trace (h : LocalEnergy d q n m) (f : Correlator (Site d q)) :
    Matrix.trace (localDensity f h.placement * h.matrix) = (h.cellValue f : ℂ) := by
  classical
  simp only [matrix, Matrix.mul_sum, Matrix.mul_smul, Matrix.trace_sum,
    Matrix.trace_smul, localDensity, trace_reconstruct_mul_pauliTensor,
    smul_eq_mul, cellValue]
  push_cast
  rfl
theorem value_eq_trace_re (h : LocalEnergy d q n m) (f : Correlator (Site d q)) :
    (Matrix.trace (localDensity f h.placement * h.matrix)).re / (q : ℝ) = h.value f := by
  rw [cellValue_eq_trace]
  rfl
def estimate (h : LocalEnergy d q n m) (v : Fin m → ℚ) : ℚ :=
  (∑ j, h.coeff j * v j) / (q : ℚ)
theorem value_error_bound (h : LocalEnergy d q n m)
    (f : Correlator (Site d q)) (v : Fin m → ℚ) (ε : Fin m → ℝ)
    (hv : ∀ j, |f (place h.placement (h.word j)) - (v j : ℝ)| ≤ ε j) :
    |h.value f - (h.estimate v : ℝ)| ≤
      (∑ j, |(h.coeff j : ℝ)| * ε j) / (q : ℝ) := by
  classical
  have hq : (0 : ℝ) < q := by exact_mod_cast h.q_pos
  have heq : h.cellValue f - ∑ j, (h.coeff j : ℝ) * (v j : ℝ) =
      ∑ j, (h.coeff j : ℝ) * (f (place h.placement (h.word j)) - (v j : ℝ)) := by
    simp only [cellValue, mul_sub, Finset.sum_sub_distrib]
  have herr : |h.cellValue f - ∑ j, (h.coeff j : ℝ) * (v j : ℝ)| ≤
      ∑ j, |(h.coeff j : ℝ)| * ε j := by
    rw [heq]
    exact (Finset.abs_sum_le_sum_abs _ _).trans (Finset.sum_le_sum fun j _ => by
      rw [abs_mul]
      exact mul_le_mul_of_nonneg_left (hv j) (abs_nonneg _))
  simp only [value, estimate, Rat.cast_div, Rat.cast_sum, Rat.cast_mul, Rat.cast_natCast]
  rw [← sub_div, abs_div, abs_of_pos hq]
  exact (div_le_div_iff_of_pos_right hq).mpr herr

theorem value_le_upper (h : LocalEnergy d q n m) (f : Correlator (Site d q))
    (v : Fin m → ℚ) (ε : Fin m → ℝ) (upper : ℚ)
    (hv : ∀ j, |f (place h.placement (h.word j)) - (v j : ℝ)| ≤ ε j)
    (hu : (h.estimate v : ℝ) + (∑ j, |(h.coeff j : ℝ)| * ε j) / q ≤ (upper : ℝ)) :
    h.value f ≤ (upper : ℝ) := by
  have herr := h.value_error_bound f v ε hv
  have hle := (le_abs_self (h.value f - (h.estimate v : ℝ))).trans herr
  linarith
end LocalEnergy

def CandidateAccepted {d q n m : ℕ} (h : LocalEnergy d q n m) (upper : ℚ)
    (f : Correlator (Site d q)) : Prop :=
  StateCondition d q f ∧ h.value f ≤ (upper : ℝ)

theorem accepted_trace_bound {d q n m : ℕ} {h : LocalEnergy d q n m} {upper : ℚ}
    {f : Correlator (Site d q)} (hf : CandidateAccepted h upper f) :
    (Matrix.trace (localDensity f h.placement * h.matrix)).re / (q : ℝ) ≤ (upper : ℝ) := by
  rw [h.value_eq_trace_re]
  exact hf.2
end QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2
