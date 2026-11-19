module

public import QuantumManyBodyVariational.RationalMath
public import Mathlib.Analysis.SpecialFunctions.Log.Basic
public import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic

/-!
# Real values with a rational control-flow interpretation

The first component is the ideal real expression used to define the state.
The second component is a fixed finite rational program used only to decide
control flow. Comparisons in generated correlators inspect `Number.rat`.
This module does not assert a numerical approximation-error bound.
-/

@[expose] public section
noncomputable section
namespace QuantumManyBodyVariational.HybridMath

structure Number where
  real : ℝ
  rat : ℚ
  /-- Only demanded when the ideal real value is used. -/
  rOk : Prop
  /-- Only demanded when control flow actually inspects the rational shadow. -/
  qOk : Prop

def ofRat (x : ℚ) : Number := ⟨(x : ℝ), x, True, True⟩
def ofInt (x : ℤ) : Number := ⟨(x : ℝ), (x : ℚ), True, True⟩

def add (a b : Number) : Number := ⟨a.real + b.real, a.rat + b.rat, a.rOk ∧ b.rOk, a.qOk ∧ b.qOk⟩
def sub (a b : Number) : Number := ⟨a.real - b.real, a.rat - b.rat, a.rOk ∧ b.rOk, a.qOk ∧ b.qOk⟩
def mul (a b : Number) : Number := ⟨a.real * b.real, a.rat * b.rat, a.rOk ∧ b.rOk, a.qOk ∧ b.qOk⟩
def div (a b : Number) : Number :=
  ⟨a.real / b.real, a.rat / b.rat, a.rOk ∧ b.rOk ∧ b.real ≠ 0, a.qOk ∧ b.qOk ∧ b.rat ≠ 0⟩
def neg (a : Number) : Number := ⟨-a.real, -a.rat, a.rOk, a.qOk⟩
def abs (a : Number) : Number := ⟨|a.real|, |a.rat|, a.rOk, a.qOk⟩
def powNat (a : Number) (n : ℕ) : Number := ⟨a.real ^ n, a.rat ^ n, a.rOk, a.qOk⟩

/-- Attach a control-flow domain condition to the chosen numerical value. -/
def mark (ok : Prop) (a : Number) : Number :=
  ⟨a.real, a.rat, ok ∧ a.rOk, ok ∧ a.qOk⟩

instance : Zero Number := ⟨ofRat 0⟩
instance : One Number := ⟨ofRat 1⟩
instance (n : ℕ) : OfNat Number n := ⟨ofRat n⟩
instance : Add Number := ⟨add⟩
instance : Sub Number := ⟨sub⟩
instance : Mul Number := ⟨mul⟩
instance : Div Number := ⟨div⟩
instance : Neg Number := ⟨neg⟩
instance : Pow Number ℕ := ⟨powNat⟩

def minimum (a b : Number) : Number :=
  ⟨min a.real b.real, min a.rat b.rat, a.rOk ∧ b.rOk, a.qOk ∧ b.qOk⟩
def maximum (a b : Number) : Number :=
  ⟨max a.real b.real, max a.rat b.rat, a.rOk ∧ b.rOk, a.qOk ∧ b.qOk⟩

def sqrtWith (bits : ℕ) (a : Number) : Number :=
  ⟨Real.sqrt a.real, RationalMath.sqrtWith bits a.rat, a.rOk ∧ 0 ≤ a.real, a.qOk ∧ 0 ≤ a.rat⟩
def expWith (terms : ℕ) (a : Number) : Number :=
  ⟨Real.exp a.real, RationalMath.expWith terms a.rat, a.rOk, a.qOk⟩
def sinWith (terms : ℕ) (a : Number) : Number :=
  ⟨Real.sin a.real, RationalMath.sinWith terms a.rat, a.rOk, a.qOk⟩
def cosWith (terms : ℕ) (a : Number) : Number :=
  ⟨Real.cos a.real, RationalMath.cosWith terms a.rat, a.rOk, a.qOk⟩
def logWith (terms : ℕ) (a : Number) : Number :=
  ⟨Real.log a.real, RationalMath.logWith terms a.rat, a.rOk ∧ 0 < a.real, a.qOk ∧ 0 < a.rat⟩
def tanhWith (terms : ℕ) (a : Number) : Number :=
  ⟨Real.tanh a.real, RationalMath.tanhWith terms a.rat, a.rOk, a.qOk⟩
def piWith (terms : ℕ) : Number := ⟨Real.pi, RationalMath.piWith terms, True, True⟩
def eWith (terms : ℕ) : Number := ⟨Real.exp 1, RationalMath.eWith terms, True, True⟩

@[simp] theorem real_ofRat (x : ℚ) : (ofRat x).real = (x : ℝ) := rfl
@[simp] theorem rat_ofRat (x : ℚ) : (ofRat x).rat = x := rfl
@[simp] theorem real_ofInt (x : ℤ) : (ofInt x).real = (x : ℝ) := rfl
@[simp] theorem rat_ofInt (x : ℤ) : (ofInt x).rat = (x : ℚ) := rfl
@[simp] theorem qOk_ofRat (x : ℚ) : (ofRat x).qOk := trivial
@[simp] theorem qOk_ofInt (x : ℤ) : (ofInt x).qOk := trivial
@[simp] theorem rOk_ofRat (x : ℚ) : (ofRat x).rOk := trivial
@[simp] theorem rOk_ofInt (x : ℤ) : (ofInt x).rOk := trivial
@[simp] theorem real_zero : (0 : Number).real = 0 := by
  change ((0 : ℚ) : ℝ) = 0
  norm_num
