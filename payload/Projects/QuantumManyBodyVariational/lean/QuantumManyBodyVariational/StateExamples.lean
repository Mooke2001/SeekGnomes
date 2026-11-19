module

public import QuantumManyBodyVariational.CorrelatorStates

/-!
# A complete, finite-query example of an infinite correlator

The infinite product of `+` states returns one precisely when every
nonidentity Pauli factor is X. Its definition only inspects the finite support
of the input word. The legality proof covers every finite site type and every
injective placement, and translation invariance covers every lattice shift.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open LocalPauli
open scoped BigOperators ComplexOrder Classical

variable {Γ : Type} [DecidableEq Γ]

/-- All nonidentity factors are X; this predicate inspects finite support only. -/
def plus (p : Word Γ) : ℝ := if ∀ x ∈ p.support, p x = 1 then 1 else 0

omit [DecidableEq Γ] in
theorem support_only_X_iff (p : Word Γ) :
    (∀ x ∈ p.support, p x = 1) ↔ ∀ x, p x = 0 ∨ p x = 1 := by
  constructor
  · intro h x
    by_cases hx : p x = 0
    · exact Or.inl hx
    · exact Or.inr (h x (Finsupp.mem_support_iff.mpr hx))
  · intro h x hx
    exact (h x).resolve_left (Finsupp.mem_support_iff.mp hx)

omit [DecidableEq Γ] in
theorem plus_eq_ite_all (p : Word Γ) :
    plus p = if ∀ x, p x = 0 ∨ p x = 1 then 1 else 0 := by
  simp only [plus, support_only_X_iff]

theorem only_X_place_iff {ι : Type} [Fintype ι] (e : ι ↪ Γ) (p : ι → Pauli) :
    (∀ x, place e p x = 0 ∨ place e p x = 1) ↔ ∀ i, p i = 0 ∨ p i = 1 := by
  constructor
  · intro h i
    simpa using h (e i)
  · intro h x
    by_cases hx : x ∈ Set.range e
    · obtain ⟨i, rfl⟩ := hx
      simpa using h i
    · left
      exact Finsupp.embDomain_of_notMem_range e _ x hx

theorem localDensity_plus {ι : Type} [Fintype ι] [DecidableEq ι]
    (e : ι ↪ Γ) :
    localDensity (plus (Γ := Γ)) e = reconstruct (plusCoeff (ι := ι)) := by
  have hc : (fun p : ι → Pauli => (plus (place e p) : ℂ)) = plusCoeff := by
    funext p
    simp only [plus_eq_ite_all, only_X_place_iff, plusCoeff, Fintype.prod_boole]
    split_ifs <;> simp
  exact congrArg reconstruct hc

theorem plus_isLegal : IsLegal (plus (Γ := Γ)) := by
  constructor
  · simp [plus]
  · intro ι _ _ e
    rw [localDensity_plus]
    exact posSemidef_reconstruct_plusCoeff

omit [DecidableEq Γ] in
theorem plus_embDomain {Δ : Type} [DecidableEq Δ] (e : Γ ↪ Δ) (p : Word Γ) :
    plus (Finsupp.embDomain e p) = plus p := by
  simp only [plus, Finsupp.support_embDomain, Finset.forall_mem_map,
    Finsupp.embDomain_apply_self]

theorem plus_isTranslationInvariant [AddGroup Γ] :
    IsTranslationInvariant (plus (Γ := Γ)) := by
  intro a p
  exact plus_embDomain (Equiv.addLeft a).toEmbedding p

theorem plus_isTranslationInvariantState [AddGroup Γ] :
    IsTranslationInvariantState (plus (Γ := Γ)) :=
  ⟨plus_isLegal, plus_isTranslationInvariant⟩

end QuantumManyBodyVariational.InfiniteState
