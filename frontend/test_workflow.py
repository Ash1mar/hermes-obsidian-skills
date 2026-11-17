"""Isolated console status and action regressions."""

from pathlib import Path
import tempfile
import unittest

from workflow import STAGE_ORDER, ledger_path, progress, save


class ConsoleWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        self.run = Path(temporary.name) / "console-test"
        self.root = self.run / "short"
        self.root.mkdir(parents=True)
        self.attempt = self.run / "attempts" / "a-first"
        self.attempt.mkdir(parents=True)
        self.request = {"operation_id": self.run.name, "vault_name": "Test", "vault_path": str(self.vault),
                        "sources": [str(self.vault.parent / f"{i}.pdf") for i in range(8)]}
        save(self.run / "request.json", self.request)

    def status(self, **options):
        snapshot = progress(self.run, **options)
        return snapshot, {node["key"]: node for node in snapshot["nodes"]}

    def operator(self, stage, state, pending=None):
        save(self.root / "operator-state.json", {"contract": "short-operator-state/v1", "attempt_id": "a-first",
             "operation_id": self.run.name, "node": stage, "state": state, "pending_pause": pending,
             "completed": list(STAGE_ORDER)[:STAGE_ORDER[stage]]})

    def test_legacy_observer_timeout_stays_separate_from_native_progress(self):
        save(self.attempt / "attempt.json", {"id": "a-first", "action": "start", "started_at": 1,
                                             "returncode": 1, "ended_at": 1201})
        (self.attempt / "pipeline.log").write_text("ValueError: native source handoff timed out\n", encoding="utf-8")
        for name in ("pipeline-start.json", "provisioning-request.json", "provisioning-receipt.json"):
            save(self.root / name, {})
        ledger = ledger_path(self.request)
        ledger.parent.mkdir(parents=True)
        outcomes = [{"path": f"10_Raw/{i}.pdf", "status": "failed" if i == 7 else "ready"} for i in range(8)]
        source = {"workflow_id": "ingest-" + self.run.name, "state": "source_preparing",
                  "current_stage": "source_preparing", "source_outcomes": outcomes[1:]}
        save(ledger, source)
        before, before_nodes = self.status()
        self.assertEqual(before["launcher_state"], "detached")
        self.assertEqual(before_nodes["prepare"]["state"], "pending")
        source.update(state="planning", current_stage="planning", source_outcomes=outcomes)
        save(ledger, source)
        after, after_nodes = self.status()
        self.assertEqual(after["source_wait"], before["source_wait"])
        self.assertEqual(after_nodes["prepare"]["state"], "partial")
        self.assertIn("7 份就绪 · 1 份失败 · 0 份未结算", after_nodes["prepare"]["detail"])
        self.assertEqual(after_nodes["exact"]["state"], "pending")
        self.assertEqual([option["id"] for option in after_nodes["exact"]["actions"]], ["attach-source"])
        self.assertFalse(any(node["actions"] for node in self.status(historical=True)[1].values()))

    def test_pause_target_and_continuation_stay_bound_to_attempt_and_node(self):
        save(self.attempt / "attempt.json", {"id": "a-first", "action": "start", "operator": True})
        self.operator("mapping", "running")
        _, nodes = self.status(active=True)
        self.assertFalse(nodes["mapping"]["actions"])
        self.assertEqual(nodes["writing"]["actions"][0]["attempt"], "a-first")
        save(self.root / "operator-pause.json", {"attempt_id": "a-prior", "node": "release"})
        self.assertEqual(self.status(active=True)[0]["state"], "running")
        save(self.root / "operator-pause.json", {"attempt_id": "a-first", "node": "writing"})
        status, nodes = self.status(active=True)
        self.assertEqual(status["state"], "pausing")
        self.assertTrue(status["launcher_active"])
        self.assertEqual(nodes["writing"]["state"], "pausing")
        self.assertFalse(any(node["actions"] for node in nodes.values()))
        self.operator("writing", "paused", "writing")
        save(self.attempt / "attempt.json", {"id": "a-first", "action": "start", "operator": True, "returncode": 0})
        snapshot, nodes = self.status()
        self.assertEqual(snapshot["state"], "paused")
        self.assertFalse(snapshot["launcher_active"])
        self.assertEqual(nodes["writing"]["actions"][0]["id"], "continue-operation")
        self.assertFalse(nodes["release"]["actions"])
        # A launcher preflight failure on continue must not discard the pause.
        later = self.attempt.with_name("a-next")
        later.mkdir()
        save(later / "attempt.json", {"id": "a-next", "action": "continue-operation", "returncode": 1})
        snapshot, nodes = self.status()
        self.assertEqual(snapshot["state"], "paused")
        self.assertEqual(snapshot["launcher_state"], "failed")
        self.assertEqual(nodes["writing"]["actions"][0]["attempt"], "a-next")


if __name__ == "__main__":
    unittest.main()
