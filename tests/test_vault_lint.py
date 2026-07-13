from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes-obsidian-vault-lint" / "scripts" / "lint_vault.py"
INGEST = ROOT / "hermes-obsidian-controlled-ingest"


class VaultLintTest(unittest.TestCase):
    def run_lint(self, vault: Path, profile: str = "post-ingest", expect: int = 0) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault",
                str(vault),
                "--profile",
                profile,
                "--ingest-skill-path",
                str(INGEST),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expect, result.returncode, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout)

    def create_vault(self, root: Path) -> Path:
        vault = root / "Vault"
        for folder in (
            "10_Raw/converted",
            "30_Cards",
            "40_Concepts",
            "50_Projects",
            "90_Dataview",
            "_system/metadata",
            "_system/reports",
        ):
            (vault / folder).mkdir(parents=True)
        (vault / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
        (vault / "_system" / "metadata" / "concept-registry.md").write_text(
            "---\ntype: metadata\nstatus: active\n---\n# Concept Registry\n",
            encoding="utf-8",
        )

        bundle = vault / "10_Raw" / "converted" / "sample_document_bundle"
        bundle.mkdir()
        (bundle / "document.md").write_text("<!-- source-page: 1 -->\n# One\nalpha\n", encoding="utf-8")
        (bundle / "outline.json").write_text(
            json.dumps(
                {
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
                            "end_line": 3,
                            "pages": [1],
                            "assets": [],
                            "quality": "pass",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "profile": "engineering",
                    "source": {
                        "path": "/source/sample.pdf",
                        "filename": "sample.pdf",
                        "sha256": "a" * 64,
                        "parsed_pages": 1,
                    },
                    "conversion": {"engine": "MinerU"},
                    "document": {"path": "document.md", "line_count": 3},
                    "outline": {"path": "outline.json", "section_count": 1},
                    "images": [],
                    "tables": [],
                    "evidence": {"default_ingest": False, "files": [], "blocks": None},
                    "quality": {"status": "pass", "issues": [], "review_required": []},
                }
            ),
            encoding="utf-8",
        )

        reports = vault / "_system" / "reports"
        ledger = {
            "ledger_schema_version": "1.0",
            "bundle_id": "bundle-v2-" + ("a" * 16),
            "revision": 1,
            "state": "active",
            "validation": {"status": "pass"},
            "source": {"sha256": "a" * 64},
            "sections": [
                {
                    "id": "one",
                    "status": "qa_required",
                    "outputs": ["30_Cards/one.md"],
                    "ingest_unit": True,
                }
            ],
        }
        (reports / "sample.section-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        (reports / "sample.source-map.md").write_text(
            "---\n"
            "type: source-map\n"
            "bundle_id: \"bundle-v2-aaaaaaaaaaaaaaaa\"\n"
            "source_sha256: \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"\n"
            "validation_status: \"pass\"\n"
            "ledger_revision: 1\n"
            "ingest_state: \"active\"\n"
            "---\n"
            "# Source Map\n",
            encoding="utf-8",
        )
        (vault / "30_Cards" / "one.md").write_text(
            "---\n"
            "type: knowledge-card\n"
            "status: draft\n"
            "source_bundle_id: bundle-v2-aaaaaaaaaaaaaaaa\n"
            "source_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "source_section_id: \"one\"\n"
            "source_lines: \"2-3\"\n"
            "source_pages:\n"
            "  - 1\n"
            "source_assets: []\n"
            "---\n"
            "# One\n",
            encoding="utf-8",
        )
        return vault

    def add_second_source(self, vault: Path) -> str:
        bundle_id = "bundle-v2-" + ("b" * 16)
        reports = vault / "_system" / "reports"
        ledger = {
            "ledger_schema_version": "1.0",
            "bundle_id": bundle_id,
            "revision": 1,
            "state": "complete",
            "validation": {"status": "pass"},
            "source": {"sha256": "b" * 64},
            "sections": [
                {
                    "id": "two",
                    "quality": "pass",
                    "status": "ingested",
                    "outputs": [],
                    "ingest_unit": True,
                }
            ],
        }
        (reports / "second.section-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        (reports / "second.source-map.md").write_text(
            "---\n"
            "type: source-map\n"
            f"bundle_id: {bundle_id}\n"
            f"source_sha256: {'b' * 64}\n"
            "validation_status: pass\n"
            "ledger_revision: 1\n"
            "ingest_state: complete\n"
            "---\n# Source Map\n",
            encoding="utf-8",
        )
        return bundle_id

    def test_post_ingest_allows_controlled_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = self.run_lint(self.create_vault(Path(temporary)))
            self.assertEqual("pass-with-warnings", data["status"])
            self.assertEqual(0, data["summary"]["errors"])
            self.assertIn("ledger.qa_open", {issue["code"] for issue in data["issues"]})
            self.assertIn("qa.boundary_weak", {issue["code"] for issue in data["issues"]})

    def test_strict_escalates_controlled_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = self.run_lint(self.create_vault(Path(temporary)), profile="strict", expect=2)
            self.assertEqual("fail", data["status"])
            self.assertIn(
                ("ledger.qa_open", "error"),
                {(issue["code"], issue["severity"]) for issue in data["issues"]},
            )

    def test_structured_multi_source_evidence_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.create_vault(Path(temporary))
            second_bundle = self.add_second_source(vault)
            first_ledger_path = vault / "_system" / "reports" / "sample.section-ledger.json"
            first_ledger = json.loads(first_ledger_path.read_text(encoding="utf-8"))
            first_ledger["sections"][0]["status"] = "ingested"
            first_ledger["sections"][0]["quality"] = "pass"
            first_ledger_path.write_text(json.dumps(first_ledger), encoding="utf-8")
            (vault / "30_Cards" / "synthesis.md").write_text(
                "---\ntype: knowledge-card\nstatus: draft\n---\n"
                "# Synthesis\n\n"
                "| source | bundle | section | pages | owned lines |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| A | `bundle-v2-aaaaaaaaaaaaaaaa` | `one` | 1 | 2-3 |\n"
                f"| B | `{second_bundle}` | `two` | 2 | 4-5 |\n",
                encoding="utf-8",
            )
            data = self.run_lint(vault)
            self.assertNotIn("synthesis.multi_source_unstructured", {issue["code"] for issue in data["issues"]})
            self.assertEqual(1, data["metrics"]["multi_source_artifacts"])
            self.assertEqual(1, data["metrics"]["structured_multi_source_artifacts"])

    def test_unstructured_multi_source_evidence_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.create_vault(Path(temporary))
            second_bundle = self.add_second_source(vault)
            (vault / "30_Cards" / "synthesis.md").write_text(
                "---\ntype: knowledge-card\nstatus: draft\n---\n"
                "# Synthesis\n\n"
                f"Compare bundle-v2-aaaaaaaaaaaaaaaa with {second_bundle}.\n",
                encoding="utf-8",
            )
            data = self.run_lint(vault)
            self.assertIn("synthesis.multi_source_unstructured", {issue["code"] for issue in data["issues"]})

    def test_qa_evidence_cannot_be_promoted_as_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.create_vault(Path(temporary))
            card = vault / "30_Cards" / "one.md"
            card.write_text(
                card.read_text(encoding="utf-8").replace("status: draft", "status: published\nevidence_level: clear"),
                encoding="utf-8",
            )
            data = self.run_lint(vault, expect=2)
            self.assertIn("qa.authority_overpromoted", {issue["code"] for issue in data["issues"]})


if __name__ == "__main__":
    unittest.main()
