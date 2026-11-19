"""Offline budget and native API tests. Every provider here is localhost."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import quote
from unittest.mock import patch

from revisions.seekgnomes_v1_2.budget import BudgetExceeded, Ledger, Limits
from revisions.seekgnomes_v1_2.gateway import RoleGateway

PROJECT = Path(__file__).resolve().parents[3]


class UnixClient(http.client.HTTPConnection):
    def __init__(self, gateway):
        super().__init__('localhost', timeout=10)
        self.gateway = gateway

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(f'/proc/self/fd/{self.gateway.dir_fd}/broker.sock')


def exchange(gateway, path, value=None, *, headers=None, method='POST'):
    connection = UnixClient(gateway)
    try:
        connection.request(method, path, None if value is None else json.dumps(value),
                           {'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


@contextmanager
def provider_fixture(plans):
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            observed.append({'path': self.path, 'headers': dict(self.headers),
                             'payload': json.loads(body)})
            plan = plans[min(len(observed) - 1, len(plans) - 1)]
            if callable(plan):
                plan(self)
                return
            status, raw, content_type = plan
            if not isinstance(raw, bytes):
                raw = json.dumps(raw).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class BudgetTests(unittest.TestCase):
    def test_configurable_limits_no_expansion_and_no_formalist_cumulative_limit(self):
        constructor = Limits.for_role('constructor', output_tokens=12345, active_seconds=9876, cpus=3)
        self.assertEqual(constructor.output, 12345)
        self.assertEqual(constructor.active_seconds, 9876)
        self.assertEqual(constructor.cpus, 3)
        self.assertIsNone(constructor.input)
        self.assertIsNone(constructor.requests)
        self.assertIsNone(constructor.numerical_cpu)
        self.assertEqual(Limits.for_role('constructor', input_tokens=4567).input, 4567)
        for tier in (2, 4):
            with self.assertRaises(ValueError):
                Limits.for_role('constructor', tier)
        formal = Limits.for_role('formalizer')
        self.assertIsNone(formal.output)
        self.assertIsNone(formal.input)
        self.assertIsNone(formal.requests)
        self.assertIsNone(formal.active_seconds)
        self.assertEqual(formal.request_output, 16000)

    def test_compaction_and_retry_share_one_ledger_and_pause_does_not_reset(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            ordinary, cap = ledger.reserve_api({'model': 'fixture', 'input': 'physics'})
            ledger.settle_api(ordinary, {'input_tokens': 100, 'output_tokens': 40})
            compact, _ = ledger.reserve_api({'model': 'fixture', 'input': 'native context compaction'})
            ledger.settle_api(compact, {'input_tokens': 60, 'output_tokens': 20})
            with patch('revisions.seekgnomes_v1_2.budget.time.monotonic', return_value=10):
                ledger.resume()
            with patch('revisions.seekgnomes_v1_2.budget.time.monotonic', return_value=15):
                ledger.pause()
            before = ledger.snapshot()
            self.assertEqual(before['requests'], 2)
            self.assertEqual(before['output_tokens_charged'], 60)
            self.assertEqual(before['input_tokens_charged'], 160)
            self.assertEqual(before['active_wall_seconds'], 5)
            with patch('revisions.seekgnomes_v1_2.budget.time.monotonic', return_value=100):
                ledger.resume()
            with patch('revisions.seekgnomes_v1_2.budget.time.monotonic', return_value=104):
                ledger.pause()
            retry, _ = ledger.reserve_api({'model': 'fixture', 'input': 'native retry'})
            ledger.settle_api(retry, {'input_tokens': 10, 'output_tokens': 5})
            after = ledger.snapshot()
            self.assertEqual(after['active_wall_seconds'], 9)
            self.assertEqual(after['requests'], 3)
            self.assertEqual(after['output_tokens_charged'], 65)
            persisted = json.loads(ledger.path.read_text())
            self.assertEqual(persisted['requests'], 3)
            with self.assertRaises((ValueError, FileExistsError, RuntimeError)):
                Ledger(ledger.path, 'constructor')
            self.assertEqual(json.loads(ledger.path.read_text()), persisted)

    def test_missing_usage_stays_reserved_and_backend_overrun_is_terminal(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            entry, cap = ledger.reserve_api({'model': 'fixture'})
            ledger.settle_api(entry, {})
            self.assertEqual(ledger.data['output_tokens_charged'], cap)
            second, cap = ledger.reserve_api({'model': 'fixture'})
            ledger.settle_api(second, {'input_tokens': 7, 'output_tokens': cap + 1})
            self.assertEqual(ledger.exhausted(), 'provider_output_exceeded_request_cap')
            with self.assertRaises(BudgetExceeded):
                ledger.reserve_api({'model': 'fixture'})
            restored = Ledger(ledger.path, 'constructor', resume=True)
            self.assertEqual(restored.exhausted(), 'provider_output_exceeded_request_cap')

    def test_reopen_keeps_reserved_usage_and_activity_without_charging_downtime(self):
        clock = 'revisions.seekgnomes_v1_2.budget.time.monotonic'
        config = {'output_tokens': 20000, 'active_seconds': 60, 'cpus': 2}
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            path = Path(folder) / 'ledger.json'
            ledger = Ledger(path, 'constructor', **config)
            with patch(clock, return_value=10):
                ledger.resume()
                settled, _ = ledger.reserve_api({'input': 'settled request'})
                ledger.settle_api(settled, {'input_tokens': 25, 'output_tokens': 8})
            with patch(clock, return_value=13):
                ledger.reserve_numerics(2, 7)
                pending, reserved = ledger.reserve_api({'input': 'interrupted request'})
                pending_input = pending['input_reservation']
            with patch(clock, return_value=20):
                ledger.charge_native_cpu(4.5)
                snapshot = ledger.checkpoint()
                self.assertEqual(snapshot['active_wall_seconds'], 10)
                self.assertIsNotNone(ledger.active_since)
            with patch(clock, return_value=500):
                restored = Ledger(path, 'constructor', resume=True, **config)
                self.assertIsNone(restored.active_since)
                self.assertEqual(restored.remaining()['active_seconds'], 50)
                self.assertEqual(restored.data['requests'], 2)
                self.assertEqual(restored.data['input_tokens_charged'], 25 + pending_input)
                self.assertEqual(restored.data['output_tokens_charged'], 8 + reserved)
                self.assertEqual(restored.data['server_cpu_seconds_charged'], 14)
                self.assertEqual(restored.data['native_cpu_seconds_charged'], 4.5)
                recovered = restored.data['entries'][-1]
                self.assertFalse(recovered['in_flight'])
                self.assertEqual(recovered['settlement_reason'], 'controller_interrupted')
                first_reopen = path.read_bytes()
                restored = Ledger(path, 'constructor', resume=True, **config)
                self.assertEqual(path.read_bytes(), first_reopen)
                restored.resume()
            with patch(clock, return_value=506):
                entry, _ = restored.reserve_api({'input': 'new request'})
                self.assertEqual(entry['index'], 2)
                restored.settle_api(entry, {'input_tokens': 5, 'output_tokens': 6})
                restored.pause()
                self.assertEqual(restored.data['active_wall_seconds'], 16)
                self.assertEqual(restored.data['output_tokens_charged'], 14 + reserved)

    def test_reopen_preserves_closing_deadline_and_single_allowance(self):
        config = {'output_tokens': 40, 'input_tokens': 10000, 'active_seconds': 30}
        clock = 'revisions.seekgnomes_v1_2.budget.time.monotonic'
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            path = Path(folder) / 'ledger.json'
            ledger = Ledger(path, 'constructor', **config)
            with patch(clock, return_value=10):
                ledger.resume()
                first, _ = ledger.reserve_api({'input': 'main phase'})
            with patch(clock, return_value=17):
                self.assertTrue(ledger.begin_closing('native finish'))
                ledger.settle_api(first, {'input_tokens': 20, 'output_tokens': 3})
                second, _ = ledger.reserve_api({'input': 'closing phase'})
                ledger.settle_api(second, {'input_tokens': 30, 'output_tokens': 4})
            with patch(clock, return_value=25):
                ledger.checkpoint()
            restored = Ledger(path, 'constructor', resume=True, **config)
            self.assertEqual(restored.data['phase'], 'closing')
            self.assertEqual(restored.remaining()['output'], 49996)
            self.assertEqual(restored.remaining()['input'], 9970)
            self.assertEqual(restored.remaining()['active_seconds'], 22)
            self.assertFalse(restored.begin_closing('restarted'))

    def test_reopen_rejects_changed_configuration_and_invalid_ledgers(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            path = Path(folder) / 'ledger.json'
            ledger = Ledger(path, 'constructor', output_tokens=100)
            entry, _ = ledger.reserve_api({'input': 'first request'})
            ledger.settle_api(entry, {'input_tokens': 7, 'output_tokens': 15})
            initial = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'configured limits'):
                Ledger(path, 'constructor', output_tokens=101, resume=True)
            with self.assertRaisesRegex(ValueError, 'schema or role'):
                Ledger(path, 'formalizer', resume=True)
            for changed in ({'requests': 0}, {'output_tokens_charged': 14},
                            {'active_wall_seconds': -1}, {'phase': 'closing'}):
                with self.subTest(changed=changed):
                    path.write_text(json.dumps({**json.loads(initial), **changed}))
                    before = path.read_bytes()
                    with self.assertRaises(ValueError):
                        Ledger(path, 'constructor', output_tokens=100, resume=True)
                    self.assertEqual(path.read_bytes(), before)

    def test_numerical_role_and_resource_limits(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            formal = Ledger(Path(folder) / 'formal.json', 'formalizer')
            with self.assertRaises(BudgetExceeded):
                formal.reserve_numerics(1, 10)
            constructor = Ledger(Path(folder) / 'constructor.json', 'constructor', cpus=2)
            for cpus, seconds in ((3, 1), (1, 301), (1, 0)):
                with self.assertRaises(ValueError):
                    constructor.reserve_numerics(cpus, seconds)
            constructor.reserve_numerics(2, 23)
            self.assertEqual(constructor.data['server_cpu_seconds_charged'], 46)
            constructor.charge_native_cpu(4)
            constructor.charge_native_cpu(2)
            self.assertIsNone(constructor.remaining()['numerical_cpu'])
            self.assertEqual(constructor.data['native_cpu_seconds_charged'], 4)

    def test_single_closing_allowance_preserves_all_prior_usage(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor', output_tokens=10,
                            input_tokens=None, active_seconds=30)
            entry, cap = ledger.reserve_api({'input': 'explore'})
            self.assertEqual(cap, 10)
            ledger.settle_api(entry, {'input_tokens': 100, 'output_tokens': 10})
            ledger.data['active_wall_seconds'] = 7
            self.assertEqual(ledger.exhausted(), 'output_budget_exhausted')
            before = ledger.snapshot()
            self.assertTrue(ledger.begin_closing('output_budget_exhausted'))
            self.assertEqual(ledger.data['phase'], 'closing')
            self.assertEqual(ledger.data['requests'], before['requests'])
            self.assertEqual(ledger.data['input_tokens_charged'], 100)
            self.assertEqual(ledger.data['output_tokens_charged'], 10)
            self.assertEqual(ledger.data['active_wall_seconds'], 7)
            self.assertEqual(ledger.remaining()['output'], 50000)
            self.assertEqual(ledger.remaining()['active_seconds'], 30)
            self.assertIsNone(ledger.remaining()['requests'])
            self.assertIsNone(ledger.remaining()['input'])
            self.assertFalse(ledger.begin_closing('another attempted allowance'))
            second, _ = ledger.reserve_api({'input': 'finish one complete state'})
            ledger.settle_api(second, {'input_tokens': 50, 'output_tokens': 5})
            self.assertEqual(ledger.data['requests'], 2)
            self.assertEqual(ledger.data['input_tokens_charged'], 150)
            self.assertEqual(ledger.data['output_tokens_charged'], 15)
            self.assertEqual(ledger.remaining()['output'], 49995)


class GatewayTests(unittest.TestCase):
    model = 'route/fixture-model'

    def test_sse_is_metered_before_replay_and_credentials_stay_host_side(self):
        raw = (b'event: response.completed\ndata: ' + json.dumps({'type': 'response.completed',
            'response': {'model': self.model, 'usage': {'input_tokens': 13, 'output_tokens': 7},
                         'output': [{'text': 'native response'}]}}).encode() + b'\n\ndata: [DONE]\n\n')
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture([
                (200, raw, 'text/event-stream')]) as (endpoint, seen):
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            gateway = RoleGateway(Path(folder) / 'gateway', role='constructor', model=self.model,
                endpoint=endpoint, api_key='host-secret-fixture-key', protocol='responses',
                ledger=ledger, tool_dispatch=lambda value: {'ok': True}).start()
            try:
                connection = UnixClient(gateway)
                connection.request('POST', '/v1/responses', json.dumps({'model': self.model, 'input': 'summarize'}),
                    {'Content-Type': 'application/json', 'Authorization': 'Bearer fake-role-capability'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertFalse(ledger.data['entries'][-1]['in_flight'])
                self.assertEqual(response.read(), raw)
                connection.close()
                self.assertEqual(ledger.data['output_tokens_charged'], 7)
                self.assertEqual(seen[0]['headers'].get('Authorization'), 'Bearer host-secret-fixture-key')
                self.assertEqual(seen[0]['payload']['model'], self.model)
                self.assertLessEqual(seen[0]['payload']['max_output_tokens'], 16000)
                for path in (Path(folder) / 'gateway/api').glob('*.json'):
                    self.assertNotIn('host-secret-fixture-key', path.read_text())
                    self.assertNotIn('fake-role-capability', path.read_text())
            finally:
                gateway.close()

    def test_wrong_model_and_overrun_never_reach_the_cli(self):
        plans = [(200, {'model': 'different-model', 'output': 'FORBIDDEN_MODEL_OUTPUT',
                       'usage': {'input_tokens': 4, 'output_tokens': 2}}, 'application/json'),
                 (200, {'model': self.model, 'output': 'FORBIDDEN_OVERRUN_OUTPUT',
                       'usage': {'input_tokens': 4, 'output_tokens': 16001}}, 'application/json')]
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture(plans) as (endpoint, seen):
            for index, marker in enumerate((b'FORBIDDEN_MODEL_OUTPUT', b'FORBIDDEN_OVERRUN_OUTPUT')):
                ledger = Ledger(Path(folder) / f'ledger-{index}.json', 'constructor')
                terminals = []
                gateway = RoleGateway(Path(folder) / f'gateway-{index}', role='constructor', model=self.model,
                    endpoint=endpoint, api_key='host-secret-fixture-key', protocol='responses', ledger=ledger,
                    tool_dispatch=lambda value: {}, terminal=lambda *args: terminals.append(args)).start()
                try:
                    status, body, _ = exchange(gateway, '/v1/responses', {'model': self.model, 'input': 'x'})
                    self.assertGreaterEqual(status, 400)
                    self.assertNotIn(marker, body)
                    self.assertFalse(ledger.data['entries'][0]['in_flight'])
                    self.assertTrue(terminals)
                finally:
                    gateway.close()

    def test_paused_role_and_nonselected_api_are_rejected_without_upstream(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            active = [False]
            gateway = RoleGateway(Path(folder) / 'gateway', role='constructor', model=self.model,
                endpoint='http://127.0.0.1:1/v1', api_key='host-secret', protocol='responses',
                ledger=ledger, tool_dispatch=lambda value: {}, active=lambda: active[0]).start()
            try:
                self.assertEqual(exchange(gateway, '/v1/responses', {'input': 'x'})[0], 409)
                active[0] = True
                self.assertEqual(exchange(gateway, '/v1/chat/completions', {'messages': []})[0], 404)
                self.assertEqual(exchange(gateway, '/v1/responses', {'model': 'other'})[0], 400)
                self.assertEqual(ledger.data['requests'], 0)
            finally:
                gateway.close()

    def test_close_waits_for_durable_final_api_record(self):
        from revisions.seekgnomes_v1_2 import gateway as gateway_module
        reply = {'model': self.model, 'usage': {'input_tokens': 3, 'output_tokens': 2}}
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture([
                (200, reply, 'application/json')]) as (endpoint, seen):
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            gateway = RoleGateway(Path(folder) / 'gateway', role='constructor', model=self.model,
                endpoint=endpoint, api_key='host-secret', protocol='responses', ledger=ledger,
                tool_dispatch=lambda value: {}).start()
            entered, release, written, closed = (threading.Event() for _ in range(4))
            original = gateway_module.atomic_json
            def delayed_audit(path, value):
                if Path(path).name == '00000.json':
                    entered.set()
                    release.wait(timeout=5)
                    original(path, value)
                    written.set()
                else:
                    original(path, value)
            closer = threading.Thread(target=lambda: (gateway.close(), closed.set()), daemon=True)
            answers = []
            client = threading.Thread(target=lambda: answers.append(exchange(gateway, '/v1/responses',
                {'model': self.model, 'input': 'x'})), daemon=True)
            with patch.object(gateway_module, 'atomic_json', side_effect=delayed_audit):
                try:
                    client.start()
                    self.assertTrue(entered.wait(timeout=3))
                    closer.start()
                    self.assertFalse(closed.wait(timeout=.75), 'Gateway returned while final audit was still in flight')
                finally:
                    release.set()
                    self.assertTrue(written.wait(timeout=3))
                    client.join(timeout=5)
                    if closer.ident is not None:
                        closer.join(timeout=5)
                    else:
                        gateway.close()
            self.assertTrue(closed.is_set())
            self.assertEqual(answers[0][0], 200)
            self.assertFalse(gateway.api_lock.locked())
            self.assertTrue((Path(folder) / 'gateway/api/00000.json').is_file())

    def test_streaming_heartbeats_cannot_extend_total_request_deadline(self):
        def heartbeat(handler):
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.end_headers()
            for _ in range(1000):
                try:
                    handler.wfile.write(b': fixture heartbeat\n\n')
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(.05)
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture([
                heartbeat]) as (endpoint, seen):
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            ledger.limits = replace(ledger.limits, request_seconds=2)
            gateway = RoleGateway(Path(folder) / 'gateway', role='constructor', model=self.model,
                endpoint=endpoint, api_key='host-secret', protocol='responses', ledger=ledger,
                tool_dispatch=lambda value: {}).start()
            try:
                started = time.monotonic()
                status, body, _ = exchange(gateway, '/v1/responses', {'model': self.model, 'input': 'x'})
                self.assertEqual(status, 502)
                # The I/O deadline is 2s; process termination also has a
                # bounded join. Permit scheduling/cleanup without allowing
                # the 50s heartbeat stream to define the request duration.
                self.assertLess(time.monotonic() - started, 8)
                self.assertEqual(gateway.last_failure['type'], 'TimeoutError')
                self.assertNotIn(b'fixture heartbeat', body)
                self.assertFalse(ledger.data['entries'][0]['in_flight'])
                self.assertEqual(ledger.data['output_tokens_charged'], 16000)
            finally:
                gateway.close()
            self.assertIsNone(gateway.upstream_process)

    def test_native_gemini_model_version_and_thinking_usage_are_preserved(self):
        document = {'modelVersion': self.model, 'usageMetadata': {
            'promptTokenCount': 20, 'cachedContentTokenCount': 4,
            'candidatesTokenCount': 7, 'thoughtsTokenCount': 5, 'totalTokenCount': 32},
            'candidates': [{'content': {'parts': [{'text': 'gemini native fixture'}]}}]}
        plans = [(200, document, 'application/json'),
                 (200, b'data: ' + json.dumps(document).encode() + b'\n\n', 'text/event-stream')]
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture(plans) as (endpoint, seen):
            for index, operation in enumerate(('generateContent', 'streamGenerateContent')):
                ledger = Ledger(Path(folder) / f'ledger-{index}.json', 'constructor')
                gateway = RoleGateway(Path(folder) / f'gateway-{index}', role='constructor', model=self.model,
                    endpoint=endpoint, api_key='host-secret-fixture-key', protocol='gemini', ledger=ledger,
                    tool_dispatch=lambda value: {}).start()
                try:
                    path = '/v1beta/models/' + quote(self.model, safe='') + ':' + operation + '?alt=sse&key=fake-role-capability'
                    status, body, _ = exchange(gateway, path, {'contents': [],
                        'generationConfig': {'maxOutputTokens': 20000}})
                    self.assertEqual(status, 200, body)
                    self.assertEqual(ledger.data['input_tokens_charged'], 20)
                    self.assertEqual(ledger.data['output_tokens_charged'], 12)
                    self.assertEqual(seen[index]['headers']['x-goog-api-key'], 'host-secret-fixture-key')
                    self.assertNotIn('fake-role-capability', seen[index]['path'])
                    self.assertEqual(seen[index]['payload']['generationConfig']['maxOutputTokens'], 16000)
                finally:
                    gateway.close()

    def test_native_gemini_cannot_select_another_model_by_url(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder:
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            gateway = RoleGateway(Path(folder) / 'gateway', role='constructor', model=self.model,
                endpoint='http://127.0.0.1:1/v1', api_key='host-secret', protocol='gemini',
                ledger=ledger, tool_dispatch=lambda value: {}).start()
            try:
                status, body, _ = exchange(gateway, '/v1beta/models/different-model:generateContent', {'contents': []})
                self.assertEqual(status, 400)
                self.assertEqual(ledger.data['requests'], 0)
            finally:
                gateway.close()

    def test_chat_completion_field_policy_reaches_the_actual_provider_request(self):
        reply = {'model': self.model, 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}, 'choices': []}
        plans = [(200, reply, 'application/json') for _ in range(3)]
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture(plans) as (endpoint, seen):
            for index, field in enumerate(('max_tokens', 'max_completion_tokens', 'both')):
                ledger = Ledger(Path(folder) / f'ledger-{index}.json', 'constructor')
                gateway = RoleGateway(Path(folder) / f'gateway-{index}', role='constructor', model=self.model,
                    endpoint=endpoint, api_key='host-secret', protocol='chat_completions', ledger=ledger,
                    tool_dispatch=lambda value: {}, completion_token_field=field).start()
                try:
                    status, body, _ = exchange(gateway, '/v1/chat/completions', {
                        'model': self.model, 'messages': [], 'max_tokens': 8000})
                    self.assertEqual(status, 200, body)
                    sent = seen[index]['payload']
                    if field in ('max_tokens', 'both'):
                        self.assertEqual(sent['max_tokens'], 8000)
                    else:
                        self.assertNotIn('max_tokens', sent)
                    if field in ('max_completion_tokens', 'both'):
                        self.assertEqual(sent['max_completion_tokens'], 8000)
                    self.assertEqual(ledger.data['output_tokens_charged'], 3)
                finally:
                    gateway.close()

    def test_constructor_active_time_tail_is_scientific_budget_exhaustion(self):
        def heartbeat(handler):
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.end_headers()
            for _ in range(1000):
                try:
                    handler.wfile.write(b': active-time fixture heartbeat\n\n')
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(.05)
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp') as folder, provider_fixture([
                heartbeat]) as (endpoint, seen):
            ledger = Ledger(Path(folder) / 'ledger.json', 'constructor')
            ledger.limits = replace(ledger.limits, active_seconds=2)
            ledger.resume()
            terminals = []
            gateway = RoleGateway(Path(folder) / 'gateway', role='constructor', model=self.model,
                endpoint=endpoint, api_key='host-secret', protocol='responses', ledger=ledger,
                tool_dispatch=lambda value: {}, terminal=lambda *value: terminals.append(value)).start()
            try:
                started = time.monotonic()
                status, body, _ = exchange(gateway, '/v1/responses', {'model': self.model, 'input': 'x'})
                self.assertEqual(status, 429, body)
                self.assertLess(time.monotonic() - started, 8)
                self.assertEqual(json.loads(body)['error']['type'], 'budget_exhausted')
                self.assertEqual(terminals[0][0], 'budget_exhausted')
                self.assertNotIn(b'active-time fixture heartbeat', body)
                self.assertFalse(ledger.data['entries'][0]['in_flight'])
            finally:
                ledger.pause()
                gateway.close()


if __name__ == '__main__':
    unittest.main()
