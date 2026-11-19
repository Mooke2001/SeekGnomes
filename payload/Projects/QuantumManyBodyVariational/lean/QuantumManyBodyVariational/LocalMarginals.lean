module

public import QuantumManyBodyVariational.LocalPauli
public import Mathlib.Logic.Equiv.Prod
public import Mathlib.Data.Fintype.BigOperators

/-!
Actual matrix partial traces of complete local Pauli reconstructions.

The site type `α ⊕ β` is a disjoint decomposition into retained and traced
sites.  The theorem applies to arbitrary finite site types, including empty
regions.  It proves the matrix identity itself, without assuming marginal
compatibility as an additional hypothesis.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState.LocalPauli

open scoped BigOperators

variable {α β : Type*} [Fintype α] [Fintype β] [DecidableEq α] [DecidableEq β]

/-- Trace out the `β` sites in the computational basis. -/
def partialTrace (ρ : Matrix (α ⊕ β → Fin 2) (α ⊕ β → Fin 2) ℂ) :
    Matrix (α → Fin 2) (α → Fin 2) ℂ :=
  fun i j => ∑ k : β → Fin 2, ρ (Sum.elim i k) (Sum.elim j k)

omit [Fintype α] [DecidableEq α] in
theorem partialTrace_smul (c : ℂ)
    (ρ : Matrix (α ⊕ β → Fin 2) (α ⊕ β → Fin 2) ℂ) :
    partialTrace (c • ρ) = c • partialTrace ρ := by
  ext i j
  simp [partialTrace, Finset.mul_sum]

omit [Fintype α] [DecidableEq α] in
theorem partialTrace_sum {κ : Type*} [Fintype κ]
    (ρ : κ → Matrix (α ⊕ β → Fin 2) (α ⊕ β → Fin 2) ℂ) :
    partialTrace (∑ k, ρ k) = ∑ k, partialTrace (ρ k) := by
  ext i j
  simp only [partialTrace, Matrix.sum_apply]
  exact Finset.sum_comm

theorem sum_sumElim {M : Type*} [AddCommMonoid M]
    (g : (α ⊕ β → Pauli) → M) :
    (∑ p, g p) = ∑ pα : α → Pauli, ∑ pβ : β → Pauli, g (Sum.elim pα pβ) := by
  classical
  calc
    (∑ p, g p) =
        ∑ q : (α → Pauli) × (β → Pauli),
          g ((Equiv.sumArrowEquivProdArrow α β Pauli).symm q) :=
      ((Equiv.sumArrowEquivProdArrow α β Pauli).symm.sum_comp g).symm
    _ = _ := Fintype.sum_prod_type _

omit [DecidableEq α] in
theorem partialTrace_pauliTensor (p : α ⊕ β → Pauli) :
    partialTrace (pauliTensor p) =
      (if (fun b => p (Sum.inr b)) = (fun _ => 0)
       then (2 : ℂ) ^ Fintype.card β else 0) •
        pauliTensor (fun a => p (Sum.inl a)) := by
  classical
  ext i j
  simp only [partialTrace, pauliTensor, Fintype.prod_sum_type,
    Sum.elim_inl, Sum.elim_inr, Matrix.smul_apply, smul_eq_mul]
  rw [← Finset.mul_sum]
  have h := trace_pauliTensor (fun b => p (Sum.inr b))
  simp only [Matrix.trace, Matrix.diag, pauliTensor] at h
  rw [h, mul_comm]

theorem partialTrace_reconstruct (f : (α ⊕ β → Pauli) → ℂ) :
    partialTrace (reconstruct f) =
      reconstruct (fun p : α → Pauli => f (Sum.elim p (fun _ => 0))) := by
  classical
  simp only [reconstruct, partialTrace_smul, partialTrace_sum,
    partialTrace_pauliTensor, smul_smul]
  rw [sum_sumElim]
  change (2 : ℂ) ^ (-(Fintype.card (α ⊕ β) : ℤ)) •
      (∑ pα : α → Pauli, ∑ pβ : β → Pauli,
        (f (Sum.elim pα pβ) *
          (if pβ = (fun _ => 0) then (2 : ℂ) ^ Fintype.card β else 0)) •
            pauliTensor pα) = _
  simp only [mul_ite, mul_zero, ite_smul, zero_smul]
  simp only [Finset.sum_ite_eq', Finset.mem_univ, ite_true]
  have hsum :
      (∑ p : α → Pauli,
        (f (Sum.elim p (fun _ => 0)) * (2 : ℂ) ^ Fintype.card β) • pauliTensor p) =
      (2 : ℂ) ^ Fintype.card β •
        ∑ p : α → Pauli, f (Sum.elim p (fun _ => 0)) • pauliTensor p := by
    simp [Finset.smul_sum, smul_smul, mul_comm]
  rw [hsum, smul_smul]
  congr 1
  rw [Fintype.card_sum, Nat.cast_add, neg_add, zpow_add₀ (by norm_num : (2 : ℂ) ≠ 0)]
  simp [zpow_neg, zpow_natCast]

end QuantumManyBodyVariational.InfiniteState.LocalPauli
