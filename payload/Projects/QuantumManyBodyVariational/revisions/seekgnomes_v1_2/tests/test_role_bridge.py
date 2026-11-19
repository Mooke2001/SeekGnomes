"""Role tool authority is enforced by the host, independently of MCP JSON."""
from contextlib import nullcontext
from pathlib import Path
import hashlib
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2.budget import Ledger
from revisions.seekgnomes_v1_2.gateway import RoleGateway
from revisions.seekgnomes_v1_2.runtime import NativeRole
from revisions.seekgnomes_v1_2 import runtime
from revisions.seekgnomes_v1_2.tests.test_budget_gateway import PROJECT, exchange


class RoleAuthorityTests(unittest.TestCase):
    def test_only_constructor_without_a_handoff_receives_one_closing_phase(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            for index, handoffs in enumerate(([], [{'handoff_id': 1}])):
                role = NativeRole.__new__(NativeRole)
                role.role = 'constructor'
                role.owner = SimpleNamespace(tools=SimpleNamespace(handoffs=handoffs), top={},
                    selection=SimpleNamespace(api_protocol='responses'), save=lambda: None)
                role.ledger = Ledger(Path(folder) / f'ledger-{index}.json', 'constructor', output_tokens=10)
                role.event = threading.Event()
                role.receipt = None
                role.gateway = SimpleNamespace(last_failure=None)
                role.record = lambda *value: None
                entry, _ = role.ledger.reserve_api({'input': 'explore'})
                role.ledger.settle_api(entry, {'input_tokens': 10, 'output_tokens': 10})
                role.terminal('budget_exhausted', 'output_budget_exhausted')
                if not handoffs:
                    self.assertFalse(role.event.is_set())
                    self.assertEqual(role.ledger.remaining()['output'], 50000)
                    payload = {'input': [{'role': 'user', 'content': 'preserve native history'}]}
                    prepared = role.prepare_request(payload)
                    self.assertEqual(prepared['input'], payload['input'])
                    self.assertIn(runtime.CLOSING_INSTRUCTION, prepared['instructions'])
                    self.assertNotIn('instructions', payload)
                    role.terminal('budget_exhausted', 'output_budget_exhausted')
                    self.assertTrue(role.event.is_set())
                    self.assertEqual(role.ledger.data['output_tokens_charged'], 10)
                else:
                    self.assertTrue(role.event.is_set())
                    self.assertEqual(role.ledger.data['phase'], 'exploration')
                    self.assertEqual(role.ledger.remaining()['output'], 0)

    def test_role_tool_lists_and_cross_role_rejections_over_real_unix_http(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            for role, forbidden, expected in (
                    ('constructor', 'run_lean', 'server'), ('formalizer', 'server', 'run_lean')):
                called = []
                host = NativeRole.__new__(NativeRole)
                host.role = role
                host.owner = SimpleNamespace(web=False, save=lambda: None)
                host.ledger = Ledger(Path(folder) / f'{role}.json', role)
                host.busy_tools = 0
                host.record = lambda *args: None
                host.broker = SimpleNamespace(dispatch=lambda action: called.append(action) or {'ok': True})
                gateway = RoleGateway(Path(folder) / role, role=role, model='fixture',
                    endpoint='http://127.0.0.1:1/v1', api_key='never-visible-key', protocol='responses',
                    ledger=host.ledger, tool_dispatch=host.dispatch).start()
                try:
                    status, raw, _ = exchange(gateway, '/host/tools', {'method': 'list_tools'})
                    self.assertEqual(status, 200)
                    import json
                    schema = json.loads(raw)['tools'][0]
                    allowed = schema['inputSchema']['properties']['tool']['enum']
                    self.assertIn('f.state' if role == 'constructor' else 'generated Lean goal', schema['description'])
                    self.assertNotIn('f.md', schema['description'])
                    self.assertIn(expected, allowed)
                    self.assertNotIn(forbidden, allowed)
                    for name in (forbidden, 'web_fetch', 'not_a_tool'):
                        request = {'method': 'call_tool', 'name': 'benchmark_action',
                                   'arguments': {'tool': name, 'arguments': {}}}
                        status, raw, _ = exchange(gateway, '/host/tools', request)
                        self.assertEqual(status, 200)
                        self.assertEqual(json.loads(raw)['error'], 'role_tool_forbidden')
                    self.assertFalse(called)
                    self.assertEqual(host.ledger.data['requests'], 0)
                finally:
                    gateway.close()

    def test_cancellation_while_initializing_cannot_later_start_a_native_role(self):
        entered, release = threading.Event(), threading.Event()
        started, answers, errors = [], [], []
        def initialize(trial, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError('Initialization fixture was not released')
            trial.cancelled = False
            trial.finished = False
            trial.out = kwargs['out']
            trial.cpus = 1
            trial.roles = []
            trial.top = {'best_verified_result': None}
            trial.tools = SimpleNamespace(constructor=object())
            trial.save = lambda: None
        def make_role(trial, role, broker):
            started.append(role)
            return SimpleNamespace(run=lambda: {'session_status': 'completed'}, cancel=lambda: None)
        def call():
            try:
                answers.append(runtime.run_trial(out=runtime.PROJECT/'runs'/'fixture-cancel-init'))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(runtime.TrialRuntime, '__init__', new=initialize), patch.object(
                runtime.TrialRuntime, 'make_role', new=make_role):
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                runtime.cancel_active_trials()
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(started, 'A native role started after initialization had been cancelled')
        self.assertFalse(errors, errors)
        self.assertEqual(len(answers), 1)
        self.assertIn(answers[0]['status'], ('cancelled_by_operator', 'cancelled_before_start'))

    def test_real_frozen_handoffs_unresolved_return_to_same_constructor(self):
        self._exercise_handoffs(real_lean=False)

    @unittest.skipUnless(os.environ.get('SEEKGNOMES_RUN_LEAN_HANDOFF_TEST') == '1',
        'Opt-in real Lean handoff test; release self-test independently checks the strict kernel')
    def test_real_frozen_handoffs_unresolved_then_strict_proof_return_to_same_constructor(self):
        self._exercise_handoffs(real_lean=True)

    def _exercise_handoffs(self, *, real_lean):
        """NativeRole.run is a host fixture; definitions and Lean are real.

        This is a controller regression, never a model performance trial.
        The first Formalist fixture reports an obstacle. The second invokes
        the unchanged real compiler, fixed proof goal and Lean kernel.
        """
        from revisions.seekgnomes_v1_1_1.smoke_fixtures import PRODUCT_SOURCE, PRODUCT_PROOF
        from revisions.seekgnomes_v1_1_1.tools import is_verified
        import json
        # Keep all proof-stage diagnostics, including a technical timeout.
        with nullcontext(tempfile.mkdtemp(dir=PROJECT / 'runs', prefix='fixture-native-handoff-')) as folder:
            trial = runtime.TrialRuntime(task_id='25', out=Path(folder) / 'trial',
                model='fixture-gpt', company='openai', endpoint='https://unused.invalid/v1',
                api_key='offline-fixture-key', budget_multiplier=1, web=False, candidate_format='legacy')
            trial.top.update(scientific_trial=False, host_fixture_only=True, external_api_calls=0)
            constructor = trial.make_role('constructor', trial.tools.constructor)
            constructor.ledger.resume()
            entry, _ = constructor.ledger.reserve_api({'input': 'host-only fixture accounting'})
            constructor.ledger.settle_api(entry, {'input_tokens': 11, 'output_tokens': 9})
            definition = ('For every canonical finite Pauli word P, f(P)=1 if all nonidentity '
                'factors are X, and f(P)=0 otherwise. f(())=1. This is the uniform plus '
                'product state on the specified 2D lattice with Q=2.')
            definitions = [definition, definition + ' The value does not depend on the coordinates.']
            calls = []
            def action(role, tool, arguments):
                return role.dispatch({'method': 'call_tool', 'name': 'benchmark_action',
                                     'arguments': {'tool': tool, 'arguments': arguments}})
            def formalist_fixture(formalist):
                calls.append(formalist)
                self.assertEqual(formalist.role, 'formalizer')
                self.assertFalse(constructor.active)
                self.assertIsNone(constructor.ledger.active_since)
                self.assertEqual(constructor.ledger.data['requests'], 1)
                self.assertEqual(constructor.ledger.data['output_tokens_charged'], 9)
                self.assertEqual(formalist.base.references['handoff/f.md'], definitions[len(calls) - 1])
                formalist.ledger.resume()
                try:
                    if len(calls) == 1 or not real_lean:
                        reply = action(formalist, 'report_issue', {'kind': 'unresolved',
                            'detail': 'Offline controller fixture: exercise the explicit obstacle path.'})
                    else:
                        for name, content in (('f.py', PRODUCT_SOURCE), ('Candidate.lean', PRODUCT_PROOF),
                            ('formalization_map.md', 'The complete definition is the uniform plus product state. '
                             'GeneratedState.f is identified with plus; plus_stateCondition proves the fixed '
                             'normalization, positivity and translation-invariance obligation.')):
                            self.assertTrue(action(formalist, 'write', {'path': name, 'content': content})['ok'])
                        reply = action(formalist, 'score', {'source_path': 'f.py', 'proof_path': 'Candidate.lean'})
                        self.assertTrue(is_verified(reply), reply)
                    self.assertEqual(reply.get('action'), 'feedback', reply)
                    return formalist.receipt
                finally:
                    formalist.active = False
                    formalist.closed = True
                    formalist.ledger.pause()
                    formalist.gateway.close()
                    formalist.save()
            try:
                with patch.object(runtime.NativeRole, 'run', new=formalist_fixture):
                    responses = []
                    for text in definitions:
                        self.assertTrue(action(constructor, 'write', {'path': 'f.md', 'content': text})['ok'])
                        responses.append(action(constructor, 'create_handoff', {'definition_path': 'f.md'}))
                        self.assertTrue(constructor.active)
                        self.assertIsNotNone(constructor.ledger.active_since)
                        self.assertEqual(constructor.ledger.data['requests'], 1)
                        self.assertEqual(constructor.ledger.data['output_tokens_charged'], 9)
                self.assertEqual(responses[0]['kind'], 'formalist_feedback')
                self.assertEqual(responses[0]['feedback']['issue_kind'], 'unresolved')
                self.assertIsNone(responses[0]['energy_density'])
                if real_lean:
                    self.assertEqual(responses[1]['kind'], 'verified_state_energy')
                    self.assertEqual(responses[1]['result']['energy_density'], -0.5)
                    self.assertTrue(is_verified(trial.top['best_verified_result']))
                else:
                    self.assertEqual(responses[1]['kind'], 'formalist_feedback')
                    self.assertIsNone(trial.top['best_verified_result'])
                self.assertEqual(len(trial.tools.handoffs), 2)
                self.assertEqual(len({str(role.sandbox.home) for role in calls}), 2)
                for index, text in enumerate(definitions, 1):
                    root = trial.out / 'handoffs' / f'handoff-{index:04d}'
                    frozen = (root / 'f.md').read_bytes()
                    self.assertEqual(frozen.decode(), text)
                    self.assertEqual(json.loads((root / 'manifest.json').read_text())['definition_sha256'],
                                     hashlib.sha256(frozen).hexdigest())
                self.assertIn('COMPLETE f.md', responses[1]['instruction'])
                if real_lean:
                    self.assertIn('f.py', responses[1]['return_files'])
            finally:
                constructor.active = False
                constructor.closed = True
                constructor.ledger.pause()
                constructor.gateway.close()
                for formalist in calls:
                    formalist.cancel()


if __name__ == '__main__':
    unittest.main()
