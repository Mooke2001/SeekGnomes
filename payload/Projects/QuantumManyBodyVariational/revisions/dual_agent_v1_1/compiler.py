"""Real value expressions with deterministic rational control flow.

The value and branch projections share one pure source program. Numeric branch
operands use rational expression shadows, never rounded real values.

Revision of the finite-loop compiler with dynamic integer cell and homogeneous
tuple indexing. Frozen src and physics_first_v1_1 are kept unchanged.
"""
from __future__ import annotations
import ast
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
from src import python_correlator_v2 as syntax
from src import python_correlator_compiler as v1
from src.correlator_hybrid_branch import BranchMath, HybridReal

Expr=syntax.Expr
CorrelatorError=syntax.CorrelatorError
MATH_FUNCTIONS=syntax.MATH_FUNCTIONS
MAX_STEPS=syntax.MAX_STEPS
MAX_SEQUENCE=syntax.MAX_SEQUENCE
error=syntax.error
promote=syntax.promote
LANGUAGE_VERSION="python-correlator-hybrid-v1"
COMPILER_REVISION="dual-agent-v1.1-dynamic-indexing"


def integer_text(value):
    """Decimal output without changing Python's global large-int policy."""
    if type(value)is not int:raise CorrelatorError("Expected an integer literal")
    if abs(value).bit_length()>65536:raise CorrelatorError("Lean numeric literal exceeds bit limit")
    if abs(value)<10**1000:return str(value)
    sign="-"if value<0 else"";value=abs(value);chunks=[]
    while value:
        value,chunk=divmod(value,10**1000);chunks.append(chunk)
    return sign+str(chunks[-1])+"".join(f"{c:01000d}"for c in reversed(chunks[:-1]))

def evaluate(e, word, env, budget, maths):
    budget[0] -= 1
    if budget[0] < 0:
        raise CorrelatorError("Exact evaluation exceeds operation budget")
    ev=lambda x:evaluate(x,word,env,budget,maths)
    k=e.kind
    if k=="const":return maths.literal(e.value) if e.typ=="real" else e.value
    if k=="math_const":return maths.constant(e.value)
    if k=="word":return word
    if k=="var":
        if e.value in env:return env[e.value]
        if e.args:return ev(e.args[0])
        raise CorrelatorError(f"Cannot evaluate unbound symbolic variable {e.value!r} as a constant")
    if k=="let":
        inner=dict(env);inner[e.value]=ev(e.args[0])
        return evaluate(e.args[1],word,inner,budget,maths)
    if k=="cast":return maths.coerce(ev(e.args[0]))
    if k in("tuple","list"):return tuple(ev(x)for x in e.args)
    if k=="dict":return dict(zip(e.value,(ev(x)for x in e.args)))
    if k=="get":
        container,index=ev(e.args[0]),ev(e.args[1])
        if type(index)not in(int,str):raise CorrelatorError("Container key must be discrete")
        if e.args[0].typ=="cell" and (type(index)is not int or not -len(container)<=index<len(container)):
            raise CorrelatorError(f"Cell index {index!r} outside Python bounds [-{len(container)}, {len(container)})")
        if isinstance(e.args[0].typ,tuple) and e.args[0].typ[0]=="tuple" and (type(index)is not int or not -len(container)<=index<len(container)):
            raise CorrelatorError(f"Tuple index {index!r} outside Python bounds [-{len(container)}, {len(container)})")
        try:return container[index]
        except(KeyError,IndexError)as exc:raise CorrelatorError("Container key/index outside its domain")from exc
    if k=="coord":return ev(e.args[0])[e.value]
    if k=="len":return len(ev(e.args[0]))
    if k=="range":
        start,stop,step=(ev(x)for x in e.args)
        if any(type(x)is not int for x in(start,stop,step))or step==0:raise CorrelatorError("range needs integers and a nonzero step")
        result=range(start,stop,step)
        if len(result)>MAX_SEQUENCE:raise CorrelatorError("range exceeds finite sequence limit")
        return result
    if k=="if":return ev(e.args[1]if ev(e.args[0])else e.args[2])
    if k=="and":return all(ev(x)for x in e.args)
    if k=="or":return any(ev(x)for x in e.args)
    if k=="not":return not ev(e.args[0])
    if k=="neg":return bounded(-ev(e.args[0]))
    if k=="abs":return bounded(abs(ev(e.args[0])))
    if k in MATH_FUNCTIONS:return maths.math_func(k,ev(e.args[0]))
    if k in("add","sub","mul","div","mod","floordiv","pow"):
        a,b=ev(e.args[0]),ev(e.args[1])
        if k=="pow":
            if type(b)is not int or not 0<=b<=1024:raise CorrelatorError("Exponent must be an integer in 0..1024")
            return bounded(a**b)
        if k=="div":
            return bounded(maths.coerce(a)/maths.coerce(b))
        if k in("mod","floordiv"):
            if type(a)is not int or type(b)is not int or b<=0:raise CorrelatorError("Integer // and % require positive integer divisor")
            return a%b if k=="mod"else a//b
        return bounded({"add":lambda:a+b,"sub":lambda:a-b,"mul":lambda:a*b}[k]())
    if k in("eq","ne","lt","le","gt","ge"):
        a,b=ev(e.args[0]),ev(e.args[1])
        if e.args[0].typ=="real":return maths.compare(k,a,b)
        return{"eq":lambda:a==b,"ne":lambda:a!=b,"lt":lambda:a<b,"le":lambda:a<=b,"gt":lambda:a>b,"ge":lambda:a>=b}[k]()
    if k in("minimum","maximum"):
        items=ev(e.args[0])
        if not items:raise CorrelatorError("min/max need a nonempty finite sequence")
        return (maths.minimum if k=="minimum" else maths.maximum)(items) if e.typ=="real" else (min if k=="minimum"else max)(items)
    if k=="reduce":
        op,generators=e.value
        def values(i=0,scope=None):
            scope=env if scope is None else scope
            if i==len(generators):
                yield evaluate(e.args[0],word,scope,budget,maths);return
            ids,it,filters=generators[i]
            for item in evaluate(it,word,scope,budget,maths):
                inner=dict(scope)
                if len(ids)==1:inner[ids[0]]=item
                else:inner.update(zip(ids,item))
                if all(evaluate(c,word,inner,budget,maths)for c in filters):yield from values(i+1,inner)
        if op=="all":return all(values())
        if op=="any":return any(values())
        result=ev(e.args[1])if len(e.args)>1 else 0 if op=="sum"else 1
        for item in values():result=bounded(result+item if op=="sum"else result*item)
        return result
    if k=="fold":
        state,it,ids=e.value
        result=ev(e.args[0])
        for item in evaluate(it,word,env,budget,maths):
            inner=dict(env);inner[state]=result
            if len(ids)==1:inner[ids[0]]=item
            else:inner.update(zip(ids,item))
            result=evaluate(e.args[1],word,inner,budget,maths)
        return result
    raise CorrelatorError(f"Unsupported exact expression {k}")


