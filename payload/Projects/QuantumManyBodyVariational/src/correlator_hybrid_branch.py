"""Real numeric values with lazy rational expressions for branch decisions.

``value`` lazily follows ordinary finite Python float arithmetic. The independent
rational expression is built from source-decimal constants and deterministic
RationalMath primitives. It is evaluated only when a branch consumes it.
The real value is never converted back into a Fraction to decide a branch.

This implements a language rule, not an error certificate: generated Lean
must use the identical rational branch algorithm while keeping its returned
value in the ideal real arithmetic. No branch margin or float comparison is
used. This module does not prove legality, translation invariance, or a bound
on the final real-value numerical error.
"""
from __future__ import annotations

from fractions import Fraction
import math
from typing import Iterable

from .correlator_rational_math import RationalMath


LANGUAGE_POLICY = "real_values_with_lazy_rational_branch_expressions-v1"
MAX_RATIONAL_BITS = 65536
MAX_FORCE_NODES = 1_000_000
MAX_INTEGER_POWER = 1024
_MATH_FUNCTIONS = {"sqrt", "exp", "sin", "cos", "log", "tanh"}
_UNSET = object()


class HybridBranchError(ValueError):
    """Unsupported numeric input, domain error, or resource exhaustion."""


class HybridDomainError(HybridBranchError):
    """The requested real numerical value or rational branch is undefined."""


def _exact(value) -> Fraction:
    if type(value) is int:
        result = Fraction(value)
    elif type(value) is Fraction:
        result = value
    elif type(value) is str:
        if not value or len(value) > 4096:
            raise HybridBranchError("Exact numeric text must contain at most 4096 characters")
        parts = value.lower().split("e")
        if len(parts) > 2:
            raise HybridBranchError("Invalid exact numeric text")
        if len(parts) == 2:
            exponent = parts[1].lstrip("+-").lstrip("0") or "0"
            if len(exponent) > 5:
                raise HybridBranchError("Exact numeric exponent exceeds resource limit")
            try:
                exponent_value = int(parts[1])
            except ValueError as exc:
                raise HybridBranchError("Invalid exact numeric exponent") from exc
            if abs(exponent_value) > 16384:
                raise HybridBranchError("Exact numeric exponent exceeds resource limit")
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            raise HybridBranchError("Invalid exact numeric text") from exc
    else:
        raise HybridBranchError("Use an int, Fraction, or exact decimal text; rounded floats are not source constants")
    if max(abs(result.numerator).bit_length(), result.denominator.bit_length()) > MAX_RATIONAL_BITS:
        raise HybridBranchError("Rational branch arithmetic exceeds its bit limit")
    return result


def _finite(value, label="Real numerical value") -> float:
    if type(value) is bool:
        raise HybridBranchError("Boolean values are not numerical real expressions")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HybridDomainError(f"{label} is outside the finite float domain") from exc
    if not math.isfinite(result):
        raise HybridDomainError(f"{label} must be finite")
    return result


class _QNode:
    """An immutable expression edge set with a mutable, private result cache."""

    __slots__ = ("kind", "args", "data", "result")

    def __init__(self, kind, args=(), data=None):
        self.kind = kind
        self.args = tuple(args)
        self.data = data
        self.result = _UNSET


class _RNode(_QNode):
    """A separate lazy graph for numerical real-value evaluation."""

    __slots__ = ()


def _force_real(root: _RNode) -> float:
    stack = [(root, False)]
    visits = 0
    while stack:
        node, ready = stack.pop()
        if node.result is not _UNSET:
            continue
        visits += 1
        if visits > MAX_FORCE_NODES:
            raise HybridBranchError("Real numerical expression exceeds its operation budget")
        if not ready:
            stack.append((node, True))
            stack.extend((arg, False) for arg in reversed(node.args) if arg.result is _UNSET)
            continue
        values = [arg.result for arg in node.args]
        kind = node.kind
        try:
            if kind == "literal":
                value = node.data
            elif kind == "constant":
                value = getattr(math, node.data)
            elif kind == "math":
                value = getattr(math, node.data)(values[0])
            elif kind == "add":
                value = values[0] + values[1]
            elif kind == "sub":
                value = values[0] - values[1]
            elif kind == "mul":
                value = values[0] * values[1]
            elif kind == "div":
                if values[1] == 0:
                    raise HybridDomainError("Real numerical division denominator is zero")
                value = values[0] / values[1]
            elif kind == "neg":
                value = -values[0]
            elif kind == "abs":
                value = abs(values[0])
            elif kind == "pow":
                value = values[0] ** node.data
            elif kind == "min":
                value = min(values)
            elif kind == "max":
                value = max(values)
            else:
                raise HybridBranchError(f"Unsupported private real numerical node: {kind}")
            node.result = _finite(value)
        except HybridBranchError:
            raise
        except (ValueError, ArithmeticError, TypeError) as exc:
            raise HybridDomainError(f"Real numerical evaluation failed: {exc}") from exc
    return root.result


