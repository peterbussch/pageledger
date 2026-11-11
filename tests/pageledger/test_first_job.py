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
    assert (
        err == "Reading letters.txt: the text of each page, as it is. Nothing is sent anywhere.\n"
    )
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


def test_process_without_config_follows_the_kind_of_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger.cli import _engine, build_parser

    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\n\nA page of notes in Markdown.\fA second page.", encoding="utf-8")
    out = tmp_path / "jobs" / "notes"
    assert main(["process", str(notes), "--out", str(out)]) == 0
    assert "the text of each page, as it is" in capsys.readouterr().err
    job = json.loads((out / "job.json").read_text(encoding="utf-8"))["payload"]
    assert job["config"]["processing"]["local_ocr"] is None
    assert job["status"] == "completed"

    scans = tmp_path / "scans"
    scans.mkdir()
    fixture = Path(__file__).parents[1] / "fixtures" / "images" / "page-gray.jpg"
    (scans / "0001.jpg").write_bytes(fixture.read_bytes())

    def engine(source: Path) -> str:
        return _engine(build_parser().parse_args(["process", str(source), "--out", "j"]))

    assert engine(scans) == "pdf_ocr"
    assert engine(scans / "0001.jpg") == "pdf_ocr"
    assert engine(notes) == "none"


@pytest.mark.parametrize(
    "name,content,message",
    [
        (
            "letter.docx",
            b"PK\x03\x04\x14\x00\x06\x00\xff\xfe",
            "is not a PDF, a page image or UTF-8 text",
        ),
        ("empty-folder", None, "is a folder with no page images"),
        ("empty.docx", b"", "is empty"),
        ("empty.pdf", b"", "is empty"),
        ("nowhere.txt", "", "Source file was not found"),
    ],
)
def test_a_source_that_is_neither_pdf_images_nor_text_is_refused_before_the_job_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, content: object, message: str
) -> None:
    from pageledger.processing import process

    source = tmp_path / name
    if content is None:
        source.mkdir()
    elif isinstance(content, bytes):
        source.write_bytes(content)
    out = tmp_path / "jobs" / "x"
    with pytest.raises(SystemExit) as exit_info:
        main(["process", str(source), "--out", str(out)])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert message in err
    if name not in {"nowhere.txt", "empty.docx", "empty.pdf"}:
        assert "A source is a PDF, a UTF-8 text file" in err
    config = _write_config(tmp_path, "--engine", "none")
    with pytest.raises(ValueError, match=message):
        process(source=source, config_path=config, out_dir=out)
    assert not out.exists()


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


def _hello_pdf(tmp_path: Path, pages: int = 1) -> Path:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    for _ in range(pages):
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
    return source


def _ocr_reads(monkeypatch: pytest.MonkeyPatch, text: str, warnings: list[str]) -> None:
    def read(self, source, *, page_id, page_number, action, prompt=None):
        return adapters.ExtractionResult(
            content=text,
            format="text",
            confidence=0.3,
            model=None,
            warnings=list(warnings),
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )

    monkeypatch.setattr(adapters.PdfOcrAdapter, "preflight", lambda self, sources: None)
    monkeypatch.setattr(adapters.PdfOcrAdapter, "extract", read)


