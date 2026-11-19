module

public import QuantumManyBodyVariational.CorrelatorStates
public import Mathlib.Tactic.Ring

/-!
# A normalized, translation-invariant correlator that is not legal

The constant-one assignment has every Pauli value in [-1, 1], but its one-site
Pauli reconstruction is `(I + X + Y + Z) / 2` and has a negative quadratic form.
This is an exact complex-matrix counterexample, not a floating-point PSD test.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open LocalPauli Matrix
open scoped BigOperators ComplexOrder

def allOne {Γ : Type} : Correlator Γ := fun _ => 1

theorem allOne_normalized {Γ : Type} : (allOne (Γ := Γ)) 0 = 1 := rfl

theorem allOne_bounds {Γ : Type} (p : Word Γ) :
    -1 ≤ allOne p ∧ allOne p ≤ 1 := by
  norm_num [allOne]

theorem allOne_isTranslationInvariant {Γ : Type} [AddGroup Γ] :
    IsTranslationInvariant (allOne (Γ := Γ)) := by
  intro a p
  rfl

def badOneSite : Matrix (Fin 2) (Fin 2) ℂ :=
  !![1, (1 - Complex.I) / 2; (1 + Complex.I) / 2, 0]

def badWitness : Fin 2 → ℂ := ![1, -1 - Complex.I]

theorem badWitness_quadratic :
    star badWitness ⬝ᵥ (badOneSite *ᵥ badWitness) = -1 := by
  norm_num [badWitness, badOneSite, dotProduct, Matrix.mulVec, Fin.sum_univ_two,
    Complex.ext_iff]

theorem badOneSite_not_posSemidef : ¬ badOneSite.PosSemidef := by
  intro h
  have hn := h.dotProduct_mulVec_nonneg badWitness
  rw [badWitness_quadratic] at hn
  have hr : (0 : ℝ) ≤ -1 := (Complex.le_def.mp hn).1
  norm_num at hr

theorem localDensity_allOne_single {Γ : Type} [DecidableEq Γ]
    (e : PUnit ↪ Γ) (i j : Fin 2) :
    localDensity (allOne (Γ := Γ)) e (fun _ => i) (fun _ => j) = badOneSite i j := by
  have hs : (∑ p : PUnit → Pauli, pauliTensor p (fun _ => i) (fun _ => j)) =
      ∑ p : Pauli, pauli p i j := by
    simpa [pauliTensor] using
      (Equiv.funUnique PUnit Pauli).sum_comp (fun p => pauli p i j)
  simp only [localDensity, reconstruct, allOne, Complex.ofReal_one, one_smul,
    Matrix.smul_apply, Matrix.sum_apply, Fintype.card_unique,
    _root_.zpow_neg, smul_eq_mul]
  rw [hs]
  fin_cases i <;> fin_cases j <;>
    norm_num [Fin.sum_univ_succ, pauli, badOneSite] <;> ring

theorem allOne_not_isLegal {Γ : Type} [DecidableEq Γ] [Nonempty Γ] :
    ¬ IsLegal (allOne (Γ := Γ)) := by
  intro h
  let x : Γ := Classical.choice inferInstance
  let e : PUnit ↪ Γ := ⟨fun _ => x, fun a b _ => Subsingleton.elim a b⟩
  have hp := (h.2 PUnit e).submatrix (fun i : Fin 2 => fun _ : PUnit => i)
  have he : (localDensity (allOne (Γ := Γ)) e).submatrix
      (fun i : Fin 2 => fun _ : PUnit => i) (fun i : Fin 2 => fun _ : PUnit => i) =
      badOneSite := by
    ext i j
    exact localDensity_allOne_single e i j
  rw [he] at hp
  exact badOneSite_not_posSemidef hp

/-- Normalization, translation invariance and valid individual expectation
bounds do not replace joint quantum positivity. -/
theorem allOne_counterexample {Γ : Type} [DecidableEq Γ] [AddGroup Γ] :
    (allOne (Γ := Γ)) 0 = 1 ∧ IsTranslationInvariant (allOne (Γ := Γ)) ∧
      (∀ p : Word Γ, -1 ≤ allOne p ∧ allOne p ≤ 1) ∧
      ¬ IsLegal (allOne (Γ := Γ)) :=
  ⟨allOne_normalized, allOne_isTranslationInvariant, allOne_bounds, allOne_not_isLegal⟩

end QuantumManyBodyVariational.InfiniteState
