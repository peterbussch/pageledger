"""An adapter's layout travels with its reading: saved, hashed, and checked again."""

from __future__ import annotations

import json

import pytest

from pageledger import runner
from pageledger.adapters import ExtractionResult, TextAdapter
from pageledger.checkpoint import Checkpoint, read_record, write_record
from pageledger.layout import layout_sha256
from pageledger.verify import verify_run

LAYOUT = {
    "units": "page_millionths",
    "level": "line",
    "items": [{"box": [100, 200, 500_000, 260], "text": "page"}],
    "source": {},
}


class LayoutAdapter(TextAdapter):
    name = "layout-stub"
    capabilities = ("local", "layout")
    layout: dict | None = LAYOUT

    def extract(self, source, *, page_id, page_number, action, prompt):
        return ExtractionResult(
            content=f"page {page_number}",
            format="text",
            confidence=None,
            model=None,
            warnings=[],
            usage={"pages": 1},
            layout=self.layout if page_number != 2 else None,
        )


@pytest.fixture
def job(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("one\ftwo\fthree")
    config = tmp_path / "config.yml"
    config.write_text(
        "taxonomy:\n  page_types:\n    prose:\n      default_action: transcribe_text\n"
        "schema_version: '0.1'\nrun:\n  adapter: text\n"
    )
    adapter = LayoutAdapter()
    monkeypatch.setattr(runner, "load_adapter", lambda *args: adapter)
    return source, config, tmp_path / "run", adapter


def launch(job, **kwargs):
    source, config, out, _ = job
    return runner.run(
        inputs=[source], config_path=config, out_dir=out, dry_run=False, resumable=True, **kwargs
    )


def _page(out, number):
    return out / ".checkpoint" / "pages" / f"doc_0001_page_{number:04d}.json"


def test_a_layout_is_saved_with_its_reading_and_hashed_in_provenance(job):
    assert launch(job)["status"] == "completed"
    out = job[2]
    provenance = [json.loads(line) for line in (out / "provenance.jsonl").read_text().splitlines()]
    assert [entry.get("layout_sha256") for entry in provenance] == [
        layout_sha256(LAYOUT),
        None,
        layout_sha256(LAYOUT),
    ]
    assert read_record(_page(out, 1))["result"]["layout"] == LAYOUT
    # A reading without a layout is saved as 0.8 saved it, without the key.
    assert "layout" not in read_record(_page(out, 2))["result"]
    assert verify_run(out)["status"] == "pass"
    Checkpoint(out, existing=True)


def test_a_layout_that_no_longer_matches_its_provenance_hash_is_refused(job):
    launch(job)
    path = _page(job[2], 1)
    record = read_record(path)
    record["result"]["layout"]["items"][0]["text"] = "edited"
    write_record(path, record)
    with pytest.raises(ValueError, match="no longer matches its provenance hash"):
        Checkpoint(job[2], existing=True)


def test_a_saved_response_with_a_layout_completes_on_resume(job):
    def interrupt(phase, _duration):
        if phase == "usage_budget_provenance":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    assert read_record(_page(job[2], 1))["state"] == "response"
    assert runner.resume(job[2])["status"] == "completed"
    provenance = json.loads((job[2] / "provenance.jsonl").read_text().splitlines()[0])
    assert provenance["layout_sha256"] == layout_sha256(LAYOUT)
    assert verify_run(job[2])["status"] == "pass"


def test_an_invalid_layout_fails_the_page(job):
    job[3].layout = {**LAYOUT, "units": "pixels"}
    with pytest.raises(RuntimeError):
        launch(job)
    record = read_record(_page(job[2], 1))
    assert record["state"] == "failed"


@pytest.mark.parametrize(
    "missing",
    [set(), {"layout"}, {"input_evidence"}, {"layout", "input_evidence"}],
    ids=["both", "no-layout", "no-evidence", "neither"],
)
def test_saved_results_with_and_without_the_optional_fields_load(job, missing):
    launch(job)
    checkpoint = Checkpoint(job[2], existing=True)
    data = {
        "content": "page 1",
        "format": "text",
        "confidence": None,
        "model": None,
        "warnings": [],
        "usage": {"pages": 1},
        "confidence_detail": None,
        "input_evidence": None,
        "layout": LAYOUT,
    }
    for key in missing:
        del data[key]
    page = {"page_number": 1, "prompt": None}
    result = checkpoint._validate_saved_result(data, "0" * 64, page)
    assert result.layout == (None if "layout" in missing else LAYOUT)
    with pytest.raises(ValueError, match="Invalid saved response"):
        checkpoint._validate_saved_result({**data, "extra": 1}, "0" * 64, page)
