"""Trusted role-side bridge. Uses only stdlib, carries no upstream credential.

Runs as namespace init, starts a loopback HTTP proxy and the official CLI, or
serves MCP stdio when invoked by that CLI. The only host capability is a private
Unix socket, whose host endpoint binds role, model, budget and allowed tools.
"""
import argparse
import ctypes
import errno
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self):super().__init__('localhost',timeout=None)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.connect('/run/seekgnomes/broker.sock')


def host_call(payload):
    conn=UnixHTTPConnection()
    try:
        conn.request('POST','/host/tools',json.dumps(payload).encode(),{'Content-Type':'application/json'})
        response=conn.getresponse();data=json.loads(response.read())
        if response.status!=200:raise RuntimeError('Host rejected role operation')
        return data
    finally:conn.close()


def mcp():
    tools=host_call({'method':'list_tools'})['tools']
    for line in sys.stdin:
        try:
            item=json.loads(line);method=item.get('method');identifier=item.get('id')
            if identifier is None:continue
            if method=='initialize':
                value={'protocolVersion':item.get('params',{}).get('protocolVersion','2024-11-05'),
                    'capabilities':{'tools':{}},'serverInfo':{'name':'seekgnomes','version':'1.3.0'}}
            elif method=='ping':value={}
            elif method=='tools/list':value={'tools':tools}
            elif method=='tools/call':
                params=item.get('params',{})
                result=host_call({'method':'call_tool','name':params.get('name'),'arguments':params.get('arguments',{})})
                value={'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],
                    'isError':result.get('ok') is False}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':identifier,'error':{'code':-32601,'message':'Method not found'}}),flush=True)
                continue
            print(json.dumps({'jsonrpc':'2.0','id':identifier,'result':value}),flush=True)
        except Exception as exc:
            print(json.dumps({'jsonrpc':'2.0','id':locals().get('identifier'),
                'error':{'code':-32603,'message':type(exc).__name__}}),flush=True)


def lock_cpu_affinity(cpu_count=1):
    """Pin this role and prevent descendants from widening their CPU mask."""
    available=os.sched_getaffinity(0)
    if type(cpu_count) is not int or not 1<=cpu_count<=len(available):
        raise RuntimeError('Configured native role CPU count is unavailable')
    os.sched_setaffinity(0,set(sorted(available)[:cpu_count]))
    library=ctypes.CDLL('libseccomp.so.2',use_errno=True)
    library.seccomp_init.argtypes=[ctypes.c_uint32]
    library.seccomp_init.restype=ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype=ctypes.c_int
    library.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
    library.seccomp_rule_add.restype=ctypes.c_int
    library.seccomp_load.argtypes=[ctypes.c_void_p]
    library.seccomp_load.restype=ctypes.c_int
    library.seccomp_release.argtypes=[ctypes.c_void_p]
    context=library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:raise RuntimeError('Cannot create native role CPU policy')
    try:
        syscall=library.seccomp_syscall_resolve_name(b'sched_setaffinity')
        if syscall<0 or library.seccomp_rule_add(context,0x00050000|errno.EPERM,syscall,0)<0:
            raise RuntimeError('Cannot constrain native role CPU affinity')
        if library.seccomp_load(context)<0:raise RuntimeError('Cannot enforce native role CPU policy')
    finally:library.seccomp_release(context)


def run(config):
    settings=json.loads(Path(config).read_text())
    # Applied before creating threads; inherited by the CLI and all commands.
    # Host numerical/Lean workers retain their independent resource policies.
    lock_cpu_affinity(settings.get('cpu_count',1))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.forward()
        def do_POST(self):self.forward()
        def forward(self):
            length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=16*1024*1024:self.send_error(413);return
            payload=self.rfile.read(length)
            connection=UnixHTTPConnection()
            try:
                headers={k:v for k,v in self.headers.items() if k.lower() in ('content-type','anthropic-version','anthropic-beta','accept')}
                connection.request(self.command,self.path,payload,headers)
                result=connection.getresponse()
                self.send_response(result.status)
                self.send_header('Content-Type',result.getheader('Content-Type','application/json'))
                self.send_header('Connection','close');self.end_headers()
                while chunk:=result.read1(65536):
                    try:self.wfile.write(chunk);self.wfile.flush()
                    except (BrokenPipeError,ConnectionResetError):pass
            finally:connection.close()
    server=ThreadingHTTPServer(('127.0.0.1',settings['port']),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    env=os.environ.copy();env.update(settings.get('env',{}))
    env.pop('SEEKGNOMES_API_KEY',None);env.pop('EUREKA_API_KEY',None)
    with open(settings['stdin_path'],'rb') if settings.get('stdin_path') else open(os.devnull,'rb') as incoming:
        process=subprocess.Popen(settings['argv'],cwd=settings.get('cwd','/work'),env=env,stdin=incoming)
        code=process.wait()
    server.shutdown()
    return code


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('mcp','run'));parser.add_argument('config',nargs='?')
    args=parser.parse_args()
    if args.mode=='mcp':mcp()
    else:raise SystemExit(run(args.config))
