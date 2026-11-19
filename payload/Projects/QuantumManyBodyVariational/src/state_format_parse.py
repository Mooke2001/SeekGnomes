"""Type-check the common correlator source without executing its Python syntax."""

from __future__ import annotations

import ast
from collections import deque
from fractions import Fraction
import re

from .correlator_energy import MAX_DIMENSION, MAX_QUBITS_PER_CELL
from .correlator_hybrid_branch import MAX_RATIONAL_BITS
from .correlator_rational_math import RationalMath, RationalMathError
from .state_format_ir import Expr, FormatError, Function, Program, Type


_SCALARS = frozenset({"Nat", "Int", "Rat", "Real", "Bool", "Pauli", "Word", "Factor", "Cell"})
_TYPE_ALIASES: dict[str, Type] = {
    "Complex": ("Tuple", "Real", "Real"),
    "Vector": ("List", "Real"),
    "Matrix": ("List", ("List", "Real")),
}
_RANK = {"Nat": 0, "Int": 1, "Rat": 2, "Real": 3}
_PAULIS = frozenset("IXYZ")
_MATH = frozenset({"sqrt", "exp", "sin", "cos", "log", "tanh"})
_FIELDS = {"cell": "Cell", "qubit": "Int", "pauli": "Pauli"}
_EXACT_EQUALITY = frozenset({"Nat", "Int", "Rat", "Bool", "Pauli", "Word", "Factor", "Cell"})
_SIGNATURES: dict[str, tuple[tuple[Type, ...], Type]] = {
    "factors": (("Word",), ("List", "Factor")),
    "labels": (("Word",), ("List", "Pauli")),
    "cell": (("Factor",), "Cell"),
    "qubit": (("Factor",), "Int"),
    "pauli": (("Factor",), "Pauli"),
    "pauli_at": (("Word", "Cell", "Int"), "Pauli"),
    "translate": (("Word", "Cell"), "Word"),
    "make_cell": ((("List", "Int"),), "Cell"),
    "factor": (("Cell", "Int", "Pauli"), "Factor"),
    "word_from_factors": ((("List", "Factor"),), "Word"),
}
_CASTS = {"to_int": "Int", "to_nat": "Nat", "to_rat": "Rat", "to_real": "Real"}
_ALIASES = {**{typ: op for op, typ in _CASTS.items()}, "Cell": "make_cell"}
_AGGREGATES = frozenset({"sum", "prod", "all", "any"})
_PRIMITIVES = (
    _SCALARS | _PAULIS | _MATH | _FIELDS.keys() | _SIGNATURES.keys() | _CASTS.keys()
    | _AGGREGATES | _TYPE_ALIASES.keys() | {"abs", "min", "max", "pi", "e", "len", "range", "map", "filter", "fold",
                                            "flat_map", "zip", "concat", "require", "limit", "contains", "lookup",
                                            "List", "Tuple", "Fn"}
)
_COMPARE = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}
_BINARY = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div",
           ast.FloorDiv: "floordiv", ast.Mod: "mod", ast.Pow: "pow"}

_Env = dict[str, Expr]
_Binding = tuple[str, Expr]


def _fail(node: ast.AST, reason: str):
    where = f" at line {node.lineno}, column {node.col_offset + 1}" if hasattr(node, "lineno") else ""
    raise FormatError(reason + where)


def _identifier(node: ast.AST, name: str, *, local=False):
    reserved = name in _PRIMITIVES and (not local or name not in _FIELDS)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is None or reserved:
        _fail(node, f"Invalid or reserved identifier {name!r}")


def _family(typ: Type, tag: str) -> bool:
    return isinstance(typ, tuple) and typ[0] == tag


def _has_exact_equality(typ: Type) -> bool:
    if isinstance(typ, str):
        return typ in _EXACT_EQUALITY
    return typ[0] in ("List", "Tuple") and all(_has_exact_equality(t) for t in typ[1:])


def _annotation(node: ast.AST | None, owner: ast.AST) -> Type:
    if isinstance(node, ast.Name):
        if node.id in _SCALARS:
            return node.id
        if node.id in _TYPE_ALIASES:
            return _TYPE_ALIASES[node.id]
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        tag = node.value.id
        inner = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if (tag == "List" and len(inner) == 1) or (tag in ("Tuple", "Fn") and len(inner) >= 2):
            return (tag, *(_annotation(x, node) for x in inner))
    _fail(owner, "Expected a type annotation such as Int, Real, List[Int], Tuple[Real, Int] or Fn[Int, Real]")


def _literal(typ: Type, value) -> Expr:
    return Expr("literal", typ, (), value)


def _builtin(name: str, typ: Type, *args: Expr) -> Expr:
    return Expr("builtin", typ, args, name)


def _var(name: str, typ: Type) -> Expr:
    return Expr("var", typ, (), name)


def _tuple_expr(values: tuple[Expr, ...]) -> Expr:
    return Expr("tuple", ("Tuple", *(e.type for e in values)), values)


