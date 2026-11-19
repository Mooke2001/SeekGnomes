"""Host-owned offline release checks, never a model leaderboard result."""
from pathlib import Path
import json
import tempfile
import time

from .budget import Ledger
from .gateway import RoleGateway
from .native import NativeInvocation
from .sandbox import RoleSandbox
from .harbor_adapter import run_fixture_smoke
from .state_tools import StateDefinitionTools, is_verified
from revisions.seekgnomes_v1_1_1.smoke import run_self_test as strict_self_test
from revisions.seekgnomes_v1_1_1.campaign import atomic_json
from revisions.seekgnomes_v1_1_1.scoring import verify_stored_result
from src.benchmark50_models import load_task

PROJECT=Path(__file__).resolve().parents[2]


def run_state_proof_smoke(out):
    out = Path(out)
    out.mkdir(mode=0o700)
    examples = PROJECT / 'examples/state_format'
    task = load_task(11)
    tools = StateDefinitionTools(task, out, web=False)

    def call(role, name, **arguments):
        return role.dispatch({'tool': name, 'arguments': arguments})

    source = call(tools.constructor, 'write', path='f.state', content=(examples / 'tracial.state').read_text())
    handoff = call(tools.constructor, 'create_handoff', definition_path='f.state') if source.get('ok') else source
    if not handoff.get('ok'):
        return {'ok': False, 'stage': 'handoff', 'detail': handoff}
    formalist = tools.formalizer
    prepared = call(formalist, 'prepare')
    if not prepared.get('ok'):
        return {'ok': False, 'stage': 'prepare', 'detail': prepared}
    proof = call(formalist, 'write', path='Candidate.lean', content=(examples / 'TracialProof.lean').read_text())
    scored = call(formalist, 'score', proof_path='Candidate.lean') if proof.get('ok') else proof
    if not is_verified(scored):
        return {'ok': False, 'stage': 'score', 'detail': scored}
    stored = verify_stored_result(scored, task, out)
    return {'ok': stored['energy_density'] == -0.375 and prepared.get('energy_density') is None,
            'task_id': task['id'], 'energy_density': stored['energy_density'],
            'source_sha256': scored['source_sha256'], 'proof_manifest': scored['proof_manifest']}


def run_role_namespace_smoke(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    workspace=out/'workspace';workspace.mkdir()
    harness=out/'fixture-harness';(harness/'bin').mkdir(parents=True)
    sentinel=out/'host-only-sentinel';sentinel.write_text('not visible to the role')
    script='''import json,os,socket,subprocess,http.client
from pathlib import Path
assert not os.environ.get('SEEKGNOMES_API_KEY')
assert not os.environ.get('EUREKA_API_KEY')
assert not Path('/home/ubuntu/Mooke/Eureka/Environment').exists()
assert not Path('/var/run/docker.sock').exists()
connection=http.client.HTTPConnection('127.0.0.1',8731,timeout=10)
connection.request('GET','/v1/models')
response=connection.getresponse()
assert response.status==200 and json.loads(response.read())['data'][0]['id']=='fixture-model'
connection.close()
try: socket.create_connection(('1.1.1.1',443),timeout=.3)
except OSError: pass
else: raise AssertionError('External network unexpectedly reachable')
messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}},
 {'jsonrpc':'2.0','id':2,'method':'tools/list'},
 {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'benchmark_action','arguments':{'tool':'finish','arguments':{}}}}]
p=subprocess.run(['/usr/bin/python3','-I','-B','/opt/seekgnomes/role_bridge.py','mcp'],
 input=''.join(json.dumps(x)+'\\n' for x in messages),text=True,capture_output=True,timeout=15)
assert p.returncode==0,p.stderr
rows=[json.loads(line) for line in p.stdout.splitlines()]
assert len(rows)==3 and rows[-1]['result']['isError'] is False,rows
print('ROLE_NAMESPACE_MCP_OK')
'''
    (workspace/'fixture.py').write_text(script)
    calls=[]
    def dispatch(item):
        calls.append(item)
        if item.get('method')=='list_tools':
            return {'tools':[{'name':'benchmark_action','description':'offline fixture',
                'inputSchema':{'type':'object','properties':{'tool':{'type':'string'},'arguments':{'type':'object'}}}}]}
        return {'ok':item.get('name')=='benchmark_action','fixture':True}
    ledger=Ledger(out/'ledger.json','constructor')
    gateway=RoleGateway(out/'gateway',role='constructor',model='fixture-model',
        endpoint='https://uncontacted.invalid/v1',api_key='host-secret-never-in-role',
        protocol='responses',ledger=ledger,tool_dispatch=dispatch).start()
    invocation=NativeInvocation(harness='fixture-only',version='1',
        argv=('/usr/bin/python3','-I','-B','/work/fixture.py'),
        env={'PATH':'/usr/bin:/bin','HOME':'/home/agent'},cwd='/work',stdin_path=None,
        config_files={},credential_aliases={},session_files=())
    sandbox=RoleSandbox(out,workspace,harness_root=harness)
    try:
        process=sandbox.start(invocation,gateway.socket_path)
        code=process.wait(timeout=25)
        stdout=(out/'native.stdout.log').read_text()
        stderr=(out/'native.stderr.log').read_text()
        result={'ok':code==0 and 'ROLE_NAMESPACE_MCP_OK' in stdout,'returncode':code,
            'stdout':stdout,'stderr':stderr,'mcp_host_calls':len(calls),'external_api_calls':0,
            'scientific_trial':False,'private_filesystem_and_loopback_gateway':True,
            'host_network_unreachable':code==0,'upstream_credential_in_role':False}
        atomic_json(out/'result.json',result)
        return result
    finally:sandbox.stop();gateway.close()


def run_self_test():
    (PROJECT/'runs').mkdir(exist_ok=True)
    out=Path(tempfile.mkdtemp(prefix='harbor-release-self-test-',dir=PROJECT/'runs'))
    strict=strict_self_test()
    state=run_state_proof_smoke(out/'state-proof')
    namespace=run_role_namespace_smoke(out/'role-namespace')
    harbor=run_fixture_smoke(out/'harbor')
    checks={'strict_physics_and_lean':strict.get('ok') is True,
            'shared_state_strict_proof_and_energy':state.get('ok') is True,
            'actual_role_namespace_and_mcp':namespace.get('ok') is True,
            'actual_harbor_job_trial_verifier':harbor.get('ok') is True}
    result={'schema':'seekgnomes-harbor-self-test/v1.2','ok':all(checks.values()),
            'checks':checks,'strict':strict,'state':state,'namespace':namespace,'harbor':harbor,
            'external_api_calls':0,'scientific_trial':False,'evidence':str(out)}
    atomic_json(out/'self-test.json',result)
    return result
