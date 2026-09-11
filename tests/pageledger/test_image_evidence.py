"""Input images are first-class evidence, including unsuccessful paid responses."""
from __future__ import annotations

import copy
import hashlib
import json
import struct

import pytest

from pageledger import adapters, runner


def image_descriptor(root, source, page_number=1, prompt=""):
    # A bounded JPEG header fixture; image decoding is the adapter's responsibility.
    jpeg = (b"\xff\xd8\xff\xc0" + struct.pack(">H B H H B", 17, 8, 2, 3, 3)
            + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
            + b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\x00\xff\xd9")
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    artifact = evidence / f"page-{page_number}.jpg"
    artifact.write_bytes(jpeg)
    return {
        "schema_version": "0.1", "kind": "page_image",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "page_number": page_number, "artifact": artifact.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(jpeg).hexdigest(), "media_type": "image/jpeg",
        "bytes": len(jpeg), "width": 3, "height": 2, "color_space": "RGB",
        "crop": {"kind": "full_page"},
        "renderer": {"command": "pdftoppm", "version": "test 1", "parameters": {"dpi": 150}},
        "requested_model": "gemini-test", "model": "gemini-test-returned", "provider": None,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }


def test_pdf_document_text_is_cached_and_changed_source_rejected(tmp_path, monkeypatch):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"PDF fixture")
    reads = []
    class Page:
        def extract_text(self):
            reads.append("text")
            return "page"
    class Reader:
        def __init__(self, _handle):
            reads.append("reader")
            self.pages = [Page(), Page()]
    monkeypatch.setattr(adapters, "_load_pypdf", lambda: Reader)
    adapter = adapters.PdfTextAdapter()
    assert adapter.page_count(source) == 2
    for number in (1, 2, 1):
        assert adapter.extract(source, page_id="p", page_number=number,
                               action="transcribe_text").content == "page"
    assert reads == ["reader", "text", "text"]
    with pytest.raises(ValueError, match="out of range"):
        adapter.extract(source, page_id="p", page_number=3, action="transcribe_text")
    source.write_bytes(b"PDF changed")
    with pytest.raises(ValueError, match="changed"):
        adapter.page_count(source)


@pytest.mark.parametrize("damage", [None, "hash", "dimensions", "traversal", "symlink",
                                   "page", "source", "prompt", "boolean", "renderer", "crop"])
def test_input_image_evidence_rejects_unbound_or_unsafe_artifacts(tmp_path, damage):
    from pageledger.image_evidence import validate_input_evidence
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    evidence = image_descriptor(tmp_path, source)
    if damage == "hash":
        (tmp_path / evidence["artifact"]).write_bytes(b"changed")
    elif damage == "dimensions":
        evidence["width"] = 4
    elif damage == "traversal":
        evidence["artifact"] = "evidence/../page.jpg"
    elif damage == "symlink":
        artifact = tmp_path / evidence["artifact"]
        artifact.unlink()
        artifact.symlink_to(source)
    elif damage == "page":
        evidence["page_number"] = 2
    elif damage == "source":
        evidence["source_sha256"] = "f" * 64
    elif damage == "prompt":
        evidence["prompt_sha256"] = "f" * 64
    elif damage == "boolean":
        evidence["width"] = True
    elif damage == "renderer":
        evidence["renderer"]["parameters"] = {"dpi": float("nan")}
    elif damage == "crop":
        evidence["crop"] = {"kind": "box", "units": "pdf_points", "x": 0, "y": 0,
                            "width": -1, "height": 2}
    kwargs = dict(root=tmp_path, source_sha256=hashlib.sha256(b"source").hexdigest(),
                  page_number=1, prompt_sha256=hashlib.sha256(b"").hexdigest())
    if damage:
        with pytest.raises(ValueError, match="[Ee]vidence|[Ii]mage|JPEG"):
            validate_input_evidence(evidence, **kwargs)
    else:
        validate_input_evidence(evidence, **kwargs)


