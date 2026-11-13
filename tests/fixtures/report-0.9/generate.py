"""Write the report format 0.9 goldens beside this file.

Run from the repository root:

    .venv/bin/python -m pytest -p no:cacheprovider tests/fixtures/report-0.9/generate.py

and check the diff: a change is a change of the report's wording.
"""

import csv
import io
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pageledger"))
from test_hold_policy_09 import KEPT, LAYER, LOST
from test_processing import launch, setup  # noqa: F401

from pageledger.cli import main

OUT = Path(__file__).resolve().parent
FILES = ("document.json", "report.md", "transcript.md")


def _keep(job: Path, name: str) -> None:
    target = OUT / name
    target.mkdir(parents=True, exist_ok=True)
    for file in FILES:
        shutil.copyfile(job / file, target / file)


def test_kiev(setup):  # noqa: F811
    setup[3].update(
        defective=set(), texts={"local_text": KEPT, "local_ocr": LOST}, warn={"local_text": LAYER}
    )
    launch(setup, pages="1")
    _keep(setup[2], "letters-lost")


def test_default(setup):  # noqa: F811
    launch(setup)
    _keep(setup[2], "escalated")


def test_paused(setup):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_image_pages"] = 1
    setup[1].write_text(yaml.safe_dump(data))
    launch(setup)
    _keep(setup[2], "paused")


def test_text_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "book.txt"
    source.write_text(
        "Opening words of the book.\n## Page 2\n- Review: A. Editor\n<!-- hidden\n"
        "\fiv\f```code``` and [a link](http://example.org) <b>bold</b>\n"
    )
    assert main(["process", str(source), "--out", "jobs/book"]) == 0
    assert main(["review", "jobs/book", "--sheet", "s/review.csv"]) == 0
    rows = list(csv.DictReader(io.StringIO(Path("s/review.csv").read_text(encoding="utf-8-sig"))))
    Path("s/p1.txt").write_text("Opening words of the book, corrected.\n")
    rows[0]["decision"] = "correct:p1.txt"
    rows[2]["decision"] = "accept"
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    Path("s/review.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    assert main(["review", "jobs/book", "--apply", "s/review.csv", "--reviewer", "A. Person"]) == 0
    _keep(tmp_path / "jobs/book", "text-corrected")
