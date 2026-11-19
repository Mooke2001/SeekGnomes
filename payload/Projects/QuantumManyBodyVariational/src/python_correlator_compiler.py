"""A pure Python correlator language and its ideal real-valued Lean emitter.

The source is parsed, never imported or executed. Numerical execution follows
Python int/float operations within explicit resource limits. Lean uses
ideal real arithmetic, not the bitwise floating-point algorithm. It operates on the support
of a finitely supported Pauli word; no cutoff on the infinite lattice is used.
This compiler is trusted software, not a Lean proof of compiler correctness.
Legality and translation invariance are obligations on the generated ``f``.
"""
from __future__ import annotations

import ast
import hashlib
import math
import operator
import re
from dataclasses import dataclass
from fractions import Fraction


LANGUAGE_VERSION = "python-correlator-v1"
MAX_SOURCE_BYTES = 65_536
MAX_AST_NODES = 4_096
MAX_AST_DEPTH = 96
MAX_EXPANDED_NODES = 32_768
MAX_WORD_FACTORS = 4_096
MAX_STEPS = 1_000_000
MAX_INTEGER_BITS = 16_384
MAX_POWER = 1_024
MAX_DECIMAL_DIGITS = 4_096
MAX_DECIMAL_EXPONENT = 4_096
_BUILTINS = {"Fraction", "prod", "sum", "all", "any", "abs", "len", "sqrt"}
_PAULIS = {"X": 1, "Y": 2, "Z": 3}


class CorrelatorError(ValueError):
    """Unsupported source, invalid word, or exhausted deterministic limits."""


@dataclass(frozen=True)
class _Expr:
    kind: str
    typ: str
    args: tuple = ()
    value: object = None


def _error(node, message):
    line = getattr(node, "lineno", None)
    where = f" at line {line}" if line is not None else ""
    raise CorrelatorError(message + where)


def _numeric(expr, node):
    if expr.typ not in ("int", "rat", "real"):
        _error(node, "Expected a numeric expression")
    return expr


def _boolean(expr, node):
    if expr.typ != "bool":
        _error(node, "Expected a boolean expression; implicit truth testing is unsupported")
    return expr


def _promote(expr, typ):
    if expr.typ == typ:
        return expr
    if expr.typ == "int" and typ == "rat":
        return _Expr("cast", "rat", (expr,))
    if expr.typ in ("int", "rat") and typ == "real":
        return _Expr("cast", "real", (expr,))
    raise CorrelatorError("Internal incompatible types")


def _common(a, b, node):
    if a.typ == b.typ:
        return a, b, a.typ
    if a.typ in ("int", "rat", "real") and b.typ in ("int", "rat", "real"):
        typ = "real" if "real" in (a.typ, b.typ) else "rat"
        return _promote(a, typ), _promote(b, typ), typ
    _error(node, "Branches or operands have incompatible types")


def _bounded(value):
    if type(value) is float:
        if not math.isfinite(value):
            raise CorrelatorError("Floating arithmetic produced a nonfinite value")
        return value
    if isinstance(value, Fraction):
        bits = max(value.numerator.bit_length(), value.denominator.bit_length())
    elif type(value) is int:
        bits = value.bit_length()
    else:
        return value
    if bits > MAX_INTEGER_BITS:
        raise CorrelatorError("Exact arithmetic exceeds integer bit limit")
    return value


