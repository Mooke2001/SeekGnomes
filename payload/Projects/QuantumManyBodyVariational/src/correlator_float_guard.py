"""Float values with independently propagated ideal-real enclosures.

The value field follows ordinary Python floating arithmetic. The rational
endpoints enclose the *mathematical* expression, whose decimal constants are
supplied before conversion to float. Branches are allowed only when these
enclosures prove a strict gap and the float comparison agrees with it.

Algebraic interval operations use Fraction exactly. Elementary functions use
a private mpmath.iv context and convert its binary endpoints without a float
round trip. This numerical backend is trusted software, not a Lean-verified
rounding theorem. These enclosures do not prove state legality or TI.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import math
from typing import Iterable

from mpmath.ctx_iv import MPIntervalContext


MAX_ENDPOINT_BITS = 65536
MAX_POWER = 4096
INTERVAL_DPS = 70
DEFAULT_MARGIN = Fraction(1, 10_000_000_000)


class FloatGuardError(ValueError):
    """Numerical domain, resource, or interval-operation failure."""


class NumericalDomainError(FloatGuardError):
    """A mathematical or float-domain condition is not established."""


class BranchAmbiguousError(FloatGuardError):
    """A floating comparison lacks a separated, matching real interpretation."""


def _fraction(value) -> Fraction:
    if type(value) is int:
        result = Fraction(value)
    elif type(value) is Fraction:
        result = value
    elif type(value) is str:
        if len(value) > 4096:
            raise FloatGuardError("Numeric text exceeds length limit")
        # Never permit a short exponent to request a gigantic integer power.
        parts = value.lower().split("e")
        if len(parts) > 2:
            raise FloatGuardError("Invalid numeric text")
        if len(parts) == 2:
            exponent = parts[1].lstrip("+-").lstrip("0") or "0"
            if len(exponent) > 5 or abs(int(parts[1])) > 16384:
                raise FloatGuardError("Numeric exponent exceeds resource limit")
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            raise FloatGuardError("Invalid exact numeric value") from exc
    else:
        raise FloatGuardError("Exact constants must be int, Fraction, or decimal text")
    if max(abs(result.numerator).bit_length(), result.denominator.bit_length()) > MAX_ENDPOINT_BITS:
        raise FloatGuardError("Interval endpoint exceeds rational size limit")
    return result


def _finite(value, label="Floating value") -> float:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise NumericalDomainError(f"{label} is outside floating-point range") from exc
    if not math.isfinite(result):
        raise NumericalDomainError(f"{label} must be finite")
    return result


def _bound_operation(value: Fraction) -> Fraction:
    return _fraction(value)


def _endpoint(raw) -> Fraction:
    """Decode mpmath's finite binary endpoint, checking size before shifting."""
    sign, mantissa, exponent, bit_count = raw
    if bit_count < 0:
        raise NumericalDomainError("Elementary-function enclosure is not finite")
    if mantissa == 0:
        return Fraction(0)
    if exponent >= 0:
        if bit_count + exponent > MAX_ENDPOINT_BITS:
            raise FloatGuardError("Elementary-function endpoint exceeds size limit")
        return Fraction((-mantissa if sign else mantissa) << exponent)
    if -exponent + 1 > MAX_ENDPOINT_BITS or bit_count > MAX_ENDPOINT_BITS:
        raise FloatGuardError("Elementary-function endpoint exceeds size limit")
    return Fraction(-mantissa if sign else mantissa, 1 << -exponent)


def _bounds(value) -> tuple[Fraction, Fraction]:
    lo, hi = map(_endpoint, value._mpi_)
    if lo > hi:
        raise FloatGuardError("Invalid elementary-function interval")
    return lo, hi


def _context() -> MPIntervalContext:
    # A private context never changes mpmath.mp or the shared mpmath.iv object.
    ctx = MPIntervalContext()
    ctx.dps = INTERVAL_DPS
    return ctx


def _interval(ctx: MPIntervalContext, lo: Fraction, hi: Fraction):
    lower = ctx.mpf(lo.numerator) / ctx.mpf(lo.denominator)
    upper = ctx.mpf(hi.numerator) / ctx.mpf(hi.denominator)
    # Both conversions are outward; retain lower lower-end and upper upper-end.
    return ctx.make_mpf((lower._mpi_[0], upper._mpi_[1]))


