"""Single-word commands: one command for a job or a run, and the earlier names still work."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
from pathlib import Path

import pytest
from test_stability import _copy_release

from pageledger import cli
from pageledger._version import __version__
from pageledger.cli import main

ROOT = Path(__file__).resolve().parents[2]

# Released names that stay accepted, exactly, and are never listed.
EARLIER_NAMES = {
    "inspect-job": "inspect",
    "inspect-run": "inspect",
    "verify-job": "verify",
    "verify-run": "verify",
    "review-job": "review",
    "review-sheet": "review",
    "init-config": "init",
    "compare-runs": "compare",
}


@pytest.fixture
def job(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> Path:
    monkeypatch.delenv("PAGELEDGER_REVIEWER", raising=False)
    source = tmp_path / "letters.txt"
    source.write_text(
        "The first letter, written from the estate in the spring, describes the harvest.\f"
        "The second letter answers it and asks after the family in the town.",
        encoding="utf-8",
    )
    assert main(["process", str(source), "--out", str(tmp_path / "job")]) == 0
    capsys.readouterr()
    return tmp_path / "job"


@pytest.fixture
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    source = tmp_path / "pages.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    assert main(["run", str(source), "--adapter", "text", "--out", str(tmp_path / "run")]) == 0
    capsys.readouterr()
    return tmp_path / "run"


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    try:
        code = main(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- one command for a job or a run --------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--json"]])
@pytest.mark.parametrize(
    ("new", "old", "target"),
    [
        ("verify", "verify-job", "job"),
        ("verify", "verify-run", "run"),
        ("verify", "verify-run", "stage"),
        ("inspect", "inspect-job", "job"),
        ("inspect", "inspect-run", "run"),
        ("inspect", "inspect-run", "stage"),
    ],
)
def test_a_folder_is_recognized_and_reads_as_the_earlier_command(
    job: Path,
    run: Path,
    capsys: pytest.CaptureFixture[str],
    new: str,
    old: str,
    target: str,
    flags: list[str],
) -> None:
    folder = {"job": job, "run": run, "stage": job / "attempts" / "local_text"}[target]
    before = _cli(capsys, old, str(folder), *flags)
    after = _cli(capsys, new, str(folder), *flags)
    assert before[0] == 0 and before[1]
    assert after == before


@pytest.mark.parametrize("version", ["0.5.2", "0.6.0", "0.7.0"])
@pytest.mark.parametrize(("kind", "old"), [("job", "verify-job"), ("run", "verify-run")])
def test_released_jobs_and_runs_verify_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str, kind: str, old: str
) -> None:
    folder = _copy_release(version, kind, tmp_path)
    before = _cli(capsys, old, str(folder), "--json")
    assert before[0] == 0
    assert _cli(capsys, "verify", str(folder), "--json") == before


def test_inspect_csv_of_a_run_matches_inspect_run(
    run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _cli(capsys, "inspect-run", str(run), "--csv")
    assert before[0] == 0 and before[1].startswith("page_id")
    assert _cli(capsys, "inspect", str(run), "--csv") == before


def test_a_failing_verification_keeps_its_exit_code_and_report(
    job: Path, run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (run / "quality.jsonl").write_text("", encoding="utf-8")
    (job / "report.md").write_text("changed\n", encoding="utf-8")
    for old, folder in (("verify-run", run), ("verify-job", job)):
        before = _cli(capsys, old, str(folder), "--json")
        assert before[0] == 1 and json.loads(before[1])["status"] == "fail"
        assert _cli(capsys, "verify", str(folder), "--json") == before


@pytest.mark.parametrize("command", ["verify", "inspect", "resume"])
@pytest.mark.parametrize(
    ("make", "message"),
    [
        ("missing", "does not exist"),
        ("file", "is a file"),
        ("empty", "is not a job or a run folder"),
        ("both", "has the records of both a job and a run"),
    ],
)
def test_a_folder_that_is_neither_is_refused_naming_what_was_looked_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, make: str, message: str
) -> None:
    target = tmp_path / "target"
    if make == "file":
        target.write_text("not a folder", encoding="utf-8")
    elif make == "empty":
        target.mkdir()
    elif make == "both":
        target.mkdir()
        (target / "job.json").write_text("{}", encoding="utf-8")
        (target / "manifest.json").write_text("{}", encoding="utf-8")
    code, out, err = _cli(capsys, command, str(target))
    assert code == 1 and out == ""
    assert message in err
    if make != "missing":
        assert "job.json" in err and "manifest.json" in err
    code, out, _ = _cli(capsys, command, str(target), "--json")
    assert code == 1
    result = json.loads(out)
    assert result["status"] == "error" and message in result["error"]


def test_inspect_csv_is_for_runs(job: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _cli(capsys, "inspect", str(job), "--csv")
    assert code == 1 and out == ""
    assert "--csv lists a run's pages" in err


def test_compare_is_compare_runs(run: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for flags in ([], ["--json"]):
        before = _cli(capsys, "compare-runs", str(run), str(run), *flags)
        assert before[0] == 0 and before[1]
        assert _cli(capsys, "compare", str(run), str(run), *flags) == before


# -- review ------------------------------------------------------------------


def test_review_writes_the_page_view_by_default(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "view"
    code, stdout, _ = _cli(capsys, "review", str(job), "--out", str(out), "--images", "none")
    assert code == 0 and stdout.startswith(f"Review view: {out}")
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "pageledger review " in page and "--apply" in page
    assert "review-job" not in page and "review-html" not in page


def test_review_with_no_option_writes_the_view_beside_the_job(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, stdout, _ = _cli(capsys, "review", f"{job}/", "--images", "none")
    view = tmp_path / "job-review"
    assert code == 0 and (view / "index.html").is_file()
    assert stdout.startswith(f"Review view: {view / 'index.html'} ")
    assert "Open that file in a web browser" in stdout
    assert "fill in review.csv beside it, as review-decisions.txt there explains." in stdout
    assert "Blank" not in stdout

    # Asked again, with nothing changed: the view there is the job's.
    code, stdout, err = _cli(capsys, "review", str(job))
    assert code == 0 and err == ""
    assert stdout.splitlines() == [
        f"The review view in {view} is up to date with the job.",
        f"Open {view / 'index.html'} in a web browser to see the pages.",
    ]
    # A view asked for with other images is a new one.
    code, stdout, err = _cli(capsys, "review", str(job), "--images", "held")
    assert code == 1 and f"{view} already exists" in err

    # Accepting a page changes no binding, but the view no longer shows the job.
    sheet = view / "review.csv"
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["decision"] = "accept"
    with sheet.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert main(["review", str(job), "--apply", str(sheet), "--reviewer", "A"]) == 0
    capsys.readouterr()
    code, stdout, err = _cli(capsys, "review", str(job))
    assert code == 1 and stdout == ""
    assert f"{view} shows the job as it was before its last change" in err
    assert "--out DIR" in err

    # A folder that is not a review view of the job is left alone.
    (view / "index.html").unlink()
    code, stdout, err = _cli(capsys, "review", str(job))
    assert code == 1 and f"{view} already exists" in err and "--out DIR" in err


def test_review_of_the_current_folder_writes_beside_it(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    monkeypatch.chdir(job)
    assert _cli(capsys, "review", ".", "--images", "none")[0] == 0
    assert (tmp_path / "job-review" / "index.html").is_file()
    assert not (job / "job-review").exists()


def test_review_apply_says_what_was_recorded_and_what_comes_next(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sheet = tmp_path / "review.csv"
    assert main(["review", str(job), "--sheet", str(sheet)]) == 0
    capsys.readouterr()
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row["decision"] = "accept" if row["page_number"] == "1" else "blank"
    with sheet.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    code, stdout, _ = _cli(capsys, "review", str(job), "--apply", str(sheet), "--reviewer", "A")
    assert code == 0
    assert stdout.splitlines() == [
        "Recorded: 2 pages: 1 accepted, 1 blank.",
        "No page needs a person now.",
        f"Next: pageledger export {job} --out job.md",
    ]


def test_review_sheet_is_review_sheet(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sheet = tmp_path / "review.csv"
    binding = tmp_path / "review.csv.binding.json"
    before = _cli(capsys, "review-sheet", str(job), "--out", str(sheet), "--json")
    written = sheet.read_bytes(), binding.read_bytes()
    sheet.unlink()
    binding.unlink()
    assert before[0] == 0
    assert _cli(capsys, "review", str(job), "--sheet", str(sheet), "--json") == before
    assert (sheet.read_bytes(), binding.read_bytes()) == written


@pytest.mark.parametrize("flags", [["--dry-run"], ["--dry-run", "--json"]])
def test_review_apply_is_review_job(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    sheet = tmp_path / "review.csv"
    assert main(["review-sheet", str(job), "--out", str(sheet)]) == 0
    capsys.readouterr()
    old = ["review-job", str(job), "--review", str(sheet), "--reviewer", "A. Reader", *flags]
    new = ["review", str(job), "--apply", str(sheet), "--reviewer", "A. Reader", *flags]
    before = _cli(capsys, *old)
    assert before[0] == 0
    after = _cli(capsys, *new)
    if "--json" in flags:
        assert after == before
    else:
        # The earlier name keeps its released lines; review --apply says it in words.
        assert before[1] == "Status: checked\nDecisions: none\n"
        assert after == (0, "Checked, and nothing recorded (dry run): no decisions.\n", "")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--out", "v", "--sheet", "s.csv"], "not allowed with argument"),
        (["--sheet", "s.csv", "--apply", "s.csv"], "not allowed with argument"),
        (["--sheet", "s.csv", "--images", "held"], "--images is for the review view"),
        (["--out", "v", "--reviewer", "A"], "--reviewer and --dry-run go with --apply"),
        (["--sheet", "s.csv", "--dry-run"], "--reviewer and --dry-run go with --apply"),
    ],
)
def test_review_modes_are_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    code, out, err = _cli(capsys, "review", str(tmp_path), *args)
    assert code == 2 and out == ""
    assert message in err


# -- init --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "old"),
    [
        ([], ["--processing"]),
        (["--lang", "rus+eng"], ["--processing", "--lang", "rus+eng"]),
        (["--engine", "none"], ["--processing", "--engine", "none"]),
        (["--run"], []),
        (["--run", "--adapter", "pdf_ocr"], ["--adapter", "pdf_ocr"]),
    ],
)
def test_init_writes_a_job_config_and_with_run_a_run_config(
    capsys: pytest.CaptureFixture[str], new: list[str], old: list[str]
) -> None:
    before = _cli(capsys, "init-config", *old)
    assert before[0] == 0
    assert _cli(capsys, "init", *new) == before


def test_init_by_default_writes_the_processing_example(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = ROOT / "docs" / "examples" / "processing.yml"
    code, out, _ = _cli(capsys, "init")
    assert code == 0 and out == example.read_text(encoding="utf-8")
    assert "`pageledger init`" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--adapter", "pdf_ocr"], "--adapter goes with --run"),
        (["--run", "--engine", "indra"], "do not go with --run"),
        (["--run", "--lang", "eng"], "do not go with --run"),
        (["--engine", "rapidocr"], "rapidocr needs --model-dir"),
    ],
)
def test_init_refuses_options_of_the_other_kind(
    capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    code, out, err = _cli(capsys, "init", *args)
    assert code == 2 and out == ""
    assert message in err


def test_init_writes_to_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "processing.yml"
    code, out, err = _cli(capsys, "init", "--out", str(path))
    assert code == 0 and out == "" and err == f"Wrote {path}\n"
    assert path.read_text(encoding="utf-8").startswith("# A PageLedger document job")


# -- help --------------------------------------------------------------------


def _choices() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    (action,) = (a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def _listed(out: str) -> list[str]:
    return re.findall(r"^  ([a-z]+) {2,}\S", out, flags=re.MULTILINE)


def test_every_command_is_on_the_first_screen_or_the_pipelines_screen_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _listed(_cli(capsys, "--help")[1])
    pipelines = _listed(_cli(capsys, "help", "pipelines")[1])
    listed = first + pipelines
    assert len(listed) == len(set(listed)) == len(cli.COMMAND_HELP)
    assert set(listed) == set(cli.COMMAND_HELP)
    assert set(_choices()) == set(listed) | set(EARLIER_NAMES)
    assert "review-html" not in _choices() and "fetch-iiif" not in _choices()
    assert all(new in first for new in EARLIER_NAMES.values() if new != "compare")
    assert "compare" in pipelines and "adjudicate" in pipelines


def test_top_level_help_lists_the_document_commands_and_points_to_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _cli(capsys, "--help")
    assert code == 0
    usage = out.splitlines()[0]
    assert usage == "usage: pageledger [-h] [--version] COMMAND ..."
    assert "{" not in out
    headings = [heading for heading, _ in cli.COMMAND_GROUPS]
    positions = [out.index(f"\n{heading}:\n") for heading in headings]
    assert positions == sorted(positions)
    assert _listed(out) == [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert _listed(out)[:6] == ["process", "review", "export", "verify", "inspect", "resume"]
    assert "pageledger help pipelines" in out
    for name in [*EARLIER_NAMES, "review-html", "fetch-iiif", "adjudicate"]:
        assert name not in out
    assert out.rstrip().endswith("docs/document-first-run.md")


def test_help_pseudo_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(capsys, "help") == _cli(capsys, "--help")
    assert _cli(capsys, "help", "export") == _cli(capsys, "export", "--help")
    code, out, _ = _cli(capsys, "help", "pipelines")
    assert code == 0 and "\nFor jobs with a model stage:\n  adjudicate" in out
    code, out, err = _cli(capsys, "help", "nothing")
    assert code == 2 and out == "" and "no help for 'nothing'" in err


def test_process_help_shows_only_lang_as_an_engine_choice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = " ".join(_cli(capsys, "process", "--help")[1].split())
    assert "--lang" in out and "`pageledger doctor` lists the languages installed" in out
    assert "--engine" not in out and "--model-dir" not in out and "pdf_ocr" not in out
    init = _cli(capsys, "init", "--help")[1]
    assert "--engine" in init and "--model-dir" in init


def test_each_listed_command_help_opens_with_a_description_and_an_example(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, (_, about, example) in cli.COMMAND_HELP.items():
        code, out, _ = _cli(capsys, name, "--help")
        assert code == 0
        assert out.startswith(f"usage: pageledger {name} ")
        assert " ".join(about.split()) in " ".join(out.split())
        assert f"example:\n  {example}" in out
        assert f"{example} ".startswith(f"pageledger {name} ")


def test_an_unknown_command_names_no_earlier_name(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _cli(capsys, "inspct", "x")
    assert code == 2 and out == ""
    assert "unknown command 'inspct'; did you mean inspect?" in err
    for name in EARLIER_NAMES:
        assert name not in err


@pytest.mark.parametrize("name", sorted(EARLIER_NAMES))
def test_an_earlier_name_keeps_its_own_usage(capsys: pytest.CaptureFixture[str], name: str) -> None:
    code, out, _ = _cli(capsys, name, "--help")
    assert code == 0 and out.startswith(f"usage: pageledger {name} ")


# -- export ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "first"),
    [
        ("book.txt", "letters.txt\n"),
        ("book.md", "# "),
        ("book.jsonl", "{"),
        ("book.xml", "<?xml"),
        ("book.tei.xml", "<?xml"),
    ],
)
def test_export_takes_the_format_from_the_file_ending(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, first: str
) -> None:
    out = tmp_path / name
    assert _cli(capsys, "export", str(job), "--out", str(out))[0] == 0
    assert out.read_text(encoding="utf-8").startswith(first)


def test_export_format_wins_and_an_unknown_ending_is_a_usage_error(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "book.md"
    assert _cli(capsys, "export", str(job), "--out", str(out), "--format", "jsonl")[0] == 0
    assert out.read_text(encoding="utf-8").startswith("{")
    code, stdout, err = _cli(capsys, "export", str(job), "--out", str(tmp_path / "book.docx"))
    assert code == 2 and stdout == ""
    assert "cannot tell the format from book.docx" in err and "txt, md, jsonl, tei" in err


# -- a job where a run belongs, and the reverse ---------------------------------


def test_run_commands_say_when_given_a_job(
    job: Path, run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(capsys, "compare", str(job), str(run))
    assert code == 1
    assert f"`compare` takes a run folder, and {job} is a document job" in err
    assert f"pageledger review {job}" in err
    code, _, err = _cli(capsys, "rerun", str(job), "--out", "x")
    assert code == 2 and f"`rerun` takes a run folder, and {job} is a document job" in err
    code, _, err = _cli(capsys, "align", str(job))
    assert code == 1 and "`align` takes a run folder" in err


def test_job_commands_say_when_given_a_run(
    run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["review", str(run)], ["export", str(run), "--out", str(tmp_path / "x.txt")]):
        code, _, err = _cli(capsys, *argv)
        assert code == 1
        assert f"takes a document job, and {run} is a run folder" in err
        assert f"pageledger inspect {run}" in err


def test_run_without_a_config_points_to_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(capsys, "run", str(tmp_path), "--out", str(tmp_path / "run"))
    assert code == 2
    assert "one of the arguments --config --adapter is required" in err
    assert "pageledger process SOURCE --out DIR" in err


# -- doctor and the opening line of process --------------------------------------


def _doctor_report(*, tesseract: bool = True, pdftoppm: bool = True) -> dict:
    command = {"install_hint": "Install it.", "version": None}
    return {
        "optional_packages": {
            "pypdf": {"available": True},
            "rapidocr": {"available": True},
            "onnxruntime": {"available": True},
            "indra_ocr": {"available": False},
        },
        "external_commands": {
            "tesseract": {**command, "available": tesseract, "version": "tesseract 5.5.2"},
            "pdftoppm": {**command, "available": pdftoppm, "install_hint": "Install poppler."},
        },
        "ocr_languages": {"available": tesseract, "languages": ["deu", "eng", "osd", "rus"]},
    }


def test_doctor_opens_with_what_process_can_do() -> None:
    assert cli._doctor_summary(_doctor_report()) == [
        "What process can do on this machine:",
        "  Read the text a PDF carries (pypdf).",
        "  Read page images and scanned PDFs with Tesseract 5.5.2 OCR.",
        "  Languages for --lang (3 installed): eng, rus, deu.",
        "  Other OCR engines ready (choose one with pageledger init): RapidOCR.",
        "  Not installed, optional: Indra OCR. See "
        f"https://github.com/peterbussch/pageledger/blob/v{__version__}/docs/engine-recipes.md",
    ]


def test_doctor_lists_installed_languages_not_tesseract_s_own_data() -> None:
    report = _doctor_report()
    report["ocr_languages"]["languages"] = ["equ", "frk", "osd", "san", "tam"]
    assert cli._doctor_summary(report, details=False)[3] == (
        "  Languages for --lang (3 installed): frk, san, tam."
    )
    report["ocr_languages"]["languages"] = ["osd"]
    assert not any("--lang" in line for line in cli._doctor_summary(report))


def test_doctor_says_what_is_missing_for_ocr() -> None:
    lines = cli._doctor_summary(_doctor_report(pdftoppm=False))
    assert lines[2] == (
        "  Read page images (scanned PDFs also need pdftoppm: Install poppler.) "
        "with Tesseract 5.5.2 OCR."
    )
    lines = cli._doctor_summary(_doctor_report(tesseract=False))
    assert lines[2].startswith("  No OCR: Tesseract is not installed")
    assert not any("--lang" in line for line in lines)


def test_doctor_text_opens_with_the_summary_and_json_is_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _cli(capsys, "doctor")
    assert code == 0 and out.startswith("What process can do on this machine:\n")
    assert out.endswith(
        "\n\n`pageledger doctor --all` shows every package, command, language and key.\n"
    )
    assert "Details:" not in out and "listed below" not in out
    code, every, _ = _cli(capsys, "doctor", "--all")
    assert code == 0 and every.startswith("What process can do on this machine:\n")
    assert "\n\nDetails:\nPageLedger " in every and "--all" not in every
    code, out, _ = _cli(capsys, "doctor", "--json")
    assert "What process" not in out and json.loads(out)["optional_packages"]


def test_the_opening_line_fits_each_kind_of_source(tmp_path: Path) -> None:
    pdf, notes, scans = tmp_path / "book.pdf", tmp_path / "notes.txt", tmp_path / "scans"
    pdf.write_bytes(b"%PDF-1.4\n")
    notes.write_text("a page", encoding="utf-8")
    scans.mkdir()
    (scans / "0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert cli._reading_line(pdf, "pdf_ocr", {"lang": "rus+eng"}) == (
        "Reading book.pdf: each page's own text, then Tesseract OCR in Russian and English "
        "for pages that need it. Nothing is sent anywhere."
    )
    assert cli._reading_line(notes, "none", None) == (
        "Reading notes.txt: the text of each page, as it is. Nothing is sent anywhere."
    )
    assert cli._reading_line(scans, "pdf_ocr", {"lang": "eng"}) == (
        "Reading scans: Tesseract OCR in English, page by page. Nothing is sent anywhere."
    )
    assert cli._reading_line(scans, "rapidocr", {"rec_model_dir": "m"}) == (
        "Reading scans: RapidOCR, page by page. Nothing is sent anywhere."
    )


# -- progress and interruption -----------------------------------------------


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_progress_rewrites_one_line_on_a_terminal() -> None:
    stream = _Terminal()
    progress = cli._Progress(stream=stream)
    progress({"stage": "local_text", "pages": [1, 2, 3], "selected": 3, "done": 0, "held": 0})
    progress({"stage": "local_ocr", "pages": [2], "selected": 3, "done": 1, "held": 1})
    progress.clear()
    written = stream.getvalue()
    assert "\n" not in written
    assert "\r0 of 3 pages done. Now: checking for text the pages already carry" in written
    assert "\r1 of 3 pages done; 1 needs a person so far. Now: OCR, page 2" in written
    assert written.endswith("\r")


def test_progress_fits_the_terminal_and_names_page_images(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda: os.terminal_size((40, 24)))
    stream = _Terminal()
    progress = cli._Progress(stream=stream)
    event = {"stage": "local_text", "pages": [1], "selected": 1, "done": 0, "held": 0}
    progress({**event, "page_images": True})
    assert stream.getvalue() == "\r" + "0 of 1 pages done. Now: preparing the page images"[:39]


def test_progress_off_a_terminal_is_quiet_at_first_then_plain_lines(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    stream = io.StringIO()
    progress = cli._Progress(stream=stream)
    event = {"stage": "local_ocr", "pages": [4], "selected": 9, "done": 3, "held": 0}
    progress(event)
    assert stream.getvalue() == ""
    clock[0] = 20.0
    progress(event)
    progress(event)
    clock[0] = 51.0
    progress(event)
    assert stream.getvalue().splitlines() == ["3 of 9 pages done. Now: OCR, page 4"] * 2
    progress({"notice": "Notice: something"})
    assert stream.getvalue().endswith("Notice: something\n")


def test_progress_is_silent_with_json_but_notices_are_not() -> None:
    stream = _Terminal()
    progress = cli._Progress(show=False, stream=stream)
    progress({"stage": "local_ocr", "pages": [1], "selected": 1, "done": 0, "held": 0})
    assert stream.getvalue() == ""
    progress({"notice": "Notice: x"})
    assert stream.getvalue() == "Notice: x\n"


def test_an_interrupted_job_stops_cleanly_and_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from pageledger.checkpoint import Checkpoint

    source = tmp_path / "letters.txt"
    source.write_text("The first page of letters.\fThe second page.\fThe third.", encoding="utf-8")
    out = tmp_path / "jobs" / "letters"
    original = Checkpoint.save

    def stop(self, page_id, record):
        original(self, page_id, record)
        if record["state"] == "completed" and page_id.endswith("0002"):
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "save", stop)
        code, _, err = _cli(capsys, "process", str(source), "--out", str(out))
    assert code == 130
    assert "Traceback" not in err
    assert err.endswith(
        f"\nInterrupted. The pages read so far are kept in {out}.\n"
        f"To continue: pageledger resume {out}\n"
    )
    code, stdout, _ = _cli(capsys, "verify", str(out))
    assert code == 1 and stdout.startswith("Status: fail (unfinished)")
    code, stdout, _ = _cli(capsys, "resume", str(out))
    assert code == 0 and stdout.startswith("Finished: 3 pages read;")
    assert _cli(capsys, "verify", str(out))[0] == 0


def test_an_interrupt_before_the_job_exists_says_nothing_was_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from pageledger import processing

    def stop(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(processing, "_process", stop)
    source = tmp_path / "letters.txt"
    source.write_text("A page.", encoding="utf-8")
    code, _, err = _cli(capsys, "process", str(source), "--out", str(tmp_path / "j"))
    assert code == 130 and err.endswith("\nInterrupted before anything was written.\n")


def test_an_interrupted_run_or_fetch_says_how_to_go_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from pageledger import iiif

    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    source = tmp_path / "pages.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    monkeypatch.setattr(cli, "run", stop)
    out = tmp_path / "run"
    code, _, err = _cli(capsys, "run", str(source), "--adapter", "text", "--out", str(out))
    assert code == 130 and err.endswith("\nInterrupted before anything was written.\n")
    out.mkdir()
    code, _, err = _cli(capsys, "run", str(source), "--adapter", "text", "--out", str(out))
    assert code == 130 and "the run was not started with --resumable" in err
    (out / "checkpoint.json").write_text("{}", encoding="utf-8")
    code, _, err = _cli(capsys, "run", str(source), "--adapter", "text", "--out", str(out))
    assert f"To continue: pageledger resume {out}\n" in err

    monkeypatch.setattr(iiif, "fetch_iiif", stop)
    url = "https://example.org/iiif/manifest.json"
    code, _, err = _cli(capsys, "fetch", url, "--out", str(out))
    assert code == 130
    assert f"To fetch the rest: pageledger fetch {url} --out {out} --resume\n" in err


def test_an_interrupted_review_view_leaves_nothing(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from pageledger import review_html

    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(review_html, "_write_view", stop)
    code, _, err = _cli(capsys, "review", str(job), "--images", "none")
    assert code == 130 and "No review view was written" in err
    assert not (tmp_path / "job-review").exists()


def test_doctor_names_a_missing_or_broken_config_in_a_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nothere.yml"
    assert main(["doctor", "--config", str(missing)]) == 1
    assert capsys.readouterr().err == f"pageledger: error: Config file not found: {missing}\n"
    broken = tmp_path / "broken.yml"
    broken.write_text("processing: [\n", encoding="utf-8")
    assert main(["doctor", "--config", str(broken)]) == 1
    assert "Could not parse config YAML in" in capsys.readouterr().err


def test_doctor_with_a_config_lists_only_the_keys_it_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "processing.yml"
    config.write_text("processing:\n  local_ocr: {adapter: pdf_ocr}\n", encoding="utf-8")
    assert main(["doctor", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "API keys: processing.yml names none (no env_key)." in out
    assert "OPENAI_API_KEY" not in out and "Env " not in out


# -- plain screens (review 2026-11-13) -------------------------------------------


def test_bare_pageledger_prints_the_command_list(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _cli(capsys)
    assert code == 0 and out.startswith("usage: pageledger") and err == ""
    assert "process" in out


def _text_job(tmp_path: Path, capsys: pytest.CaptureFixture[str], *args: str) -> Path:
    source = tmp_path / "book.txt"
    source.write_text("first page\fsecond page\fthird page\n", encoding="utf-8")
    out = tmp_path / "jobs" / "book"
    code, _, err = _cli(capsys, "process", str(source), "--out", str(out), *args)
    assert code == 0, err
    return out


def test_verify_says_what_a_pass_means(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    job = _text_job(tmp_path, capsys)
    code, out, _ = _cli(capsys, "verify", str(job))
    assert code == 0
    assert out.splitlines()[:2] == ["Status: pass", cli.VERIFIED]
    assert "does not check that the text is right" in cli.VERIFIED


def test_init_never_writes_over_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "processing.yml"
    path.write_text("# my own settings\n", encoding="utf-8")
    code, _, err = _cli(capsys, "init", "--out", str(path))
    assert code == 1 and "already exists" in err
    assert path.read_text(encoding="utf-8") == "# my own settings\n"


def test_an_interrupt_names_the_options_the_rest_needs(tmp_path: Path) -> None:
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "job.json").write_text("{}", encoding="utf-8")
    process = argparse.Namespace(command="process", out=folder, adapter_path=tmp_path / "my ad")
    assert cli._interrupted(process).endswith(
        f"pageledger resume {folder} --adapter-path '{tmp_path / 'my ad'}'"
    )
    fetch = argparse.Namespace(
        command="fetch",
        out=folder,
        url="https://example.org/manifest",
        pages="1-8",
        delay=3.0,
        max_image_bytes=1000,
    )
    assert cli._interrupted(fetch).endswith(
        "--pages 1-8 --delay 3.0 --max-image-bytes 1000 --resume"
    )
    fetch.delay, fetch.max_image_bytes, fetch.pages = 1.0, 256 * 1024 * 1024, None
    assert cli._interrupted(fetch).endswith(f"--out {folder} --resume")


def test_a_review_of_a_paused_job_names_its_folder_and_status_in_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "cfg.yml"
    config.write_text(
        "processing:\n  local_text: {adapter: text}\n  limits: {max_attempt_pages: 1}\n",
        encoding="utf-8",
    )
    job = _text_job(tmp_path, capsys, "--config", str(config))
    document = json.loads((job / "document.json").read_text(encoding="utf-8"))
    assert document["status"] == "paused_budget"
    receipt = tmp_path / "empty.json"
    receipt.write_text(
        json.dumps({"schema_version": "0.1", "source_sha256": _source_sha(job), "decisions": []}),
        encoding="utf-8",
    )
    code, out, _ = _cli(capsys, "review", str(job), "--apply", str(receipt))
    assert "JOB_DIR" not in out and "Status: paused_budget" not in out
    assert "Paused at a limit" in out and f"pageledger resume {job}" in out


def _source_sha(job: Path) -> str:
    from pageledger.checkpoint import read_record

    return read_record(job / "job.json")["source"]["sha256"]


def test_progress_counts_one_page_as_one(tmp_path: Path) -> None:
    event = {"pages": [3], "stage": "local_ocr", "done": 1, "selected": 3, "held": 1}
    assert "1 needs a person so far" in cli._Progress.line(event)
    assert "2 need a person so far" in cli._Progress.line({**event, "held": 2})


def test_a_written_review_sheet_is_told_in_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _text_job(tmp_path, capsys)
    code, out, _ = _cli(capsys, "review", str(job), "--sheet", str(tmp_path / "s.csv"))
    assert code == 0 and "Status:" not in out and "Review sheet:" in out


def test_a_view_without_images_is_found_up_to_date_when_asked_for_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _text_job(tmp_path, capsys)
    assert _cli(capsys, "review", str(job), "--images", "none")[0] == 0
    code, out, err = _cli(capsys, "review", str(job), "--images", "none")
    assert code == 0, err
    assert "up to date" in out
