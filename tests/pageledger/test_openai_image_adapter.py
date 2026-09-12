"""No provider traffic: a local HTTP fixture inspects real JPEG requests."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pageledger.adapters import AdapterFailure


def example_class():
    path = Path(__file__).parents[2] / "examples/openai_image_adapter.py"
    spec = importlib.util.spec_from_file_location("openai_image_example_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OpenAIImageAdapter


@pytest.fixture
def endpoint():
    state = {"requests": [], "status": 200, "response": {
        "model": "gemini-test-returned", "provider": "google", "usage": {"total_tokens": 17},
        "choices": [{"message": {"content": "transcribed text"}, "finish_reason": "stop"}]}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            state["requests"].append((self.path, None))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "gemini-test"}]}).encode())
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((self.path, json.loads(payload)))
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(json.dumps(state["response"]).encode())
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def image_adapter(tmp_path, endpoint):
    pytest.importorskip("PIL")
    pypdf = pytest.importorskip("pypdf")
    if not shutil.which("pdftoppm"):
        pytest.skip("Poppler renderer unavailable")
    source = tmp_path / "scan.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(source)
    root = tmp_path / "run"
    root.mkdir()
    adapter = example_class()(model="gemini-test", evidence_dir=str(root / "evidence"),
                              base_url=endpoint[0], env_key=None)
    return adapter, source, root


def test_transmitted_jpeg_is_retained_and_model_discovery_occurs_once(image_adapter, endpoint):
    from pageledger.image_evidence import validate_input_evidence
    adapter, source, root = image_adapter
    for page_id in ("first", "second"):
        result = adapter.extract(source, page_id=page_id, page_number=1,
                                 action="transcribe_text", prompt="Read this page.")
        validate_input_evidence(result.input_evidence, root=root)
        assert result.content == "transcribed text"
        assert result.usage["tokens"] == 17
        assert result.input_evidence["model"] == "gemini-test-returned"
        payload = endpoint[1]["requests"][-1][1]
        url = payload["messages"][-1]["content"][1]["image_url"]["url"]
        transmitted = base64.b64decode(url.split(",", 1)[1])
        assert transmitted == (root / result.input_evidence["artifact"]).read_bytes()
        assert hashlib.sha256(transmitted).hexdigest() == result.input_evidence["sha256"]
        assert payload["max_tokens"] == 8192
        assert payload["model"] == "gemini-test"
    assert [path for path, body in endpoint[1]["requests"] if body is None] == ["/v1/models"]


@pytest.mark.parametrize("case,code", [("truncated", "MODEL_OUTPUT_TRUNCATED"),
                                      ("quota", "MODEL_QUOTA"), ("invalid", "MODEL_INVALID_RESPONSE")])
def test_failed_request_never_retries_and_retains_evidence(image_adapter, endpoint, case, code):
    adapter, source, root = image_adapter
    if case == "truncated":
        endpoint[1]["response"]["choices"][0]["finish_reason"] = "length"
    elif case == "quota":
        endpoint[1]["status"] = 429
    else:
        endpoint[1]["response"] = {"unexpected": "secret should not escape"}
    with pytest.raises(AdapterFailure) as failure:
        adapter.extract(source, page_id="first", page_number=1,
                        action="transcribe_text", prompt="Read this page.")
    assert failure.value.code == code
    assert len([body for _, body in endpoint[1]["requests"] if body is not None]) == 1
    result = failure.value.partial_result
    assert result.input_evidence["artifact"] == "evidence/first.jpg"
    assert (root / result.input_evidence["artifact"]).is_file()
    if case == "truncated":
        assert result.content == "transcribed text"
        assert result.usage["tokens"] == 17
    assert "secret" not in str(failure.value)


def test_unavailable_and_third_party_models_make_no_paid_request(tmp_path, endpoint):
    cls = example_class()
    with pytest.raises(ValueError, match="Gemini|DeepSeek"):
        cls(model="gpt-5", evidence_dir=str(tmp_path / "evidence"), base_url=endpoint[0])
    adapter = cls(model="deepseek-missing", evidence_dir=str(tmp_path / "evidence"),
                  base_url=endpoint[0], env_key=None)
    with pytest.raises(AdapterFailure) as failure:
        adapter.extract(tmp_path / "does-not-matter.pdf", page_id="first", page_number=1,
                        action="transcribe_text", prompt="Read this page.")
    assert failure.value.code == "MODEL_UNAVAILABLE"
    assert not any(body is not None for _, body in endpoint[1]["requests"])


def test_missing_pillow_fails_before_any_paid_request(tmp_path, monkeypatch, endpoint):
    import sys
    monkeypatch.setitem(sys.modules, "PIL", None)
    adapter = example_class()(model="gemini-test", evidence_dir=str(tmp_path / "evidence"),
                              base_url=endpoint[0], env_key=None)
    with pytest.raises(AdapterFailure, match="IMAGE_RENDER_ERROR"):
        adapter.extract(tmp_path / "scan.pdf", page_id="p", page_number=1,
                        action="transcribe_text", prompt="Read this page.")
    assert not any(body is not None for _, body in endpoint[1]["requests"])


def test_total_request_limit_rejects_large_prompt_without_paid_call(image_adapter, endpoint):
    adapter, source, root = image_adapter
    with pytest.raises(AdapterFailure, match="IMAGE_EVIDENCE_INVALID") as failure:
        adapter.extract(source, page_id="big", page_number=1, action="transcribe_text",
                        prompt="x" * (4 * 1024 * 1024))
    assert failure.value.partial_result.input_evidence["artifact"] == "evidence/big.jpg"
    assert not any(body is not None for _, body in endpoint[1]["requests"])


def test_timeout_remains_typed_and_retains_input(image_adapter, monkeypatch):
    adapter, source, root = image_adapter
    adapter._check_model()
    import urllib.request
    class TimeoutOpener:
        def open(self, *args, **kwargs):
            raise TimeoutError("untrusted secret")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: TimeoutOpener())
    with pytest.raises(AdapterFailure) as failure:
        adapter.extract(source, page_id="timeout", page_number=1,
                        action="transcribe_text", prompt="Read the page")
    assert failure.value.code == "MODEL_TIMEOUT"
    assert failure.value.partial_result.content == ""
    assert (root / failure.value.partial_result.input_evidence["artifact"]).is_file()
    assert "secret" not in str(failure.value)


def test_gateway_image_limit_compresses_retained_and_transmitted_bytes(tmp_path, endpoint, monkeypatch):
    import random
    import subprocess

    Image = pytest.importorskip("PIL.Image")
    cls = example_class()
    monkeypatch.setattr(shutil, "which", lambda name: "/test/pdftoppm")
    pixels = random.Random(0).randbytes(1400 * 1400 * 3)

    def render(command, **kwargs):
        if command[-1] == "-v":
            return subprocess.CompletedProcess(command, 0, stderr=b"test renderer 1")
        Image.frombytes("RGB", (1400, 1400), pixels).save(command[-1] + ".jpg", quality=90)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", render)
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"synthetic renderer input")
    root = tmp_path / "run"
    root.mkdir()
    adapter = cls(model="gemini-test", evidence_dir=str(root / "evidence"),
                  base_url=endpoint[0], env_key=None, max_image_bytes=1024 * 1024)
    result = adapter.extract(source, page_id="p", page_number=1, action="transcribe_text")
    evidence = result.input_evidence
    payload = endpoint[1]["requests"][-1][1]
    transmitted = base64.b64decode(payload["messages"][-1]["content"][1]["image_url"]["url"].split(",", 1)[1])
    assert len(transmitted) == evidence["bytes"] <= 1024 * 1024
    assert transmitted == (root / evidence["artifact"]).read_bytes()
    assert evidence["renderer"]["parameters"]["jpeg_quality"] < 90
    assert evidence["renderer"]["parameters"]["max_image_bytes"] == 1024 * 1024


def test_unachievable_image_limit_stops_before_submission(image_adapter, endpoint):
    original, source, root = image_adapter
    adapter = type(original)(model="gemini-test", evidence_dir=str(root / "evidence"),
                             base_url=endpoint[0], env_key=None, max_image_bytes=1)
    with pytest.raises(AdapterFailure, match="IMAGE_RENDER_ERROR"):
        adapter.extract(source, page_id="p", page_number=1, action="transcribe_text")
    assert not any(body is not None for _, body in endpoint[1]["requests"])
    assert not (root / "evidence/p.jpg").exists()


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, 3 * 1024 * 1024 + 1])
def test_invalid_image_limit_is_rejected(tmp_path, limit):
    with pytest.raises(ValueError, match="max_image_bytes"):
        example_class()(model="gemini-test", evidence_dir=str(tmp_path / "evidence"),
                        max_image_bytes=limit)


def test_gateway_dimension_limit_bounds_actual_image(image_adapter, endpoint):
    from pageledger.image_evidence import jpeg_dimensions

    original, source, root = image_adapter
    adapter = type(original)(model="gemini-test", evidence_dir=str(root / "evidence"),
                             base_url=endpoint[0], env_key=None, max_image_dimension=512)
    result = adapter.extract(source, page_id="small", page_number=1, action="transcribe_text")
    evidence = result.input_evidence
    payload = endpoint[1]["requests"][-1][1]
    transmitted = base64.b64decode(payload["messages"][-1]["content"][1]["image_url"]["url"].split(",", 1)[1])
    assert transmitted == (root / evidence["artifact"]).read_bytes()
    assert jpeg_dimensions(transmitted) == (evidence["width"], evidence["height"])
    assert max(evidence["width"], evidence["height"]) == 512
    assert evidence["renderer"]["parameters"]["scale_to"] == 512


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, 4097])
def test_invalid_dimension_limit_is_rejected(tmp_path, limit):
    with pytest.raises(ValueError, match="max_image_dimension"):
        example_class()(model="gemini-test", evidence_dir=str(tmp_path / "evidence"),
                        max_image_dimension=limit)