def _eval(expr, word, env, budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise CorrelatorError("Correlator evaluation exceeds operation limit")
    k = expr.kind
    ev = lambda e: _eval(e, word, env, budget)
    if k == "const":
        return float(expr.value) if expr.typ == "real" else expr.value
    if k == "var":
        return env[expr.value] if expr.value in env else ev(expr.args[0])
    if k == "let":
        inner = dict(env)
        inner[expr.value] = ev(expr.args[0])
        return _eval(expr.args[1], word, inner, budget)
    if k == "coord":
        return ev(expr.args[0])[expr.value]
    if k == "len":
        return len(word)
    if k == "cast":
        # int/rat -> real casts are Lean type annotations, not additional
        # Python roundings. Let Python's actual operators perform promotion.
        # This retains Python's empty-sum/product and conditional branch types.
        return _bounded(ev(expr.args[0]) if expr.typ == "real" else Fraction(ev(expr.args[0])))
    if k == "sqrt":
        return _bounded(getattr(math, k)(ev(expr.args[0])))
    if k == "if":
        return ev(expr.args[1] if ev(expr.args[0]) else expr.args[2])
    if k == "not":
        return not ev(expr.args[0])
    if k == "and":
        return all(ev(x) for x in expr.args)
    if k == "or":
        return any(ev(x) for x in expr.args)
    if k == "neg":
        return _bounded(-ev(expr.args[0]))
    if k == "abs":
        return _bounded(abs(ev(expr.args[0])))
    if k == "pow":
        return _bounded(ev(expr.args[0]) ** expr.value)
    if k in ("add", "sub", "mul", "div", "mod", "floordiv", "eq", "ne", "lt", "le", "gt", "ge"):
        operations = {"add": operator.add, "sub": operator.sub,
                      "mul": operator.mul, "div": operator.truediv,
                      "mod": operator.mod, "floordiv": operator.floordiv,
                      "eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
                      "le": operator.le, "gt": operator.gt, "ge": operator.ge}
        return _bounded(operations[k](ev(expr.args[0]), ev(expr.args[1])))
    if k == "reduce":
        operation, generators = expr.value
        def values(index=0, scope=None):
            scope = env if scope is None else scope
            if index == len(generators):
                yield _eval(expr.args[0], word, scope, budget)
                return
            names, filters = generators[index]
            for factor in word:
                inner = dict(scope)
                inner.update(zip(names, factor))
                if all(_eval(c, word, inner, budget) for c in filters):
                    yield from values(index + 1, inner)
        if operation == "sum":
            # CPython 3.12 sum has improved float summation. Invoke the actual
            # builtin, preserving the submitted Python function's behavior.
            return _bounded(sum(values()))
        if operation == "all":
            return all(values())
        if operation == "any":
            return any(values())
        result = {"sum": 0, "prod": 1, "all": True, "any": False}[operation]
        for value in values():
            result = _bounded(result * value)
        return result
    raise CorrelatorError(f"Unknown expression {k}")


def _is_static(expr, memo=None):
    memo = {} if memo is None else memo
    key = id(expr)
    if key in memo:
        return memo[key]
    if expr.kind == "var":
        result = bool(expr.args) and _is_static(expr.args[0], memo)
    elif expr.kind in ("len", "reduce", "let"):
        result = False
    else:
        result = all(_is_static(a, memo) for a in expr.args)
    memo[key] = result
    return result


def _check_expansion(expr):
    """Count a shared expression graph's expanded size without expanding it."""
    memo = {}
    def size(item):
        key = id(item)
        if key in memo:
            return memo[key]
        args = list(item.args)
        if item.kind == "reduce":
            args.extend(arg for _, filters in item.value[1] for arg in filters)
        count = 1
        for arg in args:
            count += size(arg)
            if count > MAX_EXPANDED_NODES:
                raise CorrelatorError("Expanded expression exceeds size limit")
        memo[key] = count
        return count
    size(expr)


def _static(expr, node):
    _check_expansion(expr)
    if not _is_static(expr):
        _error(node, "Expected a compile-time constant")
    return _eval(expr, (), {}, [MAX_STEPS])


def _ideal_constant(expr):
    """Exact ideal arithmetic for checking static algebraic denominators.

    Transcendental constants are not accepted as denominators in v1.
    """
    if expr.kind == "const" and expr.typ in ("int", "rat", "real"):
        return Fraction(expr.value)
    values = [_ideal_constant(e) for e in expr.args]
    if expr.kind == "cast":
        return values[0]
    if expr.kind == "var" and values:
        return values[0]
    if expr.kind == "neg":
        return -values[0]
    if expr.kind == "abs":
        return abs(values[0])
    if expr.kind == "pow":
        return _bounded(values[0] ** expr.value)
    if expr.kind in ("add", "sub", "mul", "div", "mod", "floordiv"):
        fn = {"add": operator.add, "sub": operator.sub, "mul": operator.mul,
              "div": operator.truediv, "mod": operator.mod, "floordiv": operator.floordiv}[expr.kind]
        return _bounded(fn(*values))
    raise CorrelatorError("Denominator must be a static algebraic number in v1")


class _Parser:
    def __init__(self, dimension, source):
        self.dimension = dimension
        self.source = source
        self.imports = set()
        self.counter = 0
        self.parse_steps = MAX_EXPANDED_NODES

    def tick(self):
        self.parse_steps -= 1
        if self.parse_steps < 0:
            raise CorrelatorError("Source exceeds parser expansion limit")

    def expression(self, node, env):
        self.tick()
        if isinstance(node, ast.Constant):
            if type(node.value) is bool:
                return _Expr("const", "bool", value=node.value)
            if type(node.value) is int:
                return _Expr("const", "int", value=_bounded(node.value))
            if type(node.value) is float:
                if not math.isfinite(node.value):
                    _error(node, "Nonfinite float literals are unsupported")
                literal = ast.get_source_segment(self.source, node).replace("_", "")
                # Fraction('1e-1000000000') allocates a gigantic integer before
                # a post-construction bit check can run. Bound the lexical
                # exponent and mantissa before exact-decimal interpretation.
                parts = literal.lower().split("e")
                if len(parts[0]) > MAX_DECIMAL_DIGITS:
                    _error(node, "Decimal literal exceeds digit limit")
                if len(parts) == 2:
                    exponent = parts[1].lstrip("+-").lstrip("0") or "0"
                    if len(exponent) > 4 or abs(int(parts[1])) > MAX_DECIMAL_EXPONENT:
                        _error(node, "Decimal literal exceeds exponent limit")
                return _Expr("const", "real", value=_bounded(Fraction(literal)))
            if type(node.value) is str and node.value in _PAULIS:
                return _Expr("const", "pauli", value=node.value)
            _error(node, "Only finite numeric literals, booleans, and X/Y/Z strings are supported here")
        if isinstance(node, ast.Name):
            if node.id not in env:
                _error(node, f"Unknown scalar name {node.id!r}")
            return env[node.id]
        if isinstance(node, ast.Subscript):
            value = self.expression(node.value, env)
            if value.typ != "cell":
                _error(node, "Only a lattice coordinate cell[i] may be indexed")
            index = _static(self.expression(node.slice, env), node)
            if type(index) is not int or not -self.dimension <= index < self.dimension:
                _error(node, "Coordinate index out of range")
            return _Expr("coord", "int", (value,), index % self.dimension)
        if isinstance(node, ast.UnaryOp):
            value = self.expression(node.operand, env)
            if isinstance(node.op, ast.Not):
                return _Expr("not", "bool", (_boolean(value, node),))
            _numeric(value, node)
            if isinstance(node.op, ast.USub):
                return _Expr("neg", value.typ, (value,))
            if isinstance(node.op, ast.UAdd):
                return value
            _error(node, "Unsupported unary operator")
        if isinstance(node, ast.BinOp):
            a = _numeric(self.expression(node.left, env), node)
            b = _numeric(self.expression(node.right, env), node)
            if isinstance(node.op, ast.Pow):
                exponent = _static(b, node)
                if type(exponent) is not int or not 0 <= exponent <= MAX_POWER:
                    _error(node, f"Power must be an integer between 0 and {MAX_POWER}")
                return _Expr("pow", a.typ, (a,), exponent)
            if isinstance(node.op, (ast.Mod, ast.FloorDiv)):
                divisor = _static(b, node)
                if a.typ != "int" or type(divisor) is not int or divisor <= 0:
                    _error(node, "% and // require integer operands and a positive constant divisor")
                return _Expr("mod" if isinstance(node.op, ast.Mod) else "floordiv", "int", (a, b))
            if isinstance(node.op, ast.Div):
                divisor = _static(b, node)
                if divisor == 0 or _ideal_constant(b) == 0:
                    _error(node, "Division by zero")
                typ = "rat" if "rat" in (a.typ, b.typ) and "real" not in (a.typ, b.typ) else "real"
                return _Expr("div", typ, (_promote(a, typ), _promote(b, typ)))
            a, b, typ = _common(a, b, node)
            kinds = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul"}
            kind = kinds.get(type(node.op))
            if not kind:
                _error(node, "Unsupported arithmetic operator")
            return _Expr(kind, typ, (a, b))
        if isinstance(node, ast.BoolOp):
            values = tuple(_boolean(self.expression(v, env), node) for v in node.values)
            return _Expr("and" if isinstance(node.op, ast.And) else "or", "bool", values)
        if isinstance(node, ast.Compare):
            terms = [node.left, *node.comparators]
            comparisons = []
            kinds = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}
            for a_node, b_node, op in zip(terms, terms[1:], node.ops):
                a, b, typ = _common(self.expression(a_node, env), self.expression(b_node, env), node)
                kind = kinds.get(type(op))
                if not kind or typ == "cell" or (typ in ("pauli", "bool") and kind not in ("eq", "ne")):
                    _error(node, "Unsupported comparison; compare individual coordinate components")
                if typ == "real":
                    _error(node, "Float-valued comparisons are unsupported; branch on integer geometry or Pauli labels")
                comparisons.append(_Expr(kind, "bool", (a, b)))
            return comparisons[0] if len(comparisons) == 1 else _Expr("and", "bool", tuple(comparisons))
        if isinstance(node, ast.IfExp):
            condition = _boolean(self.expression(node.test, env), node)
            a, b, typ = _common(self.expression(node.body, env), self.expression(node.orelse, env), node)
            return _Expr("if", typ, (condition, a, b))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                _error(node, "Only named pure primitives without keyword arguments are supported")
            name = node.func.id
            if name == "len" and len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == "P":
                return _Expr("len", "int")
            if name == "Fraction":
                _error(node, "Fraction source expressions are unsupported in this float interface")
            if name == "abs" and len(node.args) == 1:
                arg = _numeric(self.expression(node.args[0], env), node)
                return _Expr("abs", arg.typ, (arg,))
            if name == "sqrt" and len(node.args) == 1:
                if name not in self.imports:
                    _error(node, f"{name} requires: from math import {name}")
                arg = _numeric(self.expression(node.args[0], env), node)
                return _Expr(name, "real", (_promote(arg, "real"),))
            if name in ("sum", "prod", "all", "any"):
                if name == "prod" and "prod" not in self.imports:
                    _error(node, "prod requires: from math import prod")
                if len(node.args) != 1 or not isinstance(node.args[0], ast.GeneratorExp):
                    _error(node, f"{name} expects one generator over P")
                generator = node.args[0]
                bindings = []
                inner = dict(env)
                for g in generator.generators:
                    if g.is_async or not isinstance(g.iter, ast.Name) or g.iter.id != "P":
                        _error(g, "Generators may iterate only over finite Pauli support P")
                    if not isinstance(g.target, (ast.Tuple, ast.List)) or len(g.target.elts) != 3 or not all(isinstance(x, ast.Name) for x in g.target.elts):
                        _error(g.target, "Unpack each factor as: for cell, qubit, pauli in P")
                    names = [x.id for x in g.target.elts]
                    if len(set(names)) != 3 or any(n == "P" or n in _BUILTINS or n.startswith("__") for n in names):
                        _error(g.target, "Invalid or duplicate generator binding")
                    private = []
                    for name_, typ in zip(names, ("cell", "int", "pauli")):
                        self.counter += 1
                        ident = f"v{self.counter}"
                        private.append(ident)
                        inner[name_] = _Expr("var", typ, value=ident)
                    filters = tuple(_boolean(self.expression(c, inner), c) for c in g.ifs)
                    bindings.append((tuple(private), filters))
                elt = self.expression(generator.elt, inner)
                if name in ("all", "any"):
                    _boolean(elt, node)
                else:
                    _numeric(elt, node)
                return _Expr("reduce", elt.typ, (elt,), (name, tuple(bindings)))
            _error(node, f"Unsupported call {name!r}")
        _error(node, f"Unsupported Python syntax {type(node).__name__}")

    def block(self, statements, env):
        self.tick()
        if not statements:
            raise CorrelatorError("Every execution path in f must return a scalar")
        first, *rest = statements
        if isinstance(first, ast.Return):
            if rest:
                _error(first, "Unreachable statements after return are unsupported")
            return self.expression(first.value, env)
        if isinstance(first, ast.Assign):
            if len(first.targets) != 1 or not isinstance(first.targets[0], ast.Name):
                _error(first, "Assignments must bind one scalar name")
            name = first.targets[0].id
            if name == "P" or name in _BUILTINS or name.startswith("__"):
                _error(first, "Cannot shadow P or a pure primitive")
            inner = dict(env)
            value = self.expression(first.value, env)
            _check_expansion(value)
            self.counter += 1
            ident = f"l{self.counter}"
            # Static metadata permits denominators such as `scale = 2.0`.
            # Execution still binds eagerly and evaluates a local only once.
            metadata = (value,) if _is_static(value) else ()
            inner[name] = _Expr("var", value.typ, metadata, ident)
            body = self.block(rest, inner)
            return _Expr("let", body.typ, (value, body), ident)
        if isinstance(first, ast.If):
            condition = _boolean(self.expression(first.test, env), first)
            def continued(branch):
                # Continuation follows only fall-through paths. A branch ending
                # in return must not acquire spurious unreachable statements.
                return branch if branch and isinstance(branch[-1], ast.Return) else [*branch, *rest]
            a = self.block(continued(first.body), dict(env))
            b = self.block(continued(first.orelse), dict(env))
            a, b, typ = _common(a, b, first)
            return _Expr("if", typ, (condition, a, b))
        _error(first, "f may contain scalar assignments, if/else, and return only")

    def module(self, tree):
        env = {}
        function = None
        for statement in tree.body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and type(statement.value.value) is str:
                continue
            if isinstance(statement, ast.ImportFrom):
                allowed = {"math": {"prod", "sqrt"}}
                if function is not None or statement.level or statement.module not in allowed or any(s.name not in allowed[statement.module] or s.asname for s in statement.names):
                    _error(statement, "Only unaliased math prod/sqrt imports before f are allowed")
                self.imports.update(s.name for s in statement.names)
                continue
            if isinstance(statement, ast.Assign) and function is None:
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    _error(statement, "Top-level assignments must bind one constant")
                name = statement.targets[0].id
                if name == "P" or name == "f" or name in _BUILTINS or name.startswith("__"):
                    _error(statement, "Invalid constant name")
                value = self.expression(statement.value, env)
                _check_expansion(value)
                if not _is_static(value):
                    _error(statement, "Top-level values must be constants")
                # Keep the syntax tree so Lean retains source-decimal ideal
                # arithmetic, rather than freezing Python-rounded globals.
                _static(value, statement)
                env[name] = value
                continue
            if isinstance(statement, ast.FunctionDef) and statement.name == "f" and function is None:
                function = statement
                # Parse now: imports and globals defined later must not change
                # accepted semantics through Python's late global resolution.
                a = statement.args
                if statement.decorator_list or statement.returns or a.posonlyargs or len(a.args) != 1 or a.args[0].arg != "P" or a.args[0].annotation or a.defaults or a.kwonlyargs or a.vararg or a.kwarg or getattr(statement, "type_params", ()):
                    _error(statement, "Use exactly an undecorated, unannotated `def f(P):`")
                body = statement.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and type(body[0].value.value) is str:
                    body = body[1:]
                # Python determines local-variable scope for the whole function,
                # even when an assignment appears after an earlier reference.
                local_names = {target.id for s in ast.walk(statement)
                               if isinstance(s, ast.Assign)
                               for target in s.targets if isinstance(target, ast.Name)}
                expression = self.block(body, {k: v for k, v in env.items() if k not in local_names})
                _numeric(expression, statement)
                continue
            _error(statement, "Module must contain allowed imports, constants, and exactly one def f(P)")
        if function is None:
            raise CorrelatorError("Missing def f(P)")
        return expression


