"""A first document job: init, and process without --config."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from pageledger import adapters
from pageledger.cli import main
from pageledger.config import load_config
from pageledger.processing import _process_run_settings, resume_job, verify_job
from pageledger.processing_config import processing_config

ENGINE_ARGS = {
    "pdf_ocr": ["--engine", "pdf_ocr"],
    "pdf_ocr_rus": ["--engine", "pdf_ocr", "--lang", "rus+eng"],
    "rapidocr": ["--engine", "rapidocr", "--model-dir", "MODEL_DIR"],
    "indra": ["--engine", "indra"],
    "indra_models": ["--engine", "indra", "--model-dir", "MODEL_DIR"],
    "none": ["--engine", "none"],
}


def _engine_args(name: str, tmp_path: Path) -> list[str]:
    return [str(tmp_path) if arg == "MODEL_DIR" else arg for arg in ENGINE_ARGS[name]]


def _write_config(tmp_path: Path, *args: str) -> Path:
    path = tmp_path / "processing.yml"
    assert main(["init", *args, "--out", str(path)]) == 0
    return path


def _validated(path: Path, *, pdf: bool = True) -> dict:
    config = load_config(path, validate_adapter=False)
    assert _process_run_settings(config.data) == []
    return processing_config(config.data, pdf=pdf)


@pytest.mark.parametrize("name", ENGINE_ARGS)
def test_written_config_passes_the_job_validation(tmp_path: Path, name: str) -> None:
    policy = _validated(_write_config(tmp_path, *_engine_args(name, tmp_path)))
    engine = ENGINE_ARGS[name][1]
    assert policy["local_text"]["adapter"] == "pdf_text"
    assert policy["image"] is None and policy["second_opinion"] is None
    assert policy["limits"]["max_image_pages"] == 0
    assert policy["limits"]["max_attempt_pages"] is None
    if engine == "none":
        assert policy["local_ocr"] is None
        return
    options = policy["local_ocr"]["adapter_options"]
    assert policy["local_ocr"]["adapter"] == engine
    if engine == "pdf_ocr":
        assert options == {"lang": "rus+eng" if name == "pdf_ocr_rus" else "eng", "dpi": 300}
    elif engine == "rapidocr":
        assert options == {"rec_model_dir": str(tmp_path.resolve())}
    else:
        assert options["mode"] == "ocr"
        assert ("model_dir" in options) == (name == "indra_models")
    if engine == "pdf_ocr":
        adapters.load_adapter("pdf_ocr", options)


@pytest.mark.parametrize("name", ["pdf_ocr", "rapidocr", "indra"])
def test_commented_optional_stages_are_valid_when_uncommented(tmp_path: Path, name: str) -> None:
    text = _write_config(tmp_path, *_engine_args(name, tmp_path)).read_text(encoding="utf-8")
    # A commented key ("# image:") or a commented nested line ("#   adapter: vision").
    uncommented = re.sub(r"(?m)^(\s*)# ((?:[a-z_]+:(?: |$)|  ).*)$", r"\1\2", text)
    data = yaml.safe_load(uncommented)
    processing = data["processing"]
    assert {"second_ocr", "local_image", "image", "second_opinion", "contest"} <= set(processing)
    assert set(processing["limits"]) == {
        "max_image_pages",
        "max_attempt_pages",
        "max_local_image_pages",
    }
    processing["limits"]["max_image_pages"] = 5
    path = tmp_path / "uncommented.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert _validated(path)["contest"] == {"rules": "ru-print-0.2"}


def test_written_config_follows_the_source_for_local_text(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "--engine", "none")
    assert _validated(path, pdf=False)["local_text"]["adapter"] == "text"


@pytest.mark.parametrize(
    "argv,message",
    [
        (["init-config", "--engine", "indra"], "go with --processing"),
        (["init-config", "--processing", "--adapter", "pdf_ocr"], "does not go with --processing"),
        (["init", "--run", "--engine", "indra"], "do not go with --run"),
        (["init", "--adapter", "pdf_ocr"], "--adapter goes with --run"),
        (["init", "--engine", "indra", "--lang", "rus"], "indra takes none"),
        (["init", "--engine", "rapidocr"], "rapidocr needs --model-dir"),
        (["init", "--lang", "eng; rm"], "--lang must be Tesseract"),
        (["init", "--model-dir", "m"], "not pdf_ocr"),
        (["init", "--engine", "none", "--lang", "eng"], "need an OCR"),
        (
            ["process", "a.pdf", "--config", "c.yml", "--engine", "indra", "--out", "j"],
            "set them in the config file",
        ),
        (["process", "a.pdf", "--engine", "rapidocr", "--out", "j"], "rapidocr needs --model-dir"),
    ],
)
def test_option_combinations_that_cannot_work_are_usage_errors(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_plain_init_config_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    from pageledger.cli import MINIMAL_CONFIG

    assert main(["init-config"]) == 0
    assert capsys.readouterr().out == MINIMAL_CONFIG


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "letters.txt"
    source.write_text(
        "The first letter, written from the estate in the spring, describes the harvest.\f"
        "The second letter answers it and asks after the family in the town.",
        encoding="utf-8",
    )
    return source


def test_process_without_config_records_what_the_written_config_would(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    implicit, explicit = tmp_path / "jobs" / "implicit", tmp_path / "jobs" / "explicit"
    assert main(["process", str(source), "--out", str(implicit)]) == 0
    err = capsys.readouterr().err
    assert "no --config given" in err
    assert "pageledger init --engine none --out processing.yml" in err
    config = _write_config(tmp_path, "--engine", "none")
    assert main(["process", str(source), "--config", str(config), "--out", str(explicit)]) == 0
    capsys.readouterr()

    def payload(root: Path) -> dict:
        return json.loads((root / "job.json").read_text(encoding="utf-8"))["payload"]

    for key in ("config", "config_sha256", "policy", "limits"):
        assert payload(implicit)[key] == payload(explicit)[key]
    assert verify_job(implicit)["status"] == "pass"
    assert resume_job(implicit)["status"] == "completed"
    assert verify_job(implicit)["status"] == "pass"


def test_process_without_config_reads_no_file_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "processing.yml").write_text("not: a config\n", encoding="utf-8")
    (tmp_path / "pageledger.yml").write_text("not: a config\n", encoding="utf-8")
    assert main(["process", str(_source(tmp_path)), "--out", "jobs/letters"]) == 0
    job = json.loads((tmp_path / "jobs/letters/job.json").read_text(encoding="utf-8"))["payload"]
    assert job["config"]["processing"]["local_ocr"] is None


def test_process_without_config_names_missing_ocr_tools_before_the_job_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(adapters.shutil, "which", lambda name: None)
    out = tmp_path / "jobs" / "book"
    assert main(["process", str(source), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "install Poppler" in err and "pageledger doctor" in err
    assert not out.exists()


def test_process_without_config_names_a_missing_engine_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib

    real_import = importlib.import_module

    def without_indra(name: str, *args: object) -> object:
        if name == "indra_ocr":
            raise ImportError(name)
        return real_import(name, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", without_indra)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "jobs" / "book"
    assert main(["process", str(source), "--engine", "indra", "--out", str(out), "--json"]) == 1
    captured = capsys.readouterr()
    error = json.loads(captured.out)["error"]
    assert "pip install 'pageledger[indra]'" in error and "pageledger doctor" in error
    assert not out.exists()


def test_process_without_config_runs_a_pdf_with_the_default_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
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
    source = tmp_path / "hello.pdf"
    writer.write(source)
    calls: list[int] = []

    def read(self, source, *, page_id, page_number, action, prompt=None):
        calls.append(page_number)
        return adapters.ExtractionResult(
            content="Hello PageLedger",
            format="text",
            confidence=0.9,
            model=None,
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )

    monkeypatch.setattr(adapters.PdfOcrAdapter, "preflight", lambda self, sources: None)
    monkeypatch.setattr(adapters.PdfOcrAdapter, "extract", read)
    out = tmp_path / "jobs" / "hello"
    assert main(["process", str(source), "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "pdf_ocr (lang eng)" in captured.err
    job = json.loads((out / "job.json").read_text(encoding="utf-8"))["payload"]
    assert job["policy"]["local_ocr"]["adapter"] == "pdf_ocr"
    assert [attempt["stage"] for attempt in job["pages"][0]["attempts"]] == [
        "local_text",
        "local_ocr",
    ]
    assert calls == [1]
    assert verify_job(out)["status"] == "pass"


def test_completed_process_prints_the_next_steps_but_not_in_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    assert main(["process", str(source), "--out", str(tmp_path / "a")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Status: completed"
    needing = next(line for line in lines if line.startswith("Pages needing a person:"))
    assert re.fullmatch(r"Pages needing a person: \d of 2, listed first in the report\.", needing)
    assert any(line.startswith("Next: pageledger inspect ") for line in lines)
    assert any("pageledger export " in line and "letters.md" in line for line in lines)

    assert main(["process", str(source), "--out", str(tmp_path / "b"), "--json"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert set(json.loads(out)) == {"job_id", "out_dir", "status", "report", "next_action"}


def test_the_processing_example_is_what_init_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = Path(__file__).resolve().parents[2] / "docs" / "examples" / "processing.yml"
    assert main(["init"]) == 0
    assert capsys.readouterr().out == example.read_text(encoding="utf-8")