def _force(root: _QNode) -> Fraction:
    """Iterative postorder evaluation handles deep finite accumulations.

    Nodes are private and only constructed as an acyclic graph. Shared
    subexpressions are evaluated once, including when several branches read
    the same local variable. No Python recursion follows this expression DAG.
    """
    stack = [(root, False)]
    visits = 0
    while stack:
        node, ready = stack.pop()
        if node.result is not _UNSET:
            continue
        visits += 1
        if visits > MAX_FORCE_NODES:
            raise HybridBranchError("Rational branch expression exceeds its operation budget")
        if not ready:
            stack.append((node, True))
            stack.extend((arg, False) for arg in reversed(node.args) if arg.result is _UNSET)
            continue
        values = [arg.result for arg in node.args]
        kind = node.kind
        try:
            if kind == "literal":
                value = node.data
            elif kind == "constant":
                name, backend = node.data
                value = backend.constant(name)
            elif kind == "math":
                name, backend = node.data
                value = backend.approx(name, values[0])
            elif kind == "add":
                value = values[0] + values[1]
            elif kind == "sub":
                value = values[0] - values[1]
            elif kind == "mul":
                value = values[0] * values[1]
            elif kind == "div":
                if values[1] == 0:
                    raise HybridDomainError("Rational branch division by zero")
                value = values[0] / values[1]
            elif kind == "neg":
                value = -values[0]
            elif kind == "abs":
                value = abs(values[0])
            elif kind == "pow":
                exponent = node.data
                base = values[0]
                if exponent and max(abs(base.numerator).bit_length(), base.denominator.bit_length()) * exponent > MAX_RATIONAL_BITS:
                    raise HybridBranchError("Rational branch power exceeds its bit limit")
                value = base ** exponent
            elif kind == "min":
                value = min(values)
            elif kind == "max":
                value = max(values)
            else:
                raise HybridBranchError(f"Unsupported private rational branch node: {kind}")
            node.result = _exact(value)
        except HybridBranchError:
            raise
        except (ValueError, ArithmeticError, TypeError) as exc:
            raise HybridDomainError(f"Rational branch evaluation failed: {exc}") from exc
    return root.result


class HybridReal:
    """Independent lazy float-value and rational-branch expression graphs."""

    __slots__ = ("_real_node", "_node")
    __hash__ = None

    def __init__(self, value, node):
        self._real_node = value if isinstance(value, _RNode) else _RNode("literal", data=value)
        if not isinstance(node, _QNode):
            node = _QNode("literal", data=_exact(node))
        self._node = node

    @property
    def value(self) -> float:
        return _force_real(self._real_node)

    def rational(self) -> Fraction:
        return _force(self._node)

    def __float__(self):
        return self.value

    def __bool__(self):
        raise HybridBranchError("Use an explicit comparison, whose operands are evaluated by the rational branch module")

    def __add__(self, other):
        other = coerce(other)
        return HybridReal(_RNode("add", (self._real_node, other._real_node)), _QNode("add", (self._node, other._node)))

    __radd__ = __add__

    def __sub__(self, other):
        other = coerce(other)
        return HybridReal(_RNode("sub", (self._real_node, other._real_node)), _QNode("sub", (self._node, other._node)))

    def __rsub__(self, other):
        return coerce(other) - self

    def __mul__(self, other):
        other = coerce(other)
        return HybridReal(_RNode("mul", (self._real_node, other._real_node)), _QNode("mul", (self._node, other._node)))

    __rmul__ = __mul__

    def __truediv__(self, other):
        other = coerce(other)
        return HybridReal(_RNode("div", (self._real_node, other._real_node)), _QNode("div", (self._node, other._node)))

    def __rtruediv__(self, other):
        return coerce(other) / self

    def __neg__(self):
        return HybridReal(_RNode("neg", (self._real_node,)), _QNode("neg", (self._node,)))

    def __pos__(self):
        return self

    def __abs__(self):
        return HybridReal(_RNode("abs", (self._real_node,)), _QNode("abs", (self._node,)))

    def pow_n(self, exponent):
        if type(exponent) is not int or not 0 <= exponent <= MAX_INTEGER_POWER:
            raise HybridBranchError(f"Integer exponent must lie in 0..{MAX_INTEGER_POWER}")
        return HybridReal(_RNode("pow", (self._real_node,), exponent), _QNode("pow", (self._node,), exponent))

    def __pow__(self, exponent):
        return self.pow_n(exponent)

    def __lt__(self, other):
        return compare("lt", self, other)

    def __le__(self, other):
        return compare("le", self, other)

    def __gt__(self, other):
        return compare("gt", self, other)

    def __ge__(self, other):
        return compare("ge", self, other)

    def __eq__(self, other):
        if not isinstance(other, HybridReal) and type(other) not in (int, Fraction):
            return NotImplemented
        return compare("eq", self, other)

    def __ne__(self, other):
        if not isinstance(other, HybridReal) and type(other) not in (int, Fraction):
            return NotImplemented
        return compare("ne", self, other)