def test_process_says_when_the_ocr_language_is_probably_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger import language_hint

    source = _hello_pdf(tmp_path, pages=2)
    _ocr_reads(monkeypatch, "Агоипа Фе ите оЁ фе МезоШЫс репо", ["low_confidence"])
    monkeypatch.setattr(language_hint, "_script", lambda job, numbers: "Latin")
    out = tmp_path / "jobs" / "hello"
    assert main(["process", str(source), "--lang", "rus", "--out", str(out)]) == 0
    lines = capsys.readouterr().out.splitlines()
    advice = lines[lines.index("OCR was unsure of many words on 2 of the 2 pages it read.") :]
    assert advice == [
        "OCR was unsure of many words on 2 of the 2 pages it read.",
        "They look like Latin script, which rus is not written in.",
        "Try --lang eng:",
        f"  pageledger process {source} --lang eng --out {out}-eng",
        "`pageledger doctor` lists the languages installed.",
    ]
    assert main(["process", str(source), "--lang", "rus", "--out", str(out) + "2", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def _advice_after(tmp_path, monkeypatch, capsys, script, *args: str) -> list[str]:
    from pageledger import language_hint

    source = _hello_pdf(tmp_path)
    _ocr_reads(monkeypatch, "Hallo Welt", ["low_confidence"])
    monkeypatch.setattr(language_hint, "_script", lambda job, numbers: script)
    assert main(["process", str(source), *args, "--out", str(tmp_path / "jobs" / "j")]) == 0
    lines = capsys.readouterr().out.splitlines()
    return lines[lines.index("OCR was unsure of many words on 1 of the 1 pages it read.") + 1 :]


def test_language_advice_when_the_script_fits_the_language(tmp_path, monkeypatch, capsys):
    advice = _advice_after(tmp_path, monkeypatch, capsys, "Latin", "--lang", "deu")
    assert advice[:2] == [
        "Their script, Latin, fits the language given (deu), so the cause may be the scan or "
        "the print.",
        "If the document is in another Latin-script language, run it again with --lang and "
        "that language's code.",
    ]


def test_language_advice_names_no_language_for_a_code_it_does_not_know(
    tmp_path, monkeypatch, capsys
):
    advice = _advice_after(tmp_path, monkeypatch, capsys, "Latin", "--lang", "frm")
    assert advice[0] == "That can mean the language given (frm) is not the document's."
    assert not any("Try --lang" in line for line in advice)


def test_language_advice_for_a_config_job_names_the_setting(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path, "--lang", "rus")
    advice = _advice_after(tmp_path, monkeypatch, capsys, "Latin", "--config", str(config))
    assert advice[:2] == [
        "They look like Latin script, which rus is not written in.",
        "Try eng: set lang (processing.local_ocr.adapter_options.lang) in the config.",
    ]
    assert not any("--lang" in line for line in advice)


def test_language_advice_without_a_sure_script_and_for_pre_reform_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger import language_hint

    source = _hello_pdf(tmp_path)
    old = "Городъ Гродно стоитъ на рѣкѣ Нѣманѣ; въ немъ живутъ мѣщане и купцы."
    _ocr_reads(monkeypatch, old, ["low_confidence"])
    monkeypatch.setattr(language_hint, "_script", lambda job, numbers: None)
    out = tmp_path / "jobs" / "grodno"
    assert main(["process", str(source), "--lang", "rus", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "If so, run it again with --lang and that language's code." in text
    assert "Some pages are in pre-1918 Russian spelling." in text
    assert "engine-recipes.md#tesseract-with-the-community-orus-model" in text


def test_language_advice_never_fails_the_job(tmp_path: Path, monkeypatch, capsys) -> None:
    from pageledger import language_hint

    def broken(job, numbers):
        raise OSError("tesseract vanished")

    source = _hello_pdf(tmp_path)
    _ocr_reads(monkeypatch, "Hello", ["low_confidence"])
    monkeypatch.setattr(language_hint, "_script", broken)
    assert main(["process", str(source), "--out", str(tmp_path / "jobs" / "x")]) == 0
    assert "OCR was unsure" not in capsys.readouterr().out


def test_process_without_config_runs_a_pdf_with_the_default_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _hello_pdf(tmp_path)
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
    assert captured.err == (
        "Reading hello.pdf: each page's own text, then Tesseract OCR in English for pages "
        "that need it. Nothing is sent anywhere.\n"
    )
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
    out = capsys.readouterr().out
    job = tmp_path / "a"
    # Where the job is, what needs a person and why, the next command and the way out.
    assert out.splitlines() == [
        "Finished: 2 pages read; 2 need a person.",
        f"The job is in {job}.",
        "Why they need a person:",
        "  2 pages: the text file has very little text.",
        f"Next: pageledger review {job} shows those pages for checking.",
        f"Text out: pageledger export {job} --out a.md",
    ]

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
