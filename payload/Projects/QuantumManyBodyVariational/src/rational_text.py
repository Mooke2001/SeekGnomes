"""Bounded exact rational text without changing Python's int-string policy."""
from fractions import Fraction


def integer_text(value: int) -> str:
    if type(value) is not int or abs(value).bit_length() > 262144:
        raise ValueError("Integer text exceeds output resource limit")
    if value == 0:
        return "0"
    negative = value < 0
    value = abs(value)
    chunks = []
    while value:
        value, remainder = divmod(value, 1_000_000_000)
        chunks.append(remainder)
    return ("-" if negative else "") + str(chunks[-1]) + "".join(f"{x:09d}" for x in reversed(chunks[:-1]))


def rational_text(value: Fraction | int) -> str:
    if type(value) is int:
        return integer_text(value)
    if type(value) is not Fraction:
        raise ValueError("Expected exact Fraction or int")
    numerator = integer_text(value.numerator)
    return numerator if value.denominator == 1 else numerator + "/" + integer_text(value.denominator)
