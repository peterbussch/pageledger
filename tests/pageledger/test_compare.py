"""Tests for `pageledger compare-runs` and the compare module.

Verifies:
  - Page matching on page_id across two runs
  - Warning resolved/introduced accounting
  - Rerun directories compare cleanly against their parent (page_id overlap)
  - Only-in-one-run pages are reported, not silently dropped
  - CLI text and JSON output, error on non-run directories
"""

from __future__ import annotations

import hashlib
import json

import pytest

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
    out_b = _run([source_a], tmp_path, "b")

    # Simulate a stronger extraction without changing the source identity.
    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0]["warnings"] = []
    quality[0]["character_count"] = 40
    quality_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in quality), encoding="utf-8"
    )

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


def test_compare_rejects_manifest_symlink_without_reading_target(tmp_path, monkeypatch):
    from pathlib import Path

    import pageledger.compare as compare_module

    source = tmp_path / "doc.txt"
    source.write_text("some clean page text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    manifest_path = out_b / "manifest.json"
    outside = tmp_path / "outside-manifest.json"
    manifest_path.replace(outside)
    manifest_path.symlink_to(outside)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("comparison read an out-of-run manifest")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    with pytest.raises(ValueError, match="manifest.json.*contained regular file"):
        compare_module.compare_runs(out_a, out_b)


@pytest.mark.parametrize("artifact", ["quality.jsonl", "provenance.jsonl", "cost.json"])
def test_compare_rejects_symlinked_core_evidence(tmp_path, artifact):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("some clean page text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    artifact_path = out_b / artifact
    outside = tmp_path / f"outside-{artifact}"
    artifact_path.replace(outside)
    artifact_path.symlink_to(outside)

    with pytest.raises(ValueError, match=f"{artifact}.*contained regular file"):
        compare_runs(out_a, out_b)


def test_compare_handles_run_directory_symlink_loop(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("some clean page text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    loop = tmp_path / "loop"
    loop.symlink_to("loop")

    with pytest.raises(ValueError, match="run directory cannot be resolved safely"):
        compare_runs(out_a, loop)


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


# =========================================================================
# Grade comparison (0.1.3)
# =========================================================================


def test_compare_reports_grade_changes(tmp_path):
    """A page whose grade improves in run B is counted and rendered with basis labels."""
    from pageledger.compare import compare_runs, render_comparison

    source = tmp_path / "doc.txt"
    # Page 1 empty (grade F), page 2 clean (grade A)
    source.write_text("\fsecond page of clean text here\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")

    out_b = _run([source], tmp_path, "b")
    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0]["grade"] = "A"
    quality[0]["warnings"] = []
    quality_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in quality), encoding="utf-8"
    )

    report = compare_runs(out_a, out_b)
    assert report["grades_improved_total"] == 1
    assert report["grades_regressed_total"] == 0
    page1 = next(p for p in report["pages"] if p["page_id"] == "doc_0001_page_0001")
    assert page1["grade_a"] == "F"
    assert page1["grade_b"] == "A"
    assert page1["grade_basis_a"] == "signals_only"

    rendered = render_comparison(report)
    assert "Grades (matching basis/schema only): improved 1 / regressed 0" in rendered
    assert "F (signals)→A (signals)" in rendered


def test_compare_tolerates_ungraded_runs(tmp_path):
    """Pre-0.1.3 runs without grades compare cleanly: null grades, zero totals."""
    import json as json_module

    from pageledger.compare import compare_runs, render_comparison

    source = tmp_path / "doc.txt"
    source.write_text("a page of clean text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    # Simulate an old run by stripping grade fields from run A
    quality_path = out_a / "quality.jsonl"
    entries = [json_module.loads(line) for line in quality_path.read_text().splitlines()]
    for entry in entries:
        for key in ("grade", "grade_basis", "grade_detail"):
            entry.pop(key, None)
    quality_path.write_text(
        "".join(json_module.dumps(e, sort_keys=True) + "\n" for e in entries),
        encoding="utf-8",
    )

    report = compare_runs(out_a, out_b)
    assert report["grades_improved_total"] == 0
    assert report["grades_regressed_total"] == 0
    page = report["pages"][0]
    assert page["grade_a"] is None
    assert page["grade_b"] == "A"
    render_comparison(report)  # must not raise


def test_compare_source_identity_ignores_copied_path(tmp_path):
    """The same bytes and source page remain comparable after a file move."""
    from pageledger.compare import compare_runs

    source_a = tmp_path / "original.txt"
    source_b = tmp_path / "copied.txt"
    content = "one clean page of copied text\n"
    source_a.write_text(content, encoding="utf-8")
    source_b.write_text(content, encoding="utf-8")
    out_a = _run([source_a], tmp_path, "a")
    out_b = _run([source_b], tmp_path, "b")

    report = compare_runs(out_a, out_b)

    assert report["pages_comparable_total"] == 1
    assert report["pages_incomparable_total"] == 0
    assert report["pages"][0]["source_status"] == "same"
    assert report["pages"][0]["comparability"] == "comparable"


def test_compare_page_id_collision_is_visible_but_not_ranked(tmp_path):
    """A reused page_id cannot make a changed source look improved."""
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    source.write_text("a replacement source with much more recovered text\n", encoding="utf-8")
    out_b = _run([source], tmp_path, "b")

    report = compare_runs(out_a, out_b)

    assert report["pages_compared"] == 1
    assert report["pages_comparable_total"] == 0
    assert report["pages_incomparable_total"] == 1
    assert report["page_identity_mismatches"] == ["doc_0001_page_0001"]
    page = report["pages"][0]
    assert page["source_status"] == "changed"
    assert page["comparability"] == "incomparable_source"
    assert "short_text" in page["warnings_resolved"]
    assert report["warnings_resolved_total"] == 0
    assert report["grades_improved_total"] == 0


def test_compare_unrelated_sources_with_same_page_id_are_incomparable(tmp_path):
    from pageledger.compare import compare_runs

    source_a = tmp_path / "first.txt"
    source_b = tmp_path / "unrelated.txt"
    source_a.write_text("short\n", encoding="utf-8")
    source_b.write_text("an unrelated source with a reused generated page id\n", encoding="utf-8")
    out_a = _run([source_a], tmp_path, "a")
    out_b = _run([source_b], tmp_path, "b")

    report = compare_runs(out_a, out_b)

    assert report["page_identity_mismatches"] == ["doc_0001_page_0001"]
    assert report["pages"][0]["source_status"] == "different"
    assert report["pages"][0]["comparability"] == "incomparable_source"
    assert report["pages_comparable_total"] == 0


def test_compare_cross_adapter_changes_are_unranked(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0].update(adapter="other", warnings=[], grade="A")
    quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["adapter_a"] == "text"
    assert page["adapter_b"] == "other"
    assert page["comparability"] == "incomparable_adapter"
    assert report["pages_comparable_total"] == 0
    assert report["warnings_resolved_total"] == 0
    assert report["grades_improved_total"] == 0


def test_compare_same_adapter_different_model_is_unranked(tmp_path):
    """A shared adapter name must not make different model pipelines rankable."""
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    provenance_path = out_b / "provenance.jsonl"
    provenance = [
        json.loads(line) for line in provenance_path.read_text().splitlines()
    ]
    provenance[0]["extractor"]["model"] = "alternate-model"
    provenance_path.write_text(json.dumps(provenance[0]) + "\n", encoding="utf-8")
    manifest_path = out_b / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extractors"][0]["model"] = "alternate-model"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0].update(warnings=[], grade="A")
    quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["adapter_a"] == page["adapter_b"] == "text"
    assert page["effective_extractor_a"]["model"] is None
    assert page["effective_extractor_b"]["model"] == "alternate-model"
    assert page["comparability"] == "incomparable_extractor"
    assert report["pages_comparable_total"] == 0
    assert report["warnings_resolved_total"] == 0
    assert report["grades_improved_total"] == 0


def test_compare_same_adapter_different_options_is_unranked(tmp_path):
    """Output-affecting adapter options are part of extractor identity."""
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    for out_dir, mode in ((out_a, "mode-a"), (out_b, "mode-b")):
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["extractors"][0]["options"] = {"mode": mode}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert len(page["effective_extractor_a"]["options_sha256"]) == 64
    assert len(page["effective_extractor_b"]["options_sha256"]) == 64
    assert (
        page["effective_extractor_a"]["options_sha256"]
        != page["effective_extractor_b"]["options_sha256"]
    )
    assert page["comparability"] == "incomparable_extractor"
    assert report["pages_comparable_total"] == 0


def test_compare_does_not_rank_grades_across_evidence_bases(tmp_path):
    from pageledger.compare import compare_runs, render_comparison

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0].update(grade="A", grade_basis="schema_aware")
    quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["comparability"] == "comparable"
    assert page["grade_comparability"] == "incomparable_basis"
    assert report["grades_improved_total"] == 0
    assert report["grades_regressed_total"] == 0
    assert "| comparable | incomparable_basis |" in render_comparison(report)


def test_compare_does_not_rank_schema_grades_from_different_schemas(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    for out_dir, grade, schema_hash in (
        (out_a, "B", "name: schema-a\ncolumns: []\n"),
        (out_b, "A", "name: schema-b\ncolumns: []\n"),
    ):
        quality_path = out_dir / "quality.jsonl"
        quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
        quality[0].update(grade=grade, grade_basis="schema_aware")
        quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")
        schema_snapshot = out_dir / "align-schema-snapshot.yml"
        schema_snapshot.write_text(schema_hash, encoding="utf-8")
        recorded_hash = hashlib.sha256(schema_hash.encode("utf-8")).hexdigest()
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["alignment"] = {
            "aligned_at": "2026-08-16T00:00:00Z",
            "schema_source": "test",
            "schema_sha256": recorded_hash,
            "pageledger_version": "0.3.0a1",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_comparability"] == "incomparable_schema"
    assert page["grade_schema_identity_a"] != page["grade_schema_identity_b"]
    assert report["grades_improved_total"] == 0


def test_compare_does_not_follow_external_alignment_schema_symlink(
    tmp_path, monkeypatch
):
    import pageledger.compare as compare_module

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    schema_text = "name: demo\ncolumns: []\n"
    outside = tmp_path / "outside-schema.yml"
    outside.write_text(schema_text, encoding="utf-8")
    schema_hash = hashlib.sha256(schema_text.encode("utf-8")).hexdigest()
    for out_dir, grade in ((out_a, "B"), (out_b, "A")):
        quality_path = out_dir / "quality.jsonl"
        quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
        quality[0].update(grade=grade, grade_basis="schema_aware")
        quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["alignment"] = {
            "aligned_at": "2026-08-16T00:00:00Z",
            "schema_source": "/external/schema.yml",
            "schema_sha256": schema_hash,
            "pageledger_version": "0.3.0a1",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (out_a / "align-schema-snapshot.yml").write_text(schema_text, encoding="utf-8")
    (out_b / "align-schema-snapshot.yml").symlink_to(outside)
    original_hash_matches = compare_module._file_hash_matches

    def guarded_hash_matches(path, expected):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("comparison read an out-of-run schema snapshot")
        return original_hash_matches(path, expected)

    monkeypatch.setattr(compare_module, "_file_hash_matches", guarded_hash_matches)

    report = compare_module.compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_schema_identity_a"] is not None
    assert page["grade_schema_identity_b"] is None
    assert page["grade_comparability"] == "incomparable_unknown"


def test_compare_handles_external_alignment_schema_symlink_loop(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    for out_dir, grade in ((out_a, "B"), (out_b, "A")):
        quality_path = out_dir / "quality.jsonl"
        quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
        quality[0].update(grade=grade, grade_basis="schema_aware")
        quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")
    schema_text = "name: demo\ncolumns: []\n"
    (out_a / "align-schema-snapshot.yml").write_text(schema_text, encoding="utf-8")
    (out_b / "align-schema-snapshot.yml").symlink_to("align-schema-snapshot.yml")
    for out_dir in (out_a, out_b):
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["alignment"] = {
            "aligned_at": "2026-08-16T00:00:00Z",
            "schema_source": "/external/schema.yml",
            "schema_sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
            "pageledger_version": "0.3.0a1",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_schema_identity_b"] is None
    assert page["grade_comparability"] == "incomparable_unknown"


def test_compare_recognizes_same_config_schema_after_alignment(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    schema_config = MINIMAL.replace(
        "run:\n",
        "schema:\n  name: demo\n  columns:\n    - {name: value, type: string}\nrun:\n",
    )
    out_a = _run([source], tmp_path, "a", config_text=schema_config)
    out_b = _run([source], tmp_path, "b", config_text=schema_config)
    for out_dir, grade in ((out_a, "B"), (out_b, "A")):
        quality_path = out_dir / "quality.jsonl"
        quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
        quality[0].update(grade=grade, grade_basis="schema_aware")
        quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")
    manifest_path = out_b / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config_bytes = (out_b / "config-snapshot.yml").read_bytes()
    manifest["alignment"] = {
        "aligned_at": "2026-08-16T00:00:00Z",
        "schema_source": "config_snapshot",
        "schema_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "pageledger_version": "0.3.0a1",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_comparability"] == "comparable"
    assert page["grade_schema_identity_a"] == page["grade_schema_identity_b"]
    assert report["grades_improved_total"] == 1


def test_compare_does_not_rank_grades_from_different_pageledger_versions(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    manifest_path = out_b / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pageledger_version"] = "99.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0]["grade"] = "A"
    quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    assert report["pages"][0]["grade_comparability"] == "incomparable_generator"
    assert report["grades_improved_total"] == 0


def test_compare_does_not_rank_grades_with_different_grading_config(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    config_a = MINIMAL + "  grading:\n    thresholds:\n      confidence: {A: 0.90}\n"
    config_b = MINIMAL + "  grading:\n    thresholds:\n      confidence: {A: 0.95}\n"
    out_a = _run([source], tmp_path, "a", config_text=config_a)
    out_b = _run([source], tmp_path, "b", config_text=config_b)
    quality_path = out_b / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0]["grade"] = "A"
    quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_comparability"] == "incomparable_grade_config"
    assert page["grade_config_identity_a"] != page["grade_config_identity_b"]
    assert report["grades_improved_total"] == 0


def test_compare_does_not_rank_grades_with_different_schema_quality_floor(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    schema_prefix = (
        "schema:\n"
        "  name: demo\n"
        "  columns:\n"
        "    - {name: value, type: string}\n"
        "  quality:\n"
        "    low_confidence_threshold: FLOOR\n"
    )
    config_a = MINIMAL.replace("run:\n", schema_prefix.replace("FLOOR", "0.80") + "run:\n")
    config_b = MINIMAL.replace("run:\n", schema_prefix.replace("FLOOR", "0.90") + "run:\n")
    out_a = _run([source], tmp_path, "a", config_text=config_a)
    out_b = _run([source], tmp_path, "b", config_text=config_b)
    for out_dir, grade in ((out_a, "B"), (out_b, "C")):
        quality_path = out_dir / "quality.jsonl"
        quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
        quality[0]["grade"] = grade
        quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_comparability"] == "incomparable_grade_config"
    assert page["grade_config_identity_a"] != page["grade_config_identity_b"]
    assert report["grades_regressed_total"] == 0


def test_compare_uses_external_schema_floor_for_signals_only_grades(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("short\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    for out_dir, grade, floor in ((out_a, "B", 0.88), (out_b, "C", 0.90)):
        quality_path = out_dir / "quality.jsonl"
        quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
        quality[0].update(grade=grade, grade_basis="signals_only")
        quality_path.write_text(json.dumps(quality[0]) + "\n", encoding="utf-8")
        schema_text = (
            "name: external\n"
            "columns:\n"
            "  - {name: value, type: string}\n"
            "quality:\n"
            f"  low_confidence_threshold: {floor}\n"
        )
        (out_dir / "align-schema-snapshot.yml").write_text(schema_text, encoding="utf-8")
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["alignment"] = {
            "aligned_at": "2026-08-16T00:00:00Z",
            "schema_source": "/external/schema.yml",
            "schema_sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
            "pageledger_version": "0.3.0a1",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["grade_comparability"] == "incomparable_grade_config"
    assert page["grade_config_identity_a"] != page["grade_config_identity_b"]
    assert report["grades_regressed_total"] == 0


def test_compare_missing_extractor_identity_evidence_is_unknown(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("one clean page of text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")

    provenance_path = out_a / "provenance.jsonl"
    provenance = [
        json.loads(line) for line in provenance_path.read_text().splitlines()
    ]
    del provenance[0]["extractor"]["model"]
    provenance_path.write_text(json.dumps(provenance[0]) + "\n", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    page = report["pages"][0]
    assert page["source_status"] == "same"
    assert page["effective_extractor_a"] is None
    assert page["comparability"] == "incomparable_unknown"
    assert report["pages_incomparable_total"] == 1


def test_compare_legacy_missing_provenance_is_unknown(tmp_path):
    from pageledger.compare import compare_runs

    source = tmp_path / "doc.txt"
    source.write_text("one clean page of text\n", encoding="utf-8")
    out_a = _run([source], tmp_path, "a")
    out_b = _run([source], tmp_path, "b")
    (out_a / "provenance.jsonl").write_text("", encoding="utf-8")

    report = compare_runs(out_a, out_b)

    assert report["pages"][0]["source_status"] == "unknown"
    assert report["pages"][0]["comparability"] == "incomparable_unknown"
    assert report["pages_incomparable_total"] == 1