def _lean(expr, bindings):
    k = expr.kind
    rec = lambda e: _lean(e, bindings)
    if k == "const":
        if expr.typ == "bool":
            return "true" if expr.value else "false"
        if expr.typ == "pauli":
            return f"({_PAULIS[expr.value]} : LocalPauli.Pauli)"
        if expr.typ == "rat":
            f = Fraction(expr.value)
            return f"(({f.numerator} : ℚ) / ({f.denominator} : ℚ))"
        if expr.typ == "real":
            f = Fraction(expr.value)
            return f"(({f.numerator} : ℝ) / ({f.denominator} : ℝ))"
        return f"({expr.value} : ℤ)"
    if k == "var":
        return bindings[expr.value]
    if k == "let":
        inner = dict(bindings)
        inner[expr.value] = expr.value
        typ = {"int": "ℤ", "rat": "ℚ", "real": "ℝ", "bool": "Bool", "pauli": "LocalPauli.Pauli"}[expr.args[0].typ]
        return f"(let {expr.value} : {typ} := {rec(expr.args[0])}; {_lean(expr.args[1], inner)})"
    if k == "coord":
        return f"({rec(expr.args[0])} ⟨{expr.value}, by decide⟩)"
    if k == "len":
        return "(p.support.card : ℤ)"
    if k == "cast":
        return f"(({rec(expr.args[0])}) : {'ℝ' if expr.typ == 'real' else 'ℚ'})"
    if k == "sqrt":
        return f"(Real.{k} {rec(expr.args[0])})"
    if k == "if":
        return f"(if {rec(expr.args[0])} then {rec(expr.args[1])} else {rec(expr.args[2])})"
    if k == "not":
        return f"(!{rec(expr.args[0])})"
    if k in ("and", "or"):
        return "(" + (" && " if k == "and" else " || ").join(rec(e) for e in expr.args) + ")"
    if k in ("neg", "abs"):
        return f"(-{rec(expr.args[0])})" if k == "neg" else f"|{rec(expr.args[0])}|"
    if k == "pow":
        return f"({rec(expr.args[0])} ^ ({expr.value} : ℕ))"
    if k in ("add", "sub", "mul", "div", "mod", "floordiv"):
        symbol = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%", "floordiv": "/"}[k]
        return f"({rec(expr.args[0])} {symbol} {rec(expr.args[1])})"
    if k in ("eq", "ne", "lt", "le", "gt", "ge"):
        symbol = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}[k]
        return f"(decide ({rec(expr.args[0])} {symbol} {rec(expr.args[1])}))"
    if k == "reduce":
        operation, generators = expr.value
        def emit(index, scope):
            if index == len(generators):
                return _lean(expr.args[0], scope)
            names, filters = generators[index]
            # Fresh binder from the parser's globally unique numbering.
            site = "s" + names[0][1:]
            inner = dict(scope)
            inner.update({names[0]: f"{site}.1", names[1]: f"({site}.2.val : ℤ)", names[2]: f"(p {site})"})
            body = emit(index + 1, inner)
            if filters:
                guard = "(" + " && ".join(_lean(f, inner) for f in filters) + ")"
                neutral = {"sum": "0", "prod": "1", "all": "true", "any": "false"}[operation]
                body = f"(if {guard} then {body} else {neutral})"
            if operation in ("sum", "prod"):
                symbol = "∑" if operation == "sum" else "∏"
                return f"({symbol} {site} ∈ p.support, {body})"
            if operation == "all":
                return f"(decide (∀ {site} ∈ p.support, {body} = true))"
            return f"(decide (∃ {site} ∈ p.support, {body} = true))"
        return emit(0, bindings)
    raise CorrelatorError(f"Unknown Lean expression {k}")


