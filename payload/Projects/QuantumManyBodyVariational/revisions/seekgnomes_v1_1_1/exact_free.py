"""Ground-energy references from exactly solved free-fermion models.

All values are thermodynamic energies per physical qubit for the frozen Pauli
Hamiltonian, not spin-operator S=sigma/2 conventions. Exact solution refers to
the formula and sector theorem. Its stored decimal evaluation is not a
rigorous interval certificate. Task 24 is deliberately NOT a global reference:
its cited paper establishes the uniform flux sector by limiting perturbation
theory and finite-size tests, not a global theorem at the frozen Jprime/J=1/2.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction

from src.benchmark50_models import task_digest

EXPECTED_TASKS = {
    "01": "613b113c95c68736c40db66c85e60ffc16ac63b2aaa4ae12d2c74408fdd4ea43",
    "04": "5ff2e8587ec1240519d46d3d8ab0f1a56bd89831d86c295e68a014a9559acf83",
    "09": "753355b2a9b5b9623b776efcec8865518b113d07115b1454bd069ac6f8d7bf89",
    "10": "3985134871476ae9f90007896d9b8e89624ff59e0fc89c4c89ade29043a73a08",
    "22": "36316494c6f1b78deff4be8dbf0a8891602200d4b01902197eaef314affac945",
    "24": "885dcc20ce1056c473c536914ced07b7a668659c53519e961d1745d82b90aa59",
}

DECIMALS = {
    "01": "-1.27323954473516268615107010698011",
    "04": "-1.17766960500017517139706819601499",
    "09": "-1.14647975437076737884170741636891",
    "10": "-0.573239877185383689420853708184455",
    "22": "-0.787298618775946828747346091538260",
}

LIEB_SCHULTZ_MATTIS = "https://www.math.ucdavis.edu/~bxn/lieb1961.pdf"
CLUSTER_ISING = "https://arxiv.org/abs/1503.08598"
COMPASS = "https://arxiv.org/abs/cond-mat/0702369"
KITAEV = "https://arxiv.org/abs/cond-mat/0506438"
YAO_KIVELSON = "https://arxiv.org/abs/0708.0040"


def _mp_number(value, mp):
    number = Fraction(str(value))
    return mp.mpf(number.numerator) / number.denominator


def tfim_energy(j=1, g=1, *, dps=50):
    """-mean_k |J + g exp(ik)|, including exactly solvable limits."""
    import mpmath as mp
    with mp.workdps(dps):
        j, g = abs(_mp_number(j, mp)), abs(_mp_number(g, mp))
        total = j + g
        if total == 0:
            return mp.mpf(0)
        parameter = min(mp.mpf(1), 4 * j * g / total**2)
        return +(-2 * total * mp.ellipe(parameter) / mp.pi)


def xy_energy(jx=1, jy="1/2", h="3/4", *, dps=50):
    """XY Pauli-chain ground density from the Bogoliubov dispersion."""
    import mpmath as mp
    with mp.workdps(dps):
        jx, jy, h = (_mp_number(v, mp) for v in (jx, jy, h))
        total, difference = jx + jy, jx - jy
        def dispersion(k):
            return mp.sqrt((h-total*mp.cos(k))**2 + difference**2*mp.sin(k)**2)
        cuts = [mp.mpf(0)]
        if total != 0 and -1 < h/total < 1:
            cuts.append(mp.acos(h/total))
        cuts.append(mp.pi)
        return +(-mp.quad(dispersion, cuts) / mp.pi)


def cluster_ising_energy(k=1, lam="3/4", *, dps=50):
    """The threefold momentum winding leaves the full BZ mean unchanged."""
    return tfim_energy(k, lam, dps=dps)


def compass_energy(jx=1, jz="3/4", *, dps=50):
    """One effective Ising pseudospin for each TWO physical qubits."""
    import mpmath as mp
    with mp.workdps(dps):
        return +(tfim_energy(jx, jz, dps=dps) / 2)


def honeycomb_energy(kx=1, ky=1, kz=1, *, dps=50):
    """Exact zero-flux band integral, one angular integral done analytically.

    e0 = -1/2 mean_{x,y}|Kz+Kx exp(ix)+Ky exp(iy)|.
    Integrating y gives complete elliptic E with parameter 4*r*Ky/(r+Ky)^2.
    The zero-flux global-sector theorem is invoked only for the frozen isotropic
    task by energy_reference; generic parameters here merely evaluate its band.
    """
    import mpmath as mp
    with mp.workdps(dps):
        kx, ky, kz = (abs(_mp_number(v, mp)) for v in (kx, ky, kz))
        def integrand(x):
            # Stable at x=pi and Kx=Kz; avoid subtracting nearly equal squares.
            r = mp.sqrt((kx-kz)**2+4*kx*kz*mp.cos(x/2)**2)
            total = r + ky
            if total == 0:
                return mp.mpf(0)
            parameter = min(mp.mpf(1), max(mp.mpf(0),4*r*ky/total**2))
            return total*mp.ellipe(parameter)
        cuts = [mp.mpf(0)]
        if kx*kz != 0:
            cosine = (ky*ky-kx*kx-kz*kz)/(2*kx*kz)
            if -1 < cosine < 1:
                cuts.append(mp.acos(cosine))
        cuts.append(mp.pi)
        return +(-mp.quad(integrand,cuts)/mp.pi**2)


_METADATA = {
    "01": {
        "formula": "e0=-(1/(2*pi))*integral_0^(2*pi) sqrt(J^2+g^2-2*J*g*cos(k)) dk = -4/pi at J=g=1",
        "parameters": {"J":"1","g":"1"},
        "sources": [LIEB_SCHULTZ_MATTIS],
        "source_locator": "Jordan-Wigner quadratic-chain diagonalization; TFIM is the anisotropic XY limit after a spin-axis rotation",
        "normalization_checks": ["g=0 gives -abs(J)","J=0 gives -abs(g)"],
    },
    "04": {
        "formula": "e0=-(1/pi)*integral_0^pi sqrt((h-(Jx+Jy)*cos(k))^2+(Jx-Jy)^2*sin(k)^2) dk",
        "parameters": {"Jx":"1","Jy":"1/2","h":"3/4"},
        "sources": [LIEB_SCHULTZ_MATTIS],
        "source_locator": "Anisotropic XY free-fermion solution, translated explicitly to Pauli couplings and field",
        "normalization_checks": ["Jx=Jy=0 gives -abs(h)","Jy=h=0 gives -abs(Jx)","Jx=Jy=1,h=0 gives -4/pi"],
    },
    "09": {
        "formula": "e0=-(1/(2*pi))*integral_0^(2*pi) sqrt(K^2+lambda^2-2*K*lambda*cos(3*k)) dk",
        "parameters": {"K":"1","lambda":"3/4"},
        "sources": [CLUSTER_ISING],
        "source_locator": "Eqs.(1),(6)-(9): sum positive k pairs, each pair has energy -2*sqrt(epsilon^2+delta^2)",
        "normalization_checks": ["lambda=0 gives -K, since all cluster stabilizers commute","K=0 gives -abs(lambda)","The prefactor is fixed by k>0 pair counting, not copied from the paper's printed Eq.(13)"],
    },
    "10": {
        "formula": "e0=-(1/(4*pi))*integral_0^(2*pi) sqrt(Jx^2+Jz^2-2*Jx*Jz*cos(k)) dk",
        "parameters": {"Jx":"1","Jz":"3/4"},
        "sources": [COMPASS],
        "source_locator": "Mapping alternating XX/ZZ compass bonds to a transverse-field Ising pseudospin chain; divide its per-cell energy by two physical spins",
        "normalization_checks": ["Jz=0 gives -abs(Jx)/2","Jx=0 gives -abs(Jz)/2","Jx=Jz=1 gives -2/pi"],
    },
    "22": {
        "formula": "e0=-(1/2)*mean_(x,y in [0,2*pi]^2) abs(Kz+Kx*exp(i*x)+Ky*exp(i*y))",
        "parameters": {"Kx":"1","Ky":"1","Kz":"1"},
        "sources": [KITAEV],
        "source_locator": "Sec.4, Eqs.(29)-(32), zero-flux global ground sector from Lieb's theorem, Majorana spectrum +/-2*abs(Kz+Kx*exp(i*x)+Ky*exp(i*y))",
        "normalization_checks": ["Kx=Ky=0,Kz=1 gives isolated Pauli dimers e0=-1/2","Sixth-loop zero-flux sector applies at isotropic couplings","Not the S=sigma/2 energy, which differs by a factor of four"],
    },
}


def energy_reference(task):
    """Return an audited exact-theory reference for an unmodified frozen task.

    Return None for unrelated tasks. A changed recognized task is an error,
    rather than silently applying the wrong coupling/normalization reference.
    """
    key = task.get("id")
    if key not in EXPECTED_TASKS:
        return None
    expected = EXPECTED_TASKS[key]
    if task.get("canonical_sha256") != expected or task_digest(task) != expected:
        raise ValueError("Exact free reference does not match the frozen task: "+str(key))
    if key == "24":
        return {
            "status":"reference_pending", "value":None,
            "task_sha256":expected, "normalization":"per_qubit",
            "sources":[YAO_KIVELSON],
            "source_locator":"Uniform fluxes section, arXiv:0708.0040v3, pp.2-3",
            "reason":"The cited paper does not analytically establish the global minimizing flux sector for generic couplings; at Jprime/J=1/2 its uniform-sector band integral cannot be promoted to a rigorously known global ground energy by the cited argument.",
            "global_ground_state":False,
        }
    result = deepcopy(_METADATA[key])
    result.update({
        "status":"ready", "kind":"analytic_formula_numerical_evaluation",
        "value":float(DECIMALS[key]), "value_decimal":DECIMALS[key],
        "task_sha256":expected, "normalization":"per_qubit",
        "operator_convention":"Pauli sigma, not S=sigma/2",
        "global_ground_state":True,
        "reference_scope":"thermodynamic_limit",
        "evaluation":{
            "method":"mpmath closed-form or quadrature evaluation of exact dispersion",
            "working_decimal_precisions":[50,70],
            "stored_significant_decimal_digits":32,
            "reported_absolute_tolerance":1e-12,
            "error_is_rigorous":False,
            "note":"Precision refinement and independent momentum-grid/finite-chain checks support the numerical decimal; this is not a certified interval enclosure.",
        },
    })
    return result


__all__=["energy_reference","tfim_energy","xy_energy","cluster_ising_energy",
         "compass_energy","honeycomb_energy"]
