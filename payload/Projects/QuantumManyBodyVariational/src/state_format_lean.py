"""Structure-preserving Lean generation from the checked state-format IR."""
from __future__ import annotations

from fractions import Fraction
import re

from .state_format_ir import Expr, FormatError, Program


_SUPPORT = """
structure Checked (α : Type) where
  value : α
  ok : Prop

namespace Checked

def pure (a : α) : Checked α := ⟨a, True⟩

def bind (a : Checked α) (next : α → Checked β) : Checked β :=
  let b := next a.value
  ⟨b.value, a.ok ∧ b.ok⟩

def map (f : α → Checked β) : List α → Checked (List β)
  | [] => pure []
  | x :: xs => bind (f x) fun y => bind (map f xs) fun ys => pure (y :: ys)

def filter (f : α → Checked Bool) : List α → Checked (List α)
  | [] => pure []
  | x :: xs => bind (f x) fun keep => bind (filter f xs) fun ys =>
    pure (if keep then x :: ys else ys)

def flatMap (f : α → Checked (List β)) : List α → Checked (List β)
  | [] => pure []
  | x :: xs => bind (f x) fun ys => bind (flatMap f xs) fun zs => pure (ys ++ zs)

def fold (f : α → β → Checked α) (init : α) (xs : List β) : Checked α :=
  xs.foldl (fun acc x => bind acc fun a => f a x) (pure init)

def get (xs : List α) (i : ℤ) (fallback : α) : Checked α :=
  ⟨(xs[i.toNat]?).getD fallback, 0 ≤ i ∧ i < (xs.length : ℤ)⟩

def intRange (start stop step : ℤ) : Checked (List ℤ) :=
  let count := if 0 < step then Int.toNat ((stop - start + step - 1) / step)
    else if step < 0 then Int.toNat ((start - stop - step - 1) / (-step)) else 0
  ⟨(List.range count).map (fun i => start + (i : ℤ) * step), step ≠ 0⟩

def lookup [DecidableEq α] (key : α) (fallback : β) : List (α × β) → β
  | [] => fallback
  | (other, value) :: xs => if key = other then value else lookup key fallback xs

end Checked

abbrev WordType := Word (Site __DIM__ __QUBITS__)
abbrev Factor := List ℤ × ℤ × LocalPauli.Pauli

def coordinatesLE : List ℤ → List ℤ → Bool
  | [], _ => true
  | _ :: _, [] => false
  | a :: as, b :: bs => if a < b then true else if b < a then false else coordinatesLE as bs

def factors (p : WordType) : List Factor :=
  let sites := p.support.toList.mergeSort (fun s t =>
    coordinatesLE (List.ofFn s.1 ++ [(s.2.val : ℤ)]) (List.ofFn t.1 ++ [(t.2.val : ℤ)]))
  sites.map (fun s => (List.ofFn s.1, (s.2.val : ℤ), p s))

def latticeCell (xs : List ℤ) : Lattice __DIM__ :=
  fun i => (xs[i.val]?).getD 0

def validSite (xs : List ℤ) (i : ℤ) : Prop :=
  xs.length = __DIM__ ∧ 0 ≤ i ∧ i < (__QUBITS__ : ℤ)

def factorSite (entry : Factor) : Site __DIM__ __QUBITS__ :=
  (latticeCell entry.1, Fin.ofNat __QUBITS__ entry.2.1.toNat)

def pauliAt (p : WordType) (xs : List ℤ) (i : ℤ) : Checked LocalPauli.Pauli :=
  ⟨p (latticeCell xs, Fin.ofNat __QUBITS__ i.toNat), validSite xs i⟩

def translateWord (p : WordType) (xs : List ℤ) : Checked WordType :=
  ⟨cellTranslate (latticeCell xs) p, xs.length = __DIM__⟩

def wordFromFactors (entries : List Factor) : Checked WordType :=
  ⟨(entries.map (fun e => Finsupp.single (factorSite e) e.2.2)).sum,
    (entries.map factorSite).Nodup ∧ ∀ e ∈ entries, validSite e.1 e.2.1⟩
"""

