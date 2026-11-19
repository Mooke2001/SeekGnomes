module

public import QuantumManyBodyVariational.LocalPauli
public import QuantumManyBodyVariational.StateLimits
public import QuantumManyBodyVariational.PauliRecovery
public import Mathlib.Data.Finsupp.Basic

/-!
An infinite correlator is a single function on every finitely supported Pauli
word. Finite regions are represented by embeddings of arbitrary finite site
types into the lattice. There is no bound on region size, geometry or weight.

This file formalizes the compatible-matrix notion of legality. It does not
construct the norm completion of the quasi-local C*-algebra or invoke GNS.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState

open scoped BigOperators ComplexOrder
open Filter Topology
open LocalPauli

abbrev Word (Γ : Type) := Γ →₀ Pauli
abbrev Correlator (Γ : Type) := Word Γ → ℝ

variable {Γ : Type} [DecidableEq Γ]

/-- Put a local Pauli word at the sites selected by an injective placement. -/
def place {ι : Type} [Fintype ι] (e : ι ↪ Γ) (p : ι → Pauli) : Word Γ :=
  Finsupp.embDomain e (Finsupp.equivFunOnFinite.symm p)

omit [DecidableEq Γ] in
@[simp] theorem place_zero {ι : Type} [Fintype ι] (e : ι ↪ Γ) :
    place e (fun _ => 0) = 0 := by
  apply (Finsupp.embDomain_eq_zero).mpr
  ext i
  rfl

omit [DecidableEq Γ] in
@[simp] theorem place_apply {ι : Type} [Fintype ι] (e : ι ↪ Γ)
    (p : ι → Pauli) (i : ι) : place e p (e i) = p i := by
  exact Finsupp.embDomain_apply_self e _ i

omit [DecidableEq Γ] in
theorem place_eq_zero_iff {ι : Type} [Fintype ι] (e : ι ↪ Γ)
    (p : ι → Pauli) : place e p = 0 ↔ p = (fun _ => 0) := by
  constructor
  · intro h
    funext i
    have hi := congrArg (fun w : Word Γ => w (e i)) h
    simpa using hi
  · intro h
    subst p
    exact place_zero e

def localDensity {ι : Type} [Fintype ι] [DecidableEq ι]
    (f : Correlator Γ) (e : ι ↪ Γ) : Matrix (ι → Fin 2) (ι → Fin 2) ℂ :=
  reconstruct (fun p => (f (place e p) : ℂ))

/-- All finite regions, not a finite collection of numerical tests. -/
def IsLegal (f : Correlator Γ) : Prop :=
  f 0 = 1 ∧ ∀ (ι : Type) [Fintype ι] [DecidableEq ι] (e : ι ↪ Γ),
    (localDensity f e).PosSemidef

omit [DecidableEq Γ] in
theorem localDensity_trace {ι : Type} [Fintype ι] [DecidableEq ι]
    (f : Correlator Γ) (e : ι ↪ Γ) :
    Matrix.trace (localDensity f e) = (f 0 : ℂ) := by
  simp [localDensity, trace_reconstruct]

omit [DecidableEq Γ] in
theorem IsLegal.trace_one {f : Correlator Γ} (hf : IsLegal f)
    {ι : Type} [Fintype ι] [DecidableEq ι] (e : ι ↪ Γ) :
    Matrix.trace (localDensity f e) = 1 := by
  simp [localDensity_trace, hf.1]

section Translations

variable [AddGroup Γ]

def translate (a : Γ) (p : Word Γ) : Word Γ :=
  Finsupp.embDomain (Equiv.addLeft a).toEmbedding p

def IsTranslationInvariant (f : Correlator Γ) : Prop :=
  ∀ (a : Γ) (p : Word Γ), f (translate a p) = f p

def IsTranslationInvariantState (f : Correlator Γ) : Prop :=
  IsLegal f ∧ IsTranslationInvariant f

end Translations

/-- The infinite product of maximally mixed single-qubit states. -/
def tracial (p : Word Γ) : ℝ := if p = 0 then 1 else 0

