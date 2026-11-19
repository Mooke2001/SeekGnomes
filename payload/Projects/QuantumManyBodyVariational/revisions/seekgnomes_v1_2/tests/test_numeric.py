"""Configured multi-CPU science workers keep the frozen isolation boundary."""
from pathlib import Path
import json
import os
import tempfile
import unittest

from revisions.seekgnomes_v1_2.numeric import LocalNumericalBroker

PROJECT=Path(__file__).resolve().parents[3]


class ConfiguredNumericTests(unittest.TestCase):
    @unittest.skipIf(len(os.sched_getaffinity(0))<2,'Two available CPUs required')
    def test_two_cpu_worker_and_all_thread_seccomp(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'tmp') as folder:
            root=Path(folder)
            source=root/'experiment.py'
            source.write_text('''import json,os,socket,subprocess,threading
import numpy as np
checks={"affinity":len(os.sched_getaffinity(0)),"blas_threads":os.environ["OPENBLAS_NUM_THREADS"],
"no_api_key":not os.environ.get("SEEKGNOMES_API_KEY"),
"dot":float(np.ones((20,20)).dot(np.ones((20,20)))[0,0])}
for name,action in [("network_denied",lambda:socket.socket()),
 ("process_denied",lambda:subprocess.run(["/bin/true"])),
 ("affinity_change_denied",lambda:os.sched_setaffinity(0,os.sched_getaffinity(0)))]:
 try:action()
 except OSError:checks[name]=True
 else:checks[name]=False
try:threading.Thread(target=lambda:None).start()
except (OSError,RuntimeError):checks["new_thread_denied"]=True
else:checks["new_thread_denied"]=False
print(json.dumps(checks))
''')
            result=LocalNumericalBroker(max_cpus=2)(candidate_path=source,
                invocation_dir=root/'job',cpus=2,wall_seconds=90)
            self.assertTrue(result['ok'],result)
            row=json.loads(result['stdout'].strip().splitlines()[-1])
            self.assertEqual(row['affinity'],2)
            self.assertEqual(row['blas_threads'],'2')
            self.assertEqual(row['dot'],20)
            for name in ('no_api_key','network_denied','process_denied',
                         'affinity_change_denied','new_thread_denied'):
                self.assertTrue(row[name],row)
            self.assertFalse(result['output_trusted'])
            self.assertFalse(result['infinite_state_verified'])
            self.assertEqual(result['cpu_seconds_charged'],180)

    def test_cpu_allocation_cannot_be_exceeded(self):
        broker=LocalNumericalBroker(max_cpus=1)
        with self.assertRaises(ValueError):
            broker(candidate_path=PROJECT/'does-not-exist.py',
                invocation_dir=PROJECT/'tmp/not-created',cpus=2)


if __name__=='__main__':unittest.main()