_SCALAR_TYPES = {
    "Int": "ℤ", "Nat": "ℕ", "Rat": "ℚ", "Real": "HybridMath.Number",
    "Bool": "Bool", "Pauli": "LocalPauli.Pauli", "Word": "WordType",
    "Factor": "Factor", "Cell": "(List ℤ)",
}

_PAULIS = {"I": 0, "X": 1, "Y": 2, "Z": 3}
_ARITHMETIC = {
    "add": "+", "sub": "-", "mul": "*", "div": "/",
    "floordiv": "/", "mod": "%",
}
_COMPARE = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}
_MATH = ("sqrt", "exp", "sin", "cos", "log", "tanh")

_LIMIT_THEOREMS = """
theorem approximation_tendsto (h : LimitTail) (p : WordType) :
    Filter.Tendsto (approximation p) Filter.atTop (nhds (f p)) := by
  unfold f
  apply CauchySeq.tendsto_limUnder
  apply Metric.cauchySeq_iff'.2
  intro ε hε
  obtain ⟨k, hk⟩ := exists_pow_lt_of_lt_one hε (show (1 / 2 : ℝ) < 1 by norm_num)
  refine ⟨cutoff p k, ?_⟩
  intro m hm
  rw [Real.dist_eq]
  exact lt_of_le_of_lt (h p k m hm) hk

theorem approximation_error (h : LimitTail) (p : WordType) (k : ℕ) :
    |f p - approximation p (cutoff p k)| ≤ (1 / 2 : ℝ) ^ k := by
  apply le_of_tendsto ((approximation_tendsto h p).sub tendsto_const_nhds).abs
  exact Filter.eventually_atTop.2 ⟨cutoff p k, fun m hm => h p k m hm⟩
"""


def _type(typ):
    if isinstance(typ, str) and typ in _SCALAR_TYPES:
        return _SCALAR_TYPES[typ]
    if typ[0] == "List":
        return f"(List {_type(typ[1])})"
    if typ[0] == "Tuple":
        return "(" + " × ".join(_type(item) for item in typ[1:]) + ")"
    if typ[0] == "Fn":
        return "(" + " → ".join([*map(_type, typ[1:-1]), f"Checked {_type(typ[-1])}"]) + ")"
    raise FormatError(f"Lean backend does not support type {typ!r}")


def _integer_token(value: int) -> str:
    # Lean accepts hexadecimal numerals and Python renders these without a
    # decimal digit threshold. Retain decimal notation for ordinary inputs.
    return hex(value) if value.bit_length() > 2048 else str(value)


def _rational(value):
    q = Fraction(value)
    numerator = f"({_integer_token(q.numerator)} : ℚ)"
    if q.denominator == 1:
        return numerator
    return f"({numerator} / ({_integer_token(q.denominator)} : ℚ))"


def _literal(typ, value):
    if typ == "Real":
        return f"(HybridMath.ofRat {_rational(value)})"
    if typ == "Rat":
        return _rational(value)
    if typ == "Bool":
        return "true" if value else "false"
    if typ == "Pauli":
        return f"({_PAULIS[value]} : LocalPauli.Pauli)"
    return f"({_integer_token(value)} : {_type(typ)})"


def _fallback(typ):
    if typ == "Bool":
        return "false"
    if typ == "Factor":
        return "(([], 0, 0) : Factor)"
    if typ == "Cell" or typ[0] == "List":
        return f"([] : {_type(typ)})"
    if typ[0] == "Tuple":
        return "(" + ", ".join(_fallback(x) for x in typ[1:]) + ")"
    if typ[0] == "Fn":
        args = " ".join(f"(_ : {_type(x)})" for x in typ[1:-1])
        return f"(fun {args} => Checked.pure {_fallback(typ[-1])})"
    return f"(0 : {_type(typ)})"


