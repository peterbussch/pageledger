"""The built-in indra adapter, against a stand-in for the indra_ocr package."""

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pageledger.adapters import adapter_conformance_check, load_adapter


@dataclass
class Line:
    box: list
    text: str
    conf: float


@dataclass
class Page:
    lines: list
    markdown: str | None = None
    source: str = "ocr"
    note: str = "mode=ocr; order=model"
    blocks: list = field(default_factory=list)
    rotation: int = 0
    skew: float = 0.0
    vertical: bool = False


def _fake_indra(monkeypatch, tmp_path, page, seen=None):
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    for name in ("det.onnx", "rec.onnx", "order.onnx", "meta.json"):
        (models / name).write_bytes(name.encode())
    seen = seen if seen is not None else {}

    class Converter:
        def pdf_page(self, path, doc, index):
            seen["index"] = index
            return page

    def load(**kwargs):
        seen["load"] = kwargs
        return Converter()

    def model_dir(path=None, math=False):
        seen["model_dir"] = path
        return models

    indra = types.ModuleType("indra_ocr")
    indra.__version__ = "1.1.1"
    indra.load = load
    indra.model_dir = model_dir
    pdfium = types.ModuleType("pypdfium2")

    class PdfDocument:
        def __init__(self, path):
            self.path = path

        def __len__(self):
            return 3

        def close(self):
            seen["closed"] = True

    pdfium.PdfDocument = PdfDocument
    monkeypatch.setitem(sys.modules, "indra_ocr", indra)
    monkeypatch.setitem(sys.modules, "pypdfium2", pdfium)
    return seen


PAGE = Page(
    lines=[
        Line([10, 10, 200, 30], "Embassy of the U S S R", 0.97),
        Line([10, 40, 200, 60], "1119-16th nw NAtional-7550", 0.41),
        Line([10, 70, 200, 90], "  ", 0.2),
    ],
    markdown="## Embassy of the U S S R\n\n1119-16th nw NAtional-7550",
    blocks=[{"label": "Section-Header", "bbox": [10, 10, 200, 30], "text": "Embassy"}],
    skew=-0.31,
)


def test_indra_reads_one_page_with_layout_and_confidence(monkeypatch, tmp_path):
    seen = _fake_indra(monkeypatch, tmp_path, PAGE)
    adapter = load_adapter("indra", {"dpi": 300})
    assert adapter_conformance_check(adapter) == []
    assert "model_dir" not in seen  # no model lookup, and so no download, until a page is read
    result = adapter.extract(
        Path("book.pdf"), page_id="p2", page_number=2, action="transcribe_text"
    )
    assert seen["index"] == 1 and seen["closed"]
    assert seen["load"]["mode"] == "ocr" and seen["load"]["dpi"] == 300.0
    assert result.content == "Embassy of the U S S R\n1119-16th nw NAtional-7550"
    assert result.format == "text"
    assert result.model.startswith("indra-ocr 1.1.1; models sha256:")
    assert result.model.endswith("; mode=ocr; dpi=300; source=ocr")
    assert result.confidence == 0.69
    detail = result.confidence_detail
    assert (detail["word_count"], detail["below_60_count"], detail["unit"]) == (2, 1, "line")
    assert detail["layout"]["blocks"] == [
        {"label": "Section-Header", "bbox": [10.0, 10.0, 200.0, 30.0]}
    ]
    assert detail["layout"]["skew"] == -0.31
    assert result.usage["pages"] == 1 and result.usage["cost_usd"] is None


def test_indra_markdown_output_keeps_headings_and_tables(monkeypatch, tmp_path):
    _fake_indra(monkeypatch, tmp_path, PAGE)
    adapter = load_adapter("indra", {"output": "markdown"})
    result = adapter.extract(Path("b.pdf"), page_id="p1", page_number=1, action="transcribe_text")
    assert (result.format, result.content) == ("markdown", PAGE.markdown)


def test_indra_records_a_turned_page(monkeypatch, tmp_path):
    turned = Page(lines=[Line([0, 0, 1, 1], "sideways table", 0.9)], rotation=90)
    _fake_indra(monkeypatch, tmp_path, turned)
    result = load_adapter("indra", {}).extract(
        Path("b.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    assert result.warnings == ["page_rotated"]
    assert result.confidence_detail["layout"]["rotation"] == 90


def test_indra_failed_page_raises(monkeypatch, tmp_path):
    _fake_indra(monkeypatch, tmp_path, Page(lines=[], source="error", note="ValueError: bad"))
    adapter = load_adapter("indra", {})
    with pytest.raises(RuntimeError, match="could not read page 1"):
        adapter.extract(Path("b.pdf"), page_id="p1", page_number=1, action="transcribe_text")


@pytest.mark.parametrize(
    "options",
    [{"mode": "text"}, {"output": "html"}, {"dpi": 50}, {"threads": 0}, {"math": "yes"}],
)
def test_indra_rejects_bad_options(monkeypatch, tmp_path, options):
    _fake_indra(monkeypatch, tmp_path, PAGE)
    with pytest.raises(ValueError):
        load_adapter("indra", options)


def test_indra_missing_package_says_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "indra_ocr", None)
    with pytest.raises(ValueError, match=r"pageledger\[indra\]"):
        load_adapter("indra", {})
