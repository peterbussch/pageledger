"""Jobs in report format 0.9 render and verify byte for byte.

The fixtures pin the current wording of report.md and transcript.md: a change to
it shows here first. Regenerate them only for a deliberate change, which needs a
new report format once 0.9 has been released.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pageledger.document_report import render_document_report, render_transcript

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "report-0.9"
RENDERED = ("letters-lost", "escalated", "paused", "text-corrected")


@pytest.mark.parametrize("name", RENDERED)
def test_a_format_0_9_report_renders_as_it_was_written(name):
    folder = GOLDEN / name
    report = json.loads((folder / "document.json").read_bytes())
    assert report["report_format"] == "0.9"
    assert render_document_report(report).encode("utf-8") == (folder / "report.md").read_bytes()
    assert render_transcript(report).encode("utf-8") == (folder / "transcript.md").read_bytes()
