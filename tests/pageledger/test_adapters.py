"""Phase 3 tests: adapter contract hardening, conformance, metadata validation."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import textwrap
import types
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

import pageledger.adapters as adapters_module
from pageledger.adapters import (
    ExtractionResult,
    PdfOcrAdapter,
    PdfTextAdapter,
    TextAdapter,
    adapter_conformance_check,
    load_adapter,
    ocr_pdf_page_count,
)

# =========================================================================
# Adapter conformance helper — built-in adapters
# =========================================================================


def test_builtin_text_adapter_passes_conformance() -> None:
    issues = adapter_conformance_check(TextAdapter())
    assert issues == []


def test_builtin_pdf_text_adapter_passes_conformance() -> None:
    issues = adapter_conformance_check(PdfTextAdapter())
    assert issues == []


def test_pdf_text_profile_material_provider_success_and_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pageledger.replay as replay_module

    material = {"kind": "package", "name": "pypdf", "version": "1.0", "sha256": "0" * 64}
    monkeypatch.setattr(replay_module, "package_material", lambda name: material)
    profile = replay_module.build_reproducibility_profile(PdfTextAdapter())
    assert profile is not None
    assert profile["materials"] == [material]

    def unavailable(_name: str) -> dict[str, str]:
        raise ValueError("package metadata unavailable")

    monkeypatch.setattr(replay_module, "package_material", unavailable)
    assert replay_module.build_reproducibility_profile(PdfTextAdapter()) is None


def test_pdf_ocr_profile_material_provider_success_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pageledger.replay as replay_module

    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"trained data")
    tesseract = tmp_path / "tesseract"
    pdftoppm = tmp_path / "pdftoppm"
    tesseract.write_bytes(b"tesseract")
    pdftoppm.write_bytes(b"pdftoppm")
    monkeypatch.setattr(
        adapters_module,
        "_require_binary",
        lambda name: str(tesseract if name == "tesseract" else pdftoppm),
    )
    monkeypatch.setattr(adapters_module, "_tesseract_data_dir", lambda _path: tessdata)
    monkeypatch.setattr(adapters_module, "_tesseract_model_string", lambda: "tesseract 1.0")
    monkeypatch.setattr(adapters_module, "_pdftoppm_model_string", lambda: "pdftoppm 1.0")

    profile = replay_module.build_reproducibility_profile(PdfOcrAdapter())
    assert profile is not None
    assert {material["name"] for material in profile["materials"]} == {
        "tesseract",
        "pdftoppm",
        "tesseract:eng.traineddata",
    }

    def unavailable(_name: str) -> str:
        raise RuntimeError("binary discovery unavailable")

    monkeypatch.setattr(adapters_module, "_require_binary", unavailable)
    assert replay_module.build_reproducibility_profile(PdfOcrAdapter()) is None


def test_pdf_ocr_run_without_profile_is_ordinary_but_not_bundleable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.replay import ReplayError, bundle_run
    from pageledger.runner import run

    monkeypatch.setattr(
        adapters_module,
        "_require_binary",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tesseract unavailable")),
    )
    # Extraction is mocked below, so the tool check that guards real runs is too.
    monkeypatch.setattr(PdfOcrAdapter, "preflight", lambda self, sources: None)
    monkeypatch.setattr(PdfOcrAdapter, "page_count", lambda self, source: 1)
    monkeypatch.setattr(
        PdfOcrAdapter,
        "extract",
        lambda self, source, *, page_id, page_number, action, prompt=None: ExtractionResult(
            content="OCR text",
            format="text",
            confidence=None,
            model="mock-ocr",
            warnings=[],
            usage={"pages": 1},
        ),
    )
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"not a real PDF; extraction is mocked")
    config = tmp_path / "config.yml"
    config.write_text(
        "schema_version: '0.1'\n"
        "taxonomy:\n"
        "  page_types:\n"
        "    prose:\n"
        "      default_action: transcribe_text\n"
        "run:\n"
        "  adapter: pdf_ocr\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    result = run(inputs=[source], config_path=config, out_dir=run_dir, dry_run=False)

    assert result["summary"]["pages_extracted"] == 1
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "reproducibility_profile" not in manifest["extractors"][0]
    with pytest.raises(ReplayError) as error:
        bundle_run(run_dir, tmp_path / "bundle")
    assert error.value.code == "profile_missing"


def test_pdf_text_records_pypdf_backend_version(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("pypdf")
    monkeypatch.setattr(PdfTextAdapter, "_document_text", lambda self, source: ("text",))
    result = PdfTextAdapter().extract(
        tmp_path / "input.pdf",
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )

    assert result.model == f"pypdf {importlib.metadata.version('pypdf')}"


def test_pdf_text_records_unversioned_backend_when_metadata_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(PdfTextAdapter, "_document_text", lambda self, source: ("text",))

    def missing_distribution(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(adapters_module.importlib.metadata, "version", missing_distribution)
    result = PdfTextAdapter().extract(
        tmp_path / "input.pdf",
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )

    assert result.model == "pypdf"


@pytest.mark.parametrize("adapter", [TextAdapter(), PdfTextAdapter(), PdfOcrAdapter()])
def test_builtin_adapter_identity_is_immutable(adapter) -> None:  # noqa: ANN001
    with pytest.raises(FrozenInstanceError):
        adapter.name = "renamed"


@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("text", {"name": "renamed"}),
        ("pdf_text", {"version": "renamed"}),
        ("pdf_ocr", {"capabilities": ("renamed",)}),
    ],
)
def test_builtin_adapter_metadata_is_not_constructor_configurable(name: str, options: dict) -> None:
    with pytest.raises(ValueError, match="run.adapter_options"):
        load_adapter(name, options)


def test_pdf_alias_is_preserved() -> None:
    adapter = load_adapter("pdf")
    assert isinstance(adapter, PdfTextAdapter)
    assert adapter.name == "pdf_text"


# =========================================================================
# Adapter conformance — malformed adapters
# =========================================================================


def test_conformance_reports_missing_name() -> None:
    @dataclass
    class BadAdapter:
        version: str = "0.1"
        deterministic: bool = True
        input_types: tuple[str, ...] = ("text",)
        output_types: tuple[str, ...] = ("text",)
        capabilities: tuple[str, ...] = ("local",)

        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    issues = adapter_conformance_check(BadAdapter())
    assert any("name" in i for i in issues)


def test_conformance_reports_non_string_capability() -> None:
    @dataclass
    class BadCapAdapter:
        name: str = "bad-cap"
        version: str = "0.1"
        deterministic: bool = False
        input_types: tuple[str, ...] = ("pdf",)
        output_types: tuple[str, ...] = ("text",)
        capabilities: tuple = (42,)

        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    issues = adapter_conformance_check(BadCapAdapter())
    assert any("capabilities" in i for i in issues)


def test_conformance_reports_missing_supports() -> None:
    @dataclass
    class NoSupportsAdapter:
        name: str = "no-supports"
        version: str = "0.1"
        deterministic: bool = False
        input_types: tuple[str, ...] = ("text",)
        output_types: tuple[str, ...] = ("text",)
        capabilities: tuple[str, ...] = ("local",)

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    issues = adapter_conformance_check(NoSupportsAdapter())
    assert any("supports" in i for i in issues)


def test_conformance_reports_missing_extract() -> None:
    @dataclass
    class NoExtractAdapter:
        name: str = "no-extract"
        version: str = "0.1"
        deterministic: bool = False
        input_types: tuple[str, ...] = ("text",)
        output_types: tuple[str, ...] = ("text",)
        capabilities: tuple[str, ...] = ("local",)

        def supports(self, action: str) -> bool:
            return True

    issues = adapter_conformance_check(NoExtractAdapter())
    assert any("extract" in i for i in issues)


def test_conformance_and_loading_reject_noncallable_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BadPageCountAdapter:
        name = "bad-page-count"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)
        page_count = 2

        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    module = types.ModuleType("bad_page_count_adapter")
    module.ADAPTER = BadPageCountAdapter()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert any("page_count" in issue for issue in adapter_conformance_check(module.ADAPTER))
    with pytest.raises(ValueError, match="page_count"):
        load_adapter(f"{module.__name__}:ADAPTER")


def test_conformance_reports_noncallable_reproducibility_profile() -> None:
    class BadProfileAdapter:
        name = "bad-profile"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)
        reproducibility_profile = {"materials": []}

        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    issues = adapter_conformance_check(BadProfileAdapter())
    assert any("reproducibility_profile" in issue for issue in issues)


def test_custom_adapter_missing_metadata_is_not_filled_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnderSpecifiedAdapter:
        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    module = types.ModuleType("under_specified_adapter")
    module.ADAPTER = UnderSpecifiedAdapter()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(ValueError, match="missing required attribute 'name'"):
        load_adapter(f"{module.__name__}:ADAPTER")
    assert not hasattr(module.ADAPTER, "name")


def test_custom_factory_is_constructed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class FactoryAdapter:
        name = "factory"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1},
            )

    def build_adapter() -> FactoryAdapter:
        nonlocal calls
        calls += 1
        return FactoryAdapter()

    module = types.ModuleType("factory_adapter")
    module.build_adapter = build_adapter
    monkeypatch.setitem(sys.modules, module.__name__, module)

    adapter = load_adapter(f"{module.__name__}:build_adapter")

    assert adapter.name == "factory"
    assert calls == 1


def test_conformance_clean_adapter_empty_issues() -> None:
    @dataclass
    class CleanAdapter:
        name: str = "clean"
        version: str = "1.0"
        deterministic: bool = True
        input_types: tuple[str, ...] = ("pdf", "image")
        output_types: tuple[str, ...] = ("text", "markdown")
        capabilities: tuple[str, ...] = ("ocr", "local")

        def supports(self, action: str) -> bool:
            return action == "transcribe_text"

        def page_count(self, source: Path) -> int:
            return 1

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test",
                format="text",
                confidence=0.9,
                model="test-model",
                warnings=[],
                usage={"pages": 1},
            )

    issues = adapter_conformance_check(CleanAdapter())
    assert issues == []


# =========================================================================
# Metadata validation — load_adapter rejects bad types
# =========================================================================


def test_load_adapter_rejects_non_bool_deterministic(tmp_path: Path) -> None:
    _write_adapter_module(
        tmp_path,
        "nonbool",
        """\
