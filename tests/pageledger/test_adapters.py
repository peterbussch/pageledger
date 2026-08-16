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


def test_pdf_text_records_pypdf_backend_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(adapters_module, "_pdf_page_text", lambda source, page: "text")
    result = PdfTextAdapter().extract(
        tmp_path / "input.pdf",
        page_id="doc_0001_page_0001",
        page_number=1,
        action="transcribe_text",
    )

    assert result.model == f"pypdf {importlib.metadata.version('pypdf')}"


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
def test_builtin_adapter_metadata_is_not_constructor_configurable(
    name: str, options: dict
) -> None:
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
                content="test", format="text", confidence=None,
                model=None, warnings=[], usage={"pages": 1},
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
                content="test", format="text", confidence=None,
                model=None, warnings=[], usage={"pages": 1},
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
                content="test", format="text", confidence=None,
                model=None, warnings=[], usage={"pages": 1},
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
                content="test", format="text", confidence=None,
                model=None, warnings=[], usage={"pages": 1},
            )

    module = types.ModuleType("bad_page_count_adapter")
    module.ADAPTER = BadPageCountAdapter()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert any(
        "page_count" in issue for issue in adapter_conformance_check(module.ADAPTER)
    )
    with pytest.raises(ValueError, match="page_count"):
        load_adapter(f"{module.__name__}:ADAPTER")


def test_custom_adapter_missing_metadata_is_not_filled_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnderSpecifiedAdapter:
        def supports(self, action: str) -> bool:
            return True

        def extract(self, **kw):  # noqa: ANN003
            return ExtractionResult(
                content="test", format="text", confidence=None,
                model=None, warnings=[], usage={"pages": 1},
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
                content="test", format="text", confidence=None,
                model=None, warnings=[], usage={"pages": 1},
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
                content="test", format="text", confidence=0.9,
                model="test-model", warnings=[], usage={"pages": 1},
            )

    issues = adapter_conformance_check(CleanAdapter())
    assert issues == []


# =========================================================================
# Metadata validation — load_adapter rejects bad types
# =========================================================================

def test_load_adapter_rejects_non_bool_deterministic(tmp_path: Path) -> None:
    _write_adapter_module(tmp_path, "nonbool", """\
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
""")
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(ValueError, match="deterministic"):
            load_adapter("nonbool:BadDeterministicAdapter")
    finally:
        sys.path.pop(0)


def test_load_adapter_rejects_non_sequence_capabilities(tmp_path: Path) -> None:
    _write_adapter_module(tmp_path, "badseq", """\
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
""")
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
    _write_adapter_module(tmp_path, "misreport", """\
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
""")
    _run_and_assert_error(
        tmp_path,
        "misreport:MisreportingAdapter",
        "ValueError: <redacted>",
    )


def test_runner_rejects_usage_pages_zero(tmp_path: Path) -> None:
    _write_adapter_module(tmp_path, "zeropg", """\
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
""")
    _run_and_assert_error(
        tmp_path,
        "zeropg:ZeroPageAdapter",
        "ValueError: <redacted>",
    )


# =========================================================================
# Custom adapter: no-arg import string still works
# =========================================================================

def test_custom_adapter_no_arg_class(tmp_path: Path) -> None:
    _write_adapter_module(tmp_path, "myocr", """\
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
""")
    _run_and_assert_success(tmp_path, "myocr:MyOcrAdapter", pages_total=2, pages_extracted=2)


def test_custom_adapter_page_count_invalid_raises(tmp_path: Path) -> None:
    _write_adapter_module(tmp_path, "badpc", """\
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
""")
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
    assert result.model == (
        "tesseract 5.5.2; pdftoppm version 26.05.0; dpi=400; lang=eng+deu"
    )
    assert result.format == "text"
    assert result.usage["pages"] == 1
    assert result.usage["compute_seconds"] is not None
    assert result.usage["cost_usd"] is None

    pdftoppm_call = calls[0]
    assert "-r" in pdftoppm_call and "400" in pdftoppm_call
    assert ["-f", "3", "-l", "3"] == pdftoppm_call[1:5]
    tesseract_call = calls[1]
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
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text",
    )
    assert result.usage["pages"] == 1
    assert isinstance(result.content, str)
    assert result.model and result.model.startswith("tesseract")


def test_ocr_pdf_page_count_error_names_both_installs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        adapters_module, "_pdf_page_count",
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
    config.write_text(textwrap.dedent(f"""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: {adapter_spec}
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(config), "--out", str(out_dir), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
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
    config.write_text(textwrap.dedent(f"""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: {adapter_spec}
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(config), "--out", str(out_dir), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert result.returncode == 1, f"Expected exit 1, got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
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

    tesseract_call = calls[1]
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
        if binary == "pdftoppm":
            prefix = Path(argv[-1])
            (prefix.parent / "page-1.png").write_bytes(b"png")
        elif binary == "tesseract":
            output_prefix = Path(argv[2])
            output_prefix.with_suffix(".txt").write_text("OCR TEXT\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(adapters_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(adapters_module.subprocess, "run", fake_run)


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
        "съѣздъ": "съезд",      # keep morphological ъ, drop final, ѣ→е
        "городъ.": "город.",    # final ъ before punctuation
        "подъёмъ": "подъём",    # medial ъ before vowel kept
        "объектъ": "объект",
        "уѣздъ": "уезд",
        "ѳита и ѵжица": "фита и ижица",
        "Бѣлгородъ": "Белгород",
        "мир": "мир",           # modern text untouched
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
