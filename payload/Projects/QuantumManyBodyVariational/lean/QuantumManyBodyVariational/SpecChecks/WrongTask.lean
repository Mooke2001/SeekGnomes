module
public import QuantumManyBodyVariational.TrustedSpecExamples
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec TrustedTasks TrustedSpecExamples
def f : Correlator (Lattice 2) := plus
def bound : ℚ := -2
-- A true statement about g=2 must not be accepted for the fixed g=1 task.
theorem accepted : CandidateAccepted (tfim2D 1 2) bound f := by
  refine ⟨plus_isTranslationInvariantState, ?_⟩
  unfold f
  rw [tfim2D_plus_value]
  norm_num [bound]
end Submission
