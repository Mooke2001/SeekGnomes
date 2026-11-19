"""Real bubblewrap + namespace + UDS tests; no native LLM or paid endpoint."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2.budget import Ledger
from revisions.seekgnomes_v1_2.gateway import RoleGateway
from revisions.seekgnomes_v1_2.native import NativeInvocation
from revisions.seekgnomes_v1_2.sandbox import RoleSandbox
from revisions.seekgnomes_v1_2.tests.test_budget_gateway import PROJECT, provider_fixture


FAKE_CLI = r'''#!/usr/bin/python3
import http.client, json, os, pathlib, socket, subprocess
result={'has_real_key': any('host-real-secret-fixture' in value for value in os.environ.values()),
        'native_shell_probe': subprocess.check_output(['/bin/sh','-c',
            'test -x /bin/bash && /bin/bash -c "printf NATIVE_SHELL_OK"'],text=True),
        'cpu_affinity_count': len(os.sched_getaffinity(0)),
        'child_cpu_affinity_count': int(subprocess.check_output(['/usr/bin/python3','-I','-c',
            'import os;print(len(os.sched_getaffinity(0)))'],text=True)),
        'fake_capability': os.environ.get('OPENAI_API_KEY'),
        'other_role_visible': pathlib.Path(os.environ['OTHER_ROLE_PATH']).exists(),
        'host_project_visible': pathlib.Path(os.environ['HOST_PROJECT_PATH']).exists(),
        'scientific_python_visible': pathlib.Path(os.environ['SCIENTIFIC_PYTHON']).exists()}
try:
    os.sched_setaffinity(0,os.sched_getaffinity(0))
except PermissionError:
    result['affinity_change_blocked']=True
else:
    result['affinity_change_blocked']=False
try:
    client=http.client.HTTPConnection('127.0.0.1',8731,timeout=4)
    client.request('GET','/v1/models')
    response=client.getresponse(); result['models_status']=response.status
    result['models']=json.loads(response.read()); client.close()
    client=http.client.HTTPConnection('127.0.0.1',8731,timeout=4)
    client.request('POST','/v1/responses',json.dumps({'model':'route/fixture-model','input':'ordinary'}),
                   {'Content-Type':'application/json','Authorization':'Bearer '+os.environ['OPENAI_API_KEY']})
    response=client.getresponse(); result['api_status']=response.status
    result['api_response']=json.loads(response.read());client.close()
except Exception as exc:
    result['proxy_error']=type(exc).__name__+': '+str(exc)
for label,address in [('external',('192.0.2.1',443)),('host_loopback',('127.0.0.1',int(os.environ['HOST_PORT'])))]:
    try:
        connection=socket.create_connection(address,timeout=.3);connection.close();result[label+'_reachable']=True
    except OSError:
        result[label+'_reachable']=False
requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
          {'jsonrpc':'2.0','id':2,'method':'tools/list'},
          {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'benchmark_action',
             'arguments':{'tool':'server','arguments':{}}}}]
mcp=subprocess.run(['/usr/bin/python3','-I','-B','/opt/seekgnomes/role_bridge.py','mcp'],
    input=''.join(json.dumps(item)+'\n' for item in requests),text=True,
    capture_output=True,timeout=8)
result['mcp_returncode']=mcp.returncode
result['mcp_stdout']=mcp.stdout
result['mcp_stderr']=mcp.stderr
pathlib.Path('/work/observed.json').write_text(json.dumps(result))
'''


class RealSandboxTests(unittest.TestCase):
    def test_native_config_writes_reject_linked_parents_leaves_and_hardlinks(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp', prefix='safe-native-config-') as folder:
            root = Path(folder)
            for surface in ('home', 'workspace'):
                for kind in ('parent-symlink', 'leaf-symlink', 'leaf-hardlink'):
                    with self.subTest(surface=surface, kind=kind):
                        case = root / (surface + '-' + kind)
                        workspace, harness = case / 'workspace', case / 'harness'
                        workspace.mkdir(parents=True)
                        harness.mkdir()
                        sandbox = RoleSandbox(case / 'role', workspace, harness_root=harness)
                        sentinel_dir = case / 'sentinel'
                        sentinel_dir.mkdir()
                        sentinel = sentinel_dir / 'settings.json'
                        sentinel.write_text('HOST_SENTINEL_UNCHANGED')
                        base = sandbox.home if surface == 'home' else workspace
                        parent = base / '.config'
                        target = parent / 'settings.json'
                        if kind == 'parent-symlink':
                            parent.symlink_to(sentinel_dir, target_is_directory=True)
                        else:
                            parent.mkdir()
                            if kind == 'leaf-symlink':
                                target.symlink_to(sentinel)
                            else:
                                os.link(sentinel, target)
                        virtual = ('/home/agent' if surface == 'home' else '/work') + '/.config/settings.json'
                        invocation = NativeInvocation(harness='fixture', version='offline',
                            argv=('/usr/bin/true',), env={}, cwd='/work', stdin_path=None,
                            config_files={virtual: 'WOULD_OVERWRITE_HOST'}, credential_aliases={}, session_files=())
                        with self.assertRaises((ValueError, OSError)):
                            sandbox.command(invocation, case / 'unused.sock')
                        self.assertEqual(sentinel.read_text(), 'HOST_SENTINEL_UNCHANGED')
                        self.assertFalse((sandbox.config_dir / 'launch.json').exists())

    def test_native_home_initialization_rejects_an_existing_directory_symlink(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp', prefix='safe-native-home-') as folder:
            root = Path(folder)
            run, workspace, sentinel = root / 'run', root / 'workspace', root / 'sentinel'
            for path in (run, workspace, sentinel):
                path.mkdir()
            marker = sentinel / 'marker'
            marker.write_text('HOST_SENTINEL_UNCHANGED')
            (run / 'native-home').symlink_to(sentinel, target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                RoleSandbox(run, workspace, harness_root=root)
            self.assertEqual(marker.read_text(), 'HOST_SENTINEL_UNCHANGED')
            self.assertFalse((sentinel / 'tmp').exists())

    def test_private_roles_long_socket_loopback_proxy_mcp_and_no_external_network(self):
        self.assertNotEqual(os.getuid(), 0, 'The distributed controller must run unprivileged')
        self.assertTrue(Path('/usr/bin/bwrap').is_file(), 'Missing required bubblewrap')
        with tempfile.TemporaryDirectory(dir=PROJECT / 'tmp', prefix='native-sandbox-') as folder:
            root = Path(folder)
            harness = root / 'harness'
            (harness / 'bin').mkdir(parents=True)
            cli = harness / 'bin/fixture-cli'
            cli.write_text(FAKE_CLI)
            cli.chmod(0o755)
            workspace = root / 'constructor-workspace'
            workspace.mkdir()
            sibling = root / 'formalist-secret'
            sibling.mkdir()
            (sibling / 'answer.txt').write_text('OTHER_ROLE_PRIVATE')
            ledger = Ledger(root / 'ledger.json', 'constructor')
            long_directory = root / ('long-private-path-' * 4) / ('nested-' * 4)
            provider_reply = {'model': 'route/fixture-model', 'usage': {'input_tokens': 5, 'output_tokens': 3},
                              'output': []}
            def dispatch(payload):
                if payload.get('method') == 'list_tools':
                    return {'tools': [{'name': 'benchmark_action', 'inputSchema': {'type': 'object'}}]}
                return {'ok': False, 'error': 'role_tool_forbidden'}
            with provider_fixture([(200, provider_reply, 'application/json')]) as (endpoint, seen):
                gateway = RoleGateway(long_directory, role='constructor', model='route/fixture-model',
                    endpoint=endpoint, api_key='host-real-secret-fixture', protocol='responses',
                    ledger=ledger, tool_dispatch=dispatch).start()
                sandbox = RoleSandbox(root / 'role', workspace, harness_root=harness)
                self.assertGreater(len(str(gateway.socket_path).encode()), 108)
                environment = {'HOST_PORT': endpoint.split(':')[2].split('/')[0],
                    'OTHER_ROLE_PATH': str(sibling), 'HOST_PROJECT_PATH': str(PROJECT),
                    'SCIENTIFIC_PYTHON': str(PROJECT.parents[1] / 'Environment/Python/miniconda3/bin/python')}
                invocation = NativeInvocation(harness='fixture', version='offline', argv=('fixture-cli',),
                    env=environment, cwd='/work', stdin_path=None, config_files={},
                    credential_aliases={'OPENAI_API_KEY': 'SEEKGNOMES_ROLE_TOKEN'}, session_files=())
                try:
                    with patch.dict(os.environ, {'SEEKGNOMES_API_KEY': 'host-real-secret-fixture',
                                                 'EUREKA_API_KEY': 'host-real-secret-fixture'}):
                        process = sandbox.start(invocation, gateway.socket_path)
                        returncode = process.wait(timeout=25)
                    sandbox.stop()
                    stderr = (sandbox.run_dir / 'native.stderr.log').read_text()
                    self.assertEqual(returncode, 0, stderr)
                    observed = json.loads((workspace / 'observed.json').read_text())
                    self.assertFalse(observed['has_real_key'])
                    self.assertEqual(observed['native_shell_probe'], 'NATIVE_SHELL_OK')
                    self.assertEqual(observed['cpu_affinity_count'], 1)
                    self.assertEqual(observed['child_cpu_affinity_count'], 1)
                    self.assertTrue(observed['affinity_change_blocked'])
                    self.assertEqual(observed['fake_capability'], 'seekgnomes-private-role')
                    self.assertFalse(observed['other_role_visible'])
                    self.assertFalse(observed['host_project_visible'])
                    self.assertFalse(observed['scientific_python_visible'])
                    self.assertFalse(observed['external_reachable'])
                    self.assertFalse(observed['host_loopback_reachable'])
                    self.assertEqual(observed.get('models_status'), 200, observed)
                    self.assertEqual(observed.get('api_status'), 200, observed)
                    self.assertEqual(observed['mcp_returncode'], 0, observed)
                    replies = [json.loads(line) for line in observed['mcp_stdout'].splitlines()]
                    self.assertEqual([item['id'] for item in replies], [1, 2, 3])
                    self.assertTrue(replies[2]['result']['isError'])
                    self.assertEqual(ledger.data['requests'], 1)
                    self.assertEqual(ledger.data['output_tokens_charged'], 3)
                    self.assertEqual(seen[0]['headers']['Authorization'], 'Bearer host-real-secret-fixture')
                    self.assertNotIn('host-real-secret-fixture', (sandbox.config_dir / 'launch.json').read_text())
                    self.assertNotIn('host-real-secret-fixture', json.dumps(observed))
                finally:
                    sandbox.stop()
                    gateway.close()


if __name__ == '__main__':
    unittest.main()