class CorrelatorProgram:
    """Validated Python function and its generated ideal-real Lean definition."""

    def __init__(self, source, dimension, qubits_per_cell, expression):
        self.source = source
        self.dimension = dimension
        self.qubits_per_cell = qubits_per_cell
        self._expression = expression
        self.source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()

    @classmethod
    def from_source(cls, source, dimension, qubits_per_cell):
        if type(source) is not str or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise CorrelatorError("Source must be text within the source byte limit")
        if type(dimension) is not int or not 1 <= dimension <= 32:
            raise CorrelatorError("dimension must be an integer in 1..32")
        if type(qubits_per_cell) is not int or not 1 <= qubits_per_cell <= 4096:
            raise CorrelatorError("qubits_per_cell must be an integer in 1..4096")
        try:
            tree = ast.parse(source)
        except (SyntaxError, RecursionError, ValueError) as exc:
            raise CorrelatorError(f"Invalid Python source: {exc}") from exc
        nodes = list(ast.walk(tree))
        if len(nodes) > MAX_AST_NODES:
            raise CorrelatorError("Source exceeds AST node limit")
        def depth(node):
            return 1 + max((depth(c) for c in ast.iter_child_nodes(node)), default=0)
        try:
            if depth(tree) > MAX_AST_DEPTH:
                raise CorrelatorError("Source exceeds AST depth limit")
            expression = _Parser(dimension, source).module(tree)
        except RecursionError as exc:
            raise CorrelatorError("Source exceeds parser recursion limit") from exc
        except (OverflowError, ZeroDivisionError, ValueError) as exc:
            if isinstance(exc, CorrelatorError):
                raise
            raise CorrelatorError(f"Invalid constant arithmetic: {exc}") from exc
        # Local names are expanded by substitution. Bound their expanded size
        # too: a tiny sequence a=a+a can otherwise cause exponential emission.
        stack = [(expression, 1)]
        visited = 0
        while stack:
            item, level = stack.pop()
            visited += 1
            if visited > MAX_EXPANDED_NODES or level > MAX_AST_DEPTH:
                raise CorrelatorError("Expanded expression exceeds size/depth limit")
            stack.extend((arg, level + 1) for arg in item.args)
            if item.kind == "reduce":
                stack.extend((arg, level + 1) for _, filters in item.value[1] for arg in filters)
        return cls(source, dimension, qubits_per_cell, expression)

    def normalize_word(self, word):
        """Validate unique nonidentity factors and canonicalize support order."""
        if not isinstance(word, (tuple, list)) or len(word) > MAX_WORD_FACTORS:
            raise CorrelatorError("Word must be a finite tuple/list within factor limit")
        seen = set()
        factors = []
        for factor in word:
            if not isinstance(factor, (tuple, list)) or len(factor) != 3:
                raise CorrelatorError("Each factor must be (cell_tuple, qubit, pauli)")
            cell, qubit, pauli = factor
            if not isinstance(cell, (tuple, list)) or len(cell) != self.dimension or any(type(c) is not int for c in cell):
                raise CorrelatorError("Lattice cell has wrong dimension or noninteger coordinate")
            for c in cell:
                _bounded(c)
            if type(qubit) is not int or not 0 <= qubit < self.qubits_per_cell:
                raise CorrelatorError("Qubit label out of range")
            if type(pauli) is not str or pauli not in _PAULIS:
                raise CorrelatorError("Factors must be nonidentity Pauli X, Y, or Z")
            site = (tuple(cell), qubit)
            if site in seen:
                raise CorrelatorError("Duplicate site in Pauli word")
            seen.add(site)
            factors.append((*site, pauli))
        return tuple(sorted(factors))

    def evaluate(self, word):
        word = self.normalize_word(word)
        try:
            return _bounded(float(_eval(self._expression, word, {}, [MAX_STEPS])))
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            raise CorrelatorError(f"Correlator numerical evaluation failed: {exc}") from exc

    def lean_definition(self, namespace="PythonCorrelator"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", namespace):
            raise CorrelatorError("Invalid Lean namespace")
        body = _lean(_promote(self._expression, "real"), {})
        return (
            "module\n\npublic import QuantumManyBodyVariational.TrustedStateSpecV2\n\n"
            "@[expose] public section\nnoncomputable section\n"
            f"namespace {namespace}\n"
            "open scoped BigOperators\n"
            "open QuantumManyBodyVariational.InfiniteState\n"
            "open QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2\n"
            f"-- language: {LANGUAGE_VERSION}; Python source SHA256: {self.source_sha256}\n"
            f"def fReal (p : Word (Site {self.dimension} {self.qubits_per_cell})) : ℝ := by\n"
            "  classical\n"
            f"  exact {body}\n\n"
            f"def f : Correlator (Site {self.dimension} {self.qubits_per_cell}) :=\n"
            "  fun p => fReal p\n\n"
            "/-- The LLM must prove this exact proposition for the generated f. -/\n"
            f"def ProofObligation : Prop := StateCondition {self.dimension} {self.qubits_per_cell} f\n"
            f"end {namespace}\n"
        )

    def manifest(self):
        lean = self.lean_definition()
        return {
            "language_version": LANGUAGE_VERSION,
            "python_source_sha256": self.source_sha256,
            "lean_definition_sha256": hashlib.sha256(lean.encode("utf-8")).hexdigest(),
            "dimension": self.dimension,
            "qubits_per_cell": self.qubits_per_cell,
            "number_semantics": "Python_float_approximation_to_Lean_ideal_real_expression",
            "lean_literal_semantics": "exact_source_decimal_as_real_rational",
            "numeric_equivalence": "not_bitwise_equivalence_or_certified_error_bound",
            "state_condition": "assumed_proved_externally_for_generated_f",
            "compiler_correctness": "trusted_implementation_not_formally_proved",
            "limits": {"source_bytes": MAX_SOURCE_BYTES, "ast_nodes": MAX_AST_NODES,
                       "ast_depth": MAX_AST_DEPTH, "expanded_nodes": MAX_EXPANDED_NODES,
                       "word_factors": MAX_WORD_FACTORS,
                       "evaluation_steps": MAX_STEPS, "integer_bits": MAX_INTEGER_BITS,
                       "constant_power": MAX_POWER, "decimal_digits": MAX_DECIMAL_DIGITS,
                       "decimal_exponent": MAX_DECIMAL_EXPONENT},
        }
