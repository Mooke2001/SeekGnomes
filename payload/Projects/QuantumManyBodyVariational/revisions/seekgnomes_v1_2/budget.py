"""Host-owned cumulative accounting shared by every native-harness request.

No context/history manipulation lives here. In particular native compaction
and internal planning calls pass through the same ledger as ordinary calls.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, replace
from copy import deepcopy
from pathlib import Path
import json
import math
import os
import stat
import threading
import time

from revisions.seekgnomes_v1_1_1.campaign import atomic_json, utc


LEDGER_SCHEMA = 'seekgnomes-native-ledger/v1.2'
CLOSING_OUTPUT_TOKENS = 50000


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    output: int | None
    input: int | None
    requests: int | None
    active_seconds: int | None
    numerical_cpu: int | None
    request_output: int = 16000
    request_seconds: int = 600
    cpus: int = 1

    @classmethod
    def for_role(cls, role, multiplier=1, *, output_tokens=128000,
                 input_tokens=None, active_seconds=14400, cpus=1):
        if role not in ('constructor', 'formalizer') or multiplier!=1:
            raise ValueError('v1.2 has one configurable budget, with no multiplier expansion')
        if type(cpus) is not int or cpus<=0 or (output_tokens is None and active_seconds is None):
            raise ValueError('Positive CPU allocation and a Constructor output or activity limit are required')
        for value in (output_tokens,active_seconds):
            if value is not None and (type(value) is not int or value<=0):
                raise ValueError('Resource limits must be positive integers')
        if input_tokens is not None and (type(input_tokens) is not int or input_tokens<=0):
            raise ValueError('Optional input token limit must be a positive integer')
        return (cls(output_tokens,input_tokens,None,active_seconds,None,cpus=cpus)
                if role=='constructor' else cls(None,None,None,None,None,cpus=cpus))


class Ledger:
    def __init__(self, path, role, multiplier=1, *, output_tokens=128000,
                 input_tokens=None, active_seconds=14400, cpus=1, resume=False):
        self.path=Path(path)
        if self.path.is_symlink() or (self.path.exists() and not resume):
            raise ValueError('Refusing to replace an existing role ledger; fresh trial required')
        self.role=role
        self.limits=Limits.for_role(role,multiplier,output_tokens=output_tokens,
            input_tokens=input_tokens,active_seconds=active_seconds,cpus=cpus)
        self.configured_limits=asdict(self.limits)
        self.lock=threading.RLock()
        self.active_since=None
        if resume:
            self._restore()
            return
        self.data={'schema':LEDGER_SCHEMA,'role':role,
            'limits':asdict(self.limits),'created_utc':utc(),'requests':0,
            'input_tokens_charged':0,'output_tokens_charged':0,
            'server_cpu_seconds_charged':0,'native_cpu_seconds_charged':0.0,
            'active_wall_seconds':0.0,'budget_measure':'provider_reported_completion',
            'not_a_billing_estimate':True,'entries':[],'terminal_reason':None,
            'phase':'exploration' if role=='constructor' else 'formalization',
            'configured_limits':self.configured_limits,'closing_output_tokens':CLOSING_OUTPUT_TOKENS,
            'closing_started_utc':None}
        self.persist()

    @staticmethod
    def _nonnegative_number(value):
        return type(value) in (int, float) and value >= 0 and (
            type(value) is int or math.isfinite(value))

    def _restore(self):
        if self.path.absolute() != self.path.resolve(strict=True):
            raise ValueError('Role ledger checkpoint must be an unaliased regular file')
        descriptor=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise ValueError('Role ledger checkpoint must be an unaliased regular file')
            data=json.load(stream)
        if not isinstance(data,dict) or data.get('schema')!=LEDGER_SCHEMA or data.get('role')!=self.role:
            raise ValueError('Role ledger checkpoint has a different schema or role')
        configured=data.get('configured_limits')
        if (configured!=self.configured_limits or
                any(type(configured[key]) is not type(value) for key,value in self.configured_limits.items())):
            raise ValueError('Role ledger checkpoint has different configured limits')
        if data.get('closing_output_tokens')!=CLOSING_OUTPUT_TOKENS:
            raise ValueError('Role ledger checkpoint has a different closing allowance')
        self.limits=self._validate_checkpoint(data)
        self.data=data
        restored=False
        for entry in data['entries']:
            if entry['kind']=='api' and entry['in_flight']:
                # The reservation was persisted before dispatch. Its actual
                # usage may never arrive, so keep the entire charge once.
                entry.update(in_flight=False,provider_usage={},settled_utc=utc(),
                    settlement_reason='controller_interrupted')
                restored=True
        if restored:
            self.persist()

    def _validate_checkpoint(self, data):
        token_keys=('requests','input_tokens_charged','output_tokens_charged')
        measured_keys=('active_wall_seconds','server_cpu_seconds_charged','native_cpu_seconds_charged')
        if (any(type(data.get(key)) is not int or data[key]<0 for key in token_keys)
                or any(not self._nonnegative_number(data.get(key)) for key in measured_keys)
                or data.get('terminal_reason') not in (None,'provider_output_exceeded_request_cap')
                or not isinstance(data.get('entries'),list)):
            raise ValueError('Role ledger checkpoint has invalid cumulative counters')
        phase=data.get('phase')
        phases=('exploration','closing') if self.role=='constructor' else ('formalization',)
        if phase not in phases:
            raise ValueError('Role ledger checkpoint has an invalid phase')
        request_count=input_charges=output_charges=0
        main_input=main_output=0
        numerical_seconds=0
        closing_events=0
        overran_request=False
        for entry in data['entries']:
            if not isinstance(entry,dict):
                raise ValueError('Role ledger checkpoint has an invalid entry')
            kind=entry.get('kind')
            if kind=='api':
                counters=('input_reservation','input_charged','output_requested','output_charged')
                expected_phase='closing' if closing_events else phases[0]
                if (type(entry.get('index')) is not int or entry['index']!=request_count
                        or any(type(entry.get(key)) is not int or entry[key]<0 for key in counters)
                        or not isinstance(entry.get('in_flight'),bool)
                        or entry.get('phase')!=expected_phase
                        or not 0<entry['output_requested']<=self.limits.request_output):
                    raise ValueError('Role ledger checkpoint has an invalid API charge')
                if entry['in_flight'] and (entry['input_charged']!=entry['input_reservation'] or
                        entry['output_charged']!=entry['output_requested']):
                    raise ValueError('Role ledger checkpoint has an incomplete API reservation')
                overran_request=overran_request or entry['output_charged']>entry['output_requested']
                request_count+=1
                input_charges+=entry['input_charged']
                output_charges+=entry['output_charged']
                if expected_phase=='exploration':
                    main_input+=entry['input_charged']
                    main_output+=entry['output_charged']
            elif kind=='numerical_cpu_reservation' and self.role=='constructor' and self._nonnegative_number(entry.get('seconds')):
                numerical_seconds+=entry['seconds']
            elif kind=='closing_phase_started' and phase=='closing' and not closing_events:
                closing_events+=1
            else:
                raise ValueError('Role ledger checkpoint has an invalid entry or closing transition')
        if ([request_count,input_charges,output_charges]!=[data[key] for key in token_keys]
                or numerical_seconds!=data['server_cpu_seconds_charged']
                or overran_request!=bool(data.get('terminal_reason'))):
            raise ValueError('Role ledger checkpoint totals do not match its entries')
        expected=self.configured_limits.copy()
        limits=data.get('limits')
        if phase=='closing':
            closing_start=data.get('closing_start_active_seconds')
            if (not closing_events or data.get('closing_start_output_tokens')!=main_output
                    or not self._nonnegative_number(closing_start) or not isinstance(limits,dict)):
                raise ValueError('Role ledger checkpoint has inconsistent closing limits')
            closing_limit=limits.get('active_seconds')
            base_limit=expected['active_seconds']
            if (base_limit is None and closing_limit is not None or base_limit is not None and (
                    not self._nonnegative_number(closing_limit) or not base_limit<=closing_limit<=closing_start+base_limit)):
                raise ValueError('Role ledger checkpoint has inconsistent closing activity limits')
            expected['output']=main_output+CLOSING_OUTPUT_TOKENS
            if expected['input'] is not None:
                expected['input']+=main_input
            expected['active_seconds']=closing_limit
            data['active_wall_seconds']=max(data['active_wall_seconds'],closing_start)
        if (limits!=expected or any(type(limits[key]) is not type(value) for key,value in expected.items())):
            raise ValueError('Role ledger checkpoint has different effective limits')
        return Limits(**expected)

    def persist(self):
        with self.lock:
            if self.active_since is not None:
                now=time.monotonic()
                self.data['active_wall_seconds']+=now-self.active_since
                self.active_since=now
            self.path.parent.mkdir(parents=True,exist_ok=True)
            atomic_json(self.path,self.data)
            descriptor=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def checkpoint(self):
        """Commit metered active time and return the current status snapshot."""
        with self.lock:
            self.persist()
            return self.snapshot()

    def resume(self):
        with self.lock:
            if self.active_since is None:self.active_since=time.monotonic()

    def pause(self):
        with self.lock:
            if self.active_since is not None:
                self.data['active_wall_seconds']+=time.monotonic()-self.active_since
                self.active_since=None
            self.persist()

    def elapsed(self):
        return self.data['active_wall_seconds']+(time.monotonic()-self.active_since if self.active_since is not None else 0)

    def remaining(self):
        with self.lock:
            pairs={'output':('output_tokens_charged',self.limits.output),
                   'input':('input_tokens_charged',self.limits.input),
                   'requests':('requests',self.limits.requests)}
            result={name:None if cap is None else max(0,cap-self.data[key]) for name,(key,cap) in pairs.items()}
            result['active_seconds']=None if self.limits.active_seconds is None else max(0,self.limits.active_seconds-self.elapsed())
            result['numerical_cpu']=None if self.limits.numerical_cpu is None else max(0,self.limits.numerical_cpu-self.data['server_cpu_seconds_charged']-self.data['native_cpu_seconds_charged'])
            return result

    def exhausted(self):
        if self.data.get('terminal_reason'):return self.data['terminal_reason']
        for name,value in self.remaining().items():
            if value is not None and value<=0:return name+'_budget_exhausted'
        return None

    def begin_closing(self, reason):
        """One extra output allowance, preserving every already charged unit."""
        with self.lock:
            if self.role!='constructor' or self.data['phase']!='exploration':return False
            if self.data.get('terminal_reason'):return False
            elapsed=self.elapsed()
            self.limits=replace(self.limits,
                output=self.data['output_tokens_charged']+CLOSING_OUTPUT_TOKENS,
                input=(None if self.configured_limits['input'] is None else
                       self.data['input_tokens_charged']+self.configured_limits['input']),
                active_seconds=(None if self.configured_limits['active_seconds'] is None else
                                elapsed+self.configured_limits['active_seconds']))
            self.data.update(phase='closing',closing_started_utc=utc(),
                closing_reason=reason,limits=asdict(self.limits),
                closing_start_output_tokens=self.data['output_tokens_charged'],
                closing_start_active_seconds=elapsed)
            self.data['entries'].append({'kind':'closing_phase_started','reason':reason,
                'additional_output_tokens':CLOSING_OUTPUT_TOKENS,'additional_active_seconds':self.configured_limits['active_seconds'],
                'created_utc':utc()})
            self.persist();return True

    def reserve_api(self, payload):
        with self.lock:
            reason=self.exhausted()
            if reason:raise BudgetExceeded(reason)
            remaining=self.remaining()
            # Only a reservation for accounting, never a context cutoff.
            estimate=len(json.dumps(payload,ensure_ascii=False).encode())+2048
            if remaining['input'] is not None and estimate>remaining['input']:
                raise BudgetExceeded('input_budget_exhausted')
            cap=self.limits.request_output if remaining['output'] is None else min(self.limits.request_output,int(remaining['output']))
            entry={'kind':'api','index':self.data['requests'],'created_utc':utc(),
                   'phase':self.data['phase'],
                   'input_reservation':estimate,'input_charged':estimate,
                   'output_requested':cap,'output_charged':cap,'in_flight':True}
            self.data['requests']+=1
            self.data['input_tokens_charged']+=estimate
            self.data['output_tokens_charged']+=cap
            self.data['entries'].append(entry)
            self.persist()
            return entry,cap

    def settle_api(self, entry, usage=None):
        with self.lock:
            usage=usage or {}
            incoming=usage.get('input_tokens',usage.get('prompt_tokens'))
            outgoing=usage.get('output_tokens',usage.get('completion_tokens'))
            for value in (incoming,outgoing):
                if value is not None and (type(value) is not int or value<0):raise ValueError('Invalid provider usage counter')
            for value,key,total in ((incoming,'input_charged','input_tokens_charged'),(outgoing,'output_charged','output_tokens_charged')):
                if value is not None:
                    delta=value-entry[key]
                    self.data[total]+=delta
                    # A phase notice can coincide with an internal native
                    # request already in flight. Settlement of that MAIN
                    # reservation must not enlarge the final 50k allowance.
                    if self.data['phase']=='closing' and entry.get('phase')=='exploration':
                        if key=='output_charged':
                            self.limits=replace(self.limits,output=self.limits.output+delta)
                            self.data['closing_start_output_tokens']+=delta
                        elif self.limits.input is not None:
                            self.limits=replace(self.limits,input=self.limits.input+delta)
                    entry[key]=value
            self.data['limits']=asdict(self.limits)
            entry.update(in_flight=False,provider_usage=usage,settled_utc=utc())
            if outgoing is not None and outgoing>entry['output_requested']:
                self.data['terminal_reason']='provider_output_exceeded_request_cap'
            self.persist()

    def reserve_numerics(self, cpus, seconds):
        with self.lock:
            if self.role!='constructor':raise BudgetExceeded('role_tool_forbidden')
            if type(cpus) is not int or not 1<=cpus<=self.limits.cpus or type(seconds) not in (int,float) or not 1<=seconds<=300:
                raise ValueError('Numerical requests require cpus within the configured allocation and 1<=wall_seconds<=300')
            available=self.remaining()['numerical_cpu']
            if available is not None and seconds>available:raise BudgetExceeded('numerical_cpu_budget_exhausted')
            if self.remaining()['active_seconds'] is not None and seconds>self.remaining()['active_seconds']:
                raise BudgetExceeded('active_seconds_budget_exhausted')
            self.data['server_cpu_seconds_charged']+=cpus*seconds
            self.data['entries'].append({'kind':'numerical_cpu_reservation','seconds':cpus*seconds,
                'cpus':cpus,'wall_seconds':seconds,'created_utc':utc()})
            self.persist()

    def charge_native_cpu(self, cumulative):
        with self.lock:
            self.data['native_cpu_seconds_charged']=max(float(cumulative),self.data['native_cpu_seconds_charged'])

    def snapshot(self):
        with self.lock:
            return {**deepcopy(self.data),'active_wall_seconds':self.elapsed(),'remaining':self.remaining()}
