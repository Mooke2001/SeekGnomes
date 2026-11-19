"""Private per-role HTTP-over-Unix gateway. Real credentials stay in the host.

The native CLI sees a loopback URL inside its network namespace. A trusted
bridge forwards only HTTP over the mounted Unix socket. Every upstream model
request, including the harness' own compaction calls, is metered here.
"""
from __future__ import annotations
import ctypes
from http.server import BaseHTTPRequestHandler
import http.client
import json
from pathlib import Path
import os
import signal
import socketserver
import stat
import threading
import time
import multiprocessing
from urllib.parse import urlsplit, urlencode, parse_qsl, unquote
import re
import httpx

from .budget import BudgetExceeded
from revisions.seekgnomes_v1_1_1.campaign import atomic_json, utc

BODY_LIMIT=16*1024*1024


def add_system_instruction(payload,protocol,instruction):
    """Inject a host phase notice without rewriting native conversation history."""
    from copy import deepcopy
    result=deepcopy(payload)
    if protocol=='responses':
        existing=result.get('instructions','')
        if not isinstance(existing,str):raise ValueError('Unsupported native Responses instructions shape')
        result['instructions']=existing+'\n\n'+instruction
    elif protocol=='messages':
        existing=result.get('system',[])
        if isinstance(existing,str):existing=[{'type':'text','text':existing}]
        if not isinstance(existing,list):raise ValueError('Unsupported native Messages system shape')
        result['system']=existing+[{'type':'text','text':instruction}]
    elif protocol=='chat_completions':
        messages=result.get('messages',[])
        if not isinstance(messages,list):raise ValueError('Unsupported native Chat messages shape')
        result['messages']=messages+[{'role':'system','content':instruction}]
    elif protocol=='gemini':
        system=result.get('systemInstruction') or {}
        if not isinstance(system,dict):raise ValueError('Unsupported native Gemini system shape')
        parts=system.get('parts',[])
        if not isinstance(parts,list):raise ValueError('Unsupported native Gemini system parts')
        result['systemInstruction']={**system,'parts':parts+[{'text':instruction}]}
    else:raise ValueError('Unsupported native protocol')
    return result


def response_usage(raw, content_type):
    """Read native JSON/SSE usage without dropping/recreating provider events."""
    documents=[]
    if 'event-stream' in content_type:
        for line in raw.splitlines():
            if not line.startswith(b'data:'):continue
            value=line[5:].strip()
            if value==b'[DONE]':continue
            try:documents.append(json.loads(value))
            except (ValueError,UnicodeError):pass
    else:
        try:documents=[json.loads(raw)]
        except (ValueError,UnicodeError):pass
    usage,models={},set()
    for item in documents:
        if not isinstance(item,dict):continue
        objects=[item]
        for key in ('response','message'):
            if isinstance(item.get(key),dict):objects.append(item[key])
        for obj in objects:
            if isinstance(obj.get('model'),str):models.add(obj['model'])
            if isinstance(obj.get('modelVersion'),str):models.add(obj['modelVersion'])
            value=obj.get('usage')
            if isinstance(value,dict):usage.update(value)
            value=obj.get('usageMetadata')
            if isinstance(value,dict):
                # Gemini reports thought tokens separately from candidates.
                # Both count toward the full generated-output budget.
                candidates=value.get('candidatesTokenCount')
                thoughts=value.get('thoughtsTokenCount',0)
                usage.update(input_tokens=value.get('promptTokenCount'),
                    output_tokens=(candidates+thoughts if type(candidates) is int and type(thoughts) is int else None),
                    provider_candidates_tokens=candidates,provider_thought_tokens=thoughts)
    return usage,sorted(models)