from dataclasses import dataclass
from pageledger.adapters import ExtractionResult

@dataclass
class BadDeterministicAdapter:
    name: str = "bad-det"
    version: str = "1.0"
    deterministic: str = "yes"
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("local",)

    def supports(self, action):
        return True

    def extract(self, **kw):
        return ExtractionResult(
            content="test", format="text", confidence=None,
            model=None, warnings=[], usage={"pages": 1},
        )
""",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(ValueError, match="deterministic"):
            load_adapter("nonbool:BadDeterministicAdapter")
    finally:
        sys.path.pop(0)


def test_load_adapter_rejects_non_sequence_capabilities(tmp_path: Path) -> None:
    _write_adapter_module(
        tmp_path,
        "badseq",
        """\
from dataclasses import dataclass
from pageledger.adapters import ExtractionResult

@dataclass
class BadSeqAdapter:
    name: str = "bad-seq"
    version: str = "1.0"
    deterministic: bool = False
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: str = "ocr"

    def supports(self, action):
        return True

    def extract(self, **kw):
        return ExtractionResult(
            content="test", format="text", confidence=None,
            model=None, warnings=[], usage={"pages": 1},
        )
""",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(ValueError, match="capabilities"):
            load_adapter("badseq:BadSeqAdapter")
    finally:
        sys.path.pop(0)


# =========================================================================
# usage.pages must be exactly 1 at extraction time
# =========================================================================


def test_runner_rejects_usage_pages_not_one(tmp_path: Path) -> None:
    _write_adapter_module(
        tmp_path,
        "misreport",
        """\
from dataclasses import dataclass
from pathlib import Path
from pageledger.adapters import ExtractionResult

@dataclass
class MisreportingAdapter:
    name: str = "misreporting"
    version: str = "1.0"
    deterministic: bool = False
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("local",)

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        return ExtractionResult(
            content="test", format="text", confidence=None,
            model=None, warnings=[], usage={"pages": 2},
        )
""",
    )
    _run_and_assert_error(
        tmp_path,
        "misreport:MisreportingAdapter",
        "ValueError: <redacted>",
    )


def test_runner_rejects_usage_pages_zero(tmp_path: Path) -> None:
    _write_adapter_module(
        tmp_path,
        "zeropg",
        """\
from dataclasses import dataclass
from pathlib import Path
from pageledger.adapters import ExtractionResult

@dataclass
class ZeroPageAdapter:
    name: str = "zero-page"
    version: str = "1.0"
    deterministic: bool = False
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("local",)

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        return ExtractionResult(
            content="test", format="text", confidence=None,
            model=None, warnings=[], usage={"pages": 0},
        )
