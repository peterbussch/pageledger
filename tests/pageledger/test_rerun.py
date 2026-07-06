"""Rerun manifest semantics and rerun execution.

Verifies:
  - rerun_status: executable / empty_queue / no_further_generations
  - rerun_executable true only when items exist and depth allows another generation
  - max_rerun_depth=0 produces empty items with no_further_generations status
  - max_rerun_depth>0 produces items from review_queue
  - Dry-run manifests: reason=dry_run, items from route-based review entries
  - Execute manifests: reason=audit_policy, items from quality_warning + configured_review
  - parent_run_id references the generating run correctly
  - `rerun` executes only the listed pages, preserves page ids, records lineage,
    enforces the depth cap, and warns on changed sources
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import yaml


def _run(inputs, config_text, tmp_path, *, dry_run=False, log_level="INFO"):
    from pageledger.runner import run

    config_path = tmp_path / "config.yml"
    config_path.write_text(config_text, encoding="utf-8")
    out_dir = tmp_path / "out"
    run(
        inputs=inputs,
        config_path=config_path,
        out_dir=out_dir,
        dry_run=dry_run,
        log_level=log_level,
    )
    return out_dir


def _load_rerun(out_dir: Path) -> dict:
    return yaml.safe_load((out_dir / "rerun-manifest.yml").read_text(encoding="utf-8"))


MINIMAL = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""

CONFIG_DEPTH_0 = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
  max_rerun_depth: 0
"""

CONFIG_REVIEW = """\
schema_version: "0.1"
taxonomy:
  page_types:
    figure:
      default_action: review
run:
  adapter: text
"""


# =========================================================================
# Rerun status semantics
# =========================================================================

def test_rerun_manifest_empty_queue_not_executable(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["rerun_executable"] is False
    assert rerun["rerun_status"] == "empty_queue"


def test_rerun_manifest_dry_run_is_executable(tmp_path):
    """Dry-run review items are rerun candidates, so the manifest is executable."""
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path, dry_run=True)
    rerun = _load_rerun(out_dir)
    assert rerun["rerun_executable"] is True
    assert rerun["rerun_status"] == "executable"


# =========================================================================
# max_rerun_depth guard
# =========================================================================

