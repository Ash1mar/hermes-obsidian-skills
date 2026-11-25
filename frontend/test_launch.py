"""Select the committed Hermes environment before starting the console."""

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import launch


class ConsoleLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "Hermes installation"
        self.root.mkdir()
        self.venv = self.base / "selected generation" / "venv"
        self.python = self.executable(self.venv / "bin/python3")
        self.selected = Mock(return_value=self.venv)
        package = ModuleType("pm")
        environments = ModuleType("pm.environments")
        environments.committed_venv = self.selected
        package.environments = environments
        self.enterContext(patch.dict(sys.modules, {"pm": package, "pm.environments": environments}))
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.execve = self.enterContext(patch.object(launch.os, "execve"))

    def executable(self, path):
        path.parent.mkdir(parents=True)
        path.touch()
        path.chmod(0o700)
        return path

    def run_launcher(self, *console_arguments):
        argv = ["launch.py", "--hermes-root", str(self.root), *console_arguments]
        with patch.object(sys, "argv", argv):
            launch.main()

    def test_uses_selected_python_and_preserves_console_args_and_hermes_home(self):
        alternate_home = str(self.base / "alternate profile")
        with patch.dict(os.environ, {"HERMES_HOME": alternate_home, "PYTHONHOME": "legacy-home",
                                     "PYTHONPATH": "legacy-imports", "VIRTUAL_ENV": "legacy-venv"}):
            self.run_launcher("--", "--port", "18481", "--history", str(self.base / "saved run"))

        self.selected.assert_called_once_with(self.root)
        path, argv, environment = self.execve.call_args.args
        self.assertEqual(path, str(self.python))
        self.assertEqual(argv, [str(self.python), "-B", str(Path(launch.__file__).with_name("server.py").resolve()),
                                "--port", "18481", "--history", str(self.base / "saved run")])
        self.assertEqual(environment["HERMES_HOME"], alternate_home)
        self.assertTrue({"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}.isdisjoint(environment))

    def test_missing_committed_generation_does_not_fall_back_to_checkout_venv(self):
        self.executable(self.root / "venv/bin/python3")
        self.selected.return_value = None
        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            self.run_launcher()

        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("没有可用的托管 Python", errors.getvalue())
        self.execve.assert_not_called()

    def test_broken_committed_generation_reports_error_without_starting_server(self):
        self.selected.side_effect = RuntimeError("dependency environment is missing")
        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            self.run_launcher()

        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("dependency environment is missing", errors.getvalue())
        self.execve.assert_not_called()
