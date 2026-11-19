"""Regressions for findings from the independent completion review."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2.budget import Ledger
from revisions.seekgnomes_v1_2.gateway import RoleGateway
from revisions.seekgnomes_v1_2.native import NativeInvocation
from revisions.seekgnomes_v1_2.native import probe_harness
from revisions.seekgnomes_v1_2.harnesses import resolve_harness
from revisions.seekgnomes_v1_2.runtime import TrialRuntime
from revisions.seekgnomes_v1_2.sandbox import RoleSandbox
from revisions.seekgnomes_v1_2.tests.test_budget_gateway import provider_fixture, exchange

PROJECT=Path(__file__).resolve().parents[3]


class ReviewFixTests(unittest.TestCase):
    def test_version_probe_has_private_writable_home_without_host_credentials(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder:
            binary=Path(folder)/'codex'
            binary.write_text('#!/usr/bin/python3\nimport os,pathlib\n'
                'home=pathlib.Path(os.environ["HOME"])\n'
                'assert home.is_dir() and "native-version-probe-" in home.name\n'
                'assert not os.environ.get("SEEKGNOMES_API_KEY")\n'
                'pathlib.Path(os.environ["XDG_CACHE_HOME"],"bootstrap").write_text("ok")\n'
                'print("codex-cli 0.167.1")\n')
            binary.chmod(0o755)
            with patch.dict(os.environ,{'SEEKGNOMES_API_KEY':'host-credential-fixture'}):
                result=probe_harness(resolve_harness('gpt-6.1-terra'),search_path=folder)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['observed_version'],'0.167.1')

    def test_cpu_allocation_is_inherited_by_tool_threads_and_restored_after_trial(self):
        original=os.sched_getaffinity(0)
        with tempfile.TemporaryDirectory(dir=PROJECT/'runs') as folder:
            trial=TrialRuntime(task_id=25,out=Path(folder)/'trial',model='gpt-6.1-terra',
                endpoint='https://uncontacted.invalid/v1',api_key='offline-host-key',budget_multiplier=1,
                web=False,cpus=1)
            observed=[]
            def fake_role_run():
                observed.append(os.sched_getaffinity(0))
                child=threading.Thread(target=lambda:observed.append(os.sched_getaffinity(0)))
                child.start();child.join()
                return {'session_status':'completed'}
            role=SimpleNamespace(run=fake_role_run,ledger=SimpleNamespace(data={'phase':'exploration'}))
            with patch.object(trial,'save'),patch.object(trial,'make_role',return_value=role):
                trial.run()
        self.assertEqual(os.sched_getaffinity(0),original)
        self.assertEqual([len(mask) for mask in observed],[1,1])
        self.assertEqual(observed[0],observed[1])

    def test_explicit_http_200_provider_error_events_remain_technical(self):
        for protocol,failed,path in (
            ('responses',b'data: {"type":"response.created","response":{"model":"fixture"}}\n\n'
                b'data: {"type":"response.failed","response":{"model":"fixture","status":"failed","error":{"type":"server_error"}}}\n\n','/v1/responses'),
            ('messages',b'data: {"type":"message_start","message":{"model":"fixture","usage":{"input_tokens":2,"output_tokens":0}}}\n\n'
                b'data: {"type":"error","error":{"type":"overloaded_error"}}\n\n','/v1/messages')):
            with self.subTest(protocol=protocol),tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder,provider_fixture([
                    (200,failed,'text/event-stream'),
                    (200,{'model':'fixture','usage':{'input_tokens':2,'output_tokens':1}},'application/json')]) as (url,seen):
                ledger=Ledger(Path(folder)/'ledger.json','constructor')
                gateway=RoleGateway(Path(folder)/'gateway',role='constructor',model='fixture',endpoint=url,
                    api_key='offline-key',protocol=protocol,ledger=ledger,tool_dispatch=lambda x:{}).start()
                try:
                    self.assertEqual(exchange(gateway,path,{'model':'fixture'})[0],502)
                    self.assertEqual(gateway.last_failure['type'],'NativeProviderError')
                    self.assertEqual(exchange(gateway,path,{'model':'fixture'})[0],200)
                    self.assertIsNone(gateway.last_failure)
                finally:gateway.close()

    def test_failed_requests_exhausting_small_budget_do_not_become_scientific_failure(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'runs') as folder:
            trial=TrialRuntime(task_id=25,out=Path(folder)/'trial',model='gpt-6.1-terra',
                endpoint='https://uncontacted.invalid/v1',api_key='offline-key',budget_multiplier=1,
                web=False,constructor_output_tokens=10,cpus=1)
            role=trial.make_role('constructor',trial.tools.constructor)
            try:
                entry,_=role.ledger.reserve_api({'model':'gpt-6.1-terra'})
                role.ledger.settle_api(entry,{})
                role.gateway.last_failure={'type':'ProviderHTTPError','http_status':503}
                role.terminal('budget_exhausted','output_budget_exhausted')
                self.assertTrue(role.event.is_set())
                self.assertEqual(role.receipt['session_status'],'infrastructure_failure')
                self.assertEqual(role.ledger.data['phase'],'exploration')
                self.assertFalse(role.enter_closing('native_finished'))
            finally:role.gateway.close();role.sandbox.stop()

    def test_main_reservation_settled_after_closing_cannot_enlarge_final_allowance(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder:
            ledger=Ledger(Path(folder)/'ledger.json','constructor',output_tokens=100)
            main,_=ledger.reserve_api({'model':'fixture','input':'main'})
            self.assertTrue(ledger.begin_closing('native_finish'))
            ledger.settle_api(main,{'input_tokens':20,'output_tokens':7})
            self.assertEqual(ledger.data['closing_start_output_tokens'],7)
            self.assertEqual(ledger.remaining()['output'],50000)
            closing,_=ledger.reserve_api({'model':'fixture','input':'closing'})
            ledger.settle_api(closing,{'input_tokens':30,'output_tokens':11})
            self.assertEqual(ledger.remaining()['output'],49989)
            self.assertEqual(ledger.data['output_tokens_charged'],18)
            self.assertFalse(ledger.begin_closing('second_finish'))

    def test_unrecovered_provider_error_is_recorded_and_valid_success_clears_it(self):
        plans=[(503,{'error':'temporarily unavailable'},'application/json'),
               (429,{'error':'rate limited'},'application/json'),
               (200,{'model':'fixture','usage':{'input_tokens':3,'output_tokens':2}},'application/json')]
        with tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder,provider_fixture(plans) as (url,seen):
            ledger=Ledger(Path(folder)/'ledger.json','constructor')
            gateway=RoleGateway(Path(folder)/'gateway',role='constructor',model='fixture',endpoint=url,
                api_key='offline-key',protocol='responses',ledger=ledger,tool_dispatch=lambda x:{}).start()
            try:
                for expected in (503,429):
                    status,_,_=exchange(gateway,'/v1/responses',{'model':'fixture','input':'test'})
                    self.assertEqual(status,expected)
                    self.assertEqual(gateway.last_failure['http_status'],expected)
                self.assertEqual(exchange(gateway,'/v1/responses',{'model':'fixture','input':'test'})[0],200)
                self.assertIsNone(gateway.last_failure)
            finally:gateway.close()

    def test_invalid_provider_usage_retains_audit_and_reservation(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder,provider_fixture([
                (200,{'model':'fixture','usage':{'input_tokens':-1,'output_tokens':'invalid'}},'application/json')]) as (url,seen):
            ledger=Ledger(Path(folder)/'ledger.json','constructor');terminals=[]
            gateway=RoleGateway(Path(folder)/'gateway',role='constructor',model='fixture',endpoint=url,
                api_key='offline-key',protocol='responses',ledger=ledger,tool_dispatch=lambda x:{},
                terminal=lambda *args:terminals.append(args)).start()
            try:
                self.assertEqual(exchange(gateway,'/v1/responses',{'model':'fixture','input':'test'})[0],502)
            finally:gateway.close()
            record=json.loads((Path(folder)/'gateway/api/00000.json').read_text())
            self.assertEqual(record['error'],'invalid_native_provider_response')
            self.assertEqual(terminals[0][0],'infrastructure_failure')
            self.assertEqual(ledger.data['output_tokens_charged'],16000)
            self.assertFalse(ledger.data['entries'][0]['in_flight'])

    def test_cli_exit_zero_after_unrecovered_api_error_is_a_technical_gap(self):
        (PROJECT/'runs').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT/'runs') as folder:
            trial=TrialRuntime(task_id=25,out=Path(folder)/'trial',model='gpt-6.1-terra',
                endpoint='https://uncontacted.invalid/v1',api_key='offline-host-key',budget_multiplier=1,
                web=False,cpus=1)
            role=trial.make_role('constructor',trial.tools.constructor)
            role.gateway.last_failure={'type':'ProviderHTTPError','http_status':503}
            process=SimpleNamespace(poll=lambda:0,returncode=0)
            with patch.object(role.sandbox,'start',return_value=process):
                result=role.run()
            self.assertEqual(result['session_status'],'infrastructure_failure')
            self.assertEqual(result['reason'],'unrecovered_native_api_failure')
            self.assertEqual(role.ledger.data['phase'],'exploration')

    def test_namespace_tree_pause_also_stops_detached_native_background_commands(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder:
            root=Path(folder);workspace=root/'workspace';workspace.mkdir()
            harness=root/'harness';harness.mkdir()
            child='import os,time\nf=open("/work/ticks","ab",buffering=0)\nwhile True:\n f.write(b"x");time.sleep(.02)'
            (workspace/'background.py').write_text('import subprocess,time,threading\n'
                'def from_non_main_thread():\n subprocess.Popen(["/usr/bin/python3","-I","-c",'+repr(child)+'],start_new_session=True)\n time.sleep(120)\n'
                'threading.Thread(target=from_non_main_thread,daemon=True).start()\n'
                'while True:time.sleep(1)\n')
            ledger=Ledger(root/'ledger.json','constructor')
            gateway=RoleGateway(root/'gateway',role='constructor',model='fixture',
                endpoint='https://uncontacted.invalid/v1',api_key='offline-key',protocol='responses',
                ledger=ledger,tool_dispatch=lambda x:{}).start()
            invocation=NativeInvocation(harness='fixture',version='1',
                argv=('/usr/bin/python3','-I','-B','/work/background.py'),env={},cwd='/work',
                stdin_path=None,config_files={},credential_aliases={},session_files=())
            sandbox=RoleSandbox(root/'role',workspace,harness_root=harness)
            try:
                process=sandbox.start(invocation,gateway.socket_path)
                ticks=workspace/'ticks';deadline=time.monotonic()+15
                while (not ticks.exists() or ticks.stat().st_size<3) and time.monotonic()<deadline:time.sleep(.03)
                self.assertTrue(ticks.exists())
                sandbox.pause();before=ticks.stat().st_size
                self.assertGreaterEqual(len(sandbox.suspended),3)
                time.sleep(.2)
                self.assertEqual(ticks.stat().st_size,before)
                sandbox.resume();deadline=time.monotonic()+5
                while ticks.stat().st_size==before and time.monotonic()<deadline:time.sleep(.03)
                self.assertGreater(ticks.stat().st_size,before)
                sandbox.pause();stopped=dict(sandbox.suspended);sandbox.stop()
                self.assertIsNotNone(process.poll())
                for pid,birth in stopped.items():
                    deadline=time.monotonic()+2
                    while sandbox._process_info(pid)[0]==birth and time.monotonic()<deadline:time.sleep(.02)
                    self.assertNotEqual(sandbox._process_info(pid)[0],birth)
            finally:sandbox.stop();gateway.close()


if __name__=='__main__':unittest.main()
