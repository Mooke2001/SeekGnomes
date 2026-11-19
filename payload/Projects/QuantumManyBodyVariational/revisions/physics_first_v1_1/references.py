"""Public physics-stage material and immutable, same-trial proof handoff."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def exploration_references(task: dict) -> dict[str, str]:
    from revisions.hybrid_agent_v1.run_trial import public_references
    return {
        "physics_task.json": json.dumps(task, ensure_ascii=False, indent=2),
        "scoring_rules.json": public_references(task)["scoring_rules.json"],
        "correlator_contract.md": """# Infinite-state physics exploration

Construct a normalized positive state on the specified infinite qubit lattice,
invariant under every cell translation, and minimize its energy per qubit.
Explain how your construction defines a state on arbitrary finite Pauli words,
not only on the Hamiltonian terms. A physical construction, its normalization,
positivity and translation behavior should be explained in research.md.
Do not change the Hamiltonian, units, lattice, or number of qubits per cell.

f(P) returns a real expectation. P is a canonical tuple of
(cell_tuple, qubit_index, axis) factors; axis is X/Y/Z, identity is ().
Distinct factors have distinct sites, in canonical coordinate/qubit order.
The host evaluates sum_j c_j*f(P_j)/Q using the given finite density terms.
explore_energy computes this expression without checking that f is a state.
It is not a legal-state score, a proof, or a certified variational bound.
Arbitrarily low numbers from inconsistent correlations are not solutions.

Use physical reasoning and numerical exploration to construct and optimize
ansatzes. The state family is open: no product/MPS/PEPS whitelist is imposed.
You may also use numerical scripts for constructions not yet encoded as f.
Only candidate f sources encoded in the supported language can be handed to
the later proof phase. Keep useful alternatives and explain their tradeoffs.
""",
        "python_language.md": """# Supported correlator source

Define f(P), optional pure helpers and constants. Supported: scalar arithmetic,
bounded nonnegative integer powers, integer coordinates/indexing, tuples/lists/
fixed-key dicts, unpacking, helper defaults/keywords, finite range/for,
comprehensions, sum/prod/all/any, comparisons, if/and/or, abs/min/max.
Allowed math imports: sqrt, exp, log, sin, cos, tanh, pi, e, prod.
The correlator cannot import NumPy or external files, use eigensolvers,
arbitrary while/recursion, randomness, stateful IO, or external calls.
Numerical experiment scripts may use NumPy/SciPy, optimization and eigensolvers
inside the separate networkless local numerical tool.

All comparisons in f use deterministic rational shadow arithmetic, bits40 and
terms32, while value expressions retain their ideal real interpretation.
For instance 0.1+0.2==0.3 is true under the branch semantics. Comparisons involving
sqrt/exp use the specified rational algorithms, not ordinary rounded floats.
No formal proof tools or formal state-library source are exposed in this stage.
Source limit64KiB, AST4096 nodes, sequence/range4096, evaluation1M steps.
""",
        "tools.md": """# Physics tools

Every local action is one benchmark_action call with tool and arguments.
list_files {}, read {path}, write {path,content,append:false} operate only in
this trial's private files and the explicit public references.
explore_energy {source:"candidate.py"} returns conditional floating-point
energy and freezes that source version, with legality/TI explicitly unchecked.

To run numerical code, first write it to a private FILE, e.g.
write {path:"experiment.py",content:"import numpy as np\\n..."}, then call
server {candidate:"experiment.py",cpus:1,wall_seconds:120,arguments:[]}.
The candidate argument is a RELATIVE FILE PATH, not Python code text.
This is a local, one-CPU, networkless sandbox, not a remote server. It has no
other files, credentials, host workspace or subprocess/network capability.

After exploration, write research.md describing the physical motivation,
candidate families, actual optimization, correlation structure, energy formula,
normalization/positivity/TI argument and unresolved issues. Distinguish results
you actually computed from conjectures. Then call
commit_candidates {sources:["best.py","alternative.py"],notes:"research.md"}.
Choose at most3 sources, all already successfully evaluated by explore_energy
at the SAME source hash. This freezes the pool for a separate proof stage.
The proof stage can prove any of these sources but cannot replace the physics
with a source not in the pool. Keep a justified backup when useful. These are
still unverified candidates, and there is no certified score in this stage.
""",
    }


def proof_references(task: dict, candidates: list[dict], notes_path: Path) -> dict[str, str]:
    from revisions.hybrid_agent_v1.run_trial import public_references
    result = public_references(task)
    index = []
    for position, candidate in enumerate(candidates):
        path = Path(candidate["source_path"])
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != candidate["source_sha256"]:
            raise ValueError("Frozen physics candidate changed before proof handoff")
        name = f"handoff/candidate-{position+1}.py"
        result[name] = data.decode("utf-8")
        index.append({"reference": "reference/" + name, "source_sha256": digest,
                      "physics_energy_unverified": candidate.get("energy_density"),
                      "original_path": candidate.get("original_path"),
                      "proof_verified": False})
    result["handoff/research.md"] = Path(notes_path).read_text()
    result["handoff/index.json"] = json.dumps({
        "origin": "only this trial's own physics stage; no prior-run or peer answers",
        "candidates": index, "legality_and_TI": "pending formal proof",
        "source_policy": "Copy one frozen source exactly; physics changes are closed.",
    }, indent=2, ensure_ascii=False)
    return result
