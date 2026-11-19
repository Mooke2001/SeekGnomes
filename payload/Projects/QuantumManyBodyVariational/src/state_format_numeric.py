"""Adaptive enclosures for the value path of the shared correlator format."""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
import math
import sys
from threading import local

from mpmath.ctx_iv import MPIntervalContext

from .correlator_float_guard import MAX_ENDPOINT_BITS, _bounds
from .correlator_hybrid_branch import HybridReal, MAX_FORCE_NODES
from .state_format_ir import FormatError


_START_PRECISION = 64
_MAX_PRECISION = 8192
_TOLERANCE = Fraction(1, 2**44)
_FLOAT_MAX = Fraction(sys.float_info.max)
_CONTEXT = local()


class _NeedsPrecision(Exception):
    """At this precision, an enclosure cannot establish the required result."""


def _dependencies(root):
    order = []
    seen = set()
    stack = [(root, False)]
    while stack:
        node, ready = stack.pop()
        if ready:
            order.append(node)
        elif node not in seen:
            seen.add(node)
            if len(seen) > MAX_FORCE_NODES:
                raise FormatError("Real expression exceeds its operation budget")
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.args))
    return order, Counter(child for node in order for child in node.args)


def _tanh_at(ctx, point):
    absolute = abs(point)
    if absolute >= ctx.prec:
        # exp(-2|x|) is between zero and 2^(-prec) for |x| >= prec.
        decay = ctx.mpf([0, ctx.mpf(2)**(-ctx.prec)])
    else:
        decay = ctx.exp(-2 * absolute)
    magnitude = (1 - decay) / (1 + decay)
    return -magnitude if point < 0 else magnitude


def _elementary(ctx, name, arg):
    if name == "sqrt":
        if arg.b < 0:
            raise FormatError("Real square root requires a nonnegative argument")
        if arg.a < 0:
            raise _NeedsPrecision("a square-root argument straddles zero")
    elif name == "log":
        if arg.b <= 0:
            raise FormatError("Real logarithm requires a positive argument")
        if arg.a <= 0:
            raise _NeedsPrecision("a logarithm argument straddles zero")
    elif name == "tanh":
        # Monotonicity lets the two endpoint formulas choose opposite signs
        # when the input interval crosses zero.
        lower, upper = _tanh_at(ctx, arg.a), _tanh_at(ctx, arg.b)
        return ctx.make_mpf((lower._mpi_[0], upper._mpi_[1]))
    return getattr(ctx, name)(arg)


def _node_interval(ctx, node, args):
    op = node.kind
    if op == "literal":
        value = node.data
        result = ctx.mpf(value.numerator)
        return result if value.denominator == 1 else result / ctx.mpf(value.denominator)
    if op == "constant":
        return ctx.mpf(getattr(ctx, node.data))
    if op == "math":
        return _elementary(ctx, node.data, args[0])
    if op == "add":
        return args[0] + args[1]
    if op == "sub":
        if node.args[0] is node.args[1]:
            return ctx.mpf(0)
        return args[0] - args[1]
    if op == "mul":
        if node.args[0] is node.args[1]:
            return args[0]**2
        return args[0] * args[1]
    if op == "div":
        den = args[1]
        if den.a == 0 and den.b == 0:
            raise FormatError("Real division denominator is zero")
        if den.a <= 0 <= den.b:
            raise _NeedsPrecision("a division denominator straddles zero")
        if node.args[0] is node.args[1]:
            return ctx.mpf(1)
        return args[0] / den
    if op == "neg":
        return -args[0]
    if op == "abs":
        return abs(args[0])
    if op == "pow":
        return args[0] ** node.data
    if op in ("min", "max"):
        extreme = min if op == "min" else max
        lower = extreme(value.a for value in args)
        upper = extreme(value.b for value in args)
        return ctx.make_mpf((lower._mpi_[0], upper._mpi_[1]))
    raise FormatError(f"Unknown real expression operation: {op}")


def _check_interval(interval):
    for _, mantissa, exponent, bits in interval._mpi_:
        if bits < 0:
            raise _NeedsPrecision("a real enclosure is not finite")
        if mantissa and (
            bits + max(exponent, 0) > MAX_ENDPOINT_BITS or -exponent >= MAX_ENDPOINT_BITS
        ):
            raise FormatError("Real enclosure exceeds its endpoint size limit")


def _evaluate(ctx, order, uses):
    values = {}
    remaining = uses.copy()
    for node in order:
        result = _node_interval(ctx, node, [values[child] for child in node.args])
        _check_interval(result)
        values[node] = result
        for child in node.args:
            remaining[child] -= 1
            if remaining[child] == 0:
                del values[child]
    return result


def _select_float(interval):
    lo, hi = _bounds(interval)
    if lo > _FLOAT_MAX or hi < -_FLOAT_MAX:
        raise FormatError("Real result is outside finite floating-point range")
    try:
        value = float((lo + hi) / 2)
    except OverflowError as exc:
        raise _NeedsPrecision("the real result enclosure crosses the floating-point range") from exc
    if not math.isfinite(value):
        raise _NeedsPrecision("the real result enclosure crosses the floating-point range")
    rounded = Fraction(value)
    error = max(abs(rounded - lo), abs(rounded - hi))
    if error > _TOLERANCE * max(1, abs(rounded)):
        raise _NeedsPrecision("the real result enclosure is too wide")
    return value


def approximate_real(number: HybridReal) -> float:
    """Evaluate the ideal-real expression using a private outward interval context.

    The returned float is within 2^-44 * max(1, abs(result)) of either endpoint
    of the enclosure. Control decisions have already been made by the rational
    branch interpreter. This enclosure is numerical evidence, independent of
    Lean's state legality and exact energy specification.
    """
    order, uses = _dependencies(number._real_node)
    ctx = getattr(_CONTEXT, "interval", None)
    if ctx is None:
        ctx = _CONTEXT.interval = MPIntervalContext()
    precision = _START_PRECISION
    while precision <= _MAX_PRECISION:
        ctx.prec = precision
        try:
            return _select_float(_evaluate(ctx, order, uses))
        except _NeedsPrecision as exc:
            reason = str(exc)
            precision *= 2
        except FormatError:
            raise
        except (ArithmeticError, ValueError) as exc:
            raise FormatError(f"Real enclosure evaluation failed: {exc}") from exc
    raise FormatError(f"Real enclosure could not resolve within {_MAX_PRECISION} bits: {reason}")
