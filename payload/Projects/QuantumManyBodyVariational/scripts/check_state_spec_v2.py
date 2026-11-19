"""Kernel-check an infinite multi-qubit-cell state and a fixed rational upper bound.

The trusted host derives a complete Lean task from JSON. Candidate source, when
provided, compiles in a filesystem/network-isolated namespace; the resulting
proof export is independently compared and replayed. No candidate source runs
with access to relay credentials, other attempts, or writable trusted files.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import shutil
import stat
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
LEAN = PROJECT / 'lean'
RUNTIME = PROJECT / 'tmp/lean-runtime/lean-4.35.0-linux'
BIN = RUNTIME / 'bin'
LOCK = PROJECT / 'STATE_SPEC_V2.lock.json'
V1LOCK = PROJECT / 'STATE_SPEC_V1.lock.json'
ALLOWED_IMPORTS = {'TrustedTask', 'QuantumManyBodyVariational.TrustedStateSpecV2',
                   'QuantumManyBodyVariational.TrustedSpecExamplesV2',
                   'QuantumManyBodyVariational.StateExamples',
                   'QuantumManyBodyVariational.StateClosure','Mathlib'}
SAFE_PROJECT_MODULES = ['LocalPauli','StateLimits','PauliRecovery','CorrelatorStates',
                        'TrustedStateSpec','StateExamples','StateClosure',
                        'TrustedStateSpecV2','TrustedSpecExamplesV2']
PRIMITIVES = ['Quot', 'Quot.mk', 'Quot.lift', 'Quot.ind', 'Submission.accepted',
              'Submission.f', 'Submission.bound', 'propext', 'Quot.sound', 'Classical.choice',
              'Nat.add', 'Nat.sub', 'Nat.mul', 'Nat.pow', 'Nat.gcd', 'Nat.div', 'Nat.mod',
              'Nat.beq', 'Nat.ble', 'Nat.land', 'Nat.lor', 'Nat.xor', 'Nat.shiftLeft',
              'Nat.shiftRight', 'String.ofList', 'Char.ofNat', 'List', 'eagerReduce', 'Nat',
              'String', 'String.mk', 'Char', 'optParam', 'autoParam', 'semiOutParam', 'outParam']

def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a leaf without following a symlink at any path component."""
    parts = Path(os.path.abspath(path)).parts
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                       mode, dir_fd=parent)
    finally:
        os.close(parent)

@contextmanager
def regular_input(path: Path, max_bytes: int = 512 * 1024**2):
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f'Artifact must be a single-link regular file: {path}')
        if info.st_size > max_bytes:
            raise ValueError(f'Artifact exceeds size limit: {path}')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            yield stream
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ValueError(f'Artifact changed during host read: {path}')
    finally:
        os.close(fd)

@contextmanager
def new_output(path: Path):
    # EXCL also rejects an existing regular file. Candidate-controlled paths
    # can never cause a host truncation through a symlink or a hardlink.
    fd = _open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, 'wb') as stream:
        yield stream

def write_new(path: Path, data: bytes) -> None:
    with new_output(path) as stream:
        stream.write(data)

def freeze_regular(source: Path, target: Path) -> None:
    with regular_input(source) as incoming, new_output(target) as outgoing:
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)

def check_candidate_tree(root: Path) -> None:
    """No alias, device or pipe from elaborator output may reach host IO."""
    remaining = 1024
    def visit(path: Path, depth: int):
        nonlocal remaining
        if depth > 8:
            raise ValueError('Candidate artifact tree exceeds depth limit')
        for entry in os.scandir(path):
            remaining -= 1
            if remaining < 0:
                raise ValueError('Candidate artifact tree exceeds entry limit')
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                visit(Path(entry.path),depth+1)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe candidate artifact alias or special file: '+entry.path)
    visit(root,0)

def digest(p: Path) -> str:
    h = hashlib.sha256()
    with regular_input(p) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def rat(value) -> Fraction:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError('Exact rationals must be integer or string, never float')
    return Fraction(value)

def lean_rat(value) -> str:
    v = rat(value)
    return f'({v.numerator} / {v.denominator} : ℚ)'

