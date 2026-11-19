"""Two native CLI roles, host-owned tools, and unchanged strict physics checks.

The Constructor CLI remains alive while its handoff MCP call waits for a fresh
Formalist CLI. Thus the actual harness retains its session and manages its own
context. The supervisor never trims or summarizes the native conversation.
"""
from __future__ import annotations
from pathlib import Path
import errno
import hashlib
import json
import os
import threading
import time

from .budget import Ledger, BudgetExceeded
from .checkpoint import CANCELLED, controller_lock, durable_json, open_checkpoint, read_json
from .checkpoint_evidence import restore_constructor_returns, restore_formalist, return_view
from .gateway import RoleGateway
from .harnesses import resolve_harness
from .native import build_invocation, dsh_session_id_from_events
from .numeric import LocalNumericalBroker, dispatch_numerical_action
from .sandbox import RoleSandbox, probe_role_sandbox
from .task_export import LEGACY_PROTOCOL, STATE_PROTOCOL, candidate_protocol
from revisions.dual_agent_v1_1.tools import _replace
from revisions.seekgnomes_v1_1_1.campaign import atomic_json, utc
from revisions.seekgnomes_v1_1_1.run_trial import references
from revisions.seekgnomes_v1_1_1.scoring import verify_stored_result
from revisions.seekgnomes_v1_1_1.tools import DefinitionTools, is_verified, CONSTRUCTOR_ACTIONS, FORMALIZER_ACTIONS
from src.benchmark50_models import load_task

PROJECT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
_RUNNING=set()
_RUNNING_LOCK=threading.Lock()
_CANCEL_GENERATION=0
LEGACY_CLOSING_INSTRUCTION=(
    'FINAL SUBMISSION PHASE. You have not yet handed off a complete f.md. '
    'Your exploration phase has ended. You now have at most 50000 additional output tokens, '
    'and one final configured activity-time allowance. There will be no further extension. '
    'Stop open-ended exploration. Write one complete self-contained mathematical definition '
    'in f.md for your best currently known legitimate translation-invariant state, using '
    'a simpler construction if needed. Define f(P) for every finite Pauli word and immediately '
    'call benchmark_action with tool=create_handoff and its definition_path. '
    'A file in the private workspace alone is not a submission. After Formalist returns, '
    'check fidelity and correct the definition if needed within this same remaining allowance. '
    'Do not finish without attempting the handoff. If no committed handoff exists at the end, '
    'the trial will be recorded as a scientific failure.'
)
CLOSING_INSTRUCTION=(
    'FINAL SUBMISSION PHASE. You have not yet handed off a complete f.state. '
    'You now have at most 50000 additional output tokens and one final configured activity-time allowance. '
    'Stop exploration and write a complete f.state for your best currently known legitimate '
    'translation-invariant state. Use a simpler construction if needed. Define f(P) for every '
    'finite canonical Pauli word and call benchmark_action with tool=create_handoff and '
    'arguments={"definition_path":"f.state"}. A private file alone does not commit a handoff. '
    'After Formalist returns, correct the state if needed within this same remaining allowance. '
    'A trial without a committed handoff will be recorded as a scientific failure.'
)
FORMALIST_RESUME_INSTRUCTION=(
    'PROOF SUBMISSION REQUIRED. Continue proving the same frozen f.state for this handoff. '
    'Use the existing proof files and Lean diagnostics, then call benchmark_action with '
    'tool=score and the proof path. Report only a concrete defect in the frozen definition.'
)
FORMALIST_RETRY_CHECK_INSTRUCTION=(
    'The Lean verifier encountered a temporary host failure. Retry the same check on the '
    'same Lean proof. Continue working on the frozen f.state until score accepts it or '
    'you report a concrete defect in the frozen definition.'
)
CONSTRUCTOR_CHECKPOINT_INSTRUCTION=(
    'The host controller restarted. Continue the same trial and budget from your saved workspace. '
    'Use recover_handoff to retrieve a committed result if the host asks you to read it before continuing.'
)


def state_references(constructor, formalist):
    language=(HERE/'STATE_USAGE.md').read_text(encoding='utf-8')
    constructor.pop('python_language.md',None)
    constructor.pop('dual_tools.md',None)
    constructor['correlator_contract.md']=(
        'Define the real expectation f(P) for every canonical finite Pauli word on the fixed '
        'lattice, including the empty word and arbitrary coordinates. The host evaluates '
        'sum_j c_j*f(P_j)/Q on the frozen f.state only after proving the fixed Lean obligation.\n'
    )
    for name in ('python_language.md','hybrid_semantics.md','submission_skeleton.lean.txt'):
        formalist.pop(name,None)
    constructor['state_language.md']=formalist['state_language.md']=language
    constructor['tools.md']=(HERE/'prompts/constructor-tools.state.md').read_text()
    formalist['submission_api.md']=(HERE/'prompts/formalist-tools.state.md').read_text()


def cancel_active_trials():
    """Cancel only v1.2 trials owned by this controller process."""
    global _CANCEL_GENERATION
    with _RUNNING_LOCK:
        _CANCEL_GENERATION+=1
        trials=list(_RUNNING)
    for trial in trials:
        trial.cancelled=True
        for role in list(trial.roles):role.cancel()


