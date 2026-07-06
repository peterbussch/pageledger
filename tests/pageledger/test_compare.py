"""Tests for `pageledger compare-runs` and the compare module.

Verifies:
  - Page matching on page_id across two runs
  - Warning resolved/introduced accounting
  - Rerun directories compare cleanly against their parent (page_id overlap)
  - Only-in-one-run pages are reported, not silently dropped
  - CLI text and JSON output, error on non-run directories
"""

from __future__ import annotations

import json
from pathlib import Path


MINIMAL = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""


def _run(inputs, tmp_path, out_name, *, config_text=MINIMAL):
    from pageledger.runner import run

    config_path = tmp_path / f"{out_name}-config.yml"
    config_path.write_text(config_text, encoding="utf-8")
    out_dir = tmp_path / out_name
    run(inputs=inputs, config_path=config_path, out_dir=out_dir, dry_run=False)
    return out_dir


def test_compare_identical_runs(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("first page of clean text\fsecond page of clean text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    report = compare_runs(out_a, out_b)
    assert report["pages_compared"] == 2
    assert report["pages_only_in_a"] == []
    assert report["pages_only_in_b"] == []
    assert report["warnings_resolved_total"] == 0
    assert report["warnings_introduced_total"] == 0
    for page in report["pages"]:
        assert page["character_delta"] == 0


def test_compare_detects_resolved_warning(tmp_path):
    """A page whose warning disappears in run B counts as resolved."""
    from pageledger.compare import compare_runs

    source_a = tmp_path / "doc.txt"
    source_a.write_text("short\fsecond page of clean text here\n", encoding="utf-8")
    out_a = _run([source_a], tmp_path, "a")

    # Same page ids, but page 1 now has enough text (simulates stronger engine)
    source_a.write_text(
        "page one now has plenty of recovered text\fsecond page of clean text here\n",
        encoding="utf-8",
    )
    out_b = _run([source_a], tmp_path, "b")

    report = compare_runs(out_a, out_b)
    assert report["warning_pages_a"] == 1
    assert report["warning_pages_b"] == 0
    assert report["warnings_resolved_total"] >= 1
    assert report["warnings_introduced_total"] == 0
    page1 = next(p for p in report["pages"] if p["page_id"] == "doc_0001_page_0001")
    assert "short_text" in page1["warnings_resolved"]
    assert page1["character_delta"] > 0


def test_compare_parent_and_rerun_line_up(tmp_path):
    """Rerun keeps parent page ids, so compare-runs matches them directly."""
    from pageledger.compare import compare_runs
    from pageledger.runner import rerun

    source = tmp_path / "doc.txt"
    source.write_text("short\fsecond page of clean text here\n", encoding="utf-8")
    out_parent = _run([source], tmp_path, "parent")

    config_path = tmp_path / "rerun-config.yml"
    config_path.write_text(MINIMAL, encoding="utf-8")
    out_child = tmp_path / "child"
    rerun(parent_dir=out_parent, config_path=config_path, out_dir=out_child)

    report = compare_runs(out_parent, out_child)
    # Parent has 2 pages, child re-extracted only the flagged one.
    assert report["pages_compared"] == 1
    assert report["pages"][0]["page_id"] == "doc_0001_page_0001"
    assert len(report["pages_only_in_a"]) == 1
    assert report["run_b"]["parent_run_id"] == report["run_a"]["run_id"]


def test_compare_errors_on_non_run_directory(tmp_path):
    import pytest

    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("some clean page text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    empty = tmp_path / "not-a-run"
    empty.mkdir()
    with pytest.raises(ValueError, match="manifest.json"):
        compare_runs(out_a, empty)


def test_compare_cli_text_and_json(tmp_path, capsys):
    from pageledger.cli import main

    source = tmp_path / "doc.txt"
    source.write_text("short\fsecond page of clean text here\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    exit_code = main(["compare-runs", str(out_a), str(out_b)])
    assert exit_code == 0
    text = capsys.readouterr().out
    assert "Pages compared: 2" in text
    assert "Run A:" in text

    exit_code = main(["compare-runs", str(out_a), str(out_b), "--json"])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pages_compared"] == 2
    assert {"run_a", "run_b", "pages"} <= set(report.keys())


def test_compare_cli_error_exit_code(tmp_path, capsys):
    from pageledger.cli import main

    missing = tmp_path / "missing"
    exit_code = main(["compare-runs", str(missing), str(missing), "--json"])
    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
