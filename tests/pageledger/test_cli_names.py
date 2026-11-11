"""Single-word commands: one command for a job or a run, and the earlier names still work."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest
from test_stability import _copy_release

from pageledger import cli
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
    assert _cli(capsys, *new) == before


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "one of the arguments --out --sheet --apply is required"),
        (["--out", "v", "--sheet", "s.csv"], "not allowed with argument"),
        (["--sheet", "s.csv", "--apply", "s.csv"], "not allowed with argument"),
        (["--sheet", "s.csv", "--images", "held"], "--images goes with --out"),
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


def test_every_command_is_listed_once_or_is_an_earlier_name() -> None:
    listed = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert len(listed) == len(set(listed)) == len(cli.COMMAND_HELP)
    assert set(listed) == set(cli.COMMAND_HELP)
    assert set(_choices()) == set(listed) | set(EARLIER_NAMES)
    assert "review-html" not in _choices() and "fetch-iiif" not in _choices()
    assert all(new in listed for new in EARLIER_NAMES.values())


def test_top_level_help_is_grouped_and_lists_no_earlier_name(
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
    listed = re.findall(r"^  ([a-z]+) {2,}\S", out, flags=re.MULTILINE)
    assert listed == [name for _, names in cli.COMMAND_GROUPS for name in names]
    for name in [*EARLIER_NAMES, "review-html", "fetch-iiif"]:
        assert name not in out
    assert out.rstrip().endswith("docs/document-first-run.md")


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
