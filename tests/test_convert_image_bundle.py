from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONVERT = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "convert_image_with_ocr_bundle.py"
VALIDATE = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "validate_document_bundle.py"
MANAGE = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "manage_bundle_ingest.py"


class ImageBundleTest(unittest.TestCase):
    def run_cli(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expect, result.returncode, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    def test_image_bundle_validates_warn_and_initializes_qa_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "scan.gif"
            image.write_bytes(
                b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00"
                b"\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
                b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02"
                b"D\x01\x00;"
            )
            bundle = root / "scan_image_document_bundle"
            reports = root / "vault" / "_system" / "reports"

            self.run_cli(str(CONVERT), str(image), "-o", str(bundle), "--material-type", "scanned-page")

            validation = self.run_cli(str(VALIDATE), str(bundle), "--json", expect=1)
            validation_json = json.loads(validation.stdout)
            self.assertEqual("warn", validation_json["status"])
            self.assertIn("image-source-visual-review", validation_json["review_required"])

            self.run_cli(str(MANAGE), "init", str(bundle), "--reports-dir", str(reports))
            ledger = json.loads((reports / "scan.section-ledger.json").read_text(encoding="utf-8"))
            self.assertEqual("active", ledger["state"])
            self.assertEqual(["qa_required"], [section["status"] for section in ledger["sections"]])
            self.assertEqual(["image-source-visual-review"], ledger["review_required"])


if __name__ == "__main__":
    unittest.main()
