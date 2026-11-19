"""Exact-theory thermodynamic energy references for frozen tasks 05 and 06.

The frozen numbers are floating evaluations of Bethe/Baxter equations, not
finite-size extrapolations or variational estimates. ``numerical_tolerance``
is an operational comparison tolerance, not a certified enclosure. The public
regeneration functions below expose the conventions and convergence checks.
"""
from __future__ import annotations

from copy import deepcopy
import math

from src.benchmark50_models import task_digest

XXZ_SOURCE = "https://arxiv.org/pdf/hep-th/0505006"
XXZ_DRESSED_SOURCE = "https://arxiv.org/pdf/0912.1739"
XYZ_SOURCE = "https://link.springer.com/article/10.1007/s00220-024-04977-w"
_DIGESTS = {
    "05": "08d60078c6e5a7ba88612f7f14625917c42323f1ead7457f086fe7542599b8a4",
    "06": "4e5a637fd114ebc84b79cb929513023960ea031f566931f4154e01e4d0b74f38",
}


def xxz_ground_energy(delta=1.5, h=1 / 3, *, nodes=128):
    """Pauli H=sum(XX+YY+delta ZZ-h Z), delta>1, h>=0.

    Let a_n(x)=sinh(n*eta)/(pi*(cosh(n*eta)-cos(2*x))). On
    [-Q,Q], solve rho+a_2*rho=a_1 and eps+a_2*eps=2h-4pi*sinh(eta)*a_1.
    eps(Q)=0 sets Q in the magnetized regime; Q=pi/2 below the lower field.
    e=delta-h+integral((2h-4pi*sinh(eta)*a_1)*rho).

    Kitanine et al. Eqs 1.1-1.3, 2.15-2.17, 2.26-2.33 give the Pauli,
    field and root-density conventions. Their Hamiltonian is ours minus
    delta per site, with their field h_paper=2*h. The dressed-energy equation
    is the variational dual of the root-density equation, not a finite chain.
    """
    import numpy as np
    from numpy.polynomial.legendre import leggauss
    from scipy.linalg import solve
    from scipy.optimize import brentq

    delta, h = float(delta), float(h)
    if not (delta > 1 and h >= 0 and math.isfinite(delta + h)):
        raise ValueError("Requires finite delta>1 and h>=0")
    if type(nodes) is not int or nodes < 16:
        raise ValueError("At least 16 Gauss-Legendre nodes required")
    eta = math.acosh(delta)
    if h >= 2 * (delta + 1):
        return dict(value=delta-h, fermi_boundary=0.0, magnetization_z=1.0,
                    nodes=nodes, dual_energy=delta-h, endpoint_residual=0.0,
                    linear_residual=0.0, regime="fully_polarized")
    z, weights = leggauss(nodes)

    def a(x, n):
        return np.sinh(n*eta) / (np.pi*(np.cosh(n*eta)-np.cos(2*x)))

    def bare(x):
        return 2*h - 4*np.pi*np.sinh(eta)*a(x, 1)

    def solution(q, *, full=False):
        x, w = q*z, q*weights
        matrix = np.eye(nodes) + a(x[:, None]-x[None, :], 2)*w
        epsilon = solve(matrix, bare(x), assume_a="gen")
        endpoint = float(bare(q) - np.sum(a(q-x, 2)*w*epsilon))
        if not full:
            return endpoint
        density = solve(matrix, a(x, 1), assume_a="gen")
        return dict(value=float(delta-h+np.sum(bare(x)*density*w)),
                    dual_energy=float(delta-h+np.sum(a(x, 1)*epsilon*w)),
                    fermi_boundary=float(q),
                    magnetization_z=float(1-2*np.sum(density*w)), nodes=nodes,
                    endpoint_residual=endpoint,
                    linear_residual=float(np.max(np.abs(matrix@density-a(x, 1)))),
                    regime="magnetized" if q < math.pi/2-1e-10 else "zero_magnetization")

    edge = math.pi/2
    # eps(edge)<=0 means that the entire root sea remains filled. A positive
    # field does not change this state's energy while it is below the gap.
    q = edge if solution(edge) <= 0 else brentq(solution, 1e-10, edge, xtol=1e-14)
    return solution(q, full=True)


