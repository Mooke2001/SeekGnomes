module

public import QuantumManyBodyVariational.LocalPauli

/-!
# Closedness of positivity for actual finite Pauli reconstructions

The limit theorem uses the standard `Matrix.PosSemidef` predicate, including
Hermiticity and nonnegativity of every finitely supported complex quadratic
form. It does not assume an abstract positivity predicate for correlations.

For any finite site type, pointwise convergence of *all* Pauli coordinates
implies entrywise convergence of the corresponding density matrices. Eventual
positivity therefore passes to the reconstructed limit. Applying this theorem
to each finite region supplies the positivity part of the local-limit route
to an infinite state; normalization, compatibility and translations are
separate obligations.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open Filter Topology
open scoped BigOperators ComplexOrder

/-- Entrywise limits preserve standard complex matrix positivity. The index
type need not be finite because `Matrix.PosSemidef` tests finite-support vectors. -/
theorem posSemidef_of_entrywise_tendsto {κ : Type*}
    {M : ℕ → Matrix κ κ ℂ} {A : Matrix κ κ ℂ}
    (hlim : ∀ i j, Tendsto (fun n => M n i j) atTop (𝓝 (A i j)))
    (hpsd : ∀ᶠ n in atTop, (M n).PosSemidef) : A.PosSemidef := by
  constructor
  · ext i j
    have hc := (hlim j i).star
    exact tendsto_nhds_unique_of_eventuallyEq hc (hlim i j)
      (hpsd.mono fun n hn => congrFun (congrFun hn.1 i) j)
  · intro x
    apply ge_of_tendsto ?_ (hpsd.mono fun n hn => hn.2 x)
    simp only [Finsupp.sum]
    apply tendsto_finsetSum
    intro i hi
    apply tendsto_finsetSum
    intro j hj
    exact (tendsto_const_nhds.mul (hlim i j)).mul tendsto_const_nhds

namespace LocalPauli

variable {ι : Type*} [Fintype ι] [DecidableEq ι]

/-- Pointwise convergence of the full finite-region Pauli coordinates implies
entrywise convergence of the actual Pauli-expanded matrices. -/
theorem reconstruct_tendsto_entrywise
    {fn : ℕ → ((ι → Pauli) → ℂ)} {f : (ι → Pauli) → ℂ}
    (hlim : ∀ p, Tendsto (fun n => fn n p) atTop (𝓝 (f p)))
    (i j : ι → Fin 2) :
    Tendsto (fun n => reconstruct (fn n) i j) atTop (𝓝 (reconstruct f i j)) := by
  classical
  simp only [reconstruct, Matrix.smul_apply, Matrix.sum_apply, smul_eq_mul]
  apply tendsto_const_nhds.mul
  apply tendsto_finsetSum
  intro p hp
  exact (hlim p).mul tendsto_const_nhds

/-- The local-limit positivity theorem for every finite qubit region.
Eventual, rather than all-size, positivity is sufficient. -/
theorem posSemidef_reconstruct_of_tendsto
    {fn : ℕ → ((ι → Pauli) → ℂ)} {f : (ι → Pauli) → ℂ}
    (hlim : ∀ p, Tendsto (fun n => fn n p) atTop (𝓝 (f p)))
    (hpsd : ∀ᶠ n in atTop, (reconstruct (fn n)).PosSemidef) :
    (reconstruct f).PosSemidef := by
  apply posSemidef_of_entrywise_tendsto
    (fun i j => reconstruct_tendsto_entrywise hlim i j) hpsd

end LocalPauli

end QuantumManyBodyVariational.InfiniteState