def bounded(value):
    if type(value)is int:return v1._bounded(value)
    if isinstance(value,HybridReal):return value
    raise CorrelatorError("Unexpected numerical value")


def static(e,node,maths=None):
    syntax.check_size(e)
    try:
        # A constant real expression used by control flow need not force an
        # otherwise irrelevant float approximation.
        return evaluate(e,(),{},[MAX_STEPS],maths if maths is not None else BranchMath())
    except(KeyError,CorrelatorError,ValueError,ArithmeticError)as exc:
        error(node,f"Expected a valid rational constant: {exc}")


def constant_expression(e):
    """Whether evaluation is independent of the word and lexical binders."""
    seen=set()
    def visit(x):
        if id(x)in seen:return True
        seen.add(id(x))
        if x.kind=="word"or(x.kind=="var"and not x.args):return False
        return all(visit(c)for c in syntax.children(x))
    return visit(e)


class Parser(syntax.Parser):
    def __init__(self,source,dimension,maths):
        super().__init__(source,dimension,rational_mode=True)
        self.maths=maths
        self.rational_aliases=set()

    def static(self,e,node):
        return static(e,node,self.maths)

    def expr(self,node,env):
        if not isinstance(node,ast.Subscript):return super().expr(node,env)
        self.tick()
        if isinstance(node.slice,ast.Slice):error(node,"Slices are not part of this finite interface")
        value=self.expr(node.value,env)
        index=self.expr(node.slice,env)
        if value.typ=="cell":
            if index.typ!="int":error(node,"Cell index must have integer type")
            # Keep constant validation while allowing loop/helper variables to
            # reach both execution and Lean, rather than trying to evaluate
            # those symbolic variables with an empty environment.
            if index.kind=="const" and not -self.dimension<=index.value<self.dimension:
                error(node,f"Cell index outside Python bounds [-{self.dimension}, {self.dimension})")
            return Expr("get","int",(value,index))
        if value.typ=="word":
            if index.typ!="int":error(node,"Word index must have integer type")
            return Expr("get",("tuple",("cell","int","pauli")),(value,index))
        if isinstance(value.typ,tuple)and value.typ[0]=="tuple":
            if index.typ!="int":error(node,"Tuple index must have integer type")
            types=value.typ[1]
            if not types:error(node,"Cannot index an empty tuple")
            # An expression independent of word/iterator/state bindings is a
            # genuine constant, including helper arguments and local aliases.
            constant=self.static(index,node)if constant_expression(index)else None
            if type(constant)is int:
                if not -len(types)<=constant<len(types):error(node,f"Tuple index outside Python bounds [-{len(types)}, {len(types)})")
                return Expr("get",types[constant%len(types)],(value,Expr("const","int",value=constant%len(types))))
            if any(t!=types[0]for t in types[1:]):
                error(node,"Dynamic tuple indexing requires homogeneous element types; heterogeneous tuples require a constant index")
            return Expr("get",types[0],(value,index))
        if isinstance(value.typ,tuple)and value.typ[0]in("list","dict"):
            if value.typ[0]=="dict":
                if index.typ!=value.typ[2][0]:error(node,"Dictionary key type does not match")
            elif index.typ!="int":error(node,"List index must have integer type")
            return Expr("get",value.typ[1],(value,index))
        error(node,"Unsupported container indexing")

    def annotation(self,node):
        if isinstance(node,ast.Name)and node.id in self.rational_aliases:return
        return super().annotation(node)

    def call(self,node,env):
        if isinstance(node.func,ast.Name)and node.func.id in self.rational_aliases:
            if node.keywords or len(node.args)not in(1,2):error(node,"Fraction expects one or two positional arguments")
            if len(node.args)==1 and isinstance(node.args[0],ast.Constant)and type(node.args[0].value)is str:
                value=syntax.guard._fraction(node.args[0].value)
                return Expr("const","real",value=value)
            a=syntax.scalar(self.expr(node.args[0],env),node)
            if len(node.args)==1:return promote(a,"real")
            b=syntax.scalar(self.expr(node.args[1],env),node)
            return Expr("div","real",(promote(a,"real"),promote(b,"real")))
        return super().call(node,env)

    def block(self,statements,env):
        if not statements or not isinstance(statements[0],ast.For):
            return super().block(statements,env)
        self.tick()
        first,*rest=statements
        if first.orelse:error(first,"for-else is unsupported")
        if any(isinstance(s,(ast.Return,ast.Break,ast.Continue,ast.While))for s in ast.walk(first)):
            error(first,"Finite for bodies cannot return, break, continue, or contain while")
        iterator,types=self.iterator(first.iter,env)
        ids,inner=self.bind_iterator(first.target,types,env)
        # Tuple assignment and nested for targets bind the same Python locals
        # as scalar assignments. Every pre-existing assigned local is carried.
        targets=set()
        for statement in ast.walk(first):
            if isinstance(statement,ast.Assign):assigned=statement.targets
            elif isinstance(statement,(ast.AnnAssign,ast.AugAssign,ast.For)):assigned=[statement.target]
            else:continue
            for target in assigned:
                targets.update(t.id for t in ast.walk(target)if isinstance(t,ast.Name))
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

    def module(self,tree):
        statements=[]
        for s in tree.body:
            if isinstance(s,ast.ImportFrom)and s.module=="fractions"and not s.level:
                if any(a.name!="Fraction"for a in s.names):error(s,"Only fractions.Fraction is available")
                for a in s.names:
                    name=a.asname or a.name
                    if name in self.rational_aliases:error(s,"Duplicate Fraction import alias")
                    self.rational_aliases.add(name)
                    self.aliases[name]="Fraction"
                continue
            statements.append(s)
        for s in statements:
            if isinstance(s,ast.ImportFrom)and any((a.asname or a.name)in self.rational_aliases for a in s.names):
                error(s,"Imported name collides with Fraction alias")
            if isinstance(s,ast.Import)and any((a.asname or a.name)in self.rational_aliases for a in s.names):
                error(s,"Imported module collides with Fraction alias")
        return super().module(ast.Module(body=statements,type_ignores=[]))