class _Emitter:
    def __init__(self, program):
        self.program = program
        self.counter = 0
        self.functions = {
            fn.name: f"dsl{index}_{re.sub(r'[^A-Za-z0-9_]', '_', fn.name)}"
            for index, fn in enumerate(program.functions)
        }

    def fresh(self):
        self.counter += 1
        return f"v{self.counter}"

    @staticmethod
    def bind_all(bindings, tail):
        prefix = "".join(f"(Checked.bind {term} (fun {name} => " for name, term in bindings)
        return prefix + tail + "))" * len(bindings)

    @staticmethod
    def atom(arg, scope):
        if arg.op == "var":
            return scope[arg.value]
        if arg.op == "literal":
            return _literal(arg.type, arg.value)
        return None

    def values(self, args, scope, continuation):
        """Bind each nontrivial argument once before using its value."""
        pairs = []
        values = []
        for arg in args:
            name = self.atom(arg, scope)
            if name is None:
                name = self.fresh()
                pairs.append((name, self.expr(arg, scope)))
            values.append(name)
        return self.bind_all(pairs, continuation(values))

    def let_block(self, e, scope):
        prefix = []
        closing_parens = 0
        scope = dict(scope)
        while e.op == "let":
            value, body = e.args
            if value.op == "var":
                name = scope[value.value]
            else:
                name = self.fresh()
                if value.op == "literal":
                    literal = _literal(value.type, value.value)
                    prefix.append(f"(let {name} := {literal}; ")
                    closing_parens += 1
                else:
                    prefix.append(f"(Checked.bind {self.expr(value, scope)} (fun {name} => ")
                    closing_parens += 2
            scope[e.value] = name
            e = body
        return "".join(prefix) + self.expr(e, scope) + ")" * closing_parens

    def expr(self, e, scope):
        if e.op == "var":
            return f"(Checked.pure {scope[e.value]})"
        if e.op == "literal":
            return f"(Checked.pure {_literal(e.type, e.value)})"
        if e.op == "if":
            cond, yes, no = e.args
            return self.values([cond], scope, lambda xs: (
                f"(if {xs[0]} then {self.expr(yes, scope)} else {self.expr(no, scope)})"
            ))
        if e.op == "let":
            return self.let_block(e, scope)
        if e.op == "lambda":
            types = e.type[1:-1]
            names = [self.fresh() for _ in e.value]
            params = " ".join(f"({name} : {_type(t)})" for name, t in zip(names, types))
            body = self.expr(e.args[0], {**scope, **dict(zip(e.value, names))})
            return f"(Checked.pure (fun {params} => {body}))"
        if e.op == "call":
            def call(xs):
                return "(" + " ".join([self.functions[e.value], *xs]) + ")"
            return self.values(e.args, scope, call)
        if e.op in ("list", "tuple"):
            if e.op == "list":
                return self.values(e.args, scope, lambda xs: (
                    f"(Checked.pure ([{', '.join(xs)}] : {_type(e.type)}))"
                ))
            return self.values(e.args, scope, lambda xs: f"(Checked.pure ({', '.join(xs)}))")
        if e.op == "builtin":
            if e.value in ("and", "or"):
                left, right = e.args
                return self.values([left], scope, lambda xs: (
                    f"(if {xs[0]} then {self.expr(right, scope)} else Checked.pure false)"
                    if e.value == "and" else
                    f"(if {xs[0]} then Checked.pure true else {self.expr(right, scope)})"
                ))
            if e.value == "require":
                condition, value = e.args
                return self.values([condition], scope, lambda xs: (
                    f"(if {xs[0]} then {self.expr(value, scope)} else "
                    f"(⟨{_fallback(e.type)}, False⟩ : Checked {_type(e.type)}))"
                ))
            return self.values(e.args, scope, lambda xs: self.builtin(e, xs))
        raise FormatError(f"Lean backend does not support IR operation {e.op!r}")

    def builtin(self, e, xs):
        name = e.value

        def pure(term):
            return f"(Checked.pure ({term}))"

        def checked(term, requirement):
            return f"((⟨{term}, {requirement}⟩) : Checked {_type(e.type)})"

        def applied(function):
            return " ".join([function, *xs])

        if name in _ARITHMETIC:
            a, b = xs
            term = f"{a} {_ARITHMETIC[name]} {b}"
            if name in ("floordiv", "mod"):
                return checked(term, f"0 < {b}")
            if name == "div" and e.type != "Real":
                return checked(term, f"{b} ≠ 0")
            return pure(term)
        if name == "pow":
            return pure(f"{xs[0]} ^ {xs[1]}")
        if name == "neg":
            return pure(f"-{xs[0]}")
        if name == "abs":
            if e.type == "Real":
                return pure(applied("HybridMath.abs"))
            return pure(f"|{xs[0]}|")
        if name in _COMPARE:
            a, b = xs
            if e.args[0].type == "Real":
                result = f"decide (({a}).rat {_COMPARE[name]} ({b}).rat)"
                return checked(result, f"({a}).qOk ∧ ({b}).qOk")
            return pure(f"decide ({a} {_COMPARE[name]} {b})")
        if name == "not":
            return pure(f"!{xs[0]}")
        if name.startswith("to_"):
            target, origin = e.type, e.args[0].type
            x = xs[0]
            if target == origin:
                return pure(x)
            if target == "Real":
                return pure(f"HybridMath.ofRat ({x} : ℚ)" if origin in ("Rat", "Nat")
                            else f"HybridMath.ofInt {x}")
            if target == "Rat":
                return pure(f"({x} : ℚ)")
            if target == "Int":
                return pure(f"Int.ofNat {x}")
            if target == "Nat":
                return checked(f"Int.toNat {x}", f"0 ≤ {x}")
        if name in _MATH:
            count = self.program.bits if name == "sqrt" else self.program.terms
            return pure(f"HybridMath.{name}With {count} {xs[0]}")
        if name in ("min", "max"):
            function = {"min": "HybridMath.minimum", "max": "HybridMath.maximum"}[name] if e.type == "Real" else name
            return pure(applied(function))
        if name in ("pi", "e"):
            return pure(f"HybridMath.{name}With {self.program.terms}")
        if name == "len":
            a = xs[0]
            typ = e.args[0].type
            if typ == "Word":
                return pure(f"(({a}).support.card : ℤ)")
            if typ[0] == "Tuple":
                return pure(f"({len(typ) - 1} : ℤ)")
            return pure(f"(({a}).length : ℤ)")
        if name == "range":
            return f"({applied('Checked.intRange')})"
        if name == "get":
            container, index = xs
            typ = e.args[0].type
            if typ[0] == "Tuple":
                offset = e.args[1].value
                suffix = ".2" * offset + (".1" if offset < len(typ) - 2 else "")
                return pure(f"({container}){suffix}")
            if typ == "Word":
                container = f"(factors {container})"
            if e.args[1].type == "Nat":
                index = f"({index} : ℤ)"
            return f"(Checked.get {container} {index} {_fallback(e.type)})"
        if name == "make_cell":
            return checked(xs[0], f"({xs[0]}).length = {self.program.dimension}")
        if name == "factor":
            return checked(f"({', '.join(xs)})", f"validSite {xs[0]} {xs[1]}")
        if name == "word_from_factors":
            return f"({applied('wordFromFactors')})"
        if name == "cell":
            return pure(f"({xs[0]}).1")
        if name == "qubit":
            return pure(f"({xs[0]}).2.1")
        if name == "pauli":
            return pure(f"({xs[0]}).2.2")
        if name == "factors":
            return pure(applied("factors"))
        if name == "labels":
            return pure(f"(factors {xs[0]}).map (fun factor => factor.2.2)")
        if name == "pauli_at":
            return f"({applied('pauliAt')})"
        if name == "translate":
            return f"({applied('translateWord')})"
        if name in ("map", "filter", "flat_map", "fold"):
            function = "flatMap" if name == "flat_map" else name
            return f"({applied('Checked.' + function)})"
        if name in ("sum", "prod"):
            return pure(f"({xs[0]}).{name}")
        if name in ("any", "all"):
            return pure(f"({xs[0]}).{name} (fun b => b)")
        if name == "zip":
            return pure(f"({xs[0]}).zip {xs[1]}")
        if name == "concat":
            return pure(f"{xs[0]} ++ {xs[1]}")
        if name == "contains":
            return pure(f"decide ({xs[1]} ∈ {xs[0]})")
        if name == "lookup":
            return pure(f"Checked.lookup {xs[1]} {xs[2]} {xs[0]}")
        raise FormatError(f"Lean backend does not support builtin {name!r}")

    def definition(self, fn):
        names = [self.fresh() for _ in fn.params]
        params = " ".join(
            f"({local} : {_type(t)})" for local, (_, t) in zip(names, fn.params)
        )
        scope = {original: local for local, (original, _) in zip(names, fn.params)}
        body = self.expr(fn.body, scope)
        return f"def {self.functions[fn.name]} {params} : Checked {_type(fn.returns)} :=\n  {body}\n"


