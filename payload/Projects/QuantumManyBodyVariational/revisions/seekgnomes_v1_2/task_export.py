"""Export fixed SeekGnomes physics tasks in Harbor's task format.

The exported verifier is an entry point for a host-signed receipt.  Native
agents never select their own reward or replace the fixed Lean obligation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
VERSION = "1.3.0"
STATE_PROTOCOL = "seekgnomes-harbor-state/v1"
LEGACY_PROTOCOL = "seekgnomes-harbor/v1.2.0"


def candidate_protocol(candidate_format):
    if candidate_format == "state":
        return STATE_PROTOCOL
    if candidate_format == "legacy":
        return LEGACY_PROTOCOL
    raise ValueError("Candidate format must be state or legacy")


def _new_root(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(PROJECT) or path.resolve() != path:
        raise ValueError("Task export must use a canonical project-local directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def export_task(task_id, destination, *, image="seekgnomes:1.3.0", web=True, cpus=2,
                candidate_format="state"):
    from src.benchmark50_models import load_task

    if type(cpus) is not int or cpus < 1:
        raise ValueError("The task CPU allocation must be a positive integer")
    protocol = candidate_protocol(candidate_format)
    task = load_task(int(task_id))
    root = _new_root(Path(destination))
    (root / "environment").mkdir(mode=0o700)
    (root / "tests").mkdir(mode=0o700)
    encoded = json.dumps(task, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (root / "task.json").write_text(encoded)
    (root / "environment" / "seekgnomes-task.json").write_text(encoded)
    if not isinstance(image, str) or not image or any(c.isspace() for c in image):
        raise ValueError("A single nonempty image reference is required")
    (root / "environment" / "Dockerfile").write_text("FROM " + image + "\n")
    handoff = """This trial contains two separate native-harness sessions. Constructor performs
physics exploration and submits only a COMPLETE mathematical definition f.md,
with fixed parameters, conventions and necessary limits/branches, through the
host create_handoff tool. It need not implement f.py or supply a proof.
Formalist faithfully implements that exact definition as restricted pure f.py
and proves the host-generated fixed Lean obligation. It does not optimize or
replace the Constructor's state. It has no numerical exploration capability.
After every return, Constructor checks implementation/proof/map faithfulness.
Corrections and optimization use another complete f.md handoff.

Only the host's strict fixed-goal and independent Lean-kernel receipt certifies
a state. Printed numerical energies and natural-language claims are not
certificates. Host code calculates energy from the same proved f.py. The
floating energy has no formally certified rounding interval.
"""
    if candidate_format == "state":
        handoff = """This trial contains two separate native-harness sessions. Constructor explores
states and submits a COMPLETE f.state through the host create_handoff tool.
Formalist proves the fixed Lean goal generated from that immutable .state.
Each correction or improvement starts a new handoff. Formalist continues
working on incomplete proofs and can return concrete defects in the frozen
definition. Its allowed online tools can consult public proof references.

The strict Lean check verifies legality and cell translation invariance for
the same f that the host evaluates from the frozen .state. Only a successful
proof and evaluation return a floating-point energy density to Constructor.
"""
    statement = """# SeekGnomes: infinite-state variational construction

Use the specified infinite qubit lattice, cell translations, Pauli convention
and Hamiltonian without changing units or coefficients. Construct a normalized
positive translation-invariant state and minimize its energy per qubit.
The state's target object is f(P), the expectation of EVERY canonical finite
Pauli word P, including identity and arbitrary integer coordinates/support.

{handoff}

The host coordinator retains the cumulative Constructor budget over all
handoffs. Formalist's cumulative budget is independent and uncapped, while
single requests/processes remain protected. A native harness's own context
management is used; SeekGnomes does not discard its message history.
Different models, tasks, budget configurations and attempts have isolated private state.
Public-reference access is {network}. Native CLI/API credentials and the
trusted budget, receipt and scoring records are not available to tool code.

## Fixed task

```json
{task}
```
""".format(handoff=handoff.rstrip(), network="enabled through the host reference tools" if web else "disabled",
           task=encoded.rstrip())
    (root / "instruction.md").write_text(statement)
    # Absence of agent.timeout_sec is deliberate: an outer total timeout would
    # otherwise impose a new cumulative limit on the Formalist phase.
    config = [
        'schema_version = "1.4"', "", "[metadata]",
        'benchmark = "SeekGnomes"', 'protocol_version = "1.3.0"',
        "candidate_format = " + json.dumps(candidate_format),
        "protocol = " + json.dumps(protocol),
        "task_id = " + json.dumps(task["id"]),
        "task_sha256 = " + json.dumps(task["canonical_sha256"]),
        'energy_normalization = "per_qubit"', "", "[agent]", "",
        "[verifier]", "timeout_sec = 300", 'network_mode = "no-network"', "",
        "[environment]", "build_timeout_sec = 60", "cpus = " + str(cpus),
        "memory_mb = 16384", "storage_mb = 32768",
        'network_mode = "no-network"', 'workdir = "/app"',
        "docker_image = " + json.dumps(image), "",
    ]
    (root / "task.toml").write_text("\n".join(config))
    verifier = """#!/usr/bin/env bash
set -euo pipefail
# Only the host sets these values after the native agent phase has stopped.
# A workspace-written certificate or model-selected hash is never authority.
: "${SEEKGNOMES_HOST_RECEIPT:?host receipt is required}"
: "${SEEKGNOMES_RECEIPT_VERIFY_KEY:?host verification capability is required}"
cd /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational
exec /home/ubuntu/Mooke/Eureka/Environment/Python/miniconda3/bin/python -B \\
  -m revisions.seekgnomes_v1_2.harbor_adapter verify-receipt \\
  --receipt "$SEEKGNOMES_HOST_RECEIPT" --reward /logs/verifier/reward.json --artifact-namespace
"""
    script = root / "tests" / "test.sh"
    script.write_text(verifier)
    script.chmod(0o755)
    return {"task_id": task["id"], "path": str(root),
            "task_sha256": task["canonical_sha256"],
            "instruction_sha256": hashlib.sha256(statement.encode()).hexdigest()}


def export_tasks(destination, task_ids=None, *, image="seekgnomes:1.3.0", web=True, cpus=2,
                 candidate_format="state"):
    ids = tuple(range(1, 51)) if task_ids is None else tuple(int(i) for i in task_ids)
    if not ids or len(set(ids)) != len(ids) or any(i < 1 or i > 50 for i in ids):
        raise ValueError("Select distinct fixed task IDs between 1 and 50")
    protocol = candidate_protocol(candidate_format)
    root = _new_root(Path(destination))
    entries = [export_task(i, root / f"task-{i:02d}", image=image, web=web, cpus=cpus,
                           candidate_format=candidate_format) for i in ids]
    manifest = {"schema": "seekgnomes-harbor-tasks/v1", "version": VERSION,
                "harbor_version": "0.22.0", "web_enabled": bool(web), "cpus": cpus,
                "candidate_format": candidate_format, "protocol": protocol,
                "tasks": entries, "contains_credentials": False,
                "contains_reference_answers": False}
    (root / "tasks-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
