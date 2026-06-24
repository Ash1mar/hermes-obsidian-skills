from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "manage_bundle_ingest.py"


class BundleLedgerTest(unittest.TestCase):
    def run_cli(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expect, result.returncode, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    def create_bundle(self, root: Path) -> Path:
        bundle = root / "sample_document_bundle"
        bundle.mkdir()
        document = "<!-- source-page: 1 -->\n# One\nalpha\n# Two\nbeta\n"
        (bundle / "document.md").write_text(document, encoding="utf-8")
        outline = {
            "schema_version": "2.0",
            "document": "document.md",
            "sections": [
                {
                    "id": "one",
                    "title": "One",
                    "level": 1,
                    "parent": None,
                    "path": ["one"],
                    "start_line": 2,
                    "end_line": 5,
                    "pages": [1],
                    "assets": [],
                    "quality": "pass",
                },
                {
                    "id": "two",
                    "title": "Two",
                    "level": 2,
                    "parent": "one",
                    "path": ["one", "two"],
                    "start_line": 4,
                    "end_line": 5,
                    "pages": [1],
                    "assets": [],
                    "quality": "pass",
                },
            ],
        }
        (bundle / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
        manifest = {
            "schema_version": "2.0",
            "profile": "engineering",
            "source": {
                "path": "/source/sample.pdf",
                "filename": "sample.pdf",
                "sha256": "a" * 64,
                "parsed_pages": 1,
            },
            "conversion": {"engine": "MinerU", "backend": "pipeline"},
            "document": {"path": "document.md", "line_count": 5},
            "outline": {"path": "outline.json", "section_count": 2},
            "images": [],
            "tables": [],
            "evidence": {"default_ingest": False, "files": [], "blocks": None},
            "quality": {"status": "pass", "issues": [], "review_required": []},
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return bundle

    def test_initialize_update_reconcile_and_stale_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = self.create_bundle(root)
            reports = root / "vault" / "_system" / "reports"
            ledger = reports / "sample.section-ledger.json"
            source_map = reports / "sample.source-map.md"

            self.run_cli("init", str(bundle), "--reports-dir", str(reports))
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(1, value["revision"])
            self.assertEqual("active", value["state"])
            self.assertEqual(["pending", "pending"], [item["status"] for item in value["sections"]])
            self.assertEqual(
                [[{"start_line": 2, "end_line": 3}], [{"start_line": 4, "end_line": 5}]],
                [item["content_ranges"] for item in value["sections"]],
            )
            self.assertIn("source_bundle_id", source_map.read_text(encoding="utf-8"))

            self.run_cli(
                "update",
                str(ledger),
                "--section",
                "one",
                "--status",
                "in_progress",
                "--expected-revision",
                "1",
            )
            self.run_cli(
                "update",
                str(ledger),
                "--section",
                "one",
                "--status",
                "ingested",
                "--expected-revision",
                "2",
                "--output",
                "30_Cards/one.md",
            )
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(3, value["revision"])
            self.assertEqual("ingested", value["sections"][0]["status"])

            self.run_cli("init", str(bundle), "--reports-dir", str(reports))
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(4, value["revision"])
            self.assertEqual("ingested", value["sections"][0]["status"])

            document = (bundle / "document.md").read_text(encoding="utf-8").replace("alpha", "ALPHA")
            (bundle / "document.md").write_text(document, encoding="utf-8")
            self.run_cli("init", str(bundle), "--reports-dir", str(reports))
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(5, value["revision"])
            self.assertEqual("stale", value["sections"][0]["status"])
            self.assertEqual("pending", value["sections"][1]["status"])
            self.assertEqual("stale", value["state"])

            conflict = self.run_cli(
                "update",
                str(ledger),
                "--section",
                "two",
                "--status",
                "in_progress",
                "--expected-revision",
                "1",
                expect=2,
            )
            self.assertIn("Revision conflict", conflict.stderr)


if __name__ == "__main__":
    unittest.main()
