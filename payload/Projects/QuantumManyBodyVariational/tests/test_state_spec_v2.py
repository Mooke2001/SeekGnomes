"""V2 task parser checks and optional kernel/sandbox rejection regressions.
Run with Environment/Python/run.sh .../tests/test_state_spec_v2.py --integration.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('state_spec_v2',PROJECT/'scripts/check_state_spec_v2.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

class TaskTests(unittest.TestCase):
    def setUp(self):
        self.task = json.loads((PROJECT/'tests/state_spec_v2_task.json').read_text())
    def test_two_internal_qubits_survive(self):
        normalized = api.validate_task(self.task)
        source = api.render_task(normalized)
        self.assertIn('Site 1 2',source)
        self.assertIn('(![0], 0), (![0], 1)',source)
        self.assertIn('LocalEnergy 1 2 2 3',source)
    def test_identity_is_retained(self):
        self.task['terms']=[{'coefficient':'3/2','word':[]}]
        source=api.render_task(api.validate_task(self.task))
        self.assertIn('(3 / 2 : ℚ)',source)
        self.assertIn('word := ![![0]]',source)
    def test_float_coefficient_rejected(self):
        self.task['terms'][0]['coefficient']=0.25
        with self.assertRaises(ValueError):api.validate_task(self.task)
    def test_bad_internal_index_rejected(self):
        self.task['terms'][0]['word'][0]['qubit']=2
        with self.assertRaises(ValueError):api.validate_task(self.task)
    def test_duplicate_pauli_site_rejected(self):
        self.task['terms'][0]['word']*=2
        with self.assertRaises(ValueError):api.validate_task(self.task)
    def test_zero_qubits_rejected(self):
        self.task['qubits_per_cell']=0
        with self.assertRaises(ValueError):api.validate_task(self.task)

class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'tmp',prefix='v2-artifact-test-')
        self.root=Path(self.temp.name)
        self.honey=self.root/'honeypot';self.honey.write_bytes(b'unchanged')
    def tearDown(self):self.temp.cleanup()
    def test_output_never_truncates_existing(self):
        with self.assertRaises(FileExistsError):api.write_new(self.honey,b'bad')
        self.assertEqual(self.honey.read_bytes(),b'unchanged')
    def test_output_symlink_rejected(self):
        link=self.root/'alias';link.symlink_to(self.honey)
        with self.assertRaises(OSError):api.write_new(link,b'bad')
        self.assertEqual(self.honey.read_bytes(),b'unchanged')
    def test_input_symlink_rejected(self):
        link=self.root/'alias';link.symlink_to(self.honey)
        with self.assertRaises(OSError):api.digest(link)
    def test_input_hardlink_rejected(self):
        link=self.root/'alias';os.link(self.honey,link)
        with self.assertRaises(ValueError):api.digest(link)
        self.assertEqual(self.honey.read_bytes(),b'unchanged')
    def test_input_fifo_rejected_without_blocking(self):
        pipe=self.root/'pipe';os.mkfifo(pipe)
        with self.assertRaises(ValueError):api.digest(pipe)
    def test_parent_directory_symlink_rejected(self):
        (self.root/'aliasdir').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(OSError):api.digest(self.root/'aliasdir'/'honeypot')


def integration():
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    out=PROJECT/'tmp'/f'state-spec-v2-regression-{stamp}'
    out.mkdir(parents=True)
    task=json.loads((PROJECT/'tests/state_spec_v2_task.json').read_text())
    path=out/'task.json';path.write_text(json.dumps(task)+'\n')
    baseline=api.baseline_source(task,api.rat('-1'),'plus')
    theorem=baseline.index('theorem accepted :')
    head=baseline[:theorem]
    target='CandidateAccepted TrustedTask.task bound f'
    sources={
        'plus':baseline,
        'wrong_goal':head+'theorem accepted : True := by trivial\nend Submission\n',
        'extra_assumption':head+f'theorem accepted : False → {target} := by intro h; exact False.elim h\nend Submission\n',
        'forbidden_axiom':head+f'axiom fake : False\ntheorem accepted : {target} := False.elim fake\nend Submission\n',
        'sorry':head+f'theorem accepted : {target} := by sorry\nend Submission\n',
        'wrong_upper':api.baseline_source(task,api.rat('0'),'tracial'),
    }
    sentinel=out/'hidden-sentinel.txt';sentinel.write_text('private-attempt-fixture\n')
    # The source can see no parent project output; the sentinel is outside all
    # candidate mounts. No actual secret is touched by this regression.
    sources['read_outside']=baseline.replace('namespace Submission',
        f'#eval do\n  let _ ← IO.FS.readFile "{sentinel}"\n  pure ()\nnamespace Submission')
    sources['write_trusted']=baseline.replace('namespace Submission',
        '#eval do\n  IO.FS.writeFile "../trusted/TrustedTask.lean" "tampered"\nnamespace Submission')
    honeypots={}
    for name,leaf in [('export_symlink','Candidate.ndjson'),('source_symlink','Candidate.lean')]:
        honey=out/f'{name}-honeypot.txt';honey.write_bytes(b'HOST_HONEYPOT_UNCHANGED\n')
        honeypots[name]=honey
        payload=(f'#eval do\n  let r ← IO.Process.output {{cmd := "/usr/bin/ln", '
                 f'args := #["-sf", "{honey}", "{leaf}"]}}\n'
                 '  if r.exitCode != 0 then throw (IO.userError r.stderr)\n')
        sources[name]=baseline.replace('namespace Submission',payload+'\nnamespace Submission')
    sources['source_mutation']=baseline.replace('namespace Submission',
        '#eval IO.FS.writeFile "Candidate.lean" "source changed after parsing"\nnamespace Submission')
    def invoke(name,source=None,solution_export=None,task_path=path):
        cmd=[sys.executable,str(PROJECT/'scripts/check_state_spec_v2.py'),
             '--task-json',str(task_path),'--upper=-1','--timeout','120']
        if source is not None:
            lean=out/f'{name}.lean';lean.write_text(source)
            cmd+=['--candidate-source',str(lean)]
        else:cmd+=['--solution-export',str(solution_export)]
        p=subprocess.run(cmd,cwd=PROJECT.parents[1],text=True,stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,timeout=500)
        try:result=json.loads(p.stdout.strip().splitlines()[-1])
        except (IndexError,json.JSONDecodeError):raise RuntimeError(p.stdout)
        result['process_exit_code']=p.returncode
        return name,result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=dict(pool.map(lambda item:invoke(*item),sources.items()))
    changed=json.loads(json.dumps(task));changed['terms'][0]['coefficient']='-2'
    changedpath=out/'changed-task.json';changedpath.write_text(json.dumps(changed)+'\n')
    _,changed_result=invoke('changed_task_proof',baseline,task_path=changedpath)
    if not changed_result['passed']:raise RuntimeError(changed_result)
    export=Path(changed_result['manifest']).parent/'candidate/Candidate.ndjson'
    name,result=invoke('wrong_task',solution_export=export)
    results[name]=result
    expected={name:name=='plus' for name in results}
    checks={name:bool(result['passed'])==expected[name] for name,result in results.items()}
    # Rejections must be substantive comparator failures, not incidental compile
    # errors. The two filesystem fixtures intentionally fail inside compilation.
    reasons={
        'wrong_goal':'theorem statement do not match',
        'extra_assumption':'theorem statement do not match',
        'forbidden_axiom':"Illegal axiom detected: 'Submission.fake'",
        'sorry':"Illegal axiom detected: 'sorryAx'",
        'wrong_upper':'Const does not match between challenge and target',
        'wrong_task':'Const does not match between challenge and target',
        'read_outside':'No such file or directory',
        'write_trusted':'Read-only file system',
    }
    for name,fragment in reasons.items():
        folder=Path(results[name]['manifest']).parent
        log=folder/('compile-candidate.log' if name in ('read_outside','write_trusted') else 'judge.log')
        checks[name] &= log.exists() and fragment.lower() in log.read_text().lower()
    for name,honey in honeypots.items():
        checks[name] &= honey.read_bytes()==b'HOST_HONEYPOT_UNCHANGED\n'
        checks[name] &= any('Unsafe candidate artifact' in error for error in results[name]['problems'])
    checks['source_mutation'] &= any('changed its submitted source' in error for error in results['source_mutation']['problems'])
    report={'passed':all(checks.values()),'checks':checks,'results':results,
            'lock_sha256':api.digest(PROJECT/'STATE_SPEC_V2.lock.json'),
            'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    destination=out/'regression.json';destination.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'report':str(destination),'checks':checks}))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    if '--integration' in sys.argv:
        sys.exit(integration())
    unittest.main()