def validate_task(data: dict) -> dict:
    d, q = data.get('dimension'), data.get('qubits_per_cell')
    if type(d) is not int or not 1 <= d <= 3 or type(q) is not int or not 1 <= q <= 64:
        raise ValueError('Task dimension must be 1..3 and qubits_per_cell 1..64')
    if data.get('energy_normalization', 'per_qubit') != 'per_qubit':
        raise ValueError('Task must request per_qubit normalization')
    terms = data.get('terms')
    if not isinstance(terms, list) or not 1 <= len(terms) <= 10000:
        raise ValueError('Task needs 1..10000 finite Pauli terms')
    normalized = []
    for term in terms:
        coeff = str(rat(term['coefficient']))
        word = term['word']
        if not isinstance(word, list):
            raise ValueError('word must be a finite list')
        factors, seen = [], set()
        for factor in word:
            cell, internal, pauli = factor['cell'], factor['qubit'], factor['pauli']
            if not isinstance(cell, list) or len(cell) != d or any(type(x) is not int for x in cell):
                raise ValueError('A site needs exactly dimension integer coordinates')
            if type(internal) is not int or not 0 <= internal < q or pauli not in ('X','Y','Z'):
                raise ValueError('Invalid internal qubit or Pauli label')
            key = (tuple(cell), internal)
            if key in seen:
                raise ValueError('Repeated site in one Hermitian Pauli word')
            seen.add(key)
            factors.append({'cell': cell, 'qubit': internal, 'pauli': pauli})
        factors.sort(key=lambda x: (x['cell'], x['qubit']))
        normalized.append({'coefficient': coeff, 'word': factors})
    # Other metadata is useful provenance, but only these physical data determine
    # the trusted target. A supplied digest is deliberately not trusted.
    return {'dimension': d, 'qubits_per_cell': q,
            'energy_normalization': 'per_qubit', 'terms': normalized}

def render_task(data: dict) -> str:
    d, q = data['dimension'], data['qubits_per_cell']
    sites = sorted({(tuple(x['cell']),x['qubit']) for t in data['terms'] for x in t['word']})
    # Identity-only observables still use an injective, harmless one-site region.
    if not sites:
        sites = [((0,) * d, 0)]
    def vec(xs): return '![' + ', '.join(str(x) for x in xs) + ']'
    placement = '![' + ', '.join(f'({vec(c)}, {i})' for c,i in sites) + ']'
    coeff = '![' + ', '.join(lean_rat(t['coefficient']) for t in data['terms']) + ']'
    words = []
    labels = {'X':1,'Y':2,'Z':3}
    for term in data['terms']:
        factors = {(tuple(x['cell']),x['qubit']): labels[x['pauli']] for x in term['word']}
        words.append(vec([factors.get(s,0) for s in sites]))
    return f'''module
public import QuantumManyBodyVariational.TrustedStateSpecV2
@[expose] public section
noncomputable section
namespace TrustedTask
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpecV2
set_option maxRecDepth 100000
set_option maxHeartbeats 2000000

def placement : Fin {len(sites)} ↪ Site {d} {q} where
  toFun := {placement}
  inj' := by decide

def task : LocalEnergy {d} {q} {len(sites)} {len(words)} where
  q_pos := by norm_num
  placement := placement
  coeff := {coeff}
  word := ![{', '.join(words)}]
end TrustedTask
'''

def baseline_source(task: dict, upper: Fraction, kind='tracial') -> str:
    d, q = task['dimension'], task['qubits_per_cell']
    return f'''module
public import QuantumManyBodyVariational.TrustedSpecExamplesV2
public import TrustedTask
@[expose] public section
noncomputable section
namespace Submission
open QuantumManyBodyVariational.InfiniteState
open TrustedStateSpecV2 TrustedSpecExamplesV2

def f : Correlator (Site {d} {q}) := {kind}
def bound : ℚ := {lean_rat(str(upper))}
theorem accepted : CandidateAccepted TrustedTask.task bound f := by
  constructor
  · exact {kind}_stateCondition {d} {q}
  · norm_num [LocalEnergy.value, LocalEnergy.cellValue, TrustedTask.task,
      Fin.sum_univ_succ, {kind}_place, funext_iff, Fin.forall_fin_succ, f, bound]
end Submission
'''

def _seccomp_library():
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.seccomp_export_bpf.restype = ctypes.c_int
    return library