theorem localDensity_tracial {ι : Type} [Fintype ι] [DecidableEq ι]
    (e : ι ↪ Γ) :
    localDensity (tracial (Γ := Γ)) e = reconstruct (tracialCoeff (ι := ι)) := by
  have hc : (fun p : ι → Pauli => (tracial (place e p) : ℂ)) = tracialCoeff := by
    funext p
    by_cases hp : p = (fun _ => 0)
    · subst p
      simp [tracial, tracialCoeff]
    · have hplace : place e p ≠ 0 := fun h => hp ((place_eq_zero_iff e p).mp h)
      simp [tracial, tracialCoeff, hplace, hp]
  exact congrArg reconstruct hc

theorem tracial_isLegal : IsLegal (tracial (Γ := Γ)) := by
  constructor
  · simp [tracial]
  · intro ι _ _ e
    rw [localDensity_tracial]
    exact posSemidef_reconstruct_tracialCoeff

theorem tracial_isTranslationInvariant [AddGroup Γ] :
    IsTranslationInvariant (tracial (Γ := Γ)) := by
  intro a p
  simp [tracial, translate, Finsupp.embDomain_eq_zero]

theorem tracial_isTranslationInvariantState [AddGroup Γ] :
    IsTranslationInvariantState (tracial (Γ := Γ)) :=
  ⟨tracial_isLegal, tracial_isTranslationInvariant⟩

omit [DecidableEq Γ] in
/-- Pointwise limits of globally normalized correlators retain normalization. -/
theorem normalized_of_tendsto {fn : ℕ → Correlator Γ} {f : Correlator Γ}
    (hlim : ∀ p, Filter.Tendsto (fun n => fn n p) Filter.atTop (nhds (f p)))
    (hnorm : ∀ᶠ n in Filter.atTop, fn n 0 = 1) : f 0 = 1 := by
  exact tendsto_nhds_unique_of_eventuallyEq (hlim 0)
    tendsto_const_nhds hnorm

omit [DecidableEq Γ] in
/-- All-size legality from local pointwise convergence and eventual local PSD.
No bounded-size numerical check is substituted for the universally quantified
premise, and the finite approximants need not themselves be infinite states. -/
theorem isLegal_of_tendsto {fn : ℕ → Correlator Γ} {f : Correlator Γ}
    (hlim : ∀ p, Filter.Tendsto (fun n => fn n p) Filter.atTop (nhds (f p)))
    (hnorm : ∀ᶠ n in Filter.atTop, fn n 0 = 1)
    (hpsd : ∀ (ι : Type) [Fintype ι] [DecidableEq ι] (e : ι ↪ Γ),
      ∀ᶠ n in Filter.atTop, (localDensity (fn n) e).PosSemidef) : IsLegal f := by
  refine ⟨normalized_of_tendsto hlim hnorm, ?_⟩
  intro ι _ _ e
  apply LocalPauli.posSemidef_reconstruct_of_tendsto ?_ (hpsd ι e)
  intro p
  exact (Complex.continuous_ofReal.tendsto (f (place e p))).comp (hlim (place e p))

omit [DecidableEq Γ] in
theorem isTranslationInvariant_of_tendsto [AddGroup Γ]
    {fn : ℕ → Correlator Γ} {f : Correlator Γ}
    (hlim : ∀ p, Filter.Tendsto (fun n => fn n p) Filter.atTop (nhds (f p)))
    (hti : ∀ (a : Γ) (p : Word Γ), ∀ᶠ n in Filter.atTop,
      fn n (translate a p) = fn n p) : IsTranslationInvariant f := by
  intro a p
  exact tendsto_nhds_unique_of_eventuallyEq (hlim (translate a p)) (hlim p) (hti a p)

omit [DecidableEq Γ] in
theorem isTranslationInvariantState_of_tendsto [AddGroup Γ]
    {fn : ℕ → Correlator Γ} {f : Correlator Γ}
    (hlim : ∀ p, Filter.Tendsto (fun n => fn n p) Filter.atTop (nhds (f p)))
    (hnorm : ∀ᶠ n in Filter.atTop, fn n 0 = 1)
    (hpsd : ∀ (ι : Type) [Fintype ι] [DecidableEq ι] (e : ι ↪ Γ),
      ∀ᶠ n in Filter.atTop, (localDensity (fn n) e).PosSemidef)
    (hti : ∀ (a : Γ) (p : Word Γ), ∀ᶠ n in Filter.atTop,
      fn n (translate a p) = fn n p) : IsTranslationInvariantState f :=
  ⟨isLegal_of_tendsto hlim hnorm hpsd, isTranslationInvariant_of_tendsto hlim hti⟩

end QuantumManyBodyVariational.InfiniteState