def native_response_error(raw,content_type):
    """Explicit protocol errors can occur inside an HTTP-200 stream."""
    documents=[]
    candidates=[line[5:].strip() for line in raw.splitlines() if line.startswith(b'data:')] if 'event-stream' in content_type else [raw]
    for candidate in candidates:
        try:documents.append(json.loads(candidate))
        except (ValueError,UnicodeError):pass
    for item in documents:
        if not isinstance(item,dict):continue
        if item.get('type') in ('error','response.failed','message_error'):return item.get('type')
        if item.get('error') is not None:return 'provider_error'
        response=item.get('response')
        if isinstance(response,dict) and (response.get('status')=='failed' or response.get('error') is not None):
            return 'response_failed'
        if item.get('status')=='failed':return 'response_failed'
    return None


def bounded_payload(payload, protocol, model, cap, completion_token_field='max_tokens'):
    result=dict(payload)
    if protocol=='gemini':
        generation=dict(result.get('generationConfig') or {})
        value=generation.get('maxOutputTokens',cap)
        if type(value) is not int or value<=0:raise ValueError('Invalid native output limit')
        generation['maxOutputTokens']=min(cap,value)
        result['generationConfig']=generation
    else:
        requested=result.get('model')
        if requested is not None and requested!=model:
            raise ValueError('Native harness attempted a different model than the fixed trial model')
        result['model']=model
        field={'responses':'max_output_tokens','messages':'max_tokens','chat_completions':'max_tokens'}[protocol]
        if protocol=='chat_completions' and completion_token_field=='max_completion_tokens':field='max_completion_tokens'
        limits=[cap]
        for key in ((field,'max_tokens','max_completion_tokens') if protocol=='chat_completions' else (field,)):
            if key in result:
                value=result[key]
                if type(value) is not int or value<=0:raise ValueError('Invalid native output limit')
                limits.append(value)
        result[field]=min(limits)
        if protocol=='chat_completions' and 'max_completion_tokens' in result:
            result['max_completion_tokens']=result[field]
        if protocol=='chat_completions':
            if completion_token_field=='both':result['max_completion_tokens']=result[field]
            elif completion_token_field=='max_completion_tokens':result.pop('max_tokens',None)
    return result


class _UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=False
    block_on_close=True


