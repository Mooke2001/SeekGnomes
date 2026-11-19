"""Numeric evaluation of checked correlator programs.

The evaluator consumes typed syntax and never executes the submitted source.
Real values use an independent rational shadow for comparisons.
When the selected real expression has only rational operations, its exact
rational result can also be used before the final conversion to float.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from itertools import chain
import operator

from .correlator_energy import normalize_word
from .correlator_hybrid_branch import (
    MAX_INTEGER_POWER,
    MAX_RATIONAL_BITS,
    BranchMath,
    HybridBranchError,
    HybridDomainError,
    HybridReal,
)
from .state_format_ir import Expr, FormatError, Program
from .state_format_numeric import approximate_real


DEFAULT_STEPS = 1_000_000
_COMPARISONS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}
_ARITHMETIC = {"add": operator.add, "sub": operator.sub, "mul": operator.mul}
_MATH = {"sqrt", "exp", "sin", "cos", "log", "tanh"}


@dataclass(frozen=True, slots=True)
class _Closure:
    names: tuple[str, ...]
    body: Expr
    env: dict


@dataclass(frozen=True, slots=True)
class _RealValue:
    number: HybridReal
    rational_exact: bool


def _checked_exact(value):
    if type(value) is int:
        bits = value.bit_length()
    elif type(value) is Fraction:
        bits = max(value.numerator.bit_length(), value.denominator.bit_length())
    else:
        return value
    if bits > MAX_RATIONAL_BITS:
        raise FormatError("Exact arithmetic exceeds its bit limit")
    return value


def _length(value):
    if isinstance(value, range):
        distance = value.stop - value.start
        if value.step < 0:
            distance = -distance
        step = abs(value.step)
        return max(0, (distance + step - 1) // step)
    return len(value)


class Evaluator:
    def __init__(
        self, program: Program, *, max_steps: int = DEFAULT_STEPS, precision_bits: int = 40
    ):
        if type(max_steps) is not int or max_steps < 1:
            raise FormatError("The evaluation step budget must be a positive integer")
        if type(precision_bits) is not int or precision_bits < 0:
            raise FormatError("The truncation precision must be a nonnegative integer")
        self.program = program
        self.functions = {fn.name: fn for fn in program.functions}
        self.maths = BranchMath(program.bits, program.terms)
        self.max_steps = max_steps
        self.precision_bits = precision_bits

    def evaluate(self, word) -> float:
        """Evaluate the ideal correlator approximately on a standardized Pauli word."""
        try:
            word = normalize_word(word, self.program.dimension, self.program.qubits_per_cell)
        except ValueError as exc:
            raise FormatError(str(exc)) from exc
        return self.evaluate_normalized(word)

    def evaluate_normalized(self, word) -> float:
        """Evaluate a word already validated and sorted by the energy interface."""
        fuel = [self.max_steps]
        try:
            if self.program.limit is None:
                result = self._call("f", (word,), fuel)
            else:
                approx, cutoff = self.program.limit
                index = self._call(cutoff, (word, self.precision_bits), fuel)
                result = self._call(approx, (word, index), fuel)
            return self._float_result(self._real(result))
        except FormatError:
            raise
        except RecursionError as exc:
            raise FormatError("Evaluation exceeds the supported nesting depth") from exc
        except (HybridBranchError, ArithmeticError, ValueError) as exc:
            raise FormatError(str(exc)) from exc

    def _real(self, value):
        if isinstance(value, _RealValue):
            return value
        return _RealValue(self.maths.coerce(value), True)

    @staticmethod
    def _float_result(value: _RealValue) -> float:
        if value.rational_exact:
            try:
                return float(value.number.rational())
            except HybridDomainError:
                raise
            except HybridBranchError:
                # An exact intermediate can exceed the branch bit budget even
                # when the independent numerical path has a finite result.
                pass
        return approximate_real(value.number)

    def _real_binary(self, operation, left, right):
        left, right = self._real(left), self._real(right)
        return _RealValue(
            operation(left.number, right.number), left.rational_exact and right.rational_exact
        )

    @staticmethod
    def _spend(fuel, count=1):
        fuel[0] -= count
        if fuel[0] < 0:
            raise FormatError("Evaluation exceeds its step budget")

    def _call(self, name, args, fuel):
        function = self.functions[name]
        env = {parameter: value for (parameter, _), value in zip(function.params, args, strict=True)}
        return self._expr(function.body, env, fuel)

    def _apply(self, closure, args, fuel):
        env = dict(closure.env)
        env.update(zip(closure.names, args, strict=True))
        return self._expr(closure.body, env, fuel)

    def _expr(self, expr, env, fuel):
        local_scope = False
        while True:
            self._spend(fuel)
            op = expr.op
            args = expr.args
            if op == "let":
                value = self._expr(args[0], env, fuel)
                if not local_scope:
                    env = dict(env)
                    local_scope = True
                env[expr.value] = value
                expr = args[1]
                continue
            if op == "if":
                expr = args[1] if self._expr(args[0], env, fuel) else args[2]
                continue
            break

        if op == "literal":
            return self._real(expr.value) if expr.type == "Real" else _checked_exact(expr.value)
        if op == "var":
            return env[expr.value]
        if op == "lambda":
            return _Closure(expr.value, args[0], dict(env))
        if op in {"list", "tuple"}:
            return tuple(self._expr(arg, env, fuel) for arg in args)
        if op == "call":
            return self._call(expr.value, tuple(self._expr(arg, env, fuel) for arg in args), fuel)
        if op == "builtin":
            if expr.value == "and":
                return self._expr(args[0], env, fuel) and self._expr(args[1], env, fuel)
            if expr.value == "or":
                return self._expr(args[0], env, fuel) or self._expr(args[1], env, fuel)
            if expr.value == "require":
                if not self._expr(args[0], env, fuel):
                    raise FormatError("A required condition is false")
                return self._expr(args[1], env, fuel)
            values = tuple(self._expr(arg, env, fuel) for arg in args)
            return self._builtin(expr, values, fuel)
        raise FormatError(f"Unknown expression operation: {op}")

    def _reduce(self, name, result_type, values, fuel):
        if name in {"all", "any"}:
            stop = name == "any"
            for value in values:
                self._spend(fuel)
                if value is stop:
                    return stop
            return not stop
        result = 0 if name == "sum" else 1
        if result_type == "Real":
            result = self._real(result)
        elif result_type == "Rat":
            result = Fraction(result)
        combine = operator.add if name == "sum" else operator.mul
        for value in values:
            self._spend(fuel)
            result = (self._real_binary(combine, result, value) if result_type == "Real"
                      else _checked_exact(combine(result, value)))
        return result

    def _sequence(self, name, values, fuel):
        if name == "range":
            if values[2] == 0:
                raise FormatError("Range step must be nonzero")
            return range(*values)
        if name == "len":
            return _length(values[0])
        if name == "get":
            sequence, index = values
            if index < 0 or index >= _length(sequence):
                raise FormatError("Sequence index is out of bounds")
            return sequence[index]
        if name == "contains":
            sequence, target = values
            for item in sequence:
                self._spend(fuel)
                if item == target:
                    return True
            return False
        if name == "lookup":
            pairs, target, default = values
            for key, value in pairs:
                self._spend(fuel)
                if key == target:
                    return value
            return default
        if name in {"map", "filter", "flat_map"}:
            fn, items = values
            result = []
            for item in items:
                self._spend(fuel)
                evaluated = self._apply(fn, (item,), fuel)
                if name == "map":
                    result.append(evaluated)
                elif name == "flat_map":
                    self._spend(fuel, _length(evaluated))
                    result.extend(evaluated)
                elif evaluated:
                    result.append(item)
            return tuple(result)
        if name == "fold":
            fn, state, items = values
            for item in items:
                self._spend(fuel)
                state = self._apply(fn, (state, item), fuel)
            return state
        items = zip(*values) if name == "zip" else chain.from_iterable(values)
        result = []
        for item in items:
            self._spend(fuel)
            result.append(item)
        return tuple(result)

    def _pauli(self, name, values, fuel):
        if name == "make_cell":
            if _length(values[0]) != self.program.dimension:
                raise FormatError("A cell needs exactly dimension coordinates")
            return tuple(values[0])
        if name in {"cell", "qubit", "pauli"}:
            return values[0][{"cell": 0, "qubit": 1, "pauli": 2}[name]]
        if name == "factor":
            cell, qubit, label = values
            if not 0 <= qubit < self.program.qubits_per_cell:
                raise FormatError("Qubit index is outside the cell")
            return (cell, qubit, label)
        if name == "word_from_factors":
            seen = set()
            word = []
            for cell, qubit, label in values[0]:
                self._spend(fuel)
                site = (cell, qubit)
                if site in seen:
                    raise FormatError("Repeated site in one Hermitian Pauli word")
                seen.add(site)
                if label != "I":
                    word.append((cell, qubit, label))
            return tuple(sorted(word))
        if name == "factors":
            return values[0]
        if name == "labels":
            self._spend(fuel, len(values[0]))
            return tuple(factor[2] for factor in values[0])
        if name == "pauli_at":
            word, cell, qubit = values
            if not 0 <= qubit < self.program.qubits_per_cell:
                raise FormatError("Qubit index is outside the cell")
            offset = bisect_left(word, (cell, qubit, ""))
            if offset < len(word) and word[offset][:2] == (cell, qubit):
                return word[offset][2]
            return "I"
        word, by = values
        self._spend(fuel, len(word))
        return tuple(
            (tuple(_checked_exact(x + y) for x, y in zip(cell, by, strict=True)), qubit, label)
            for cell, qubit, label in word
        )

    def _builtin(self, expr, values, fuel):
        name = expr.value
        if name in _COMPARISONS:
            if expr.args[0].type == "Real" or expr.args[1].type == "Real":
                return self.maths.compare(name, *(self._real(value).number for value in values))
            return _COMPARISONS[name](*values)
        if name == "not":
            return not values[0]
        if name in _ARITHMETIC:
            operation = _ARITHMETIC[name]
            if expr.type == "Real":
                return self._real_binary(operation, *values)
            result = operation(*values)
            if name == "sub" and expr.type == "Nat":
                return max(0, result)
            return _checked_exact(result)
        if name in {"neg", "abs"}:
            apply = operator.neg if name == "neg" else abs
            if expr.type == "Real":
                value = self._real(values[0])
                return _RealValue(apply(value.number), value.rational_exact)
            return _checked_exact(apply(values[0]))
        if name == "div":
            if expr.type == "Real":
                return self._real_binary(operator.truediv, *values)
            if values[1] == 0:
                raise FormatError("Division by zero")
            return _checked_exact(Fraction(values[0]) / values[1])
        if name in {"floordiv", "mod"}:
            left, right = values
            if right <= 0:
                raise FormatError("Integer division and remainder require a positive divisor")
            return left // right if name == "floordiv" else left % right
        if name == "pow":
            base, exponent = values
            if not 0 <= exponent <= MAX_INTEGER_POWER:
                raise FormatError(f"The exponent must lie in 0..{MAX_INTEGER_POWER}")
            if expr.type == "Real":
                value = self._real(base)
                return _RealValue(value.number.pow_n(exponent), value.rational_exact)
            base_bits = (max(base.numerator.bit_length(), base.denominator.bit_length())
                         if type(base) is Fraction else base.bit_length())
            if base_bits * exponent > MAX_RATIONAL_BITS:
                raise FormatError("Exact power exceeds its bit limit")
            return _checked_exact(base ** exponent)
        if name == "to_real":
            return self._real(values[0])
        if name == "to_rat":
            return _checked_exact(Fraction(values[0]))
        if name == "to_int":
            return int(values[0])
        if name == "to_nat":
            if values[0] < 0:
                raise FormatError("Conversion to Nat requires a nonnegative integer")
            return int(values[0])
        if name in _MATH:
            return _RealValue(self.maths.math_func(name, self._real(values[0]).number), False)
        if name in {"pi", "e"}:
            return _RealValue(self.maths.constant(name), False)
        if name in {"min", "max"}:
            items = values if len(values) == 2 else values[0]
            if _length(items) == 0:
                raise FormatError(f"{name} requires a nonempty finite sequence")
            self._spend(fuel, _length(items))
            if expr.type == "Real":
                method = self.maths.minimum if name == "min" else self.maths.maximum
                reals = tuple(map(self._real, items))
                return _RealValue(method(x.number for x in reals), all(x.rational_exact for x in reals))
            return (min if name == "min" else max)(items)
        if name in {"sum", "prod", "all", "any"}:
            return self._reduce(name, expr.type, values[0], fuel)
        if name in {"range", "len", "get", "contains", "lookup", "map", "filter", "flat_map", "fold", "zip", "concat"}:
            return self._sequence(name, values, fuel)
        if name in {"cell", "qubit", "pauli", "factor", "factors", "word_from_factors",
                    "labels", "pauli_at", "translate", "make_cell"}:
            return self._pauli(name, values, fuel)
        raise FormatError(f"Unknown builtin: {name}")
