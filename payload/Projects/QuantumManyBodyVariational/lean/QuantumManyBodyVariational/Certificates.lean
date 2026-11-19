module

public import Mathlib.Analysis.Complex.Basic
public import Mathlib.Algebra.BigOperators.Field
public import Mathlib.LinearAlgebra.Matrix.PosDef
public import Mathlib.LinearAlgebra.Matrix.Notation
public import Mathlib.Tactic.NormNum
public import Mathlib.Tactic.Ring

/-!
Kernel-checkable finite certificates used by the TFIM benchmark.

The main result turns an exact rational LDL identity into positivity for all
complex vectors, and hence a lower bound for every normalized finite-patch
wavefunction. Generated instances define the TFIM patch by Pauli bit-flip and
ZZ matrices, rather than trusting a matrix provided by a model.

These theorems do not formalize the infinite-lattice translation-invariance
bridge, uniqueness of a ground state, or convergence of a variational search.
-/

@[expose] public section

namespace QuantumManyBodyVariational

open Matrix
open scoped BigOperators ComplexOrder

def complexify {n : ℕ} (A : Matrix (Fin n) (Fin n) ℚ) : Matrix (Fin n) (Fin n) ℂ :=
  fun i j => (A i j : ℂ)

theorem complexify_mul {n : ℕ} (A B : Matrix (Fin n) (Fin n) ℚ) :
    complexify (A * B) = complexify A * complexify B := by
  ext i j
  simp [complexify, Matrix.mul_apply]

theorem complexify_diagonal {n : ℕ} (d : Fin n → ℚ) :
    complexify (Matrix.diagonal d) = Matrix.diagonal (fun i => (d i : ℂ)) := by
  ext i j
  by_cases h : i = j <;> simp [complexify, Matrix.diagonal, h]

theorem complexify_transpose {n : ℕ} (A : Matrix (Fin n) (Fin n) ℚ) :
    complexify A.transpose = (complexify A).conjTranspose := by
  ext i j
  simp [complexify, Matrix.conjTranspose_apply]

theorem complexify_shift {n : ℕ} (K : Matrix (Fin n) (Fin n) ℚ) (l : ℚ) :
    complexify (K - l • (1 : Matrix (Fin n) (Fin n) ℚ)) =
      complexify K - (l : ℂ) • (1 : Matrix (Fin n) (Fin n) ℂ) := by
  ext i j
  by_cases h : i = j <;> simp [complexify, h]

/-- A rational LDL witness certifies positivity on complex Hilbert space. -/
theorem psd_of_rational_ldl {n : ℕ} (A L : Matrix (Fin n) (Fin n) ℚ)
    (d : Fin n → ℚ) (hd : ∀ i, 0 ≤ d i)
    (hA : A = L * Matrix.diagonal d * L.transpose) :
    (complexify A).PosSemidef := by
  have hdc : ∀ i, (0 : ℂ) ≤ (d i : ℂ) := by
    intro i
    apply Complex.le_def.mpr
    constructor
    · simpa using (show (0 : ℝ) ≤ (d i : ℝ) from by exact_mod_cast hd i)
    · simp
  rw [hA, complexify_mul, complexify_mul, complexify_diagonal, complexify_transpose]
  exact (Matrix.PosSemidef.diagonal hdc).mul_mul_conjTranspose_same (complexify L)

def quadratic {n : ℕ} (A : Matrix (Fin n) (Fin n) ℂ) (x : Fin n → ℂ) : ℂ :=
  star x ⬝ᵥ (A *ᵥ x)

/-- A positive shifted Hamiltonian bounds every normalized complex wavefunction. -/
theorem normalized_energy_lower {n : ℕ} (K : Matrix (Fin n) (Fin n) ℂ) (l : ℂ)
    (hK : (K - l • (1 : Matrix (Fin n) (Fin n) ℂ)).PosSemidef)
    (x : Fin n → ℂ) (hx : star x ⬝ᵥ x = 1) : l ≤ quadratic K x := by
  have h := hK.dotProduct_mulVec_nonneg x
  have heq : star x ⬝ᵥ ((K - l • (1 : Matrix (Fin n) (Fin n) ℂ)) *ᵥ x) =
      quadratic K x - l := by
    simp [quadratic, Matrix.sub_mulVec, Matrix.smul_mulVec, dotProduct_sub,
      dotProduct_smul, hx]
  rw [heq] at h
  exact sub_nonneg.mp h

/-- Pauli X at the bit indicated by `mask`, in the computational basis. -/
def spinFlip (size mask : ℕ) : Matrix (Fin size) (Fin size) ℚ :=
  fun i j => if Nat.xor i.val mask = j.val then 1 else 0

/-- A diagonal Pauli-Z product, in the computational basis. -/
def spinZZ (size mask₁ mask₂ : ℕ) : Matrix (Fin size) (Fin size) ℚ :=
  fun i j => if i = j then
    (if (Nat.land i.val mask₁ = 0) = (Nat.land i.val mask₂ = 0) then 1 else -1)
    else 0

def normSquared {n : ℕ} (a : Fin n → ℚ) : ℚ := ∑ i, a i * a i

def amplitudeDensity {n : ℕ} (a : Fin n → ℚ) : Matrix (Fin n) (Fin n) ℚ :=
  fun i j => a i * a j / normSquared a

/-- Exact finite-amplitude normalization; no irrational square root is needed. -/
theorem amplitudeDensity_trace {n : ℕ} (a : Fin n → ℚ) (ha : normSquared a ≠ 0) :
    Matrix.trace (amplitudeDensity a) = 1 := by
  simp only [Matrix.trace, Matrix.diag, amplitudeDensity]
  rw [← Finset.sum_div]
  exact div_self ha

def expectation {n : ℕ} (rho A : Matrix (Fin n) (Fin n) ℚ) : ℚ :=
  Matrix.trace (rho * A)

end QuantumManyBodyVariational