class NativeRole:
    def __init__(self, owner, role, broker, prompt):
        self.owner,self.role,self.broker,self.prompt=owner,role,broker,prompt
        self.base=getattr(broker,'base',broker)
        self.run_dir=self.base.run_dir
        ledger_path=self.run_dir/'budget-ledger.json'
        self.resuming=getattr(owner,'resuming',False) and ledger_path.exists()
        self.ledger=Ledger(ledger_path,role,owner.multiplier,
            output_tokens=owner.constructor_output_tokens,input_tokens=owner.constructor_input_tokens,
            active_seconds=owner.constructor_active_seconds,cpus=owner.cpus,
            resume=self.resuming)
        self.active=True;self.closed=False;self.busy_tools=0
        self.event=threading.Event();self.receipt=None
        self.sandbox=RoleSandbox(self.run_dir,self.base.workspace,cpus=owner.cpus)
        self.completed_native_cpu=self.ledger.data['native_cpu_seconds_charged']
        self.closing_resume_used=self.resuming and self.ledger.data['phase']=='closing'
        self.gateway=RoleGateway(self.run_dir/'native-gateway',role=role,
            model=owner.model,endpoint=owner.endpoint,api_key=owner.api_key,
            protocol=owner.selection.api_protocol,ledger=self.ledger,
            tool_dispatch=self.dispatch,active=lambda:self.active and not self.closed and not self.event.is_set(),
            terminal=self.terminal,completion_token_field=owner.completion_token_field,
            prepare_request=self.prepare_request,resume=self.resuming)
        self.gateway.start()
        self.trace=self.run_dir/'trace.jsonl'
        self.trace_lock=threading.Lock()
        self.created=self.ledger.data['created_utc']
        self.status='created'

    @property
    def closing_instruction(self):
        return (LEGACY_CLOSING_INSTRUCTION if getattr(self.owner,'candidate_format','state')=='legacy'
            else CLOSING_INSTRUCTION)

    def enter_closing(self,reason):
        if self.role!='constructor' or self.owner.tools.handoffs:return False
        if getattr(getattr(self,'gateway',None),'last_failure',None) is not None:return False
        if not self.ledger.begin_closing(reason):return False
        self.record('closing_phase_started',{'reason':reason,'instruction':self.closing_instruction})
        self.owner.top['closing_phase_started']=True
        self.owner.save()
        return True

    def prepare_request(self,payload):
        if self.role!='constructor':return payload
        reason=self.ledger.exhausted()
        if reason and not reason.startswith('provider_'):self.enter_closing(reason)
        if self.ledger.data['phase']!='closing':return payload
        from .gateway import add_system_instruction
        return add_system_instruction(payload,self.owner.selection.api_protocol,self.closing_instruction)

    def terminal(self,status,reason):
        if self.event.is_set():return
        if (self.role=='formalizer' and self.owner.candidate_format=='state'
                and status=='infrastructure_failure' and reason=='invalid_native_provider_response'):
            # The gateway rejects the malformed response. The CLI can retry or
            # exit, in which case the supervisor reopens its native session.
            return
        if status=='budget_exhausted' and self.gateway.last_failure is not None:
            status='infrastructure_failure';reason='unrecovered_native_api_failure'
        if status=='budget_exhausted' and self.enter_closing(reason):return
        self.receipt={'session_status':status,'reason':reason,'proof_verified':False}
        self.event.set()

    def record(self,kind,data):
        with self.trace_lock:
            with self.trace.open('a') as stream:
                stream.write(json.dumps({'created_utc':utc(),'kind':kind,'data':data},ensure_ascii=False,allow_nan=False)+'\n')

    def save(self):
        snapshot=self.ledger.checkpoint() if self.ledger.active_since is not None else self.ledger.snapshot()
        atomic_json(self.run_dir/'manifest.json',{'schema':'seekgnomes-native-role/v1.2',
            'created_utc':self.created,'role':self.role,'model':self.owner.model,
            'status':self.status,'turns':snapshot['requests'],
            'input_tokens_charged':snapshot['input_tokens_charged'],
            'output_tokens_charged':snapshot['output_tokens_charged'],
            'server_cpu_seconds_charged':snapshot['server_cpu_seconds_charged'],
            'native_cpu_seconds_charged':snapshot['native_cpu_seconds_charged'],
            'active_wall_seconds':snapshot['active_wall_seconds'],
            'remaining_budgets':snapshot['remaining'],'harness':self.owner.selection.to_dict(),
            'native_context_management':True,'host_context_trimming':False,
                'terminal':self.receipt,'budget_phase':snapshot['phase'],
                'private_session_directory':str(self.sandbox.home)})

    def dispatch(self,payload):
        state_formalist=self.role=='formalizer' and getattr(self.owner,'candidate_format','state')=='state'
        if state_formalist:
            from .state_tools import STATE_FORMALIST_ACTIONS
            allowed=list(STATE_FORMALIST_ACTIONS)
        else:
            allowed=list(CONSTRUCTOR_ACTIONS if self.role=='constructor' else FORMALIZER_ACTIONS)
        recovering_constructor=self.role=='constructor' and getattr(self.owner,'resuming',False)
        if recovering_constructor:allowed.append('recover_handoff')
        if self.owner.web:allowed+=['web_search','web_fetch']
        if payload.get('method')=='list_tools':
            if getattr(self.owner,'candidate_format','state')=='legacy':
                description='Use the role tools documented in reference/tools.md. Submit a complete f.md early, then improve after verified feedback.'
            elif self.role=='constructor':
                description='Use reference/tools.md. Commit f.state with create_handoff and read verified energy when Formalist returns.'
            else:
                description='Use reference/submission_api.md. Prepare the frozen .state, prove the generated Lean goal, then submit your proof with score.'
            return {'tools':[{'name':'benchmark_action',
                'description':description,
                'inputSchema':{'type':'object','properties':{'tool':{'type':'string','enum':allowed},
                    'arguments':{'type':'object'}},'required':['tool','arguments'],'additionalProperties':False}}]}
        if payload.get('method')!='call_tool' or payload.get('name')!='benchmark_action':
            return {'ok':False,'error':'unknown_host_tool'}
        action=payload.get('arguments')
        if not isinstance(action,dict) or action.get('tool') not in allowed:
            return {'ok':False,'error':'role_tool_forbidden'}
        self.busy_tools+=1
        try:
            self.record('tool_request',action)
            if action['tool']=='server':
                arguments=action.get('arguments') or {}
                self.ledger.reserve_numerics(arguments.get('cpus',1),arguments.get('wall_seconds',120))
            if action['tool']=='server':
                result=dispatch_numerical_action(self.base,action['arguments'],self.owner.cpus)
            else:
                result=self.owner.recovery_action(action) if recovering_constructor else None
                if result is None:result=self.broker.dispatch(action)
            diagnostic=result.get('technical_diagnostic')
            verifier_failure=(state_formalist
                and action['tool'] in ('prepare','run_lean','score') and isinstance(diagnostic,dict)
                and diagnostic.get('kind')=='strict_verifier_environment_failure')
            retryable_check=verifier_failure and diagnostic.get('retryable') is True
            if retryable_check:
                result={**result,'instruction':FORMALIST_RETRY_CHECK_INSTRUCTION}
            elif state_formalist and action['tool']=='score' and result.get('ok') is False and not verifier_failure:
                result={**result,'instruction':FORMALIST_RESUME_INSTRUCTION}
            self.record('tool_result',result)
            if result.get('action')=='handoff' and self.role=='constructor':
                return self.owner.handoff(self,result)
            if verifier_failure and not retryable_check:
                self.receipt={**result,'session_status':'infrastructure_failure',
                    'reason':'strict_verifier_environment_failure'}
                self.event.set()
            elif result.get('action') in ('feedback','finish'):
                if self.role=='constructor' and result.get('action')=='finish' and self.enter_closing('constructor_requested_finish_without_definition'):
                    return {'ok':False,'action':'continue','instruction':self.closing_instruction,
                        'remaining_budgets':self.ledger.remaining()}
                if self.role=='formalizer' or result.get('action')=='finish':
                    self.receipt={**result,'session_status':'completed'}
                    if self.role=='constructor':self.save()
                    self.event.set()
            self.owner.save()
            return {**result,'remaining_budgets':self.ledger.remaining()}
        except (BudgetExceeded,ValueError) as exc:
            result={'ok':False,'error':type(exc).__name__,'detail':str(exc),
                'allowed_numerical_resources':{'cpus_max':self.owner.cpus,'wall_seconds_max':300},
                'remaining_budgets':self.ledger.remaining()}
            self.record('tool_result',result)
            return result
        finally:self.busy_tools-=1

    def run(self, *, resume_from=None):
        try:
            return self._run_native() if resume_from is None else self._run_native(resume_from=resume_from)
        finally:
            self.active=False;self.closed=True;self.ledger.pause()
            self.sandbox.stop();self.gateway.close();self.save()

    def _wait_for_native_restart(self, delay, **details):
        self.record('native_recovery',{'delay_seconds':delay, **details})
        self.sandbox.stop()
        self.event.wait(delay)
        return min(delay*2, 30)

    def _has_native_session(self, invocation):
        home=self.sandbox.home.resolve()
        if invocation.harness=='codex':
            # The Codex SQLite file also exists before the first conversation.
            sessions=(home/'.codex/sessions').resolve()
            return sessions.is_relative_to(home) and any(sessions.rglob('rollout-*.jsonl'))
        for name in invocation.session_files:
            path=(home/Path(name).relative_to('/home/agent')).resolve()
            if path.is_relative_to(home) and (path.is_file() or any(child.is_file() for child in path.rglob('*'))):
                return True
        return False

    def recovery_session(self, instruction):
        """Select the same native session, if the interrupted CLI saved one."""
        if not self.resuming:
            return None
        selection=self.owner.selection
        if selection.name=='dsh':
            session_id=dsh_session_id_from_events(self.run_dir/'native.stdout.log')
            if session_id:return (session_id,instruction)
        else:
            invocation=build_invocation(selection,'/work','/home/agent','/work/seekgnomes-task.md',
                'http://127.0.0.1:8731/v1',model_context_size=self.owner.model_context_tokens)
            if self._has_native_session(invocation):return (None,instruction)
        self.prompt+='\n\n'+instruction
        return None

    def _run_native(self, *, resume_from=None):
        if self.owner.cancelled:
            self.receipt={'session_status':'cancelled_by_operator','proof_verified':False}
            self.status='cancelled_by_operator'
            return self.receipt
        self.status='running';self.ledger.resume();self.save()
        # The source contract is preserved; native filesystem operations act in
        # the same private workspace as the existing host broker.
        prompt_file=self.base.workspace/'seekgnomes-task.md'
        refs='\n\n'.join('## reference/'+name+'\n'+content for name,content in self.base.references.items())
        guidance='Use the seekgnomes MCP benchmark_action tool for host operations. '
        if self.owner.candidate_format=='legacy':
            guidance+=(
                'Native file edits are private experiments, not verified submissions. '
                'Keep a durable research-notes.md with the current candidate, completed checks and next action. '
                'After finding a complete definable candidate, submit f.md and create_handoff promptly; '
                'further optimization can continue after feedback. Do not wait for an exact ground state. '
            )
        else:
            guidance+='Keep durable private notes with completed checks and the next action. '
            if self.role=='constructor':
                guidance+='Commit a complete f.state promptly. Continue optimizing after feedback. '
        if self.role=='constructor' or self.owner.candidate_format=='legacy':
            guidance+=(
                'All model calls, including native context compaction, share this role budget. '
                f'One numerical worker is allowed, using 1..{self.owner.cpus} CPUs, with each request at most 300 seconds. '
                'The selected Constructor budget is described in reference/runtime_resources.md. '
                'API count is unlimited. If no complete definition has been handed off by the end, '
                'there is exactly one final 50000-output-token submission phase, with the same configured activity allowance.'
            )
        _replace(prompt_file,(self.prompt+'\n\n'+refs+'\n\n'+guidance.rstrip()+'\n').encode())
        state_formalist=self.role=='formalizer' and self.owner.candidate_format=='state'
        native_stdout=self.run_dir/'native.stdout.log'
        resume=resume_from is not None
        recovering=False
        restart_delay=1
        while not self.event.is_set() and not self.owner.cancelled:
            resume_id=None
            if resume_from is not None:
                resume_id,instruction=resume_from
                prompt_file=self.base.workspace/'seekgnomes-recovery.md'
                _replace(prompt_file,(instruction+'\nThe original task is in /work/seekgnomes-task.md.\n').encode())
            elif resume and self.owner.selection.name=='dsh':
                resume_id=dsh_session_id_from_events(native_stdout)
                if resume_id is None and not state_formalist:
                    self.receipt={'session_status':'infrastructure_failure',
                        'reason':'native_closing_resume_session_id_missing','proof_verified':False}
                    break
                # When DSH stopped before publishing its session ID, relaunch
                # from the private proof workspace until a session exists.
                resume=resume_id is not None
            if resume_from is None and (resume or recovering):
                prompt_file=self.base.workspace/('seekgnomes-prove.md' if state_formalist else 'seekgnomes-closing.md')
                instruction=FORMALIST_RESUME_INSTRUCTION if state_formalist else self.closing_instruction
                context=('Read your existing proof files and any private notes before continuing.' if state_formalist
                    else 'Read your existing research-notes.md and files.')
                _replace(prompt_file,(instruction+'\nThe original task is in /work/seekgnomes-task.md. '+context+'\n').encode())
            invocation=build_invocation(self.owner.selection,'/work','/home/agent','/work/'+prompt_file.name,
                'http://127.0.0.1:8731/v1',mcp_command=('/usr/bin/python3','-I','-B','/opt/seekgnomes/role_bridge.py','mcp'),
                model_context_size=self.owner.model_context_tokens,
                resume_id=resume_id,resume_last=resume and resume_id is None)
            if self.event.is_set() or self.owner.cancelled:break
            self.record('native_launch',{'resume_existing_session':resume,'phase':self.ledger.data['phase']})
            append_logs=resume or ((recovering or self.resuming) and native_stdout.exists())
            try:
                self.active=True
                process=self.sandbox.start(invocation,self.gateway.socket_path,append_logs=append_logs)
            except OSError as error:
                if not state_formalist or error.errno not in (errno.EAGAIN, errno.ENOMEM):
                    raise
                self.active=False
                recovering=True
                restart_delay=self._wait_for_native_restart(restart_delay,
                    reason='native_start_resource_unavailable', errno=error.errno)
                continue
            resume_from=None
            started=time.monotonic()
            last_save=0;cpu_peak=0.0
            while process.poll() is None and not self.event.is_set():
                cpu_peak=max(cpu_peak,self.sandbox.cpu_seconds())
                self.ledger.charge_native_cpu(self.completed_native_cpu+cpu_peak)
                reason=self.ledger.exhausted()
                if (self.active and not self.busy_tools and not self.gateway.api_lock.locked()
                        and reason and time.monotonic()-self.gateway.last_activity>3):
                    self.terminal('infrastructure_failure' if reason.startswith('provider_') else 'budget_exhausted',reason)
                if time.monotonic()-last_save>1:
                    self.save();self.owner.save();last_save=time.monotonic()
                time.sleep(.1)
            self.completed_native_cpu+=max(cpu_peak,self.sandbox.cpu_seconds())
            self.ledger.charge_native_cpu(self.completed_native_cpu)
            if state_formalist and process.poll() is not None:
                # A detached CLI can exit with a verifier or provider request
                # still in flight. Consume its terminal receipt before relaunch.
                self.active=False
                while (not self.owner.cancelled and not self.event.is_set()
                        and (self.busy_tools or self.gateway.api_lock.locked())):
                    time.sleep(.1)
            if self.owner.cancelled:
                self.receipt={'session_status':'cancelled_by_operator','proof_verified':False};break
            if self.event.is_set():
                # Let a final MCP response reach the CLI before terminating it.
                time.sleep(.2);break
            if (self.role=='constructor' and getattr(self.owner,'awaiting_recovery_feedback',None) is not None
                    and not self.ledger.exhausted()):
                self.active=False
                instruction=(CONSTRUCTOR_CHECKPOINT_INSTRUCTION+
                    f' Retrieve handoff_id {self.owner.awaiting_recovery_feedback} with recover_handoff.')
                resume_from=self.recovery_session(instruction)
                resume=resume_from is not None
                restart_delay=self._wait_for_native_restart(restart_delay,
                    reason='native_constructor_exited_before_recovering_handoff',returncode=process.returncode)
                continue
            if state_formalist:
                if time.monotonic()-started>=30:
                    restart_delay=1
                recovering=True
                resume=process.returncode==0 or self._has_native_session(invocation)
                restart_delay=self._wait_for_native_restart(restart_delay,
                    reason='native_formalist_exited_before_handoff', returncode=process.returncode,
                    last_api_failure=self.gateway.last_failure)
                continue
            if self.gateway.last_failure is not None:
                # Some official CLIs handle an API error but still exit 0.
                # An unrecovered provider/transport failure remains missing.
                self.receipt={'session_status':'infrastructure_failure',
                    'reason':'unrecovered_native_api_failure','last_api_failure':self.gateway.last_failure,
                    'returncode':process.returncode,'proof_verified':False}
                break
            if (self.role=='constructor' and not self.owner.tools.handoffs and not self.closing_resume_used
                    and (process.returncode==0 or self.ledger.data['phase']=='closing')):
                self.enter_closing('native_cli_finished_without_definition')
                if not self.ledger.exhausted():
                    self.sandbox.stop();self.closing_resume_used=True;resume=True
                    continue
            if process.returncode:
                self.receipt={'session_status':'infrastructure_failure','reason':'native_cli_exit_nonzero',
                    'returncode':process.returncode,'proof_verified':False}
            else:
                self.receipt={'session_status':'completed','reason':'native_cli_finished',
                    'proof_verified':False}
            break
        if self.owner.cancelled:
            self.receipt={'session_status':'cancelled_by_operator','proof_verified':False}
        if (not state_formalist and not self.owner.cancelled and self.gateway.last_failure is not None
                and (self.receipt or {}).get('session_status') in ('completed','budget_exhausted')):
            self.receipt={**(self.receipt or {}),'session_status':'infrastructure_failure',
                'reason':'unrecovered_native_api_failure','last_api_failure':self.gateway.last_failure}
        self.status=(self.receipt or {}).get('session_status','infrastructure_failure')
        return self.receipt

    def cancel(self):
        self.active=False;self.closed=True
        self.event.set();self.sandbox.stop()