def emit_lean(program: Program, namespace: str = "GeneratedState") -> str:
    """Emit named Lean functions and a fixed, task-specific acceptance target."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", namespace):
        raise FormatError("Invalid Lean namespace")
    emitter = _Emitter(program)
    support = _SUPPORT.replace("__DIM__", str(program.dimension)).replace("__QUBITS__", str(program.qubits_per_cell))
    try:
        definitions = "\n".join(emitter.definition(fn) for fn in program.functions)
    except RecursionError as exc:
        raise FormatError("Source nesting exceeds the Lean generator recursion budget") from exc
    state_condition = f"StateCondition {program.dimension} {program.qubits_per_cell} f"
    imports = ""
    if program.limit is None:
        entry = emitter.functions["f"]
        result = (
            f"def f (p : WordType) : ℝ := ({entry} p).value.real\n\n"
            f"def NumericalPreconditions : Prop := ∀ p : WordType,\n"
            f"  let result := {entry} p\n"
            "  result.ok ∧ result.value.rOk\n\n"
            "def ProofObligation : Prop :=\n"
            f"  {state_condition} ∧ NumericalPreconditions\n"
        )
    else:
        imports = "public import Mathlib.Topology.MetricSpace.Cauchy\n"
        approximant, cutoff = (emitter.functions[name] for name in program.limit)
        result = (
            "def approximation (p : WordType) (n : ℕ) : ℝ :=\n"
            f"  ({approximant} p n).value.real\n\n"
            "def cutoff (p : WordType) (k : ℕ) : ℕ :=\n"
            f"  ({cutoff} p k).value\n\n"
            "def f (p : WordType) : ℝ :=\n"
            "  Filter.limUnder Filter.atTop (approximation p)\n\n"
            "def NumericalPreconditions : Prop :=\n"
            "  (∀ (p : WordType) (n : ℕ),\n"
            f"    let result := {approximant} p n\n"
            "    result.ok ∧ result.value.rOk) ∧\n"
            f"  (∀ (p : WordType) (k : ℕ), ({cutoff} p k).ok)\n\n"
            "def LimitTail : Prop := ∀ (p : WordType) (k m : ℕ),\n"
            "  cutoff p k ≤ m →\n"
            "    |approximation p m - approximation p (cutoff p k)| ≤ (1 / 2 : ℝ) ^ k\n\n"
            "def ProofObligation : Prop :=\n"
            f"  {state_condition} ∧ NumericalPreconditions ∧ LimitTail\n"
            + _LIMIT_THEOREMS
        )
    return (
        "module\n\n"
        "public import QuantumManyBodyVariational.TrustedStateSpecV2\n"
        "public import QuantumManyBodyVariational.HybridMath\n"
        + imports + "\n@[expose] public section\n"
        "noncomputable section\n"
        f"namespace {namespace}\n"
        "open QuantumManyBodyVariational\n"
        "open QuantumManyBodyVariational.InfiniteState\n"
        "open QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2\n"
        + support + "\n" + definitions + "\n" + result + f"\nend {namespace}\n"
    )