def lean_type(typ):
    if isinstance(typ,str):return {"int":"ℤ","real":"HM.Number","bool":"Bool","pauli":"LocalPauli.Pauli","str":"String","cell":"(Fin DIM → ℤ)","word":"List ((Fin DIM → ℤ) × ℤ × LocalPauli.Pauli)"}[typ]
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
    return"(("+value+")"+"".join(".2"for _ in range(index))+(".1"if index<length-1 else"")+")"


def mark_value(typ, value, condition):
    """Attach a control/domain obligation to numerical leaves only."""
    if typ == "real":
        return f"(HM.mark ({condition}) ({value}))"
    if isinstance(typ, tuple):
        if typ[0] == "tuple":
            ts = typ[1]
            if not ts:
                return value
            xs = [mark_value(t, tuple_get(value, i, len(ts)), condition) for i, t in enumerate(ts)]
            return xs[0] if len(xs) == 1 else "(" + ", ".join(xs) + ")"
        if typ[0] in ("list", "range"):
            return f"({value}).map (fun marked => {mark_value(typ[1], 'marked', condition)})"
        if typ[0] == "dict":
            ts = [typ[1]] * len(typ[3])
            xs = [mark_value(t, tuple_get(value, i, len(ts)), condition) for i, t in enumerate(ts)]
            return xs[0] if len(xs) == 1 else "(" + ", ".join(xs) + ")"
    return value


