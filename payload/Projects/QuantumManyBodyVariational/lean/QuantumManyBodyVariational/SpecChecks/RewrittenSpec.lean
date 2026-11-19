module
public import QuantumManyBodyVariational.TrustedStateSpec
@[expose] public section
noncomputable section
namespace QuantumManyBodyVariational.InfiniteState.TrustedTasks
open TrustedStateSpec
def squarePlacement : Fin 3 ↪ Lattice 2 where
  toFun := ![![0, 0], ![1, 0], ![0, 1]]
  inj' := by
    intro i j h
    fin_cases i <;> fin_cases j <;>
      simp_all [funext_iff, Fin.forall_fin_succ]
-- Intentional attack: same fully qualified task name and type, wrong body.
def tfim2D (_J _g : ℚ) : LocalEnergy 2 3 3 where
  placement := squarePlacement
  coeff := ![0, 0, 0]
  word := ![![1, 0, 0], ![3, 3, 0], ![3, 0, 3]]
end QuantumManyBodyVariational.InfiniteState.TrustedTasks
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec TrustedTasks
def f : Correlator (Lattice 2) := tracial
def bound : ℚ := 0
theorem accepted : CandidateAccepted (tfim2D 1 1) bound f := by
  refine ⟨tracial_isTranslationInvariantState, ?_⟩
  norm_num [LocalEnergy.value, tfim2D, Fin.sum_univ_succ, bound]
end Submission