def from_literal(value) -> HybridReal:
    exact = _exact(value)
    return HybridReal(_RNode("literal", data=exact), _QNode("literal", data=exact))


def coerce(value) -> HybridReal:
    if isinstance(value, HybridReal):
        return value
    return from_literal(value)


def math_func(name: str, value, maths: RationalMath | None = None) -> HybridReal:
    if name not in _MATH_FUNCTIONS:
        raise HybridBranchError(f"Unsupported mathematical function: {name}")
    value = coerce(value)
    return HybridReal(_RNode("math", (value._real_node,), name),
                      _QNode("math", (value._node,), (name, maths or RationalMath())))


def constant(name: str, maths: RationalMath | None = None) -> HybridReal:
    if name not in ("pi", "e"):
        raise HybridBranchError("Only pi and e are supported mathematical constants")
    return HybridReal(_RNode("constant", data=name), _QNode("constant", data=(name, maths or RationalMath())))


def minimum(values: Iterable) -> HybridReal:
    items = tuple(coerce(value) for value in values)
    if not items:
        raise HybridDomainError("min requires a nonempty finite sequence")
    return HybridReal(_RNode("min", tuple(item._real_node for item in items)),
                      _QNode("min", tuple(item._node for item in items)))


def maximum(values: Iterable) -> HybridReal:
    items = tuple(coerce(value) for value in values)
    if not items:
        raise HybridDomainError("max requires a nonempty finite sequence")
    return HybridReal(_RNode("max", tuple(item._real_node for item in items)),
                      _QNode("max", tuple(item._node for item in items)))


def compare(op: str, a, b) -> bool:
    aliases = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge", "==": "eq", "!=": "ne"}
    op = aliases.get(op, op)
    if op not in ("lt", "le", "gt", "ge", "eq", "ne"):
        raise HybridBranchError("Unsupported rational branch comparison")
    # Integer-to-integer control stays exact even beyond float's finite range.
    if type(a) is int and type(b) is int:
        left, right = a, b
    else:
        left, right = coerce(a).rational(), coerce(b).rational()
    return {"lt": lambda: left < right, "le": lambda: left <= right,
            "gt": lambda: left > right, "ge": lambda: left >= right,
            "eq": lambda: left == right, "ne": lambda: left != right}[op]()


class BranchMath:
    """One explicit approximation policy shared by a compiled program."""

    def __init__(self, bits=40, terms=32):
        self.maths = RationalMath(bits=bits, terms=terms)

    @staticmethod
    def literal(value):
        return from_literal(value)

    @staticmethod
    def coerce(value):
        return coerce(value)

    def math_func(self, name, value):
        return math_func(name, value, self.maths)

    def constant(self, name):
        return constant(name, self.maths)

    @staticmethod
    def minimum(values):
        return minimum(values)

    @staticmethod
    def maximum(values):
        return maximum(values)

    @staticmethod
    def compare(op, a, b):
        return compare(op, a, b)

    def manifest(self):
        return {"policy": LANGUAGE_POLICY, "rational_math": self.maths.manifest(),
                "value_semantics": "lazy_finite_float_evaluation_of_selected_ideal_real_expression",
                "branch_semantics": "exact_comparison_of_lazy_deterministic_rational_expression",
                "evaluation_strategy": "independent_iterative_real_and_rational_dags; unused pure values are not forced",
                "real_value_recast_to_fraction": False,
                "branch_margin": None, "formal_numerical_error_bound": False,
                "limits": {"rational_bits": MAX_RATIONAL_BITS, "force_nodes": MAX_FORCE_NODES,
                           "integer_power": MAX_INTEGER_POWER}}
