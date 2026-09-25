from __future__ import annotations

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

pypdf = pytest.importorskip("pypdf")
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm is required")


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
    assert result.confidence_detail == {
        "word_count": 2,
        "mean": 65.0,
        "min": 40.0,
        "below_60_count": 1,
        "below_60_ratio": 0.5,
    }
    assert result.model.startswith("rapidocr 3.9.2; rec PP-OCRv5 sha256:")


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
    with pytest.raises(ValueError, match="inference.onnx and keys.txt"):
        load_adapter("rapidocr", {"rec_model_dir": str(tmp_path)})


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