@dataclass(frozen=True)
class GuardedReal:
    value: float
    lo: Fraction
    hi: Fraction

    def __post_init__(self):
        object.__setattr__(self, "value", _finite(self.value))
        object.__setattr__(self, "lo", _fraction(self.lo))
        object.__setattr__(self, "hi", _fraction(self.hi))
        if self.lo > self.hi:
            raise FloatGuardError("Interval lower endpoint exceeds upper endpoint")

    @classmethod
    def coerce(cls, value) -> "GuardedReal":
        return coerce(value)

    def __float__(self) -> float:
        return self.value

    def __bool__(self):
        raise BranchAmbiguousError("Real truth testing requires an explicit separated comparison")

    def __add__(self, other):
        other = coerce(other)
        return GuardedReal(self.value + other.value,
                           _bound_operation(self.lo + other.lo),
                           _bound_operation(self.hi + other.hi))

    __radd__ = __add__

    def __sub__(self, other):
        return self + (-coerce(other))

    def __rsub__(self, other):
        return coerce(other) - self

    def __neg__(self):
        return GuardedReal(-self.value, -self.hi, -self.lo)

    def __pos__(self):
        return self

    def __mul__(self, other):
        other = coerce(other)
        endpoints = [self.lo * other.lo, self.lo * other.hi,
                     self.hi * other.lo, self.hi * other.hi]
        return GuardedReal(self.value * other.value,
                           _bound_operation(min(endpoints)),
                           _bound_operation(max(endpoints)))

    __rmul__ = __mul__

    def __truediv__(self, other):
        other = coerce(other)
        if other.lo <= 0 <= other.hi:
            raise NumericalDomainError("Division denominator enclosure includes zero")
        if other.value == 0:
            raise NumericalDomainError("Floating division denominator is zero")
        endpoints = [self.lo / other.lo, self.lo / other.hi,
                     self.hi / other.lo, self.hi / other.hi]
        return GuardedReal(self.value / other.value,
                           _bound_operation(min(endpoints)),
                           _bound_operation(max(endpoints)))

    def __rtruediv__(self, other):
        return coerce(other) / self

    def pow_n(self, exponent: int):
        if type(exponent) is not int or not -MAX_POWER <= exponent <= MAX_POWER:
            raise FloatGuardError(f"Integer exponent must lie within +/-{MAX_POWER}")
        if exponent < 0:
            if self.lo <= 0 <= self.hi or self.value == 0:
                raise NumericalDomainError("Negative power requires a nonzero input enclosure")
        if exponent == 0:
            return from_literal(1)
        # Check before constructing potentially enormous integer powers.
        if max(abs(self.lo.numerator).bit_length(), self.lo.denominator.bit_length(),
               abs(self.hi.numerator).bit_length(), self.hi.denominator.bit_length()) * abs(exponent) > MAX_ENDPOINT_BITS:
            raise FloatGuardError("Power enclosure exceeds endpoint size limit")
        left, right = self.lo ** exponent, self.hi ** exponent
        if exponent > 0 and exponent % 2 == 0 and self.lo <= 0 <= self.hi:
            lo, hi = Fraction(0), max(left, right)
        else:
            lo, hi = min(left, right), max(left, right)
        try:
            value = self.value ** exponent
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            raise NumericalDomainError("Floating power failed") from exc
        return GuardedReal(value, lo, hi)

    def __pow__(self, exponent):
        return self.pow_n(exponent)

    def __abs__(self):
        if self.lo >= 0:
            return GuardedReal(abs(self.value), self.lo, self.hi)
        if self.hi <= 0:
            return GuardedReal(abs(self.value), -self.hi, -self.lo)
        return GuardedReal(abs(self.value), Fraction(0), max(-self.lo, self.hi))


def from_literal(value: Fraction | int | str) -> GuardedReal:
    exact = _fraction(value)
    return GuardedReal(_finite(exact), exact, exact)


def promote_exact(value: int) -> GuardedReal:
    if type(value) is not int:
        raise FloatGuardError("Only exact Python integers can be promoted")
    return from_literal(value)


def coerce(value) -> GuardedReal:
    if isinstance(value, GuardedReal):
        return value
    if type(value) in (int, Fraction):
        return from_literal(value)
    if type(value) is float:
        # This is an explicitly supplied binary float constant, not a source
        # decimal. The compiler must call from_literal for source decimals.
        finite = _finite(value)
        exact = Fraction.from_float(finite)
        return GuardedReal(finite, exact, exact)
    raise FloatGuardError("Expected a guarded real or exact scalar")