""",
    )
    _run_and_assert_error(
        tmp_path,
        "zeropg:ZeroPageAdapter",
        "ValueError: <redacted>",
    )


@pytest.mark.parametrize(
    "field",
    ["tokens", "compute_seconds", "cost_usd"],
)
def test_runner_rejects_negative_usage_values(field: str) -> None:
    from pageledger import runner as runner_module

    usage = {
        "pages": 1,
        "tokens": None,
        "compute_seconds": None,
        "cost_usd": None,
    }
    usage[field] = -1
    result = ExtractionResult(
        content="test",
        format="text",
        confidence=None,
        model=None,
        warnings=[],
        usage=usage,
    )

    with pytest.raises(ValueError, match=rf"usage\.{field}.*non-negative"):
        runner_module._validate_extraction_result("negative-usage", result)


# =========================================================================
# Custom adapter: no-arg import string still works
# =========================================================================


def test_custom_adapter_no_arg_class(tmp_path: Path) -> None:
    _write_adapter_module(
        tmp_path,
        "myocr",
        """\
from dataclasses import dataclass
from pathlib import Path
from pageledger.adapters import ExtractionResult

@dataclass
class MyOcrAdapter:
    name: str = "my-ocr"
    version: str = "1.0"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("ocr", "local")

    def supports(self, action):
        return action == "transcribe_text"

    def page_count(self, source: Path) -> int:
        return source.read_text(encoding="utf-8").count("\\f") + 1

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        pages = source.read_text(encoding="utf-8").split("\\f")
        text = pages[page_number - 1] if 0 < page_number <= len(pages) else ""
        return ExtractionResult(
            content=text, format="text", confidence=0.95,
            model="my-model", warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )
""",
    )
    _run_and_assert_success(tmp_path, "myocr:MyOcrAdapter", pages_total=2, pages_extracted=2)


def test_custom_adapter_page_count_invalid_raises(tmp_path: Path) -> None:
    _write_adapter_module(
        tmp_path,
        "badpc",
        """\
from dataclasses import dataclass
from pathlib import Path
from pageledger.adapters import ExtractionResult

@dataclass
class BadPageCountAdapter:
    name: str = "bad-pc"
    version: str = "1.0"
    deterministic: bool = False
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("local",)

    def supports(self, action):
        return action == "transcribe_text"

    def page_count(self, source: Path) -> int:
        return 0

    def extract(self, **kw):
        return ExtractionResult(
            content="test", format="text", confidence=None,
            model=None, warnings=[], usage={"pages": 1},
        )
""",
    )
    _run_and_assert_error(tmp_path, "badpc:BadPageCountAdapter", "page_count")


# =========================================================================
# Built-in pdf_ocr adapter
# =========================================================================


def test_pdf_ocr_adapter_passes_conformance() -> None:
    issues = adapter_conformance_check(PdfOcrAdapter())
    assert issues == []


def test_pdf_ocr_rejects_bad_dpi() -> None:
    with pytest.raises(ValueError, match="run.adapter_options.dpi"):
        PdfOcrAdapter(dpi=10)
    with pytest.raises(ValueError, match="run.adapter_options.dpi"):
        PdfOcrAdapter(dpi="300")  # type: ignore[arg-type]


def test_pdf_ocr_rejects_bad_lang() -> None:
    with pytest.raises(ValueError, match="run.adapter_options.lang"):
        PdfOcrAdapter(lang="eng; rm -rf /")
    with pytest.raises(ValueError, match="run.adapter_options.lang"):
        PdfOcrAdapter(lang="")


def test_pdf_ocr_accepts_multi_language() -> None:
    adapter = PdfOcrAdapter(dpi=400, lang="eng+rus")
    assert adapter.dpi == 400
    assert adapter.lang == "eng+rus"


def test_load_adapter_passes_options_to_pdf_ocr() -> None:
    adapter = load_adapter("pdf_ocr", {"dpi": 400, "lang": "deu"})
    assert adapter.dpi == 400
    assert adapter.lang == "deu"


def test_load_adapter_rejects_options_for_text_adapter() -> None:
    with pytest.raises(ValueError, match="run.adapter_options"):
        load_adapter("text", {"dpi": 300})


def test_pdf_ocr_extract_with_mocked_binaries(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_which(name: str) -> str:
        return f"/fake/bin/{name}"

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        binary = Path(argv[0]).name
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="tesseract 5.5.2\n", stderr="")
        if binary == "pdftoppm" and "-v" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="", stderr="pdftoppm version 26.05.0\n"
            )
        if "--list-langs" in argv:
            # Listing unavailable — the language preflight skips itself.
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        calls.append(list(argv))
        if binary == "pdftoppm":
            prefix = Path(argv[-1])
            (prefix.parent / "page-1.png").write_bytes(b"png")
        elif binary == "tesseract":
            output_prefix = Path(argv[2])
            output_prefix.with_suffix(".txt").write_text("OCR TEXT\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(adapters_module.shutil, "which", fake_which)
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)

    adapter = PdfOcrAdapter(dpi=400, lang="eng+deu")
    result = adapter.extract(
        tmp_path / "scan.pdf",
        page_id="doc_0001_page_0003",
        page_number=3,
        action="transcribe_text",
    )

    assert result.content == "OCR TEXT\n"
    assert result.model == ("tesseract 5.5.2; pdftoppm version 26.05.0; dpi=400; lang=eng+deu")
    assert result.format == "text"
    assert result.usage["pages"] == 1
    assert result.usage["compute_seconds"] is not None
    assert result.usage["cost_usd"] is None

    renders = [c for c in calls if Path(c[0]).name == "pdftoppm" and "-png" in c]
    # The first render is the 10-DPI size probe; the last one is the OCR image.
    assert renders[0][renders[0].index("-r") + 1] == "10"
    pdftoppm_call = renders[-1]
    assert pdftoppm_call[pdftoppm_call.index("-r") + 1] == "400"
    assert ["-f", "3", "-l", "3"] == pdftoppm_call[1:5]
    tesseract_call = next(c for c in calls if Path(c[0]).name == "tesseract" and "tsv" in c)
    assert tesseract_call[-4:] == ["-l", "eng+deu", "txt", "tsv"]


def test_pdf_ocr_extract_missing_binary_points_at_doctor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: None)
    adapter = PdfOcrAdapter()
    with pytest.raises(RuntimeError, match="pageledger doctor"):
        adapter.extract(
            tmp_path / "scan.pdf",
            page_id="doc_0001_page_0001",
            page_number=1,
            action="transcribe_text",
        )


def test_pdf_ocr_extract_surfaces_subprocess_stderr(tmp_path: Path, monkeypatch) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)
    adapter = PdfOcrAdapter()
    with pytest.raises(RuntimeError, match="boom"):
        adapter.extract(
            tmp_path / "scan.pdf",
            page_id="doc_0001_page_0001",
            page_number=1,
            action="transcribe_text",
        )


def test_pdf_ocr_rejects_unsupported_action(tmp_path: Path) -> None:
    adapter = PdfOcrAdapter()
    with pytest.raises(ValueError, match="does not support action"):
        adapter.extract(
            tmp_path / "scan.pdf",
            page_id="doc_0001_page_0001",
            page_number=1,
            action="summarize",
        )


def test_ocr_pdf_page_count_uses_pdfinfo(tmp_path: Path, monkeypatch) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(
            argv, 0, stdout="Title: x\nPages:          107\n", stderr=""
        )

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)
    assert ocr_pdf_page_count(tmp_path / "scan.pdf") == 107


_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj
trailer << /Root 1 0 R >>
"""


