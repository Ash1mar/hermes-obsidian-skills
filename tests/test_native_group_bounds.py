import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import fixed_semantic
from hermes_source_units.source_units import _load_json
from fixed_semantic import (
    PHASE_BALANCED_POLICY,
    build_group_plan,
    bounded_correction_message,
    budget_policy_pin,
    correction_reserve,
    group_usage,
    persist_group_plan,
    prepare_request_projection,
    slot_schema,
    subset,
)


def task(alias, text_ref):
    return {
        "task": alias,
        "materials": [{"role": "core", "text_ref": text_ref, "asset": False}],
        "continuation": {"candidate": {"candidates": []}},
    }


def packet(texts):
    tasks = [task(f"t{i}", ref) for i, ref in enumerate(texts, 1)]
    return {
        "contract": "test-packet/v1",
        "tasks": tasks,
        "texts": dict(texts),
        "headings": {},
        "qa": {},
        "qa_rule": "Preserve uncertainty.",
    }


class PassBoundsTests(unittest.TestCase):
    def test_budget_matches_complete_wire_request_including_schema(self):
        source = packet([("m1", "Safety condition applies."), ("m2", "Keep the unit and exception.")])
        tasks = source["tasks"]
        usage = group_usage(source, tasks, "candidate", 30000, None, PHASE_BALANCED_POLICY)
        schema = slot_schema("candidate", tasks)
        prepared = prepare_request_projection("candidate", subset(source, tasks, "candidate"), schema)
        self.assertEqual(usage["input_codepoints"], prepared["input_size"])
        self.assertEqual(usage["correction_reserve_codepoints"], correction_reserve(2, PHASE_BALANCED_POLICY))
        self.assertLessEqual(usage["input_codepoints"] + usage["correction_reserve_codepoints"], 30000)
        self.assertLessEqual(usage["estimated_output_tokens"], 8192 - 1024)

    def test_ready_plan_records_whole_group_and_policy_source(self):
        source = packet([("m1", "One full task."), ("m2", "Another full task.")])
        groups, plan = build_group_plan(
            source, source["tasks"], "candidate", 30000, None,
            PHASE_BALANCED_POLICY, pinned=False,
        )
        self.assertEqual([[t["task"] for t in group] for group in groups], [["t1", "t2"]])
        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["policy"], budget_policy_pin())
        self.assertFalse(plan["policy_pinned_by_batch"])
        self.assertEqual(plan["groups"][0]["tasks"], ["t1", "t2"])
        self.assertIn("estimated_output_tokens", plan["groups"][0])

    def test_oversized_task_stays_intact_and_all_slots_are_audited(self):
        source = packet([("m1", "small authorized task"), ("m2", "large authorized task " * 3000)])
        groups, plan = build_group_plan(
            source, source["tasks"], "candidate", 30000, None,
            PHASE_BALANCED_POLICY, pinned=True,
        )
        self.assertEqual(groups, [])
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual([group["tasks"] for group in plan["groups"]], [["t1"], ["t2"]])
        self.assertEqual(plan["groups"][0]["status"], "held_due_to_sibling_blocker")
        self.assertEqual(plan["groups"][1]["status"], "blocked")
        self.assertIn("input_with_correction_reserve", plan["groups"][1]["blocking_reasons"])
        self.assertIn("code", plan["error"])
        with tempfile.TemporaryDirectory() as root:
            binding = {"workflow_id": "wf-test", "node": "pass-slice:test"}
            ref = persist_group_plan(root, binding, "candidate", plan)
            saved = _load_json(Path(root) / ref)
            self.assertEqual(saved["groups"][1]["tasks"], ["t2"])
            self.assertIn("input_with_correction_reserve", saved["groups"][1]["blocking_reasons"])
            self.assertEqual(saved["state"], "blocked")

    def test_correction_diagnostic_is_wire_bounded(self):
        diagnostic = "VALIDATION_FAILED: " + ("bad\\nfield\t" * 100)
        bounded = bounded_correction_message(diagnostic, PHASE_BALANCED_POLICY)
        import json
        wire_size = len(json.dumps(json.dumps(bounded, ensure_ascii=False), ensure_ascii=False))
        self.assertLessEqual(wire_size, PHASE_BALANCED_POLICY["correction_message_codepoints"])
        self.assertTrue(bounded.startswith("VALIDATION_FAILED:"))

    def test_full_correction_slots_fit_the_reserved_allowance(self):
        source = packet([("m1", "First full task."), ("m2", "Second full task.")])
        tasks = source["tasks"]
        schema = slot_schema("candidate", tasks)
        view = subset(source, tasks, "candidate")
        base = prepare_request_projection("candidate", view, schema)
        message = bounded_correction_message("INVALID_SCHEMA: " + ("bad\\nfield\t" * 100), PHASE_BALANCED_POLICY)
        correction = {"failures": [
            {"task": task["task"], "code": "INVALID_SCHEMA", "message": message}
            for task in tasks
        ]}
        corrected = prepare_request_projection("candidate", view, schema, correction=correction)
        reserve = correction_reserve(len(tasks), PHASE_BALANCED_POLICY)
        self.assertLessEqual(corrected["input_size"] - base["input_size"], reserve)

    def test_group_audit_reports_registered_image_count_and_bytes(self):
        source = packet([("m1", "Visual evidence is authorized.")])
        material = source["tasks"][0]["materials"][0]
        material.update({"ref": "m1", "asset": {"path": "registered/image.png"}})
        catalog = {"registered/image.png": {
            "bytes": 5, "media_type": "image/png", "sha256": "fixture-hash",
            "url": "data:image/png;base64,YWJjZGU=",
        }}
        usage = group_usage(source, source["tasks"], "candidate", 30000, catalog, PHASE_BALANCED_POLICY)
        self.assertEqual(usage["image_count"], 1)
        self.assertEqual(usage["image_bytes"], 5)
        self.assertEqual(usage["largest_image_bytes"], 5)
        self.assertGreaterEqual(usage["image_max_count"], usage["image_count"])
        self.assertGreaterEqual(usage["image_max_bytes"], usage["image_bytes"])

    def test_submission_rejection_keeps_partial_success_and_corrects_only_failed_slot_once(self):
        source = packet([("m1", "First source fact."), ("m2", "Second source fact.")])
        for task in source["tasks"]:
            task.update({"next_sequence": 1, "continuation": {"action": "candidate_then_citation"}})
        cause = "EVIDENCE_REF_INVALID: c2 lacks its authorized page reference"
        model_calls = []
        policies = []
        submission_calls = []

        class Knowledge:
            def _batch(self, _batch_id):
                return {"slice_config": {"slice_max_input_codepoints": 30000}}

        class Workflow:
            vault = Path(".")
            knowledge = Knowledge()

            def status(self, _workflow_id):
                return {"batch_id": "batch-test"}

        class Adapter:
            workflow = Workflow()

        binding = {"workflow_id": "wf-test", "node": "pass-slice:test", "task_id": "kanban-test"}

        def fake_model_call(_adapter, _binding, phase, view, _caller, _limit, correction=None, **_kwargs):
            policies.append(_kwargs.get('policy'))
            model_calls.append((phase, [task["task"] for task in view["tasks"]], correction))
            return {"drafts": [], "slot_failures": []}, "audit-ref"

        def fake_prepare(_adapter, _draft, _packet, tasks, phase):
            if phase == "candidate":
                return ([{"task": task["task"], "sequence": 1} for task in tasks], [], [])
            return [], [], []

        def fake_pass_action(_vault, action, payload=None, **_kwargs):
            if action == "submit":
                submitted = [item["task"] for item in payload["passes"]]
                submission_calls.append(submitted)
                if len(submission_calls) == 1:
                    return {"ok": False, "results": [{"task": "t1"}], "failures": [
                        {"task": "t2", "code": "EVIDENCE_REF_INVALID", "message": cause}
                    ]}
                return {"ok": True, "results": [{"task": "t2"}], "failures": []}
            return {"ok": True, "domain_complete": action == "complete"}

        with tempfile.TemporaryDirectory() as root:
            Workflow.vault = Path(root)
            with patch.multiple(
                fixed_semantic,
                read_pass_input=lambda *_args, **_kwargs: {"ok": True},
                pass_packet=lambda _adapter, _receipt: source,
                image_catalog=lambda *_args, **_kwargs: {},
                model_call=fake_model_call,
                prepare_pass_items=fake_prepare,
                pass_action=fake_pass_action,
            ):
                result = fixed_semantic.run_pass(Adapter(), binding, object())

        self.assertTrue(result["domain_complete"])
        self.assertTrue(policies and all(policy is None for policy in policies))
        self.assertEqual(submission_calls, [["t1", "t2"], ["t2"]])
        correction_calls = [call for call in model_calls if call[2] is not None]
        self.assertEqual(len(correction_calls), 1)
        self.assertEqual(correction_calls[0][1], ["t2"])
        self.assertEqual(correction_calls[0][2]["failures"][0]["code"], "EVIDENCE_REF_INVALID")
        self.assertEqual(correction_calls[0][2]["failures"][0]["message"], cause)


if __name__ == "__main__":
    unittest.main()
