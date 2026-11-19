module

public import QuantumManyBodyVariational.TrustedTasks
public import QuantumManyBodyVariational.StateExamples
public import QuantumManyBodyVariational.StateCounterexample

/-!
Regression examples for the fixed state specification and fixed TFIM tasks.
This module is a proof library client, not part of the minimal semantic base.
All energies are expectations of the same global correlator accepted by the
fixed predicate. No finite-volume sequence is supplied or required.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState.TrustedSpecExamples

open LocalPauli TrustedStateSpec TrustedTasks
open scoped BigOperators

theorem plus_place {Γ : Type} [DecidableEq Γ] {ι : Type} [Fintype ι]
    (e : ι ↪ Γ) (p : ι → Pauli) :
    plus (place e p) = if ∀ i, p i = 0 ∨ p i = 1 then 1 else 0 := by
  simp only [plus_eq_ite_all, only_X_place_iff]

theorem tracial_place {Γ : Type} [DecidableEq Γ] {ι : Type} [Fintype ι]
    (e : ι ↪ Γ) (p : ι → Pauli) :
    tracial (place e p) = if p = (fun _ => 0) then 1 else 0 := by
  simp only [tracial, place_eq_zero_iff]

theorem tfim2D_plus_value (J g : ℚ) :
    (tfim2D J g).value (plus (Γ := Lattice 2)) = -(g : ℝ) := by
  norm_num [LocalEnergy.value, tfim2D, Fin.sum_univ_succ, plus_place,
    Fin.forall_fin_succ]

theorem tfim3D_plus_value (J g : ℚ) :
    (tfim3D J g).value (plus (Γ := Lattice 3)) = -(g : ℝ) := by
  norm_num [LocalEnergy.value, tfim3D, Fin.sum_univ_succ, plus_place,
    Fin.forall_fin_succ]

theorem tfim2D_tracial_value (J g : ℚ) :
    (tfim2D J g).value (tracial (Γ := Lattice 2)) = 0 := by
  norm_num [LocalEnergy.value, tfim2D, Fin.sum_univ_succ, tracial_place,
    funext_iff, Fin.forall_fin_succ]

theorem tfim3D_tracial_value (J g : ℚ) :
    (tfim3D J g).value (tracial (Γ := Lattice 3)) = 0 := by
  norm_num [LocalEnergy.value, tfim3D, Fin.sum_univ_succ, tracial_place,
    funext_iff, Fin.forall_fin_succ]

theorem tfim2D_plus_accepted :
    CandidateAccepted (tfim2D 1 1) (-1) (plus (Γ := Lattice 2)) := by
  constructor
  · exact plus_isTranslationInvariantState
  · rw [tfim2D_plus_value]
    norm_num

theorem tfim3D_plus_accepted :
    CandidateAccepted (tfim3D 1 1) (-1) (plus (Γ := Lattice 3)) := by
  constructor
  · exact plus_isTranslationInvariantState
  · rw [tfim3D_plus_value]
    norm_num

theorem tfim2D_tracial_accepted :
    CandidateAccepted (tfim2D 1 1) 0 (tracial (Γ := Lattice 2)) := by
  constructor
  · exact tracial_isTranslationInvariantState
  · rw [tfim2D_tracial_value]
    norm_num

theorem tfim3D_tracial_accepted :
    CandidateAccepted (tfim3D 1 1) 0 (tracial (Γ := Lattice 3)) := by
  constructor
  · exact tracial_isTranslationInvariantState
  · rw [tfim3D_tracial_value]
    norm_num

/-- Even normalized, TI, individually bounded values fail the fixed interface
when joint quantum positivity fails. -/
theorem allOne_not_stateCondition (d : ℕ) :
    ¬ StateCondition d (allOne (Γ := Lattice d)) := by
  intro h
  exact allOne_not_isLegal h.1

theorem allOne_not_accepted {d n m : ℕ} (h : LocalEnergy d n m) (upper : ℚ) :
    ¬ CandidateAccepted h upper (allOne (Γ := Lattice d)) := by
  intro hf
  exact allOne_not_stateCondition d hf.1

end QuantumManyBodyVariational.InfiniteState.TrustedSpecExamples