def _deny_affinity_changes():
    """Persist across bwrap, exec, threads and descendants."""
    library = _seccomp_library()
    context = library.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError('cannot allocate CPU-affinity syscall filter')
    try:
        number = library.seccomp_syscall_resolve_name(b'sched_setaffinity')
        if number < 0 or library.seccomp_rule_add(context, 0x50000 | 1, number, 0):
            raise RuntimeError('cannot prohibit CPU-affinity expansion')
        if library.seccomp_load(context):
            raise RuntimeError('cannot load CPU-affinity syscall filter')
    finally:
        library.seccomp_release(context)


def _sandbox_process_filter(path):
    """After bwrap setup, allow Lean threads but prohibit forked processes.

    Returning ENOSYS for clone3 permits libc's legacy clone thread fallback.
    The clone flags must include CLONE_THREAD. No proof axiom is introduced.
    """
    class ArgCompare(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                    ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]
    library = _seccomp_library()
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_int, ctypes.c_uint,
                                               ctypes.POINTER(ArgCompare)]
    library.seccomp_rule_add_array.restype = ctypes.c_int
    context = library.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError('cannot allocate candidate process filter')
    try:
        for name in ('fork', 'vfork', 'clone3'):
            number = library.seccomp_syscall_resolve_name(name.encode())
            denied_errno = 38 if name == 'clone3' else 1
            if number >= 0 and library.seccomp_rule_add(context, 0x50000 | denied_errno, number, 0):
                raise RuntimeError('cannot restrict candidate process creation')
        number = library.seccomp_syscall_resolve_name(b'clone')
        comparison = ArgCompare(0, 7, 0x10000, 0)
        if number >= 0 and library.seccomp_rule_add_array(context, 0x50000 | 1, number, 1,
                                                          ctypes.byref(comparison)):
            raise RuntimeError('cannot restrict clone to threads')
        with new_output(path) as output:
            if library.seccomp_export_bpf(context, output.fileno()):
                raise RuntimeError('cannot export candidate process filter')
    finally:
        library.seccomp_release(context)


