"""A IIIF snapshot is a folder of page images: fetch it, process it, verify it, move it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from test_iiif import SERVICE, FakeResponse, FakeServer, info_v3, jpeg, manifest_v3
from test_image_sources import StubOCR, stub, validate  # noqa: F401 (stub is a fixture)

from pageledger import iiif, review_html
from pageledger.checkpoint import read_record
from pageledger.image_source import read_image_source
from pageledger.processing import process, verify_job
from pageledger.review_html import write_review_html
from pageledger.verify import verify_run

MANIFEST_URL = "https://iiif.example.org/item/manifest.json"


def three_canvases() -> FakeServer:
    routes: dict[str, object] = {MANIFEST_URL: manifest_v3([f"{SERVICE}{n}" for n in (1, 2, 3)])}
    for n in (1, 2, 3):
        routes[f"{SERVICE}{n}/info.json"] = info_v3(f"{SERVICE}{n}", 300 + n, 400)
        routes[f"{SERVICE}{n}/full/max/0/default.jpg"] = jpeg(300 + n, 400)
    return FakeServer(routes)


def fetch(out: Path, *, missing_third: bool, resume: bool = False) -> dict:
    server = three_canvases()
    if missing_third:
        server.routes[f"{SERVICE}3/full/max/0/default.jpg"] = FakeResponse(404)
    return iiif.fetch_iiif(
        MANIFEST_URL, out, pages="2,3", resume=resume, opener=server, sleep=lambda _s: None
    )


def config(tmp_path: Path) -> Path:
    path = tmp_path / "job.yml"
    data = {
        "schema_version": "0.1",
        "processing": {"local_text": {"adapter": "pdf_text"}, "local_ocr": {"adapter": "stub"}},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def codes(result: dict) -> list[str]:
    return [warning["code"] for warning in result["warnings"]]


def test_a_fetched_snapshot_is_read_as_a_folder_of_page_images(tmp_path):
    snap = tmp_path / "snap"
    assert fetch(snap, missing_third=True)["status"] == "incomplete"
    source = read_image_source(snap)
    assert [page.name for page in source.pages] == ["0002.jpg"]
    record = (snap / iiif.RECORD_NAME).read_bytes()
    assert source.fetch is not None
    assert source.fetch["status"] == "incomplete"
    assert source.fetch["requested_url"] == MANIFEST_URL
    assert source.fetch["sha256"] == iiif._sha256(record)
    assert source.fetch["canvases"] == [
        {
            "name": "0002.jpg",
            "canvas_index": 2,
            "canvas_id": "https://iiif.example.org/item/canvas/2",
        }
    ]
    # The record is context: the same images without it have the same identity.
    plain = tmp_path / "plain"
    plain.mkdir()
    shutil.copy2(snap / "0002.jpg", plain / "0002.jpg")
    assert read_image_source(plain).sha256 == source.sha256
    assert read_image_source(plain).fetch is None


def test_a_snapshot_that_does_not_match_its_record_is_refused_and_says_why(tmp_path):
    snap = tmp_path / "snap"
    fetch(snap, missing_third=False)
    (snap / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ValueError, match="'notes.txt' is not a page image"):
        read_image_source(snap)
    (snap / "notes.txt").unlink()
    (snap / "0003.jpg").write_bytes(jpeg(303, 400, size=65))
    with pytest.raises(ValueError, match="images changed since they were fetched: 0003.jpg"):
        read_image_source(snap)
    (snap / iiif.RECORD_NAME).unlink()
    with pytest.raises(ValueError, match="without iiif-fetch.json the folder is not a IIIF"):
        read_image_source(snap)
    (snap / "extra").mkdir()
    shutil.rmtree(snap / "iiif")
    with pytest.raises(ValueError, match="'extra' is not one"):
        read_image_source(snap)


def test_fetch_process_verify_and_move_a_snapshot(stub, tmp_path, monkeypatch):  # noqa: F811
    snap = tmp_path / "snap"
    fetch(snap, missing_third=True)
    root = tmp_path / "job"
    assert process(source=snap, config_path=config(tmp_path), out_dir=root)["status"] == "completed"

    job = read_record(root / "job.json")
    validate(json.loads((root / "job.json").read_text()), "job")
    validate(json.loads((root / "document.json").read_text()), "document")
    annotation = job["source"]["iiif"]
    assert annotation["status"] == "incomplete"
    assert annotation["canvases"][0]["canvas_index"] == 2
    assert job["source"]["images"][0]["name"] == "0002.jpg"
    assert job["pages"][0]["page_number"] == 1
    for run_dir in (root / "attempts").iterdir():
        manifest = json.loads((run_dir / "manifest.json").read_text())
        validate(manifest, "manifest")
        validate(json.loads((run_dir / "checkpoint.json").read_text()), "checkpoint")
        assert manifest["inputs"][0]["iiif"] == annotation
        assert verify_run(run_dir)["status"] == "pass"
        assert "iiif_snapshot_incomplete" in codes(verify_run(run_dir))

    report = (root / "report.md").read_text(encoding="utf-8")
    assert f"IIIF snapshot of {MANIFEST_URL}, incomplete" in report
    assert "| [1 (0002.jpg)](<../snap/0002.jpg>) |" in report
    assert "[Source page (0002.jpg)](<../snap/0002.jpg>)" in (root / "transcript.md").read_text()

    result = verify_job(root)
    assert result["status"] == "pass" and codes(result) == ["iiif_snapshot_incomplete"]

    # A moved copy of the job verifies where it is, against the same snapshot.
    moved = tmp_path / "deposit" / "job"
    shutil.copytree(root, moved)
    result = verify_job(moved)
    assert result["status"] == "pass"
    assert codes(result) == ["job_relocated", "iiif_snapshot_incomplete"]

    # The record rewritten with the same images is context that changed, not a failure.
    record = json.loads((snap / iiif.RECORD_NAME).read_text())
    record["finished_at"] = "2026-11-11T00:00:00Z"
    (snap / iiif.RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    assert codes(verify_job(root)) == ["iiif_snapshot_incomplete", "iiif_record_changed"]

    # A resume that fetches the missing canvas adds an image: the job's own image is
    # unchanged, so it verifies with a warning naming what was added.
    (snap / iiif.RECORD_NAME).write_bytes(_restore(record))
    assert fetch(snap, missing_third=False, resume=True)["status"] == "complete"
    result = verify_job(root)
    assert result["status"] == "pass", result
    assert codes(result) == ["iiif_snapshot_incomplete", "iiif_snapshot_grown"]
    assert result["warnings"][1]["added"] == ["0003.jpg"]
    assert verify_job(moved)["status"] == "pass"
    run_dir = next((root / "attempts").iterdir())
    assert "source_changed" in codes(verify_run(run_dir))

    # The review view still shows each page's own image by its recorded name.
    seen = []

    def render(source, page_number, target):
        seen.append((source.suffix, page_number))
        target.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(review_html, "render_page_image", render)
    monkeypatch.setattr(review_html.shutil, "which", lambda name: "/usr/bin/" + name)
    write_review_html(root, tmp_path / "view")
    page = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert seen == [(".pdf", 1)]
    assert "Page 1 (0002.jpg)" in page
    assert 'href="../snap/0002.jpg"' in page

    # A changed image the job read still fails, as for any folder.
    (snap / "0002.jpg").write_bytes(jpeg(302, 400, size=65))
    assert verify_job(root)["status"] == "fail"


def _restore(record: dict) -> bytes:
    """The record as fetch-iiif writes it, so a resume can continue it."""
    return iiif._dump(record)