def xyz_ground_energy(jx=1, jy=0.75, jz=0.5, *, dps=50):
    """Baxter's infinite-chain energy, for 0<=jz<jy<jx in Pauli convention.

    The unitary alternating Z rotation maps +jx XX+jy YY+jz ZZ to the
    source's -1/2 Hamiltonian with J=(2*jx,2*jy,-2*jz). Divide these source
    couplings by scale=jx+jy: zeta=(jx-jy)/scale, Gamma=-2*jz/scale.
    Source Eqs 3.5 and 3.7 fix eta and tau=i*t. Appendix D Eq D.3 (itself
    Baxter 1982 Eq 10.14.30) then gives the full, not supersymmetric-only,
    ground energy. Theta functions use mpmath's nome convention.
    """
    import mpmath

    c = mpmath.mp.clone()
    c.dps = int(dps)
    jx, jy, jz = [c.mpf(str(v)) for v in (jx, jy, jz)]
    if not (0 <= jz < jy < jx):
        raise ValueError("This evaluator requires 0<=jz<jy<jx")
    scale = jx+jy
    zeta, gamma = (jx-jy)/scale, -2*jz/scale
    th = lambda n, u, t: c.jtheta(n, u, c.exp(-2*c.pi*t))

    def equations(eta, t):
        denominator = th(4, 2*eta, t)**2
        zz = th(1, 2*eta, t)**2/denominator
        gg = th(2, 2*eta, t)*th(3, 2*eta, t)*th(4, 0, t)**2 / (
            th(2, 0, t)*th(3, 0, t)*denominator)
        return zz-zeta, gg-gamma

    eta0 = c.acos(gamma)/2
    t0 = c.log(4*c.sin(2*eta0)**2/zeta)/c.pi
    eta, t = c.findroot(equations, (eta0, t0), tol=c.mpf(10)**(-c.dps+12),
                        maxsteps=100)
    if not (0 < eta < c.pi/2 and t > 0):
        raise ArithmeticError("Theta inversion left the intended real regime")
    x, r = c.exp((2*eta-c.pi)/t), c.exp(-c.pi/t)
    aa, bb = x*x, r/x
    if not (0 < aa < 1 and 0 < bb < 1 and 0 < r < 1):
        raise ArithmeticError("Baxter series is outside its convergent regime")
    prefactor = 4*th(1, 2*eta, t) / (
        t*c.jtheta(2, 0, c.exp(-c.pi*t))**2*th(4, 2*eta, t))
    series, count = c.mpf(0), 0
    target = c.mpf(10)**(-c.dps+10)
    while True:
        count += 1
        an, bn, rn = aa**count, bb**count, r**(2*count)
        series += (an-bn)*(1-bn)*(1-an)/((1-rn)*(1+an))
        # For exact eta,t, this bounds the omitted series by two geometric
        # tails. It does not enclose numerical root-finding/rounding errors.
        tail = (aa**(count+1)/(1-aa)+bb**(count+1)/(1-bb))/(1-r*r)
        if abs(scale*prefactor)*tail < target:
            break
        if count > 100000:
            raise ArithmeticError("Baxter series did not converge")
    value = scale*(-(2+gamma)/2-prefactor*series)
    residual = max(abs(v) for v in equations(eta, t))
    return dict(value=float(value), decimal=c.nstr(value, c.dps-5),
                eta=c.nstr(eta, c.dps-5), tau_imaginary=c.nstr(t, c.dps-5),
                zeta=c.nstr(zeta, c.dps-5), gamma=c.nstr(gamma, c.dps-5),
                scale=c.nstr(scale, c.dps-5), series_terms=count,
                series_tail_bound_given_parameters=float(abs(scale*prefactor)*tail),
                parameter_residual=float(residual), working_decimal_digits=c.dps)