def _wrap(bindings: list[_Binding], body: Expr) -> Expr:
    for name, value in reversed(bindings):
        body = Expr("let", body.type, (value, body), name)
    return body


def _coerce(value: Expr, target: Type, where: ast.AST) -> Expr:
    if value.type == target:
        return value
    if value.type == "Int" and target == "Nat":
        if value.op == "literal":
            if value.value < 0:
                _fail(where, "A natural number must be nonnegative")
            return _literal("Nat", value.value)
        return _builtin("to_nat", "Nat", value)
    if value.type in _RANK and target in _RANK and _RANK[value.type] < _RANK[target]:
        return _builtin("to_" + target.lower(), target, value)
    _fail(where, f"Expected {target}, got {value.type}")


def _numeric(value: Expr, where: ast.AST) -> Expr:
    if value.type not in _RANK:
        _fail(where, f"Expected a number, got {value.type}")
    return value


def _integers(value: Expr, where: ast.AST) -> Expr:
    if value.type not in ("Int", "Nat"):
        _fail(where, f"Expected an integer, got {value.type}")
    return value


def _common(a: Expr, b: Expr, where: ast.AST) -> tuple[Expr, Expr, Type]:
    if a.type == b.type:
        return a, b, a.type
    if a.type in _RANK and b.type in _RANK:
        typ = max((a.type, b.type), key=_RANK.__getitem__)
        return _coerce(a, typ, where), _coerce(b, typ, where), typ
    _fail(where, f"Incompatible types {a.type} and {b.type}")


class _Assigned(ast.NodeVisitor):
    def __init__(self):
        self.names: dict[str, None] = {}

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.names.setdefault(node.id, None)

    def visit_For(self, node):
        for statement in node.body:
            self.visit(statement)

    def visit_comprehension(self, node):
        return

    def visit_FunctionDef(self, node):
        return


def _assigned(statements: list[ast.stmt]) -> dict[str, None]:
    visitor = _Assigned()
    for statement in statements:
        visitor.visit(statement)
    return visitor.names


def _returns(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, ast.Return):
            return True
        if isinstance(statement, ast.If) and _returns(statement.body) and _returns(statement.orelse):
            return True
        if isinstance(statement, ast.Match) and all(_returns(case.body) for case in statement.cases):
            return True
    return False


