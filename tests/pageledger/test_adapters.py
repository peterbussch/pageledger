"""Phase 3 tests: adapter contract hardening, conformance, metadata validation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from pageledger.adapters import (
    ExtractionResult,
    TextAdapter,
    PdfTextAdapter,
    adapter_conformance_check,
    load_adapter,
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
    _run_and_assert_error(tmp_path, "misreport:MisreportingAdapter", "exactly 1")


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
    _run_and_assert_error(tmp_path, "zeropg:ZeroPageAdapter", "exactly 1")


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
