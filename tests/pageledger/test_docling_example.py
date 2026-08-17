"""Contract tests for the optional machine-level Docling example adapter."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pageledger.adapters import adapter_conformance_check


@pytest.fixture
def docling_module():  # noqa: ANN201
    examples = Path(__file__).resolve().parents[2] / "examples"
    sys.path.insert(0, str(examples))
    try:
        import docling_adapter

        yield docling_adapter
    finally:
        sys.path.pop(0)
        sys.modules.pop("docling_adapter", None)


def _docling_document() -> dict:
    return {
        "schema_name": "DoclingDocument",
        "version": "1.9.0",
        "name": "sample",
        "pages": {"1": {"page_no": 1}, "2": {"page_no": 2}},
        "body": {
            "self_ref": "#/body",
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/tables/0"},
                {"$ref": "#/groups/0"},
            ],
        },
        "furniture": {"self_ref": "#/furniture", "children": []},
        "groups": [
            {
                "self_ref": "#/groups/0",
                "children": [{"$ref": "#/texts/2"}],
                "label": "list",
            }
        ],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "title",
                "text": "First Page",
                "prov": [{"page_no": 1}],
                "children": [],
            },
            {
                "self_ref": "#/texts/1",
                "label": "caption",
                "text": "Table 1 caption",
                "prov": [{"page_no": 1}],
                "children": [],
            },
            {
                "self_ref": "#/texts/2",
                "label": "text",
                "text": "Second page paragraph.",
                "prov": [{"page_no": 2}],
                "children": [],
            },
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "label": "table",
                "prov": [{"page_no": 1}],
                "captions": [{"$ref": "#/texts/1"}],
                "children": [],
                "data": {
                    "num_rows": 2,
                    "num_cols": 2,
                    "grid": [
                        [{"text": "Year"}, {"text": "Value"}],
                        [{"text": "2024"}, {"text": "1 | 2"}],
                    ],
                },
            }
        ],
        "pictures": [],
        "form_items": [],
        "key_value_items": [],
    }


def _install_fake_docling(monkeypatch, module, document: dict) -> list[list[str]]:  # noqa: ANN001
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return "/machine/bin/docling" if name == "docling" else None

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        call = [str(value) for value in argv]
        calls.append(call)
        if "--version" in call:
            return subprocess.CompletedProcess(
                call,
                0,
                stdout="Docling version: 2.120.1\nPython: cpython-313\n",
                stderr="",
            )
        output = Path(call[call.index("--output") + 1])
        output.mkdir(parents=True, exist_ok=True)
        target_format = call[call.index("--to") + 1]
        if target_format == "md":
            (output / "sample.md").write_text("VLM page output", encoding="utf-8")
        else:
            (output / "sample.json").write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(call, 0, stdout="", stderr="")

    monkeypatch.setattr(module.shutil, "which", fake_which)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def test_docling_adapter_passes_conformance(docling_module) -> None:  # noqa: ANN001
    adapter = docling_module.DoclingAdapter()
    assert adapter_conformance_check(adapter) == []
    assert adapter.capabilities == ("ocr", "layout", "tables", "local")


@pytest.mark.parametrize(
    ("pipeline", "action", "supported"),
    [
        ("standard", "transcribe_text", True),
        ("standard", "extract_table", True),
        ("standard", "vlm_table", False),
        ("vlm", "transcribe_text", True),
        ("vlm", "extract_table", True),
        ("vlm", "vlm_table", True),
    ],
)
def test_docling_actions_and_capabilities_are_pipeline_aware(
    docling_module, pipeline: str, action: str, supported: bool  # noqa: ANN001
) -> None:
    adapter = docling_module.DoclingAdapter(pipeline=pipeline)
    assert adapter.supports(action) is supported
    assert ("vlm" in adapter.capabilities) is (pipeline == "vlm")


def test_docling_adapter_rejects_prompts_it_cannot_apply(
    tmp_path: Path, docling_module  # noqa: ANN001
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")

    with pytest.raises(ValueError, match="does not apply route prompts"):
        docling_module.DoclingAdapter().extract(
            source,
            page_id="doc_0001_page_0001",
            page_number=1,
            action="transcribe_text",
            prompt="Preserve spelling.",
        )


def test_docling_standard_pipeline_rejects_vlm_action(
    tmp_path: Path, docling_module  # noqa: ANN001
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")

    with pytest.raises(ValueError, match="does not support action: vlm_table"):
        docling_module.DoclingAdapter().extract(
            source,
            page_id="doc_0001_page_0001",
            page_number=1,
            action="vlm_table",
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"pipeline": "remote"}, "pipeline"),
        ({"pipeline": "vlm", "vlm_model": "deepseek_ocr"}, "dogfooded local VLM"),
        ({"pipeline": "vlm", "vlm_model": "granite_docling"}, "dogfooded local VLM"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"num_threads": True}, "num_threads"),
    ],
)
def test_docling_adapter_rejects_unsafe_or_invalid_options(
    docling_module, options: dict, message: str  # noqa: ANN001
) -> None:
    with pytest.raises(ValueError, match=message):
        docling_module.DoclingAdapter(**options)


def test_docling_adapter_batches_once_and_emits_page_markdown(
    tmp_path: Path, monkeypatch, docling_module  # noqa: ANN001
) -> None:
    calls = _install_fake_docling(monkeypatch, docling_module, _docling_document())
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")
    adapter = docling_module.DoclingAdapter()

    first = adapter.extract(
        source,
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )
    second = adapter.extract(
        source,
        page_id="doc_0001_page_0002",
        page_number=2,
        action="transcribe_text",
    )

    assert first.format == "markdown"
    assert first.content == (
        "# First Page\n\n"
        "*Table 1 caption*\n\n"
        "| Year | Value |\n"
        "| --- | --- |\n"
        "| 2024 | 1 \\| 2 |"
    )
    assert second.content == "Second page paragraph."
    assert first.model == (
        "docling 2.120.1; pipeline=standard; ocr=auto; "
        "tables=accurate; batch=document"
    )
    assert first.confidence is None
    assert first.usage["pages"] == second.usage["pages"] == 1
    assert first.usage["compute_seconds"] == second.usage["compute_seconds"]
    assert first.usage["cost_usd"] is None

    assert len(calls) == 2  # one version probe, one conversion for both pages
    conversion = calls[1]
    assert conversion[0] == "/machine/bin/docling"
    assert conversion[1] == str(source.resolve())
    assert conversion[conversion.index("--pipeline") + 1] == "standard"
    assert conversion[conversion.index("--ocr-engine") + 1] == "auto"
    assert conversion[conversion.index("--table-mode") + 1] == "accurate"
    assert "--enable-remote-services" not in conversion
    assert "--allow-external-plugins" not in conversion
    assert "--page-range" not in conversion


def test_docling_vlm_mode_records_preset_and_keeps_services_local(
    tmp_path: Path, monkeypatch, docling_module  # noqa: ANN001
) -> None:
    calls = _install_fake_docling(monkeypatch, docling_module, _docling_document())
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")
    adapter = docling_module.DoclingAdapter(pipeline="vlm", vlm_model="smoldocling")

    result = adapter.extract(
        source,
        page_id="doc_0001_page_0001",
        page_number=1,
        action="vlm_table",
    )

    assert result.model == (
        "docling 2.120.1; pipeline=vlm; vlm_model=smoldocling; batch=page"
    )
    assert result.content == "VLM page output"
    assert result.warnings == ["docling_vlm_uncalibrated"]
    conversion = calls[1]
    assert conversion[conversion.index("--to") + 1] == "md"
    assert conversion[conversion.index("--pipeline") + 1] == "vlm"
    assert conversion[conversion.index("--vlm-model") + 1] == "smoldocling"
    assert conversion[conversion.index("--page-range") + 1] == "1-1"
    assert "--enable-remote-services" not in conversion
    assert "--allow-external-plugins" not in conversion


def test_docling_adapter_rejects_missing_executable(
    tmp_path: Path, monkeypatch, docling_module  # noqa: ANN001
) -> None:
    monkeypatch.setattr(docling_module.shutil, "which", lambda name: None)
    adapter = docling_module.DoclingAdapter()
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")
    with pytest.raises(RuntimeError, match="uv tool install docling"):
        adapter.extract(
            source,
            page_id="doc_0001_page_0001",
            page_number=1,
            action="transcribe_text",
        )


def test_docling_adapter_rejects_malformed_document_output(
    tmp_path: Path, monkeypatch, docling_module  # noqa: ANN001
) -> None:
    malformed = _docling_document()
    malformed.pop("pages")
    _install_fake_docling(monkeypatch, docling_module, malformed)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")

    with pytest.raises(RuntimeError, match="pages mapping"):
        docling_module.DoclingAdapter().extract(
            source,
            page_id="doc_0001_page_0001",
            page_number=1,
            action="transcribe_text",
        )


def test_docling_adapter_warns_on_untranscribed_visual_regions(
    tmp_path: Path, monkeypatch, docling_module  # noqa: ANN001
) -> None:
    document = _docling_document()
    document["body"]["children"].extend(
        [{"$ref": "#/pictures/0"}, {"$ref": "#/tables/1"}]
    )
    document["pictures"] = [
        {
            "self_ref": "#/pictures/0",
            "label": "picture",
            "prov": [{"page_no": 1}],
            "captions": [],
            "children": [],
        }
    ]
    document["tables"].append(
        {
            "self_ref": "#/tables/1",
            "label": "table",
            "prov": [{"page_no": 1}],
            "captions": [],
            "children": [],
            "data": {"num_rows": 0, "num_cols": 0, "grid": []},
        }
    )
    _install_fake_docling(monkeypatch, docling_module, document)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-fake")

    result = docling_module.DoclingAdapter().extract(
        source,
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )

    assert "*[Picture not transcribed]*" in result.content
    assert "*[Table without cells]*" in result.content
    assert result.warnings == [
        "docling_picture_not_transcribed",
        "docling_table_without_cells",
    ]