class _Parser:
    def __init__(self, source: str):
        # AST columns are UTF-8 byte offsets. Unlike str.splitlines(), bytes
        # splitlines does not mistake a Unicode separator in a string for a
        # source line break.
        self.lines = source.encode("utf-8").splitlines()
        self.signatures: dict[str, tuple[tuple[tuple[str, Type], ...], Type]] = {}
        self.constants: set[str] = set()
        self.dependencies: dict[str, dict[str, None]] = {}
        self.current = ""
        self.counter = 0

    def _fresh(self) -> str:
        self.counter += 1
        return f"slot{self.counter}"

    def module(self, tree: ast.Module) -> tuple[tuple[Function, ...], tuple[str, str] | None]:
        declarations: dict[str, ast.FunctionDef | ast.AnnAssign] = {}
        for position, node in enumerate(tree.body):
            if position == 0 and self._docstring(node):
                continue
            if isinstance(node, ast.FunctionDef):
                args = node.args
                if node.decorator_list or getattr(node, "type_params", []) or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.defaults:
                    _fail(node, "Use plain typed functions with fixed positional parameters")
                names = []
                for param in args.args:
                    _identifier(param, param.arg, local=True)
                    if any(name == param.arg for name, _ in names):
                        _fail(param, f"Duplicate parameter {param.arg}")
                    names.append((param.arg, _annotation(param.annotation, param)))
                name, signature = node.name, (tuple(names), _annotation(node.returns, node))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                name, signature = node.target.id, ((), _annotation(node.annotation, node))
                self.constants.add(name)
            else:
                _fail(node, "A module may contain only typed definitions and typed constants")
            _identifier(node, name)
            if name in declarations:
                _fail(node, f"Duplicate definition {name}")
            declarations[name] = node
            self.signatures[name] = signature
            self.dependencies[name] = {}
        if "f" not in self.signatures:
            _fail(tree, "Define an entry function f(P: Word) -> Real")
        params, typ = self.signatures["f"]
        if len(params) != 1 or params[0][1] != "Word" or typ != "Real":
            _fail(declarations["f"], "The entry function signature must be f(P: Word) -> Real")
        limit = self._limit(declarations["f"], params[0][0])
        if limit is not None:
            del declarations["f"]
            del self.signatures["f"]
            del self.dependencies["f"]

        compiled = {}
        for name, node in declarations.items():
            self.current = name
            params, typ = self.signatures[name]
            env = {local: _var(self._fresh(), parameter_type) for local, parameter_type in params}
            if isinstance(node, ast.FunctionDef):
                body = node.body[1:] if node.body and self._docstring(node.body[0]) else node.body
                value = self._block(body, env, typ)
            else:
                value = self.expr(node.value, env, typ)
            compiled[name] = Function(name, tuple((env[local].value, t) for local, t in params), value, typ)
        return self._order(compiled, declarations), limit

    @staticmethod
    def _docstring(node: ast.AST) -> bool:
        return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)

    def _limit(self, node: ast.FunctionDef, word_parameter: str) -> tuple[str, str] | None:
        body = node.body[1:] if node.body and self._docstring(node.body[0]) else node.body
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            return None
        call = body[0].value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "limit":
            return None
        if call.keywords or len(call.args) != 3 or not all(isinstance(arg, ast.Name) for arg in call.args):
            _fail(call, "Use return limit(approximation, cutoff, P) with two named helper definitions")
        approximation, cutoff, word = (arg.id for arg in call.args)
        if word != word_parameter:
            _fail(call, "The last argument of limit must be f's input word")
        for name, result in ((approximation, "Real"), (cutoff, "Nat")):
            signature = self.signatures.get(name)
            if signature is None or tuple(typ for _, typ in signature[0]) != ("Word", "Nat") or signature[1] != result:
                _fail(call, f"{name} must be a helper with signature (Word, Nat) -> {result}")
        return approximation, cutoff

    def _order(self, functions, declarations) -> tuple[Function, ...]:
        dependents = {name: [] for name in functions}
        pending = {name: len(self.dependencies[name]) for name in functions}
        for name, dependencies in self.dependencies.items():
            for required in dependencies:
                dependents[required].append(name)
        queue = deque(name for name in functions if pending[name] == 0)
        result = []
        while queue:
            name = queue.popleft()
            result.append(functions[name])
            for caller in dependents[name]:
                pending[caller] -= 1
                if pending[caller] == 0:
                    queue.append(caller)
        if len(result) != len(functions):
            recursive = next(name for name, count in pending.items() if count)
            _fail(declarations[recursive], f"Definition {recursive} depends on a recursive call cycle")
        return tuple(result)

    def _read_name(self, node, env: _Env) -> Expr:
        if node.id in env:
            return env[node.id]
        if node.id in _PAULIS:
            return _literal("Pauli", node.id)
        if node.id in ("pi", "e"):
            return _builtin(node.id, "Real")
        if node.id in self.constants:
            return self._user_call(node.id, (), node)
        if node.id in self.signatures:
            params, result = self.signatures[node.id]
            if not params:
                _fail(node, "Call a helper without parameters using ()")
            formals = tuple(self._fresh() for _ in params)
            arguments = tuple(_var(name, param[1]) for name, param in zip(formals, params))
            body = self._user_call(node.id, arguments, node)
            return Expr("lambda", ("Fn", *(p[1] for p in params), result), (body,), formals)
        _fail(node, f"Unknown local name {node.id}")

    def _user_call(self, name: str, values: tuple[Expr, ...], node: ast.AST) -> Expr:
        parameters, result = self.signatures[name]
        if len(values) != len(parameters):
            _fail(node, f"{name} takes {len(parameters)} arguments")
        self.dependencies[self.current].setdefault(name, None)
        args = tuple(_coerce(value, kind, node) for value, (_, kind) in zip(values, parameters))
        return Expr("call", result, args, name)

    def _constant(self, node: ast.Constant) -> Expr:
        value = node.value
        if type(value) is bool:
            return _literal("Bool", value)
        if type(value) is int:
            if value.bit_length() > MAX_RATIONAL_BITS:
                _fail(node, f"Integer literal exceeds {MAX_RATIONAL_BITS} bits")
            return _literal("Int", value)
        if type(value) is float:
            spelling = self.lines[node.lineno - 1][node.col_offset:node.end_col_offset].decode("utf-8")
            # Parse the written decimal directly, including trailing zeros
            # and scientific notation, rather than Python's binary float.
            if not re.fullmatch(r"[0-9_.]+(?:[eE][+-]?[0-9_]{1,4})?", spelling):
                _fail(node, "Expected a finite decimal literal")
            try:
                rational = Fraction(spelling.replace("_", ""))
            except ValueError as exc:
                _fail(node, f"Invalid decimal literal: {exc}")
            if max(rational.numerator.bit_length(), rational.denominator.bit_length()) > MAX_RATIONAL_BITS:
                _fail(node, f"Decimal literal exceeds {MAX_RATIONAL_BITS} exact bits")
            return _literal("Rat", rational)
        if type(value) is str and value in _PAULIS:
            return _literal("Pauli", value)
        _fail(node, "Only signed integer, decimal, boolean and Pauli literals are supported")

    def expr(self, node: ast.AST, env: _Env, expected: Type | None = None) -> Expr:
        value = self._expr(node, env, expected)
        return value if expected is None else _coerce(value, expected, node)

    def _expr(self, node: ast.AST, env: _Env, expected: Type | None) -> Expr:
        if isinstance(node, ast.Constant):
            return self._constant(node)
        if isinstance(node, ast.Name):
            return self._read_name(node, env)
        if isinstance(node, ast.Tuple):
            types = expected[1:] if expected is not None and _family(expected, "Tuple") else [None] * len(node.elts)
            if len(types) != len(node.elts) or len(node.elts) < 2:
                _fail(node, "Expected a tuple of at least two elements, with the declared arity")
            return _tuple_expr(tuple(self.expr(e, env, t) for e, t in zip(node.elts, types)))
        if isinstance(node, ast.List):
            return self._list(node, env, expected)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            element = expected[1] if expected is not None and _family(expected, "List") else None
            return self._comprehension(node, env, element, 0)
        if isinstance(node, ast.IfExp):
            test = self.expr(node.test, env, "Bool")
            left, right, typ = _common(self.expr(node.body, env, expected), self.expr(node.orelse, env, expected), node)
            return Expr("if", typ, (test, left, right))
        if isinstance(node, ast.BoolOp):
            op = "and" if isinstance(node.op, ast.And) else "or"
            result = self.expr(node.values[-1], env, "Bool")
            for operand in reversed(node.values[:-1]):
                result = _builtin(op, "Bool", self.expr(operand, env, "Bool"), result)
            return result
        if isinstance(node, ast.UnaryOp):
            return self._unary(node, env)
        if isinstance(node, ast.BinOp):
            return self._binary(node, self.expr(node.left, env), self.expr(node.right, env))
        if isinstance(node, ast.Compare):
            return self._compare_chain(node, env, self.expr(node.left, env), 0)
        if isinstance(node, ast.Attribute):
            if node.attr not in _FIELDS:
                _fail(node, f"Unknown Factor field {node.attr!r}")
            return _builtin(node.attr, _FIELDS[node.attr], self.expr(node.value, env, "Factor"))
        if isinstance(node, ast.Subscript):
            return self._item(node, self.expr(node.value, env), self.expr(node.slice, env, "Int"))
        if isinstance(node, ast.Call):
            return self._call(node, env, expected)
        if isinstance(node, ast.Lambda) and expected is not None and _family(expected, "Fn"):
            return self._function_arg(node, env, expected[1:-1], expected[-1])
        _fail(node, f"Unsupported expression {type(node).__name__}")

    def _list(self, node: ast.List, env: _Env, expected: Type | None) -> Expr:
        element = expected[1] if expected is not None and _family(expected, "List") else None
        values = tuple(self.expr(e, env, element) for e in node.elts)
        if not values and element is None:
            _fail(node, "An empty list needs an expected type such as List[Int]")
        if element is None:
            element = values[0].type
            for value in values[1:]:
                if element == value.type:
                    continue
                if element in _RANK and value.type in _RANK:
                    element = max((element, value.type), key=_RANK.__getitem__)
                else:
                    _fail(node, f"Incompatible list element types {element} and {value.type}")
        return Expr("list", ("List", element), tuple(_coerce(e, element, node) for e in values))

    def _unary(self, node, env):
        if isinstance(node.op, ast.Not):
            return _builtin("not", "Bool", self.expr(node.operand, env, "Bool"))
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            _fail(node, "Supported unary operators are +, - and not")
        value = _numeric(self.expr(node.operand, env), node)
        if isinstance(node.op, ast.UAdd):
            return value
        if value.type == "Nat":
            value = _coerce(value, "Int", node)
        if value.op == "literal":
            return _literal(value.type, -value.value)
        return _builtin("neg", value.type, value)

    def _binary(self, node, a, b):
        op = _BINARY.get(type(node.op))
        if op is None:
            _fail(node, f"Unsupported binary operator {type(node.op).__name__}")
        if op == "add" and _family(a.type, "List") and a.type == b.type:
            return _builtin("concat", a.type, a, b)
        if op in ("floordiv", "mod"):
            return _builtin(op, "Int", _coerce(_integers(a, node), "Int", node), _coerce(_integers(b, node), "Int", node))
        _numeric(a, node)
        _numeric(b, node)
        if op == "pow":
            _integers(b, node)
            if b.type == "Int" and b.op == "literal" and b.value < 0:
                base = _coerce(a, "Rat", node) if a.type in ("Nat", "Int") else a
                power = _builtin(op, base.type, base, _literal("Nat", -b.value))
                return _builtin("div", base.type, _coerce(_literal("Int", 1), base.type, node), power)
            return _builtin(op, a.type, a, self._cast("to_nat", b, node))
        a, b, typ = _common(a, b, node)
        if op == "div" and typ in ("Nat", "Int"):
            typ = "Rat"
        if op == "sub" and typ == "Nat":
            typ = "Int"
        return _builtin(op, typ, _coerce(a, typ, node), _coerce(b, typ, node))

    def _compare(self, node, op, left, right):
        if op is None:
            _fail(node, "Use ==, !=, <, <=, > or >= for comparisons")
        left, right, typ = _common(left, right, node)
        if typ not in _RANK and not (op in ("eq", "ne") and _has_exact_equality(typ)):
            _fail(node, f"Comparison {op} is not defined on {typ}")
        return _builtin(op, "Bool", left, right)

    def _compare_chain(self, node, env, left, index):
        right = self.expr(node.comparators[index], env)
        op = _COMPARE.get(type(node.ops[index]))
        if index + 1 == len(node.ops):
            return self._compare(node, op, left, right)
        lhs = _var(self._fresh(), left.type)
        rhs = _var(self._fresh(), right.type)
        body = _builtin("and", "Bool", self._compare(node, op, lhs, rhs), self._compare_chain(node, env, rhs, index + 1))
        return _wrap([(lhs.value, left), (rhs.value, right)], body)

    def _item(self, node, sequence, index):
        if sequence.type == "Word":
            sequence = _builtin("factors", ("List", "Factor"), sequence)
        if sequence.type == "Cell":
            item_type = "Int"
        elif _family(sequence.type, "List"):
            item_type = sequence.type[1]
        elif _family(sequence.type, "Tuple"):
            if index.op != "literal" or not 0 <= index.value < len(sequence.type) - 1:
                _fail(node, "Tuple indices must be in-range nonnegative integer literals")
            item_type = sequence.type[index.value + 1]
        else:
            _fail(node, f"Cannot index {sequence.type}")
        return _builtin("get", item_type, sequence, index)

    @staticmethod
    def _cast(name, value, node):
        return _coerce(value, _CASTS[name], node)

    @staticmethod
    def _arity(node, name, *choices):
        if len(node.args) not in choices:
            allowed = " or ".join(str(n) for n in choices)
            _fail(node, f"{name} expects {allowed} arguments")

    def _sequence(self, node, env, element=None):
        expected = ("List", element) if element is not None and isinstance(node, (ast.List, ast.ListComp, ast.GeneratorExp)) else None
        value = self.expr(node, env, expected)
        if value.type == "Word":
            value = _builtin("factors", ("List", "Factor"), value)
        if not _family(value.type, "List"):
            _fail(node, f"Expected a finite list, got {value.type}")
        return value

    def _call(self, node, env, expected):
        if not isinstance(node.func, ast.Name) or node.keywords:
            _fail(node, "Only named positional function calls are allowed")
        name = _ALIASES.get(node.func.id, node.func.id)
        if name in env:
            _fail(node, "Anonymous functions are evaluated by finite sequence operators such as map and fold")
        if name in self.signatures and name not in self.constants:
            params, _ = self.signatures[name]
            self._arity(node, name, len(params))
            args = tuple(self.expr(arg, env, typ) for arg, (_, typ) in zip(node.args, params))
            return self._user_call(name, args, node)
        if name in _SIGNATURES:
            types, result = _SIGNATURES[name]
            self._arity(node, name, len(types))
            return _builtin(name, result, *(self.expr(arg, env, typ) for arg, typ in zip(node.args, types)))
        if name in _CASTS:
            self._arity(node, name, 1)
            return self._cast(name, self.expr(node.args[0], env), node)
        if name == "require":
            self._arity(node, name, 2)
            test = self.expr(node.args[0], env, "Bool")
            value = self.expr(node.args[1], env, expected)
            return _builtin(name, value.type, test, value)
        if name == "limit":
            _fail(node, "Use limit as the entire return expression of f")
        if name in _MATH:
            self._arity(node, name, 1)
            return _builtin(name, "Real", self.expr(node.args[0], env, "Real"))
        if name in ("pi", "e"):
            self._arity(node, name, 0)
            return _builtin(name, "Real")
        if name == "abs":
            self._arity(node, name, 1)
            value = _numeric(self.expr(node.args[0], env), node)
            return value if value.type == "Nat" else _builtin(name, value.type, value)
        if name in ("min", "max"):
            self._arity(node, name, 2)
            a, b, typ = _common(*(_numeric(self.expr(arg, env), node) for arg in node.args), node)
            return _builtin(name, typ, a, b)
        if name == "range":
            self._arity(node, name, 1, 2, 3)
            args = [self.expr(arg, env, "Int") for arg in node.args]
            if len(args) == 1:
                args.insert(0, _literal("Int", 0))
            if len(args) == 2:
                args.append(_literal("Int", 1))
            return _builtin(name, ("List", "Int"), *args)
        if name == "len":
            self._arity(node, name, 1)
            return _builtin(name, "Int", self._sequence(node.args[0], env))
        if name in _AGGREGATES:
            self._arity(node, name, 1)
            element = "Bool" if name in ("all", "any") else expected if expected in _RANK else None
            sequence = self._sequence(node.args[0], env, element)
            typ = sequence.type[1]
            if (name in ("all", "any") and typ != "Bool") or (name in ("sum", "prod") and typ not in _RANK):
                _fail(node, f"{name} cannot reduce values of type {typ}")
            return _builtin(name, typ, sequence)
        if name in ("zip", "concat"):
            self._arity(node, name, 2)
            a = self._sequence(node.args[0], env, expected[1] if name == "concat" and expected and _family(expected, "List") else None)
            b = self._sequence(node.args[1], env, a.type[1] if name == "concat" else None)
            if name == "zip":
                result = ("List", ("Tuple", a.type[1], b.type[1]))
            elif a.type == b.type:
                result = a.type
            else:
                _fail(node, f"concat needs equal element types, got {a.type} and {b.type}")
            return _builtin(name, result, a, b)
        if name in ("map", "filter", "flat_map", "fold"):
            return self._combinator(node, name, env, expected)
        if name in ("contains", "lookup"):
            return self._mapping_op(node, name, env, expected)
        _fail(node, f"Unknown function {node.func.id}")

    def _mapping_op(self, node, name, env, expected):
        self._arity(node, name, 2 if name == "contains" else 3)
        collection, key_node = node.args[:2]
        if isinstance(collection, ast.List) and not collection.elts:
            key = self.expr(key_node, env)
            if name == "contains":
                sequence = Expr("list", ("List", key.type))
            else:
                default = self.expr(node.args[2], env, expected)
                sequence = Expr("list", ("List", ("Tuple", key.type, default.type)))
        else:
            sequence = self._sequence(collection, env)
            element = sequence.type[1]
            if name == "contains":
                key = self.expr(key_node, env, element)
            else:
                if not _family(element, "Tuple") or len(element) != 3:
                    _fail(collection, "lookup requires a finite list of (key, value) pairs")
                key = self.expr(key_node, env, element[1])
                default = self.expr(node.args[2], env, element[2])
        if not _has_exact_equality(key.type):
            _fail(key_node, f"{name} needs an exact equality for keys, got {key.type}")
        if name == "contains":
            return _builtin(name, "Bool", sequence, key)
        return _builtin(name, default.type, sequence, key, default)

    def _function_arg(self, node, env, parameter_types: tuple[Type, ...], result: Type | None = None):
        formals = tuple(self._fresh() for _ in parameter_types)
        arguments = tuple(_var(name, typ) for name, typ in zip(formals, parameter_types))
        if isinstance(node, ast.Lambda):
            params = node.args
            if params.posonlyargs or params.kwonlyargs or params.vararg or params.kwarg or params.defaults or len(params.args) != len(parameter_types):
                _fail(node, f"Lambda must have {len(parameter_types)} positional parameters")
            local = dict(env)
            seen = set()
            for param, expr in zip(params.args, arguments):
                _identifier(param, param.arg, local=True)
                if param.arg in seen:
                    _fail(param, f"Duplicate parameter {param.arg}")
                seen.add(param.arg)
                local[param.arg] = expr
            body = self.expr(node.body, local, result)
        elif isinstance(node, ast.Name) and node.id not in env and node.id in self.signatures and node.id not in self.constants:
            body = self._user_call(node.id, arguments, node)
            if result is not None:
                body = _coerce(body, result, node)
        elif isinstance(node, ast.Name) and node.id in env and _family(env[node.id].type, "Fn"):
            fn = env[node.id]
            if fn.type[1:-1] != parameter_types or (result is not None and fn.type[-1] != result):
                _fail(node, "Function parameter or result types do not match the sequence operator")
            return fn
        else:
            _fail(node, "Use a lambda or a named helper for the sequence operator")
        return Expr("lambda", ("Fn", *parameter_types, body.type), (body,), formals)

    def _combinator(self, node, name, env, expected):
        self._arity(node, name, 3 if name == "fold" else 2)
        if name == "fold":
            fn, initial, sequence = node.args
            seed = self.expr(initial, env, expected)
            data = self._sequence(sequence, env)
            function = self._function_arg(fn, env, (seed.type, data.type[1]), seed.type)
            return _builtin(name, seed.type, function, seed, data)
        fn, sequence = node.args
        data = self._sequence(sequence, env)
        if name == "filter":
            result = "Bool"
        elif expected and _family(expected, "List"):
            result = expected if name == "flat_map" else expected[1]
        else:
            result = None
        function = self._function_arg(fn, env, (data.type[1],), result)
        if name == "flat_map" and not _family(function.type[-1], "List"):
            _fail(node, "flat_map requires a function returning a list")
        typ = data.type if name == "filter" else function.type[-1] if name == "flat_map" else ("List", function.type[-1])
        return _builtin(name, typ, function, data)

    def _comprehension(self, node, env, typ, index):
        generator = node.generators[index]
        if generator.is_async:
            _fail(generator, "Only finite synchronous iteration is supported")
        sequence = self._sequence(generator.iter, env)
        arg = _var(self._fresh(), sequence.type[1])
        inner = dict(env)
        bound: list[_Binding] = []
        self._bind_target(generator.target, arg, inner, bound, rebind=False)
        if generator.ifs:
            predicates = [self.expr(cond, inner, "Bool") for cond in generator.ifs]
            body = predicates[-1]
            for test in reversed(predicates[:-1]):
                body = _builtin("and", "Bool", test, body)
            predicate = Expr("lambda", ("Fn", arg.type, "Bool"), (_wrap(bound, body),), (arg.value,))
            sequence = _builtin("filter", sequence.type, predicate, sequence)
        last = index + 1 == len(node.generators)
        body = self.expr(node.elt, inner, typ) if last else self._comprehension(node, inner, typ, index + 1)
        transform = Expr("lambda", ("Fn", arg.type, body.type), (_wrap(bound, body),), (arg.value,))
        return _builtin("map" if last else "flat_map", ("List", body.type) if last else body.type, transform, sequence)

    def _bind_target(self, target, value, env, bindings, *, rebind=True):
        if isinstance(target, ast.Name):
            _identifier(target, target.id, local=True)
            if rebind and target.id in env:
                value = _coerce(value, env[target.id].type, target)
            if value.op == "var":
                env[target.id] = value
            else:
                binder = self._fresh()
                env[target.id] = _var(binder, value.type)
                bindings.append((binder, value))
            return
        if isinstance(target, (ast.List, ast.Tuple)):
            if value.type == "Factor" and len(target.elts) == 3:
                types = tuple(_FIELDS.values())
                names = tuple(_FIELDS)
            elif _family(value.type, "Tuple") and len(target.elts) == len(value.type) - 1:
                types, names = value.type[1:], None
            else:
                _fail(target, "Tuple unpacking requires a compatible Tuple or Factor")
            if value.op != "var":
                binder = self._fresh()
                bindings.append((binder, value))
                value = _var(binder, value.type)
            for index, item in enumerate(target.elts):
                projection = _builtin(names[index], types[index], value) if names else _builtin("get", types[index], value, _literal("Int", index))
                self._bind_target(item, projection, env, bindings, rebind=rebind)
            return
        _fail(target, "Bind local names or unpack tuples, without assigning into containers")

    @staticmethod
    def _pack(values: tuple[Expr, ...]):
        if not values:
            return _literal("Bool", False)
        return values[0] if len(values) == 1 else _tuple_expr(values)

    def _unpack(self, names, value, env, bindings):
        binder = self._fresh()
        bindings.append((binder, value))
        if len(names) == 1:
            env[names[0]] = _var(binder, value.type)
        elif len(names) > 1:
            product = _var(binder, value.type)
            for index, name in enumerate(names):
                component = self._fresh()
                typ = value.type[index + 1]
                bindings.append((component, _builtin("get", typ, product, _literal("Int", index))))
                env[name] = _var(component, typ)

    def _block(self, statements, env, result, finish=None):
        scope = dict(env)
        bindings: list[_Binding] = []
        for i, statement in enumerate(statements):
            if isinstance(statement, ast.Return):
                if result is None or statement.value is None:
                    _fail(statement, "This block needs a finite value in its outer scope, not an early return")
                return _wrap(bindings, self.expr(statement.value, scope, result))
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1:
                    _fail(statement, "Use a single assignment or tuple unpacking")
                target = statement.targets[0]
                expected = scope[target.id].type if isinstance(target, ast.Name) and target.id in scope else None
                self._bind_target(target, self.expr(statement.value, scope, expected), scope, bindings)
            elif isinstance(statement, ast.AnnAssign):
                if statement.value is None or not isinstance(statement.target, ast.Name):
                    _fail(statement, "Assign a value to a typed local name")
                typ = _annotation(statement.annotation, statement)
                value = self.expr(statement.value, scope, typ)
                self._bind_target(statement.target, value, scope, bindings)
            elif isinstance(statement, ast.AugAssign):
                target = statement.target
                if not isinstance(target, ast.Name) or target.id not in scope:
                    _fail(statement, "Update a previously bound local name")
                value = self._binary(statement, scope[target.id], self.expr(statement.value, scope))
                self._bind_target(target, value, scope, bindings)
            elif isinstance(statement, ast.If):
                condition = self.expr(statement.test, scope, "Bool")
                left_exit, right_exit = _returns(statement.body), _returns(statement.orelse)
                if left_exit or right_exit:
                    if result is None:
                        _fail(statement, "Return from the surrounding function after this finite block")
                    rest = statements[i + 1:]
                    left = self._block(statement.body if left_exit else [*statement.body, *rest], scope, result, finish)
                    right = self._block(statement.orelse if right_exit else [*statement.orelse, *rest], scope, result, finish)
                    return _wrap(bindings, Expr("if", result, (condition, left, right)))
                left_names = _assigned(statement.body)
                right_names = _assigned(statement.orelse)
                bound = tuple(n for n in dict.fromkeys((*left_names, *right_names)) if n in scope or n in left_names and n in right_names)
                def merge(local):
                    missing = [name for name in bound if name not in local]
                    if missing:
                        _fail(statement, "Local has no value on every branch: " + ", ".join(missing))
                    return self._pack(tuple(local[n] for n in bound))
                left = self._block(statement.body, scope, None, merge)
                right = self._block(statement.orelse, scope, None, merge)
                left, right, typ = _common(left, right, statement)
                self._unpack(bound, Expr("if", typ, (condition, left, right)), scope, bindings)
            elif isinstance(statement, ast.For):
                if statement.orelse:
                    _fail(statement, "Finite for loops use a single body")
                self._for(statement, scope, bindings)
            elif isinstance(statement, ast.Match):
                synthetic = self._match(statement, scope, bindings)
                body = self._block([*synthetic, *statements[i + 1:]], scope, result, finish)
                return _wrap(bindings, body)
            elif not isinstance(statement, ast.Pass):
                _fail(statement, f"Unsupported statement {type(statement).__name__}")
        if finish is None:
            raise FormatError("Every path in a function needs a return statement")
        return _wrap(bindings, finish(scope))

    def _for(self, node, env, bindings):
        sequence = self._sequence(node.iter, env)
        target_names = {part.id for part in ast.walk(node.target) if isinstance(part, ast.Name)}
        assigned = tuple(n for n in _assigned(node.body) if n in env and n not in target_names)
        initial = self._pack(tuple(env[name] for name in assigned))
        state = _var(self._fresh(), initial.type)
        item = _var(self._fresh(), sequence.type[1])
        local = dict(env)
        unpack: list[_Binding] = []
        self._unpack(assigned, state, local, unpack)
        self._bind_target(node.target, item, local, unpack, rebind=False)
        def finish(scope):
            return self._pack(tuple(scope[name] for name in assigned))
        body = self._block(node.body, local, None, finish)
        if initial.type != body.type:
            _fail(node, "Finite loop accumulator types must remain fixed")
        function = Expr("lambda", ("Fn", initial.type, item.type, initial.type), (_wrap(unpack, body),), (state.value, item.value))
        self._unpack(assigned, _builtin("fold", initial.type, function, initial, sequence), env, bindings)

    def _match(self, node, env, bindings):
        subject = self.expr(node.subject, env)
        if subject.type not in ("Pauli", "Int", "Nat", "Bool"):
            _fail(node, "Match on discrete Pauli, integer or boolean values")
        binder = self._fresh()
        bindings.append((binder, subject))
        key = f"__match_{binder}"
        env[key] = _var(binder, subject.type)
        alternatives = []
        seen = set()
        fallback = None
        for case in node.cases:
            pattern = case.pattern
            if case.guard is not None:
                _fail(pattern, "Use an if expression inside the matching case")
            if fallback is not None:
                _fail(pattern, "A wildcard must be the final case")
            if isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None:
                fallback = case.body
                continue
            if isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name in _PAULIS:
                value = ast.Constant(pattern.name)
            elif isinstance(pattern, ast.MatchValue):
                value = pattern.value
            elif isinstance(pattern, ast.MatchSingleton) and type(pattern.value) is bool:
                value = ast.Constant(pattern.value)
            else:
                _fail(pattern, "Use literal patterns or a final wildcard")
            value = ast.copy_location(value, pattern)
            typed = self.expr(value, env, subject.type)
            if typed.op != "literal" or typed.value in seen:
                _fail(pattern, "Each literal pattern must be distinct")
            seen.add(typed.value)
            alternatives.append((value, case.body))
        if fallback is None:
            if (subject.type == "Pauli" and seen == _PAULIS) or (subject.type == "Bool" and seen == {True, False}):
                _, fallback = alternatives.pop()
            else:
                _fail(node, "Match must exhaust the possible values or include a wildcard")
        for value, body in reversed(alternatives):
            test = ast.Compare(left=ast.Name(id=key, ctx=ast.Load()), ops=[ast.Eq()], comparators=[value])
            fallback = [ast.copy_location(ast.If(test=test, body=body, orelse=fallback), node)]
        return fallback