@pytest.mark.skipif(
    not (adapters_module.shutil.which("tesseract") and adapters_module.shutil.which("pdftoppm")),
    reason="tesseract and pdftoppm not installed",
)
def test_pdf_ocr_real_binaries_smoke(tmp_path: Path) -> None:
    pdf = tmp_path / "blank.pdf"
    pdf.write_bytes(_MINIMAL_PDF)
    adapter = PdfOcrAdapter(dpi=100)
    result = adapter.extract(
        pdf,
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )
    assert result.usage["pages"] == 1
    assert isinstance(result.content, str)
    assert result.model and result.model.startswith("tesseract")


def test_ocr_pdf_page_count_error_names_both_installs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        adapters_module,
        "_pdf_page_count",
        lambda source: (_ for _ in ()).throw(ValueError("pypdf missing")),
    )
    with pytest.raises(ValueError, match="poppler"):
        ocr_pdf_page_count(tmp_path / "scan.pdf")


# =========================================================================
# Example adapters compile and pass conformance
# =========================================================================


def test_tesseract_example_passes_conformance() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "examples"))
    try:
        from tesseract_pdftoppm_adapter import TesseractPdftoppmAdapter

        issues = adapter_conformance_check(TesseractPdftoppmAdapter())
        assert issues == []
    finally:
        sys.path.pop(0)
        for mod in list(sys.modules):
            if "tesseract" in mod.lower():
                del sys.modules[mod]


def test_cloud_vlm_example_passes_conformance() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "examples"))
    try:
        from cloud_vlm_adapter_skeleton import CloudVlmAdapter

        issues = adapter_conformance_check(CloudVlmAdapter())
        assert issues == []
    finally:
        sys.path.pop(0)
        for mod in list(sys.modules):
            if "cloud_vlm" in mod.lower():
                del sys.modules[mod]


# =========================================================================
# Helpers
# =========================================================================


