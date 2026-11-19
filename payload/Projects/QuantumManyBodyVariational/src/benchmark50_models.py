"""Frozen finite-range Pauli interactions for the 50-model benchmark.

Cells are integer d-tuples, species are 0-based qubit indices. All coefficients
are exact rationals. H is an interaction family, not a bounded infinite sum.
One listed h0 is summed over every cell; energy per qubit is <h0>/q.
"""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict
from fractions import Fraction as F
from itertools import combinations, permutations, product
from math import prod
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CATALOG = PROJECT / "config/benchmark50_tasks.json"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def task_digest(task):
    return hashlib.sha256(canonical_json({k:v for k,v in task.items() if k != "canonical_sha256"}).encode()).hexdigest()


def site(cell, qubit=0):
    return (tuple(cell), qubit)


def shift(s, delta):
    return (tuple(a+b for a,b in zip(s[0], delta)), s[1])


def word_key(word):
    return tuple(sorted((tuple(p["cell"]), p["qubit"], p["pauli"]) for p in word))


class Builder:
    def __init__(self, number, name, d, q, parameters, lattice, label, sources):
        self.task = dict(id=f"{number:02d}", name=name, dimension=d, qubits_per_cell=q,
                         parameters={k:str(F(v)) for k,v in parameters.items()},
                         lattice=lattice, solvability=label, sources=sources,
                         energy_normalization="per_qubit", terms=[])
        self.terms = defaultdict(F)
        self.o = (0,)*d
    def add(self, coefficient, *factors):
        key = tuple(sorted((tuple(s[0]), s[1], p) for s,p in factors))
        if len({(c,a) for c,a,p in key}) != len(key):
            raise ValueError("Repeated site in explicit Hermitian Pauli word")
        self.terms[key] += F(coefficient)
    def pair(self, a, b, coupling=1, axes="XYZ"):
        for p in axes: self.add(F(coupling)/4, (a,p),(b,p))
    def field(self, coefficient, axis):
        for a in range(self.task["qubits_per_cell"]): self.add(coefficient,(site(self.o,a),axis))
    def finish(self):
        self.task["terms"] = [dict(coefficient=str(v),word=[dict(cell=list(c),qubit=a,pauli=p) for c,a,p in k])
                              for k,v in sorted(self.terms.items()) if v]
        self.task["canonical_sha256"] = task_digest(self.task)
        return self.task


def rational_product(*polys):
    """Multiply commuting diagonal Pauli polynomials (support -> coefficient)."""
    out={frozenset():F(1)}
    for poly in polys:
        nxt=defaultdict(F)
        for a,c in out.items():
            for b,d in poly.items(): nxt[a^b] += c*d
        out={k:v for k,v in nxt.items() if v}
    return out


def singlet(a,b):
    return [(F(1,4),()),*[(F(-1,4),((a,p),(b,p))) for p in "XYZ"]]


def add_projector_product(b, coefficient, bonds):
    terms=[(F(coefficient),())]
    for a,c in bonds:
        terms=[(v*w,fs+gs) for v,fs in terms for w,gs in singlet(a,c)]
    for v,fs in terms: b.add(v,*fs)


def periodic_edges(positions, metric, distance):
    """Undirected nearest bonds with one endpoint in the reference cell."""
    d=len(positions[0]); result=[]
    for a,p in enumerate(positions):
        for b,q in enumerate(positions):
            for delta in product(range(-1,2),repeat=d):
                v=tuple(F(q[i])+delta[i]-F(p[i]) for i in range(d))
                norm=sum(v[i]*metric[i][j]*v[j] for i in range(d) for j in range(d))
                if norm != distance: continue
                # Positive translation; within a cell retain only a < b.
                if delta < (0,)*d or (not any(delta) and a>=b): continue
                result.append((site((0,)*d,a),site(delta,b)))
    return result


