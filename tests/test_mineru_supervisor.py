"""Linux subprocess tests for the Skill gate, cancellation and orphan recovery."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "hermes-obsidian-controlled-ingest/scripts/supervise_mineru.py"
spec = importlib.util.spec_from_file_location("mineru_supervisor", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


@unittest.skipUnless(sys.platform == "linux", "actual Linux process and flock semantics")
class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes = []
        self.config = json.loads((SCRIPT.parents[1] / "config/mineru-runtime.json").read_text())
        self.config.update(min_available_memory_mib=0, startup_reservation_mib=0,
                           min_free_gpu_memory_mib=0, timeout_seconds=5,
                           queue_timeout_seconds=5, terminate_grace_seconds=0.2,
                           poll_seconds=0.03)

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for record in (self.root / "state").glob("slot-*.json"):
            runtime.cleanup(json.loads(record.read_text()), 0.1)
        self.temp.cleanup()

    def launch(self, code, extra=(), caller=None):
        config = self.root / "config.json"
        config.write_text(json.dumps(self.config))
        caller = caller or os.getpid()
        env = {k: v for k, v in os.environ.items() if k not in (
            "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK")}
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--config", str(config),
            "--state-dir", str(self.root / "state"), "--caller-pid", str(caller),
            "--caller-start", runtime.identity(caller), *extra, "--", sys.executable, "-c", code],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.processes.append(process)
        return process

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail("timed out waiting for subprocess state")

    def test_two_slots_across_independent_supervisors(self):
        events = self.root / "events"
        code = ("import os,time; p=" + repr(str(events)) + "; "
                "f=os.open(p,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600); "
                "os.write(f,('start '+str(os.getpid())+'\\n').encode()); time.sleep(.5); "
                "os.write(f,('end '+str(os.getpid())+'\\n').encode()); os.close(f)")
        processes = [self.launch(code) for _ in range(3)]
        self.assertEqual([p.wait(timeout=8) for p in processes], [0, 0, 0])
        current = peak = 0
        for line in events.read_text().splitlines():
            current += 1 if line.startswith("start") else -1
            peak = max(peak, current)
        self.assertEqual(peak, 2)
        self.assertEqual(current, 0)
        self.assertFalse(list((self.root / "state").glob("slot-*.json")))

    def child_code(self):
        child = self.root / "child"
        code = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(60)'],start_new_session=True); "
                "open(" + repr(str(child)) + ",'w').write(str(p.pid)); time.sleep(60)")
        return child, code

    def test_timeout_cleans_detached_child(self):
        self.config["timeout_seconds"] = 0.4
        child, code = self.child_code()
        process = self.launch(code)
        self.until(child.exists)
        pid = int(child.read_text())
        self.assertNotEqual(process.wait(timeout=8), 0)
        self.assertIsNone(runtime.identity(pid))

    def test_normal_and_failed_exit_clean_detached_child(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                child, code = self.child_code()
                child.unlink(missing_ok=True)
                code = code.rsplit("time.sleep(60)", 1)[0] + f"sys.exit({exit_code})"
                process = self.launch(code)
                self.assertEqual(process.wait(timeout=8), exit_code)
                self.assertIsNone(runtime.identity(int(child.read_text())))

    def test_caller_death_cleans_detached_child(self):
        caller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.processes.append(caller)
        child, code = self.child_code()
        process = self.launch(code, caller=caller.pid)
        self.until(child.exists)
        caller.kill()
        caller.wait()
        self.assertNotEqual(process.wait(timeout=8), 0)
        self.assertIsNone(runtime.identity(int(child.read_text())))

    def test_killed_supervisor_is_reaped_before_new_start(self):
        child, code = self.child_code()
        process = self.launch(code)
        self.until(child.exists)
        process.kill()
        process.wait()
        self.assertIsNotNone(runtime.identity(int(child.read_text())))
        replacement = self.launch("pass")
        self.assertEqual(replacement.wait(timeout=8), 0)
        self.assertIsNone(runtime.identity(int(child.read_text())))

    def test_cancellation_and_missing_binding(self):
        ledger = self.root / "_system/ledgers/ingest-workflows"
        ledger.mkdir(parents=True)
        binding = {"workflow_id": "ingest-test", "task_id": "task-1", "node": "source-prepare:123"}
        request = self.root / "binding.json"
        request.write_text(json.dumps(binding))
        value = {"cancel_requested": False, "batch_id": None, "current_stage": "source_preparing",
                 "kanban": {"task_map": [binding]}}
        workflow = ledger / "ingest-test.json"
        workflow.write_text(json.dumps(value))
        child, code = self.child_code()
        process = self.launch(code, ["--vault", str(self.root), "--worker-binding", str(request)])
        self.until(child.exists)
        value["cancel_requested"] = True
        workflow.write_text(json.dumps(value))
        self.assertNotEqual(process.wait(timeout=8), 0)
        self.assertIsNone(runtime.identity(int(child.read_text())))
        old = os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT")
        os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
        try:
            with self.assertRaisesRegex(RuntimeError, "requires"):
                runtime.check_binding(None, None)
        finally:
            if old is None:
                os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT")
            else:
                os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = old

    def test_low_memory_queues_without_starting(self):
        self.config.update(min_available_memory_mib=10**12, queue_timeout_seconds=0.2)
        marker = self.root / "started"
        process = self.launch("open(" + repr(str(marker)) + ",'w').write('started')")
        self.assertNotEqual(process.wait(timeout=8), 0)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