_REFERENCES = {
    "05": {
        "value": -2.09528018177565,
        "kind": "exact_theory_numeric_evaluation",
        "normalization": "per_qubit",
        "expression": "delta-h+integral[-Q,Q] (2h-4*pi*sinh(eta)*a1(x))*rho(x) dx",
        "formula": "eta=acosh(delta); a_n=sinh(n*eta)/(pi*(cosh(n*eta)-cos(2x))); rho+a2*rho=a1; epsilon+a2*epsilon=2h-4*pi*sinh(eta)*a1; epsilon(Q)=0",
        "sources": [XXZ_SOURCE, XXZ_DRESSED_SOURCE, "https://doi.org/10.1103/PhysRev.150.327"],
        "source_equations": "Kitanine et al. 1.1-1.3, 2.15-2.17, 2.26-2.33; dressed-energy boundary from variational dual",
        "conventions": "Pauli couplings +XX+YY+1.5ZZ-(1/3)Z. Kitanine's shifted Hamiltonian requires +delta per site and field h_paper=2/3.",
        "parameters": {"delta": "3/2", "h_pauli": "1/3"},
        "fermi_boundary": 1.15824375204452,
        "magnetization_z": 0.01664571470648,
        "lower_critical_h_pauli": 0.17317888355122063,
        "numerical_tolerance": 1e-9,
        "uncertainty_tolerance": 1e-9,
        "certified_error": False,
        "evaluation_note": "Gauss-Legendre 64/128/256 nodes agree to 2e-14. Tolerance is numerical validation, not a rigorous interval. Zero-field value is NOT used at h=1/3.",
    },
    "06": {
        "value": -1.3555861298068899,
        "kind": "exact_theory_numeric_evaluation",
        "normalization": "per_qubit",
        "expression": "(7/4)*[-(2+Gamma)/2 - 4*theta1(2eta|2tau)/(t*theta2(0|tau)^2*theta4(2eta|2tau))*S]",
        "formula": "tau=i*t; zeta=1/7,Gamma=-4/7 fixed by Eq3.5; x=exp((2eta-pi)/t),r=exp(-pi/t); S=sum_n>=1 [(x^(2n)-(r/x)^n)*(1-(r/x)^n)*(1-x^(2n))]/[(1-r^(2n))*(1+x^(2n))]",
        "sources": [XYZ_SOURCE, "https://doi.org/10.1103/PhysRevLett.26.834"],
        "source_equations": "Hagendorf and Rosengren (2024), Hamiltonian 2.1, parameter map 3.5/3.7, general Baxter energy D.2-D.3; Baxter (1982) 10.14.30",
        "conventions": "Alternating Z rotation maps Pauli +XX+0.75YY+0.5ZZ to source -1/2 sum (2XX+1.5YY-1ZZ), then divide J by 7/4.",
        "parameters": {"zeta": "1/7", "Gamma": "-4/7", "scale": "7/4"},
        "eta": 1.0978425432223012,
        "tau_imaginary": 0.9261377971889808,
        "numerical_tolerance": 1e-11,
        "uncertainty_tolerance": 1e-11,
        "certified_error": False,
        "evaluation_note": "Theta inversion and the convergent Baxter series were checked at 50/80 decimal digits. Analytic series tail bound excludes parameter solve rounding, so no certified interval is claimed.",
    },
}


def energy_reference(task):
    """Return a frozen exact-theory reference only for the original full task."""
    key = str(task.get("id", "")).zfill(2)
    if key not in _REFERENCES:
        return None
    if task_digest(task) != _DIGESTS[key]:
        raise ValueError("Exact chain reference does not match the frozen Hamiltonian: "+key)
    result = deepcopy(_REFERENCES[key])
    result["task_sha256"] = _DIGESTS[key]
    result["source"] = result["sources"][0]
    return result


__all__ = ["energy_reference", "xxz_ground_energy", "xyz_ground_energy"]