def parse(source: str, dimension: int, qubits_per_cell: int, *, bits=40, terms=32) -> Program:
    """Parse a pure mathematical program and preserve lexical call/loop structure."""
    if type(source) is not str:
        raise FormatError("The correlator source must be text")
    if len(source.encode("utf-8")) > 1024 * 1024:
        raise FormatError("The correlator source exceeds 1 MiB")
    if type(dimension) is not int or not 1 <= dimension <= MAX_DIMENSION:
        raise FormatError(f"Lattice dimension must be an integer in 1..{MAX_DIMENSION}")
    if type(qubits_per_cell) is not int or not 1 <= qubits_per_cell <= MAX_QUBITS_PER_CELL:
        raise FormatError(f"Qubits per cell must be an integer in 1..{MAX_QUBITS_PER_CELL}")
    try:
        RationalMath(precision_bits=bits, terms=terms)
    except RationalMathError as exc:
        raise FormatError(str(exc)) from exc
    try:
        tree = ast.parse(source, mode="exec")
        functions, limit = _Parser(source).module(tree)
    except SyntaxError as exc:
        raise FormatError(f"Invalid source at line {exc.lineno}, column {exc.offset}: {exc.msg}") from exc
    except RecursionError as exc:
        raise FormatError("Source nesting exceeds the parser recursion budget") from exc
    return Program(functions, dimension, qubits_per_cell, bits, terms, limit)
