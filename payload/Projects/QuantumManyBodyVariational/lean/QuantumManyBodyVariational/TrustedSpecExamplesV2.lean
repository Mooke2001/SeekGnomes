module
public import QuantumManyBodyVariational.TrustedStateSpecV2
public import QuantumManyBodyVariational.StateExamples
public import QuantumManyBodyVariational.StateClosure
@[expose] public section
noncomputable section
namespace QuantumManyBodyVariational.InfiniteState.TrustedSpecExamplesV2
open LocalPauli TrustedStateSpecV2

theorem tracial_stateCondition (d q : ℕ) :
    StateCondition d q (tracial (Γ := Site d q)) := by
  refine ⟨tracial_isLegal, ?_⟩
  intro a p
  simp [tracial, cellTranslate, Finsupp.embDomain_eq_zero]
theorem plus_stateCondition (d q : ℕ) :
    StateCondition d q (plus (Γ := Site d q)) := by
  refine ⟨plus_isLegal, ?_⟩
  intro a p
  exact plus_embDomain (cellShift a) p

theorem tracial_place {Γ : Type} [DecidableEq Γ] {ι : Type} [Fintype ι]
    (e : ι ↪ Γ) (p : ι → Pauli) :
    tracial (place e p) = if p = (fun _ => 0) then 1 else 0 :=
  by simp only [tracial, place_eq_zero_iff]
theorem plus_place {Γ : Type} [DecidableEq Γ] {ι : Type} [Fintype ι]
    (e : ι ↪ Γ) (p : ι → Pauli) :
    plus (place e p) = if ∀ i, p i = 0 ∨ p i = 1 then 1 else 0 :=
  by simp only [plus_eq_ite_all, only_X_place_iff]

theorem mix_stateCondition {d q : ℕ} {f g : Correlator (Site d q)}
    (hf : StateCondition d q f) (hg : StateCondition d q g)
    (t : ℝ) (ht : 0 ≤ t) (ht1 : t ≤ 1) : StateCondition d q (mix t f g) := by
  refine ⟨hf.1.mix hg.1 t ht ht1, ?_⟩
  intro a p
  simp only [mix, hf.2 a p, hg.2 a p]

end QuantumManyBodyVariational.InfiniteState.TrustedSpecExamplesV2
