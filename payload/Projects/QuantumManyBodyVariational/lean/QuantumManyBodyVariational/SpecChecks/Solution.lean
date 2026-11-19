module
public import QuantumManyBodyVariational.TrustedSpecExamples
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec TrustedTasks TrustedSpecExamples
def f : Correlator (Lattice 2) := plus
def bound : ℚ := -1
theorem accepted : CandidateAccepted (tfim2D 1 1) bound f :=
  tfim2D_plus_accepted
end Submission
