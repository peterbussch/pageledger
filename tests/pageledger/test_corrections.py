"""A person's corrected text: recorded as a new reading with a receipt, never as an edit."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from jsonschema import validate
from test_processing import TEXT, StageAdapter, _adjudicated, _review, setup  # noqa: F401

from pageledger import runner
from pageledger.checkpoint import read_record, write_record
from pageledger.cli import main
from pageledger.corrections import normalize, prepare, word_changes
from pageledger.export import export_job
from pageledger.processing import create_review_sheet, process, review_job, verify_job
from pageledger.review_sheet import FIELDS, binding_path

ROOT = Path(__file__).resolve().parents[2]
TEI = "{http://www.tei-c.org/ns/1.0}"
WRONG = TEXT.replace("complete", "compleat", 1)


@pytest.fixture
def job(tmp_path, monkeypatch):
    """Two pages; page 2 was held after local text and read again by OCR."""
    source = tmp_path / "scans" / "source.txt"
    source.parent.mkdir()
    source.write_text(WRONG + "\f" + TEXT)
    config = tmp_path / "config.yml"
    stages = {stage: {"adapter": stage} for stage in ("local_text", "local_ocr")}
    config.write_text(yaml.safe_dump({"schema_version": "0.1", "processing": stages}))

    class Reader(StageAdapter):
        def extract(self, source, *, page_id, page_number, action, prompt=None):
            result = super().extract(
                source, page_id=page_id, page_number=page_number, action=action, prompt=prompt
            )
            if page_number == 1:
                object.__setattr__(result, "content", WRONG)
            return result

    def adapter(name, *args, **kwargs):
        stage = Reader()
        object.__setattr__(stage, "stage", name)
        object.__setattr__(stage, "defective", {2})
        return stage

    monkeypatch.setattr(runner, "load_adapter", adapter)
    monkeypatch.delenv("PAGELEDGER_REVIEWER", raising=False)
    root = tmp_path / "job"
    process(source=source, config_path=config, out_dir=root)
    return root


def _pages(job):
    return read_record(job / "job.json")["pages"]


def _decisions(job, folder: Path, rows: list[dict], name: str = "review.json") -> Path:
    """A decisions file as the review view downloads it, written for the job as it is now."""
    folder.mkdir(parents=True, exist_ok=True)
    sheet = folder / "review.csv"
    create_review_sheet(job, sheet)
    bindings = json.loads(binding_path(sheet).read_text())["bindings"]
    path = folder / name
    path.write_text(
        json.dumps({"schema_version": "0.1", "bindings": bindings, "rows": rows}),
        encoding="utf-8",
    )
    return path


def _row(number: int, **fields) -> dict:
    return {"row_id": f"p{number:04d}", "page_number": str(number), **fields}


def _sheet(job, folder: Path, decisions: dict[int, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    sheet = folder / "review.csv"
    create_review_sheet(job, sheet)
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for number, decision in decisions.items():
        rows[number - 1]["decision"] = decision
    with sheet.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    return sheet


def _schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text())


def test_a_correction_is_a_new_reading_bound_by_its_receipt(job, tmp_path):
    first = _pages(job)[0]
    attempt = next(a for a in first["attempts"] if a["attempt_id"] == first["selected_attempt"])
    path = _decisions(
        job, tmp_path / "view", [_row(1, decision="correct", note="compleat", text=TEXT)]
    )

    result = review_job(job, path, reviewer="A. Reader")

    sha256 = hashlib.sha256(TEXT.encode()).hexdigest()
    assert result["decisions"] == {"reviewed_text": 1}
    assert result["corrections"] == [
        {
            "page_number": 1,
            "from": attempt["attempt_id"],
            "words": {"changed": 1, "added": 0, "removed": 0},
            "large": False,
        }
    ]
    page = _pages(job)[0]
    decision = page["review"]["decisions"][0]
    assert decision["disposition"] == page["disposition"] == "reviewed_text"
    assert decision["selected_attempt"] == attempt["attempt_id"]
    assert decision["output_sha256"] == attempt["raw_sha256"]
    assert decision["reviewer"] == "A. Reader" and decision["reason"] == "compleat"
    assert decision["correction"] == {
        "artifact": f"corrections/{page['page_id']}-{sha256}.txt",
        "sha256": sha256,
        "from_sha256": attempt["raw_sha256"],
        "words": {"changed": 1, "added": 0, "removed": 0},
    }
    assert (job / decision["correction"]["artifact"]).read_text() == TEXT
    # The machine reading is kept as it was.
    assert (job / attempt["raw_artifact"]).read_text() == WRONG
    validate(page["review"], _schema("job-review.schema.json"))
    validate(json.loads((job / "job.json").read_text()), _schema("job.schema.json"))

    document = json.loads((job / "document.json").read_text())
    validate(document, _schema("document.schema.json"))
    selected = document["pages"][0]["selected_output"]
    assert selected["text"] == TEXT and selected["sha256"] == sha256
    assert selected["correction"] == {
        "from_path": attempt["raw_artifact"],
        "from_sha256": attempt["raw_sha256"],
        "words": {"changed": 1, "added": 0, "removed": 0},
        "reviewer": "A. Reader",
        "reviewed_at": decision["reviewed_at"],
        "reason": "compleat",
    }
    assert document["counts"]["corrected_pages"] == 1
    report = (job / "report.md").read_text()
    assert "Pages whose text a person corrected: 1." in report
    assert "Correction by a person of Local text attempt" in report
    assert "1 word changed, 0 added, 0 removed" in report
    assert "Correction by a person of attempt" in (job / "transcript.md").read_text()
    assert verify_job(job)["status"] == "pass"


def test_a_job_without_corrections_carries_none_of_the_new_fields(job):
    document = json.loads((job / "document.json").read_text())
    assert "corrected_pages" not in document["counts"]
    assert all("correction" not in (p["selected_output"] or {}) for p in document["pages"])
    assert not (job / "corrections").exists()


def test_a_correction_file_beside_a_sheet_is_read_relative_to_it(job, tmp_path):
    folder = tmp_path / "sheet"
    folder.mkdir()
    (folder / "texts").mkdir()
    (folder / "texts" / "page-1.txt").write_bytes(b"\xef\xbb\xbf" + TEXT.encode())
    sheet = _sheet(job, folder, {1: "correct:texts/page-1.txt", 2: "accept"})

    result = review_job(job, sheet, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 2}
    first, second = _pages(job)
    # The byte-order mark marks the file as UTF-8; it is not part of the text.
    assert (job / first["review"]["decisions"][0]["correction"]["artifact"]).read_text() == TEXT
    assert "correction" not in second["review"]["decisions"][0]
    assert verify_job(job)["status"] == "pass"


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("../outside.txt", TEXT.encode(), "must be inside"),
        ("missing.txt", None, "Cannot read the correction file"),
        ("latin1.txt", "Тула".encode("cp1251"), "is not UTF-8"),
        ("empty.txt", b" \n\t\n", "is empty. If the page has no text, decide blank"),
        ("same.txt", WRONG.encode(), "identical to the reading it started from.*accept"),
        ("same-crlf.txt", WRONG.replace(". ", ".\r\n").encode(), None),
    ],
)
def test_unfit_correction_files_are_refused_before_anything_is_recorded(
    job, tmp_path, name, content, message
):
    folder = tmp_path / "sheet"
    folder.mkdir()
    if content is not None:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        target = (folder / name).resolve()
        target.write_bytes(content)
    sheet = _sheet(job, folder, {1: f"correct:{name}"})
    if message is None:
        # Line breaks differ from the reading, so this is a correction, not a copy.
        review_job(job, sheet, reviewer="A. Reader", dry_run=True)
        return
    with pytest.raises(ValueError, match=message):
        review_job(job, sheet, reviewer="A. Reader")
    assert all(page["review"] is None for page in _pages(job))
    assert not (job / "corrections").exists()


def test_an_absolute_correction_path_is_refused(job, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(TEXT)
    sheet = _sheet(job, tmp_path / "sheet", {1: f"correct:{outside}"})
    with pytest.raises(ValueError, match="must be inside"):
        review_job(job, sheet, reviewer="A. Reader")


def test_identical_text_names_accept(job, tmp_path):
    path = _decisions(job, tmp_path, [_row(1, decision="correct", text=WRONG)])
    with pytest.raises(ValueError, match="decide accept instead"):
        review_job(job, path, reviewer="A. Reader")
    # Only line endings differ: still the same text.
    with pytest.raises(ValueError, match="decide accept instead"):
        prepare("first\nsecond\r", "first\r\nsecond\n", 1)
    assert prepare("first second", "first  second", 1)[1]["changed"] == 0


def test_a_sheet_cannot_carry_text_and_rows_must_say_what_they_correct(job, tmp_path):
    sheet = _sheet(job, tmp_path / "sheet", {1: "correct"})
    with pytest.raises(ValueError, match="cannot carry the corrected text"):
        review_job(job, sheet, reviewer="A. Reader")
    path = _decisions(job, tmp_path / "a", [_row(1, decision="correct")])
    with pytest.raises(ValueError, match="either in the row's text"):
        review_job(job, path, reviewer="A. Reader")
    path = _decisions(job, tmp_path / "b", [_row(1, decision="accept", text=TEXT)])
    with pytest.raises(ValueError, match="text and from go with a correct decision"):
        review_job(job, path, reviewer="A. Reader")
    path = _decisions(job, tmp_path / "c", [_row(1, decision="correct", text=TEXT, extra="x")])
    with pytest.raises(ValueError, match="Each row of a decisions file"):
        review_job(job, path, reviewer="A. Reader")


def test_a_decisions_file_is_bound_like_a_sheet(job, tmp_path):
    path = _decisions(job, tmp_path, [_row(1, decision="correct", text=TEXT)])
    data = json.loads(path.read_text())
    data["bindings"]["p0001"]["output_sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="p0001 no longer matches"):
        review_job(job, path, reviewer="A. Reader")
    path = _decisions(job, tmp_path / "unnamed", [_row(1, decision="correct", text=TEXT)])
    with pytest.raises(ValueError, match="Name the reviewer"):
        review_job(job, path)


def test_dry_run_reports_the_change_and_warns_of_a_likely_wrong_page(job, tmp_path, capsys):
    other = "An entirely different page about railway timetables and their stations. " * 8
    path = _decisions(job, tmp_path, [_row(1, decision="correct", text=other)])

    code = main(["review", str(job), "--apply", str(path), "--reviewer", "A. R", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("Checked, and nothing recorded (dry run): 1 page: 1 corrected.\n")
    assert "Correction of page 1, from local_text:doc_0001_page_0001:" in out
    assert "Warning: the correction of page 1 changes more than half the words" in out
    assert _pages(job)[0]["review"] is None
    assert not (job / "corrections").exists()


def test_a_correction_can_start_from_another_reading(job, tmp_path):
    ocr = next(a for a in _pages(job)[1]["attempts"] if a["stage"] == "local_ocr")
    text = TEXT.replace("prose", "verse", 1)
    path = _decisions(
        job, tmp_path, [_row(2, decision="correct", text=text, **{"from": ocr["attempt_id"]})]
    )

    review_job(job, path, reviewer="A. Reader")

    page = _pages(job)[1]
    assert page["selected_attempt"] == ocr["attempt_id"]
    decision = page["review"]["decisions"][0]
    assert decision["output_sha256"] == decision["correction"]["from_sha256"] == ocr["raw_sha256"]
    assert verify_job(job)["status"] == "pass"


def test_correcting_a_correction_is_recorded_as_a_chain(job, tmp_path):
    first = TEXT.replace("prose", "verse", 1)
    review_job(
        job,
        _decisions(job, tmp_path / "one", [_row(1, decision="correct", text=first)]),
        reviewer="A. Reader",
    )
    stale = _decisions(job, tmp_path / "stale", [_row(1, decision="accept")])
    earlier = _pages(job)[0]["review"]["decisions"][0]["correction"]
    review_job(
        job,
        _decisions(job, tmp_path / "two", [_row(1, decision="correct", text=TEXT)]),
        reviewer="B. Reader",
    )

    page = _pages(job)[0]
    assert len(page["review_history"]) == 2
    later = page["review"]["decisions"][0]
    assert later["correction"]["from_sha256"] == earlier["sha256"]
    assert (
        later["selected_attempt"] == page["review_history"][0]["decisions"][0]["selected_attempt"]
    )
    assert (job / earlier["artifact"]).read_text() == first
    document = json.loads((job / "document.json").read_text())
    assert document["pages"][0]["selected_output"]["correction"]["from_path"] == earlier["artifact"]
    assert "revising [the correction by A. Reader at" in (job / "report.md").read_text()
    assert verify_job(job)["status"] == "pass"

    # A decision written for the earlier text no longer matches the page.
    accepted = _decisions(job, tmp_path / "three", [_row(1, decision="accept")])
    with pytest.raises(ValueError, match="p0001 no longer matches"):
        review_job(job, stale, reviewer="C. Reader")
    # Accepting a corrected page accepts the correction; it does not undo it.
    review_job(job, accepted, reviewer="C. Reader")
    page = _pages(job)[0]
    assert page["review"]["decisions"][0]["correction"] == later["correction"]
    selected = json.loads((job / "document.json").read_text())["pages"][0]["selected_output"]
    assert selected["text"] == TEXT
    # The correction is still credited to the person who made it, not to the one who accepted it.
    assert selected["correction"]["reviewer"] == "B. Reader"
    assert selected["correction"]["reviewed_at"] == later["reviewed_at"]
    assert "made by B. Reader at" in (job / "report.md").read_text()
    out = tmp_path / "book.xml"
    export_job(job, out, format="tei")
    change = ET.fromstring(out.read_bytes()).find(f"{TEI}teiHeader/{TEI}revisionDesc/{TEI}change")
    assert change is not None and "corrected by B. Reader" in change.text
    assert change.get("when") == later["reviewed_at"]
    assert verify_job(job)["status"] == "pass"


def test_a_plain_receipt_cannot_replace_a_correction(job, tmp_path):
    page = _pages(job)[0]
    attempt = next(a for a in page["attempts"] if a["attempt_id"] == page["selected_attempt"])
    decision = {
        "page_id": page["page_id"],
        "page_number": 1,
        "disposition": "reviewed_text",
        "selected_attempt": attempt["attempt_id"],
        "output_sha256": attempt["raw_sha256"],
        "reason": "looks fine",
        "reviewer": "B. Old",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "source_sha256": page["source_sha256"],
                "decisions": [decision],
            }
        )
    )
    review_job(
        job,
        _decisions(job, tmp_path / "view", [_row(1, decision="correct", text=TEXT)]),
        reviewer="A. Reader",
    )
    before = (job / "job.json").read_bytes()

    for dry_run in (True, False):
        with pytest.raises(ValueError, match="Page 1 carries text corrected by A. Reader"):
            review_job(job, stale, dry_run=dry_run)
    assert (job / "job.json").read_bytes() == before

    # A receipt that carries the correction keeps it, and is still accepted.
    kept = tmp_path / "kept.json"
    kept.write_text(json.dumps(_pages(job)[0]["review"]))
    review_job(job, kept)
    out = tmp_path / "book.txt"
    export_job(job, out, format="txt")
    assert TEXT in out.read_text().split("Page 2")[0]
    assert verify_job(job)["status"] == "pass"


def test_a_correction_of_an_edition_starts_from_the_edition(setup, monkeypatch, tmp_path):  # noqa: F811
    page = _adjudicated(setup, monkeypatch, tmp_path)
    edition = (setup[2] / page["edition"]["artifact"]).read_text(encoding="utf-8")
    (tmp_path / "edition.txt").write_text(edition.replace("полные", "полныя", 1), encoding="utf-8")

    assert _review(setup, tmp_path, "correct:edition.txt")["status"] == "completed"

    decision = read_record(setup[2] / "job.json")["pages"][0]["review"]["decisions"][0]
    assert decision["output_sha256"] == page["edition"]["sha256"]
    assert decision["correction"]["from_sha256"] == page["edition"]["sha256"]
    selected = json.loads((setup[2] / "document.json").read_text())["pages"][0]["selected_output"]
    assert "edition" not in selected
    assert selected["correction"]["from_path"] == page["edition"]["artifact"]
    assert selected["correction"]["words"] == {"changed": 1, "added": 0, "removed": 0}
    assert verify_job(setup[2])["status"] == "pass"


def test_a_tampered_or_missing_correction_fails_verification(job, tmp_path):
    review_job(
        job,
        _decisions(job, tmp_path / "one", [_row(1, decision="correct", text=TEXT.upper())]),
        reviewer="A. Reader",
    )
    review_job(
        job,
        _decisions(job, tmp_path / "two", [_row(1, decision="correct", text=TEXT)]),
        reviewer="B. Reader",
    )
    first, second = (
        receipt["decisions"][0]["correction"]["artifact"]
        for receipt in _pages(job)[0]["review_history"]
    )
    for artifact in (first, second):
        target = job / artifact
        original = target.read_bytes()
        target.write_bytes(original + b" ")
        result = verify_job(job)
        assert result["status"] == "fail" and "Corrected text changed" in result["error"]
        target.unlink()
        assert verify_job(job)["status"] == "fail"
        target.write_bytes(original)
    assert verify_job(job)["status"] == "pass"

    job_record = read_record(job / "job.json")
    job_record["pages"][0]["review"]["decisions"][0]["correction"]["from_sha256"] = "1" * 64
    job_record["pages"][0]["review_history"][-1] = job_record["pages"][0]["review"]
    write_record(job / "job.json", job_record)
    result = verify_job(job)
    assert result["status"] == "fail" and "does not record" in result["error"]


def test_a_moved_copy_accepts_corrections(job, tmp_path):
    copy = tmp_path / "elsewhere" / "job"
    shutil.copytree(job, copy)
    path = _decisions(copy, tmp_path / "view", [_row(1, decision="correct", text=TEXT)])

    review_job(copy, path, reviewer="A. Reader")

    record = read_record(copy / "job.json")
    assert record["relocations"][-1]["command"] == "review-job"
    assert record["root"] == str(job)
    assert (copy / record["pages"][0]["review"]["decisions"][0]["correction"]["artifact"]).exists()
    assert not (job / "corrections").exists()
    assert verify_job(copy)["status"] == "pass"


def test_exact_text_survives_apart_from_line_endings(job, tmp_path):
    typed = "Тула ѣ е́\tтабуляция  \r\nвторая строка   \r\nтретья\rконец  "
    path = _decisions(job, tmp_path, [_row(1, decision="correct", text=typed)])

    review_job(job, path, reviewer="A. Reader")

    artifact = _pages(job)[0]["review"]["decisions"][0]["correction"]["artifact"]
    stored = (job / artifact).read_bytes()
    assert stored == normalize(typed).encode("utf-8")
    assert stored == "Тула ѣ е́\tтабуляция  \nвторая строка   \nтретья\nконец  ".encode()
    document = json.loads((job / "document.json").read_text())
    assert document["pages"][0]["selected_output"]["text"] == normalize(typed)
    assert verify_job(job)["status"] == "pass"


def test_word_changes_count_exact_words():
    assert word_changes("a b c", "a x c") == {"changed": 1, "added": 0, "removed": 0}
    assert word_changes("a b c", "a b c d") == {"changed": 0, "added": 1, "removed": 0}
    assert word_changes("a b c", "a c") == {"changed": 0, "added": 0, "removed": 1}
    # ѣ against е, and a combining accent, are changes even though they look alike.
    assert word_changes("хлѣб", "хлеб")["changed"] == 1
    assert word_changes("мука", "му́ка")["changed"] == 1
    assert word_changes("a  b", "a b") == {"changed": 0, "added": 0, "removed": 0}


@pytest.fixture
def corrected(job, tmp_path):
    review_job(
        job,
        _decisions(job, tmp_path / "view", [_row(1, decision="correct", note="fixed", text=TEXT)]),
        reviewer="A. Reader",
    )
    return job


def test_txt_and_md_exports_carry_the_corrected_text_and_say_so(corrected, tmp_path):
    for format in ("txt", "md"):
        out = tmp_path / f"book.{format}"
        export_job(corrected, out, format=format)
        content = out.read_text()
        first = content.split("Page 2")[0]
        assert TEXT in first and WRONG not in first
        assert "Correction: corrections/doc_0001_page_0001-" in first
        assert "the text as A. Reader corrected it on 20" in first
        assert "1 word changed, 0 added, 0 removed" in first
        assert "Correction SHA-256: " in first


def test_jsonl_export_marks_the_correction(corrected, tmp_path):
    out = tmp_path / "book.jsonl"
    export_job(corrected, out, format="jsonl")
    first, second = (json.loads(line) for line in out.read_text().splitlines())
    schema = _schema("export-page.schema.json")
    validate(first, schema)
    validate(second, schema)
    assert first["text"] == TEXT
    assert first["correction"]["path"].startswith("corrections/")
    assert first["correction"]["words"] == {"changed": 1, "added": 0, "removed": 0}
    assert first["review"]["correction"]["sha256"] == first["correction"]["sha256"]
    assert "correction" not in second


def test_tei_export_records_the_correction_as_a_change(corrected, tmp_path):
    out = tmp_path / "book.xml"
    export_job(corrected, out, format="tei")
    tei = ET.fromstring(out.read_bytes())
    change = tei.find(f"{TEI}teiHeader/{TEI}revisionDesc/{TEI}change")
    assert change is not None
    assert change.get("{http://www.w3.org/XML/1998/namespace}id") == "correction-1"
    assert "corrected by A. Reader" in change.text and change.text.endswith(": fixed")
    first, second = tei.findall(f".//{TEI}div[@type='page']")
    assert first.get("subtype") == "reviewed" and first.get("change") == "#correction-1"
    assert second.get("change") is None
    assert first.find(f"{TEI}ab").text == TEXT
    xmllint = shutil.which("xmllint")
    if xmllint is None:
        pytest.skip("xmllint is not installed")
    schema = ROOT / "tests" / "fixtures" / "tei" / "tei_all.rng"
    subprocess.run([xmllint, "--noout", "--relaxng", str(schema), str(out)], check=True)
