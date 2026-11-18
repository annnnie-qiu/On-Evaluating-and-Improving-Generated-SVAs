import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class ProcessWatchTests(unittest.TestCase):
    def test_ctrl_c_terminates_jasper_group_for_both_entry_points(self):
        for implementation in ['engine', 'native']:
            with self.subTest(implementation=implementation), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                child = root / 'child.py'
                child.write_text('''import json,os,pathlib,signal,subprocess,sys,time
descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(signum, frame):
    descendant.wait(timeout=3)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
pathlib.Path('pids.json').write_text(json.dumps([os.getpid(), descendant.pid]))
while True: time.sleep(0.1)
''')
                module = Path(__file__).parent / implementation
                command = shlex.join([sys.executable, str(child)])
                worker = subprocess.Popen([sys.executable, '-c',
                    'import sys; from pathlib import Path; '
                    f'sys.path.insert(0, {str(module)!r}); '
                    'from process_watch import run_logged; '
                    f'run_logged({command!r}, cwd=Path({str(root)!r}), '
                    f'log_path=Path({str(root / "run.log")!r}), timeout_seconds=60, label="test")'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                pids = []
                try:
                    deadline = time.monotonic() + 5
                    while not (root / 'pids.json').exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((root / 'pids.json').exists())
                    pids = json.loads((root / 'pids.json').read_text())
                    worker.send_signal(signal.SIGINT)
                    out, err = worker.communicate(timeout=8)
                    self.assertNotEqual(worker.returncode, 0)
                    status = json.loads((root / 'status.json').read_text())
                    self.assertEqual(status['state'], 'INTERRUPTED', out + err)
                    for pid in pids:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.communicate()
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == '__main__':
    unittest.main()
