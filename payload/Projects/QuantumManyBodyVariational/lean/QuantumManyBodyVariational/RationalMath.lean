module

public import Mathlib.Data.Rat.Floor
public import Mathlib.Data.Nat.Sqrt
public import Mathlib.Data.Nat.Factorial.Basic
public import Mathlib.Algebra.BigOperators.Group.Finset.Basic

/-!
# Deterministic rational numerical primitives

These are finite rational algorithms, not aliases for ideal real functions.
The Python evaluator uses the same integer/rational formulas. No assertion of
a global approximation-error bound is made by this module. `sqrtWith` and
`logWith` are totalized at invalid arguments; the generated correlator's
domain obligation requires nonnegative and positive arguments respectively.
-/

@[expose] public section
namespace QuantumManyBodyVariational.RationalMath
open scoped BigOperators

def precisionBits : ℕ := 40
def seriesTerms : ℕ := 32
def expReductionLevels : ℕ := 8
def logReductionLevels : ℕ := 16

/-- Floor of the positive square root on a dyadic grid. -/
def sqrtWith (bits : ℕ) (x : ℚ) : ℚ :=
  if x ≤ 0 then 0 else
    let scaled := (x.num.toNat * 2 ^ (2 * bits)) / x.den
    (Nat.sqrt scaled : ℚ) / (2 : ℚ) ^ bits

def expScale (x : ℚ) : ℕ :=
  ((List.range expReductionLevels).filter
    (fun i => decide ((2 : ℚ) ^ i / 2 < |x|))).length

def expWith (terms : ℕ) (x : ℚ) : ℚ :=
  let scale := expScale x
  let y := |x| / (2 : ℚ) ^ scale
  let base := ∑ k ∈ Finset.range terms, y ^ k / (Nat.factorial k : ℚ)
  let result := base ^ (2 ^ scale)
  if x < 0 then 1 / result else result

def arctanInvWith (terms base : ℕ) : ℚ :=
  ∑ k ∈ Finset.range terms,
    (-1 : ℚ) ^ k / (((2 * k + 1 : ℕ) : ℚ) * (base : ℚ) ^ (2 * k + 1))

/-- A finite Machin sum. This constant is rational, not `Real.pi`. -/
def piWith (terms : ℕ) : ℚ :=
  16 * arctanInvWith terms 5 - 4 * arctanInvWith terms 239

def angleReduceWith (terms : ℕ) (x : ℚ) : ℚ :=
  let p := piWith terms
  x - 2 * p * (⌊(x + p) / (2 * p)⌋ : ℤ)

def sinWith (terms : ℕ) (x : ℚ) : ℚ :=
  let y := angleReduceWith terms x
  ∑ k ∈ Finset.range terms,
    (-1 : ℚ) ^ k * y ^ (2 * k + 1) / (Nat.factorial (2 * k + 1) : ℚ)

def cosWith (terms : ℕ) (x : ℚ) : ℚ :=
  let y := angleReduceWith terms x
  ∑ k ∈ Finset.range terms,
    (-1 : ℚ) ^ k * y ^ (2 * k) / (Nat.factorial (2 * k) : ℚ)

def logScale (x : ℚ) : ℤ :=
  let up := ((List.range logReductionLevels).filter
    (fun i => decide ((2 : ℚ) ^ (i + 1) ≤ x))).length
  let down := ((List.range logReductionLevels).filter
    (fun i => decide (x < 1 / (2 : ℚ) ^ i))).length
  (up : ℤ) - (down : ℤ)

def atanhSeriesWith (terms : ℕ) (z : ℚ) : ℚ :=
  2 * ∑ k ∈ Finset.range terms, z ^ (2 * k + 1) / ((2 * k + 1 : ℕ) : ℚ)

def logWith (terms : ℕ) (x : ℚ) : ℚ :=
  if x ≤ 0 then 0 else
    let scale := logScale x
    let m := if 0 ≤ scale then x / (2 : ℚ) ^ scale.toNat
      else x * (2 : ℚ) ^ (-scale).toNat
    atanhSeriesWith terms ((m - 1) / (m + 1)) +
      (scale : ℚ) * atanhSeriesWith terms (1 / 3)

def tanhWith (terms : ℕ) (x : ℚ) : ℚ :=
  let y := expWith terms (2 * x)
  (y - 1) / (y + 1)

def eWith (terms : ℕ) : ℚ := expWith terms 1

def sqrt (x : ℚ) : ℚ := sqrtWith precisionBits x
def exp (x : ℚ) : ℚ := expWith seriesTerms x
def sin (x : ℚ) : ℚ := sinWith seriesTerms x
def cos (x : ℚ) : ℚ := cosWith seriesTerms x
def log (x : ℚ) : ℚ := logWith seriesTerms x
def tanh (x : ℚ) : ℚ := tanhWith seriesTerms x
def pi : ℚ := piWith seriesTerms
def e : ℚ := eWith seriesTerms

end QuantumManyBodyVariational.RationalMath
