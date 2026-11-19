"""Real native session closing, with a local deterministic provider only."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from revisions.seekgnomes_v1_2.runtime import run_trial
from revisions.seekgnomes_v1_2.tests.test_budget_gateway import provider_fixture
from revisions.seekgnomes_v1_2.tests.test_native import _codex_fixture_sse

PROJECT=Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.environ.get('SEEKGNOMES_REAL_CODEX'),'Pinned Codex binary is required')
class RealClosingTests(unittest.TestCase):
    def test_normal_exit_without_definition_resumes_same_session_and_closes_once(self):
        evidence=Path(tempfile.mkdtemp(prefix='native-closing-fixture-',dir=PROJECT/'runs'))
        harness=evidence/'harness';(harness/'bin').mkdir(parents=True)
        binary=Path(os.environ['SEEKGNOMES_REAL_CODEX']).resolve(strict=True)
        for name in ('codex','codex-code-mode-host'):
            shutil.copy2(binary.with_name(name),harness/'bin'/name)
        requests=[]
        def plan(handler):
            request=seen[-1]['payload'];requests.append(request)
            tool=None
            if len(requests)==2:
                tool={'type':'custom','name':'exec','namespace':'functions',
                    'input':'const entry=ALL_TOOLS.find(x=>x.name.includes("benchmark_action")); '
                    'text(await tools[entry.name]({tool:"finish",arguments:{reason:"offline closing fixture"}}));'}
            raw=_codex_fixture_sse('gpt-6.1-terra',len(requests),tool=tool)
            handler.send_response(200);handler.send_header('Content-Type','text/event-stream')
            handler.send_header('Content-Length',str(len(raw)));handler.end_headers();handler.wfile.write(raw)
        with provider_fixture([plan]) as (endpoint,seen),patch.dict(os.environ,{
                'SEEKGNOMES_HARNESS_ROOT':str(harness)}):
            result=run_trial(task_id=25,out=evidence/'trial',model='gpt-6.1-terra',
                endpoint=endpoint,api_key='offline-host-fixture-only',budget_multiplier=1,
                web=False,constructor_output_tokens=100,constructor_active_seconds=60,cpus=2)
        ledger=json.loads((evidence/'trial/constructor/budget-ledger.json').read_text())
        events=[json.loads(line) for line in (evidence/'trial/constructor/native.stdout.log').read_text().splitlines()
            if line.startswith('{')]
        ids=[x['thread_id'] for x in events if x.get('type')=='thread.started']
        outcome={'result':result,'ledger':ledger,'native_thread_ids':ids,
            'requests':requests,'external_api_calls':0,'scientific_trial':False}
        (evidence/'fixture-result.json').write_text(json.dumps(outcome,indent=2))
        self.assertEqual(result['status'],'final_submission_not_verified',str(evidence))
        self.assertFalse(result['definition_submitted'])
        self.assertTrue(result['closing_phase_used'])
        self.assertEqual(result['failure_reason'],'no_complete_definition_after_final_submission_phase')
        self.assertEqual(ledger['phase'],'closing')
        self.assertEqual(ledger['limits']['output']-ledger['closing_start_output_tokens'],50000)
        self.assertEqual(len([x for x in ledger['entries'] if x['kind']=='closing_phase_started']),1)
        self.assertEqual(len(ids),2,str(evidence))
        self.assertEqual(ids[0],ids[1])
        self.assertGreaterEqual(len(requests),2)
        self.assertIn('FINAL SUBMISSION PHASE',requests[1]['instructions'])
        self.assertGreaterEqual(ledger['output_tokens_charged'],14)


if __name__=='__main__':unittest.main()
