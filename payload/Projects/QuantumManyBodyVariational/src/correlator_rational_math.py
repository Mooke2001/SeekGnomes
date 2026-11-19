"""Deterministic finite rational approximations with a matching Lean algorithm.

No transcendental oracle, floating point, interval backend, or external state
is used. The returned Fraction is the *definition* of the approximation. In
particular, replacing a real function by these rational programs changes the
candidate correlator, whose legality must be proved for that new definition.

The finite formulas are mirrored by QuantumManyBodyVariational.RationalMath.
There is no claim of a Lean-certified or uniform numerical error bound. The
default formulas are accurate on moderate arguments, while bounded range
reduction may lose accuracy outside the ranges described by manifest().
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import math


ALGORITHM_VERSION = "finite-rational-math-v1"
DEFAULT_PRECISION_BITS = 40
DEFAULT_TERMS = 32
EXP_REDUCTION_LEVELS = 8
LOG_REDUCTION_LEVELS = 16
MAX_INPUT_BITS = 4096
MAX_WORK_BITS = 262144


class RationalMathError(ValueError):
    """Invalid rational input, unsupported domain, or exhausted resource cap."""


def _size(value: Fraction) -> int:
    return max(abs(value.numerator).bit_length(), value.denominator.bit_length())


def _checked(value: Fraction, label="Rational approximation") -> Fraction:
    if _size(value) > MAX_WORK_BITS:
        raise RationalMathError(f"{label} exceeds the {MAX_WORK_BITS}-bit work limit")
    return value


def as_rational(value) -> Fraction:
    """Parse exact numeric input, never accepting an already rounded float."""
    if type(value) is Fraction:
        result = value
    elif type(value) is int:
        result = Fraction(value)
    elif type(value) is str:
        if not value or len(value) > 4096:
            raise RationalMathError("Rational text is empty or too long")
        parts = value.lower().split("e")
        if len(parts) > 2:
            raise RationalMathError("Invalid rational numeric text")
        if len(parts) == 2:
            exp = parts[1].lstrip("+-").lstrip("0") or "0"
            if len(exp) > 5 or abs(int(parts[1])) > 16384:
                raise RationalMathError("Decimal exponent exceeds resource limit")
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            raise RationalMathError("Invalid rational numeric text") from exc
    else:
        raise RationalMathError("Arguments must be int, Fraction, or exact decimal/rational text, not float")
    if _size(result) > MAX_INPUT_BITS:
        raise RationalMathError(f"Argument exceeds the {MAX_INPUT_BITS}-bit input limit")
    return result


def _pow(value: Fraction, exponent: int) -> Fraction:
    if type(exponent) is not int or exponent < 0:
        raise RationalMathError("Internal rational powers require nonnegative integers")
    if _size(value) * exponent > MAX_WORK_BITS:
        raise RationalMathError("Rational power exceeds the work limit")
    return _checked(value ** exponent)


@dataclass(frozen=True, init=False)
class RationalMath:
    precision_bits: int = DEFAULT_PRECISION_BITS
    terms: int = DEFAULT_TERMS

    def __init__(self, precision_bits=DEFAULT_PRECISION_BITS, terms=DEFAULT_TERMS, *, bits=None):
        if bits is not None:
            if precision_bits != DEFAULT_PRECISION_BITS and precision_bits != bits:
                raise RationalMathError("bits and precision_bits specify conflicting values")
            precision_bits = bits
        object.__setattr__(self, "precision_bits", precision_bits)
        object.__setattr__(self, "terms", terms)
        self.__post_init__()

    @property
    def bits(self):
        return self.precision_bits

    def __post_init__(self):
        if type(self.precision_bits) is not int or not 8 <= self.precision_bits <= 80:
            raise RationalMathError("precision_bits must be an integer in 8..80")
        if type(self.terms) is not int or not 4 <= self.terms <= 64:
            raise RationalMathError("terms must be an integer in 4..64")

    def _sqrt(self, x: Fraction) -> Fraction:
        if x < 0:
            raise RationalMathError("sqrt approximation requires a nonnegative argument")
        scale = 1 << self.precision_bits
        squared_scaled = (x.numerator << (2 * self.precision_bits)) // x.denominator
        return Fraction(math.isqrt(squared_scaled), scale)

    def _exp(self, x: Fraction) -> Fraction:
        a = abs(x)
        reductions = sum(a > Fraction(1 << i, 2) for i in range(EXP_REDUCTION_LEVELS))
        y = a / (1 << reductions)
        term = Fraction(1)
        result = Fraction(1)
        for k in range(1, self.terms):
            term = _checked(term * y / k)
            result = _checked(result + term)
        result = _pow(result, 1 << reductions)
        # All Taylor terms above are nonnegative, including term_0 = 1.
        return Fraction(1, 1) / result if x < 0 else result

    @lru_cache(maxsize=64)
    def _pi(self) -> Fraction:
        def atan_reciprocal(base: int):
            return sum((Fraction(1 if k % 2 == 0 else -1,
                                 (2*k+1) * base**(2*k+1))
                        for k in range(self.terms)), Fraction(0))
        return _checked(16 * atan_reciprocal(5) - 4 * atan_reciprocal(239))

    def _reduce_trig(self, x: Fraction) -> Fraction:
        pi = self._pi()
        period = 2*pi
        quotient = (x + pi) / period
        winding = quotient.numerator // quotient.denominator
        return _checked(x - period * winding)

    def _sin(self, x: Fraction) -> Fraction:
        y = self._reduce_trig(x)
        square = _checked(y*y)
        term, result = y, y
        for k in range(1, self.terms):
            term = _checked(-term * square / ((2*k)*(2*k+1)))
            result = _checked(result + term)
        return result

    def _cos(self, x: Fraction) -> Fraction:
        y = self._reduce_trig(x)
        square = _checked(y*y)
        term = result = Fraction(1)
        for k in range(1, self.terms):
            term = _checked(-term * square / ((2*k-1)*(2*k)))
            result = _checked(result + term)
        return result

    def _log_series(self, z: Fraction) -> Fraction:
        square = _checked(z*z)
        power, result = z, Fraction(0)
        for k in range(self.terms):
            result = _checked(result + power / (2*k+1))
            power = _checked(power * square)
        return _checked(2*result)

    def _log(self, x: Fraction) -> Fraction:
        if x <= 0:
            raise RationalMathError("log approximation requires a strictly positive argument")
        positive = sum(x >= (1 << (i+1)) for i in range(LOG_REDUCTION_LEVELS))
        negative = sum(x < Fraction(1, 1 << i) for i in range(LOG_REDUCTION_LEVELS))
        exponent = positive - negative
        normalized = x / (1 << exponent) if exponent >= 0 else x * (1 << -exponent)
        z = (normalized - 1) / (normalized + 1)
        return _checked(self._log_series(z) + exponent * self._log_series(Fraction(1,3)))

    def _tanh(self, x: Fraction) -> Fraction:
        exponential = self._exp(2*x)
        return _checked((exponential-1)/(exponential+1))

    def approx(self, name: str, x) -> Fraction:
        if name not in ("sqrt", "exp", "sin", "cos", "log", "tanh"):
            raise RationalMathError(f"Unsupported rational mathematical function: {name}")
        exact = as_rational(x)
        return _checked(getattr(self, "_"+name)(exact))

    def constant(self, name: str) -> Fraction:
        if name == "pi":
            return self._pi()
        if name == "e":
            return self._exp(Fraction(1))
        raise RationalMathError("Only pi and e are supported rational constants")

    def manifest(self) -> dict:
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "number_semantics": "exact_finite_rational_program",
            "precision_bits": self.precision_bits,
            "series_terms": self.terms,
            "exp_reduction_levels": EXP_REDUCTION_LEVELS,
            "log_reduction_levels": LOG_REDUCTION_LEVELS,
            "sqrt": "isqrt(floor(x*2^(2*bits)))/2^bits",
            "exp": "Taylor sum k<terms on abs(x)/2^s; power 2^s; reciprocal for negative x",
            "pi": "16*atan_series(1/5)-4*atan_series(1/239), each with terms finite terms",
            "sin_cos": "Taylor sum k<terms after exact rational pi range reduction",
            "log": "bounded power-two normalization and 2*atanh_series, including rational log(2)",
            "tanh": "(qexp(2*x)-1)/(qexp(2*x)+1)",
            "certified_error_bound": False,
            "accuracy_scope": {
                "exp": "range reduction reaches abs(y)<=1/2 when abs(x)<=128",
                "log": "normalization reaches [1,2) when 2^-16<=x<=2^16",
                "sin_cos": "phase error from rational pi may accumulate for large abs(x)",
                "sqrt": "downward dyadic quantization; exact mathematical error is less than 2^-bits",
            },
            "limits": {"input_bits": MAX_INPUT_BITS, "work_bits": MAX_WORK_BITS,
                       "precision_bits": [8,80], "series_terms": [4,64]},
            "state_legality": "must_be_proved_for_the_approximated_correlator",
        }


def approx(name: str, x, *, precision_bits=DEFAULT_PRECISION_BITS, terms=DEFAULT_TERMS) -> Fraction:
    return RationalMath(precision_bits=precision_bits, terms=terms).approx(name,x)


def constant(name: str, *, precision_bits=DEFAULT_PRECISION_BITS, terms=DEFAULT_TERMS) -> Fraction:
    return RationalMath(precision_bits=precision_bits, terms=terms).constant(name)
