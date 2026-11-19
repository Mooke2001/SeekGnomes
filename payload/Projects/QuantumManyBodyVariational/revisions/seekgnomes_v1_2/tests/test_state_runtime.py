"""Controller regressions for the native .state handoff and feedback boundary."""

from pathlib import Path
import errno
import hashlib
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2 import runtime


def _action(role, tool, arguments):
    return role.dispatch({'method': 'call_tool', 'name': 'benchmark_action',
                          'arguments': {'tool': tool, 'arguments': arguments}})


class StateRuntimeTests(unittest.TestCase):
    def test_retryable_host_verifier_failure_keeps_formalist_active(self):
        role = runtime.NativeRole.__new__(runtime.NativeRole)
        role.role = 'formalizer'
        role.owner = SimpleNamespace(web=False, candidate_format='state', save=lambda: None)
        role.busy_tools = 0
        role.active = True
        role.ledger = SimpleNamespace(remaining=lambda: {})
        role.event = threading.Event()
        role.receipt = None
        role.record = lambda *args: None
        checked = {'ok': False, 'proof_verified': False, 'goal_matched': False}
        role.broker = SimpleNamespace(dispatch=lambda action: checked)
        _action(role, 'score', {'proof_path': 'Candidate.lean'})
        self.assertFalse(role.event.is_set())
        self.assertIsNone(role.receipt)

        checked['technical_diagnostic'] = {
            'kind': 'strict_verifier_environment_failure', 'stage': 'compile-generic-LocalPauli',
            'timed_out': True, 'retryable': True,
        }
        retryable = _action(role, 'score', {'proof_path': 'Candidate.lean'})
        self.assertFalse(retryable['ok'])
        self.assertTrue(retryable['technical_diagnostic']['retryable'])
        self.assertTrue(role.active)
        self.assertFalse(role.event.is_set())
        self.assertIsNone(role.receipt)
        self.assertNotIn('energy_density', retryable)
        self.assertEqual(retryable['instruction'], runtime.FORMALIST_RETRY_CHECK_INSTRUCTION)

        checked.clear()
        checked.update(ok=True, proof_verified=True)
        self.assertTrue(_action(role, 'run_lean', {'proof_path': 'Candidate.lean'})['proof_verified'])
        self.assertFalse(role.event.is_set())
        self.assertIsNone(role.receipt)

        checked.update(ok=False, proof_verified=False)
        checked['technical_diagnostic'] = {
            'kind': 'strict_verifier_environment_failure', 'stage': 'lock-before',
            'timed_out': False,
        }
        _action(role, 'run_lean', {'proof_path': 'Candidate.lean'})
        self.assertTrue(role.event.is_set())
        self.assertEqual(role.receipt['session_status'], 'infrastructure_failure')
        self.assertFalse(role.receipt['proof_verified'])
        self.assertNotIn('energy_density', role.receipt)

    def test_invalid_provider_response_allows_formalist_recovery(self):
        role = runtime.NativeRole.__new__(runtime.NativeRole)
        role.role = 'formalizer'
        role.owner = SimpleNamespace(candidate_format='state')
        role.event = threading.Event()
        role.receipt = None

        role.terminal('infrastructure_failure', 'invalid_native_provider_response')
        self.assertFalse(role.event.is_set())
        self.assertIsNone(role.receipt)

        role.terminal('infrastructure_failure', 'model_identity_mismatch_or_missing')
        self.assertTrue(role.event.is_set())
        self.assertEqual(role.receipt['reason'], 'model_identity_mismatch_or_missing')

    def test_handoff_rejects_unresolved_proof_and_accepts_concrete_source_defect(self):
        source = 'def f(P: Word) -> Real:\n    return 2 if len(P) == 0 else 0\n'
        guide = (runtime.HERE / 'STATE_USAGE.md').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory(dir=runtime.PROJECT / 'runs', prefix='fixture-state-runtime-') as folder:
            trial = runtime.TrialRuntime(task_id='1', out=Path(folder) / 'trial',
                model='fixture-gpt', company='openai', endpoint='https://unused.invalid/v1',
                api_key='offline-fixture-key', budget_multiplier=1, web=False, cpus=1)
            constructor = trial.make_role('constructor', trial.tools.constructor)
            constructor.ledger.resume()
            self.assertEqual(trial.top['protocol'], runtime.STATE_PROTOCOL)
            self.assertEqual(trial.top['provenance']['constructor_submission'], 'complete f.state only')
            self.assertIn('write a complete f.state', constructor.closing_instruction)
            self.assertIn('f.state', constructor.prompt)
            self.assertIn('reference/state_language.md', constructor.prompt)
            self.assertEqual(_action(constructor, 'read', {'path': 'reference/state_language.md'})['content'], guide)
            self.assertNotIn('python_language.md', constructor.base.references)

            def run_formalist(formalist):
                try:
                    self.assertFalse(constructor.active)
                    self.assertIsNone(constructor.ledger.active_since)
                    self.assertEqual(formalist.base.references['handoff/f.state'], source)
                    self.assertIn('reference/state_language.md', formalist.prompt)
                    self.assertEqual(_action(formalist, 'read', {'path': 'reference/state_language.md'})['content'], guide)
                    self.assertIn('GeneratedState.ProofObligation', formalist.prompt)
                    self.assertNotIn('f.py', formalist.prompt)
                    self.assertNotIn('formalization_map', formalist.prompt)
                    self.assertNotIn('python_language.md', formalist.base.references)
                    self.assertFalse(_action(formalist, 'prepare', {'source_path': 'f.py'})['ok'])
                    prepared = _action(formalist, 'prepare', {})
                    self.assertTrue(prepared['ok'], prepared)
                    self.assertNotIn('energy_density', prepared)
                    generated = _action(formalist, 'read', {'path': prepared['generated_paths']['generated_state']})
                    self.assertTrue(generated['ok'], generated)
                    self.assertEqual(prepared['source_sha256'], hashlib.sha256(source.encode()).hexdigest())
                    schema = formalist.dispatch({'method': 'list_tools'})['tools'][0]['inputSchema']
                    self.assertNotIn('finish', schema['properties']['tool']['enum'])
                    self.assertEqual(_action(formalist, 'finish', {})['error'], 'role_tool_forbidden')
                    for kind in ('unresolved', 'infrastructure'):
                        rejected = _action(formalist, 'report_issue', {'kind': kind,
                            'detail': 'The proof requires a missing lemma.'})
                        self.assertEqual(rejected['error'], 'formalist_proof_required')
                        self.assertIsNone(formalist.receipt)
                        self.assertFalse(formalist.event.is_set())
                        self.assertFalse(constructor.active)
                    self.assertTrue(_action(formalist, 'report_issue', {'kind': 'definition_inconsistent',
                        'detail': 'f(I) equals 2 by the empty-support branch and must equal 1.'})['ok'])
                    return formalist.receipt
                finally:
                    formalist.cancel()
                    formalist.gateway.close()

            try:
                self.assertTrue(_action(constructor, 'write', {'path': 'f.state', 'content': source})['ok'])
                with patch.object(runtime.NativeRole, 'run', new=run_formalist):
                    receipt = _action(constructor, 'create_handoff', {'definition_path': 'f.state'})
                self.assertTrue(constructor.active)
                self.assertIsNotNone(constructor.ledger.active_since)
                self.assertEqual(receipt['kind'], 'formalist_feedback')
                self.assertEqual(receipt['feedback']['issue_kind'], 'definition_inconsistent')
                self.assertIsNone(receipt['energy_density'])
                self.assertIsNone(trial.top['best_verified_result'])
                self.assertEqual(len(trial.tools.handoffs), 1)
                self.assertIn('COMPLETE f.state', receipt['instruction'])
                self.assertIn('f.state', receipt['return_files'])
                self.assertNotIn('f.py', receipt['return_files'])
                self.assertEqual((trial.out / 'handoffs/handoff-0001/f.state').read_text(), source)
            finally:
                constructor.cancel()
                constructor.gateway.close()

    def test_formalist_exits_resume_without_a_score_or_attempt_limit(self):
        self._check_formalist_resume(cancel_during_retry=False)

    def test_cancellation_during_formalist_recovery_does_not_relaunch(self):
        self._check_formalist_resume(cancel_during_retry=True)

    def _check_formalist_resume(self, *, cancel_during_retry):
        source = 'def f(P: Word) -> Real:\n    return 2 if len(P) == 0 else 0\n'
        run_native = runtime.NativeRole.run
        thread_id = '55cf55d5-55cf-4559-a460-30cadeaff138'
        proof = 'import GeneratedState\n'
        exit_codes = (None, 17, 0, 0, 17, 0, 0)
        first_session_attempt = 2
        with tempfile.TemporaryDirectory(dir=runtime.PROJECT / 'runs', prefix='fixture-state-resume-') as folder:
            trial = runtime.TrialRuntime(task_id=1, out=Path(folder) / 'trial',
                model='fixture-gpt', company='openai', endpoint='https://unused.invalid/v1',
                api_key='offline-fixture-key', budget_multiplier=1, web=False, cpus=1)
            constructor = trial.make_role('constructor', trial.tools.constructor)
            starts = []

            def run_formalist(formalist):
                workspace = formalist.base.workspace
                session_home = formalist.sandbox.home
                session = session_home / '.codex' / 'sessions' / f'rollout-{thread_id}.jsonl'
                proof_path = workspace / 'Candidate.lean'
                handoff = formalist.broker._handoff
                self.assertTrue(_action(formalist, 'write', {'path': 'Candidate.lean',
                    'content': proof})['ok'])

                def start(invocation, socket_path, *, append_logs):
                    attempt = len(starts)
                    self.assertLess(attempt, 1 if cancel_during_retry else len(exit_codes))
                    self.assertFalse(constructor.active)
                    self.assertIsNone(constructor.ledger.active_since)
                    self.assertIs(formalist.broker._handoff, handoff)
                    self.assertEqual(formalist.base.workspace, workspace)
                    self.assertEqual(formalist.sandbox.home, session_home)
                    self.assertEqual(formalist.base.references['handoff/f.state'], source)
                    self.assertEqual(proof_path.read_text(), proof)
                    self.assertEqual(socket_path, formalist.gateway.socket_path)
                    self.assertEqual(formalist.ledger.data['phase'], 'formalization')
                    self.assertEqual(append_logs, attempt > 0)
                    if attempt > first_session_attempt:
                        self.assertIn('resume', invocation.argv)
                        self.assertEqual(json.loads(session.read_text())['thread_id'], thread_id)
                        if invocation.resume_id is not None:
                            self.assertEqual(invocation.resume_id, thread_id)
                        else:
                            self.assertTrue(invocation.resume_last)
                    else:
                        self.assertFalse(invocation.resume_last)
                        self.assertIsNone(invocation.resume_id)
                        self.assertFalse(session.exists())
                        if attempt == first_session_attempt:
                            session.parent.mkdir(parents=True)
                            session.write_text(json.dumps({'thread_id': thread_id}) + '\n')
                    starts.append(invocation)
                    with (formalist.run_dir / 'native.stdout.log').open('a', encoding='utf-8') as log:
                        if session.exists():
                            log.write(json.dumps({'type': 'thread.started', 'thread_id': thread_id}) + '\n')

                    if attempt == len(exit_codes) - 2:
                        formalist.gateway.last_failure = {'type': 'TimeoutError'}
                    if attempt == len(exit_codes) - 1:
                        issue = _action(formalist, 'report_issue', {'kind': 'definition_inconsistent',
                            'detail': 'f(I) equals 2 by the empty-support branch and must equal 1.'})
                        self.assertTrue(issue['ok'], issue)
                    code = exit_codes[attempt]
                    if code is None:
                        raise OSError(errno.EAGAIN, 'Temporary fixture resource pressure')
                    return SimpleNamespace(poll=lambda: code, returncode=code)

                def wait_for_retry(_seconds):
                    if cancel_during_retry:
                        trial.cancelled = True
                        formalist.cancel()
                    return formalist.event.is_set()

                with (patch.object(formalist.sandbox, 'start', side_effect=start),
                      patch.object(formalist.event, 'wait', side_effect=wait_for_retry)):
                    return run_native(formalist)

            try:
                constructor.ledger.resume()
                self.assertTrue(_action(constructor, 'write', {'path': 'f.state', 'content': source})['ok'])
                with patch.object(runtime.NativeRole, 'run', new=run_formalist):
                    receipt = _action(constructor, 'create_handoff', {'definition_path': 'f.state'})
                self.assertEqual(len(trial.tools.handoffs), 1)
                self.assertEqual(len(trial.roles), 2)
                formalist = trial.roles[-1]
                if cancel_during_retry:
                    self.assertEqual(len(starts), 1)
                    self.assertEqual(formalist.receipt['session_status'], 'cancelled_by_operator')
                    self.assertEqual(receipt['kind'], 'cancelled_by_operator')
                    self.assertEqual(trial.top['feedback'], [])
                    self.assertFalse(constructor.active)
                    self.assertIsNone(constructor.ledger.active_since)
                else:
                    self.assertEqual(len(starts), len(exit_codes))
                    prompt = formalist.base.workspace / 'seekgnomes-prove.md'
                    self.assertIn(runtime.FORMALIST_RESUME_INSTRUCTION, prompt.read_text())
                    self.assertFalse((prompt.parent / 'seekgnomes-closing.md').exists())
                    self.assertTrue(receipt['ok'])
                    self.assertEqual(receipt['kind'], 'formalist_feedback')
                    self.assertEqual(receipt['feedback']['issue_kind'], 'definition_inconsistent')
                    self.assertTrue(constructor.active)
                self.assertIsNone(receipt.get('energy_density'))
                self.assertIsNone(trial.top['best_verified_result'])
            finally:
                constructor.cancel()
                constructor.gateway.close()

    def test_each_rejected_score_allows_same_session_resume_after_clean_exit(self):
        source = 'def f(P: Word) -> Real:\n    return 2 if len(P) == 0 else 0\n'
        digest = hashlib.sha256(source.encode()).hexdigest()
        run_native = runtime.NativeRole.run
        with tempfile.TemporaryDirectory(dir=runtime.PROJECT / 'runs', prefix='fixture-score-resume-') as folder:
            trial = runtime.TrialRuntime(task_id=1, out=Path(folder) / 'trial',
                model='fixture-gpt', company='openai', endpoint='https://unused.invalid/v1',
                api_key='offline-fixture-key', budget_multiplier=1, web=False, cpus=1)
            constructor = trial.make_role('constructor', trial.tools.constructor)
            calls = []
            starts = []

            def reject_score(*, source_path, proof_path, invocation_dir):
                self.assertEqual(source_path.read_text(), source)
                calls.append((invocation_dir, proof_path.read_text()))
                return {'ok': False, 'goal_matched': False,
                        'diagnostics': {'judge': 'Const not found in solution: Submission.accepted'}}

            def run_formalist(formalist):
                workspace = formalist.base.workspace
                session_home = formalist.sandbox.home
                gateway_socket = formalist.gateway.socket_path
                proof_file = workspace / 'Candidate.lean'
                handoff = formalist.broker._handoff
                proof_attempts = ['import GeneratedState\n', 'import GeneratedState\n-- partial proof\n']

                def start(invocation, socket_path, *, append_logs):
                    attempt = len(starts)
                    self.assertLessEqual(attempt, len(proof_attempts))
                    self.assertFalse(constructor.active)
                    self.assertIsNone(constructor.ledger.active_since)
                    self.assertIs(formalist.broker._handoff, handoff)
                    self.assertEqual(formalist.base.workspace, workspace)
                    self.assertEqual(formalist.sandbox.home, session_home)
                    self.assertEqual(socket_path, gateway_socket)
                    self.assertEqual(formalist.base.references['handoff/f.state'], source)
                    self.assertEqual(invocation.resume_last, attempt > 0)
                    self.assertEqual(append_logs, attempt > 0)
                    starts.append(invocation)
                    if attempt > 0:
                        self.assertEqual(proof_file.read_text(), proof_attempts[attempt - 1])
                    if attempt < len(proof_attempts):
                        wrote = _action(formalist, 'write', {'path': 'Candidate.lean',
                            'content': proof_attempts[attempt]})
                        self.assertTrue(wrote['ok'], wrote)
                        rejected = _action(formalist, 'score', {'proof_path': 'Candidate.lean'})
                        self.assertFalse(rejected['ok'])
                        self.assertFalse(rejected['proof_verified'])
                        self.assertIn('Submission.accepted', rejected['diagnostics']['judge'])
                        self.assertEqual(rejected['instruction'], runtime.FORMALIST_RESUME_INSTRUCTION)
                        self.assertEqual(rejected['source_sha256'], digest)
                        self.assertNotIn('energy_density', rejected)
                        self.assertFalse(formalist.event.is_set())
                    else:
                        issue = _action(formalist, 'report_issue', {'kind': 'definition_inconsistent',
                            'detail': 'f(I) equals 2 by the empty-support branch and must equal 1.'})
                        self.assertTrue(issue['ok'], issue)
                    return SimpleNamespace(poll=lambda: 0, returncode=0)

                with (patch.object(formalist.sandbox, 'start', side_effect=start),
                      patch.object(formalist.event, 'wait', return_value=False)):
                    return run_native(formalist)

            try:
                constructor.ledger.resume()
                self.assertTrue(_action(constructor, 'write', {'path': 'f.state', 'content': source})['ok'])
                with (patch.object(trial.tools, 'score_callback', side_effect=reject_score),
                      patch.object(runtime.NativeRole, 'run', new=run_formalist)):
                    receipt = _action(constructor, 'create_handoff', {'definition_path': 'f.state'})
                self.assertEqual(len(starts), 3)
                self.assertEqual(len(calls), 2)
                first_check, second_check = calls
                self.assertNotEqual(first_check[0], second_check[0])
                self.assertNotEqual(first_check[1], second_check[1])
                self.assertEqual(len(trial.roles), 2)
                self.assertEqual(len(trial.tools.handoffs), 1)
                self.assertEqual(receipt['definition_sha256'], digest)
                self.assertIsNone(receipt['energy_density'])
                self.assertIsNone(trial.top['best_verified_result'])
                self.assertEqual(receipt['kind'], 'formalist_feedback')
                self.assertEqual(receipt['feedback']['issue_kind'], 'definition_inconsistent')
                self.assertTrue(constructor.active)
            finally:
                constructor.cancel()
                constructor.gateway.close()


if __name__ == '__main__':
    unittest.main()
