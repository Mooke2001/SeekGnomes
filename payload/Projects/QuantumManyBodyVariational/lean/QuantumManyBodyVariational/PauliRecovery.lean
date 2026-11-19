module

public import QuantumManyBodyVariational.LocalPauli

/-!
Trace orthogonality and recovery of every finite-region Pauli coefficient.
The theorems are uniform in the finite site type; no fixed-size cutoff is used.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState.LocalPauli

open scoped BigOperators ComplexOrder

theorem trace_pauli_mul (p q : Pauli) :
    Matrix.trace (pauli p * pauli q) = if p = q then 2 else 0 := by
  fin_cases p <;> fin_cases q <;>
    norm_num [pauli, Matrix.trace, Matrix.mul_apply, Fin.sum_univ_two]

variable {ι : Type*} [Fintype ι] [DecidableEq ι]

theorem pauliTensor_mul_apply (p q : ι → Pauli) (i j : ι → Fin 2) :
    (pauliTensor p * pauliTensor q) i j =
      ∏ x, (pauli (p x) * pauli (q x)) (i x) (j x) := by
  simp only [Matrix.mul_apply, pauliTensor]
  simp_rw [← Finset.prod_mul_distrib]
  exact (Fintype.prod_sum (fun x (k : Fin 2) =>
    pauli (p x) (i x) k * pauli (q x) k (j x))).symm

theorem trace_pauliTensor_mul (p q : ι → Pauli) :
    Matrix.trace (pauliTensor p * pauliTensor q) =
      if p = q then (2 : ℂ) ^ Fintype.card ι else 0 := by
  calc
    Matrix.trace (pauliTensor p * pauliTensor q) =
        ∏ x, Matrix.trace (pauli (p x) * pauli (q x)) := by
      simp only [Matrix.trace, Matrix.diag, pauliTensor_mul_apply]
      exact (Fintype.prod_sum (fun x (i : Fin 2) =>
        (pauli (p x) * pauli (q x)) i i)).symm
    _ = if p = q then (2 : ℂ) ^ Fintype.card ι else 0 := by
      simp only [trace_pauli_mul, Fintype.prod_ite_zero,
        Finset.prod_const, Finset.card_univ]
      congr 1
      exact propext funext_iff.symm

/-- Every submitted coefficient is exactly the corresponding expectation in the
reconstructed matrix.  Positivity is not needed for this algebraic identity. -/
theorem trace_reconstruct_mul_pauliTensor (f : (ι → Pauli) → ℂ) (q : ι → Pauli) :
    Matrix.trace (reconstruct f * pauliTensor q) = f q := by
  classical
  simp only [reconstruct, Matrix.smul_mul, Matrix.sum_mul, Matrix.trace_smul,
    Matrix.trace_sum, trace_pauliTensor_mul]
  simp only [smul_eq_mul, mul_ite, mul_zero, Finset.sum_ite_eq',
    Finset.mem_univ, ite_true]
  rw [mul_left_comm, ← mul_assoc]
  simp [zpow_neg, zpow_natCast]

end QuantumManyBodyVariational.InfiniteState.LocalPauli
