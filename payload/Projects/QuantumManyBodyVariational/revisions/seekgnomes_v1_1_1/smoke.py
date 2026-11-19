"""Offline release acceptance using the real kernel and numerical sandbox."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

PROJECT = Path(__file__).resolve().parents[2]


def run_self_test():
    from revisions.seekgnomes_v1_1_1.numeric import LocalNumericalBroker
    from revisions.seekgnomes_v1_1_1.smoke_fixtures import PRODUCT_SOURCE, PRODUCT_PROOF
    from revisions.seekgnomes_v1_1_1.tools import DefinitionTools, is_verified
    from src.benchmark50_models import load_task
    (PROJECT / 'runs').mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='self-test-', dir=PROJECT / 'runs'))
    def call(broker, tool, args):
        return broker.dispatch({'tool':tool, 'arguments':args})
    result = {'schema':'seekgnomes-offline-self-test/v1',
              'created_utc':datetime.now(timezone.utc).isoformat(),
              'external_api_calls':0, 'scientific_trial':False,
              'evidence':str(out), 'checks':{}}
    task = load_task(25)
    coordinator = DefinitionTools(task, out / 'trial', web=False)
    constructor = coordinator.constructor
    definition = 'For every canonical finite Pauli word P, f(P)=1 if every nonidentity factor is X, and f(P)=0 otherwise. f(())=1. This is the uniform plus product state on the specified 2D lattice with Q=2.'
    call(constructor, 'write', {'path':'f.md','content':definition})
    handoff = call(constructor, 'create_handoff', {'definition_path':'f.md'})
    result['checks']['definition_only_handoff'] = handoff.get('action') == 'handoff'
    formal = coordinator.formalizer
    call(formal, 'write', {'path':'f.py','content':PRODUCT_SOURCE})
    call(formal, 'write', {'path':'Candidate.lean','content':PRODUCT_PROOF})
    call(formal, 'write', {'path':'formalization_map.md','content':'The definition gives the uniform plus product state. The proof identifies GeneratedState.f with plus and invokes plus_stateCondition for normalization, positivity and translation invariance. all_fold_true discharges numerical preconditions.'})
    prepared = call(formal, 'prepare', {'source_path':'f.py'})
    result['checks']['exact_definition_prepared'] = prepared.get('ok') is True
    checked = call(formal, 'score', {'source_path':'f.py','proof_path':'Candidate.lean'})
    result['positive_receipt'] = checked
    result['checks']['strict_fixed_goal_kernel'] = is_verified(checked)
    result['checks']['energy_per_qubit'] = checked.get('energy_density') == -0.5
    result['checks']['exact_gap_amplitude'] = checked.get('score_components',{}).get('amplitude') == 0.5
    result['checks']['baseline_improvement_unchanged'] = checked.get('score_components',{}).get('improvement_rate') == 0.0
    view = coordinator.return_view()
    result['checks']['constructor_receives_code'] = 'f.py' in json.dumps(view)
    call(formal, 'write', {'path':'Wrong.lean','content':'module\npublic import GeneratedState\n@[expose] public section\nnamespace Submission\ntheorem accepted : GeneratedState.ProofObligation := by sorry\nend Submission\n'})
    rejected = call(formal, 'score', {'source_path':'f.py','proof_path':'Wrong.lean'})
    result['negative_receipt'] = rejected
    result['checks']['sorry_is_rejected'] = not is_verified(rejected) and rejected.get('ok') is False
    # Each role gets different capabilities. Formalist has no numerical broker.
    denied = call(formal, 'server', {'candidate':'f.py'})
    result['checks']['formalist_cannot_run_numerics'] = denied.get('ok') is False
    experiment = out / 'experiment.py'
    experiment.write_text('''import os, socket
from pathlib import Path
import numpy as np
import scipy.linalg as la
import tenpy
import qutip
assert abs(la.eigh(np.array([[0.,1.],[1.,0.]]))[0][0]+1)<1e-12
assert qutip.sigmax().shape==(2,2)
assert not any(k in os.environ for k in ["SEEKGNOMES_API_KEY","EUREKA_API_KEY","OPENAI_API_KEY"])
assert not Path("/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/config/benchmark50_tasks.json").exists()
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError("network socket unexpectedly allowed")
print("NUMERICS_ISOLATED_OK",np.__version__,tenpy.__version__,qutip.__version__)
''')
    numeric = LocalNumericalBroker()(candidate_path=experiment, invocation_dir=out / 'numeric', wall_seconds=60)
    result['numeric_receipt'] = numeric
    result['checks']['constructor_four_libraries_and_isolation'] = numeric.get('ok') is True and 'NUMERICS_ISOLATED_OK' in numeric.get('stdout','')
    result['passed'] = all(result['checks'].values())
    result['ok'] = result['passed']
    (out / 'self-test.json').write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    return result


if __name__ == '__main__':
    report = run_self_test()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
