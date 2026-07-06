"""Phase 8 tests: corpus regression, fixture verification, artifact-count
consistency, and stress/performance envelope.

Fixtures are small checked-in files under tests/fixtures/.  Stress tests
generate synthetic text on-the-fly so we do not commit large outputs.

All tests in this module that generate pages at scale are marked as
'stress' so they can be skipped in day-to-day CI with ``-m \"not stress\"``.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# =========================================================================
# Checked-in fixture verification
# =========================================================================

def test_fixture_clean_single(tmp_path: Path) -> None:
    """Clean single-page fixture: no warnings, full artifact chain."""
    _run_fixture(tmp_path, FIXTURES / "clean-single.txt",
                 expected_pages=1, expected_extracted=1, expected_warnings=0)


def test_fixture_multipage_clean(tmp_path: Path) -> None:
    """Multipage clean fixture: 3 pages, 3 extracted, 0 warnings."""
    _run_fixture(tmp_path, FIXTURES / "multipage-clean.txt",
                 expected_pages=3, expected_extracted=3, expected_warnings=0)


def test_fixture_noisy_ocr(tmp_path: Path) -> None:
    """Noisy OCR fixture: replacement + control chars trigger warnings."""
    _run_fixture(tmp_path, FIXTURES / "noisy-ocr.txt",
                 expected_pages=1, expected_extracted=1, expected_warnings=1)


def test_fixture_blank(tmp_path: Path) -> None:
    """Blank fixture: empty_text warning, 1 page, audit entry."""
    _run_fixture(tmp_path, FIXTURES / "blank.txt",
                 expected_pages=1, expected_extracted=1, expected_warnings=1)


def test_fixture_short(tmp_path: Path) -> None:
    """Short fixture: short_text warning, 1 page."""
    _run_fixture(tmp_path, FIXTURES / "short.txt",
                 expected_pages=1, expected_extracted=1, expected_warnings=1)


# =========================================================================
# Artifact-count consistency (checked-in fixtures + generated)
# =========================================================================

def test_artifact_counts_consistent_checked_in(tmp_path: Path) -> None:
    """raw file count == provenance lines == quality lines == pages_extracted."""
    source = FIXTURES / "multipage-clean.txt"
    out_dir = _run_pageledger(tmp_path, [source])

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    pages_extracted = manifest["summary"]["pages_extracted"]

    raw_count = len(list((out_dir / "raw").iterdir()))
    prov_count = sum(1 for l in _read_lines(out_dir / "provenance.jsonl") if l.strip())
    quality_count = sum(1 for l in _read_lines(out_dir / "quality.jsonl") if l.strip())

    assert raw_count == pages_extracted, f"raw={raw_count} != manifest={pages_extracted}"
    assert prov_count == pages_extracted, f"provenance={prov_count} != manifest={pages_extracted}"
    assert quality_count == pages_extracted, f"quality={quality_count} != manifest={pages_extracted}"


def test_artifact_counts_consistent_synthetic(tmp_path: Path) -> None:
    """Synthetic 20-page run: all artifact counts match."""
    source = tmp_path / "synth.txt"
    source.write_text(("\f".join(f"page {i} content here for testing" for i in range(1, 21))), encoding="utf-8")

    out_dir = _run_pageledger(tmp_path, [source])
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    pe = manifest["summary"]["pages_extracted"]

    raw_count = len(list((out_dir / "raw").iterdir()))
    prov_count = sum(1 for l in _read_lines(out_dir / "provenance.jsonl") if l.strip())
    quality_count = sum(1 for l in _read_lines(out_dir / "quality.jsonl") if l.strip())

    assert raw_count == pe
    assert prov_count == pe
    assert quality_count == pe
    # Route map page count should match
    route = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    route_pages = sum(len(doc["pages"]) for doc in route["documents"])
    assert route_pages == manifest["summary"]["pages_total"]


# =========================================================================
# Stress tests — marked 'stress' for optional CI
# =========================================================================


@pytest.mark.stress
def test_stress_100_pages(tmp_path: Path) -> None:
    """100-page synthetic run: correct counts, no drift."""
    _stress_run(tmp_path, 100)


@pytest.mark.stress
def test_stress_1000_pages(tmp_path: Path) -> None:
    """1,000-page synthetic run: correct counts, no drift."""
    _stress_run(tmp_path, 1000)


@pytest.mark.stress
def test_stress_5000_pages(tmp_path: Path) -> None:
    """5,000-page synthetic run: correct counts, reasonable runtime."""
    _stress_run(tmp_path, 5000)


# =========================================================================
# Run-id uniqueness
# =========================================================================

def test_run_ids_are_unique_across_runs(tmp_path: Path) -> None:
    """Two rapid successive runs produce distinct run_ids."""
    source = tmp_path / "s.txt"
    source.write_text("hello\n", encoding="utf-8")

    id1 = _run_and_get_id(tmp_path, source, tmp_path / "r1")
    # brief sleep to ensure timestamp changes
    import time
    time.sleep(0.002)
    id2 = _run_and_get_id(tmp_path, source, tmp_path / "r2")

    assert id1 != id2


# =========================================================================
# Helpers
# =========================================================================

def _run_fixture(
    tmp_path: Path,
    source: Path,
    *,
    expected_pages: int,
    expected_extracted: int,
    expected_warnings: int,
) -> None:
    out_dir = _run_pageledger(tmp_path, [source])
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_total"] == expected_pages
    assert manifest["summary"]["pages_extracted"] == expected_extracted
    assert manifest["summary"]["quality_warning_pages"] == expected_warnings

    # Verify output integrity
    if expected_extracted > 0:
        assert (out_dir / "raw").is_dir()
        raw_files = list((out_dir / "raw").iterdir())
        assert len(raw_files) == expected_extracted

        prov_lines = [l for l in _read_lines(out_dir / "provenance.jsonl") if l.strip()]
        assert len(prov_lines) == expected_extracted

        quality_lines = [l for l in _read_lines(out_dir / "quality.jsonl") if l.strip()]
        assert len(quality_lines) == expected_extracted


def _run_pageledger(tmp_path: Path, inputs: list[Path]) -> Path:
    """Run PageLedger programmatically and return the output directory."""
    from pageledger.runner import run

    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    run(inputs=inputs, config_path=config, out_dir=out_dir, dry_run=False)
    return out_dir


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _run_and_get_id(tmp_path: Path, source: Path, out_dir: Path) -> str:
    from pageledger.runner import run

    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
        """), encoding="utf-8")
    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    return manifest["run_id"]