class TrialRuntime:
    def __init__(self,*,task_id,out,model,endpoint,api_key,budget_multiplier,
                 company=None,harness=None,api_protocol='auto',web=True,
                 completion_token_field='max_tokens',experiment_id='',model_context_tokens=None,
                 constructor_output_tokens=128000,constructor_input_tokens=None,
                 constructor_active_seconds=14400,cpus=2,candidate_format='state',resume=False):
        with _RUNNING_LOCK:self.cancel_generation=_CANCEL_GENERATION
        self.candidate_format=candidate_format
        protocol=candidate_protocol(candidate_format)
        self.definition_filename='f.state' if candidate_format=='state' else 'f.md'
        self.out=Path(os.path.abspath(out));self.model=model;self.endpoint=endpoint;self.api_key=api_key
        self.completion_token_field=completion_token_field
        self.constructor_output_tokens=constructor_output_tokens
        self.constructor_input_tokens=constructor_input_tokens
        self.constructor_active_seconds=constructor_active_seconds
        self.cpus=cpus
        self.multiplier=budget_multiplier;self.web=web;self.model_context_tokens=model_context_tokens
        if budget_multiplier!=1:raise ValueError('v1.2 uses a single configurable budget, no multiplier expansion')
        from .budget import Limits
        Limits.for_role('constructor',output_tokens=constructor_output_tokens,input_tokens=constructor_input_tokens,
            active_seconds=constructor_active_seconds,cpus=cpus)
        if cpus>len(os.sched_getaffinity(0)):raise ValueError('Requested CPUs exceed the available affinity')
        if not self.out.is_relative_to(PROJECT/'runs') or self.out.resolve()!=self.out or self.out==PROJECT/'runs':
            raise ValueError('Trial output must be a canonical path under project runs')
        if not api_key or not model or api_key in model or api_key in endpoint:raise ValueError('Invalid credential/model configuration')
        self.selection=resolve_harness(model,company=company,harness=harness,api_protocol=api_protocol)
        if not self.selection.supported:raise ValueError(self.selection.unsupported_reason)
        self.task=load_task(int(task_id));self.roles=[];self.lock=threading.RLock();self.cancelled=False
        self.archived_roles={}
        self.awaiting_recovery_feedback=None
        harness_selection=self.selection.to_dict()
        harness_selection['evidence']=list(harness_selection['evidence'])
        self.top={'schema':'seekgnomes-harbor-native-trial/v1.2','created_utc':utc(),
            'model':model,'task_id':self.task['id'],'budget_multiplier':budget_multiplier,
            'experiment_id':experiment_id,'task_sha256':hashlib.sha256(json.dumps(self.task,
                sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest(),
            'status':'constructor_running','harness_selection':harness_selection,
            'protocol':protocol,'candidate_format':candidate_format,'host_context_trimming':False,
            'resources':{'constructor_output_tokens':constructor_output_tokens,
                'constructor_input_tokens':constructor_input_tokens,'constructor_active_seconds':constructor_active_seconds,
                'cpus':cpus,'api_requests_limit':None,'closing_output_tokens':50000,
                'closing_active_seconds':constructor_active_seconds,'budget_expansion':False},
            'provenance':{'api_base_url':endpoint,'api_protocol':self.selection.api_protocol,
                'same_model_for_both_roles':True,
                'candidate_format':candidate_format,'constructor_submission':f'complete {self.definition_filename} only',
                'formalist_total_token_limit':None,'native_cli_context_management':True,
                'one_active_role_per_trial':True,'web':web,
                'cpu_meter':'reserved numerical CPU plus observed native role process-tree CPU'},
            'handoffs':[],'feedback':[],'best_verified_result':None,'verified_result':None}
        config={key:self.top[key] for key in ('task_id','task_sha256','protocol','harness_selection',
                'experiment_id','resources','provenance')}
        proof_lock=(HERE/'STATE_PROOF_SPEC.lock.json' if candidate_format=='state'
            else HERE.parent/'dual_agent_v1_1/PROOF_SPEC.lock.json')
        config.update(model_context_tokens=model_context_tokens,completion_token_field=completion_token_field,
            proof_lock_sha256=hashlib.sha256(proof_lock.read_bytes()).hexdigest())
        self.resuming=open_checkpoint(self.out,config,resume)
        manifest=self.out/'manifest.json'
        if self.resuming and manifest.exists():
            self.top=read_json(manifest)
            if any(self.top.get(key)!=config[key] for key in ('task_id','task_sha256','resources','harness_selection')):
                raise ValueError('Trial manifest differs from the authorized checkpoint')
        self.finished=bool(self.top.get('ended_utc'))
        if self.finished:return
        self.top['controller_pid']=os.getpid()
        self.top['controller_restarts']=self.top.get('controller_restarts',0)+int(self.resuming)
        task_path=self.out/'task.json'
        if self.resuming and task_path.exists():
            if read_json(task_path)!=self.task:raise ValueError('Checkpoint task has changed')
        else:
            durable_json(task_path,self.task)
        ctor_refs,formal_refs=references(self.task)
        tools_class=DefinitionTools
        self.is_verified=is_verified
        if candidate_format=='state':
            from .state_tools import StateDefinitionTools, is_verified as is_state_verified
            state_references(ctor_refs,formal_refs)
            tools_class=StateDefinitionTools
            self.is_verified=is_state_verified
        resource_notice=(
            f'Constructor main-phase output-token limit: {constructor_output_tokens or "unlimited"}; '
            f'active-second limit: {constructor_active_seconds or "unlimited"}; {cpus} CPUs; API calls unlimited; '
            f'input-token limit: {constructor_input_tokens or "unlimited"}; no cumulative CPU-seconds cap. '
            'Numerical requests may use 1..configured CPUs and 1..300 seconds. '
            f'If no complete {self.definition_filename} has been handed off, one closing phase is available. '
            f'Its output-token limit is 50000 and its active-second limit is {constructor_active_seconds or "unlimited"}. '
            'Formalist remains cumulatively uncapped.\n')
        ctor_refs['runtime_resources.md']=resource_notice
        formal_refs['runtime_resources.md']=resource_notice
        self.tools=tools_class(self.task,self.out,constructor_references=ctor_refs,
            formalizer_references=formal_refs,server=LocalNumericalBroker(max_cpus=cpus),web=web)
        if self.resuming:self._restore_progress()
        self.save()

    def _restore_progress(self):
        self.top.update(feedback=[],best_verified_result=None,verified_result=None)
        handoffs={record['handoff_id']:record for record in self.tools.handoffs}
        recovered=set()
        for path in sorted(self.out.glob('feedback-*.json')):
            record=read_json(path)
            index=record['handoff_id']
            if (index in recovered or index not in handoffs
                    or record['definition_sha256']!=handoffs[index]['definition_sha256']):
                raise ValueError('Handoff feedback does not match a unique committed definition')
            if record['kind']=='verified_state_energy':
                result=record['result']
                if (not self.is_verified(result) or result.get('handoff_id')!=index
                        or result.get('definition_sha256')!=record['definition_sha256']):
                    raise ValueError('Feedback contains an invalid fixed-goal receipt')
                verify_stored_result(result,self.task,self.out)
                self._record_verified(result)
            elif record['kind'] not in ('formalist_feedback','formalist_runtime_failure'):
                raise ValueError('Unrecognized handoff feedback kind')
            recovered.add(index)
            self.top['feedback'].append(record)
        if set(handoffs)-recovered-{max(handoffs,default=0)}:
            raise ValueError('An earlier committed handoff is missing its completed feedback')
        restore_constructor_returns(self)
        for role,directory in [('constructor',self.out/'constructor'), *[
                ('formalizer',self.out/'formalizer'/f'handoff-{index:04d}') for index in handoffs]]:
            path=directory/'budget-ledger.json'
            if not path.exists():continue
            ledger=Ledger(path,role,self.multiplier,output_tokens=self.constructor_output_tokens,
                input_tokens=self.constructor_input_tokens,active_seconds=self.constructor_active_seconds,
                cpus=self.cpus,resume=True)
            self.archived_roles[directory]=(role,ledger.snapshot())

    def _record_verified(self, result):
        self.top['verified_result']=result
        best=self.top['best_verified_result']
        if best is None or result['energy_density']<best['energy_density']:
            self.top['best_verified_result']=result

    def recovery_action(self, action):
        args=action.get('arguments')
        feedback=None
        if action['tool']=='recover_handoff':
            if not isinstance(args,dict) or set(args)!={'handoff_id'} or type(args['handoff_id']) is not int:
                return {'ok':False,'error':'Expected one integer handoff_id'}
            feedback=next((item for item in self.top['feedback'] if item['handoff_id']==args['handoff_id']),
                {'ok':False,'error':'No completed feedback for this handoff'})
        # An interrupted native tool call may be reissued on resume. Return
        # the already committed result for the same definition and notes.
        if action['tool']=='create_handoff' and isinstance(args,dict) and 'definition_path' in args:
            committed=self.tools.handoff
            if committed is None:return None
            notes=args.get('notes','')
            if (set(args)<={'definition_path','notes'} and isinstance(notes,str)
                    and args['definition_path']==committed['original_definition_path']
                    and hashlib.sha256(notes.encode()).hexdigest()==committed['notes_sha256']
                    and hashlib.sha256(self.tools.constructor_base._read_private(args['definition_path'])).hexdigest()
                        ==committed['definition_sha256']):
                feedback=next((item for item in self.top['feedback']
                    if item['handoff_id']==committed['handoff_id']),None)
        if feedback is not None:
            if feedback.get('handoff_id')==self.awaiting_recovery_feedback:
                self.awaiting_recovery_feedback=None
            return feedback
        if self.awaiting_recovery_feedback is not None and action['tool'] in ('create_handoff','finish'):
            return {'ok':False,'error':'Recover the prior handoff result before continuing',
                'handoff_id':self.awaiting_recovery_feedback}
        return None

    def save(self):
        with self.lock:
            roles={**self.archived_roles, **{r.run_dir:(r.role,r.ledger.snapshot()) for r in self.roles}}
            ordered=sorted(roles.items())
            self.top['roles']=[{'role':role,'manifest_path':str(path/'manifest.json')}
                for path,(role,_) in ordered]
            snapshots=[snapshot for _,(_,snapshot) in ordered]
            for key in ('input_tokens_charged','output_tokens_charged','server_cpu_seconds_charged'):
                self.top[key]=sum(s[key] for s in snapshots)
            self.top['turns']=sum(s['requests'] for s in snapshots)
            self.top['constructor_usage']=snapshots[0] if snapshots else None
            self.top['formalizer_usage']={key:sum(s[key] for s in snapshots[1:])
                for key in ('input_tokens_charged','output_tokens_charged','server_cpu_seconds_charged')}
            self.top['handoffs']=self.tools.handoffs
            self.top['last_updated_utc']=utc()
            atomic_json(self.out/'manifest.json',self.top)

    def make_role(self,role,broker):
        if self.cancelled:raise InterruptedError('Trial was cancelled before native role creation')
        name='constructor' if role=='constructor' else 'formalist'
        source=(HERE/'prompts'/f'{name}.state.md' if self.candidate_format=='state'
            else PROJECT/'revisions/seekgnomes_v1_1_1/prompts'/f'{name}.md')
        prompt=source.read_text()+'\n\nFixed task:\n'+json.dumps(self.task,ensure_ascii=False,indent=2)
        result=NativeRole(self,role,broker,prompt)
        self.roles.append(result)
        if self.cancelled:result.cancel()
        self.save();return result

    def handoff(self,constructor,result,*,resume_constructor=True):
        constructor.active=False
        formal=None
        try:
            # Drain requests admitted before handoff while Constructor time
            # still runs; new requests are rejected after active=False.
            with constructor.gateway.api_lock:
                constructor.sandbox.pause()
                constructor.ledger.pause()
            handoff=self.tools.handoff
            if any(handoff[key]!=result[key] for key in ('handoff_id','definition_sha256')):
                raise ValueError('Committed definition does not match the requested handoff')
            broker=self.tools.formalizer
            self.top['status']='formalizer_running';self.save()
            feedback=restore_formalist(self,broker) if self.resuming else None
            if feedback is None:
                formal=self.make_role('formalizer',broker)
                instruction=(FORMALIST_RESUME_INSTRUCTION if self.candidate_format=='state' else
                    'Resume this frozen f.md handoff with your existing implementation and Lean proof. '
                    'Continue the original proof protocol and submit the checked result.')
                checkpoint=formal.recovery_session(instruction)
                feedback=formal.run() if checkpoint is None else formal.run(resume_from=checkpoint)
            technical_failure=feedback.get('session_status') in ('infrastructure_failure','account_budget_exceeded')
            base=getattr(broker,'base',broker)
            verified=base.last_verified
            if self.is_verified(verified):
                if self.candidate_format=='state' and (
                    verified['handoff_id']!=handoff['handoff_id']
                    or verified['source_sha256']!=handoff['definition_sha256']
                ):
                    raise ValueError('State checker receipt does not match the active handoff')
                verified=dict(verified)
                for key in ('proof_manifest','source_path','proof_path','invocation_dir','energy_report'):
                    if key in verified:
                        path=Path(verified[key])
                        if not path.is_relative_to(self.out):
                            raise ValueError('Checker receipt escaped this trial')
                        verified[key+'_relative']=path.relative_to(self.out).as_posix()
                self._record_verified(verified)
                response={'ok':True,'kind':'verified_state_energy','result':verified}
                self.save()
            elif self.candidate_format=='state' and feedback.get('session_status')=='cancelled_by_operator':
                return {'ok':False,'kind':'cancelled_by_operator','proof_verified':False,
                    'handoff_id':handoff['handoff_id'],'definition_sha256':handoff['definition_sha256']}
            else:
                returned_feedback=feedback
                if self.candidate_format=='state':
                    returned_feedback={key:feedback[key] for key in (
                        'session_status','reason','issue_kind','detail','handoff_id',
                        'definition_sha256','last_api_failure','technical_diagnostic') if key in feedback}
                    returned_feedback['proof_verified']=False
                state_runtime_failure=self.candidate_format=='state' and technical_failure
                response={'ok':not state_runtime_failure,
                    'kind':'formalist_runtime_failure' if state_runtime_failure else 'formalist_feedback',
                    'feedback':returned_feedback,
                    'proof_verified':False,'energy_density':None}
            instruction=('Review the frozen .state and returned diagnostics or proof. To correct or improve '
                'the candidate, submit a new COMPLETE f.state and call create_handoff again. '
                'Otherwise continue optimization or finish.' if self.candidate_format=='state' else
                'Inspect the returned read-only implementation, proof and map for fidelity. '
                'Correct or improve by submitting a new COMPLETE f.md and calling create_handoff again; '
                'otherwise continue optimization or finish.')
            if response['kind']=='formalist_runtime_failure':
                instruction='Formalist runtime ended before verification. The trial has ended with a technical failure.'
            response.update(handoff_id=handoff['handoff_id'],definition_sha256=handoff['definition_sha256'],
                return_files=return_view(self),instruction=instruction)
            with self.lock:
                durable_json(self.out/f'feedback-{len(self.top["feedback"])+1:04d}.json',response)
                self.top['feedback'].append(response)
            if technical_failure:
                constructor.terminal(feedback['session_status'],'formalist_native_runtime_failure')
            return response
        finally:
            self.top['status']='cancelled_by_operator' if self.cancelled else 'constructor_running'
            if not self.cancelled and resume_constructor:
                constructor.ledger.resume();constructor.active=True
                constructor.sandbox.resume()
            self.save()

    def _resume_constructor(self):
        handoff=self.tools.handoff
        last_feedback=next((item for item in self.top['feedback']
            if handoff and item['handoff_id']==handoff['handoff_id']),None)
        pending=handoff is not None and last_feedback is None
        path=self.out/'constructor/manifest.json'
        if not pending and path.exists():
            terminal=read_json(path).get('terminal')
            if terminal is not None:return terminal
        if last_feedback and last_feedback['kind']=='formalist_runtime_failure':
            return {'session_status':last_feedback['feedback']['session_status'],
                'reason':'formalist_native_runtime_failure','proof_verified':False}
        constructor=self.make_role('constructor',self.tools.constructor)
        try:
            if pending:
                self.handoff(constructor,handoff,resume_constructor=False)
            instruction=CONSTRUCTOR_CHECKPOINT_INSTRUCTION
            if handoff is not None and not constructor.event.is_set():
                self.awaiting_recovery_feedback=handoff['handoff_id']
                instruction+=(f' Call benchmark_action with tool=recover_handoff and arguments='
                    f'{{"handoff_id":{self.awaiting_recovery_feedback}}} before submitting another '
                    'definition or finishing. The host will return the original handoff result.')
            checkpoint=constructor.recovery_session(instruction)
        except BaseException:
            constructor.cancel()
            constructor.gateway.close()
            raise
        return constructor.run() if checkpoint is None else constructor.run(resume_from=checkpoint)

    def run(self):
        if self.finished:return self.top
        original_affinity=os.sched_getaffinity(0)
        os.sched_setaffinity(0,set(sorted(original_affinity)[:self.cpus]))
        self.top['research_cpu_affinity']=sorted(os.sched_getaffinity(0))
        with _RUNNING_LOCK:
            if self.cancel_generation!=_CANCEL_GENERATION or (self.out/CANCELLED).exists():self.cancelled=True
            _RUNNING.add(self)
        try:
            if self.cancelled:
                self.top.update(status='cancelled_by_operator',automatically_resumed=False)
                return self.top
            if self.resuming:
                terminal=self._resume_constructor()
            else:
                terminal=self.make_role('constructor',self.tools.constructor).run()
            self.top['constructor_terminal']=terminal
            self.top['definition_submitted']=bool(self.tools.handoffs)
            constructor=next((role for role in self.roles if role.role=='constructor'),None)
            usage=constructor.ledger.snapshot() if constructor else self.top['constructor_usage']
            self.top['closing_phase_used']=bool(usage and usage['phase']=='closing')
            status=terminal.get('session_status')
            if status in ('infrastructure_failure','account_budget_exceeded','cancelled_by_operator'):
                self.top['status']=status
            else:
                self.top['status']='completed' if self.top['best_verified_result'] else 'final_submission_not_verified'
                if not self.top['best_verified_result']:
                    self.top['failure_reason']=('no_complete_definition_after_final_submission_phase'
                        if not self.tools.handoffs else 'state_legality_not_verified')
        except KeyboardInterrupt:
            self.cancelled=True;self.top.update(status='cancelled_by_operator',automatically_resumed=False)
        except Exception as exc:
            self.top.update(status='cancelled_by_operator' if self.cancelled else 'infrastructure_failure',
                error_type=type(exc).__name__)
            if self.resuming and not self.cancelled:
                self.top['controller_recovery_failure']=True
        finally:
            try:
                for role in self.roles:role.cancel()
                self.top['ended_utc']=utc();self.save()
            finally:
                with _RUNNING_LOCK:_RUNNING.discard(self)
                os.sched_setaffinity(0,original_affinity)
        return self.top


def run_trial(**kwargs):
    # Capture before initialization: cancellation can arrive while loading the
    # trusted definition/Lean broker, before a TrialRuntime is registrable.
    with _RUNNING_LOCK:generation=_CANCEL_GENERATION
    with controller_lock(kwargs['out'],PROJECT/'runs'):
        trial=TrialRuntime(**kwargs)
        trial.cancel_generation=generation
        return trial.run()