def _tanh_bounds(ctx, lo: Fraction, hi: Fraction):
    # Monotonicity permits saturation outside a fixed interval. This avoids
    # huge exponential endpoints for a perfectly finite tanh(large argument).
    def endpoint(x, side):
        if x >= 32:
            if side == "upper":
                return Fraction(1)
            x = Fraction(32)
        elif x <= -32:
            if side == "lower":
                return Fraction(-1)
            x = Fraction(-32)
        y = _interval(ctx, abs(x), abs(x))
        decay = ctx.exp(-2 * y)
        result = (1 - decay) / (1 + decay)
        if x < 0:
            result = -result
        lower, upper = _bounds(result)
        return lower if side == "lower" else upper
    return endpoint(lo, "lower"), endpoint(hi, "upper")


def math_func(name: str, value) -> GuardedReal:
    """Evaluate a supported real elementary function with outward enclosure."""
    x = coerce(value)
    if name not in ("sqrt", "exp", "sin", "cos", "log", "tanh"):
        raise FloatGuardError(f"Unsupported guarded mathematical function: {name}")
    if name == "sqrt" and x.lo < 0:
        raise NumericalDomainError("sqrt requires a nonnegative input enclosure")
    if name == "log" and x.lo <= 0:
        raise NumericalDomainError("log requires a strictly positive input enclosure")
    try:
        approximate = _finite(getattr(math, name)(x.value), f"math.{name} result")
        ctx = _context()
        if name == "tanh":
            lo, hi = _tanh_bounds(ctx, x.lo, x.hi)
        else:
            lo, hi = _bounds(getattr(ctx, name)(_interval(ctx, x.lo, x.hi)))
    except FloatGuardError:
        raise
    except (ValueError, ZeroDivisionError, OverflowError, TypeError) as exc:
        raise NumericalDomainError(f"math.{name} enclosure evaluation failed") from exc
    return GuardedReal(approximate, lo, hi)


@lru_cache(maxsize=2)
def constant(name: str) -> GuardedReal:
    if name not in ("pi", "e"):
        raise FloatGuardError("Only pi and e are supported mathematical constants")
    ctx = _context()
    lo, hi = _bounds(getattr(ctx, name))
    return GuardedReal(getattr(math, name), lo, hi)


def minimum(values: Iterable) -> GuardedReal:
    items = [coerce(value) for value in values]
    if not items:
        raise NumericalDomainError("min requires a nonempty finite collection")
    return GuardedReal(min(x.value for x in items), min(x.lo for x in items), min(x.hi for x in items))


def maximum(values: Iterable) -> GuardedReal:
    items = [coerce(value) for value in values]
    if not items:
        raise NumericalDomainError("max requires a nonempty finite collection")
    return GuardedReal(max(x.value for x in items), max(x.lo for x in items), max(x.hi for x in items))


def compare(op: str, a, b, margin=DEFAULT_MARGIN) -> bool:
    """Check an ordered branch with a provable gap and matching float truth.

    A sufficient gap is established from independent interval endpoints. A
    large raw float difference by itself is never accepted as evidence.
    """
    aliases = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
    op = aliases.get(op, op)
    if op not in ("lt", "le", "gt", "ge"):
        raise BranchAmbiguousError("Real branches support ordered comparisons only, not equality")
    if type(margin) is float:
        margin = str(_finite(margin, "Branch margin"))
    margin = _fraction(margin)
    if margin <= 0:
        raise BranchAmbiguousError("Branch margin must be strictly positive")
    a, b = coerce(a), coerce(b)
    difference_lo, difference_hi = a.lo - b.hi, a.hi - b.lo
    if difference_lo > margin:
        relation = 1
    elif difference_hi < -margin:
        relation = -1
    else:
        raise BranchAmbiguousError(
            "Real branch lacks the required numerical gap; "
            f"difference enclosure=[{difference_lo}, {difference_hi}], margin={margin}"
        )
    float_relation = 1 if a.value > b.value else -1 if a.value < b.value else 0
    if float_relation != relation:
        raise BranchAmbiguousError("Float comparison disagrees with the separated ideal-real branch")
    return relation < 0 if op in ("lt", "le") else relation > 0


def backend_manifest() -> dict:
    import mpmath
    return {
        "guard_backend": "exact_rational_algebra_and_private_mpmath_iv",
        "mpmath_version": mpmath.__version__,
        "interval_dps": INTERVAL_DPS,
        "endpoint_bits": MAX_ENDPOINT_BITS,
        "default_branch_margin": str(DEFAULT_MARGIN),
        "formal_rounding_proof": False,
        "branch_policy": "strict_interval_gap_and_agreement_with_float_comparison",
    }
