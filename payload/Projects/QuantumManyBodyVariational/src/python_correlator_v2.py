"""Pure finite Python correlators, guarded float evaluation, and Lean real code.

Candidate modules are parsed, never exec'd or imported. v1 remains unchanged.
Floating decisions require interval-separated operands; ordinary arithmetic
still returns floats. Generated mathematical preconditions are proof targets,
not a claim that this Python compiler or interval backend is proved in Lean.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import math
import re

from . import python_correlator_compiler as v1
from . import correlator_float_guard as guard

CorrelatorError = v1.CorrelatorError
LANGUAGE_VERSION = "python-correlator-v2"
MAX_STEPS = 1_000_000
MAX_SEQUENCE = 4096
MAX_NODES = 32768
MATH_FUNCTIONS = {"sqrt", "exp", "sin", "cos", "log", "tanh"}
BUILTINS = {"sum", "prod", "all", "any", "abs", "len", "min", "max", "range", "float", "int", "bool", "str"}


@dataclass(frozen=True)
class Expr:
    kind: str
    typ: object
    args: tuple = ()
    value: object = None


def error(node, message):
    raise CorrelatorError(f"{message} at line {getattr(node, 'lineno', '?')}")


def scalar(e, node):
    if e.typ not in ("int", "real"):
        error(node, "Expected a numeric scalar")
    return e


def boolean(e, node):
    if e.typ != "bool":
        error(node, "Use an explicit boolean condition")
    return e


def promote(e, typ):
    if e.typ == typ:
        return e
    if e.typ == "int" and typ == "real":
        return Expr("cast", "real", (e,))
    raise CorrelatorError(f"Cannot promote {e.typ} to {typ}")


def common(a, b, node):
    if a.typ == b.typ:
        return a, b, a.typ
    if a.typ in ("int", "real") and b.typ in ("int", "real"):
        return promote(a, "real"), promote(b, "real"), "real"
    error(node, "Operands or branches have incompatible types")


def children(e):
    yield from e.args
    if e.kind == "reduce":
        for _, iterator, filters in e.value[1]:
            yield iterator
            yield from filters
    if e.kind == "fold":
        yield e.value[1]


def check_size(e):
    memo = {}
    def walk(x, depth=0):
        if depth > 96:
            raise CorrelatorError("Expanded expression exceeds depth limit")
        key = id(x)
        if key in memo:
            return memo[key]
        size = 1
        for c in children(x):
            size += walk(c, depth+1)
            if size > MAX_NODES:
                raise CorrelatorError("Expanded expression exceeds size limit")
        memo[key] = size
        return size
    walk(e)


def static(e, node):
    check_size(e)
    try:
        return evaluate(e, (), {}, [MAX_STEPS], Fraction("1e-10"))
    except (KeyError, CorrelatorError, ValueError, ArithmeticError) as exc:
        error(node, f"Expected a computable constant: {exc}")


def real(value):
    return guard.coerce(value)


def numeric_value(value):
    return value.value if isinstance(value, guard.GuardedReal) else value


def bounded(value):
    if type(value) is int:
        v1._bounded(value)
    elif isinstance(value, guard.GuardedReal):
        if not math.isfinite(value.value):
            raise CorrelatorError("Nonfinite numeric result")
    return value


def evaluate(e, word, env, budget, margin):
    budget[0] -= 1
    if budget[0] < 0:
        raise CorrelatorError("Evaluation exceeds operation budget")
    ev = lambda x: evaluate(x, word, env, budget, margin)
    k = e.kind
    if k == "const":
        return guard.from_literal(e.value) if e.typ == "real" else e.value
    if k == "math_const":
        return guard.constant(e.value)
    if k == "word":
        return word
    if k == "var":
        return env[e.value] if e.value in env else ev(e.args[0])
    if k == "let":
        scope = dict(env)
        scope[e.value] = ev(e.args[0])
        return evaluate(e.args[1], word, scope, budget, margin)
    if k == "cast":
        return real(ev(e.args[0]))
    if k in ("tuple", "list"):
        return tuple(ev(x) for x in e.args)
    if k == "dict":
        return dict(zip(e.value, (ev(x) for x in e.args)))
    if k == "get":
        container, index = ev(e.args[0]), ev(e.args[1])
        if type(index) is int or type(index) is str:
            try:
                return container[index]
            except (IndexError, KeyError) as exc:
                raise CorrelatorError("Container index/key outside its domain") from exc
        raise CorrelatorError("Container index must be discrete")
    if k == "coord":
        return ev(e.args[0])[e.value]
    if k == "len":
        return len(ev(e.args[0]))
    if k == "range":
        start, stop, step = (ev(x) for x in e.args)
        if any(type(x) is not int for x in (start,stop,step)) or step == 0:
            raise CorrelatorError("range needs integer arguments and a nonzero step")
        result = range(start,stop,step)
        if len(result) > MAX_SEQUENCE:
            raise CorrelatorError("range exceeds finite sequence limit")
        return result
    if k == "if":
        return ev(e.args[1] if ev(e.args[0]) else e.args[2])
    if k == "and":
        return all(ev(x) for x in e.args)
    if k == "or":
        return any(ev(x) for x in e.args)
    if k == "not":
        return not ev(e.args[0])
    if k == "neg":
        return bounded(-ev(e.args[0]))
    if k == "abs":
        return bounded(abs(ev(e.args[0])))
    if k in MATH_FUNCTIONS:
        return guard.math_func(k, real(ev(e.args[0])))
    if k in ("add", "sub", "mul", "div", "mod", "floordiv", "pow"):
        a,b = ev(e.args[0]), ev(e.args[1])
        if k == "pow":
            if type(b) is not int or not 0 <= b <= 1024:
                raise CorrelatorError("Exponent must evaluate to an integer in 0..1024")
            return bounded(a.pow_n(b) if isinstance(a,guard.GuardedReal) else a**b)
        if k == "div":
            return real(a) / real(b)
        if k in ("mod", "floordiv"):
            if type(a) is not int or type(b) is not int or b <= 0:
                raise CorrelatorError("Integer // and % require positive divisor")
            return a%b if k == "mod" else a//b
        if e.typ == "real":
            a,b = real(a),real(b)
        return bounded({"add":lambda:a+b,"sub":lambda:a-b,"mul":lambda:a*b}[k]())
    if k in ("eq","ne","lt","le","gt","ge"):
        a,b = ev(e.args[0]),ev(e.args[1])
        if e.args[0].typ == "real":
            return guard.compare(k, real(a), real(b), margin=margin)
        return {"eq":lambda:a==b,"ne":lambda:a!=b,"lt":lambda:a<b,"le":lambda:a<=b,"gt":lambda:a>b,"ge":lambda:a>=b}[k]()
    if k in ("minimum","maximum"):
        items = ev(e.args[0])
        if not items:
            raise CorrelatorError("min/max need a nonempty sequence")
        if e.typ == "real":
            return (guard.minimum if k == "minimum" else guard.maximum)([real(x) for x in items])
        return (min if k == "minimum" else max)(items)
    if k == "reduce":
        op, generators = e.value
        def values(i=0, scope=None):
            scope = env if scope is None else scope
            if i == len(generators):
                yield evaluate(e.args[0],word,scope,budget,margin)
                return
            bindings,iterator,filters = generators[i]
            for item in evaluate(iterator,word,scope,budget,margin):
                inner=dict(scope)
                if len(bindings)==1:
                    inner[bindings[0]]=item
                else:
                    inner.update(zip(bindings,item))
                if all(evaluate(c,word,inner,budget,margin) for c in filters):
                    yield from values(i+1,inner)
        if op == "all":
            return all(values())
        if op == "any":
            return any(values())
        initial=ev(e.args[1]) if len(e.args)>1 else (0 if op=="sum" else 1)
        result=initial
        for item in values():
            result=bounded(result+item if op=="sum" else result*item)
        return result
    if k == "fold":
        names,iterator,bindings=e.value
        state=ev(e.args[0])
        for item in evaluate(iterator,word,env,budget,margin):
            inner=dict(env)
            inner[names]=state
            if len(bindings)==1:
                inner[bindings[0]]=item
            else:
                inner.update(zip(bindings,item))
            state=evaluate(e.args[1],word,inner,budget,margin)
        return state
    raise CorrelatorError(f"Unsupported internal expression {k}")


class Parser:
    def __init__(self,source,dimension,rational_mode=False):
        self.source=source
        self.dimension=dimension
        self.aliases={}
        self.modules=set()
        self.functions={}
        self.defaults={}
        self.globals={}
        self.active=[]
        self.count=0
        self.work=MAX_NODES
        self.used_math=set()
        self.rational_mode=rational_mode

    def static(self,e,node):
        return static(e,node)

    def fresh(self,prefix="v"):
        self.count+=1
        return f"{prefix}{self.count}"

    def tick(self):
        self.work-=1
        if self.work<0:
            raise CorrelatorError("Parser expansion budget exhausted")

    def name(self,name,node):
        if name.startswith("__") or name in BUILTINS or name in self.modules or name in self.aliases or name in self.functions:
            error(node,"Cannot shadow an intrinsic, function, or module name")

    def expr(self,node,env):
        self.tick()
        if isinstance(node,ast.Constant):
            if type(node.value) in (bool,int):
                return Expr("const","bool" if type(node.value)is bool else "int",value=v1._bounded(node.value))
            if type(node.value)is float:
                # Reuse v1's lexical checks before allocating a decimal fraction.
                e=v1._Parser(self.dimension,self.source).expression(node,{})
                return Expr("const","real",value=e.value)
            if type(node.value)is str:
                if len(node.value)>256:
                    error(node,"String literal too long")
                return Expr("const","pauli" if node.value in "XYZ" and len(node.value)==1 else "str",value=node.value)
            error(node,"Unsupported literal")
        if isinstance(node,ast.Name):
            if node.id not in env:
                error(node,f"Unknown or unbound name {node.id!r}")
            return env[node.id]
        if isinstance(node,ast.Attribute):
            if isinstance(node.value,ast.Name) and node.value.id in self.modules and node.attr in ("pi","e"):
                self.used_math.add(node.attr)
                return Expr("math_const","real",value=node.attr)
            error(node,"Only approved math attributes are available")
        if isinstance(node,(ast.List,ast.Tuple)):
            values=tuple(self.expr(x,env) for x in node.elts)
            if len(values)>MAX_SEQUENCE:
                error(node,"Container too large")
            if isinstance(node,ast.Tuple):
                return Expr("tuple",("tuple",tuple(x.typ for x in values)),values)
            if not values:
                return Expr("list",("list","int"),values)
            typ=values[0].typ
            for x in values[1:]:
                _,_,typ=common(Expr("const",typ),x,node)
            values=tuple(promote(x,typ) for x in values)
            return Expr("list",("list",typ),values)
        if isinstance(node,ast.Dict):
            if not node.keys or len(node.keys)>MAX_SEQUENCE or any(k is None for k in node.keys):
                error(node,"Dictionary must be nonempty and finite")
            key_exprs=tuple(self.expr(k,env) for k in node.keys)
            keys=tuple(self.static(k,node) for k in key_exprs)
            if any(type(k)not in (str,int)for k in keys) or len(set(keys))!=len(keys):
                error(node,"Dictionary keys must be distinct constant integers or strings")
            if len({k.typ for k in key_exprs})!=1:
                error(node,"Dictionary keys must have one discrete type")
            values=tuple(self.expr(v,env)for v in node.values)
            typ=values[0].typ
            for x in values[1:]:
                _,_,typ=common(Expr("const",typ),x,node)
            return Expr("dict",("dict",typ,tuple(k.typ for k in key_exprs),keys),tuple(promote(x,typ)for x in values),keys)
        if isinstance(node,ast.Subscript):
            value=self.expr(node.value,env)
            if isinstance(node.slice,ast.Slice):
                error(node,"Slices are not part of this finite interface")
            index=self.expr(node.slice,env)
            if value.typ=="cell":
                i=self.static(index,node)
                if type(i)is not int or not-self.dimension<=i<self.dimension:
                    error(node,"Coordinate index must be a valid constant")
                return Expr("coord","int",(value,),i%self.dimension)
            if value.typ=="word":
                typ=("tuple",("cell","int","pauli"))
            elif isinstance(value.typ,tuple) and value.typ[0]=="tuple":
                i=self.static(index,node)
                if type(i)is not int or not-len(value.typ[1])<=i<len(value.typ[1]):
                    error(node,"Tuple index must be a valid constant")
                typ=value.typ[1][i%len(value.typ[1])]
                index=Expr("const","int",value=i%len(value.typ[1]))
            elif isinstance(value.typ,tuple)and value.typ[0]in("list","dict"):
                typ=value.typ[1]
                if value.typ[0]=="dict" and index.typ!=value.typ[2][0]:
                    error(node,"Dictionary key type does not match")
            else:
                error(node,"Unsupported container indexing")
            if index.typ not in("int","pauli","str"):
                error(node,"Index/key must be discrete")
            return Expr("get",typ,(value,index))
        if isinstance(node,ast.UnaryOp):
            a=self.expr(node.operand,env)
            if isinstance(node.op,ast.Not):
                return Expr("not","bool",(boolean(a,node),))
            scalar(a,node)
            if isinstance(node.op,ast.USub):return Expr("neg",a.typ,(a,))
            if isinstance(node.op,ast.UAdd):return a
            error(node,"Unsupported unary operator")
        if isinstance(node,ast.BinOp):
            a,b=scalar(self.expr(node.left,env),node),scalar(self.expr(node.right,env),node)
            if isinstance(node.op,ast.Pow):
                if b.typ!="int":error(node,"Exponent must be an integer")
                return Expr("pow",a.typ,(a,b))
            if isinstance(node.op,(ast.Mod,ast.FloorDiv)):
                if a.typ!="int"or b.typ!="int":error(node,"Integer % and // need integer operands")
                return Expr("mod"if isinstance(node.op,ast.Mod)else"floordiv","int",(a,b))
            if isinstance(node.op,ast.Div):
                return Expr("div","real",(promote(a,"real"),promote(b,"real")))
            a,b,typ=common(a,b,node)
            kind={ast.Add:"add",ast.Sub:"sub",ast.Mult:"mul"}.get(type(node.op))
            if kind:return Expr(kind,typ,(a,b))
            error(node,"Unsupported arithmetic operation")
        if isinstance(node,ast.BoolOp):
            return Expr("and"if isinstance(node.op,ast.And)else"or","bool",tuple(boolean(self.expr(x,env),node)for x in node.values))
        if isinstance(node,ast.Compare):
            ops={ast.Eq:"eq",ast.NotEq:"ne",ast.Lt:"lt",ast.LtE:"le",ast.Gt:"gt",ast.GtE:"ge"}
            terms=[node.left,*node.comparators]
            result=[]
            for left,right,op in zip(terms,terms[1:],node.ops):
                a,b,typ=common(self.expr(left,env),self.expr(right,env),node)
                kind=ops.get(type(op))
                if not kind:error(node,"Unsupported comparison")
                if typ=="real"and kind in("eq","ne")and not self.rational_mode:
                    error(node,"Float equality and inequality are unsupported; use ordered comparisons with a gap")
                if typ not in("int","real","str","pauli","bool")or(typ in("pauli","bool")and kind not in("eq","ne")):
                    error(node,"Unsupported discrete comparison")
                result.append(Expr(kind,"bool",(a,b)))
            return result[0]if len(result)==1 else Expr("and","bool",tuple(result))
        if isinstance(node,ast.IfExp):
            c=boolean(self.expr(node.test,env),node)
            a,b,typ=common(self.expr(node.body,env),self.expr(node.orelse,env),node)
            return Expr("if",typ,(c,a,b))
        if isinstance(node,ast.Call):
            return self.call(node,env)
        error(node,f"Unsupported Python syntax {type(node).__name__}")

    def iterator(self,node,env):
        value=self.expr(node,env)
        if value.typ=="word":return value,("cell","int","pauli")
        if isinstance(value.typ,tuple)and value.typ[0]in("list","range"):
            return value,(value.typ[1],)
        if isinstance(value.typ,tuple)and value.typ[0]=="tuple":
            if not value.typ[1]or len(set(value.typ[1]))!=1:error(node,"Iterator tuple must be nonempty and homogeneous")
            return value,(value.typ[1][0],)
        error(node,"Expected P or a finite range/list/tuple iterator")

    def bind_iterator(self,target,types,env):
        if len(types)==1 and isinstance(target,ast.Name):names=[target.id]
        elif isinstance(target,(ast.Tuple,ast.List))and len(target.elts)==len(types)and all(isinstance(x,ast.Name)for x in target.elts):names=[x.id for x in target.elts]
        else:error(target,"Iterator target has incompatible unpacking")
        if len(set(names))!=len(names):error(target,"Duplicate iterator binding")
        ids=[]
        inner=dict(env)
        for name,typ in zip(names,types):
            self.name(name,target)
            if name=="P":error(target,"Cannot replace P")
            ident=self.fresh()
            ids.append(ident)
            inner[name]=Expr("var",typ,value=ident)
        return tuple(ids),inner

    def call(self,node,env):
        if any(k.arg is None for k in node.keywords):error(node,"Keyword expansion is unsupported")
        if isinstance(node.func,ast.Name):name=self.aliases.get(node.func.id,node.func.id)
        elif isinstance(node.func,ast.Attribute)and isinstance(node.func.value,ast.Name)and node.func.value.id in self.modules:
            name=node.func.attr
            if name not in MATH_FUNCTIONS|{"prod"}:error(node,"Unapproved math function")
        else:error(node,"Only approved pure function calls are supported")
        if name in self.functions:
            return self.helper(name,node,env)
        if name in("sum","prod","all","any"):
            keywords={k.arg:k.value for k in node.keywords}
            if len(keywords)!=len(node.keywords)or set(keywords)-{"start"}:error(node,"Unsupported aggregator keyword")
            if len(node.args)not in(1,2)or(name in("all","any")and(len(node.args)!=1 or keywords)):
                error(node,"Invalid aggregator arguments")
            if len(node.args)==2 and keywords:error(node,"Duplicate start argument")
            start=node.args[1]if len(node.args)==2 else keywords.get("start")
            generator=node.args[0]
            if isinstance(generator,ast.GeneratorExp):
                bindings=[];inner=dict(env)
                for g in generator.generators:
                    if g.is_async:error(g,"Async generators are unsupported")
                    it,types=self.iterator(g.iter,inner)
                    ids,inner=self.bind_iterator(g.target,types,inner)
                    filters=tuple(boolean(self.expr(c,inner),c)for c in g.ifs)
                    bindings.append((ids,it,filters))
                elt=self.expr(generator.elt,inner)
            else:
                it,types=self.iterator(generator,env)
                if len(types)!=1:error(node,"Aggregate scalar values rather than raw P factors")
                ident=self.fresh()
                bindings=[((ident,),it,())]
                elt=Expr("var",types[0],value=ident)
            if name in("all","any"):boolean(elt,node)
            else:scalar(elt,node)
            args=(elt,)
            if start is not None:
                initial=scalar(self.expr(start,env),node)
                elt,initial,typ=common(elt,initial,node)
                args=(elt,initial)
            return Expr("reduce",elt.typ,args,(name,tuple(bindings)))
        if name=="range":
            if node.keywords or not 1<=len(node.args)<=3:error(node,"range requires one to three positional arguments")
            args=[self.expr(x,env)for x in node.args]
            if any(x.typ!="int"for x in args):error(node,"range arguments must be integers")
            if len(args)==1:args=[Expr("const","int",value=0),args[0],Expr("const","int",value=1)]
            elif len(args)==2:args.append(Expr("const","int",value=1))
            return Expr("range",("range","int"),tuple(args))
        if name=="len"and len(node.args)==1 and not node.keywords:
            value=self.expr(node.args[0],env)
            if value.typ!="word"and not isinstance(value.typ,tuple):error(node,"len requires a finite container")
            return Expr("len","int",(value,))
        if name in("min","max"):
            if node.keywords or not node.args:error(node,"min/max do not support these keywords")
            if len(node.args)==1:
                value=self.expr(node.args[0],env)
                if not isinstance(value.typ,tuple)or value.typ[0]not in("list","tuple"):error(node,"min/max need a finite numeric sequence")
                types=value.typ[1]if value.typ[0]=="tuple"else(value.typ[1],)
                if not types or any(t not in("int","real")for t in types):error(node,"min/max need numeric values")
                typ="real"if"real"in types else"int"
            else:
                values=[scalar(self.expr(x,env),node)for x in node.args]
                typ="real"if any(x.typ=="real"for x in values)else"int"
                value=Expr("list",("list",typ),tuple(promote(x,typ)for x in values))
            return Expr("minimum"if name=="min"else"maximum",typ,(value,))
        if name=="abs"and len(node.args)==1 and not node.keywords:
            a=scalar(self.expr(node.args[0],env),node)
            return Expr("abs",a.typ,(a,))
        if name in MATH_FUNCTIONS and len(node.args)==1 and not node.keywords:
            if name not in self.aliases.values()and not isinstance(node.func,ast.Attribute):error(node,"Import the requested math function explicitly")
            self.used_math.add(name)
            return Expr(name,"real",(promote(scalar(self.expr(node.args[0],env),node),"real"),))
        error(node,f"Unsupported function {name!r}")

    def annotation(self,node):
        if node is None:return
        if isinstance(node,ast.Name)and node.id in("int","float","bool","str","tuple","list","dict"):return
        if isinstance(node,ast.Subscript):
            self.annotation(node.value)
            for n in(node.slice.elts if isinstance(node.slice,ast.Tuple)else[node.slice]):
                if isinstance(n,ast.Constant)and n.value is Ellipsis:continue
                self.annotation(n)
            return
        error(node,"Only ordinary static scalar/container annotations are allowed")

    def bind(self,target,value,env,rest):
        if isinstance(target,ast.Name):
            self.name(target.id,target)
            if target.id=="P":error(target,"Cannot assign to P")
            ident=self.fresh("l")
            inner=dict(env);inner[target.id]=Expr("var",value.typ,(value,),ident)
            check_size(value)
            body=self.block(rest,inner)
            return Expr("let",body.typ,(value,body),ident)
        if isinstance(target,(ast.Tuple,ast.List))and isinstance(value.typ,tuple)and value.typ[0]=="tuple"and len(target.elts)==len(value.typ[1]):
            ident=self.fresh("t");bound=Expr("var",value.typ,(value,),ident)
            inner=dict(env)
            for i,(t,typ)in enumerate(zip(target.elts,value.typ[1])):
                if not isinstance(t,ast.Name):error(t,"Only one-level tuple unpacking is supported")
                self.name(t.id,t)
                if t.id=="P":error(t,"Cannot replace P")
                inner[t.id]=Expr("get",typ,(bound,Expr("const","int",value=i)))
            body=self.block(rest,inner)
            return Expr("let",body.typ,(value,body),ident)
        error(target,"Unsupported assignment target")

    def block(self,statements,env):
        self.tick()
        if not statements:raise CorrelatorError("Every path must return a value")
        first,*rest=statements
        if isinstance(first,ast.Return):
            if rest:error(first,"Unreachable statements are unsupported")
            return self.expr(first.value,env)
        if isinstance(first,ast.Assign):
            if len(first.targets)!=1:error(first,"Chained assignments are unsupported")
            return self.bind(first.targets[0],self.expr(first.value,env),env,rest)
        if isinstance(first,ast.AnnAssign):
            self.annotation(first.annotation)
            if first.value is None:error(first,"Annotated assignment requires a value")
            return self.bind(first.target,self.expr(first.value,env),env,rest)
        if isinstance(first,ast.AugAssign):
            if not isinstance(first.target,ast.Name)or first.target.id not in env:error(first,"Augmented assignment needs a bound scalar")
            node=ast.copy_location(ast.BinOp(left=ast.Name(id=first.target.id,ctx=ast.Load()),op=first.op,right=first.value),first)
            return self.bind(first.target,self.expr(node,env),env,rest)
        if isinstance(first,ast.If):
            cond=boolean(self.expr(first.test,env),first)
            def cont(branch):return branch if branch and isinstance(branch[-1],ast.Return)else[*branch,*rest]
            a,b,typ=common(self.block(cont(first.body),dict(env)),self.block(cont(first.orelse),dict(env)),first)
            return Expr("if",typ,(cond,a,b))
        if isinstance(first,ast.For):
            if first.orelse:error(first,"for-else is unsupported")
            if any(isinstance(s,(ast.Return,ast.Break,ast.Continue,ast.While))for s in ast.walk(first)):
                error(first,"Finite for bodies cannot return, break, continue, or contain while")
            iterator,types=self.iterator(first.iter,env)
            ids,inner=self.bind_iterator(first.target,types,env)
            targets={t.id for s in ast.walk(first)for t in ((s.targets if isinstance(s,ast.Assign)else[s.target])if isinstance(s,(ast.Assign,ast.AnnAssign,ast.AugAssign))else[])if isinstance(t,ast.Name)}
            loop_names={t.id for t in ast.walk(first.target)if isinstance(t,ast.Name)}
            carried=sorted((targets|loop_names)&env.keys())
            if not carried:error(first,"Finite for needs an existing accumulator variable")
            initial=Expr("tuple",("tuple",tuple(env[n].typ for n in carried)),tuple(env[n]for n in carried))
            state=self.fresh("state")
            for i,n in enumerate(carried):
                if n not in loop_names:
                    inner[n]=Expr("get",env[n].typ,(Expr("var",initial.typ,value=state),Expr("const","int",value=i)))
            ret=ast.Return(value=ast.Tuple(elts=[ast.Name(id=n,ctx=ast.Load())for n in carried],ctx=ast.Load()))
            body=self.block([*first.body,ret],inner)
            if body.typ!=initial.typ:error(first,"Loop accumulator types must stay fixed")
            folded=Expr("fold",initial.typ,(initial,body),(state,iterator,ids))
            synthetic=ast.Tuple(elts=[ast.Name(id=n,ctx=ast.Store())for n in carried],ctx=ast.Store())
            return self.bind(synthetic,folded,env,rest)
        error(first,f"Unsupported statement {type(first).__name__}")

    def helper(self,name,node,env):
        if name in self.active:error(node,"Recursive calls are unsupported")
        function=self.functions[name];args=function.args
        params=[a.arg for a in args.args]
        if len(node.args)>len(params):error(node,"Too many positional arguments")
        actual={n:self.expr(x,env)for n,x in zip(params,node.args)}
        for k in node.keywords:
            if k.arg not in params or k.arg in actual:error(node,"Invalid or duplicate keyword")
            actual[k.arg]=self.expr(k.value,env)
        for n,d in self.defaults[name].items():actual.setdefault(n,d)
        if set(actual)!=set(params):error(node,"Missing helper argument")
        inner=self.local_environment(function)
        bindings=[]
        # Actual expressions are evaluated in Python call order; defaults have
        # pure constant semantics and cause no mutable default sharing.
        order=[*params[:len(node.args)],*(k.arg for k in node.keywords)]
        order += [n for n in params if n not in order]
        for n in order:
            ident=self.fresh("arg");value=actual[n]
            inner[n]=Expr("var",value.typ,(value,),ident)
            bindings.append((ident,value))
        self.active.append(name)
        result=self.block(self.body(function),inner)
        self.active.pop()
        for ident,value in reversed(bindings):result=Expr("let",result.typ,(value,result),ident)
        check_size(result)
        return result

    def local_environment(self,function):
        names=set()
        for s in ast.walk(function):
            if isinstance(s,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
                for target in(s.targets if isinstance(s,ast.Assign)else[s.target]):
                    names.update(t.id for t in ast.walk(target)if isinstance(t,ast.Name))
            if isinstance(s,ast.For):names.update(t.id for t in ast.walk(s.target)if isinstance(t,ast.Name))
        return {k:v for k,v in self.globals.items()if k not in names}

    @staticmethod
    def body(function):
        body=function.body
        if body and isinstance(body[0],ast.Expr)and isinstance(body[0].value,ast.Constant)and type(body[0].value.value)is str:body=body[1:]
        return body

    def module(self,tree):
        for s in tree.body:
            self.tick()
            if isinstance(s,ast.Expr)and isinstance(s.value,ast.Constant)and type(s.value.value)is str:continue
            if isinstance(s,ast.Import):
                if any(a.name!="math"for a in s.names):error(s,"Only the math module may be imported")
                for a in s.names:
                    name=a.asname or"math"
                    if name in self.modules or name in self.aliases or name in self.globals or name in self.functions:
                        error(s,"Import name collides with an existing binding")
                    self.modules.add(name)
                continue
            if isinstance(s,ast.ImportFrom):
                if s.level or s.module!="math"or any(a.name not in MATH_FUNCTIONS|{"prod","pi","e"}for a in s.names):error(s,"Only approved math names may be imported")
                for a in s.names:
                    name=a.asname or a.name
                    if name in self.modules or name in self.aliases or name in self.globals or name in self.functions:
                        error(s,"Import name collides with an existing binding")
                    if a.name in("pi","e"):
                        self.globals[name]=Expr("math_const","real",value=a.name);self.used_math.add(a.name)
                    else:self.aliases[name]=a.name
                continue
            if isinstance(s,(ast.Assign,ast.AnnAssign)):
                target=s.targets[0]if isinstance(s,ast.Assign)and len(s.targets)==1 else s.target if isinstance(s,ast.AnnAssign)else None
                if not isinstance(target,ast.Name):error(s,"Global constants need one name")
                self.name(target.id,s)
                if target.id=="P":error(s,"P is reserved")
                if isinstance(s,ast.AnnAssign):self.annotation(s.annotation)
                value=self.expr(s.value,self.globals)
                check_size(value);self.static(value,s)
                self.globals[target.id]=value
                continue
            if isinstance(s,ast.FunctionDef):
                if s.name in self.functions or s.name in BUILTINS or s.name in self.globals or s.name in self.aliases or s.name in self.modules:error(s,"Invalid or duplicate function name")
                a=s.args
                if s.decorator_list or a.posonlyargs or a.vararg or a.kwarg or a.kwonlyargs or getattr(s,"type_params",()):error(s,"Use pure functions with ordinary positional-or-keyword parameters")
                for param in a.args:self.annotation(param.annotation)
                self.annotation(s.returns)
                names=[p.arg for p in a.args]
                if len(set(names))!=len(names):error(s,"Duplicate parameter")
                if any(n.startswith("__")or n in BUILTINS or n in self.modules or n in self.aliases for n in names):
                    error(s,"Parameters cannot shadow intrinsic or module names")
                self.functions[s.name]=s
                self.defaults[s.name]={n:self.expr(d,self.globals)for n,d in zip(names[len(names)-len(a.defaults):],a.defaults)}
                for d in self.defaults[s.name].values():self.static(d,s)
                continue
            error(s,f"Unsupported module statement {type(s).__name__}")
        if"f"not in self.functions:raise CorrelatorError("Missing def f(P)")
        f=self.functions["f"]
        if[f.arg for f in f.args.args]!=["P"]or f.args.defaults:error(f,"Entry point must be def f(P)")
        env=self.local_environment(f);env["P"]=Expr("word","word")
        self.active=["f"]
        result=self.block(self.body(f),env)
        scalar(result,f);check_size(result)
        return result


def lean_type(typ):
    if isinstance(typ,str):return {"int":"ℤ","real":"ℝ","bool":"Bool","pauli":"LocalPauli.Pauli","str":"String","cell":"(Fin DIM → ℤ)","word":"List ((Fin DIM → ℤ) × ℤ × LocalPauli.Pauli)"}[typ]
    if typ[0]=="tuple":
        types=typ[1]
        if not types:return"Unit"
        if len(types)==1:return lean_type(types[0])
        return"("+" × ".join(lean_type(t)for t in types)+")"
    if typ[0]in("list","range"):return f"List {lean_type(typ[1])}"
    if typ[0]=="dict":return"("+" × ".join(lean_type(typ[1])for _ in typ[2])+")"if len(typ[2])>1 else lean_type(typ[1])
    raise CorrelatorError("Unknown Lean type")


def tuple_get(value,index,length):
    if length==1:return value
    return"("+value+"".join(".2"for _ in range(index))+(".1"if index<length-1 else"")+")"


def lean(e,bindings,mode="value",margin="(1 / 10000000000 : ℝ)"):
    """Emit values or path-sensitive numerical-domain preconditions."""
    rec=lambda x:lean(x,bindings,mode,margin)
    val=lambda x:lean(x,bindings,"value",margin)
    k=e.kind
    if mode=="condition":
        if k=="var":return"True"
        if k=="let":
            inner=dict(bindings);inner[e.value]=e.value
            return f"({rec(e.args[0])} ∧ (let {e.value} : {lean_type(e.args[0].typ)} := {val(e.args[0])}; {lean(e.args[1],inner,mode,margin)}))"
        if k=="if":return f"({rec(e.args[0])} ∧ (if {val(e.args[0])} then {rec(e.args[1])} else {rec(e.args[2])}))"
        if k in("and","or"):
            def go(i):
                if i==len(e.args):return"True"
                condition=val(e.args[i]);gate=condition if k=="and"else f"!{condition}"
                return f"({rec(e.args[i])} ∧ (if {gate} then {go(i+1)} else True))"
            return go(0)
        if k in("reduce","fold"):
            if k=="fold":
                # Fold conditions follow the same evolving state, unlike a
                # universal condition on impossible accumulator values.
                state,it,ids=e.value
                init=val(e.args[0]);itval=val(it);s="foldState";x="foldItem"
                inner=dict(bindings);inner[state]=f"{s}.1"
                bind_lean_iterator(inner,ids,x,it)
                body=lean(e.args[1],inner,"value",margin);cond=lean(e.args[1],inner,"condition",margin)
                pair=f"({itval}).foldl (fun {s} {x} => ({body}, {s}.2 ∧ {cond})) ({init}, True)"
                return f"({rec(e.args[0])} ∧ {rec(it)} ∧ ({pair}).2)"
            op,gens=e.value
            def go(i,scope):
                if i==len(gens):return lean(e.args[0],scope,"condition",margin)
                ids,it,filters=gens[i];x="condItem"+ids[0]
                inner=dict(scope);bind_lean_iterator(inner,ids,x,it)
                cond=" ∧ ".join(lean(c,inner,"condition",margin)for c in filters)or"True"
                gate=" && ".join(lean(c,inner,"value",margin)for c in filters)or"true"
                return f"({lean(it,scope,'condition',margin)} ∧ ∀ {x} ∈ {lean(it,scope,'value',margin)}, {cond} ∧ (if {gate} then {go(i+1,inner)} else True))"
            result=go(0,bindings)
            if len(e.args)>1:result=f"({result} ∧ {rec(e.args[1])})"
            return result
        parts=[rec(x)for x in e.args]
        if k=="div":parts.append(f"{val(e.args[1])} ≠ 0")
        if k=="sqrt":parts.append(f"0 ≤ {val(e.args[0])}")
        if k=="log":parts.append(f"0 < {val(e.args[0])}")
        if k=="pow":parts.append(f"0 ≤ {val(e.args[1])}")
        if k in("mod","floordiv"):parts.append(f"0 < {val(e.args[1])}")
        if k=="range":parts.append(f"{val(e.args[2])} ≠ 0")
        if k in("lt","le","gt","ge")and e.args[0].typ=="real":parts.append(f"{margin} < |{val(e.args[0])} - {val(e.args[1])}|")
        if k=="get":
            container,index=e.args
            if container.typ=="word"or(isinstance(container.typ,tuple)and container.typ[0]=="list"):
                parts.append(f"-(({val(container)}).length : ℤ) ≤ {val(index)} ∧ {val(index)} < (({val(container)}).length : ℤ)")
            if isinstance(container.typ,tuple)and container.typ[0]=="dict":
                keys=container.value if container.kind=="dict"else None
                if keys is not None:parts.append("("+" ∨ ".join(f"{val(index)} = {lean(Expr('const',index.typ,value=x),bindings)}"for x in keys)+")")
        return"("+" ∧ ".join(parts)+")"if parts else"True"
    if k=="const":
        if e.typ=="real":return f"(({e.value.numerator} : ℝ) / ({e.value.denominator} : ℝ))"
        if e.typ=="int":return f"({e.value} : ℤ)"
        if e.typ=="bool":return"true"if e.value else"false"
        if e.typ=="pauli":return f"({v1._PAULIS[e.value]} : LocalPauli.Pauli)"
        import json
        return json.dumps(e.value,ensure_ascii=False)
    if k=="math_const":return"Real.pi"if e.value=="pi"else"(Real.exp 1)"
    if k=="word":return"wordFactors p"
    if k=="var":return bindings[e.value]
    if k=="let":
        inner=dict(bindings);inner[e.value]=e.value
        return f"(let {e.value} : {lean_type(e.args[0].typ)} := {val(e.args[0])}; {lean(e.args[1],inner,mode,margin)})"
    if k=="cast":return f"(({val(e.args[0])}) : ℝ)"
    if k=="tuple":return"()"if not e.args else val(e.args[0])if len(e.args)==1 else"("+", ".join(val(x)for x in e.args)+")"
    if k=="list":return"["+", ".join(val(x)for x in e.args)+"]"
    if k=="dict":return val(e.args[0])if len(e.args)==1 else"("+", ".join(val(x)for x in e.args)+")"
    if k=="get":
        c,i=e.args;cv,iv=val(c),val(i)
        if isinstance(c.typ,tuple)and c.typ[0]=="tuple":
            index=i.value if i.kind=="const"else static(i,None)
            return tuple_get(cv,index%len(c.typ[1]),len(c.typ[1]))
        if isinstance(c.typ,tuple)and c.typ[0]=="dict":
            original=c
            while original.kind=="var"and original.args:original=original.args[0]
            keys=original.value
            result=tuple_get(cv,len(keys)-1,len(keys))
            for j,key in reversed(list(enumerate(keys[:-1]))):
                result=f"(if {iv} = {lean(Expr('const',i.typ,value=key),bindings)} then {tuple_get(cv,j,len(keys))} else {result})"
            return result
        return f"({cv})[Int.toNat (if {iv} < 0 then (({cv}).length : ℤ) + {iv} else {iv})]!"
    if k=="coord":return f"({val(e.args[0])} ⟨{e.value}, by decide⟩)"
    if k=="len":
        a=e.args[0]
        if isinstance(a.typ,tuple)and a.typ[0]in("tuple","dict"):
            return f"({len(a.typ[1])if a.typ[0]=='tuple'else len(a.typ[2])} : ℤ)"
        return f"(({val(a)}).length : ℤ)"
    if k=="range":return"(intRange "+" ".join(val(x)for x in e.args)+")"
    if k=="if":return f"(if {val(e.args[0])} then {val(e.args[1])} else {val(e.args[2])})"
    if k=="not":return f"(!{val(e.args[0])})"
    if k in("and","or"):return"("+(" && "if k=="and"else" || ").join(val(x)for x in e.args)+")"
    if k=="neg":return f"(-{val(e.args[0])})"
    if k=="abs":return f"|{val(e.args[0])}|"
    if k in MATH_FUNCTIONS:return f"(Real.{k} {val(e.args[0])})"
    if k in("add","sub","mul","div","mod","floordiv"):
        return f"({val(e.args[0])} "+{"add":"+","sub":"-","mul":"*","div":"/","mod":"%","floordiv":"/"}[k]+f" {val(e.args[1])})"
    if k=="pow":return f"({val(e.args[0])} ^ Int.toNat {val(e.args[1])})"
    if k in("eq","ne","lt","le","gt","ge"):
        return f"(decide ({val(e.args[0])} "+{"eq":"=","ne":"≠","lt":"<","le":"≤","gt":">","ge":"≥"}[k]+f" {val(e.args[1])}))"
    if k in("minimum","maximum"):
        a=e.args[0]
        if a.kind in("list","tuple"):xs=list(a.args)
        else:
            if a.typ[0]=="tuple":xs=[Expr("get",t,(a,Expr("const","int",value=i)))for i,t in enumerate(a.typ[1])]
            else:error(None,"min/max on variable-length lists is not supported by Lean emitter")
        if not xs:error(None,"min/max need nonempty finite containers")
        result=lean(promote(xs[0],e.typ),bindings)
        for x in xs[1:]:result=f"({'min'if k=='minimum'else'max'} {result} {lean(promote(x,e.typ),bindings)})"
        return result
    if k=="reduce":
        op,gens=e.value
        def emit(j,scope):
            if j==len(gens):return lean(e.args[0],scope,"value",margin)
            ids,it,filters=gens[j];x="item"+ids[0]
            inner=dict(scope);bind_lean_iterator(inner,ids,x,it)
            body=emit(j+1,inner)
            gate=" && ".join(lean(c,inner,"value",margin)for c in filters)
            neutral={"sum":"0","prod":"1","all":"true","any":"false"}[op]
            if gate:body=f"(if {gate} then {body} else {neutral})"
            values=f"({lean(it,scope,'value',margin)}).map (fun {x} => {body})"
            if op in("sum","prod"):return f"({values}).{'sum'if op=='sum'else'prod'}"
            return f"({values}).{'all'if op=='all'else'any'} (fun b => b)"
        result=emit(0,bindings)
        if len(e.args)>1:result=f"({result} {'+'if op=='sum'else'*'} {val(e.args[1])})"
        return result
    if k=="fold":
        state,it,ids=e.value;x="foldItem"+ids[0]
        inner=dict(bindings);inner[state]=state;bind_lean_iterator(inner,ids,x,it)
        return f"({val(it)}).foldl (fun {state} {x} => {lean(e.args[1],inner,'value',margin)}) {val(e.args[0])}"
    raise CorrelatorError(f"Cannot emit Lean expression {k}")


def bind_lean_iterator(scope,ids,name,iterator):
    if iterator.typ=="word":
        scope.update({ids[0]:name+".1",ids[1]:name+".2.1",ids[2]:name+".2.2"})
    else:scope[ids[0]]=name


SUPPORT = r'''
def keyLe : List ℤ → List ℤ → Bool
  | [], _ => true
  | _ :: _, [] => false
  | a :: as, b :: bs => if a < b then true else if b < a then false else keyLe as bs

def wordFactors (p : Word (Site DIM QUBITS)) : List ((Fin DIM → ℤ) × ℤ × LocalPauli.Pauli) :=
  let sites := p.support.toList.mergeSort (fun s t =>
    keyLe ((List.ofFn s.1) ++ [(s.2.val : ℤ)]) ((List.ofFn t.1) ++ [(t.2.val : ℤ)]))
  sites.map (fun s => (s.1, (s.2.val : ℤ), p s))

def intRange (start stop step : ℤ) : List ℤ :=
  let count := if 0 < step then Int.toNat ((stop - start + step - 1) / step)
    else if step < 0 then Int.toNat ((start - stop - step - 1) / (-step)) else 0
  (List.range count).map (fun i => start + (i : ℤ) * step)
'''


class CorrelatorProgram:
    def __init__(self,source,d,q,expression,used_math,margin):
        self.source=source;self.dimension=d;self.qubits_per_cell=q
        self._expression=expression;self.used_math=set(used_math);self.branch_margin=margin
        self.source_sha256=hashlib.sha256(source.encode()).hexdigest()

    @classmethod
    def from_source(cls,source,dimension,qubits_per_cell,branch_margin="1e-10"):
        if type(source)is not str or len(source.encode())>65536:raise CorrelatorError("Source must be text within 64KiB")
        if type(dimension)is not int or not 1<=dimension<=32:raise CorrelatorError("Invalid dimension")
        if type(qubits_per_cell)is not int or not 1<=qubits_per_cell<=4096:raise CorrelatorError("Invalid qubits per cell")
        margin=guard._fraction(str(branch_margin))
        if margin<=0:raise CorrelatorError("branch_margin must be positive")
        try:
            tree=ast.parse(source)
            nodes=list(ast.walk(tree))
            if len(nodes)>4096:raise CorrelatorError("AST exceeds node limit")
            def depth(x):return 1+max((depth(c)for c in ast.iter_child_nodes(x)),default=0)
            if depth(tree)>96:raise CorrelatorError("AST exceeds depth limit")
            parser=Parser(source,dimension);expression=parser.module(tree)
            return cls(source,dimension,qubits_per_cell,expression,parser.used_math,margin)
        except RecursionError as exc:raise CorrelatorError("Source exceeds recursion depth")from exc

    def normalize_word(self,word):
        dummy=v1.CorrelatorProgram("",self.dimension,self.qubits_per_cell,None)
        return dummy.normalize_word(word)

    def evaluate(self,word):
        try:
            value=evaluate(self._expression,self.normalize_word(word),{},[MAX_STEPS],self.branch_margin)
            result=float(numeric_value(value))
            if not math.isfinite(result):raise CorrelatorError("Nonfinite function result")
            return result
        except (ValueError,ArithmeticError,KeyError,IndexError,TypeError)as exc:
            if isinstance(exc,CorrelatorError):raise
            raise CorrelatorError(f"Numerical evaluation rejected: {exc}")from exc

    def lean_definition(self,namespace="PythonCorrelator"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*",namespace):raise CorrelatorError("Invalid namespace")
        m=self.branch_margin;margin=f"({m.numerator} / {m.denominator} : ℝ)"
        body=lean(promote(self._expression,"real"),{},margin=margin)
        condition=lean(self._expression,{},"condition",margin)
        support=SUPPORT.replace("DIM",str(self.dimension)).replace("QUBITS",str(self.qubits_per_cell))
        imports=""
        if self.used_math&{"exp","sin","cos","tanh","e"}:imports+="public import Mathlib.Analysis.Complex.Trigonometric\n"
        if"log"in self.used_math:imports+="public import Mathlib.Analysis.SpecialFunctions.Log.Basic\n"
        if"pi"in self.used_math:imports+="public import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic\n"
        result=("module\npublic import QuantumManyBodyVariational.TrustedStateSpecV2\n"+imports+
                "@[expose] public section\nnoncomputable section\n"+f"namespace {namespace}\n"+
                "open scoped BigOperators\nopen QuantumManyBodyVariational.InfiniteState\nopen QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2\n"+
                f"-- {LANGUAGE_VERSION}; source SHA256 {self.source_sha256}\n"+support+
                f"\ndef fReal (p : Word (Site {self.dimension} {self.qubits_per_cell})) : ℝ := by\n  classical\n  exact {body}\n"+
                f"\ndef f : Correlator (Site {self.dimension} {self.qubits_per_cell}) := fReal\n"+
                f"\ndef NumericalPreconditions : Prop := ∀ p : Word (Site {self.dimension} {self.qubits_per_cell}), {condition}\n"+
                f"\ndef ProofObligation : Prop := StateCondition {self.dimension} {self.qubits_per_cell} f ∧ NumericalPreconditions\n"+
                f"end {namespace}\n")
        return result.replace("DIM",str(self.dimension))

    def manifest(self):
        result={"language_version":LANGUAGE_VERSION,"python_source_sha256":self.source_sha256,
                "lean_definition_sha256":hashlib.sha256(self.lean_definition().encode()).hexdigest(),
                "dimension":self.dimension,"qubits_per_cell":self.qubits_per_cell,
                "branch_margin":str(self.branch_margin),"number_semantics":"float_with_outward_ideal_real_enclosures_for_branch_decisions",
                "branch_policy":"ordered_real_comparisons_require_interval_separation_and_float_direction_agreement",
                "proof_status":"state_condition_and_numerical_preconditions_are_external_obligations",
                "compiler_correctness":"trusted_implementation_not_formally_proved",
                "numeric_backend":guard.backend_manifest(),
                "limits":{"source_bytes":65536,"ast_nodes":4096,"expanded_nodes":MAX_NODES,"depth":96,"sequence_length":MAX_SEQUENCE,"evaluation_steps":MAX_STEPS,"integer_exponent":1024}}
        return result