def filters_condition(filters, bindings, margin):
    def go(i):
        if i == len(filters):
            return "True"
        x = filters[i]
        cond = lean(x, bindings, "condition", margin)
        value = lean(x, bindings, "value", margin)
        return f"({cond} ∧ (if {value} then {go(i + 1)} else True))"
    return go(0)


def index_condition(c, i, bindings, margin):
    cv, iv = lean(c, bindings, "value", margin), lean(i, bindings, "value", margin)
    parts = [lean(c, bindings, "construction", margin), lean(i, bindings, "condition", margin)]
    if c.typ == "cell":
        parts.append(f"-(DIM : ℤ) ≤ {iv} ∧ {iv} < (DIM : ℤ)")
    if isinstance(c.typ, tuple) and c.typ[0] == "tuple" and i.kind!="const":
        length=len(c.typ[1])
        parts.append(f"-({length} : ℤ) ≤ {iv} ∧ {iv} < ({length} : ℤ)")
    if c.typ == "word" or isinstance(c.typ, tuple) and c.typ[0] == "list":
        parts.append(f"-(({cv}).length : ℤ) ≤ {iv} ∧ {iv} < (({cv}).length : ℤ)")
    if isinstance(c.typ, tuple) and c.typ[0] == "dict":
        keys = c.typ[3]
        parts.append("(" + " ∨ ".join(f"{iv} = {lean(Expr('const', i.typ, value=x), bindings)}" for x in keys) + ")")
    return "(" + " ∧ ".join(parts) + ")"


