module
public import QuantumManyBodyVariational.TrustedSpecExamplesV2
@[expose] public section
noncomputable section
namespace QuantumManyBodyVariational.InfiniteState.TrustedSpecRegressionV2
open LocalPauli TrustedStateSpecV2 TrustedSpecExamplesV2
/-- A two-qubit cell contains an intra-cell ZZ bond and one X field per qubit.
The field sum gives cell energy -2 in the plus state and per-qubit energy -1. -/
def twoQubitPlacement : Fin 2 ↪ Site 1 2 where
  toFun i := (![0], i)
  inj' := by intro i j h; exact congrArg Prod.snd h
def twoQubitTask : LocalEnergy 1 2 2 3 where
  q_pos := by norm_num
  placement := twoQubitPlacement
  coeff := ![-1,-1,-1]
  word := ![![1,0],![0,1],![3,3]]
theorem twoQubit_plus_value : twoQubitTask.value (plus (Γ := Site 1 2)) = -1 := by
  norm_num [LocalEnergy.value, LocalEnergy.cellValue, twoQubitTask,
    Fin.sum_univ_succ, plus_place, Fin.forall_fin_succ]
theorem twoQubit_plus_accepted : CandidateAccepted twoQubitTask (-1) plus := by
  exact ⟨plus_stateCondition 1 2, by rw [twoQubit_plus_value]; norm_num⟩
theorem twoQubit_tracial_accepted : CandidateAccepted twoQubitTask 0 tracial := by
  constructor
  · exact tracial_stateCondition 1 2
  · norm_num [LocalEnergy.value, LocalEnergy.cellValue, twoQubitTask,
      Fin.sum_univ_succ, tracial_place, funext_iff, Fin.forall_fin_succ]
end QuantumManyBodyVariational.InfiniteState.TrustedSpecRegressionV2
