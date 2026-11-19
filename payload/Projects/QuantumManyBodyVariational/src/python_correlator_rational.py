"""Exact rational correlators with deterministic rational mathematical approximants.

The extended source syntax is shared with v2, but arithmetic and every branch
use exact rationals. Special functions denote fixed rational algorithms, not
the corresponding ideal transcendental functions.
"""
from __future__ import annotations
import ast
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
from . import python_correlator_v2 as syntax
from . import python_correlator_compiler as v1
from .correlator_rational_math import RationalMath

Expr=syntax.Expr
CorrelatorError=syntax.CorrelatorError
MATH_FUNCTIONS=syntax.MATH_FUNCTIONS
MAX_STEPS=syntax.MAX_STEPS
MAX_SEQUENCE=syntax.MAX_SEQUENCE
error=syntax.error
promote=syntax.promote
static=syntax.static
LANGUAGE_VERSION="python-correlator-rational-v1"


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
    if k=="const":return Fraction(e.value)if e.typ=="real"else e.value
    if k=="math_const":return maths.constant(e.value)
    if k=="word":return word
    if k=="var":return env[e.value]if e.value in env else ev(e.args[0])
    if k=="let":
        inner=dict(env);inner[e.value]=ev(e.args[0])
        return evaluate(e.args[1],word,inner,budget,maths)
    if k=="cast":return Fraction(ev(e.args[0]))
    if k in("tuple","list"):return tuple(ev(x)for x in e.args)
    if k=="dict":return dict(zip(e.value,(ev(x)for x in e.args)))
    if k=="get":
        container,index=ev(e.args[0]),ev(e.args[1])
        if type(index)not in(int,str):raise CorrelatorError("Container key must be discrete")
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
    if k in MATH_FUNCTIONS:return maths.approx(k,Fraction(ev(e.args[0])))
    if k in("add","sub","mul","div","mod","floordiv","pow"):
        a,b=ev(e.args[0]),ev(e.args[1])
        if k=="pow":
            if type(b)is not int or not 0<=b<=1024:raise CorrelatorError("Exponent must be an integer in 0..1024")
            return bounded(a**b)
        if k=="div":
            if b==0:raise CorrelatorError("Exact division by zero")
            return bounded(Fraction(a)/Fraction(b))
        if k in("mod","floordiv"):
            if type(a)is not int or type(b)is not int or b<=0:raise CorrelatorError("Integer // and % require positive integer divisor")
            return a%b if k=="mod"else a//b
        return bounded({"add":lambda:a+b,"sub":lambda:a-b,"mul":lambda:a*b}[k]())
    if k in("eq","ne","lt","le","gt","ge"):
        a,b=ev(e.args[0]),ev(e.args[1])
        return{"eq":lambda:a==b,"ne":lambda:a!=b,"lt":lambda:a<b,"le":lambda:a<=b,"gt":lambda:a>b,"ge":lambda:a>=b}[k]()
    if k in("minimum","maximum"):
        items=ev(e.args[0])
        if not items:raise CorrelatorError("min/max need a nonempty finite sequence")
        return(min if k=="minimum"else max)(items)
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
    if type(value)is Fraction:
        if max(abs(value.numerator).bit_length(),value.denominator.bit_length())>65536:raise CorrelatorError("Exact arithmetic exceeds rational bit limit")
        return value
    raise CorrelatorError("Numeric arithmetic left the rational domain")


class Parser(syntax.Parser):
    def __init__(self,source,dimension,maths):
        super().__init__(source,dimension,rational_mode=True)
        self.maths=maths
        self.rational_aliases=set()

    def static(self,e,node):
        syntax.check_size(e)
        try:return evaluate(e,(),{},[MAX_STEPS],self.maths)
        except(KeyError,ValueError,ArithmeticError)as exc:error(node,f"Expected a valid rational constant: {exc}")

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
    if isinstance(typ,str):return {"int":"ℤ","real":"ℚ","bool":"Bool","pauli":"LocalPauli.Pauli","str":"String","cell":"(Fin DIM → ℤ)","word":"List ((Fin DIM → ℤ) × ℤ × LocalPauli.Pauli)"}[typ]
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


