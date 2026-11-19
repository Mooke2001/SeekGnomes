"""Ground energies evaluated from exact theory for the frozen task points.

References are independent of a submitted state. Float evaluation is not a
certified error interval, and a missing reference never uses a baseline instead.
"""
from __future__ import annotations
from fractions import Fraction as F
from functools import lru_cache
import hashlib
import json
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[2]
STATIC_IDS={'07','08','18','25','26','27','28','29','30','44','45','46','47','48','49','50'}


def frozen_task(task):
    from src.benchmark50_models import load_task
    if task != load_task(int(task['id'])):
        raise ValueError('Exact reference is defined only for the frozen task point')
    return task


def _static_reference(task):
    key=task['id']
    if key not in STATIC_IDS:
        return None
    if key=='07':
        value=-F(3,8)*F(task['parameters']['J1'])
        method='Majumdar-Ghosh dimer ground state and triangle lower bound'
        expression='-3*J1/8'
    elif key=='18':
        value=-F(3,8)*F(task['parameters']['Jd'])
        method='Orthogonal singlet dimers, Jd>=2*J triangle bound'
        expression='-3*Jd/8'
    elif key in {'29','30'}:
        value=F(-1)
        method='Commuting single-flip involutions, one center per physical qubit'
        expression='-1'
    else:
        assert all(F(t['coefficient'])<0 for t in task['terms'])
        value=sum(F(t['coefficient']) for t in task['terms'])/task['qubits_per_cell']
        method='Commuting frustration-free Pauli generators; all +1 simultaneously attainable'
        expression=str(value)
    return {'status':'ready','value':float(value),'exact_rational':str(value),
            'expression':expression,'formula':expression,'method':method,
            'kind':'exact_rational_float_evaluation','normalization':'per_qubit',
            'sources':task['sources'],'numerical_tolerance':1e-13,
            'certified_error':False}


@lru_cache(maxsize=50)
def _reference(key):
    from src.benchmark50_models import load_task
    task=load_task(int(key))
    result=_static_reference(task)
    if result is None:
        from . import exact_free,exact_chains
        result=exact_free.energy_reference(task)
        if result is None:
            result=exact_chains.energy_reference(task)
    if result is None:
        result={'status':'reference_pending','value':None,'reason':'No verified exact ground-energy reference at this task point'}
    result=dict(result)
    result.setdefault('status','ready' if result.get('value') is not None else 'reference_pending')
    result['task_id']=key
    result['task_sha256']=hashlib.sha256(json.dumps(task,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    result['normalization']='per_qubit'
    if 'numerical_tolerance' not in result:
        result['numerical_tolerance']=result.get('uncertainty_tolerance',result.get('evaluation',{}).get('reported_absolute_tolerance',1e-10))
    return result


def energy_reference(task):
    frozen_task(task)
    # Return a fresh JSON object so callers cannot alter the cached reference.
    return json.loads(json.dumps(_reference(task['id']),allow_nan=False))


def _word(word):
    return tuple(sorted((tuple(p['cell']),p['qubit'],p['pauli']) for p in word))


def _shift(word,delta):
    return tuple(sorted((tuple(a+b for a,b in zip(c,delta)),q,p) for c,q,p in word))


def _product_word(a,b):
    out={(c,q):p for c,q,p in a}
    phase=0
    cyclic={('X','Y'):'Z',('Y','Z'):'X',('Z','X'):'Y'}
    for c,q,p in b:
        old=out.get((c,q))
        if old is None:
            out[(c,q)]=p
        elif old==p:
            del out[(c,q)]
        elif (old,p) in cyclic:
            out[(c,q)]=cyclic[old,p];phase+=1
        else:
            out[(c,q)]=cyclic[p,old];phase+=3
    return tuple(sorted((c,q,p) for (c,q),p in out.items())),phase%4


def _poly_product(a,b):
    out={}
    for x,cx in a.items():
        for y,cy in b.items():
            word,k=_product_word(x,y)
            real=cx[0]*cy[0]-cx[1]*cy[1]
            imag=cx[0]*cy[1]+cx[1]*cy[0]
            if k==1:real,imag=-imag,real
            elif k==2:real,imag=-real,-imag
            elif k==3:real,imag=imag,-real
            before=out.get(word,(F(0),F(0)))
            out[word]=(before[0]+real,before[1]+imag)
    return {w:c for w,c in out.items() if c!=(0,0)}


def audit_commuting_reference(task):
    """Exact local algebra audit, over every relative shift with an overlap.

    Pauli-only tasks are CSS, or one translated nonzero Pauli generator over
    the integral domain F2[x_1^+-1,...]. These cases have no finite -I relation.
    SPT generators flip exactly their own center; no nonempty finite product
    is scalar. Their involution and commutation identities are checked below.
    Consequently finite products of the preferred spectral projectors have
    positive trace, and compactness supplies an infinite frustration-free state.
    """
    frozen_task(task)
    key=task['id']
    if key not in STATIC_IDS-{'07','18'}:
        raise ValueError('Not a commuting-reference task')
    if key in {'29','30'}:
        pieces={s:{} for s in range(task['qubits_per_cell'])}
        for term in task['terms']:
            xs=[p for p in term['word'] if p['pauli']=='X']
            assert len(xs)==1 and xs[0]['cell']==[0]*task['dimension']
            assert all(p['pauli'] in {'X','Z'} for p in term['word'])
            pieces[xs[0]['qubit']][_word(term['word'])]=(F(term['coefficient']),F(0))
        polys=list(pieces.values())
        justification='single_flip_centers'
    else:
        polys=[{_word(t['word']):(F(1),F(0))} for t in task['terms']]
        css=all(len({p['pauli'] for p in t['word']})==1 and t['word'][0]['pauli'] in {'X','Z'} for t in task['terms'])
        assert css or len(task['terms'])==1
        justification='CSS_no_minus_identity' if css else 'single_orbit_Laurent_domain'
    for p in polys:
        assert _poly_product(p,p)=={():(F(1),F(0))}
    count=0
    for a in polys:
        sites_a={(c,q) for w in a for c,q,p in w}
        for b in polys:
            sites_b={(c,q) for w in b for c,q,p in w}
            shifts={tuple(x-y for x,y in zip(c,d)) for c,q in sites_a for d,r in sites_b if q==r}
            for v in shifts:
                bv={_shift(w,v):c for w,c in b.items()}
                if _poly_product(a,bv)!=_poly_product(bv,a):
                    raise ValueError('Noncommuting frozen generators in task '+key+' at '+str(v))
                count+=1
    return {'task_id':key,'involutions':len(polys),'relative_commutators_checked':count,
            'no_minus_identity_reason':justification,'passed':True}
