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
from src.python_correlator_hybrid import CorrelatorProgram
from src.hybrid_proof_verifier import verify, LOCK, _old

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


def public_references(task):
    lean = PROJECT/'lean/QuantumManyBodyVariational'
    names = ['LocalPauli','StateLimits','PauliRecovery','CorrelatorStates','TrustedStateSpec',
             'StateExamples','StateClosure','TrustedStateSpecV2','TrustedSpecExamplesV2',
             'RationalMath','HybridMath']
    references={name+'.lean':(lean/(name+'.lean')).read_text() for name in names}
    references['submission_api.md']='''# Hybrid infinite-state submission contract

Write a restricted pure Python source defining f(P). P is a canonical tuple of
(cell, qubit, axis) factors: cell is a D-tuple of arbitrary integers, qubit is
0..Q-1 and axis is X, Y or Z. Identity is P=(). Factors at different sites commute;
the word contains no identity factors or duplicate sites. f returns a real Pauli
expectation. It must define a normalized positive infinite state, invariant
under every cell translation. This is not a finite-region sampling condition.

Use prepare {source:"f.py"}. Read its generated_paths before proving anything.
The host fixes GeneratedState.f and GeneratedState.ProofObligation. Write a Lean
module importing GeneratedState with public theorem Submission.accepted of that
EXACT type. The target contains StateCondition D Q f AND NumericalPreconditions.
Use module/public import/@[expose] public section/noncomputable section as in
the returned template. Do not redefine GeneratedState, change the target,
introduce assumptions, sorry, admit or new axioms. Generic state constructors
in the public library are optional aids; the search is not restricted to them.

run_lean/score/finish accept {source:"f.py",proof:"Candidate.lean"}. Every check
freezes both files and independently regenerates the fixed module. The host
compares the full goal and definitions, audits axioms and replays the kernel.
score computes energy only after successful proof verification. Energy is
float, per qubit, from the fixed Hamiltonian's finite Pauli terms. It has no
automatic certified rounding bound. LLM-reported numbers are never scores.

prepare is not a proof. A changed source needs a new definition and proof check.
The readonly generated files can be read in pages: read {path, start_line:1,
max_lines:120}. Do not try to overwrite generated files or host references.
Only compiler errors and legitimate mathematical constructions are in scope.
'''
    references['python_language.md']='''# Supported pure Python language

Define exactly f(P), with optional typed pure helper functions and constants.
Supported: scalar arithmetic, nonnegative bounded integer powers, exact integer
coordinates/indexing, tuples/lists/fixed-key dicts, tuple unpacking, helper
defaults/keyword args, finite range/for accumulators, finite comprehensions and
generators, sum/prod/all/any, comparisons and if/and/or, abs/min/max. Supported
math imports are sqrt, exp, log, sin, cos, tanh, pi, e and prod. sum is a builtin.
No NumPy in f. No eigensolver, arbitrary while, recursion, random, file IO,
external calls or stateful code. Use server only for separate numerical scripts.
Source limit 64KiB, AST4096 nodes, sequences/ranges4096, evaluation1M steps.

Values outside control use ideal real arithmetic in Lean and float approximations
in the numerical evaluator. ALL numeric comparisons use the same deterministic
rational shadow arithmetic in Lean and Python. Real exp/sqrt etc used for a
branch are recomputed using a fixed rational algorithm, not by converting an
already rounded float. Branch configuration is bits40/terms32. Changing it
changes the definition. For example 0.1+0.2==0.3 is true in this exact-Q branch
semantics; sqrt(2)**2==2 can be false. Real value expressions retain Real.sqrt,
Real.exp, etc. Required real/Q domain conditions are part of the Lean goal.
Use coordinate/Pauli structure to construct arbitrary supported correlators;
you are not limited to a tensor network or preset ansatz.
'''
    references['hybrid_semantics.md']=(PROJECT/'PYTHON_CORRELATOR_HYBRID.md').read_text()
    references['submission_skeleton.lean.txt']=template()
    rule=json.loads((PROJECT/'config/benchmark50_score_rules.json').read_text())['tasks'][task['id']]
    references['scoring_rules.json']=json.dumps({'task_id':task['id'],'reference':rule,
        'current_endpoint':'lowest host-computed float energy of a fixed-goal-kernel-verified state',
        'reference_meaning':'Public tracial/uniform-plus baseline and algebraic scale; not the true ground energy',
        'not_comparable_as_same_protocol':'Legacy certified upper bounds differ from the present approximate energy endpoint'},indent=2)
    return references


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id',type=int,required=True,choices=range(1,51))
    parser.add_argument('--model',required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--experiment-id',required=True)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--checker-seconds',type=int,default=300)
    parser.add_argument('--request-seconds',type=int,default=600)
    parser.add_argument('--max-turns',type=int,default=64)
    parser.add_argument('--output-tokens',type=int,default=128000)
    parser.add_argument('--wall-seconds',type=int,default=14400)
    args=parser.parse_args()
    sources=locked_sources()
    task=load_task(args.task_id)
    prepare, lean, score=callbacks(task,timeout=args.checker_seconds)
    def progress(kind,value):
        if kind=='api':
            record={'event':kind,**value}
        elif kind=='tool_request':
            record={'event':kind,'tool':value.get('tool')}
        else:
            record={'event':kind,**{k:value[k] for k in ('ok','verified','proof_verified','energy_density','finished','error') if k in value}}
        print(json.dumps(record,ensure_ascii=False,allow_nan=False),flush=True)
    client=RelayClient.from_environment(timeout=args.request_seconds,connect_timeout=60,
        persistent_session=True,max_connect_retries=2,
        response_body_limit_bytes=16*1024**2,completion_token_field='max_tokens',
        reasoning_usage_policy='provider_reported_completion')
    try:
        manifest=run_agent(client,args.model,task,args.out,references=public_references(task),
            prepare=prepare,run_lean=lean,score=score,server=LocalNumericalBroker(),
            limits=AgentLimits(max_turns=args.max_turns,max_tokens_per_turn=16000,
                max_total_output_tokens=args.output_tokens,max_total_input_tokens=2000000,
                max_verifier_calls=64,max_server_cpu_seconds=3600,
                final_output_reserve=16000,wall_seconds=args.wall_seconds),
            system_prompt=(HERE/'system_prompt.md').read_text(),tool_protocol='native_function',
            transport_reconnect=True,max_transport_reconnects=2,resume=args.resume,
            normal_request_tokens=16000,normal_output_safety_margin=4096,closing_request_tokens=8192,
            progress=progress,provenance={'family':'kimi','experiment_id':args.experiment_id,
                'trial':1,'scope':'local_hybrid_five_task_capability_test','runtime_sources':sources,
                'energy_endpoint':'approximate_float_for_kernel_verified_state',
                'compute_location':'local_no_ssh','sqrt_bits':BITS,'series_terms':TERMS,
                'transport_limits':{'checker_seconds':args.checker_seconds},
                'usage_measure':'provider_reported_completion_once_not_billing_or_physical_generation'})
        print(json.dumps({'status':manifest['status'],'turns':manifest['turns'],
            'model':manifest['model'],'out':str(args.out.absolute()),
            'best_verified_result':manifest.get('best_verified_result'),
            'verified_result':manifest.get('verified_result')},ensure_ascii=False,allow_nan=False))
        return 0 if manifest['status']=='completed' else 1
    finally:
        client.close()


if __name__=='__main__':
    raise SystemExit(main())
