module
public import QuantumManyBodyVariational.TrustedSpecExamples
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec
def f : Correlator (Lattice 2) := allOne
def bound : ℚ := -100
-- A compiling proof of True must not count as a proof of CandidateAccepted.
theorem accepted : True := True.intro
end Submission