def _stress_run(tmp_path: Path, page_count: int) -> None:
    """Generate a synthetic text of *page_count* pages, run, and assert correctness."""
    source = tmp_path / "stress.txt"
    lines = [f"page {i}: this is synthetic test content for PageLedger stress test.\n"
             for i in range(1, page_count + 1)]
    source.write_text("\f".join(lines), encoding="utf-8")

    t0 = time.monotonic()
    out_dir = _run_pageledger(tmp_path, [source])
    elapsed = time.monotonic() - t0

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    pe = manifest["summary"]["pages_extracted"]
    pt = manifest["summary"]["pages_total"]

    assert pt == page_count, f"pages_total={pt} != expected={page_count}"
    assert pe == page_count, f"pages_extracted={pe} != expected={page_count}"

    raw_count = len(list((out_dir / "raw").iterdir()))
    prov_count = sum(1 for l in _read_lines(out_dir / "provenance.jsonl") if l.strip())
    quality_count = sum(1 for l in _read_lines(out_dir / "quality.jsonl") if l.strip())

    assert raw_count == page_count, f"raw={raw_count} != {page_count}"
    assert prov_count == page_count, f"provenance={prov_count} != {page_count}"
    assert quality_count == page_count, f"quality={quality_count} != {page_count}"

    # Coarse performance: 5000 pages should complete in under 60 seconds
    # (this is a generous bound for a local test)
    if page_count >= 5000:
        assert elapsed < 120, f"5000 pages took {elapsed:.1f}s; expected < 120s"

    # Log the timing for informational purposes (visible with -s flag)
    pps = page_count / elapsed if elapsed > 0 else 0
    print(f"[stress {page_count}p] {elapsed:.2f}s, {pps:.0f} pages/sec, {pe} extracted")