def lean(e,bindings,mode="value",margin="(1 / 10000000000 : ℚ)"):
    """Emit values or path-sensitive numerical-domain preconditions."""
    rec=lambda x:lean(x,bindings,mode,margin)
    val=lambda x:lean(x,bindings,"value",margin)
    k=e.kind
    if mode in ("condition", "construction"):
        if mode=="condition" and isinstance(e.typ,tuple):
            # Constructing a container need not evaluate its numerical
            # projections. Selected numerical leaves carry their own domains.
            return lean(e,bindings,"construction",margin)
        if mode=="condition" and e.typ=="real":return f"({val(e)}).rOk"
        if k in ("eq","ne","lt","le","gt","ge") and e.args[0].typ=="real":
            return f"(({val(e.args[0])}).qOk ∧ ({val(e.args[1])}).qOk)"
        if k=="var":return bindings.get("__domain__"+e.value,"True") if mode=="condition" else "True"
        if k=="let":
            inner=dict(bindings);inner[e.value]=e.value
            inner["__domain__"+e.value]=rec(e.args[0])
            prefix=lean(e.args[0],bindings,"construction",margin)
            return f"({prefix} ∧ (let {e.value} : {lean_type(e.args[0].typ)} := {val(e.args[0])}; {lean(e.args[1],inner,mode,margin)}))"
        if k=="if":return f"({lean(e.args[0],bindings,'condition',margin)} ∧ (if {val(e.args[0])} then {rec(e.args[1])} else {rec(e.args[2])}))"
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
                init=val(e.args[0]);itval=iterator_value(it,bindings,margin);s="foldState"+state;x="foldItem"+ids[0]
                inner=dict(bindings);inner[state]=f"{s}.1"
                bind_lean_iterator(inner,ids,x,it)
                body=lean(e.args[1],inner,"value",margin);cond=lean(e.args[1],inner,mode,margin)
                pair=f"({itval}).foldl (fun ({s} : {lean_type(e.typ)} × Prop) {x} => ({body}, {s}.2 ∧ {cond})) ({init}, True)"
                return f"({rec(e.args[0])} ∧ {rec(it)} ∧ ({pair}).2)"
            op,gens=e.value
            if op in("all","any"):
                initial="(true, True)"if op=="all"else"(false, True)"
                def active(state):return f"{state}.1"if op=="all"else f"!{state}.1"
                def boolean_fold(i,scope,state):
                    if i==len(gens):
                        value=lean(e.args[0],scope,"value",margin)
                        condition=lean(e.args[0],scope,"condition",margin)
                        combine="&&"if op=="all"else"||"
                        return f"({state}.1 {combine} {value}, {state}.2 ∧ {condition})"
                    ids,it,filters=gens[i]
                    acc="qGuard"+ids[0];item="qItem"+ids[0];begin="qBegin"+ids[0]
                    inner=dict(scope);bind_lean_iterator(inner,ids,item,it)
                    def filtered(j,current):
                        if j==len(filters):return boolean_fold(i+1,inner,current)
                        next_state="qFilter"+ids[0]+"x"+str(j)
                        condition=lean(filters[j],inner,"condition",margin)
                        value=lean(filters[j],inner,"value",margin)
                        return f"(let {next_state} := ({current}.1, {current}.2 ∧ {condition}); if {value} then {filtered(j+1,next_state)} else {next_state})"
                    iterator_condition=lean(it,scope,"condition",margin)
                    values=iterator_value(it,scope,margin)
                    folded=f"({values}).foldl (fun {acc} {item} => if {active(acc)} then {filtered(0,acc)} else {acc}) {begin}"
                    return f"(if {active(state)} then (let {begin} := ({state}.1, {state}.2 ∧ {iterator_condition}); {folded}) else {state})"
                return f"(let qInitial : Bool × Prop := {initial}; ({boolean_fold(0,bindings,'qInitial')}).2)"
            def go(i,scope):
                if i==len(gens):return lean(e.args[0],scope,mode,margin)
                ids,it,filters=gens[i];x="condItem"+ids[0]
                inner=dict(scope);bind_lean_iterator(inner,ids,x,it)
                def filtered(j):
                    if j==len(filters):return go(i+1,inner)
                    condition=lean(filters[j],inner,"condition",margin)
                    value=lean(filters[j],inner,"value",margin)
                    return f"({condition} ∧ (if {value} then {filtered(j+1)} else True))"
                return f"({lean(it,scope,'condition',margin)} ∧ ∀ {x} ∈ {iterator_value(it,scope,margin)}, {filtered(0)})"
            result=go(0,bindings)
            if len(e.args)>1:result=f"({result} ∧ {rec(e.args[1])})"
            return result
        parts=[rec(x)for x in e.args]
        if k=="div" and mode=="condition":parts.append(f"({val(e.args[1])}).real ≠ 0")
        if k=="sqrt" and mode=="condition":parts.append(f"0 ≤ ({val(e.args[0])}).real")
        if k=="log" and mode=="condition":parts.append(f"0 < ({val(e.args[0])}).real")
        if k=="pow":parts.append(f"0 ≤ {val(e.args[1])}")
        if k in("mod","floordiv"):parts.append(f"0 < {val(e.args[1])}")
        if k=="range":parts.append(f"{val(e.args[2])} ≠ 0")
        if k in("minimum","maximum"):
            parts.append(f"0 < ({iterator_value(e.args[0],bindings,margin)}).length")
        # Exact rational branches require no separation condition.
        if k=="get":
            container,index=e.args
            if container.typ=="cell":
                parts.append(f"-(DIM : ℤ) ≤ {val(index)} ∧ {val(index)} < (DIM : ℤ)")
            if isinstance(container.typ,tuple)and container.typ[0]=="tuple"and index.kind!="const":
                length=len(container.typ[1])
                parts.append(f"-({length} : ℤ) ≤ {val(index)} ∧ {val(index)} < ({length} : ℤ)")
            if container.typ=="word"or(isinstance(container.typ,tuple)and container.typ[0]=="list"):
                parts.append(f"-(({val(container)}).length : ℤ) ≤ {val(index)} ∧ {val(index)} < (({val(container)}).length : ℤ)")
            if isinstance(container.typ,tuple)and container.typ[0]=="dict":
                keys=container.typ[3]
                if keys is not None:parts.append("("+" ∨ ".join(f"{val(index)} = {lean(Expr('const',index.typ,value=x),bindings)}"for x in keys)+")")
        return"("+" ∧ ".join(parts)+")"if parts else"True"
    if k=="const":
        if e.typ=="real":return f"(HM.ofRat (({integer_text(e.value.numerator)} : ℚ) / ({integer_text(e.value.denominator)} : ℚ)))"
        if e.typ=="int":return f"({integer_text(e.value)} : ℤ)"
        if e.typ=="bool":return"true"if e.value else"false"
        if e.typ=="pauli":return f"({v1._PAULIS[e.value]} : LocalPauli.Pauli)"
        import json
        return json.dumps(e.value,ensure_ascii=False)
    if k=="math_const":return "h_pi" if e.value == "pi" else "h_e"
    if k=="word":return"wordFactors p"
    if k=="var":return bindings[e.value]
    if k=="let":
        inner=dict(bindings);inner[e.value]=e.value
        inner["__domain__"+e.value]=lean(e.args[0],bindings,"condition",margin)
        body=lean(e.args[1],inner,mode,margin)
        prefix=lean(e.args[0],bindings,"construction",margin)
        body=mark_value(e.typ,body,prefix)
        return f"(let {e.value} : {lean_type(e.args[0].typ)} := {val(e.args[0])}; {body})"
    if k=="cast":return f"(HM.mark ({lean(e.args[0],bindings,"condition",margin)}) (HM.ofInt {val(e.args[0])}))"
    if k=="tuple":return"()"if not e.args else val(e.args[0])if len(e.args)==1 else"("+", ".join(val(x)for x in e.args)+")"
    if k=="list":return"["+", ".join(val(x)for x in e.args)+"]"
    if k=="dict":return val(e.args[0])if len(e.args)==1 else"("+", ".join(val(x)for x in e.args)+")"
    if k=="get":
        c,i=e.args;cv,iv=val(c),val(i)
        if c.typ=="cell":
            # Lean is total: the modulo only constructs a Fin on invalid
            # inputs. Such paths are explicitly excluded by the generated
            # domain condition, so a default value cannot certify Python OOB.
            selected=(f"(let indexedCell := {cv}; let cellIndex : ℤ := {iv}; "
                      f"indexedCell ⟨Int.toNat (if cellIndex < 0 then (DIM : ℤ) + cellIndex else cellIndex) % DIM, "
                      "Nat.mod_lt _ (by decide)⟩)")
            return selected
        if isinstance(c.typ,tuple)and c.typ[0]=="tuple":
            length=len(c.typ[1])
            if i.kind=="const":selected=tuple_get(cv,i.value%length,length)
            else:
                result=tuple_get("indexedTuple",length-1,length)
                for j in reversed(range(length-1)):
                    result=f"(if tupleIndex = ({j} : ℤ) ∨ tupleIndex = ({j-length} : ℤ) then {tuple_get('indexedTuple',j,length)} else {result})"
                selected=f"(let indexedTuple := {cv}; let tupleIndex : ℤ := {iv}; {result})"
            return mark_value(e.typ,selected,index_condition(c,i,bindings,margin))
        if isinstance(c.typ,tuple)and c.typ[0]=="dict":
            keys=c.typ[3]
            result=tuple_get(cv,len(keys)-1,len(keys))
            for j,key in reversed(list(enumerate(keys[:-1]))):
                result=f"(if {iv} = {lean(Expr('const',i.typ,value=key),bindings)} then {tuple_get(cv,j,len(keys))} else {result})"
            return mark_value(e.typ,result,index_condition(c,i,bindings,margin))
        selected=f"({cv})[Int.toNat (if {iv} < 0 then (({cv}).length : ℤ) + {iv} else {iv})]!"
        return mark_value(e.typ,selected,index_condition(c,i,bindings,margin))
    if k=="coord":return f"({val(e.args[0])} ⟨{e.value}, by decide⟩)"
    if k=="len":
        a=e.args[0]
        if isinstance(a.typ,tuple)and a.typ[0]in("tuple","dict"):
            return f"({len(a.typ[1])if a.typ[0]=='tuple'else len(a.typ[2])} : ℤ)"
        return f"(({val(a)}).length : ℤ)"
    if k=="range":return"(intRange "+" ".join(val(x)for x in e.args)+")"
    if k=="if":
        selected=f"(if {val(e.args[0])} then {val(e.args[1])} else {val(e.args[2])})"
        return mark_value(e.typ, selected, lean(e.args[0],bindings,"condition",margin))
    if k=="not":return f"(!{val(e.args[0])})"
    if k in("and","or"):return"("+(" && "if k=="and"else" || ").join(val(x)for x in e.args)+")"
    if k=="neg":return f"(-{val(e.args[0])})"
    if k=="abs":return f"(HM.abs {val(e.args[0])})" if e.typ=="real" else f"|{val(e.args[0])}|"
    if k in MATH_FUNCTIONS:return f"(h_{k} {val(e.args[0])})"
    if k in("add","sub","mul","div","mod","floordiv"):
        return f"({val(e.args[0])} "+{"add":"+","sub":"-","mul":"*","div":"/","mod":"%","floordiv":"/"}[k]+f" {val(e.args[1])})"
    if k=="pow":
        result=f"({val(e.args[0])} ^ Int.toNat {val(e.args[1])})"
        return mark_value(e.typ,result,f"({lean(e.args[1],bindings,'condition',margin)} ∧ 0 ≤ {val(e.args[1])})")
    if k in("eq","ne","lt","le","gt","ge"):
        left,right=val(e.args[0]),val(e.args[1])
        if e.args[0].typ=="real":left,right=f"({left}).rat",f"({right}).rat"
        return f"(decide ({left} "+{"eq":"=","ne":"≠","lt":"<","le":"≤","gt":">","ge":"≥"}[k]+f" {right}))"
    if k in("minimum","maximum"):
        a=e.args[0]
        if a.kind in("list","tuple"):xs=list(a.args)
        else:
            if a.typ[0]=="tuple":xs=[Expr("get",t,(a,Expr("const","int",value=i)))for i,t in enumerate(a.typ[1])]
            else:
                values=val(a)
                fn=("HM.minimum" if k=="minimum" else "HM.maximum") if e.typ=="real" else ("min" if k=="minimum" else "max")
                return f"(match {values} with | [] => 0 | x :: xs => xs.foldl {fn} x)"
        if not xs:error(None,"min/max need nonempty finite containers")
        result=lean(promote(xs[0],e.typ),bindings)
        fn=("HM.minimum" if k=="minimum" else "HM.maximum") if e.typ=="real" else ("min" if k=="minimum" else "max")
        for x in xs[1:]:result=f"({fn} {result} {lean(promote(x,e.typ),bindings)})"
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
            if gate:
                body=f"(if {gate} then {body} else {neutral})"
                if e.typ=="real":
                    condition=filters_condition(filters,inner,margin)
                    body=mark_value("real",body,condition)
            values=f"({iterator_value(it,scope,margin)}).map (fun {x} => {body})"
            if op in("sum","prod"):
                answer=f"({values}).{'sum'if op=='sum'else'prod'}"
                return mark_value(e.typ,answer,lean(it,scope,"condition",margin))
            return f"({values}).{'all'if op=='all'else'any'} (fun b => b)"
        result=emit(0,bindings)
        if len(e.args)>1:result=f"({result} {'+'if op=='sum'else'*'} {val(e.args[1])})"
        return result
    if k=="fold":
        state,it,ids=e.value;x="foldItem"+ids[0]
        inner=dict(bindings);inner[state]=state;bind_lean_iterator(inner,ids,x,it)
        answer=f"({iterator_value(it,bindings,margin)}).foldl (fun ({state} : {lean_type(e.typ)}) {x} => {lean(e.args[1],inner,'value',margin)}) {val(e.args[0])}"
        return mark_value(e.typ,answer,lean(it,bindings,"construction",margin))
    raise CorrelatorError(f"Cannot emit Lean expression {k}")