def test_max_rerun_depth_zero_produces_empty_items(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("short", encoding="utf-8")  # will get short_text warning
    out_dir = _run([source], CONFIG_DEPTH_0, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["items"] == []
    assert rerun["rerun_status"] == "no_further_generations"
    assert rerun["max_rerun_depth"] == 0


def test_max_rerun_depth_zero_still_preserves_metadata(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], CONFIG_DEPTH_0, tmp_path)
    rerun = _load_rerun(out_dir)
    # Metadata fields still present
    assert rerun["schema_version"] == "0.1"
    assert rerun["run_id"].endswith("-rerun")
    assert rerun["parent_run_id"] is not None
    assert rerun["parent_manifest"] == "manifest.json"
    assert rerun["reason"] == "audit_policy"


def test_max_rerun_depth_positive_produces_items(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("short", encoding="utf-8")  # short_text → quality_warning → review
    out_dir = _run([source], MINIMAL, tmp_path)  # default max_rerun_depth=2
    rerun = _load_rerun(out_dir)
    assert len(rerun["items"]) >= 1
    assert rerun["rerun_status"] == "executable"
    assert rerun["rerun_executable"] is True


# =========================================================================
# Dry-run review queue semantics
# =========================================================================

def test_dry_run_rerun_reason_is_dry_run(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path, dry_run=True)
    rerun = _load_rerun(out_dir)
    assert rerun["reason"] == "dry_run"


def test_dry_run_rerun_items_are_route_based(tmp_path):
    """Dry-run rerun items come from route-based review entries (no_classifier_available)."""
    source = tmp_path / "doc.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path, dry_run=True)
    rerun = _load_rerun(out_dir)
    # Every page should be in review queue (no classifier)
    assert len(rerun["items"]) == 2
    for item in rerun["items"]:
        assert item["reason"] == "no_classifier_available"
        assert item["action"] == "review"


def test_dry_run_configured_review_appears_in_rerun(tmp_path):
    """Pages with configured default_action=review appear in rerun items."""
    source = tmp_path / "doc.txt"
    source.write_text("hello\n", encoding="utf-8")
    out_dir = _run([source], CONFIG_REVIEW, tmp_path, dry_run=True)
    rerun = _load_rerun(out_dir)
    reasons = {item["reason"] for item in rerun["items"]}
    assert "no_classifier_available" in reasons


# =========================================================================
# Execute review queue semantics
# =========================================================================

def test_execute_rerun_reason_is_audit_policy(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["reason"] == "audit_policy"


def test_execute_quality_warning_appears_in_rerun(tmp_path):
    """Quality-warning pages appear in execute-mode rerun items."""
    source = tmp_path / "doc.txt"
    source.write_text("short", encoding="utf-8")  # triggers short_text
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    reasons = {item["reason"] for item in rerun["items"]}
    assert "quality_warning" in reasons


def test_execute_configured_review_appears_in_rerun(tmp_path):
    """Pages with configured default_action=review appear in execute rerun items."""
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], CONFIG_REVIEW, tmp_path)
    rerun = _load_rerun(out_dir)
    assert len(rerun["items"]) >= 1
    assert rerun["items"][0]["reason"] in ("configured_review", "quality_warning")


def test_execute_clean_text_no_review_empty_rerun(tmp_path):
    """Clean text with no warnings and no configured review produces empty rerun."""
    source = tmp_path / "doc.txt"
    source.write_text("clean page with enough text to avoid warnings\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["items"] == []


# =========================================================================
# Parent linkage
# =========================================================================

def test_rerun_parent_run_id_matches_manifest(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert rerun["parent_run_id"] == manifest["run_id"]


def test_rerun_parent_manifest_is_manifest_json(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["parent_manifest"] == "manifest.json"


def test_rerun_run_id_differs_from_parent(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    # Rerun run_id is the proposed rerun id, not the parent
    assert rerun["run_id"] != manifest["run_id"]
    assert rerun["run_id"].endswith("-rerun")


# =========================================================================
# Per-item field stability
# =========================================================================

def test_rerun_item_fields_are_stable(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path, dry_run=True)
    rerun = _load_rerun(out_dir)
    for item in rerun["items"]:
        required = {"page_id", "page_number", "source", "action", "reason", "previous_grade"}
        assert required <= set(item.keys())
        assert item["action"] == "review"
        assert item["previous_grade"] is None
        assert isinstance(item["page_number"], int)
        assert item["page_number"] >= 1


# =========================================================================
# max_rerun_depth config inheritance
# =========================================================================

def test_max_rerun_depth_default_is_2(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    out_dir = _run([source], MINIMAL, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["max_rerun_depth"] == 2


def test_max_rerun_depth_from_config(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hello world\n", encoding="utf-8")
    config = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
  max_rerun_depth: 7
"""
    out_dir = _run([source], config, tmp_path)
    rerun = _load_rerun(out_dir)
    assert rerun["max_rerun_depth"] == 7
    assert rerun["rerun_status"] == "empty_queue"


# =========================================================================
# Rerun execution
# =========================================================================

def _parent_with_flagged_page(tmp_path):
    """Parent run over two pages where page 1 gets a short_text warning."""
    source = tmp_path / "doc.txt"
    source.write_text(
        "short\fclean second page with plenty of ordinary text\n", encoding="utf-8"
    )
    out_dir = _run([source], MINIMAL, tmp_path)
    return source, out_dir


def _do_rerun(parent_dir, tmp_path, *, config_text=MINIMAL, dry_run=False):
    from pageledger.runner import rerun

    config_path = tmp_path / "rerun-config.yml"
    config_path.write_text(config_text, encoding="utf-8")
    rerun_out = tmp_path / "rerun-out"
    result = rerun(
        parent_dir=parent_dir,
        config_path=config_path,
        out_dir=rerun_out,
        dry_run=dry_run,
    )
    return result, rerun_out


def test_rerun_executes_only_flagged_pages(tmp_path):
    source, parent_out = _parent_with_flagged_page(tmp_path)
    result, rerun_out = _do_rerun(parent_out, tmp_path)
    assert result["summary"]["pages_total"] == 1
    assert result["summary"]["pages_extracted"] == 1
    raw = list((rerun_out / "raw").iterdir())
    assert [p.name for p in raw] == ["doc_0001_page_0001.txt"]


def test_rerun_preserves_parent_page_ids(tmp_path):
    source, parent_out = _parent_with_flagged_page(tmp_path)
    result, rerun_out = _do_rerun(parent_out, tmp_path)
    provenance = [
        json.loads(line)
        for line in (rerun_out / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert provenance[0]["page_id"] == "doc_0001_page_0001"
    assert provenance[0]["source"]["page_number"] == 1


def test_rerun_records_parent_lineage(tmp_path):
    source, parent_out = _parent_with_flagged_page(tmp_path)
    parent_manifest = json.loads((parent_out / "manifest.json").read_text(encoding="utf-8"))
    result, rerun_out = _do_rerun(parent_out, tmp_path)
    child_manifest = json.loads((rerun_out / "manifest.json").read_text(encoding="utf-8"))
    assert child_manifest["parent_run_id"] == parent_manifest["run_id"]
    assert result["parent_run_id"] == parent_manifest["run_id"]
    assert result["rerun_depth"] == 1
    child_rerun = _load_rerun(rerun_out)
    assert child_rerun["rerun_depth"] == 1


def test_rerun_refuses_empty_manifest(tmp_path):
    import pytest

    source = tmp_path / "doc.txt"
    source.write_text("clean page with enough text to avoid warnings\n", encoding="utf-8")
    parent_out = _run([source], MINIMAL, tmp_path)
    with pytest.raises(ValueError, match="no items"):
        _do_rerun(parent_out, tmp_path)


def test_rerun_enforces_depth_cap(tmp_path):
    import pytest

    config_depth_1 = MINIMAL + "  max_rerun_depth: 1\n"
    source = tmp_path / "doc.txt"
    source.write_text("short\fclean second page with plenty of ordinary text\n", encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(config_depth_1, encoding="utf-8")
    parent_out = tmp_path / "parent"
    from pageledger.runner import run as run_fn, rerun as rerun_fn

    run_fn(inputs=[source], config_path=config_path, out_dir=parent_out, dry_run=False)
    # Generation 1 is allowed; the child still has a short page, but its own
    # rerun manifest must refuse generation 2.
    child_out = tmp_path / "child"
    rerun_fn(parent_dir=parent_out, config_path=config_path, out_dir=child_out)
    child_rerun = _load_rerun(child_out)
    assert child_rerun["rerun_status"] == "no_further_generations"
    assert child_rerun["items"] == []
    grandchild_out = tmp_path / "grandchild"
    with pytest.raises(ValueError, match="[Mm]ax rerun depth"):
        rerun_fn(parent_dir=child_out, config_path=config_path, out_dir=grandchild_out)


def test_rerun_warns_on_changed_source(tmp_path):
    source, parent_out = _parent_with_flagged_page(tmp_path)
    source.write_text("short\fmodified second page content since the parent run\n", encoding="utf-8")
    result, rerun_out = _do_rerun(parent_out, tmp_path)
    warnings = result.get("source_integrity_warnings", [])
    assert len(warnings) == 1
    assert "sha256" in warnings[0]


def test_rerun_cli_roundtrip(tmp_path):
    from pageledger.cli import main

    source, parent_out = _parent_with_flagged_page(tmp_path)
    config_path = tmp_path / "rerun-config.yml"
    config_path.write_text(MINIMAL, encoding="utf-8")
    rerun_out = tmp_path / "cli-rerun-out"
    exit_code = main(
        [
            "rerun",
            str(parent_out),
            "--config",
            str(config_path),
            "--out",
            str(rerun_out),
            "--json",
        ]
    )
    assert exit_code == 0
    manifest = json.loads((rerun_out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parent_run_id"] is not None
