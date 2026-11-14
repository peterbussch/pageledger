from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml

try:
    import pypdf  # noqa: F401
except ModuleNotFoundError:
    pypdf = None

ROOT = Path(__file__).resolve().parents[2]
# Only the tests that render a page need these; the reading-order logic needs neither.
needs_pdf = pytest.mark.skipif(
    pypdf is None or shutil.which("pdftoppm") is None,
    reason="needs the pdf extra and Poppler's pdftoppm",
)


def _pdf(path: Path) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _recognizer(path: Path) -> Path:
    path.mkdir()
    (path / "inference.onnx").write_bytes(b"recognizer")
    (path / "keys.txt").write_text("keys", encoding="utf-8")
    return path


def _fake_rapidocr(monkeypatch: pytest.MonkeyPatch, engine: type) -> None:
    fake = types.ModuleType("rapidocr")
    fake.OCRVersion = types.SimpleNamespace(PPOCRV5="v5")
    fake.RapidOCR = engine
    monkeypatch.setitem(sys.modules, "rapidocr", fake)
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "3.9.2")


@needs_pdf
def test_rapidocr_assembles_text_and_maps_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    points = [[[0, 0], [10, 0], [10, 10], [0, 10]], [[20, 0], [30, 0], [30, 10], [20, 10]]]

    class Engine:
        def __init__(self, params):
            assert params["Rec.rec_keys_path"].endswith("keys.txt")

        def __call__(self, _image):
            return types.SimpleNamespace(boxes=points, txts=["Alpha", "Beta"], scores=[0.9, 0.4])

    _fake_rapidocr(monkeypatch, Engine)
    adapter = load_adapter("rapidocr", {"rec_model_dir": str(_recognizer(tmp_path / "rec"))})
    result = adapter.extract(
        _pdf(tmp_path / "sample.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    assert result.content == "Alpha | Beta"
    assert result.confidence == 0.65
    assert "multi_column_rows" not in result.warnings
    assert result.confidence_detail == {
        "word_count": 2,
        "mean": 65.0,
        "min": 40.0,
        "below_60_count": 1,
        "below_60_ratio": 0.5,
    }
    assert result.model.startswith("rapidocr 3.9.2; rec PP-OCRv5 sha256:")


def _box(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# A journal page: a full-width title, then two columns whose lines sit at the same heights.
TWO_COLUMNS = [
    (_box(0, 0, 420, 20), "Title across the page"),
    (_box(0, 60, 200, 72), "left one"),
    (_box(220, 60, 420, 72), "right one"),
    (_box(0, 80, 200, 92), "left two"),
    (_box(220, 80, 420, 92), "right two"),
    (_box(0, 100, 200, 112), "left three"),
    (_box(220, 100, 420, 112), "right three"),
]


def _engine_for(lines):
    class Engine:
        def __init__(self, params):
            pass

        def __call__(self, _image):
            return types.SimpleNamespace(
                boxes=[box for box, _ in lines],
                txts=[text for _, text in lines],
                scores=[0.9] * len(lines),
            )

    return Engine


@needs_pdf
def test_rapidocr_reads_rows_across_the_page_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    _fake_rapidocr(monkeypatch, _engine_for(TWO_COLUMNS))
    adapter = load_adapter("rapidocr", {"rec_model_dir": str(_recognizer(tmp_path / "rec"))})
    result = adapter.extract(
        _pdf(tmp_path / "sample.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    assert result.content.splitlines()[1] == "left one | right one"
    assert "reading-order" not in result.model


@needs_pdf
def test_rapidocr_column_reading_order_reads_each_column_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    _fake_rapidocr(monkeypatch, _engine_for(TWO_COLUMNS))
    adapter = load_adapter(
        "rapidocr",
        {"rec_model_dir": str(_recognizer(tmp_path / "rec")), "reading_order": "columns"},
    )
    result = adapter.extract(
        _pdf(tmp_path / "sample.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    assert result.content.splitlines() == [
        "Title across the page",
        "left one",
        "left two",
        "left three",
        "right one",
        "right two",
        "right three",
    ]
    assert result.model.endswith("; reading-order=columns")


def test_rapidocr_column_order_keeps_a_side_block_beside_a_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.rapidocr_adapter import assemble

    # Authors on the left beside the title and abstract on the right, then body columns.
    lines = [
        {"text": text, "x0": b[0][0], "y0": b[0][1], "x1": b[2][0], "y1": b[2][1]}
        for b, text in [
            (_box(0, 0, 100, 12), "Author A"),
            (_box(0, 16, 100, 28), "Author B"),
            (_box(140, 0, 420, 12), "Title line"),
            (_box(140, 16, 420, 28), "Abstract line"),
            (_box(0, 60, 200, 72), "body left"),
            (_box(220, 60, 420, 72), "body right"),
        ]
    ]
    for line in lines:
        line.update(cx=(line["x0"] + line["x1"]) / 2, cy=(line["y0"] + line["y1"]) / 2)
        line["h"] = line["y1"] - line["y0"]
    assert assemble(lines, reading_order="columns").splitlines() == [
        "Author A",
        "Author B",
        "Title line",
        "Abstract line",
        "body left",
        "body right",
    ]


def test_rapidocr_column_order_survives_a_break_at_the_same_height_in_both_columns() -> None:
    from pageledger.rapidocr_adapter import assemble

    rows = [(0, "a1", "b1"), (20, "a2", "b2"), (60, "a3", "b3"), (80, "a4", "b4")]
    lines = []
    for y, left, right in rows:
        for x0, text in ((0, left), (220, right)):
            lines.append(
                {
                    "text": text,
                    "x0": x0,
                    "x1": x0 + 200,
                    "y0": y,
                    "y1": y + 12,
                    "cx": x0 + 100,
                    "cy": y + 6,
                    "h": 12,
                }
            )
    assert assemble(lines, reading_order="columns").splitlines() == [
        "a1",
        "a2",
        "a3",
        "a4",
        "b1",
        "b2",
        "b3",
        "b4",
    ]


def test_rapidocr_column_order_finds_a_gutter_hidden_by_padded_boxes() -> None:
    from pageledger.rapidocr_adapter import assemble

    # Detector boxes overlap across a narrow gutter, and one line was merged across it.
    lines = []
    for row in range(12):
        y = row * 14
        for x0, x1, side in ((100, 1099, "L"), (1092, 2070, "R")):
            lines.append({"text": f"{side}{row}", "x0": x0, "x1": x1, "y0": y, "y1": y + 12})
    lines.append({"text": "merged", "x0": 100, "x1": 2070, "y0": 168, "y1": 180})
    for line in lines:
        line.update(cx=(line["x0"] + line["x1"]) / 2, cy=(line["y0"] + line["y1"]) / 2, h=12)
    assert assemble(lines, reading_order="columns").splitlines() == [
        *(f"L{row}" for row in range(12)),
        *(f"R{row}" for row in range(12)),
        "merged",
    ]


def _line(text: str, x0: float, y0: float, x1: float, y1: float) -> dict:
    return {
        "text": text,
        "x0": x0,
        "x1": x1,
        "y0": y0,
        "y1": y1,
        "cx": (x0 + x1) / 2,
        "cy": (y0 + y1) / 2,
        "h": y1 - y0,
    }


def _single_column(pattern: list[str]) -> list[dict]:
    """One column of prose: full lines, short paragraph tails, indented dialogue, very short lines."""
    spans = {"full": (0, 1000), "tail": (0, 380), "dlg": (60, 420), "short": (0, 70)}
    return [
        _line(f"{kind}{i}", *(spans[kind][0], i * 40, spans[kind][1], i * 40 + 30))
        for i, kind in enumerate(pattern)
    ]


@pytest.mark.parametrize(
    "pattern",
    [
        [
            "dlg",
            "tail",
            "dlg",
            "full",
            "full",
            "tail",
            "dlg",
            "tail",
            "dlg",
            "tail",
            "dlg",
            "full",
            "tail",
            "dlg",
            "tail",
            "dlg",
            "tail",
        ],
        [
            "dlg",
            "tail",
            "dlg",
            "full",
            "full",
            "short",
            "dlg",
            "tail",
            "dlg",
            "short",
            "dlg",
            "full",
            "tail",
            "dlg",
            "short",
            "dlg",
            "tail",
        ],
    ],
)
def test_rapidocr_column_order_reads_a_single_column_with_dialogue_in_order(pattern) -> None:
    from pageledger.rapidocr_adapter import assemble

    lines = _single_column(pattern)
    assert assemble(lines, reading_order="columns").splitlines() == [line["text"] for line in lines]


def test_rapidocr_column_order_reads_alternating_verse_indents_in_order() -> None:
    from pageledger.rapidocr_adapter import assemble

    lines = [
        _line(f"verse{i}", 30 * (i % 2), i * 16, 30 * (i % 2) + 200, i * 16 + 12) for i in range(12)
    ]
    assert assemble(lines, reading_order="columns").splitlines() == [line["text"] for line in lines]


def test_rapidocr_rejects_an_unknown_reading_order(tmp_path: Path, monkeypatch) -> None:
    from pageledger.adapters import load_adapter

    _fake_rapidocr(monkeypatch, _engine_for(TWO_COLUMNS))
    with pytest.raises(ValueError, match="reading_order must be rows or columns"):
        load_adapter(
            "rapidocr",
            {"rec_model_dir": str(_recognizer(tmp_path / "rec")), "reading_order": "auto"},
        )


@needs_pdf
def test_rapidocr_run_cli_writes_normalized_quality_and_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.cli import main

    constructions = []

    class Engine:
        def __init__(self, params):
            constructions.append(params)

        def __call__(self, _image):
            return types.SimpleNamespace(
                boxes=[[[0, 0], [10, 0], [10, 10], [0, 10]]],
                txts=["Recognized text"],
                scores=[0.8],
            )

    _fake_rapidocr(monkeypatch, Engine)
    config = tmp_path / "config.yml"
    options = {"rec_model_dir": str(_recognizer(tmp_path / "rec")), "max_side": 3200}
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "run": {"adapter": "rapidocr", "adapter_options": options},
                "taxonomy": {"page_types": {"prose": {"default_action": "transcribe_text"}}},
            }
        ),
        encoding="utf-8",
    )
    source = _pdf(tmp_path / "source.pdf")
    out = tmp_path / "run"

    assert main(["run", str(source), "--config", str(config), "--out", str(out)]) == 0
    quality = json.loads((out / "quality.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert quality["confidence"] == 0.8
    assert quality["confidence_detail"]["mean"] == 80.0
    assert len(constructions) == 1


@needs_pdf
def test_rapidocr_render_timeout_reaches_pdftoppm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger import rapidocr_adapter
    from pageledger.adapters import load_adapter

    class Engine:
        def __init__(self, params):
            pass

        def __call__(self, _image):
            return types.SimpleNamespace(boxes=[], txts=[], scores=[])

    _fake_rapidocr(monkeypatch, Engine)
    timeouts = []

    def fake_run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        Path(command[-1] + ".png").write_bytes(b"png")

    monkeypatch.setattr(rapidocr_adapter.subprocess, "run", fake_run)
    rec = str(_recognizer(tmp_path / "rec"))
    source = _pdf(tmp_path / "source.pdf")
    for options, expected in (({}, 120), ({"render_timeout_seconds": 900}, 900)):
        adapter = load_adapter("rapidocr", {"rec_model_dir": rec, **options})
        adapter.extract(source, page_id="p1", page_number=1, action="transcribe_text")
        assert timeouts[-1] == expected


@pytest.mark.parametrize("value", [5, 7200, "600", True])
def test_rapidocr_rejects_bad_render_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    from pageledger.adapters import load_adapter

    monkeypatch.setitem(sys.modules, "rapidocr", types.ModuleType("rapidocr"))
    with pytest.raises(ValueError, match="render_timeout_seconds"):
        load_adapter(
            "rapidocr",
            {"rec_model_dir": str(_recognizer(tmp_path / "rec")), "render_timeout_seconds": value},
        )


def test_rapidocr_missing_package_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    monkeypatch.setitem(sys.modules, "rapidocr", None)
    with pytest.raises(ValueError, match=r"pip install 'pageledger\[rapidocr\]'"):
        load_adapter("rapidocr", {"rec_model_dir": str(_recognizer(tmp_path / "rec"))})


def test_rapidocr_missing_model_files_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    monkeypatch.setitem(sys.modules, "rapidocr", types.ModuleType("rapidocr"))
    with pytest.raises(ValueError, match="inference.onnx and either keys.txt or inference.yml"):
        load_adapter("rapidocr", {"rec_model_dir": str(tmp_path)})


@needs_pdf
def test_rapidocr_reads_character_dictionary_from_inference_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    recognizer = tmp_path / "english-rec"
    recognizer.mkdir()
    (recognizer / "inference.onnx").write_bytes(b"recognizer")
    (recognizer / "inference.yml").write_text(
        "PostProcess:\n  character_dict: [A, B, C]\n", encoding="utf-8"
    )
    paths = []

    class Engine:
        def __init__(self, params):
            path = Path(params["Rec.rec_keys_path"])
            paths.append(path)
            assert path.read_text(encoding="utf-8").splitlines() == ["A", "B", "C"]

        def __call__(self, _image):
            return types.SimpleNamespace(boxes=[], txts=[], scores=[])

    _fake_rapidocr(monkeypatch, Engine)
    adapter = load_adapter("rapidocr", {"rec_model_dir": str(recognizer)})
    result = adapter.extract(
        _pdf(tmp_path / "sample.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    dictionary_bytes = ("A" + chr(10) + "B" + chr(10) + "C" + chr(10)).encode()
    dictionary_hash = hashlib.sha256(dictionary_bytes).hexdigest()
    assert result.model and f"keys-sha256:{dictionary_hash}" in result.model
    assert paths[0].is_file()
    assert paths[0].parent == Path(adapter._recognizer_keys_path).parent


@needs_pdf
def test_rapidocr_warns_for_rows_joining_four_lines_in_most_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter

    boxes = []
    texts = []
    scores = []
    for y in (10, 30, 50):
        for column in range(4):
            boxes.append(_box(column * 30, y, column * 30 + 20, y + 10))
            texts.append(f"c{column}")
            scores.append(0.9)
    boxes.extend([_box(0, 80, 20, 90), _box(30, 80, 50, 90)])
    texts.extend(["left", "right"])
    scores.extend([0.9, 0.9])

    class Engine:
        def __init__(self, params):
            pass

        def __call__(self, _image):
            return types.SimpleNamespace(boxes=boxes, txts=texts, scores=scores)

    _fake_rapidocr(monkeypatch, Engine)
    source = _pdf(tmp_path / "sample.pdf")
    rows = load_adapter("rapidocr", {"rec_model_dir": str(_recognizer(tmp_path / "rec"))})
    result = rows.extract(source, page_id="p1", page_number=1, action="transcribe_text")
    columns = load_adapter(
        "rapidocr",
        {"rec_model_dir": str(_recognizer(tmp_path / "rec-columns")), "reading_order": "columns"},
    )
    column_result = columns.extract(source, page_id="p1", page_number=1, action="transcribe_text")
    assert "multi_column_rows" in result.warnings
    assert "multi_column_rows" not in column_result.warnings


@needs_pdf
def test_rapidocr_returns_its_line_boxes_as_a_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import load_adapter
    from pageledger.layout import validate_layout

    lines = [*TWO_COLUMNS, (_box(-5, 400, 30, 420), "padded past the edge")]
    _fake_rapidocr(monkeypatch, _engine_for(lines))
    adapter = load_adapter(
        "rapidocr", {"rec_model_dir": str(_recognizer(tmp_path / "rec")), "max_side": 1000}
    )
    assert "layout" in adapter.capabilities
    result = adapter.extract(
        _pdf(tmp_path / "sample.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    layout = result.layout
    validate_layout(layout)
    assert layout["level"] == "line" and layout["source"] == {}
    # The 72-point page renders at 1000 x 1000 pixels: a pixel is 1000 grid steps.
    assert layout["items"][0] == {"box": [0, 0, 420_000, 20_000], "text": "Title across the page"}
    assert layout["items"][-1]["box"] == [0, 400_000, 30_000, 420_000]
    assert len(layout["items"]) == len(lines)


def test_multi_column_rows_threshold_excludes_single_and_two_column_rows():
    from pageledger.rapidocr_adapter import _multi_column_rows

    two_columns = [
        {
            "text": f"{column}-{row}",
            "cx": column * 100 + 20,
            "cy": row * 20,
            "h": 10,
        }
        for row in range(8)
        for column in range(2)
    ]
    assert not _multi_column_rows(two_columns)
    assert not _multi_column_rows(_single_column(["full", "tail", "dlg", "short"]))
    four_column_rows = [
        {
            "text": f"{column}-{row}",
            "cx": column * 100 + 20,
            "cy": row * 20,
            "h": 10,
        }
        for row in range(8)
        for column in range(4 if row < 4 else 2)
    ]
    assert not _multi_column_rows(four_column_rows)


@needs_pdf
def test_apple_vision_adapter_runs_helper_and_assembles_rows(tmp_path: Path) -> None:
    helper = tmp_path / "helper"
    helper.write_text(
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'lines':[{'text':'second','confidence':0.8,'box':[0.5,0.8,0.1,0.1]},{'text':'first','confidence':0.9,'box':[0.1,0.8,0.1,0.1]}]}))\n",
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    module = _load(ROOT / "examples/apple_vision_adapter.py", "apple_vision_example")
    result = module.AppleVisionAdapter(helper=str(helper)).extract(
        _pdf(tmp_path / "scan.pdf"), page_id="p1", page_number=1, action="transcribe_text"
    )
    assert result.content == "first | second"
    assert result.usage["pages"] == 1


def test_rapidocr_rows_read_a_right_to_left_row_from_the_right() -> None:
    from pageledger.rapidocr_adapter import _assemble, assemble

    lines = [
        _line("שם", 600, 100, 700, 130),
        _line("עיר", 300, 100, 400, 130),
        _line("שנה", 50, 100, 150, 130),
        _line("Name", 50, 200, 150, 230),
        _line("City", 300, 200, 400, 230),
    ]
    assert assemble(lines).splitlines() == ["שם | עיר | שנה", "Name | City"]
    assert _assemble(lines, "rows")[1] == 1


@pytest.mark.parametrize(
    "name",
    [
        "apple_vision_adapter:AppleVisionAdapter",
        "cloud_vlm_adapter_skeleton:CloudVlmAdapter",
        "docling_adapter:DoclingAdapter",
        "local_llm_cleanup_adapter:LocalLlmCleanupAdapter",
        "ollama_cleanup_adapter:OllamaCleanupAdapter",
        "prereform_normalizer_adapter:PrereformNormalizerAdapter",
        "tesseract_pdftoppm_adapter:TesseractPdftoppmAdapter",
        "tesseract_tsv_table_adapter:TesseractTsvTableAdapter",
    ],
)
def test_every_example_adapter_loads_by_name_and_keeps_the_protocol(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.adapters import adapter_conformance_check, load_adapter

    monkeypatch.syspath_prepend(str(ROOT / "examples"))
    assert adapter_conformance_check(load_adapter(name)) == []


def test_every_example_adapter_is_in_the_conformance_list() -> None:
    listed = {
        "apple_vision_adapter",
        "cloud_vlm_adapter_skeleton",
        "docling_adapter",
        "local_llm_cleanup_adapter",
        "ollama_cleanup_adapter",
        "prereform_normalizer_adapter",
        "tesseract_pdftoppm_adapter",
        "tesseract_tsv_table_adapter",
    }
    assert {path.stem for path in (ROOT / "examples").glob("*_adapter*.py")} == listed
