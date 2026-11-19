"""OS-enforced private role filesystem and network for official CLI harnesses."""
from __future__ import annotations
from pathlib import Path
import json
import os
import signal
import subprocess
import time

from revisions.seekgnomes_v1_1_1.campaign import atomic_json
from revisions.dual_agent_v1_1.tools import _mkdir, _replace
from . import role_bridge

HARNESS_ROOT=Path('/opt/seekgnomes/harness')


def _within(path, root):
    try:return Path(path).is_relative_to(root)
    except (ValueError,TypeError):return False


class RoleSandbox:
    def __init__(self, run_dir, workspace, *, harness_root=None,cpus=1):
        self.run_dir=Path(run_dir)
        self.workspace=Path(workspace)
        self.home=self.run_dir/'native-home'
        self.config_dir=self.run_dir/'native-launch'
        for path in (self.home,self.config_dir,self.home/'tmp'):
            _mkdir(path)
        self.harness_root=Path(harness_root or os.environ.get('SEEKGNOMES_HARNESS_ROOT',str(HARNESS_ROOT)))
        self.process=None
        self.streams=[]
        self.cpus=cpus
        self.launch_count=max((int(path.stem.rsplit('-',1)[1])
            for path in self.config_dir.glob('native-invocation-*.json')
            if path.stem.rsplit('-',1)[1].isascii() and path.stem.rsplit('-',1)[1].isdigit()),default=0)
        self.suspended={}

    def _physical_config(self, path):
        candidate=Path(path)
        if '..' in candidate.parts:raise ValueError('Unsafe native configuration path')
        if _within(candidate,Path('/home/agent')):
            return self.home/candidate.relative_to('/home/agent')
        if _within(candidate,Path('/work')):
            return self.workspace/candidate.relative_to('/work')
        raise ValueError('Native configuration must stay in the private role home or workspace')

    def command(self, invocation, socket_path, *, port=8731, role_token='seekgnomes-private-role'):
        if os.getuid()==0:raise RuntimeError('Role sandboxes require a non-root controller user')
        if not Path('/usr/bin/bwrap').is_file():raise RuntimeError('bubblewrap is required; no isolation fallback')
        if not self.harness_root.is_dir():raise RuntimeError('Pinned native CLI assets are missing')
        for name,text in invocation.render_config_files(role_token).items():
            target=self._physical_config(name)
            # The native CLI can edit its home between phases. Reuse the
            # reviewed descriptor-relative writer: no symlink traversal in
            # parents or leaves, no hardlink writes, atomic replacement.
            _replace(target,text.encode())
        settings={'argv':list(invocation.argv),'env':invocation.environment(role_token),
                  'cwd':invocation.cwd,'stdin_path':invocation.stdin_path,'port':port,'cpu_count':self.cpus}
        atomic_json(self.config_dir/'launch.json',settings)
        atomic_json(self.config_dir/'native-invocation.json',invocation.snapshot())
        command=['/usr/bin/bwrap','--die-with-parent','--new-session',
            '--unshare-pid','--unshare-ipc','--unshare-net','--unshare-uts',
            '--clearenv','--cap-drop','ALL','--ro-bind','/usr','/usr',
            '--symlink','usr/bin','/bin','--symlink','usr/sbin','/sbin']
        for path in ('/lib','/lib64'):
            if Path(path).exists():command+=['--ro-bind',path,path]
        command+=['--dir','/etc']
        for path in ('/etc/ld.so.cache','/etc/passwd','/etc/group','/etc/nsswitch.conf'):
            if Path(path).exists():command+=['--ro-bind',path,path]
        command+=['--dir','/opt','--dir','/opt/seekgnomes',
            '--ro-bind',str(self.harness_root),str(HARNESS_ROOT),
            '--ro-bind',str(Path(role_bridge.__file__).resolve()),'/opt/seekgnomes/role_bridge.py',
            '--dir','/run','--dir','/run/seekgnomes',
            '--ro-bind',str(socket_path),'/run/seekgnomes/broker.sock',
            '--ro-bind',str(self.config_dir/'launch.json'),'/run/seekgnomes/launch.json',
            '--bind',str(self.workspace),'/work','--dir','/home',
            '--bind',str(self.home),'/home/agent',
            '--tmpfs','/tmp','--dev','/dev','--proc','/proc',
            '--setenv','PATH','/opt/seekgnomes/harness/bin:/usr/local/bin:/usr/bin:/bin',
            '--setenv','HOME','/home/agent','--setenv','LANG','C.UTF-8',
            '--setenv','PYTHONDONTWRITEBYTECODE','1','--setenv','PYTHONNOUSERSITE','1',
            '--remount-ro','/dev','--remount-ro','/','--chdir','/work',
            '--','/usr/bin/python3','-I','-B','/opt/seekgnomes/role_bridge.py',
            'run','/run/seekgnomes/launch.json']
        return command

    def start(self, invocation, socket_path, *, port=8731,append_logs=False):
        command=self.command(invocation,socket_path,port=port)
        mode='ab' if append_logs else 'xb'
        out=(self.run_dir/'native.stdout.log').open(mode)
        err=(self.run_dir/'native.stderr.log').open(mode)
        self.streams=[out,err]
        self.launch_count+=1
        atomic_json(self.config_dir/f'native-invocation-{self.launch_count:04d}.json',invocation.snapshot())
        self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
            env={},start_new_session=True)
        record={'pid':self.process.pid,
            'argv':command,'upstream_key_in_environment':False,'network':'isolated_namespace_role_gateway_only',
            'filesystem':'private_role_workspace_and_home_readonly_system_and_native_harness',
            'native_role_cpu_affinity_count':self.cpus,
            'native_role_cpu_affinity_expansion':'seccomp_denied',
            'scientific_python_mounted':False,'host_docker_socket_mounted':False}
        atomic_json(self.run_dir/f'native-process-{self.launch_count:04d}.json',record)
        atomic_json(self.run_dir/'native-process.json',record)
        return self.process

    def cpu_seconds(self):
        if self.process is None:return 0.0
        ticks=os.sysconf('SC_CLK_TCK')
        seen=set()
        def visit(pid):
            if pid in seen:return 0
            seen.add(pid)
            try:
                raw=Path(f'/proc/{pid}/stat').read_text()
                fields=raw[raw.rfind(')')+2:].split()
                value=sum(int(fields[index]) for index in (11,12,13,14))
                _,children=self._process_info(pid)
                return value+sum(visit(child) for child in children)
            except (OSError,ValueError,IndexError):return 0
        return visit(self.process.pid)/ticks

    @staticmethod
    def _process_info(pid):
        try:
            raw=Path(f'/proc/{pid}/stat').read_text()
            fields=raw[raw.rfind(')')+2:].split()
            children=set()
            for task in Path(f'/proc/{pid}/task').iterdir():
                try:children.update(int(x) for x in (task/'children').read_text().split())
                except (OSError,ValueError):pass
            return fields[19],sorted(children)
        except (OSError,ValueError,IndexError):return None,[]

    def pause(self):
        """Stop this namespace tree, including detached native commands."""
        if self.process is None or self.process.poll() is not None:return
        pending=[self.process.pid]
        # Stopping each discovered parent before descending closes the fork
        # race. Revisit all stopped parents to find children reparented to the
        # namespace init during the first traversal.
        while pending:
            pid=pending.pop()
            birth,children=self._process_info(pid)
            if birth is None:continue
            if pid not in self.suspended:
                try:os.kill(pid,signal.SIGSTOP)
                except ProcessLookupError:continue
                self.suspended[pid]=birth
                deadline=time.monotonic()+2
                while True:
                    try:
                        status=Path(f'/proc/{pid}/stat').read_text()
                        state=status[status.rfind(')')+2:].split()[0]
                    except (OSError,IndexError):break
                    if state in ('T','t','Z','D'):break
                    if time.monotonic()>=deadline:raise RuntimeError('Cannot confirm native role pause')
                    time.sleep(.005)
            pending.extend(child for child in children if child not in self.suspended)
            if not pending:
                for parent in list(self.suspended):
                    _,children=self._process_info(parent)
                    pending.extend(child for child in children if child not in self.suspended)

    def resume(self):
        for pid,birth in reversed(list(self.suspended.items())):
            current,_=self._process_info(pid)
            if current==birth:
                try:os.kill(pid,signal.SIGCONT)
                except ProcessLookupError:pass
        self.suspended.clear()

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            try:self.pause()
            except (OSError,RuntimeError):pass
        owned=dict(self.suspended)
        self.resume()
        for pid,birth in reversed(list(owned.items())):
            if self._process_info(pid)[0]==birth:
                try:os.kill(pid,signal.SIGTERM)
                except ProcessLookupError:pass
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid,signal.SIGTERM)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait()
            except ProcessLookupError:pass
        for pid,birth in owned.items():
            if self._process_info(pid)[0]==birth:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
        for stream in self.streams:stream.close()


def probe_role_sandbox():
    """No model call; reports prerequisites without claiming an executed probe."""
    return {'ok':os.getuid()!=0 and Path('/usr/bin/bwrap').is_file(),
            'non_root':os.getuid()!=0,'bubblewrap':Path('/usr/bin/bwrap').is_file(),
            'actual_namespace_self_test':'run seekgnomes self-test',
            'isolation_fallback':False,'external_api_calls':0}