def require_live_parent(parent_pid):
    """Keep a Linux subprocess from outliving the controller that launched it."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # Cover the spawn-to-prctl window as well as later parent death.
    if os.getppid() != parent_pid:
        os._exit(1)


def _upstream_worker(pipe,url,request,headers,seconds,parent_pid):
    """Trusted isolated I/O process; no credential in argv or persistent files."""
    try:
        require_live_parent(parent_pid)
        with httpx.Client(trust_env=False,timeout=httpx.Timeout(seconds,connect=min(60,seconds)),follow_redirects=False) as client:
            with client.stream('POST',url,json=request,headers=headers) as response:
                pipe.send(('headers',response.status_code,response.headers.get('content-type','application/json')))
                size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>BODY_LIMIT:raise ValueError('Provider response exceeded bound')
                    pipe.send(('data',chunk))
                pipe.send(('done',))
    except Exception as exc:
        pipe.send(('error',type(exc).__name__))
    finally:pipe.close()


class RoleGateway:
    def __init__(self, directory, *, role, model, endpoint, api_key, protocol, ledger,
                 tool_dispatch, active=lambda:True, terminal=None,completion_token_field='max_tokens',
                 prepare_request=lambda payload:payload, resume=False):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.api_dir=self.directory/'api';self.api_dir.mkdir(exist_ok=True)
        self.role,self.model,self.endpoint,self.key,self.protocol=role,model,endpoint.rstrip('/'),api_key,protocol
        self.ledger,self.tool_dispatch,self.active,self.terminal=ledger,tool_dispatch,active,terminal
        if completion_token_field not in ('max_tokens','max_completion_tokens','both'):raise ValueError('Invalid completion token field')
        self.completion_token_field=completion_token_field
        self.prepare_request=prepare_request
        self.api_lock=threading.Lock()
        self.tool_lock=threading.RLock()
        self.request_index=0
        self.last_activity=time.monotonic()
        self.last_failure=None
        self.upstream_process=None
        self.socket_path=self.directory/'broker.sock'
        # AF_UNIX paths are limited to 108 bytes. /proc/self/fd preserves a
        # real project-local socket without relocating it outside the project.
        self.dir_fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        address=f'/proc/self/fd/{self.dir_fd}/broker.sock'
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):owner._handle(self)
            def do_GET(self):owner._handle(self)
        try:
            if resume:
                # The resumed runtime owns the exclusive controller lock.
                try:
                    old = os.stat('broker.sock', dir_fd=self.dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid() or old.st_nlink != 1:
                        raise ValueError('Unsafe checkpoint broker socket')
                    os.unlink('broker.sock', dir_fd=self.dir_fd)
            self.server=_UnixServer(address,Handler)
        except BaseException:
            os.close(self.dir_fd)
            raise
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)

    def start(self):
        self.thread.start();return self

    def close(self):
        process=self.upstream_process
        if process is not None and process.is_alive():
            process.terminate();process.join(timeout=3)
            if process.is_alive():process.kill();process.join()
        self.server.shutdown();self.server.server_close()
        self.socket_path.unlink(missing_ok=True)
        os.close(self.dir_fd)

    @staticmethod
    def _reply(handler,status,value):
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        try:
            handler._response_started=True
            handler.send_response(status);handler.send_header('Content-Type','application/json')
            handler.send_header('Content-Length',str(len(raw)));handler.end_headers()
            handler.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass

    def _exchange(self,url,request,headers):
        context=multiprocessing.get_context('spawn')
        reader,writer=context.Pipe(duplex=False)
        seconds=self.ledger.limits.request_seconds
        active_remaining=self.ledger.remaining()['active_seconds']
        if active_remaining is not None:seconds=min(seconds,active_remaining)
        if seconds<=0:
            reader.close();writer.close()
            raise BudgetExceeded('active_seconds_budget_exhausted')
        process=context.Process(target=_upstream_worker,
            args=(writer,url,request,headers,seconds,os.getpid()),daemon=True)
        self.upstream_process=process
        deadline=time.monotonic()+seconds
        process.start();writer.close()
        raw=bytearray();status=None;content_type=None
        try:
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not reader.poll(remaining):
                    if active_remaining is not None and active_remaining<=self.ledger.limits.request_seconds:
                        raise BudgetExceeded('active_seconds_budget_exhausted')
                    raise TimeoutError('Upstream total request deadline exceeded')
                message=reader.recv()
                if message[0]=='headers':status,content_type=message[1:]
                elif message[0]=='data':
                    raw.extend(message[1])
                    if len(raw)>BODY_LIMIT:raise ValueError('Provider response exceeded bound')
                elif message[0]=='error':raise RuntimeError('Upstream failure: '+message[1])
                elif message[0]=='done':break
            if status is None:raise RuntimeError('Missing upstream response headers')
            return status,content_type,raw
        except BaseException as exc:
            # Keep bytes already received for diagnosis without replaying an
            # incomplete stream to the model or executing its partial tools.
            exc.native_partial_response=bytes(raw)
            exc.native_http_status=status
            exc.native_response_content_type=content_type
            raise
        finally:
            reader.close()
            if process.is_alive():process.terminate()
            process.join(timeout=3)
            if process.is_alive():process.kill();process.join()
            self.upstream_process=None

    def _handle(self, handler):
        try:
            if not self.active():return self._reply(handler,409,{'error':{'message':'This role is paused or terminal'}})
            length=int(handler.headers.get('Content-Length','0'))
            if not 0<=length<=BODY_LIMIT:raise ValueError('Bounded request body required')
            raw=handler.rfile.read(length)
            payload=json.loads(raw) if raw else {}
            if not isinstance(payload,dict):raise ValueError('Expected a JSON object')
            json.dumps(payload,allow_nan=False)
            if handler.path=='/host/tools':
                if handler.command!='POST':raise ValueError('Tools require POST')
                with self.tool_lock:
                    if not self.active():return self._reply(handler,409,{'error':'role_paused'})
                    result=self.tool_dispatch(payload)
                    return self._reply(handler,200,result)
            if handler.path in ('/models','/v1/models') and handler.command=='GET':
                return self._reply(handler,200,{'object':'list','data':[{'id':self.model,'object':'model'}]})
            suffix={'responses':'/responses','messages':'/messages','chat_completions':'/chat/completions'}
            if handler.command!='POST' or (self.protocol!='gemini' and urlsplit(handler.path).path not in (suffix[self.protocol],'/v1'+suffix[self.protocol])):
                return self._reply(handler,404,{'error':{'message':'Only the selected native model API is exposed'}})
            if self.protocol=='gemini':
                match=re.fullmatch(r'/v1(?:beta)?/models/(.+):(generateContent|streamGenerateContent)',urlsplit(handler.path).path)
                if not match:return self._reply(handler,404,{'error':{'message':'Unsupported Gemini operation'}})
                if unquote(match[1])!=self.model:
                    self.last_failure={'type':'NativeModelSelectionRejected','at':utc()}
                    return self._reply(handler,400,{'error':{'message':'Native harness attempted a different model than the fixed trial model'}})
            # Native CLIs may send the next turn as soon as response bytes
            # arrive, while the preceding handler is durably writing its
            # audit. Serialize admission instead of rejecting that valid turn.
            if not self.api_lock.acquire(timeout=self.ledger.limits.request_seconds+5):
                return self._reply(handler,429,{'error':{'message':'One model request per active role is permitted'}})
            try:
                if not self.active():return self._reply(handler,409,{'error':{'message':'This role is paused or terminal'}})
                self._forward(handler,payload)
            finally:self.api_lock.release()
        except BudgetExceeded as exc:
            if self.terminal:self.terminal('budget_exhausted',str(exc))
            return self._reply(handler,429,{'error':{'message':str(exc),'type':'budget_exhausted'}})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
            if handler.command=='POST' and urlsplit(handler.path).path!='/host/tools':
                self.last_failure={'type':'NativeRequestValidationError','at':utc()}
            return self._reply(handler,400,{'error':{'message':str(exc)[:300]}})
        except Exception as exc:
            # Credentials may occur in HTTP exception objects. Preserve type only.
            self.last_failure={'type':type(exc).__name__,'at':utc()}
            if getattr(handler,'_response_started',False):return
            return self._reply(handler,502,{'error':{'message':'Host gateway failed','type':type(exc).__name__}})

    def _forward(self,handler,payload):
        # Validate before admitting a request. No model-changing request reaches
        # a provider, and no budget is charged for a rejected local schema.
        original_payload=payload
        payload=self.prepare_request(payload)
        bounded_payload(payload,self.protocol,self.model,self.ledger.limits.request_output,self.completion_token_field)
        entry,cap=self.ledger.reserve_api(payload)
        index=entry['index']
        request=bounded_payload(payload,self.protocol,self.model,cap,self.completion_token_field)
        record={'schema':'seekgnomes-native-api/v1.2','created_utc':utc(),'role':self.role,
            'requested_model':self.model,'protocol':self.protocol,'request':request,
            'native_request':original_payload,'budget_phase':self.ledger.data['phase'],
            'request_timeout_seconds':self.ledger.limits.request_seconds,'http_status':None,'ok':False}
        atomic_json(self.api_dir/f'{index:05d}.request.json',record)
        usage={};raw=bytearray();started=time.monotonic()
        try:
            suffix={'responses':'/responses','messages':'/messages','chat_completions':'/chat/completions'}
            if self.protocol=='gemini':
                path=urlsplit(handler.path).path
                base=urlsplit(self.endpoint)
                prefix=base.path.rstrip('/')
                for version in ('/v1beta','/v1'):
                    if prefix.endswith(version):
                        prefix=prefix[:-len(version)];break
                url=base.scheme+'://'+base.netloc+prefix+path
                query=[(k,v) for k,v in parse_qsl(urlsplit(handler.path).query)
                       if k.lower() not in ('key','api_key','access_token')]
                if query:url+='?'+urlencode(query)
            else:
                base=self.endpoint[:-3] if self.endpoint.endswith('/v1') else self.endpoint
                url=base+'/v1'+suffix[self.protocol]
            headers={'Content-Type':'application/json','Authorization':'Bearer '+self.key}
            if self.protocol=='messages':
                headers.update({'x-api-key':self.key,'anthropic-version':handler.headers.get('anthropic-version','2023-06-01')})
            if self.protocol=='gemini':headers['x-goog-api-key']=self.key
            status,content_type,raw=self._exchange(url,request,headers)
            record['http_status']=status
            if status!=200:self.last_failure={'type':'ProviderHTTPError','http_status':status,'at':utc()}
            usage,models=response_usage(bytes(raw),content_type)
            if self.protocol=='messages' and 'input_tokens' in usage:
                usage['provider_input_tokens_uncached']=usage['input_tokens']
                usage['input_tokens']+=usage.get('cache_creation_input_tokens',0)+usage.get('cache_read_input_tokens',0)
            record.update(ok=status==200,returned_models=models,provider_usage=usage,
                        response_content_type=content_type)
            provider_error=native_response_error(bytes(raw),content_type) if status==200 else None
            if provider_error:
                record.update(ok=False,error='native_provider_error',native_error_type=provider_error)
                self.last_failure={'type':'NativeProviderError','native_error_type':provider_error,'at':utc()}
                self._reply(handler,502,{'error':{'message':'Provider returned a failed native response','type':provider_error}})
                return
            if status==200 and (not models or any(value!=self.model for value in models)):
                record.update(ok=False,error='model_identity_mismatch_or_missing')
                if self.terminal:self.terminal('infrastructure_failure',record['error'])
                self._reply(handler,502,{'error':{'message':record['error']}})
                return
            if status in (401,402,403):
                record['error']='provider_auth_or_account_rejected'
                if self.terminal:self.terminal('account_budget_exceeded',record['error'])
            # Drain and validate before replaying the original native
            # bytes: a wrong-model response must never execute a tool.
            self.ledger.settle_api(entry,usage)
            if self.ledger.data.get('terminal_reason'):
                record.update(ok=False,error=self.ledger.data['terminal_reason'])
                if self.terminal:self.terminal('infrastructure_failure',record['error'])
                self._reply(handler,502,{'error':{'message':record['error']}})
                return
            if status==200:self.last_failure=None
            handler._response_started=True
            handler.send_response(status)
            handler.send_header('Content-Type',content_type)
            handler.send_header('Content-Length',str(len(raw)))
            handler.send_header('Connection','close');handler.end_headers()
            try:handler.wfile.write(raw);handler.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:
            record.update(ok=False,error_type=type(exc).__name__)
            if hasattr(exc,'native_partial_response'):
                raw=bytearray(exc.native_partial_response)
                record['http_status']=exc.native_http_status
                record['response_content_type']=exc.native_response_content_type
            if not isinstance(exc,BudgetExceeded):
                self.last_failure={'type':type(exc).__name__,'at':utc()}
            if record.get('http_status')==200 and isinstance(exc,(ValueError,TypeError,KeyError)):
                record.update(ok=False,error='invalid_native_provider_response')
                if self.terminal:self.terminal('infrastructure_failure',record['error'])
                self._reply(handler,502,{'error':{'message':record['error'],'type':type(exc).__name__}})
                return
            raise
        finally:
            if entry.get('in_flight'):
                try:self.ledger.settle_api(entry,usage)
                except ValueError:
                    # Retain the admitted reservation when a provider gives
                    # malformed counters; never lose the final API audit.
                    self.ledger.settle_api(entry,{})
            record['elapsed_seconds']=time.monotonic()-started
            # Raw native events are retained unchanged apart from secret redaction.
            record['response_text']=bytes(raw).decode('utf-8',errors='replace').replace(self.key,'[REDACTED]')
            atomic_json(self.api_dir/f'{index:05d}.json',record)
            self.last_activity=time.monotonic()
