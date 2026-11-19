module
public import GeneratedState

@[expose] public section
noncomputable section

open QuantumManyBodyVariational
open QuantumManyBodyVariational.InfiniteState
open QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2
open GeneratedState

namespace Submission

private theorem factors_eq_nil (p : WordType) : factors p = [] ↔ p = 0 := by
  constructor
  · intro h
    have hc : (factors p).length = 0 := by rw [h]; rfl
    simpa [factors] using hc
  · intro hp
    simp [hp, factors]

theorem state_eq_tracial : f = tracial := by
  funext p
  simp [f, dsl0_f, Checked.pure, Checked.bind, HybridMath.ofInt, tracial, factors_eq_nil]
  split <;> simp

theorem accepted : ProofObligation := by
  refine ⟨?_, ?_⟩
  · rw [state_eq_tracial]
    refine ⟨tracial_isLegal, ?_⟩
    intro a p
    simp [tracial, cellTranslate, Finsupp.embDomain_eq_zero]
  · intro p
    simp [dsl0_f, Checked.pure, Checked.bind, HybridMath.ofInt]
    split <;> trivial

end Submission
