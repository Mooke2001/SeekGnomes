"""Kill an actual supervised controller while its two native roles are live."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2.harbor_adapter import run_harbor_trial
from revisions.seekgnomes_v1_2.tests.test_budget_gateway import provider_fixture
from revisions.seekgnomes_v1_2.tests.test_native import _codex_fixture_sse


PROJECT = Path(__file__).resolve().parents[3]


def read_json(path):
    return json.loads(path.read_bytes())


def thread_ids(role_dir):
    events = (role_dir / 'native.stdout.log').read_text().splitlines()
    return [event['thread_id'] for line in events if line.startswith('{')
            and (event := json.loads(line)).get('type') == 'thread.started']


def tool_call(tool, arguments):
    request = json.dumps({'tool': tool, 'arguments': arguments})
    return {'type': 'custom', 'name': 'exec', 'namespace': 'functions',
            'input': 'const entry = ALL_TOOLS.find(x => x.name.includes("benchmark_action")); '
                     f'text(await tools[entry.name]({request}));'}


def recovered_feedback(request, call_id):
    for item in request['input']:
        if item.get('type') not in ('function_call_output', 'custom_tool_call_output'):
            continue
        if item.get('call_id') != call_id:
            continue
        output = item['output']
        texts = [output] if isinstance(output, str) else [part['text'] for part in output]
        for line in (line for text in texts for line in text.splitlines()):
            try:
                for block in json.loads(line).get('content', []):
                    result = json.loads(block['text'])
                    if result.get('kind') == 'verified_state_energy':
                        return result
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
    return None


def request_role(request):
    for item in request['input']:
        if item.get('type') != 'message' or item.get('role') != 'user':
            continue
        for part in item['content']:
            text = part.get('text', '')
            if text.startswith('You are the Constructor in SeekGnomes.'):
                return 'constructor'
            if text.startswith('You are the Formalist in SeekGnomes.'):
                return 'formalizer'
    raise ValueError('Fixture received a request without its SeekGnomes role prompt')


@unittest.skipUnless(os.environ.get('SEEKGNOMES_REAL_CODEX'), 'Pinned Codex binary is required')
class ControllerRecoveryTests(unittest.TestCase):
    def test_sigkill_preserves_pending_handoff_sessions_and_budgets(self):
        root = Path(tempfile.mkdtemp(prefix='native-controller-recovery-fixture-', dir=PROJECT / 'runs'))
        trial_dir = root / 'trial'
        constructor = trial_dir / 'constructor'
        formalist = trial_dir / 'formalizer/handoff-0001'
        frozen = trial_dir / 'handoffs/handoff-0001/f.state'
        harness = root / 'harness'
        (harness / 'bin').mkdir(parents=True)
        binary = Path(os.environ['SEEKGNOMES_REAL_CODEX']).resolve(strict=True)
        for name in ('codex', 'codex-code-mode-host'):
            shutil.copy2(binary.with_name(name), harness / 'bin' / name)
        examples = PROJECT / 'examples/state_format'
        source = (examples / 'tracial.state').read_text()
        proof = (examples / 'TracialProof.lean').read_text()
        source_digest = sha256(source.encode()).hexdigest()
        proof_digest = sha256(proof.encode()).hexdigest()
        model = 'gpt-6.1-terra'
        requests = {'constructor': [], 'formalizer': []}
        interrupted = {}
        fixture_errors = []
        recovered_call = None
        calls = {
            ('constructor', 1): tool_call('write', {'path': 'f.state', 'content': source}),
            ('constructor', 2): tool_call('create_handoff', {'definition_path': 'f.state'}),
            ('formalizer', 1): tool_call('write', {'path': 'Candidate.lean', 'content': proof}),
            ('formalizer', 3): tool_call('score', {'proof_path': 'Candidate.lean'}),
            ('constructor', 3): tool_call('recover_handoff', {'handoff_id': 1}),
            ('constructor', 4): tool_call('finish', {'reason': 'received the recovered verified energy'}),
        }

        def kill_controller():
            manifest = read_json(trial_dir / 'manifest.json')
            self.assertEqual(manifest['status'], 'formalizer_running')
            self.assertEqual(frozen.read_text(), source)
            self.assertEqual((formalist / 'workspace/Candidate.lean').read_text(), proof)
            for role_dir in (constructor, formalist):
                ids = thread_ids(role_dir)
                self.assertEqual(len(ids), 1)
                interrupted[role_dir.name] = {'thread_id': ids[0],
                    'ledger': read_json(role_dir / 'budget-ledger.json')}
            self.assertTrue(interrupted[formalist.name]['ledger']['entries'][-1]['in_flight'])
            pid = manifest['controller_pid']
            worker = os.open(f'/proc/{pid}', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with os.fdopen(os.open('status', os.O_RDONLY, dir_fd=worker)) as stream:
                    status = stream.read().splitlines()
                self.assertIn(f'PPid:\t{os.getpid()}', status)
                signal.pidfd_send_signal(worker, signal.SIGKILL)
            finally:
                os.close(worker)
            interrupted['controller_pid'] = pid

        def respond(handler):
            nonlocal recovered_call
            request = seen[-1]['payload']
            role = request_role(request)
            requests[role].append(request)
            stage = role, len(requests[role])
            index = len(seen)
            if stage == ('formalizer', 2):
                try:
                    kill_controller()
                    return
                except (AssertionError, KeyError, OSError, ValueError) as error:
                    fixture_errors.append(str(error))
            if stage == ('constructor', 3):
                recovered_call = f'call_local_{index}'
            raw = _codex_fixture_sse(model, index, tool=calls.get(stage))
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Content-Length', str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)

        def deadline(signum, frame):
            raise TimeoutError(f'Native controller fixture did not complete; inspect {root}')

        previous_handler = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(240)
        try:
            with provider_fixture([respond]) as (endpoint, seen), patch.dict(os.environ, {
                    'SEEKGNOMES_HARNESS_ROOT': str(harness)}):
                self.assertTrue(endpoint.startswith('http://127.0.0.1:'))
                result = run_harbor_trial(task_id=11, out=trial_dir, model=model,
                    endpoint=endpoint, api_key='offline-controller-fixture-key', company='openai',
                    web=False, constructor_output_tokens=20000, constructor_active_seconds=120, cpus=1)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

        self.assertFalse(fixture_errors, (str(root), fixture_errors))
        self.assertIsInstance(interrupted.get('controller_pid'), int, str(root))
        harbor_trial = result['harbor']['trial_result'] or {}
        technical_error = (harbor_trial.get('exception_info') or {}).get('exception_message')
        self.assertEqual(result['status'], 'completed', (str(root), result.get('error_type'), technical_error))
        self.assertEqual(result['controller_restarts'], 1)
        self.assertNotEqual(result['controller_pid'], interrupted['controller_pid'])
        self.assertEqual(len(result['handoffs']), 1)
        self.assertEqual(result['handoffs'][0]['definition_sha256'], source_digest)
        self.assertEqual(len(list(trial_dir.glob('feedback-*.json'))), 1)
        feedback = result['feedback']
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]['kind'], 'verified_state_energy')
        self.assertEqual(feedback[0]['definition_sha256'], source_digest)
        proof_result = feedback[0]['result']
        self.assertTrue(proof_result['kernel_verified'])
        self.assertEqual(proof_result['source_sha256'], source_digest)
        self.assertEqual(proof_result['candidate_source_sha256'], proof_digest)
        self.assertEqual(proof_result['energy_density'], -0.375)
        self.assertGreaterEqual(len(requests['constructor']), 4)
        self.assertGreaterEqual(len(requests['formalizer']), 3)
        delivered = recovered_feedback(requests['constructor'][3], recovered_call)
        self.assertIsNotNone(delivered, str(root))
        self.assertEqual(delivered['definition_sha256'], source_digest)
        self.assertEqual(delivered['result']['energy_density'], -0.375)

        for role_dir, role in ((constructor, 'constructor'), (formalist, 'formalizer')):
            before = interrupted[role_dir.name]
            self.assertEqual(thread_ids(role_dir), [before['thread_id'], before['thread_id']])
            budget = read_json(role_dir / 'budget-ledger.json')
            self.assertEqual(budget['requests'], len(requests[role]))
            added_requests = len(requests[role]) - before['ledger']['requests']
            self.assertEqual(budget['output_tokens_charged'],
                before['ledger']['output_tokens_charged'] + 7 * added_requests)
            self.assertGreaterEqual(budget['active_wall_seconds'], before['ledger']['active_wall_seconds'])
        budget = read_json(formalist / 'budget-ledger.json')
        indices = [entry['index'] for entry in budget['entries'] if entry['kind'] == 'api']
        self.assertEqual(indices, list(range(len(requests['formalizer']))))
        self.assertEqual(budget['entries'][1]['settlement_reason'], 'controller_interrupted')
        restarts = list((root / '.harbor').glob('**/host-environment/logs/artifacts/controller-restarts.json'))
        self.assertEqual(len(restarts), 1, str(root))
        self.assertEqual(read_json(restarts[0])['controller_interruptions'],
                         [{'exit': -signal.SIGKILL, 'checkpoint_exists': True}])
        self.assertEqual(result['constructor_terminal']['action'], 'finish')
        self.assertIsNone(harbor_trial['exception_info'])


if __name__ == '__main__':
    unittest.main()
