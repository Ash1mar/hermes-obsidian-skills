from __future__ import annotations

import argparse
import importlib.util
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONVERTER = (
    ROOT
    / "hermes-obsidian-controlled-ingest"
    / "scripts"
    / "convert_pdf_with_mineru_bundle.py"
)


def load_converter():
    spec = importlib.util.spec_from_file_location("convert_pdf_with_mineru_bundle", CONVERTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployment_args(**overrides):
    values = {
        "mineru_api_url": None,
        "mineru_invocation": None,
        "from_mineru_output": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_main_defaults_to_local_mineru_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_converter()
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    args = deployment_args()

    module.apply_mineru_deployment_defaults(args, {})

    assert args.mineru_invocation == "cli"
    assert args.mineru_api_url is None


def test_checked_in_profile_selects_mineru_http_without_runtime_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_converter()
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    args = deployment_args()

    module.apply_mineru_deployment_defaults(
        args,
        {
            "mineru_invocation": "api",
            "mineru_api_url": "http://mineru.internal:7861",
        },
    )

    assert args.mineru_invocation == "api"
    assert args.mineru_api_url == "http://mineru.internal:7861"


def test_explicit_cli_overrides_packaged_http_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_converter()
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    args = deployment_args(mineru_invocation="cli")

    module.apply_mineru_deployment_defaults(
        args,
        {
            "mineru_invocation": "api",
            "mineru_api_url": "http://mineru.internal:7861",
        },
    )

    assert args.mineru_invocation == "cli"


def test_explicit_deployment_config_must_exist(tmp_path: Path) -> None:
    module = load_converter()

    with pytest.raises(ValueError, match="Deployment config does not exist"):
        module.load_deployment_config(tmp_path / "missing.json")


def test_http_bundle_extraction_rejects_parent_traversal(tmp_path: Path) -> None:
    module = load_converter()
    zip_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")

    with pytest.raises(ValueError, match="unsafe zip member path"):
        module.safe_extract_zip(zip_path, tmp_path / "output")


def test_http_bundle_form_contains_required_mineru_fields(tmp_path: Path) -> None:
    module = load_converter()
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF fixture")
    args = argparse.Namespace(
        lang="ch",
        start=None,
        end=None,
        backend="hybrid-engine",
        effort="high",
        method="auto",
        formula=True,
        table=True,
        image_analysis=False,
        api_return_original_file=True,
    )

    fields = dict(module.api_form_fields(args))
    body, content_boundary = module.multipart_form_data(list(fields.items()), "files", pdf)

    assert fields["response_format_zip"] == "true"
    assert fields["return_middle_json"] == "true"
    assert fields["return_content_list"] == "true"
    assert f"--{content_boundary}".encode() in body
    assert b'filename="sample.pdf"' in body
