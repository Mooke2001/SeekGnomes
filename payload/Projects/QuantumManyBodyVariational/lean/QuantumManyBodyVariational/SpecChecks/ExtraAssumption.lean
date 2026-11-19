module
public import QuantumManyBodyVariational.TrustedSpecExamples
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec TrustedTasks TrustedSpecExamples
def f : Correlator (Lattice 2) := plus
def bound : ℚ := -1
-- Adding a premise changes the target, even if the extra premise is unused.
theorem accepted (_h : StateCondition 2 f) :
    CandidateAccepted (tfim2D 1 1) bound f := tfim2D_plus_accepted
end Submission
