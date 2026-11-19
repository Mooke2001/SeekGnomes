"""Shared typed terms for correlators, independent of either backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


Type: TypeAlias = str | tuple["Type", ...]
LANGUAGE_VERSION = "state-format-v1"


class FormatError(ValueError):
    """Invalid source, unsupported operation, or undefined evaluation."""


@dataclass(frozen=True, slots=True)
class Expr:
    op: str
    type: Type
    args: tuple[Expr, ...] = ()
    value: object = None


@dataclass(frozen=True, slots=True)
class Function:
    name: str
    params: tuple[tuple[str, Type], ...]
    body: Expr
    returns: Type


@dataclass(frozen=True, slots=True)
class Program:
    functions: tuple[Function, ...]
    dimension: int
    qubits_per_cell: int
    bits: int = 40
    terms: int = 32
    limit: tuple[str, str] | None = None
