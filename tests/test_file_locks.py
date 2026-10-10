"""Real subprocess lifetime tests, using the actual workspace filesystem in WSL."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hermes-source-units/src"
sys.path.insert(0, str(SOURCE))
from hermes_source_units.file_locks import exclusive_lock, process_exists
from hermes_source_units import ContractError


class KernelLockTest(unittest.TestCase):
    def setUp(self):
        # WSL tests exercise /mnt/c, not just a Linux temporary filesystem.
        parent = ROOT.parent / "tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kernel-lock-test-", dir=parent)
        self.root = Path(self.temp.name)
        self.lock = self.root / "workflow.lock"
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.temp.cleanup()

    def helper(self, hold=False):
        code = ("from pathlib import Path; import time; "
                "from hermes_source_units.file_locks import exclusive_lock; "
                "from hermes_source_units import ContractError\n"
                "try:\n with exclusive_lock(Path(" + repr(str(self.lock)) + ")):\n"
                "  Path(" + repr(str(self.root / "ready")) + ").write_text('ready')\n"
                + ("  time.sleep(60)\n" if hold else "  pass\n") +
                "except ContractError as e:\n print(e.code); raise SystemExit(3)\n")
        process = subprocess.Popen([sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(SOURCE)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def await_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (self.root / "ready").exists():
                return
            time.sleep(.03)
        self.fail("lock holder failed to start")

    def test_kill_releases_kernel_lock_and_recovers_owner_marker(self):
        holder = self.helper(hold=True)
        self.await_ready()
        contender = self.helper()
        output, error = contender.communicate(timeout=10)
        self.assertEqual(contender.returncode, 3, error)
        self.assertIn(b"LOCK_BUSY", output)
        record = json.loads(self.lock.read_text())
        self.assertEqual(record["pid"], holder.pid)
        holder.kill()
        holder.wait()
        self.assertTrue(self.lock.exists())
        replacement = self.helper()
        output, error = replacement.communicate(timeout=10)
        self.assertEqual(replacement.returncode, 0, error)
        self.assertFalse(self.lock.exists())

    def test_termination_releases_kernel_lock(self):
        holder = self.helper(hold=True)
        self.await_ready()
        holder.terminate()
        holder.wait()
        with exclusive_lock(self.lock):
            self.assertEqual(json.loads(self.lock.read_text())["pid"], os.getpid())
        self.assertFalse(self.lock.exists())

    def test_dead_legacy_pid_requires_supported_recovery(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(process_exists(child.pid))
        data = str(child.pid)
        self.lock.write_text(data)
        with self.assertRaisesRegex(ContractError, "LOCK_RECOVERY_REQUIRED"):
            with exclusive_lock(self.lock):
                self.fail("legacy marker was silently bypassed")
        self.assertEqual(self.lock.read_text(), data)

    def test_bounded_wait_acquires_after_owner_exits(self):
        holder = self.helper(hold=True)
        self.await_ready()
        release = threading.Timer(.2, holder.terminate)
        release.start()
        try:
            with exclusive_lock(self.lock, timeout=2):
                self.assertEqual(json.loads(self.lock.read_text())["pid"], os.getpid())
        finally:
            release.cancel()
            release.join()
        holder.wait()

    def test_bounded_wait_expires_without_removing_owner(self):
        holder = self.helper(hold=True)
        self.await_ready()
        before = self.lock.read_bytes()
        with self.assertRaisesRegex(ContractError, "LOCK_TIMEOUT"):
            with exclusive_lock(self.lock, timeout=.15):
                self.fail("waiter bypassed live owner")
        self.assertEqual(self.lock.read_bytes(), before)
        root = (Path.home()/'.cache/hermes-skill-runtime/lock-diagnostics' if os.name=='posix'
                else Path(tempfile.gettempdir())/'hermes-skill-lock-diagnostics')
        events=[json.loads(line) for line in (root/f'{os.getpid()}.jsonl').read_text().splitlines()]
        failure=next(e for e in reversed(events) if e['event']=='wait_failed' and e['path']==str(self.lock.resolve()))
        self.assertEqual(failure['holder']['pid'],holder.pid)
        self.assertGreaterEqual(failure['wait_ms'],100)
        self.assertIn('operation',failure['holder'])


if __name__ == "__main__":
    unittest.main()