@[simp] theorem real_one : (1 : Number).real = 1 := by
  change ((1 : ℚ) : ℝ) = 1
  norm_num
@[simp] theorem rat_zero : (0 : Number).rat = 0 := rfl
@[simp] theorem rat_one : (1 : Number).rat = 1 := rfl
@[simp] theorem rOk_zero : (0 : Number).rOk := trivial
@[simp] theorem rOk_one : (1 : Number).rOk := trivial
@[simp] theorem qOk_zero : (0 : Number).qOk := trivial
@[simp] theorem qOk_one : (1 : Number).qOk := trivial
@[simp] theorem real_add (a b : Number) : (a + b).real = a.real + b.real := rfl
@[simp] theorem real_sub (a b : Number) : (a - b).real = a.real - b.real := rfl
@[simp] theorem real_mul (a b : Number) : (a * b).real = a.real * b.real := rfl
@[simp] theorem real_div (a b : Number) : (a / b).real = a.real / b.real := rfl
@[simp] theorem real_neg (a : Number) : (-a).real = -a.real := rfl
@[simp] theorem real_pow (a : Number) (n : ℕ) : (a ^ n).real = a.real ^ n := rfl
@[simp] theorem rat_add (a b : Number) : (a + b).rat = a.rat + b.rat := rfl
@[simp] theorem rat_sub (a b : Number) : (a - b).rat = a.rat - b.rat := rfl
@[simp] theorem rat_mul (a b : Number) : (a * b).rat = a.rat * b.rat := rfl
@[simp] theorem rat_div (a b : Number) : (a / b).rat = a.rat / b.rat := rfl
@[simp] theorem rat_neg (a : Number) : (-a).rat = -a.rat := rfl
@[simp] theorem rat_pow (a : Number) (n : ℕ) : (a ^ n).rat = a.rat ^ n := rfl
@[simp] theorem rOk_add (a b : Number) : (a + b).rOk ↔ a.rOk ∧ b.rOk := Iff.rfl
@[simp] theorem rOk_sub (a b : Number) : (a - b).rOk ↔ a.rOk ∧ b.rOk := Iff.rfl
@[simp] theorem rOk_mul (a b : Number) : (a * b).rOk ↔ a.rOk ∧ b.rOk := Iff.rfl
@[simp] theorem rOk_div (a b : Number) : (a / b).rOk ↔ a.rOk ∧ b.rOk ∧ b.real ≠ 0 := Iff.rfl
@[simp] theorem qOk_add (a b : Number) : (a + b).qOk ↔ a.qOk ∧ b.qOk := Iff.rfl
@[simp] theorem qOk_sub (a b : Number) : (a - b).qOk ↔ a.qOk ∧ b.qOk := Iff.rfl
@[simp] theorem qOk_mul (a b : Number) : (a * b).qOk ↔ a.qOk ∧ b.qOk := Iff.rfl
@[simp] theorem qOk_div (a b : Number) : (a / b).qOk ↔ a.qOk ∧ b.qOk ∧ b.rat ≠ 0 := Iff.rfl
@[simp] theorem rOk_pow (a : Number) (n : ℕ) : (a ^ n).rOk ↔ a.rOk := Iff.rfl
@[simp] theorem qOk_pow (a : Number) (n : ℕ) : (a ^ n).qOk ↔ a.qOk := Iff.rfl
@[simp] theorem real_mark (ok : Prop) (a : Number) : (mark ok a).real = a.real := rfl
@[simp] theorem rat_mark (ok : Prop) (a : Number) : (mark ok a).rat = a.rat := rfl
@[simp] theorem rOk_mark (ok : Prop) (a : Number) : (mark ok a).rOk ↔ ok ∧ a.rOk := Iff.rfl
@[simp] theorem qOk_mark (ok : Prop) (a : Number) : (mark ok a).qOk ↔ ok ∧ a.qOk := Iff.rfl

@[simp] theorem real_sqrtWith (bits : ℕ) (a : Number) :
    (sqrtWith bits a).real = Real.sqrt a.real := rfl
@[simp] theorem rat_sqrtWith (bits : ℕ) (a : Number) :
    (sqrtWith bits a).rat = RationalMath.sqrtWith bits a.rat := rfl

theorem real_list_prod (xs : List Number) : xs.prod.real = (xs.map Number.real).prod := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]

theorem real_list_sum (xs : List Number) : xs.sum.real = (xs.map Number.real).sum := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]

@[simp] theorem rOk_list_prod (xs : List Number) : xs.prod.rOk ↔ ∀ x ∈ xs, x.rOk := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]

@[simp] theorem qOk_list_prod (xs : List Number) : xs.prod.qOk ↔ ∀ x ∈ xs, x.qOk := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]

@[simp] theorem rOk_list_sum (xs : List Number) : xs.sum.rOk ↔ ∀ x ∈ xs, x.rOk := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]

@[simp] theorem qOk_list_sum (xs : List Number) : xs.sum.qOk ↔ ∀ x ∈ xs, x.qOk := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]

end QuantumManyBodyVariational.HybridMath