def bind_lean_iterator(scope,ids,name,iterator):
    if iterator.typ=="word":
        scope.update({ids[0]:name+".1",ids[1]:name+".2.1",ids[2]:name+".2.2"})
    else:scope[ids[0]]=name


def iterator_value(e,bindings,margin=None):
    if isinstance(e.typ,tuple)and e.typ[0]=="tuple":
        values=[Expr("get",t,(e,Expr("const","int",value=i)))for i,t in enumerate(e.typ[1])]
        return"["+", ".join(lean(x,bindings,"value",margin)for x in values)+"]"
    return lean(e,bindings,"value",margin)


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
    """One real-valued function with a fixed rational control-flow policy."""

    def __init__(self,source,d,q,expression,used_math,maths):
        self.source=source
        self.dimension=d
        self.qubits_per_cell=q
        self._expression=expression
        self.used_math=set(used_math)
        self.maths=maths
        self.source_sha256=hashlib.sha256(source.encode()).hexdigest()

    @classmethod
    def from_source(cls,source,dimension,qubits_per_cell,*,bits=40,terms=32):
        if type(source)is not str or len(source.encode())>65536:
            raise CorrelatorError("Source must be text within 64KiB")
        if type(dimension)is not int or not 1<=dimension<=32:
            raise CorrelatorError("dimension must be an integer in 1..32")
        if type(qubits_per_cell)is not int or not 1<=qubits_per_cell<=4096:
            raise CorrelatorError("qubits_per_cell must be an integer in 1..4096")
        maths=BranchMath(bits=bits,terms=terms)
        try:
            tree=ast.parse(source)
            if len(list(ast.walk(tree)))>4096:raise CorrelatorError("AST exceeds node limit")
            def depth(node):return 1+max((depth(c)for c in ast.iter_child_nodes(node)),default=0)
            if depth(tree)>96:raise CorrelatorError("AST exceeds depth limit")
            parser=Parser(source,dimension,maths)
            expression=parser.module(tree)
            return cls(source,dimension,qubits_per_cell,expression,parser.used_math,maths)
        except RecursionError as exc:raise CorrelatorError("Source exceeds recursion depth")from exc

    def normalize_word(self,word):
        return v1.CorrelatorProgram("",self.dimension,self.qubits_per_cell,None).normalize_word(word)

    def evaluate(self,word):
        try:
            value=evaluate(self._expression,self.normalize_word(word),{},[MAX_STEPS],self.maths)
            if isinstance(value,HybridReal):return value.value
            if type(value)is int:return float(value)
            raise CorrelatorError("f must return a numerical real value")
        except(ValueError,ArithmeticError,KeyError,IndexError,TypeError)as exc:
            if isinstance(exc,CorrelatorError):raise
            raise CorrelatorError(f"Exact evaluation rejected: {exc}")from exc

    def evaluate_float(self,word):
        return float(self.evaluate(word))

    def lean_definition(self,namespace="PythonCorrelator"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*",namespace):
            raise CorrelatorError("Invalid Lean namespace")
        d,q=self.dimension,self.qubits_per_cell
        body=lean(promote(self._expression,"real"),{})
        conditions=lean(self._expression,{},"condition")
        support=SUPPORT.replace("DIM",str(d)).replace("QUBITS",str(q))
        config=self.maths.maths.manifest()
        bits=config["precision_bits"];terms=config["series_terms"]
        wrappers=f"def h_sqrt : HM.Number → HM.Number := HM.sqrtWith {bits}\n"
        for name in ("exp","sin","cos","log","tanh"):
            wrappers+=f"def h_{name} : HM.Number → HM.Number := HM.{name}With {terms}\n"
        wrappers+=f"def h_pi : HM.Number := HM.piWith {terms}\n"
        wrappers+=f"def h_e : HM.Number := HM.eWith {terms}\n"
        source=("module\npublic import QuantumManyBodyVariational.TrustedStateSpecV2\n"
                "public import QuantumManyBodyVariational.HybridMath\n"
                "@[expose] public section\nnoncomputable section\n"
                f"namespace {namespace}\nopen scoped BigOperators\n"
                "open QuantumManyBodyVariational.InfiniteState\n"
                "open QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2\n"
                "open QuantumManyBodyVariational\n"
                f"-- {LANGUAGE_VERSION}; {COMPILER_REVISION}; source SHA256 {self.source_sha256}\n"+support+wrappers+
                f"\ndef fPair (p : Word (Site {d} {q})) : HM.Number := by\n  classical\n  exact {body}\n"
                f"\ndef fReal (p : Word (Site {d} {q})) : ℝ := (fPair p).real\n"
                f"def f : Correlator (Site {d} {q}) := fReal\n"
                f"\ndef NumericalPreconditions : Prop := ∀ p : Word (Site {d} {q}), {conditions}\n"
                f"def ProofObligation : Prop := StateCondition {d} {q} f ∧ NumericalPreconditions\n"
                f"end {namespace}\n")
        return source.replace("DIM",str(d)).replace("HM.","HybridMath.")

    def manifest(self):
        project=Path(__file__).resolve().parents[2]
        dependencies=["src/python_correlator_v2.py","src/python_correlator_compiler.py",
                      "src/correlator_hybrid_branch.py","src/correlator_rational_math.py",
                      "lean/QuantumManyBodyVariational/RationalMath.lean",
                      "lean/QuantumManyBodyVariational/HybridMath.lean"]
        return {"language_version":LANGUAGE_VERSION,"compiler_revision":COMPILER_REVISION,
                "python_source_sha256":self.source_sha256,
                "lean_definition_sha256":hashlib.sha256(self.lean_definition().encode()).hexdigest(),
                "compiler_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "dependencies":{name:hashlib.sha256((project/name).read_bytes()).hexdigest() for name in dependencies},
                "dimension":self.dimension,"qubits_per_cell":self.qubits_per_cell,
                "rational_math":self.maths.maths.manifest(),
                "branch_semantics_version":self.maths.manifest()["policy"],
                "value_semantics":"ideal_real_Lean_expression_with_approximate_numeric_evaluation",
                "branch_semantics":"same_exact_rational_expression_in_Lean_and_Python",
                "branch_policy":"all_numeric_comparisons_use_rational_shadows_no_margin",
                "index_semantics":"python_negative_integer_cell_and_homogeneous_tuple_with_path_sensitive_bounds",
                "compiler_correctness":"trusted_implementation_not_formally_proved",
                "numerical_error_certified":False,
                "proof_status":"state_condition_and_required_projection_domains_are_external_obligations",
                "limits":{"source_bytes":65536,"ast_nodes":4096,"depth":96,"expanded_nodes":32768,
                          "evaluation_steps":MAX_STEPS,"sequence_length":MAX_SEQUENCE,"integer_exponent":1024}}