def _limits():
    os.setsid()
    # Terminate a worker even if the orchestrator itself is killed before it
    # can clean up. bubblewrap's own --die-with-parent covers its descendants.
    ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
    # All concurrent checkers share the same local two CPUs. This is separate
    # from the authorized eight aggregate numerical CPUs on server 211.
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    _deny_affinity_changes()
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    resource.setrlimit(resource.RLIMIT_AS, (12 * 1024**3, 12 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser._negative_number_matcher = re.compile(r'^-\d+(?:/\d+)?$')
    parser.add_argument('--task-json', type=Path, required=True)
    parser.add_argument('--upper', required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--candidate-source', type=Path)
    choice.add_argument('--solution-export', type=Path)
    choice.add_argument('--baseline', choices=['tracial','plus'], help='Trusted smoke-test source only')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    evidence = PROJECT / 'tmp' / f'state-spec-v2-check-{stamp}'
    trusted, candidate = evidence/'trusted', evidence/'candidate'
    trusted.mkdir(parents=True)
    candidate.mkdir()
    export_scratch = evidence/'export-scratch'
    export_scratch.mkdir()
    export_output = evidence/'export-output'
    export_output.mkdir()
    report = {'spec':'infinite-state-spec/v2','created_utc':stamp,
              'checks':{},'problems':[],'passed':False,
              'resource_policy': {'local_cpu_affinity': sorted(os.sched_getaffinity(0))[:2],
                                  'aggregate_local_cpu_limit': 2,
                                  'candidate_process_creation': 'threads_only',
                                  'address_space_bytes': 12 * 1024**3}}
    process_filter = trusted/'candidate-process-filter.bpf'
    _sandbox_process_filter(process_filter)
    process_filter_fd = os.open(process_filter, os.O_RDONLY | os.O_NOFOLLOW)
    env = {'PATH':f'{BIN}:/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8',
           'LEAN_SYSROOT':str(RUNTIME),'MATHLIB_NO_CACHE_ON_UPDATE':'1',
           'GIT_TERMINAL_PROMPT':'0','LEAN_NUM_THREADS':'2'}

    def run(label, cmd, *, cwd=LEAN, export=None, process_env=None, timeout=None):
        actual_env = process_env or env
        log = evidence/f'{label}.log'
        with ExitStack() as files:
            out = files.enter_context(new_output(export or log))
            errout = files.enter_context(new_output(log)) if export else subprocess.STDOUT
            # bwrap consumes the shared descriptor. Each sandbox must read the
            # full filter from offset zero, including after a trusted compile.
            os.lseek(process_filter_fd, 0, os.SEEK_SET)
            p = subprocess.Popen(cmd, cwd=cwd, env=actual_env, stdout=out,
                                 stderr=errout,
                                 pass_fds=(process_filter_fd,),
                                 preexec_fn=_limits)
            try:
                p.communicate(timeout=timeout or args.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.communicate()
                raise ValueError(f'{label}: deadline exceeded')
        item = {'exit_code':p.returncode,'log':str(log.relative_to(PROJECT)),
                'log_sha256':digest(log),'command':cmd}
        if export:
            item['export_sha256'] = digest(export)
        report['checks'][label] = item
        if p.returncode:
            raise ValueError(f'{label}: exit {p.returncode}; see {log}')
        return log.read_text(errors='replace')

    def check_lock(stage):
        lock = json.loads(LOCK.read_text())
        failures = []
        for name,expected in lock['source_sha256'].items():
            path = (PROJECT/name).resolve()
            if not path.is_relative_to(PROJECT) or not path.is_file() or digest(path)!=expected:
                failures.append(name)
        for name,expected in lock['toolchain_binary_sha256'].items():
            if digest(BIN/name)!=expected:
                failures.append('toolchain/'+name)
        if digest(V1LOCK) != lock['v1_lock_sha256']:
            failures.append('v1 lock')
        if digest(LOCK) != report['lock_sha256']:
            failures.append('v2 lock changed during verification')
        report['checks'][stage] = {'passed':not failures,'mismatches':failures}
        if failures:
            raise ValueError('Frozen specification mismatch: '+', '.join(failures))
        return lock

    try:
        upper = rat(args.upper)
        report['upper'] = str(upper)
        report['lock_sha256'] = digest(LOCK)
        lock = check_lock('lock-before')
        original_task = json.loads(args.task_json.read_text())
        data = validate_task(original_task)
        canonical = json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n'
        write_new(trusted/'task.json',canonical.encode())
        report['physical_task_sha256'] = digest(trusted/'task.json')
        task_canonical = json.dumps({k:v for k,v in original_task.items() if k!='canonical_sha256'},
                                    sort_keys=True,separators=(',',':'),ensure_ascii=False)
        report['task_sha256'] = hashlib.sha256(task_canonical.encode()).hexdigest()
        write_new(trusted/'original-task.json',(task_canonical+'\n').encode())
        report['task_id'] = original_task.get('id')
        write_new(trusted/'TrustedTask.lean',render_task(data).encode())
        # The challenge's concrete bound is independently fixed. Only f's body
        # is a comparator hole; a candidate cannot claim a different numeric score.
        if args.candidate_source:
            source = args.candidate_source.absolute()
            with regular_input(source,2 * 1024**2) as incoming:
                original_source = incoming.read()
            content = original_source.decode()
            imports = re.findall(r'^\s*(?:public\s+)?import\s+([^\n]+)',content,re.M)
            names = [x for line in imports for x in line.split()]
            if not names or any(x not in ALLOWED_IMPORTS and not x.startswith('Mathlib.') for x in names):
                raise ValueError('Candidate imports must use the documented trusted API or Mathlib')
            write_new(candidate/'Candidate.lean',original_source)
        elif args.baseline:
            original_source = baseline_source(data,upper,args.baseline).encode()
            write_new(candidate/'Candidate.lean',original_source)
        else:
            submitted = args.solution_export.absolute()
            freeze_regular(submitted,trusted/'judge-input.ndjson')
        if not args.solution_export:
            write_new(trusted/'Submitted.lean',original_source)
            report['candidate_source_sha256'] = hashlib.sha256(original_source).hexdigest()
        revision = run('mathlib-revision',['/usr/bin/git','-C',str(LEAN/'.lake/packages/mathlib'),'rev-parse','HEAD']).strip()
        if revision != lock['mathlib_revision']:
            raise ValueError('mathlib revision differs from frozen revision')
        run('mathlib-clean',['/usr/bin/git','-C',str(LEAN/'.lake/packages/mathlib'),'diff','--exit-code','HEAD','--'])
        run('build',[str(BIN/'lake'),'build','QuantumManyBodyVariational.TrustedSpecRegressionV2'])
        audit = run('axioms',[str(BIN/'lake'),'env','lean','-j2','InfiniteStateAuditV2.lean'])
        axiom_lists = re.findall(r'depends on axioms:\s*\[([^\]]*)\]', audit)
        expected_count = (LEAN/'InfiniteStateAuditV2.lean').read_text().count('#print axioms')
        if len(axiom_lists) != expected_count:
            raise ValueError('Incomplete v2 axiom audit')
        for axiom_list in axiom_lists:
            if {s.strip() for s in axiom_list.split(',') if s.strip()} - set(lock['permitted_axioms']):
                raise ValueError('Forbidden axiom in trusted v2 library: '+axiom_list)
        lean_path = run('lean-path',[str(BIN/'lake'),'env','printenv','LEAN_PATH']).strip().split(':')
        lean_path = [str(Path(x).resolve(strict=True)) for x in lean_path if Path(x).exists()]
        # Import gating alone cannot hide old solved tasks: elaborator code can
        # inspect mounted files or load modules dynamically. Mount a snapshot
        # containing only generic semantic modules, never the old task library
        # or this version's regression examples.
        project_lib = (LEAN/'.lake/build/lib/lean').resolve()
        safe_lib = trusted/'safe-lib'
        safe_modules = safe_lib/'QuantumManyBodyVariational'
        safe_modules.mkdir(parents=True)
        for name in SAFE_PROJECT_MODULES:
            matches = list((project_lib/'QuantumManyBodyVariational').glob(name+'.*'))
            if not any(p.suffix == '.olean' for p in matches):
                raise ValueError('Missing generic compiled module: '+name)
            for module_file in matches:
                if module_file.is_file():
                    freeze_regular(module_file,safe_modules/module_file.name)
        lean_path = [str(safe_lib) if Path(x)==project_lib else x for x in lean_path]

        def sandbox(work: Path, writable: Path):
            cmd = ['/usr/bin/bwrap','--seccomp',str(process_filter_fd),
                   '--tmpfs','/','--proc','/proc','--dev','/dev',
                   '--tmpfs','/tmp','--unshare-net','--unshare-pid','--die-with-parent',
                   '--new-session']
            for path in ['/usr','/lib','/lib64',str(RUNTIME),*lean_path]:
                if Path(path).exists():
                    cmd += ['--ro-bind',path,path]
            cmd += ['--ro-bind',str(trusted),str(trusted)]
            cmd += ['--bind',str(writable),str(writable),'--chdir',str(work),'--']
            return cmd

        sandbox_env = dict(env, LEAN_PATH=':'.join([str(trusted),*lean_path,str(candidate)]))
        # Host-generated task and challenge are compiled before candidate code
        # and become read-only for the candidate's entire execution.
        # A separate trusted workspace keeps the challenge's bytes inaccessible
        # for writes even when arbitrary elaborator code runs in Candidate.lean.
        trusted_cmd = sandbox(trusted,trusted)
        run('compile-task',trusted_cmd+[str(BIN/'lean'),'-j2','-M8192','TrustedTask.lean','-o','TrustedTask.olean'],cwd=trusted,process_env=sandbox_env)
        # A challenge is a theorem shape, not a demonstration that every upper
        # endpoint is attainable. The comparator requires theorem-kind matching;
        # its reference proof placeholder is never a candidate proof and
        # sorryAx remains forbidden in the independently replayed candidate.
        challenge = baseline_source(data,upper)
        begin = challenge.index('theorem accepted :')
        challenge = challenge[:begin] + 'theorem accepted : CandidateAccepted TrustedTask.task bound f := by sorry\nend Submission\n'
        write_new(trusted/'Challenge.lean',challenge.encode())
        run('compile-challenge',trusted_cmd+[str(BIN/'lean'),'-j2','-M8192','Challenge.lean','-o','Challenge.olean'],cwd=trusted,process_env=sandbox_env)
        run('export-challenge',trusted_cmd+[str(BIN/'leanexport'),'Challenge','--',*PRIMITIVES],cwd=trusted,process_env=sandbox_env,export=trusted/'Challenge.ndjson')
        if not args.solution_export:
            candidate_cmd = sandbox(candidate,candidate)
            run('compile-candidate',candidate_cmd+[str(BIN/'lean'),'-j2','-M8192','Candidate.lean','-o','Candidate.olean'],cwd=candidate,process_env=sandbox_env)
            # The compiler may execute arbitrary elaborator code. Inspect its
            # artifacts before any host read/copy and freeze source provenance.
            check_candidate_tree(candidate)
            with regular_input(candidate/'Candidate.lean',2 * 1024**2) as source_file:
                if source_file.read() != original_source:
                    raise ValueError('Candidate changed its submitted source during compilation')
            if (candidate/'Candidate.ndjson').exists():
                raise ValueError('Candidate precreated the reserved export output')
            frozen_module = trusted/'candidate-module'
            frozen_module.mkdir()
            module_files = ['Candidate.olean','Candidate.olean.private','Candidate.olean.server',
                            'Candidate.ir','Candidate.ir.sig']
            for name in module_files:
                artifact = candidate/name
                if name == 'Candidate.olean' or artifact.exists():
                    freeze_regular(artifact,frozen_module/name)
            # Native import initializers are untrusted too. They see only the
            # read-only module snapshot and a fresh scratch directory. The
            # stdout destination is an exclusively created host descriptor,
            # never a path in the candidate's writable namespace.
            export_env = dict(env, LEAN_PATH=':'.join([str(trusted),*lean_path,str(frozen_module)]))
            raw_export = export_output/'Candidate.ndjson'
            run('export-candidate',sandbox(export_scratch,export_scratch)+
                [str(BIN/'leanexport'),'Candidate','--',*PRIMITIVES],
                cwd=export_scratch,process_env=export_env,export=raw_export)
            check_candidate_tree(export_scratch)
            freeze_regular(raw_export,trusted/'judge-input.ndjson')
            # Preserve the historical evidence path, created safely after all
            # untrusted processes have exited and never used for adjudication.
            freeze_regular(trusted/'judge-input.ndjson',candidate/'Candidate.ndjson')
        report['candidate_export_sha256'] = digest(trusted/'judge-input.ndjson')
        config = {'challenge_module':'Challenge','solution_module':'Candidate',
                  'theorem_names':['Submission.accepted'],'definition_names':['Submission.f'],
                  'permitted_axioms':lock['permitted_axioms']}
        config_path = trusted/'comparator.json'
        write_new(config_path,(json.dumps(config,indent=2)+'\n').encode())
        cmd = ['/usr/bin/bwrap','--ro-bind','/','/','--bind',str(evidence),str(evidence),
               '--tmpfs','/tmp','--dev','/dev','--proc','/proc','--unshare-net',
               '--die-with-parent','--',str(BIN/'lake'),'comparator','--config',str(config_path),
               '--challenge-from-export',str(trusted/'Challenge.ndjson'),
               '--solution-from-export',str(trusted/'judge-input.ndjson')]
        out = run('judge',cmd,timeout=max(args.timeout,180))
        if 'Lean default kernel accepts the solution' not in out or 'Your solution is okay!' not in out:
            raise ValueError('Comparator exited without the required kernel acceptance diagnostics')
        check_lock('lock-after')
        report['passed'] = True
        report['certificate'] = {'kind':'lean-kernel-v2','task_sha256':report['task_sha256'],
                                 'upper':str(upper),'lock_sha256':report['lock_sha256'],
                                 'candidate_export_sha256':report['candidate_export_sha256']}
    except (ValueError,OSError,KeyError,TypeError,ZeroDivisionError,subprocess.SubprocessError) as e:
        report['problems'].append(str(e))
    path = evidence/'manifest.json'
    write_new(path,(json.dumps(report,indent=2,ensure_ascii=False)+'\n').encode())
    result = {k:report[k] for k in ('passed','problems','task_sha256','upper','certificate') if k in report}
    result['manifest'] = str(path)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    sys.exit(main())
