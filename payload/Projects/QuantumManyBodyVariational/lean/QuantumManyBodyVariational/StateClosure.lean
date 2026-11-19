module

public import QuantumManyBodyVariational.CorrelatorStates

/-!
Convex mixtures preserve legality and translation invariance, with no bound
on the size or shape of the local regions quantified over in `IsLegal`.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open scoped BigOperators ComplexOrder
open LocalPauli

variable {Γ : Type}

/-- A pointwise convex-combination formula; the weight bounds are premises
of the preservation theorems rather than restrictions on this definition. -/
def mix (t : ℝ) (f g : Correlator Γ) : Correlator Γ :=
  fun p => t * f p + (1 - t) * g p

theorem localDensity_mix {ι : Type} [Fintype ι] [DecidableEq ι]
    (t : ℝ) (f g : Correlator Γ) (e : ι ↪ Γ) :
    localDensity (mix t f g) e =
      (t : ℂ) • localDensity f e + ((1 - t : ℝ) : ℂ) • localDensity g e := by
  classical
  simp only [localDensity, reconstruct, mix, Complex.ofReal_add,
    Complex.ofReal_mul, add_smul, mul_smul, Finset.sum_add_distrib,
    ← Finset.smul_sum, smul_add]
  simp only [smul_smul, mul_comm]

theorem IsLegal.mix {f g : Correlator Γ} (hf : IsLegal f) (hg : IsLegal g)
    (t : ℝ) (ht : 0 ≤ t) (ht1 : t ≤ 1) : IsLegal (mix t f g) := by
  constructor
  · change t * f 0 + (1 - t) * g 0 = 1
    rw [hf.1, hg.1]
    simp
  · intro ι _ _ e
    rw [localDensity_mix]
    apply Matrix.PosSemidef.add
    · apply Matrix.PosSemidef.smul (hf.2 ι e)
      exact_mod_cast ht
    · apply Matrix.PosSemidef.smul (hg.2 ι e)
      exact_mod_cast sub_nonneg.mpr ht1

theorem isLegal_mix {f g : Correlator Γ} (hf : IsLegal f) (hg : IsLegal g)
    (t : ℝ) (ht : 0 ≤ t) (ht1 : t ≤ 1) : IsLegal (mix t f g) :=
  hf.mix hg t ht ht1

theorem IsTranslationInvariant.mix [AddGroup Γ] {f g : Correlator Γ}
    (hf : IsTranslationInvariant f) (hg : IsTranslationInvariant g) (t : ℝ) :
    IsTranslationInvariant (mix t f g) := by
  intro a p
  change t * f (translate a p) + (1 - t) * g (translate a p) =
    t * f p + (1 - t) * g p
  rw [hf a p, hg a p]

theorem IsTranslationInvariantState.mix [AddGroup Γ] {f g : Correlator Γ}
    (hf : IsTranslationInvariantState f) (hg : IsTranslationInvariantState g)
    (t : ℝ) (ht : 0 ≤ t) (ht1 : t ≤ 1) :
    IsTranslationInvariantState (mix t f g) :=
  ⟨hf.1.mix hg.1 t ht ht1, hf.2.mix hg.2 t⟩

end QuantumManyBodyVariational.InfiniteState
