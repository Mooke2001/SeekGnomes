"""Run one local hybrid Python-correlator/Lean-proof trial through the relay."""
from __future__ import annotations
import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
EUREKA = PROJECT.parents[1]
sys.path.insert(0, str(PROJECT))
from revisions.hybrid_agent_v1.runtime.api_client import RelayClient
from revisions.hybrid_agent_v1.runtime.benchmark50_agent import AgentLimits, run_agent
from revisions.hybrid_agent_v1.local_numeric import LocalNumericalBroker
from src.benchmark50_models import load_task
from src.correlator_energy import evaluate_energy
from revisions.physics_first_v1_1.compiler import CorrelatorProgram
from revisions.physics_first_v1_1.verifier import verify, LOCK, _old

HERE = Path(__file__).resolve().parent
BITS, TERMS = 40, 32


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def template():
    return '''module
public import GeneratedState
public import QuantumManyBodyVariational.TrustedSpecExamplesV2
@[expose] public section
noncomputable section
namespace Submission

theorem accepted : GeneratedState.ProofObligation := by
  -- Replace skip with the actual proof. This template is not a certificate.
  skip

end Submission
'''


def locked_sources():
    lock = json.loads(LOCK.read_text())
    values = dict(lock['source_sha256'])
    names = ['HYBRID_PROOF_SPEC.lock.json', 'config/benchmark50_tasks.json',
             'config/benchmark50_score_rules.json',
             'revisions/hybrid_agent_v1/run_trial.py',
             'revisions/hybrid_agent_v1/local_numeric.py',
             'revisions/hybrid_agent_v1/system_prompt.md']
    for name, expected in values.items():
        if digest(PROJECT/name) != expected:
            raise ValueError('Hybrid specification source changed: '+name)
    values.update({name:digest(PROJECT/name) for name in names})
    return values


def read_source(path):
    with _old.regular_input(path, 65536) as stream:
        return stream.read().decode('utf-8')


def callbacks(task, *, timeout=300):
    d, q = task['dimension'], task['qubits_per_cell']

    def prepare(*, source_path, invocation_dir):
        source = read_source(source_path)
        program = CorrelatorProgram.from_source(source, d, q, bits=BITS, terms=TERMS)
        definition = program.lean_definition(namespace='GeneratedState')
        return {'ok':True, 'proof_verified':False, 'generated_state':definition,
                'proof_template':template(), 'source_sha256':program.source_sha256,
                'generated_state_sha256':hashlib.sha256(definition.encode()).hexdigest(),
                'dimension':d, 'qubits_per_cell':q, 'sqrt_bits':BITS, 'series_terms':TERMS,
                'target':'Submission.accepted : GeneratedState.ProofObligation',
                'meaning':'Prepared fixed definition only; no state proof has been checked.'}

    def check(*, source_path, proof_path, invocation_dir, energy=False):
        source = read_source(source_path)
        result = verify(source, proof_path, d, q, bits=BITS, terms=TERMS,
                        timeout=timeout, evidence_dir=invocation_dir/'verification')
        passed = result.get('passed') is True
        response = {key:result.get(key) for key in ('compile_passed','goal_matched','kernel_verified','problems','certificate')}
        response.update(ok=passed, verified=passed, proof_verified=passed,
                        proof_manifest=result['manifest'], energy_certified=False,
                        energy_mode='approximate_float', energy_density=None)
        # Only host-owned verifier logs are exposed, never candidate-supplied paths.
        diagnostics = {}
        evidence = Path(result['manifest']).parent
        for label in ('compile-generated','compile-candidate','export-candidate','judge'):
            path = result.get('checks',{}).get(label,{}).get('log')
            if path:
                path = Path(path)
                if path.parent == evidence:
                    with _old.regular_input(path) as stream:
                        data=stream.read(24001)
                    diagnostics[label]=data[:24000].decode(errors='replace')
        response['diagnostics']=diagnostics
        if energy and passed:
            program=CorrelatorProgram.from_source(source,d,q,bits=BITS,terms=TERMS)
            numeric=evaluate_energy(task,program.evaluate,f_manifest=program.manifest())
            numeric['preconditions']={'legality':'verified_by_fixed_goal_kernel_replay',
                'translation_invariance':'verified_by_fixed_goal_kernel_replay',
                'numerical_preconditions':'verified_by_fixed_goal_kernel_replay',
                'proof_checked_here':True}
            _old.write_new(invocation_dir/'energy.json',
                           (json.dumps(numeric,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode())
            response.update(energy_density=numeric['energy_density'],
                            energy_normalization='per_qubit', terms=numeric['terms'],
                            task_physics_sha256=numeric['task_physics_sha256'],
                            source_sha256=program.source_sha256,
                            energy_report=str(invocation_dir/'energy.json'))
            rule=json.loads((PROJECT/'config/benchmark50_score_rules.json').read_text())['tasks'][task['id']]
            baseline=float(Fraction(rule['baseline']))
            scale=float(Fraction(rule['scale']))
            response.update(baseline_energy=baseline,
                            numerical_normalized_gain=max(0.0,(baseline-numeric['energy_density'])/scale),
                            improvement_is_certified=False)
        return response

    def lean(*,source_path,proof_path,invocation_dir):
        return check(source_path=source_path,proof_path=proof_path,invocation_dir=invocation_dir)

    def score(*,source_path,proof_path,invocation_dir):
        return check(source_path=source_path,proof_path=proof_path,invocation_dir=invocation_dir,energy=True)

    return prepare, lean, score

