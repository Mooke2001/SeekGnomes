module

public import QuantumManyBodyVariational.GlobalMarginals
public import QuantumManyBodyVariational.StateClosure
public import QuantumManyBodyVariational.StateExamples
public import QuantumManyBodyVariational.StateCounterexample

/-!
Proof-carrying entry points for infinite qubit correlators. The factorization
certificate is quantified over all finite regions, not just tested sizes.
No finite-description or tensor-network restriction is imposed on Correlator.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open LocalPauli
open scoped ComplexOrder

variable {Γ : Type}

/-- The input correlator is exactly the expectation of every local Pauli word. -/
theorem localDensity_expectation {ι : Type} [Fintype ι] [DecidableEq ι]
    (f : Correlator Γ) (e : ι ↪ Γ) (p : ι → Pauli) :
    Matrix.trace (localDensity f e * pauliTensor p) = (f (place e p) : ℂ) := by
  exact trace_reconstruct_mul_pauliTensor _ p

/-- A uniform Gram-factorization proof gives legality at every finite size.
The factor may depend on the finite region and need not be computable. -/
theorem isLegal_of_factorization {f : Correlator Γ} (hnorm : f 0 = 1)
    (hfactor : ∀ (ι : Type) [Fintype ι] [DecidableEq ι] (e : ι ↪ Γ),
      ∃ V : Matrix (ι → Fin 2) (ι → Fin 2) ℂ,
        localDensity f e = V.conjTranspose * V) : IsLegal f := by
  refine ⟨hnorm, ?_⟩
  intro ι _ _ e
  obtain ⟨V, hV⟩ := hfactor ι e
  rw [hV]
  exact Matrix.posSemidef_conjTranspose_mul_self V

/-- The square lattice, with no finite-volume cutoff. -/
example : IsTranslationInvariantState (plus (Γ := Fin 2 → ℤ)) :=
  plus_isTranslationInvariantState

/-- The simple cubic lattice, with no finite-volume cutoff. -/
example : IsTranslationInvariantState (plus (Γ := Fin 3 → ℤ)) :=
  plus_isTranslationInvariantState

/-- An entire continuous family of legal TI states, on the cubic lattice. -/
example (t : ℝ) (ht : 0 ≤ t) (ht1 : t ≤ 1) :
    IsTranslationInvariantState
      (mix t (plus (Γ := Fin 3 → ℤ)) tracial) :=
  plus_isTranslationInvariantState.mix tracial_isTranslationInvariantState t ht ht1

end QuantumManyBodyVariational.InfiniteState