def _write_adapter_module(tmp_path: Path, module_name: str, source: str) -> Path:
    """Write a custom adapter module as a .py file, return the module path."""
    path = tmp_path / f"{module_name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def _run_and_assert_success(
    tmp_path: Path,
    adapter_spec: str,
    *,
    pages_total: int,
    pages_extracted: int,
) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(
        textwrap.dedent(f"""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: {adapter_spec}
        """),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pageledger",
            "run",
            str(source),
            "--config",
            str(config),
            "--out",
            str(out_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, f"Failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    r = json.loads(result.stdout)
    assert r["summary"]["pages_total"] == pages_total
    assert r["summary"]["pages_extracted"] == pages_extracted


def _run_and_assert_error(
    tmp_path: Path,
    adapter_spec: str,
    expected_in_error: str,
) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(
        textwrap.dedent(f"""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: {adapter_spec}
        """),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pageledger",
            "run",
            str(source),
            "--config",
            str(config),
            "--out",
            str(out_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 1, (
        f"Expected exit 1, got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    error_json = json.loads(result.stdout)
    assert expected_in_error in error_json["error"], (
        f"Expected '{expected_in_error}' in error, got: {error_json['error']}"
    )


# ---------------------------------------------------------------------------
# pdf_ocr word confidence (Tesseract TSV)
# ---------------------------------------------------------------------------

_TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
    "\tleft\ttop\twidth\theight\tconf\ttext\n"
)


def _fake_ocr_binaries(monkeypatch, *, tsv_body: str | None, txt: str = "OCR TEXT\n"):
    """Mock pdftoppm/tesseract; tesseract writes txt plus tsv when given."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        binary = Path(argv[0]).name
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="tesseract 5.5.2\n", stderr="")
        if binary == "pdftoppm" and "-v" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="", stderr="pdftoppm version 26.05.0\n"
            )
        if "--list-langs" in argv:
            # Listing unavailable — the language preflight skips itself.
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        calls.append(list(argv))
        if binary == "pdftoppm":
            prefix = Path(argv[-1])
            (prefix.parent / "page-1.png").write_bytes(b"png")
        elif binary == "tesseract":
            output_prefix = Path(argv[2])
            output_prefix.with_suffix(".txt").write_text(txt, encoding="utf-8")
            if tsv_body is not None:
                output_prefix.with_suffix(".tsv").write_text(tsv_body, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)
    return calls


def _extract_one(adapter, tmp_path):
    return adapter.extract(
        tmp_path / "scan.pdf",
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )


def test_pdf_ocr_reports_word_confidence_from_tsv(tmp_path: Path, monkeypatch) -> None:
    tsv = _TSV_HEADER + (
        "1\t1\t0\t0\t0\t0\t0\t0\t1000\t1500\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t100\t100\t80\t20\t96.5\tHello\n"
        "5\t1\t1\t1\t1\t2\t200\t100\t80\t20\t42.1\tw0rld\n"
        "5\t1\t1\t1\t1\t3\t300\t100\t80\t20\t91.0\tagain\n"
    )
    calls = _fake_ocr_binaries(monkeypatch, tsv_body=tsv)
    result = _extract_one(PdfOcrAdapter(), tmp_path)

    tesseract_call = next(c for c in calls if Path(c[0]).name == "tesseract" and "tsv" in c)
    assert tesseract_call[-2:] == ["txt", "tsv"]
    assert result.confidence == pytest.approx(0.7653, abs=1e-4)
    detail = result.confidence_detail
    assert detail["word_count"] == 3
    assert detail["mean"] == pytest.approx(76.53, abs=0.01)
    assert detail["min"] == pytest.approx(42.1)
    assert detail["below_60_count"] == 1
    assert detail["below_60_ratio"] == pytest.approx(1 / 3, abs=1e-4)


def test_pdf_ocr_confidence_none_when_tsv_missing(tmp_path: Path, monkeypatch) -> None:
    _fake_ocr_binaries(monkeypatch, tsv_body=None)
    result = _extract_one(PdfOcrAdapter(), tmp_path)
    assert result.confidence is None
    assert result.confidence_detail is None


def test_pdf_ocr_confidence_none_when_tsv_has_no_words(tmp_path: Path, monkeypatch) -> None:
    tsv = _TSV_HEADER + "1\t1\t0\t0\t0\t0\t0\t0\t1000\t1500\t-1\t\n"
    _fake_ocr_binaries(monkeypatch, tsv_body=tsv)
    result = _extract_one(PdfOcrAdapter(), tmp_path)
    assert result.confidence is None
    assert result.confidence_detail is None


def test_pdf_ocr_confidence_survives_malformed_tsv(tmp_path: Path, monkeypatch) -> None:
    _fake_ocr_binaries(monkeypatch, tsv_body="not\ta\ttsv\nat all")
    result = _extract_one(PdfOcrAdapter(), tmp_path)
    assert result.content == "OCR TEXT\n"
    assert result.confidence is None


# ---------------------------------------------------------------------------
# pdf_ocr language preflight
# ---------------------------------------------------------------------------


def _fake_binaries_with_langs(monkeypatch, langs: list[str]):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        binary = Path(argv[0]).name
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="tesseract 5.5.2\n", stderr="")
        if binary == "pdftoppm" and "-v" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="", stderr="pdftoppm version 26.05.0\n"
            )
        if "--list-langs" in argv:
            listed = "\n".join(langs)
            body = f'List of available languages in "/fake/tessdata/" ({len(langs)}):\n{listed}\n'
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        calls.append(list(argv))
        if binary == "pdftoppm":
            prefix = Path(argv[-1])
            (prefix.parent / "page-1.png").write_bytes(b"png")
        elif binary == "tesseract":
            output_prefix = Path(argv[2])
            output_prefix.with_suffix(".txt").write_text("OCR TEXT\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)
    return calls


def test_pdf_ocr_rejects_missing_language_pack(tmp_path: Path, monkeypatch) -> None:
    _fake_binaries_with_langs(monkeypatch, ["eng", "osd"])
    adapter = PdfOcrAdapter(lang="rus")
    with pytest.raises(RuntimeError) as excinfo:
        _extract_one(adapter, tmp_path)
    message = str(excinfo.value)
    assert "rus" in message
    assert "eng" in message  # names what IS installed
    assert "doctor" in message


def test_pdf_ocr_accepts_installed_language_pack(tmp_path: Path, monkeypatch) -> None:
    _fake_binaries_with_langs(monkeypatch, ["eng", "rus", "osd"])
    result = _extract_one(PdfOcrAdapter(lang="eng+rus"), tmp_path)
    assert result.content == "OCR TEXT\n"


def test_pdf_ocr_skips_lang_check_when_listing_fails(tmp_path: Path, monkeypatch) -> None:
    """An unparseable --list-langs must not block extraction."""

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        binary = Path(argv[0]).name
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="tesseract 5.5.2\n", stderr="")
        if binary == "pdftoppm" and "-v" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="", stderr="pdftoppm version 26.05.0\n"
            )
        if "--list-langs" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="weird failure")
        if binary == "pdftoppm":
            (Path(argv[-1]).parent / "page-1.png").write_bytes(b"png")
        elif binary == "tesseract":
            Path(argv[2]).with_suffix(".txt").write_text("OCR TEXT\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)
    result = _extract_one(PdfOcrAdapter(lang="rus"), tmp_path)
    assert result.content == "OCR TEXT\n"


# ---------------------------------------------------------------------------
# examples/prereform_normalizer_adapter.py
# ---------------------------------------------------------------------------


def _load_prereform_example():
    examples_dir = Path(__file__).resolve().parents[2] / "examples"
    sys.path.insert(0, str(examples_dir))
    try:
        import prereform_normalizer_adapter

        return prereform_normalizer_adapter
    finally:
        sys.path.pop(0)


def test_prereform_normalization_rules() -> None:
    module = _load_prereform_example()
    cases = {
        "съѣздъ": "съезд",  # keep morphological ъ, drop final, ѣ→е
        "городъ.": "город.",  # final ъ before punctuation
        "подъёмъ": "подъём",  # medial ъ before vowel kept
        "объектъ": "объект",
        "уѣздъ": "уезд",
        "ѳита и ѵжица": "фита и ижица",
        "Бѣлгородъ": "Белгород",
        "мир": "мир",  # modern text untouched
    }
    for original, expected in cases.items():
        normalized, _ = module.normalize_orthography(original)
        assert normalized == expected, original
    _, replacements = module.normalize_orthography("уѣздъ")
    assert replacements == 2  # ѣ→е plus dropped final ъ


def test_prereform_adapter_normalizes_and_records(tmp_path: Path, monkeypatch) -> None:
    module = _load_prereform_example()
    _fake_ocr_binaries(monkeypatch, tsv_body=None, txt="Харьковскій уѣздъ\n")

    adapter = module.PrereformNormalizerAdapter(dpi=200, lang="rus")
    result = _extract_one(adapter, tmp_path)

    assert result.content == "Харьковский уезд\n"
    assert any(w.startswith("prereform_normalization_applied:") for w in result.warnings)
    assert "prereform-normalizer" in result.model


def test_prereform_adapter_passes_conformance() -> None:
    module = _load_prereform_example()
    issues = adapter_conformance_check(module.PrereformNormalizerAdapter())
    assert issues == []


def test_tesseract_tsv_table_example_passes_conformance() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "examples"))
    try:
        from tesseract_tsv_table_adapter import TesseractTsvTableAdapter

        issues = adapter_conformance_check(TesseractTsvTableAdapter())
        assert issues == []
    finally:
        sys.path.pop(0)
        for mod in list(sys.modules):
            if "tesseract" in mod.lower():
                del sys.modules[mod]


def test_tesseract_tsv_example_rejects_invalid_options() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples"))
    try:
        from tesseract_tsv_table_adapter import TesseractTsvTableAdapter

        with pytest.raises(ValueError, match="dpi"):
            TesseractTsvTableAdapter(dpi=True)
        with pytest.raises(ValueError, match="lang"):
            TesseractTsvTableAdapter(lang="eng; command")
        with pytest.raises(TypeError):
            TesseractTsvTableAdapter(name="dishonest")
    finally:
        sys.path.pop(0)


def test_tesseract_tsv_example_rejects_malformed_header() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples"))
    try:
        from tesseract_tsv_table_adapter import _parse_tsv_words

        with pytest.raises(ValueError, match="missing columns"):
            _parse_tsv_words("level\ttext\n5\tword\n")
    finally:
        sys.path.pop(0)


def test_local_llm_example_rejects_invalid_generation_options() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples"))
    try:
        from local_llm_cleanup_adapter import LocalLlmCleanupAdapter

        with pytest.raises(ValueError, match="max_tokens"):
            LocalLlmCleanupAdapter(max_tokens=True)
        with pytest.raises(ValueError, match="temperature"):
            LocalLlmCleanupAdapter(temperature=float("nan"))
    finally:
        sys.path.pop(0)


def _load_ollama_example():
    examples_dir = Path(__file__).resolve().parents[2] / "examples"
    sys.path.insert(0, str(examples_dir))
    try:
        import ollama_cleanup_adapter

        return ollama_cleanup_adapter
    finally:
        sys.path.pop(0)


def test_ollama_cleanup_example_passes_conformance() -> None:
    module = _load_ollama_example()
    assert adapter_conformance_check(module.OllamaCleanupAdapter()) == []


def test_ollama_cleanup_extract_strips_thoughts_and_records_tokens(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_ollama_example()
    _fake_ocr_binaries(monkeypatch, tsv_body=None, txt="ClA controlled\n")
    adapter = module.OllamaCleanupAdapter(model="test-model")
    monkeypatch.setattr(
        adapter,
        "_generate",
        lambda prompt: ("<think>fix letters</think>CIA-controlled\n", 37),
    )

    result = _extract_one(adapter, tmp_path)

    assert result.content == "CIA-controlled"
    assert result.usage["tokens"] == 37
    assert "thought_block_stripped" in result.warnings
    assert result.model.endswith("ollama:test-model")


def test_ollama_generate_uses_non_streaming_api_and_real_token_counts(
    monkeypatch,
) -> None:
    module = _load_ollama_example()
    captured: dict = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {
                    "response": "Clean text",
                    "prompt_eval_count": 11,
                    "eval_count": 7,
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    adapter = module.OllamaCleanupAdapter(
        model="test-model",
        base_url="http://ollama.test/",
        max_tokens=123,
        temperature=0.2,
        timeout_seconds=9,
    )

    completion, tokens = adapter._generate("OCR text")

    assert (completion, tokens) == ("Clean text", 18)
    assert captured["url"] == "http://ollama.test/api/generate"
    assert captured["payload"] == {
        "model": "test-model",
        "prompt": "OCR text",
        "stream": False,
        "think": False,
        "options": {"num_predict": 123, "temperature": 0.2},
    }
    assert captured["timeout"] == 9.0


def test_tsv_table_clustering_builds_markdown_table() -> None:
    """Words on TSV lines become rows; wide horizontal gaps become columns."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "examples"))
    try:
        from tesseract_tsv_table_adapter import _cluster_table, _parse_tsv_words

        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
        rows = [
            # header line: two cells separated by a wide gap
            "5\t1\t1\t1\t1\t1\t10\t10\t60\t20\t95\tplace",
            "5\t1\t1\t1\t1\t2\t300\t10\t60\t20\t96\ttotal",
            # data line
            "5\t1\t1\t1\t2\t1\t10\t50\t80\t20\t91\tMoscow",
            "5\t1\t1\t1\t2\t2\t300\t50\t80\t20\t88\t4137000",
            # rejected: level 4 and conf -1 rows
            "4\t1\t1\t1\t3\t0\t0\t90\t500\t20\t-1\t",
        ]
        words = _parse_tsv_words("\n".join([header, *rows]))
        assert len(words) == 4
        table = _cluster_table(words)
        assert table.splitlines()[0] == "| place | total |"
        assert "| Moscow | 4137000 |" in table
    finally:
        sys.path.pop(0)
        for mod in list(sys.modules):
            if "tesseract" in mod.lower():
                del sys.modules[mod]


def test_tsv_table_clustering_accepts_scaled_gap() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples"))
    try:
        from tesseract_tsv_table_adapter import _cluster_table

        words = [
            {"row_key": (1, 1, 1), "top": 0, "left": 0, "right": 20, "text": "A"},
            {"row_key": (1, 1, 1), "top": 0, "left": 51, "right": 70, "text": "B"},
        ]
        assert _cluster_table(words, column_gap_px=30).splitlines()[0] == "| A | B |"
        assert _cluster_table(words, column_gap_px=40).splitlines()[0] == "| A B |"
    finally:
        sys.path.pop(0)


def test_strip_thought_blocks_variants() -> None:
    """Both channel-marker spellings strip; unterminated thought yields empty."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "examples"))
    try:
        from local_llm_cleanup_adapter import strip_thought_blocks

        # two-pipe form with final channel
        text, seen = strip_thought_blocks(
            "<|channel|>thought reasoning here <|channel|>final The answer."
        )
        assert (text, seen) == ("The answer.", True)
        # one-pipe form (Gemma) with answer channel
        text, seen = strip_thought_blocks(
            "<|channel>thought reasoning <|channel>answer Cleaned text."
        )
        assert (text, seen) == ("Cleaned text.", True)
        # <think> form
        text, seen = strip_thought_blocks("<think>hmm</think>Result")
        assert (text, seen) == ("Result", True)
        # unterminated thought: whole budget burned reasoning -> empty answer
        text, seen = strip_thought_blocks(
            "<|channel>thought endless reasoning that never reaches an answer"
        )
        assert (text, seen) == ("", True)
        # plain answer untouched
        text, seen = strip_thought_blocks("Just the transcription.")
        assert (text, seen) == ("Just the transcription.", False)
    finally:
        sys.path.pop(0)
        for mod in list(sys.modules):
            if "local_llm" in mod.lower():
                del sys.modules[mod]


# ---------------------------------------------------------------------------
# Typed diagnostics and preflight
# ---------------------------------------------------------------------------


def _one_page_pdf(path: Path) -> Path:
    writer = pytest.importorskip("pypdf").PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_pdf_ocr_preflight_reports_missing_language_pack(tmp_path: Path, monkeypatch) -> None:
    from pageledger.adapters import PageLedgerDiagnostic

    _fake_binaries_with_langs(monkeypatch, ["eng", "osd"])
    with pytest.raises(PageLedgerDiagnostic) as excinfo:
        PdfOcrAdapter(lang="eng+rus").preflight([_one_page_pdf(tmp_path / "scan.pdf")])
    assert excinfo.value.code == "missing_language_pack"
    assert "rus" in str(excinfo.value) and "eng" in str(excinfo.value)


def test_pdf_ocr_preflight_reports_missing_binary(tmp_path: Path, monkeypatch) -> None:
    from pageledger.adapters import PageLedgerDiagnostic

    monkeypatch.setattr(
        adapters_module.shutil, "which", lambda name: None if name == "pdftoppm" else f"/x/{name}"
    )
    with pytest.raises(PageLedgerDiagnostic) as excinfo:
        PdfOcrAdapter().preflight([_one_page_pdf(tmp_path / "scan.pdf")])
    assert excinfo.value.code == "missing_binary"
    assert "pdftoppm" in str(excinfo.value) and "Poppler" in str(excinfo.value)


def test_run_refuses_missing_language_pack_before_any_page(tmp_path: Path, monkeypatch) -> None:
    from pageledger.adapters import PageLedgerDiagnostic
    from pageledger.runner import run

    _fake_binaries_with_langs(monkeypatch, ["eng", "osd"])
    pdf = _one_page_pdf(tmp_path / "scan.pdf")
    config = tmp_path / "ocr.yml"
    config.write_text(
        'schema_version: "0.1"\ntaxonomy:\n  page_types:\n    prose:\n'
        "      default_action: transcribe_text\nrun:\n  adapter: pdf_ocr\n"
        "  adapter_options:\n    lang: rus\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    with pytest.raises(PageLedgerDiagnostic) as excinfo:
        run(inputs=[pdf], config_path=config, out_dir=out_dir, dry_run=False)
    assert excinfo.value.code == "missing_language_pack"
    assert not (out_dir / "raw").exists()


def test_built_in_diagnostics_survive_redaction_but_custom_errors_do_not() -> None:
    from pageledger.adapters import PageLedgerDiagnostic
    from pageledger.runner import AdapterExecutionError

    diagnostic = PageLedgerDiagnostic(
        "missing_binary", "pdftoppm is not installed; install Poppler"
    )
    kept = AdapterExecutionError(
        adapter="pdf_ocr",
        page_id="doc_0001_page_0001",
        status="failed",
        message=str(diagnostic),
        trusted=True,
    )
    assert "install Poppler" in str(kept)
    redacted = AdapterExecutionError(
        adapter="custom",
        page_id="doc_0001_page_0001",
        status="failed",
        message="RuntimeError: token sk-secret",
    )
    assert "sk-secret" not in str(redacted)


def test_diagnostic_codes_are_allowlisted() -> None:
    from pageledger.adapters import PageLedgerDiagnostic

    with pytest.raises(ValueError):
        PageLedgerDiagnostic("made_up_code", "anything")


# ---------------------------------------------------------------------------
# Render safety
# ---------------------------------------------------------------------------


def test_render_dpi_caps_oversized_pages(monkeypatch) -> None:
    # A 10-dpi probe of a 1.75 m x 2.47 m page (Internet Archive derivative) is ~689 x 972 px.
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *a: (689, 972))
    dpi, capped = adapters_module._render_dpi("pdftoppm", Path("x.pdf"), 1, 300, 60_000_000)
    assert capped is True
    assert 72 <= dpi < 100
    assert (689 * dpi / 10) * (972 * dpi / 10) <= 60_000_000


def test_render_dpi_never_raises_resolution(monkeypatch) -> None:
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *a: (7, 11))
    assert adapters_module._render_dpi("pdftoppm", Path("x.pdf"), 1, 300, 60_000_000) == (
        300,
        False,
    )


def test_render_below_minimum_dpi_is_a_typed_refusal(monkeypatch) -> None:
    from pageledger.adapters import PageLedgerDiagnostic

    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *a: (4000, 4000))
    with pytest.raises(PageLedgerDiagnostic) as excinfo:
        adapters_module._render_dpi("pdftoppm", Path("x.pdf"), 1, 300, 60_000_000)
    assert excinfo.value.code == "render_limit"


def test_unreadable_probe_keeps_requested_dpi(monkeypatch) -> None:
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *a: None)
    assert adapters_module._render_dpi("pdftoppm", Path("x.pdf"), 1, 300, 60_000_000) == (
        300,
        False,
    )


def _image_pdf(path: Path, *, width: int = 440, height: int = 700, pages: int = 1) -> Path:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=18 * 72 / 25.4, height=29 * 72 / 25.4)
        image = DecodedStreamObject()
        image.set_data(b"\x00" * width * height)
        image.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(width),
                NameObject("/Height"): NumberObject(height),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): writer._add_object(image)}
                )
            }
        )
    writer.write(path)
    return path


def test_pdf_ocr_raises_render_dpi_to_embedded_image_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    pytest.importorskip("pypdf")
    calls = _fake_binaries_with_langs(monkeypatch, ["eng"])
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *args: (7, 12))
    pdf = _image_pdf(tmp_path / "small-page.pdf")
    result = PdfOcrAdapter().extract(
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text"
    )
    render = [call for call in calls if Path(call[0]).name == "pdftoppm" and "-png" in call][-1]
    dpi = render[render.index("-r") + 1]
    assert int(dpi) == 621
    assert "dpi=621 (requested 300, native image)" in result.model


def test_pdf_ocr_born_digital_keeps_requested_dpi(tmp_path: Path, monkeypatch) -> None:
    _fake_binaries_with_langs(monkeypatch, ["eng"])
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *args: (7, 12))
    pdf = _one_page_pdf(tmp_path / "digital.pdf")
    result = PdfOcrAdapter(dpi=300).extract(
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text"
    )
    assert result.model and "; dpi=300;" in result.model


def test_pdf_ocr_without_pypdf_keeps_requested_dpi(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("pypdf")
    calls = _fake_binaries_with_langs(monkeypatch, ["eng"])
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *args: (7, 12))
    pdf = _image_pdf(tmp_path / "small-page.pdf")
    monkeypatch.setitem(sys.modules, "pypdf", None)
    result = PdfOcrAdapter().extract(
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text"
    )
    render = [call for call in calls if Path(call[0]).name == "pdftoppm" and "-png" in call][-1]
    assert render[render.index("-r") + 1] == "300"
    assert "; dpi=300;" in result.model


def test_native_dpi_still_respects_pixel_cap(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("pypdf")
    calls = _fake_binaries_with_langs(monkeypatch, ["eng"])
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *args: (100, 100))
    pdf = _image_pdf(tmp_path / "capped.pdf", width=1200, height=1200)
    result = PdfOcrAdapter(max_render_pixels=1_000_000).extract(
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text"
    )
    render = [call for call in calls if Path(call[0]).name == "pdftoppm" and "-png" in call][-1]
    assert int(render[render.index("-r") + 1]) < 1200
    assert "render_dpi_capped" in result.warnings


def test_pdf_ocr_scans_a_source_once_for_all_its_pages(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("pypdf")
    _fake_binaries_with_langs(monkeypatch, ["eng"])
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *args: (7, 12))
    pdf = _image_pdf(tmp_path / "volume.pdf", pages=2)
    scans = []
    scan = adapters_module._page_image_ppis
    monkeypatch.setattr(
        adapters_module, "_page_image_ppis", lambda path: scans.append(path) or scan(path)
    )
    adapter = PdfOcrAdapter()
    for number in (1, 2):
        result = adapter.extract(
            pdf, page_id=f"doc_0001_page_{number:04d}", page_number=number, action="transcribe_text"
        )
        assert "native image" in result.model
    assert len(scans) == 1


def test_pdf_sources_are_read_once_and_refused_when_changed(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"first")
    reads = []
    cache: dict = {}

    def read(path: Path) -> str:
        reads.append(path)
        return path.read_text()

    assert adapters_module._read_once(cache, source, read) == "first"
    assert adapters_module._read_once(cache, source, read) == "first"
    assert len(reads) == 1
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        adapters_module._read_once(cache, source, read)


def test_pdf_ocr_records_capped_render(tmp_path: Path, monkeypatch) -> None:
    _fake_binaries_with_langs(monkeypatch, ["eng"])
    monkeypatch.setattr(adapters_module, "_probe_size_at_10dpi", lambda *a: (689, 972))
    result = _extract_one(PdfOcrAdapter(), tmp_path)
    assert "render_dpi_capped" in result.warnings
    assert "(requested 300)" in result.model


def test_max_render_pixels_is_validated() -> None:
    with pytest.raises(ValueError, match="max_render_pixels"):
        PdfOcrAdapter(max_render_pixels=10)


# ---------------------------------------------------------------------------
# Encrypted and damaged PDFs
# ---------------------------------------------------------------------------


def _text_pdf(path: Path, *, algorithm: str | None = None, user_password: str = "") -> Path:
    """One page reading 'Hello PageLedger', optionally encrypted by pypdf."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (Hello PageLedger) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    if algorithm is not None:
        writer.encrypt(user_password=user_password, owner_password="owner", algorithm=algorithm)
    writer.write(path)
    return path


def _lending_copy_pdf(path: Path) -> Path:
    """A PDF whose /Encrypt names a non-Standard handler, as Internet Archive lending copies do."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        b"<< /Filter /FOPN_foweb /V 1 /Length 40 >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    file_id = b"<00112233445566778899aabbccddeeff>"
    data += b"trailer\n<< /Size %d /Root 1 0 R /Encrypt 4 0 R /ID [%s %s] >>\n" % (
        len(objects) + 1,
        file_id,
        file_id,
    )
    data += b"startxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(data))
    return path


def _pdf_readers():
    return [PdfTextAdapter().page_count, adapters_module.pdf_page_count]


def test_pdf_extra_installs_aes_support() -> None:
    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pypdf[crypto]>=6"' in pyproject


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_pdf_with_only_an_owner_password_is_read(tmp_path: Path, algorithm: str) -> None:
    pytest.importorskip("pypdf")
    pytest.importorskip("cryptography")
    pdf = _text_pdf(tmp_path / "restricted.pdf", algorithm=algorithm)

    result = PdfTextAdapter().extract(
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text"
    )

    assert result.content.strip() == "Hello PageLedger"
    assert adapters_module.pdf_page_count(pdf) == 1


@pytest.mark.parametrize("failing_step", ["open", "pages"])
def test_missing_aes_support_is_a_typed_diagnostic(
    tmp_path: Path, monkeypatch, failing_step: str
) -> None:
    pypdf = pytest.importorskip("pypdf")
    from pageledger.adapters import PageLedgerDiagnostic

    def no_aes(*_args):
        raise pypdf.errors.DependencyError("cryptography>=3.1 is required for AES algorithm")

    class Reader:
        def __init__(self, stream, strict=False):
            if failing_step == "open":
                no_aes()

        pages = property(no_aes)

    monkeypatch.setattr(adapters_module, "_load_pypdf", lambda: Reader)
    pdf = _one_page_pdf(tmp_path / "aes.pdf")
    for read in _pdf_readers():
        with pytest.raises(PageLedgerDiagnostic) as excinfo:
            read(pdf)
        assert excinfo.value.code == "missing_crypto_dependency"
        assert "pageledger[pdf]" in str(excinfo.value)


def test_pdf_that_needs_a_password_is_refused(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    from pageledger.adapters import PageLedgerDiagnostic

    pdf = _text_pdf(tmp_path / "locked.pdf", algorithm="RC4-128", user_password="s3cret")
    for read in _pdf_readers():
        with pytest.raises(PageLedgerDiagnostic) as excinfo:
            read(pdf)
        assert excinfo.value.code == "unsupported_encryption"
        assert "password" in str(excinfo.value)


def test_unsupported_encryption_handler_is_refused(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    from pageledger.adapters import PageLedgerDiagnostic

    pdf = _lending_copy_pdf(tmp_path / "lending_encrypted.pdf")
    for read in _pdf_readers():
        with pytest.raises(PageLedgerDiagnostic) as excinfo:
            read(pdf)
        assert excinfo.value.code == "unsupported_encryption"
        assert "lending" in str(excinfo.value)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"hello world, not a pdf at all\n",
        b"%PDF-1.4\n1 0 obj<<>>endobj\nstartxref\n-926232656\n%%EOF\n",
    ],
    ids=["empty", "not-a-pdf", "negative-startxref"],
)
def test_damaged_pdf_is_a_typed_refusal(tmp_path: Path, monkeypatch, content: bytes) -> None:
    pytest.importorskip("pypdf")
    from pageledger.adapters import PageLedgerDiagnostic

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: None)
    pdf = tmp_path / "damaged.pdf"
    pdf.write_bytes(content)
    for read in [*_pdf_readers(), ocr_pdf_page_count]:
        with pytest.raises(PageLedgerDiagnostic) as excinfo:
            read(pdf)
        assert excinfo.value.code == "malformed_pdf"
        assert "damaged.pdf" in str(excinfo.value)


def test_damaged_pdf_run_reports_the_code_without_a_traceback(tmp_path: Path, capsys) -> None:
    pytest.importorskip("pypdf")
    from pageledger.cli import main

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"hello world, not a pdf at all\n")
    out_dir = tmp_path / "out"

    code = main(["run", str(pdf), "--adapter", "pdf_text", "--out", str(out_dir), "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["code"] == "malformed_pdf"
    assert "Traceback" not in captured.err
    assert not (out_dir / "raw").exists()


def test_hung_ocr_engine_is_a_typed_timeout(tmp_path: Path, monkeypatch) -> None:
    from pageledger.adapters import PageLedgerDiagnostic

    _fake_binaries_with_langs(monkeypatch, ["eng"])
    fake_run = adapters_module.subprocess.run

    def hang(argv, **kwargs):  # noqa: ANN001, ANN003
        if Path(argv[0]).name == "pdftoppm" and "-png" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return fake_run(argv, **kwargs)

    monkeypatch.setattr(adapters_module.subprocess, "run", hang)
    with pytest.raises(PageLedgerDiagnostic) as excinfo:
        _extract_one(PdfOcrAdapter(), tmp_path)
    assert excinfo.value.code == "engine_timeout"
    assert "timed out after 120s" in str(excinfo.value)
