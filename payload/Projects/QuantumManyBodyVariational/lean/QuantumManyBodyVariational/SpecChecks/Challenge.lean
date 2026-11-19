module
public import QuantumManyBodyVariational.TrustedSpecExamples
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpec TrustedTasks TrustedSpecExamples
-- These two bodies are the only holes a solution may replace.
def f : Correlator (Lattice 2) := tracial
def bound : ℚ := 0
-- The type and every non-hole definition it references are fixed.
theorem accepted : CandidateAccepted (tfim2D 1 1) bound f :=
  tfim2D_tracial_accepted
end Submission
