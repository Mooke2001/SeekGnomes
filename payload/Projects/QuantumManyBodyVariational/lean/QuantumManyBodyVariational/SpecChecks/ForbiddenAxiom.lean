module
public import QuantumManyBodyVariational.TrustedSpecExamples
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec TrustedTasks
def f : Correlator (Lattice 2) := allOne
def bound : ℚ := -100
-- Intentional rejection fixture. This axiom is never admitted by the judge.
axiom fake : False
theorem accepted : CandidateAccepted (tfim2D 1 1) bound f := False.elim fake
end Submission
