module

public import Mathlib.Analysis.Complex.Basic
public import Mathlib.Data.Fintype.Pi
public import Mathlib.Algebra.BigOperators.Ring.Finset
public import Mathlib.LinearAlgebra.Matrix.PosDef
public import Mathlib.LinearAlgebra.Matrix.Trace
public import Mathlib.LinearAlgebra.Matrix.Notation
public import Mathlib.Tactic.FinCases
public import Mathlib.Tactic.NormNum

/-!
Finite-region Pauli coordinates for arbitrary finite site types.

`Pauli` uses the labels 0 = I, 1 = X, 2 = Y, 3 = Z.  In particular the
Y matrix contains the actual complex phases.  `reconstruct` is the complete
Pauli expansion, not a truncation to low-weight observables.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState.LocalPauli

open scoped BigOperators ComplexOrder

abbrev Pauli := Fin 4

def pauli (p : Pauli) : Matrix (Fin 2) (Fin 2) ℂ :=
  if p = 0 then !![1, 0; 0, 1]
  else if p = 1 then !![0, 1; 1, 0]
  else if p = 2 then !![0, -Complex.I; Complex.I, 0]
  else !![1, 0; 0, -1]

@[simp] theorem pauli_zero : pauli 0 = 1 := by
  ext i j
  fin_cases i <;> fin_cases j <;> norm_num [pauli]

theorem trace_pauli (p : Pauli) : Matrix.trace (pauli p) = if p = 0 then 2 else 0 := by
  fin_cases p <;> norm_num [pauli, Matrix.trace, Fin.sum_univ_two]

variable {ι : Type*} [Fintype ι] [DecidableEq ι]

def pauliTensor (p : ι → Pauli) : Matrix (ι → Fin 2) (ι → Fin 2) ℂ :=
  fun i j => ∏ x, pauli (p x) (i x) (j x)

def reconstruct (f : (ι → Pauli) → ℂ) : Matrix (ι → Fin 2) (ι → Fin 2) ℂ :=
  (2 : ℂ) ^ (-(Fintype.card ι : ℤ)) • ∑ p, f p • pauliTensor p

theorem trace_pauliTensor (p : ι → Pauli) :
    Matrix.trace (pauliTensor p) = if p = (fun _ => 0) then (2 : ℂ) ^ Fintype.card ι else 0 := by
  classical
  calc
    Matrix.trace (pauliTensor p) = ∏ x, Matrix.trace (pauli (p x)) := by
      simp only [Matrix.trace, Matrix.diag, pauliTensor]
      exact (Fintype.prod_sum (fun x (i : Fin 2) => pauli (p x) i i)).symm
    _ = if p = (fun _ => 0) then (2 : ℂ) ^ Fintype.card ι else 0 := by
      simp only [trace_pauli, Fintype.prod_ite_zero, Finset.prod_const, Finset.card_univ]
      congr 1
      exact propext funext_iff.symm

theorem trace_reconstruct (f : (ι → Pauli) → ℂ) :
    Matrix.trace (reconstruct f) = f (fun _ => 0) := by
  classical
  simp only [reconstruct, Matrix.trace_smul, Matrix.trace_sum, trace_pauliTensor]
  simp only [smul_eq_mul, mul_ite, mul_zero, Finset.sum_ite_eq', Finset.mem_univ,
    ite_true]
  rw [mul_left_comm, ← mul_assoc]
  simp [zpow_neg, zpow_natCast]

omit [DecidableEq ι] in
theorem pauliTensor_zero : pauliTensor (fun _ : ι => 0) = 1 := by
  classical
  ext i j
  simp only [pauliTensor, pauli_zero, Matrix.one_apply, Fintype.prod_boole]
  congr 1
  exact propext funext_iff.symm

/-- The Pauli coordinates of the normalized trace state on a finite region. -/
def tracialCoeff (p : ι → Pauli) : ℂ := if p = (fun _ => 0) then 1 else 0

theorem reconstruct_tracialCoeff :
    reconstruct (tracialCoeff (ι := ι)) =
      (2 : ℂ) ^ (-(Fintype.card ι : ℤ)) • (1 : Matrix (ι → Fin 2) (ι → Fin 2) ℂ) := by
  classical
  simp [reconstruct, tracialCoeff, pauliTensor_zero]

theorem trace_reconstruct_tracialCoeff :
    Matrix.trace (reconstruct (tracialCoeff (ι := ι))) = 1 := by
  simp [trace_reconstruct, tracialCoeff]

theorem posSemidef_reconstruct_tracialCoeff :
    (reconstruct (tracialCoeff (ι := ι))).PosSemidef := by
  classical
  rw [reconstruct_tracialCoeff]
  apply Matrix.PosSemidef.smul Matrix.PosSemidef.one
  apply zpow_nonneg
  norm_num

/-- Pauli coordinates of the product of the single-qubit `+` states. -/
def plusCoeff (p : ι → Pauli) : ℂ :=
  ∏ x, if p x = 0 ∨ p x = 1 then 1 else 0

theorem plus_single_sum (i j : Fin 2) :
    (∑ p : Pauli, (if p = 0 ∨ p = 1 then (1 : ℂ) else 0) * pauli p i j) = 1 := by
  fin_cases i <;> fin_cases j <;> norm_num [Fin.sum_univ_succ, pauli]

theorem reconstruct_plusCoeff :
    reconstruct (plusCoeff (ι := ι)) =
      (2 : ℂ) ^ (-(Fintype.card ι : ℤ)) •
        Matrix.vecMulVec (fun _ : ι → Fin 2 => (1 : ℂ))
          (star (fun _ : ι → Fin 2 => (1 : ℂ))) := by
  classical
  ext i j
  simp only [reconstruct, Matrix.smul_apply, smul_eq_mul, Matrix.sum_apply,
    plusCoeff, pauliTensor, Matrix.vecMulVec_apply, Pi.star_apply, star_one, mul_one]
  simp_rw [← Finset.prod_mul_distrib]
  rw [← Fintype.prod_sum (fun x (q : Pauli) =>
    (if q = 0 ∨ q = 1 then (1 : ℂ) else 0) * pauli q (i x) (j x))]
  simp only [plus_single_sum, Finset.prod_const_one, mul_one]

theorem trace_reconstruct_plusCoeff :
    Matrix.trace (reconstruct (plusCoeff (ι := ι))) = 1 := by
  simp [trace_reconstruct, plusCoeff]

theorem posSemidef_reconstruct_plusCoeff :
    (reconstruct (plusCoeff (ι := ι))).PosSemidef := by
  rw [reconstruct_plusCoeff]
  apply Matrix.PosSemidef.smul (Matrix.posSemidef_vecMulVec_self_star _)
  apply zpow_nonneg
  norm_num

end QuantumManyBodyVariational.InfiniteState.LocalPauli
