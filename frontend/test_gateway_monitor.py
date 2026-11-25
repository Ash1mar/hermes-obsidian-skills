"""Focused checks for console Gateway lifecycle requests."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gateway_monitor import GatewayMonitor
from workflow import UserError


class GatewayMonitorTests(unittest.TestCase):
    def test_stop_latches_before_poll_and_does_not_admit_a_second_action(self):
        monitor = GatewayMonitor(".", True, autostart=False)
        monitor.report("ready", pid=123, ready=True, can_stop=True)

        status = monitor.request("stop")
        monitor.report("ready", pid=123, ready=True, can_stop=True)

        self.assertEqual(status["state"], "draining")
        self.assertIs(monitor.status, status)
        self.assertFalse(status["ready"])
        with self.assertRaises(UserError):
            monitor.request("stop")
        self.assertEqual(monitor.take_request(), "stop")

    def test_observation_advertises_start_and_reused_stop_for_this_profile_only(self):
        monitor = GatewayMonitor(".", True, autostart=False)
        load_config, stale = lambda: {}, lambda _: False
        stopped = SimpleNamespace(running=False, pid=None, source="none", probe_error=False)
        monitor.report_observation({"pid": 123, "gateway_state": "running"}, stopped, load_config, stale)
        self.assertEqual(monitor.status["state"], "stopped")
        self.assertTrue(monitor.status["can_start"])

        record = {"pid": 123, "gateway_state": "running"}
        live = SimpleNamespace(running=True, pid=123, source="pid", probe_error=False)
        monitor.report_observation(record, live, load_config, stale)
        self.assertEqual(monitor.status["ownership"], "reused")
        self.assertTrue(monitor.status["can_stop"])
        live.source = "multiplexer"
        monitor.report_observation(record, live, load_config, stale)
        self.assertFalse(monitor.status["can_stop"])

    def test_read_only_controls_are_unavailable(self):
        monitor = GatewayMonitor(".", False)
        for action in ("start", "stop"):
            with self.subTest(action=action), self.assertRaises(UserError):
                monitor.request(action)

    @unittest.skipUnless(os.name == "posix", "profile lifecycle uses a native flock")
    def test_start_rechecks_profile_after_acquiring_lock_and_reuses_live_process(self):
        with TemporaryDirectory() as directory:
            monitor = GatewayMonitor(directory, True)
            live = SimpleNamespace(running=True, probe_error=False)
            observe = Mock(return_value=({}, live))
            with patch.object(monitor, "invoke") as invoke:
                monitor.run_operation(Path(directory), "start", observe)
            observe.assert_called_once()
            invoke.assert_not_called()

    def test_stop_invokes_native_cli_and_preserves_refusal(self):
        with TemporaryDirectory() as directory:
            home = Path(directory) / "profile"
            monitor = GatewayMonitor(directory, True)
            with patch("gateway_monitor.shutil.which", return_value="/verified/hermes"), patch(
                    "gateway_monitor.subprocess.run", return_value=subprocess.CompletedProcess([], 3)) as run:
                with self.assertRaisesRegex(UserError, "退出码 3"):
                    monitor.invoke(home, "stop")
            self.assertEqual(run.call_args.args[0], ["/verified/hermes", "gateway", "stop"])
            self.assertEqual(run.call_args.kwargs["env"]["HERMES_HOME"], str(home))

    def test_stop_waits_for_confirmed_exit_after_native_command_returns(self):
        monitor = GatewayMonitor(".", True)
        record = {"pid": 123, "gateway_state": "running"}
        still_running = SimpleNamespace(running=True, pid=123, probe_error=False)
        unknown = SimpleNamespace(running=False, pid=None, probe_error=True)
        stopped = SimpleNamespace(running=False, pid=None, probe_error=False)
        observe = Mock(side_effect=((record, still_running), ({}, unknown), ({}, stopped)))
        with patch("gateway_monitor.POLL_INTERVAL", 0):
            monitor.wait_for("stop", observe, time.monotonic() + 1)
        self.assertEqual(observe.call_count, 3)