def grid_site(coords, shape):
    return site(tuple(x//n for x,n in zip(coords,shape)),
                sum((x%n)*prod(shape[:i]) for i,(x,n) in enumerate(zip(coords,shape))))


def _sources(*ids): return ["https://arxiv.org/abs/"+i for i in ids]


def build_tasks():
    tasks=[]
    def new(n,name,d,q,params,lattice,label,*sources): return Builder(n,name,d,q,params,lattice,label,list(sources))
    def done(b): tasks.append(b.finish())
    for n,name in [(1,"Transverse-field Ising chain"),(2,"Mixed-field Ising chain"),(3,"ANNNI chain")]:
        pars={"J":1,"g":1} if n!=3 else {"J1":1,"J2":"1/2","g":"3/4"}
        if n==2: pars["h"]="1/2"
        sources = (["https://www.math.ucdavis.edu/~bxn/lieb1961.pdf"] if n==1
                   else _sources("2307.16703" if n==2 else "cond-mat/9604051"))
        b=new(n,name,1,1,pars,"integer chain", "E" if n==1 else "V", *sources)
        b.add(-1,(site((0,)),"Z"),(site((1,)),"Z"));b.field(-F(pars["g"]),"X")
        if n==2:b.field("-1/2","Z")
        if n==3:b.add("1/2",(site((0,)),"Z"),(site((2,)),"Z"))
        done(b)
    b=new(4,"Anisotropic XY chain",1,1,{"Jx":1,"Jy":"1/2","h":"3/4"},"integer chain","E","https://www.math.ucdavis.edu/~bxn/lieb1961.pdf")
    for p,c in [("X",-1),("Y",F(-1,2))]:b.add(c,(site((0,)),p),(site((1,)),p))
    b.field("-3/4","Z");done(b)
    b=new(5,"Longitudinal-field XXZ chain",1,1,{"Jperp":1,"Jz":"3/2","h":"1/3"},"integer chain","E","https://doi.org/10.1103/PhysRev.150.327")
    for p,c in [("X",1),("Y",1),("Z",F(3,2))]:b.add(c,(site((0,)),p),(site((1,)),p))
    b.field("-1/3","Z");done(b)
    b=new(6,"Zero-field XYZ chain",1,1,{"Jx":1,"Jy":"3/4","Jz":"1/2"},"integer chain","E","https://doi.org/10.1103/PhysRevLett.26.834")
    for p,c in [("X",1),("Y",F(3,4)),("Z",F(1,2))]:b.add(c,(site((0,)),p),(site((1,)),p))
    done(b)
    b=new(7,"J1-J2 Heisenberg chain (Majumdar-Ghosh point)",1,1,{"J1":1,"J2":"1/2"},"integer chain","P",*_sources("cond-mat/0205072"))
    b.pair(site((0,)),site((1,)));b.pair(site((0,)),site((2,)),F(1,2));done(b)
    b=new(8,"Cluster chain",1,1,{"K":1},"integer chain","E",*_sources("quant-ph/0004051"))
    b.add(-1,(site((-1,)),"Z"),(site((0,)),"X"),(site((1,)),"Z"));done(b)
    b=new(9,"Cluster-Ising chain",1,1,{"K":1,"lambda":"3/4"},"integer chain","E",*_sources("1503.08598"))
    b.add(-1,(site((-1,)),"X"),(site((0,)),"Z"),(site((1,)),"X"));b.add("3/4",(site((0,)),"Y"),(site((1,)),"Y"));done(b)
    b=new(10,"Alternating compass chain",1,2,{"Jx":1,"Jz":"3/4"},"two-site chain cell","E",*_sources("cond-mat/0702369"))
    b.add(-1,(site((0,),0),"X"),(site((0,),1),"X"));b.add("-3/4",(site((0,),1),"Z"),(site((1,),0),"Z"));done(b)
    b=new(11,"J-Q2 chain",1,1,{"J":1,"Q":2},"integer chain","V","https://iaizzi.me/wp-content/uploads/2019/01/physrevb.95.174436.pdf")
    add_projector_product(b,-1,[(site((0,)),site((1,)))]);add_projector_product(b,-2,[(site((0,)),site((1,))),(site((2,)),site((3,)))]);done(b)
    b=new(12,"Two-leg Heisenberg ladder",1,2,{"Jparallel":1,"Jperp":1},"one rung per cell","V","https://doi.org/10.1103/PhysRevLett.73.886")
    for a in range(2):b.pair(site((0,),a),site((1,),a))
    b.pair(site((0,),0),site((0,),1));done(b)

    O=(0,0);e1=(1,0);e2=(0,1);square=[e1,e2];tri=[e1,e2,(1,-1)]
    kag_bonds=[(site(O,0),site(O,1)),(site(O,0),site(O,2)),(site(O,1),site(O,2)),(site(O,0),site((-1,0),1)),(site(O,0),site((0,-1),2)),(site(O,1),site((1,-1),2))]
    kag_hex=[site(O,1),site((1,-1),2),site((1,-1),0),site((0,-1),1),site((0,-1),2),site(O,0)]
    honey=[("Z",site(O,0),site(O,1)),("X",site(O,0),site((-1,0),1)),("Y",site(O,0),site((0,-1),1))]
    for n,name,deltas,c,g in [(13,"Square TFIM",square,-1,3),(14,"Triangular AF TFIM",tri,1,1)]:
        b=new(n,name,2,1,{"J":1,"g":g},"square" if n==13 else "triangular primitive basis","V",*_sources("2005.13592" if n==13 else "cond-mat/0011250"))
        for delta in deltas:b.add(c,(site(O),"Z"),(site(delta),"Z"))
        b.field(-g,"X");done(b)
    b=new(15,"Square J1-J2 Heisenberg",2,1,{"J1":1,"J2":"1/2"},"square; NNN both diagonals","V",*_sources("0909.3723"))
    for delta in square:b.pair(site(O),site(delta))
    for delta in [(1,1),(1,-1)]:b.pair(site(O),site(delta),F(1,2))
    done(b)
    b=new(16,"Triangular AF Heisenberg",2,1,{"J":1},"triangular primitive basis","V",*_sources("0705.2746"))
    for delta in tri:b.pair(site(O),site(delta))
    done(b)
    b=new(17,"Kagome AF Heisenberg",2,3,{"J":1},"triangular basis; positions A=0,B=a1/2,C=a2/2","V",*_sources("1011.6114"))
    for a,c in kag_bonds:b.pair(a,c)
    done(b)
    b=new(18,"Shastry-Sutherland",2,4,{"J":1,"Jd":2},"2x2 square cell; species x+2y; diagonals on even-even and odd-odd plaquettes","P","https://doi.org/10.1016/0378-4363(81)90838-X")
    for x,y in product(range(2),repeat=2):
        for dx,dy in square:b.pair(grid_site((x,y),(2,2)),grid_site((x+dx,y+dy),(2,2)))
    b.pair(grid_site((0,0),(2,2)),grid_site((1,1),(2,2)),2)
    b.pair(grid_site((2,1),(2,2)),grid_site((1,2),(2,2)),2);done(b)
    b=new(19,"Square J-Q",2,1,{"J":1,"Q":2},"square; both opposite-edge products on every plaquette","V",*_sources("cond-mat/0611343"))
    for delta in square:add_projector_product(b,-1,[(site(O),site(delta))])
    corners=[site(O),site(e1),site((1,1)),site(e2)]
    for a,c,d,e in [(0,1,2,3),(1,2,3,0)]:add_projector_product(b,-2,[(corners[a],corners[c]),(corners[d],corners[e])])
    done(b)
    b=new(20,"Square compass",2,1,{"Jx":1,"Jz":1},"square","V",*_sources("cond-mat/0501708"))
    for delta,p in [(e1,"X"),(e2,"Z")]:b.add(-1,(site(O),p),(site(delta),p))
    done(b)
    b=new(21,"Xu-Moore quantum plaquette Ising",2,1,{"K":1,"h":1},"square; one plaquette per cell","V",*_sources("cond-mat/0312587"))
    b.add(-1,*[(s,"Z") for s in corners]);b.field(-1,"X");done(b)
    b=new(22,"Honeycomb Kitaev",2,2,{"Kx":1,"Ky":1,"Kz":1},"honeycomb brick convention: z intra,x B[-1,0],y B[0,-1]","E",*_sources("cond-mat/0506438"))
    for p,a,c in honey:b.add(-1,(a,p),(c,p))
    done(b)
    b=new(23,"Honeycomb Heisenberg-Kitaev-Gamma",2,2,{"J":"1/2","K":-1,"Gamma":"1/2"},"honeycomb same bond convention as ID22","V",*_sources("1310.7940"))
    for p,a,c in honey:
        b.pair(a,c,F(1,2));b.add("-1/4",(a,p),(c,p));u,v=[t for t in "XYZ" if t!=p]
        b.add("1/8",(a,u),(c,v));b.add("1/8",(a,v),(c,u))
    done(b)
    b=new(24,"Yao-Kivelson decorated honeycomb",2,6,{"Jx":1,"Jy":1,"Jz":1,"Jprime":"1/2"},"A/B triangles; species 3*s+alpha, alpha=x,y,z; missing-color internal edge; external inherits honeycomb color","E",*_sources("0708.0040"))
    for s in range(2):
        for a,p in enumerate("XYZ"):
            u,v=[i for i in range(3) if i!=a];b.add(-1,(site(O,3*s+u),p),(site(O,3*s+v),p))
    for a,p in enumerate("XYZ"):
        delta={"X":(-1,0),"Y":(0,-1),"Z":O}[p];b.add("-1/2",(site(O,a),p),(site(delta,3+a),p))
    done(b)
    b=new(25,"2D toric code",2,2,{"Je":1,"Jm":1},"square edges: species0 +x,1 +y starting at cell","E",*_sources("quant-ph/9707021"))
    star=[site(O,0),site((-1,0),0),site(O,1),site((0,-1),1)]
    face=[site(O,0),site(e1,1),site(e2,0),site(O,1)]
    b.add(-1,*[(s,"X") for s in star]);b.add(-1,*[(s,"Z") for s in face]);done(b)
    b=new(26,"Wen plaquette",2,1,{"J":1},"square vertices cyclic XYXY","E",*_sources("quant-ph/0205004"))
    b.add(-1,*list(zip(corners,"XYXY")));done(b)
    b=new(27,"Honeycomb color code",2,2,{"JX":1,"JZ":1},"honeycomb ID22 graph; one hexagon per cell; vertex qubits","E",*_sources("quant-ph/0605138"))
    hexagon=[site(O,0),site(O,1),site(e1,0),site((1,-1),1),site((1,-1),0),site((0,-1),1)]
    for p in "XZ":b.add(-1,*[(s,p) for s in hexagon])
    done(b)
    b=new(28,"Square cluster",2,1,{"J":1},"square vertices","E",*_sources("quant-ph/0004051"))
    b.add(-1,(site(O),"X"),*[(site(delta),"Z") for delta in [(1,0),(-1,0),(0,1),(0,-1)]]);done(b)
    b=new(29,"Levin-Gu triangular Ising SPT",2,1,{"J":1},"six neighbors cyclic a1,a2,a2-a1,-a1,-a2,a1-a2; original Bp minus sign preserved","E",*_sources("1202.3120"))
    ring=[(1,0),(0,1),(-1,1),(-1,0),(0,-1),(1,-1)]
    # -Bp = +Xp*(-1)^(domainwalls/2). Exact Walsh expansion.
    for mask in range(64):
        c=F(0)
        for bits in product((0,1),repeat=6):
            walls=sum(bits[i]!=bits[(i+1)%6] for i in range(6));phase=(-1)**(walls//2)
            c+=F(phase*(-1)**sum(bits[i] for i in range(6) if mask>>i&1),64)
        if c:b.add(c,(site(O),"X"),*[(site(ring[i]),"Z") for i in range(6) if mask>>i&1])
    done(b)
    b=new(30,"Union-Jack CCZ SPT",2,2,{"J":1},"A square vertex at integer r; B face center r+(1/2,1/2); four triangles per square","E",*_sources("1508.02695"))
    def cz(a,c):return {frozenset():F(1,2),frozenset([a]):F(1,2),frozenset([c]):F(1,2),frozenset([a,c]):F(-1,2)}
    for species in range(2):
        links=[]
        if species==1:links=[(corners[i],corners[(i+1)%4]) for i in range(4)]
        else:
            for x,y in product((-1,0),repeat=2):
                vs=[site((x,y)),site((x+1,y)),site((x+1,y+1)),site((x,y+1))]
                for i in range(4):
                    a,c=vs[i],vs[(i+1)%4]
                    if a==site(O):links.append((site((x,y),1),c))
                    if c==site(O):links.append((site((x,y),1),a))
        poly=rational_product(*(cz(a,c) for a,c in links))
        for zs,c in poly.items():b.add(-c,(site(O,species),"X"),*[(s,"Z") for s in sorted(zs)])
    done(b)
    b=new(31,"Transverse-field Baxter-Wu",2,1,{"J":1,"h":1},"triangular basis; both upward and downward triangles","V",*_sources("1403.1406"))
    for triangle in [[O,e1,e2],[e1,(1,1),e2]]:b.add(-1,*[(site(x),"Z") for x in triangle])
    b.field(-1,"X");done(b)
    b=new(32,"BFG easy-axis kagome",2,3,{"Jz":1,"Jperp":"1/10"},"kagome ID17; every hexagon all 15 pair interactions; exact additive constant retained","V",*_sources("cond-mat/0110005"))
    b.add("3/2")
    for a,c in combinations(kag_hex,2):
        b.add("1/2",(a,"Z"),(c,"Z"))
        for p in "XY":b.add("1/20",(a,p),(c,p))
    done(b)
    b=new(33,"Kagome Heisenberg plus scalar chirality",2,3,{"J":1,"Jchi":"1/2"},"kagome ID17; oriented triangles A0 B0 C0 and A0 B[-1,0] C[0,-1] are both CCW","V",*_sources("1401.3017"))
    for a,c in kag_bonds:b.pair(a,c)
    for triangle in [[site(O,0),site(O,1),site(O,2)],[site(O,0),site((-1,0),1),site((0,-1),2)]]:
        for perm in permutations(range(3)):
            inversions=sum(perm[i]>perm[j] for i in range(3) for j in range(i+1,3))
            b.add(F((-1)**inversions,16),*[(s,"XYZ"[p]) for s,p in zip(triangle,perm)])
    done(b)

    O=(0,0,0);axes=[(1,0,0),(0,1,0),(0,0,1)]
    b=new(34,"Simple cubic TFIM",3,1,{"J":1,"g":5},"simple cubic","V",*_sources("2005.13592"))
    for delta in axes:b.add(-1,(site(O),"Z"),(site(delta),"Z"))
    b.field(-5,"X");done(b)
    b=new(35,"Simple cubic J1-J2 Heisenberg",3,1,{"J1":1,"J2":"1/2"},"simple cubic; NNN all face diagonals","V",*_sources("0910.0638"))
    for delta in axes:b.pair(site(O),site(delta))
    for a,c in combinations(range(3),2):
        for sign in (-1,1):
            v=[0]*3;v[a]=1;v[c]=sign;b.pair(site(O),site(v),F(1,2))
    done(b)
    b=new(36,"BCC J1-J2 Heisenberg",3,1,{"J1":1,"J2":"2/3"},"primitive a1=(1,1,-1)/2,a2=(-1,1,1)/2,a3=(1,-1,1)/2","V",*_sources("cond-mat/0203085"))
    for delta in axes+[(1,1,1)]:b.pair(site(O),site(delta))
    for delta in [(1,1,0),(1,0,1),(0,1,1)]:b.pair(site(O),site(delta),F(2,3))
    done(b)
    b=new(37,"Cubic compass",3,1,{"Jx":1,"Jy":1,"Jz":1},"simple cubic positive couplings","V",*_sources("1010.3804"))
    for delta,p in zip(axes,"XYZ"):b.add(1,(site(O),p),(site(delta),p))
    done(b)
    b=new(38,"Cubic transverse-field plaquette Ising",3,1,{"K":1,"g":1},"all xy,yz,zx elementary plaquettes","V",*_sources("1603.04442"))
    for a,c in combinations(axes,2):b.add(-1,*[(site(v),"Z") for v in [O,a,c,tuple(x+y for x,y in zip(a,c))]])
    b.field(-1,"X");done(b)
    b=new(39,"Pyrochlore XXZ quantum spin ice",3,4,{"Jz":1,"Jperp":"-1/5"},"FCC a1=(0,1,1)/2,a2=(1,0,1)/2,a3=(1,1,0)/2; positions 0,a1/2,a2/2,a3/2; abstract qubit axes","V",*_sources("cond-mat/0305401"))
    pos=[(0,0,0),(F(1,2),0,0),(0,F(1,2),0),(0,0,F(1,2))]
    metric=[[F(1,2) if i==j else F(1,4) for j in range(3)] for i in range(3)]
    edges=periodic_edges(pos,metric,F(1,8));assert len(edges)==12
    for a,c in edges:
        b.add("1/4",(a,"Z"),(c,"Z"))
        for p in "XY":b.add("-1/20",(a,p),(c,p))
    done(b)
    b=new(40,"Dimerized diamond Heisenberg",3,2,{"JD":2,"J":1},"FCC primitive labels; A0-B0 strong, A0-B[-e_mu] weak","V",*_sources("2410.13706"))
    b.pair(site(O,0),site(O,1),2)
    for delta in axes:b.pair(site(O,0),site(tuple(-x for x in delta),1))
    done(b)
    b=new(41,"Hyperhoneycomb Kitaev",3,4,{"Jx":1,"Jy":1,"Jz":1},"four-site primitive cell, explicit original-paper six-bond convention","G",*_sources("0801.0229"))
    colored=[("X",0,3,(0,0,-1)),("X",1,2,O),("Y",0,3,(1,0,-1)),("Y",2,1,(0,1,0)),("Z",0,1,O),("Z",2,3,O)]
    for p,a,c,v in colored:b.add(-1,(site(O,a),p),(site(v,c),p))
    done(b)
    b=new(42,"Hyperoctagon Kitaev",3,4,{"Jx":1,"Jy":1,"Jz":1},"BCC primitive a1=(1,0,0),a2=(1,1,-1)/2,a3=(1,1,1)/2; explicit six bonds","G","https://www.thp.uni-koeln.de/trebst/pubs/HyperoctagonSpinonFermiSurface.pdf")
    colored=[("X",0,2,(0,-1,0)),("X",1,3,(0,0,-1)),("Y",0,1,O),("Y",2,3,O),("Z",1,2,O),("Z",0,3,(-1,0,0))]
    for p,a,c,v in colored:b.add(-1,(site(O,a),p),(site(v,c),p))
    done(b)
    b=new(43,"Hyperkagome AF Heisenberg",3,12,{"J":1},"simple cubic primitive cell; exact positions from arXiv:2311.08210 Eq1, modulo cells; NN squared distance 1/8","V",*_sources("2311.08210"))
    coords=[(-2,0,2),(-1,3,2),(-2,3,1),(-1,1,0),(-2,1,3),(-1,2,3),(-3,2,1),(0,2,2),(0,1,1),(-3,3,0),(0,0,0),(-3,0,3)]
    positions=[tuple(F(x%4,4) for x in p) for p in coords]
    b.task["positions_in_primitive_basis"]=[[str(x) for x in p] for p in positions]
    edges=periodic_edges(positions,[[int(i==j) for j in range(3)] for i in range(3)],F(1,8));assert len(edges)==24
    for a,c in edges:b.pair(a,c)
    done(b)
    b=new(44,"Cubic cluster",3,1,{"J":1},"simple cubic vertices","E",*_sources("quant-ph/0004051"))
    b.add(-1,(site(O),"X"),*[(site(tuple(sign*x for x in delta)),"Z") for delta in axes for sign in (-1,1)]);done(b)
    b=new(45,"3D toric code",3,3,{"Je":1,"Jm":1},"species mu is +mu edge starting at reference vertex","E",*_sources("cond-mat/0411752"))
    b.add(-1,*[(site(v,mu),"X") for mu,delta in enumerate(axes) for v in [O,tuple(-x for x in delta)]])
    for mu,nu in combinations(range(3),2):
        b.add(-1,(site(O,mu),"Z"),(site(axes[nu],mu),"Z"),(site(O,nu),"Z"),(site(axes[mu],nu),"Z"))
    done(b)
    b=new(46,"X-cube",3,3,{"Jc":1,"Jv":1},"cubic edge species as ID45; X cube, Z planar crosses","E",*_sources("1603.04442"))
    cube=[]
    for mu in range(3):
        other=[i for i in range(3) if i!=mu]
        for bits in product((0,1),repeat=2):
            v=[0]*3
            for a,x in zip(other,bits):v[a]=x
            cube.append((site(v,mu),"X"))
    b.add(-1,*cube)
    for normal in range(3):
        b.add(-1,*[(site(v,mu),"Z") for mu in range(3) if mu!=normal for v in [O,tuple(-x for x in axes[mu])]])
    done(b)
    b=new(47,"Checkerboard fracton",3,8,{"JX":1,"JZ":1},"2x2x2 cubic cell; species x+2y+4z; cubes lower-corner parity even","E",*_sources("1806.08633"))
    for origin in product((0,1),repeat=3):
        if sum(origin)%2:continue
        cube=[grid_site(tuple(x+y for x,y in zip(origin,v)),(2,2,2)) for v in product((0,1),repeat=3)]
        for p in "XZ":b.add(-1,*[(s,p) for s in cube])
    done(b)
    b=new(48,"Haah cubic code 1",3,2,{"JX":1,"JZ":1},"F={0,ex,ey,ez},G={0,ex+ey,ey+ez,ez+ex}; Z uses reciprocal supports AND F/G swap","E",*_sources("1101.1962"))
    fs=[O,*axes];gs=[O,(1,1,0),(0,1,1),(1,0,1)]
    b.add(-1,*[(site(v,0),"X") for v in fs],*[(site(v,1),"X") for v in gs])
    b.add(-1,*[(site(tuple(-x for x in v),0),"Z") for v in gs],*[(site(tuple(-x for x in v),1),"Z") for v in fs]);done(b)
    b=new(49,"Chamon",3,1,{"J":1},"FCC even sites; primitive a1=ex+ey,a2=ey+ez,a3=ez+ex; odd center ex for reference stabilizer","E",*_sources("cond-mat/0404182","1006.4871"))
    # Cartesian even vector v converts to ((x+y-z)/2,(-x+y+z)/2,(x-y+z)/2).
    factors=[]
    for delta,p in zip(axes,"XYZ"):
        for sign in (-1,1):
            x,y,z=tuple((1 if i==0 else 0)+sign*delta[i] for i in range(3))
            factors.append((site(((x+y-z)//2,(-x+y+z)//2,(x-y+z)//2)),p))
    b.add(-1,*factors);done(b)
    b=new(50,"Truncated-octahedral 3D color code",3,12,{"JX":1,"JZ":1},"BCC Voronoi cells centered at Z^3 and Z^3+(1/2,1/2,1/2); qubits at Voronoi vertices; conventional cubic translations","E","https://doi.org/10.1103/PRXQuantum.4.030338","https://arxiv.org/abs/cond-mat/0607736")
    offsets=sorted(set(tuple(F(s*x,4) for s,x in zip(signs,p)) for p in permutations((0,1,2)) for signs in product((-1,1),repeat=3)))
    assert len(offsets)==24
    vertex_positions=sorted(set(tuple(x%1 for x in p) for p in offsets));assert len(vertex_positions)==12
    b.task["positions_in_primitive_basis"]=[[str(x) for x in p] for p in vertex_positions]
    index={p:i for i,p in enumerate(vertex_positions)}
    def vertex(v):
        cell=tuple(x.numerator//x.denominator for x in v)
        fractional=tuple(x-y for x,y in zip(v,cell));return site(cell,index[fractional])
    normals=[tuple(sign*x for x in delta) for delta in axes for sign in (-1,1)]+list(product((-1,1),repeat=3))
    for center in [tuple(F(0) for _ in range(3)),tuple(F(1,2) for _ in range(3))]:
        vertices=[tuple(x+y for x,y in zip(center,v)) for v in offsets]
        b.add(-1,*[(vertex(v),"X") for v in vertices])
        for normal in normals:
            if normal < (0,0,0):continue  # exactly one member of each shared-face pair
            rhs=F(1,2) if sum(abs(n) for n in normal)==1 else F(3,4)
            face=[tuple(x+y for x,y in zip(center,v)) for v in offsets if sum(x*n for x,n in zip(v,normal))==rhs]
            assert len(face) in (4,6)
            b.add(-1,*[(vertex(v),"Z") for v in face])
    done(b)
    assert [int(t["id"]) for t in tasks]==list(range(1,51))
    return dict(schema_version=2, interaction_convention="H = sum_cell translate(h0); terms are h0; e=<h0>/qubits_per_cell", tasks=tasks)


def validate_task(task):
    if type(task["dimension"]) is not int or task["dimension"] not in (1,2,3):raise ValueError("invalid dimension")
    if type(task["qubits_per_cell"]) is not int or task["qubits_per_cell"]<1:raise ValueError("invalid species count")
    d,q=task["dimension"],task["qubits_per_cell"]
    seen=set()
    for term in task["terms"]:
        if not isinstance(term["coefficient"],str):raise ValueError("coefficients must be exact strings")
        if not F(term["coefficient"]):raise ValueError("zero coefficient")
        sites=set()
        for factor in term["word"]:
            c,a,p=factor["cell"],factor["qubit"],factor["pauli"]
            if len(c)!=d or any(type(x) is not int for x in c):raise ValueError("invalid cell coordinate")
            if type(a) is not int or not 0<=a<q:raise ValueError("invalid qubit index")
            if p not in "XYZ" or len(p)!=1:raise ValueError("invalid Pauli letter")
            s=(tuple(c),a)
            if s in sites:raise ValueError("duplicate site")
            sites.add(s)
        key=word_key(term["word"])
        if key in seen:raise ValueError("duplicate word")
        seen.add(key)
    if task.get("canonical_sha256") != task_digest(task):raise ValueError("task digest mismatch")
    return True


def load_catalog(path=CATALOG):
    data=json.loads(Path(path).read_text())
    if data["schema_version"]!=2 or len(data["tasks"])!=50:raise ValueError("expected frozen 50-model schema v2")
    for task in data["tasks"]:validate_task(task)
    if [t["id"] for t in data["tasks"]]!=[f"{i:02d}" for i in range(1,51)]:raise ValueError("invalid IDs")
    return data


def load_task(task_id, path=CATALOG):
    key=f"{int(task_id):02d}"
    return next(t for t in load_catalog(path)["tasks"] if t["id"]==key)


if __name__ == "__main__":
    data=build_tasks()
    for t in data["tasks"]:validate_task(t)
    CATALOG.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"tasks":len(data["tasks"]),"terms":sum(len(t["terms"]) for t in data["tasks"]),"output":str(CATALOG)}))
