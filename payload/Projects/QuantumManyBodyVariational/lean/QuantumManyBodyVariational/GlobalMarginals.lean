module

public import QuantumManyBodyVariational.CorrelatorStates
public import QuantumManyBodyVariational.LocalMarginals

/-!
Marginal compatibility for a single correlator on arbitrary finite Pauli words.
`place_left_zero` identifies the two genuinely equal global words, and
`localDensity_partialTrace` derives the actual matrix partial-trace identity.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open LocalPauli

variable {Γ α β : Type} [DecidableEq Γ]
  [Fintype α] [Fintype β] [DecidableEq α] [DecidableEq β]

/-- The retained part of an injective placement of a disjoint union. -/
def leftPlacement (e : α ⊕ β ↪ Γ) : α ↪ Γ :=
  ⟨fun a => e (Sum.inl a), fun _ _ h => Sum.inl.inj (e.injective h)⟩

omit [DecidableEq Γ] in
theorem place_of_notMem_range {ι : Type} [Fintype ι]
    (e : ι ↪ Γ) (p : ι → Pauli) (x : Γ) (hx : x ∉ Set.range e) :
    place e p x = 0 := by
  exact Finsupp.embDomain_of_notMem_range e _ x hx

omit [DecidableEq α] [DecidableEq β] in
theorem place_left_zero (e : α ⊕ β ↪ Γ) (p : α → Pauli) :
    place e (Sum.elim p (fun _ => 0)) = place (leftPlacement e) p := by
  classical
  apply Finsupp.ext
  intro x
  by_cases hx : x ∈ Set.range e
  · obtain ⟨a | b, rfl⟩ := hx
    · rw [place_apply]
      change p a = place (leftPlacement e) p (leftPlacement e a)
      rw [place_apply]
    · rw [place_apply]
      have hn : e (Sum.inr b) ∉ Set.range (leftPlacement e) := by
        rintro ⟨a, ha⟩
        have h := e.injective ha
        cases h
      rw [place_of_notMem_range _ _ _ hn]
      rfl
  · rw [place_of_notMem_range _ _ _ hx]
    have hn : x ∉ Set.range (leftPlacement e) := by
      rintro ⟨a, rfl⟩
      exact hx ⟨Sum.inl a, rfl⟩
    rw [place_of_notMem_range _ _ _ hn]

theorem localDensity_partialTrace (f : Correlator Γ) (e : α ⊕ β ↪ Γ) :
    partialTrace (localDensity f e) = localDensity f (leftPlacement e) := by
  unfold localDensity
  rw [partialTrace_reconstruct]
  congr 1
  funext p
  rw [place_left_zero]

end QuantumManyBodyVariational.InfiniteState
