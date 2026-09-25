"""Execute the maintained first-run tutorial as one dependent shell journey."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fake_pageledger_environment(
    tmp_path: Path, *, distribution_version: str | None
) -> tuple[Path, Path]:
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin" / "python"
    purelib = Path(
        subprocess.run(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    package = purelib / "pageledger"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.4.1"\n', encoding="utf-8")
    if distribution_version is not None:
        distribution = purelib / f"pageledger-{distribution_version}.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: pageledger\nVersion: {distribution_version}\n",
            encoding="utf-8",
        )
    return python, purelib


def test_exact_wheel_mode_rejects_distribution_module_version_mismatch(
    tmp_path: Path,
) -> None:
    python, _ = _fake_pageledger_environment(tmp_path, distribution_version="9.9.9")
    document = tmp_path / "journey.md"
    document.write_text(
        "```bash pageledger-tutorial\necho TUTORIAL_RAN\n```\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(python),
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(document),
            "--work-dir",
            str(tmp_path / "tutorial"),
            "--python",
            str(python),
            "--expected-version",
            "0.4.1",
            "--forbid-import-root",
            str(ROOT),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert result.returncode != 0
    assert "distribution metadata version 9.9.9" in result.stderr
    assert "TUTORIAL_RAN" not in result.stdout


def test_source_mode_allows_import_without_distribution_metadata(tmp_path: Path) -> None:
    python, source_root = _fake_pageledger_environment(tmp_path, distribution_version=None)
    document = tmp_path / "source-journey.md"
    document.write_text(
        "```bash pageledger-tutorial\necho SOURCE_TUTORIAL_RAN\n```\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(python),
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(document),
            "--work-dir",
            str(tmp_path / "source-tutorial"),
            "--python",
            str(python),
            "--source-root",
            str(source_root),
            "--expected-version",
            "0.4.1",
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert result.returncode == 0, result.stderr
    assert "SOURCE_TUTORIAL_RAN" in result.stdout


def test_first_run_tutorial_executes_the_documented_sequence(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(ROOT / "docs" / "first-run.md"),
            "--work-dir",
            str(tmp_path / "tutorial"),
            "--python",
            sys.executable,
            "--source-root",
            str(ROOT),
            "--expected-version",
            "0.5.2",
        ],
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    work = tmp_path / "tutorial"

    first = _lines(work / "runs" / "first" / "quality.jsonl")
    flagged = [(row["page_id"], row["warnings"]) for row in first if row["warnings"]]
    assert flagged == [("doc_0001_page_0002", ["replacement_characters"])]
    pages = list(csv.DictReader((work / "pages.csv").open(newline="", encoding="utf-8")))
    assert (pages[1]["page_number"], pages[1]["warnings"]) == ("2", "replacement_characters")

    second = json.loads((work / "runs" / "second" / "manifest.json").read_text())
    assert second["summary"]["pages_total"] == 1
    assert [row["page_id"] for row in _lines(work / "runs" / "second" / "quality.jsonl")] == [
        "doc_0001_page_0002"
    ]
    assert [path.name for path in (work / "runs" / "second" / "raw").iterdir()] == [
        "doc_0001_page_0002.txt"
    ]
    assert (work / "review" / "decisions.csv").is_file()

    replay = json.loads((work / "runs" / "replayed" / "replay.json").read_text())
    assert replay["outcome"] == "exact"
    assert (replay["raw"]["equal"], replay["raw"]["different"], replay["raw"]["missing"]) == (
        3,
        0,
        0,
    )
    assert (work / "relocated" / "original-source.moved").is_file()
    assert not (work / "sample.txt").exists()


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