@pytest.fixture
def image_job(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("one\ftwo")
    config = tmp_path / "config.yml"
    config.write_text("schema_version: '0.1'\nrun:\n  adapter: text\n"
                      "taxonomy:\n  page_types:\n    prose:\n      default_action: transcribe_text\n")
    out = tmp_path / "run"
    class ImageAdapter(adapters.TextAdapter):
        calls = 0
        fail = False
        def extract(self, source, *, page_id, page_number, action, prompt):
            self.calls += 1
            result = adapters.ExtractionResult(
                content="partial" if self.fail else "complete", format="text", confidence=None,
                model="gemini-test-returned", warnings=[], usage={"pages": 1, "tokens": 22},
                input_evidence=image_descriptor(out, source, page_number, prompt or ""))
            if self.fail:
                raise adapters.AdapterFailure("MODEL_OUTPUT_TRUNCATED", partial_result=result)
            return result
    adapter = ImageAdapter()
    monkeypatch.setattr(runner, "load_adapter", lambda *a: adapter)
    return source, config, out, adapter


def launch_image(job):
    return runner.run(inputs=[job[0]], config_path=job[1], out_dir=job[2],
                      dry_run=False, resumable=True)


def test_partial_failure_is_retained_with_usage_and_never_retried(image_job):
    from pageledger.checkpoint import Checkpoint
    image_job[3].fail = True
    with pytest.raises(RuntimeError):
        launch_image(image_job)
    checkpoint = Checkpoint(image_job[2], existing=True)
    failed = checkpoint.records["doc_0001_page_0001"]
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "MODEL_OUTPUT_TRUNCATED"
    assert failed["partial_result"]["content"] == "partial"
    assert failed["partial_result"]["usage"]["tokens"] == 22
    assert "input_evidence" in failed["partial_result"]
    assert "completion" not in failed
    with pytest.raises(ValueError, match="failure"):
        runner.resume(image_job[2])
    assert image_job[3].calls == 1
    (image_job[2] / failed["partial_result"]["input_evidence"]["artifact"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="[Ee]vidence|[Ii]mage|JPEG"):
        Checkpoint(image_job[2], existing=True)


def test_completed_images_verified_and_bundle_fails_explicitly(image_job):
    from pageledger.replay import ReplayError, bundle_run
    from pageledger.verify import verify_run
    launch_image(image_job)
    assert verify_run(image_job[2])["status"] == "pass"
    rows = [json.loads(line) for line in (image_job[2] / "provenance.jsonl").read_text().splitlines()]
    assert rows[0]["input_evidence"]["width"] == 3
    with pytest.raises(ReplayError) as error:
        bundle_run(image_job[2], image_job[2].parent / "bundle")
    assert error.value.code == "image_evidence_unsupported"
    (image_job[2] / rows[0]["input_evidence"]["artifact"]).write_bytes(b"tampered")
    report = verify_run(image_job[2])
    assert report["status"] == "fail"
    assert any(issue["code"] == "input_evidence_invalid" for issue in report["errors"])


def test_image_descriptor_schema_agrees_with_runtime(tmp_path):
    from pathlib import Path

    import jsonschema
    source = tmp_path / "source"
    source.write_bytes(b"source")
    evidence = image_descriptor(tmp_path, source)
    schema = json.loads((Path(__file__).parents[2] / "schemas/image-evidence.schema.json").read_text())
    jsonschema.validate(evidence, schema)
    bad = copy.deepcopy(evidence)
    bad["width"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_image_receipts_validate_against_published_schemas(image_job):
    from pathlib import Path

    import jsonschema
    from referencing import Registry, Resource

    schemas = Path(__file__).parents[2] / "schemas"
    def retrieve(uri):
        return Resource.from_contents(json.loads((schemas / uri.rsplit("/", 1)[-1]).read_text()))
    registry = Registry(retrieve=retrieve)
    schema = json.loads((schemas / "checkpoint-page.schema.json").read_text())
    launch_image(image_job)
    for path in (image_job[2] / ".checkpoint/pages").glob("*.json"):
        jsonschema.Draft202012Validator(schema, registry=registry).validate(json.loads(path.read_text()))


def test_response_image_damage_prevents_resume_before_more_calls(image_job, monkeypatch):
    from pageledger.checkpoint import Checkpoint
    original = Checkpoint.save
    def interrupt(self, page_id, record):
        original(self, page_id, record)
        if record["state"] == "response":
            raise KeyboardInterrupt
    monkeypatch.setattr(Checkpoint, "save", interrupt)
    with pytest.raises(KeyboardInterrupt):
        launch_image(image_job)
    (image_job[2] / "evidence/page-1.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Image evidence"):
        runner.resume(image_job[2])
    assert image_job[3].calls == 1


@pytest.mark.parametrize("artifact", ["evidence/../a.jpg", "evidence/./a.jpg", "evidence/a//b.jpg",
                                     "evidence/a/../b.jpg", "/evidence/a.jpg"])
def test_schema_rejects_artifact_traversal_and_noncanonical_paths(tmp_path, artifact):
    from pathlib import Path

    import jsonschema

    source = tmp_path / "source"
    source.write_bytes(b"source")
    evidence = image_descriptor(tmp_path, source)
    evidence["artifact"] = artifact
    schema = json.loads((Path(__file__).parents[2] / "schemas/image-evidence.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(evidence, schema)


def test_circular_evidence_is_rejected_as_invalid_input(tmp_path):
    from pageledger.image_evidence import validate_input_evidence
    source = tmp_path / "source"
    source.write_bytes(b"source")
    evidence = image_descriptor(tmp_path, source)
    evidence["renderer"]["parameters"]["cycle"] = evidence
    with pytest.raises(ValueError, match="Image evidence"):
        validate_input_evidence(evidence)