def lean(e,bindings,mode="value",margin="(1 / 10000000000 : ℚ)"):
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
                init=val(e.args[0]);itval=iterator_value(it,bindings,margin);s="foldState";x="foldItem"
                inner=dict(bindings);inner[state]=f"{s}.1"
                bind_lean_iterator(inner,ids,x,it)
                body=lean(e.args[1],inner,"value",margin);cond=lean(e.args[1],inner,"condition",margin)
                pair=f"({itval}).foldl (fun {s} {x} => ({body}, {s}.2 ∧ {cond})) ({init}, True)"
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
                if i==len(gens):return lean(e.args[0],scope,"condition",margin)
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
        if k=="div":parts.append(f"{val(e.args[1])} ≠ 0")
        if k=="sqrt":parts.append(f"0 ≤ {val(e.args[0])}")
        if k=="log":parts.append(f"0 < {val(e.args[0])}")
        if k=="pow":parts.append(f"0 ≤ {val(e.args[1])}")
        if k in("mod","floordiv"):parts.append(f"0 < {val(e.args[1])}")
        if k=="range":parts.append(f"{val(e.args[2])} ≠ 0")
        if k in("minimum","maximum"):
            parts.append(f"0 < ({iterator_value(e.args[0],bindings,margin)}).length")
        # Exact rational branches require no separation condition.
        if k=="get":
            container,index=e.args
            if container.typ=="word"or(isinstance(container.typ,tuple)and container.typ[0]=="list"):
                parts.append(f"-(({val(container)}).length : ℤ) ≤ {val(index)} ∧ {val(index)} < (({val(container)}).length : ℤ)")
            if isinstance(container.typ,tuple)and container.typ[0]=="dict":
                keys=container.typ[3]
                if keys is not None:parts.append("("+" ∨ ".join(f"{val(index)} = {lean(Expr('const',index.typ,value=x),bindings)}"for x in keys)+")")
        return"("+" ∧ ".join(parts)+")"if parts else"True"
    if k=="const":
        if e.typ=="real":return f"(({integer_text(e.value.numerator)} : ℚ) / ({integer_text(e.value.denominator)} : ℚ))"
        if e.typ=="int":return f"({integer_text(e.value)} : ℤ)"
        if e.typ=="bool":return"true"if e.value else"false"
        if e.typ=="pauli":return f"({v1._PAULIS[e.value]} : LocalPauli.Pauli)"
        import json
        return json.dumps(e.value,ensure_ascii=False)
    if k=="math_const":return "q_pi" if e.value == "pi" else "q_e"
    if k=="word":return"wordFactors p"
    if k=="var":return bindings[e.value]
    if k=="let":
        inner=dict(bindings);inner[e.value]=e.value
        return f"(let {e.value} : {lean_type(e.args[0].typ)} := {val(e.args[0])}; {lean(e.args[1],inner,mode,margin)})"
    if k=="cast":return f"(({val(e.args[0])}) : ℚ)"
    if k=="tuple":return"()"if not e.args else val(e.args[0])if len(e.args)==1 else"("+", ".join(val(x)for x in e.args)+")"
    if k=="list":return"["+", ".join(val(x)for x in e.args)+"]"
    if k=="dict":return val(e.args[0])if len(e.args)==1 else"("+", ".join(val(x)for x in e.args)+")"
    if k=="get":
        c,i=e.args;cv,iv=val(c),val(i)
        if isinstance(c.typ,tuple)and c.typ[0]=="tuple":
            index=i.value if i.kind=="const"else static(i,None)
            return tuple_get(cv,index%len(c.typ[1]),len(c.typ[1]))
        if isinstance(c.typ,tuple)and c.typ[0]=="dict":
            keys=c.typ[3]
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
    if k in MATH_FUNCTIONS:return f"(q_{k} {val(e.args[0])})"
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
            else:
                values=val(a)
                return f"(match {values} with | [] => 0 | x :: xs => xs.foldl {'min'if k=='minimum'else'max'} x)"
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
            values=f"({iterator_value(it,scope,margin)}).map (fun {x} => {body})"
            if op in("sum","prod"):return f"({values}).{'sum'if op=='sum'else'prod'}"
            return f"({values}).{'all'if op=='all'else'any'} (fun b => b)"
        result=emit(0,bindings)
        if len(e.args)>1:result=f"({result} {'+'if op=='sum'else'*'} {val(e.args[1])})"
        return result
    if k=="fold":
        state,it,ids=e.value;x="foldItem"+ids[0]
        inner=dict(bindings);inner[state]=state;bind_lean_iterator(inner,ids,x,it)
        return f"({iterator_value(it,bindings,margin)}).foldl (fun {state} {x} => {lean(e.args[1],inner,'value',margin)}) {val(e.args[0])}"
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
    """One exact rational function, a rational backend, and its fixed Lean definition."""

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
        maths=RationalMath(precision_bits=bits,terms=terms)
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
            if type(value)not in(int,Fraction):raise CorrelatorError("f must return a rational number")
            return bounded(Fraction(value))
        except(ValueError,ArithmeticError,KeyError,IndexError,TypeError)as exc:
            if isinstance(exc,CorrelatorError):raise
            raise CorrelatorError(f"Exact evaluation rejected: {exc}")from exc

    def evaluate_float(self,word):
        return float(self.evaluate(word))

    def lean_definition(self,namespace="PythonCorrelator"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*",namespace):
            raise CorrelatorError("Invalid Lean namespace")
        d,q=self.dimension,self.qubits_per_cell
        body=lean(promote(self._expression,"real"),{},margin=None)
        conditions=lean(self._expression,{},"condition",None)
        support=SUPPORT.replace("DIM",str(d)).replace("QUBITS",str(q))
        config=self.maths.manifest()
        bits=config.get("bits",config.get("precision_bits",40))
        terms=config.get("terms",config.get("series_terms",32))
        wrappers=f"def q_sqrt : ℚ → ℚ := QuantumManyBodyVariational.RationalMath.sqrtWith {bits}\n"
        for name in("exp","sin","cos","log","tanh"):
            wrappers+=f"def q_{name} : ℚ → ℚ := QuantumManyBodyVariational.RationalMath.{name}With {terms}\n"
        wrappers+=f"def q_pi : ℚ := QuantumManyBodyVariational.RationalMath.piWith {terms}\n"
        wrappers+=f"def q_e : ℚ := QuantumManyBodyVariational.RationalMath.eWith {terms}\n"
        source=("module\npublic import QuantumManyBodyVariational.TrustedStateSpecV2\n"
                "public import QuantumManyBodyVariational.RationalMath\n"
                "@[expose] public section\nnoncomputable section\n"
                f"namespace {namespace}\nopen scoped BigOperators\n"
                "open QuantumManyBodyVariational.InfiniteState\n"
                "open QuantumManyBodyVariational.InfiniteState.TrustedStateSpecV2\n"
                f"-- {LANGUAGE_VERSION}; source SHA256 {self.source_sha256}\n"+support+wrappers+
                f"\ndef fRat (p : Word (Site {d} {q})) : ℚ := by\n  classical\n  exact {body}\n"
                f"\ndef fReal (p : Word (Site {d} {q})) : ℝ := (fRat p : ℝ)\n"
                f"def f : Correlator (Site {d} {q}) := fReal\n"
                f"theorem f_eq_cast (p : Word (Site {d} {q})) : f p = (fRat p : ℝ) := rfl\n"
                f"\ndef NumericalPreconditions : Prop := ∀ p : Word (Site {d} {q}), {conditions}\n"
                f"def ProofObligation : Prop := StateCondition {d} {q} f ∧ NumericalPreconditions\n"
                f"end {namespace}\n")
        return source.replace("DIM",str(d))

    def manifest(self):
        backend=Path(__file__).with_name("correlator_rational_math.py")
        frontend=Path(__file__).with_name("python_correlator_v2.py")
        lean_backend=Path(__file__).resolve().parents[1]/"lean/QuantumManyBodyVariational/RationalMath.lean"
        return{"language_version":LANGUAGE_VERSION,"python_source_sha256":self.source_sha256,
               "lean_definition_sha256":hashlib.sha256(self.lean_definition().encode()).hexdigest(),
               "compiler_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "frontend_sha256":hashlib.sha256(frontend.read_bytes()).hexdigest(),
               "rational_backend_sha256":hashlib.sha256(backend.read_bytes()).hexdigest(),
               "lean_rational_backend_sha256":hashlib.sha256(lean_backend.read_bytes()).hexdigest()if lean_backend.exists()else None,
               "dimension":self.dimension,"qubits_per_cell":self.qubits_per_cell,
               "rational_math":self.maths.manifest(),
               "number_semantics":"exact_rational_arithmetic_and_exact_comparisons",
               "special_function_semantics":"fixed_deterministic_rational_algorithms_not_ideal_transcendentals",
               "branch_policy":"unrestricted_exact_rational_comparisons",
               "compiler_correctness":"trusted_implementation_not_formally_proved",
               "proof_status":"state_condition_and_numerical_domains_are_external_obligations",
               "limits":{"source_bytes":65536,"ast_nodes":4096,"depth":96,"expanded_nodes":32768,
                         "evaluation_steps":MAX_STEPS,"sequence_length":MAX_SEQUENCE,"integer_exponent":1024,"rational_bits":65536}}

