module

public import QuantumManyBodyVariational.TrustedStateSpec

/-!
Evaluator-owned physical tasks. Each positive-coordinate nearest-neighbour
bond is counted once; the field occurs once per qubit. These choices belong
to the task, not to a submitted correlator or its claimed energy bound.
-/

@[expose] public section

noncomputable section

namespace QuantumManyBodyVariational.InfiniteState.TrustedTasks

open TrustedStateSpec

/-- Origin, positive x neighbour, positive y neighbour. -/
def squarePlacement : Fin 3 ↪ Lattice 2 where
  toFun := ![![0, 0], ![1, 0], ![0, 1]]
  inj' := by
    intro i j h
    fin_cases i <;> fin_cases j <;>
      simp_all [funext_iff, Fin.forall_fin_succ]

/-- Origin and the three positive-coordinate nearest neighbours. -/
def cubicPlacement : Fin 4 ↪ Lattice 3 where
  toFun := ![![0, 0, 0], ![1, 0, 0], ![0, 1, 0], ![0, 0, 1]]
  inj' := by
    intro i j h
    fin_cases i <;> fin_cases j <;>
      simp_all [funext_iff, Fin.forall_fin_succ]

/-- Square-lattice TFIM h₀ = -g X₀ - J Z₀Zₓ - J Z₀Zᵧ, per qubit. -/
def tfim2D (J g : ℚ) : LocalEnergy 2 3 3 where
  placement := squarePlacement
  coeff := ![-g, -J, -J]
  word := ![![1, 0, 0], ![3, 3, 0], ![3, 0, 3]]

/-- Cubic-lattice TFIM h₀ = -g X₀ - J (Z₀Zₓ+Z₀Zᵧ+Z₀Z𝓏), per qubit. -/
def tfim3D (J g : ℚ) : LocalEnergy 3 4 4 where
  placement := cubicPlacement
  coeff := ![-g, -J, -J, -J]
  word := ![![1, 0, 0, 0], ![3, 3, 0, 0], ![3, 0, 3, 0], ![3, 0, 0, 3]]

end QuantumManyBodyVariational.InfiniteState.TrustedTasks
