"""The vision adapter against a fake OpenAI-compatible endpoint on this machine."""

from __future__ import annotations

import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from pageledger import vision
from pageledger.adapters import AdapterFailure, PageLedgerDiagnostic, load_adapter
from pageledger.cli import main
from pageledger.image_evidence import validate_input_evidence
from pageledger.processing import process

pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs Poppler")
READING = {
    "model": "served-model",
    "choices": [{"message": {"content": "page text"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


class Endpoint(BaseHTTPRequestHandler):
    """Answers every POST with the class's configured reply and records the request."""

    reply: dict | bytes = READING
    status = 200
    delay = 0.0
    requests: list[tuple[dict, dict]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append((dict(self.headers), json.loads(body)))
        time.sleep(type(self).delay)
        if type(self).status == 302:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        reply = type(self).reply
        data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    Endpoint.reply, Endpoint.status, Endpoint.delay, Endpoint.requests = READING, 200, 0.0, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    thread.join()


@pytest.fixture
def pdf(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    path = tmp_path / "page.pdf"
    writer.write(path)
    return path


def _read(pdf, endpoint, **options):
    adapter = load_adapter("vision", {"base_url": endpoint, "model": "requested", **options})
    return adapter.extract(
        pdf, page_id="doc_0001_page_0001", page_number=1, action="transcribe_text", prompt="Read."
    )


def _failure(pdf, endpoint, **options):
    with pytest.raises(AdapterFailure) as caught:
        _read(pdf, endpoint, **options)
    return caught.value


def test_reads_a_page_and_records_what_produced_the_text(pdf, endpoint):
    result = _read(pdf, endpoint, max_image_side=512)

    assert result.content == "page text"
    assert result.model.startswith("requested (served as served-model); pdftoppm")
    assert result.model.endswith("; scale-to=512")
    assert result.usage["tokens"] == 7 and result.usage["pages"] == 1
    _headers, sent = Endpoint.requests[0]
    assert sent["model"] == "requested" and sent["max_tokens"] == 8192
    text, image = sent["messages"][0]["content"]
    assert text == {"type": "text", "text": "Read."}
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_stage_evidence_records_the_exact_jpeg_and_served_model(pdf, endpoint, tmp_path):
    run = tmp_path / "run"
    run.mkdir()

    result = _read(pdf, endpoint, evidence_dir=str(run / "evidence"))

    evidence = result.input_evidence
    validate_input_evidence(evidence, root=run, page_number=1)
    assert result.model == evidence["model"] == "served-model"
    assert evidence["requested_model"] == "requested"
    assert "-scale-to" in evidence["renderer"]["parameters"]["arguments"]
    assert (run / evidence["artifact"]).is_file()


@pytest.mark.parametrize(
    ("finish", "content", "code"),
    [
        ("length", "cut off", "MODEL_OUTPUT_TRUNCATED"),
        ("content_filter", "", "MODEL_CONTENT_FILTERED"),
        ("recitation", "", "MODEL_RECITATION"),
        ("tool_calls", "text", "MODEL_INVALID_RESPONSE"),
        ("stop", "  ", "MODEL_EMPTY_RESPONSE"),
    ],
)
def test_finish_states_become_typed_failures(pdf, endpoint, finish, content, code):
    Endpoint.reply = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}

    failure = _failure(pdf, endpoint)

    assert failure.code == code
    assert failure.partial_result.content == content


@pytest.mark.parametrize(
    ("status", "reply", "code"),
    [
        (429, READING, "MODEL_QUOTA"),
        (402, READING, "MODEL_QUOTA"),
        (500, READING, "MODEL_HTTP_ERROR"),
        (302, READING, "MODEL_HTTP_ERROR"),
        (200, b"not json", "MODEL_INVALID_RESPONSE"),
        (200, b" " * (vision.MAX_RESPONSE_BYTES + 1), "MODEL_INVALID_RESPONSE"),
    ],
    ids=["429", "402", "500", "redirect", "not-json", "oversized"],
)
def test_endpoint_failures_become_typed_failures(pdf, endpoint, status, reply, code):
    Endpoint.status, Endpoint.reply = status, reply

    failure = _failure(pdf, endpoint)

    assert failure.code == code
    assert len(Endpoint.requests) == 1


def test_a_slow_endpoint_times_out(pdf, endpoint):
    Endpoint.delay = 2

    assert _failure(pdf, endpoint, timeout_seconds=0.5).code == "MODEL_TIMEOUT"


def test_an_unreachable_endpoint_is_a_network_error(pdf):
    assert _failure(pdf, "http://127.0.0.1:9/v1").code == "MODEL_NETWORK_ERROR"


def test_the_key_is_sent_but_never_recorded(pdf, endpoint, monkeypatch):
    monkeypatch.setenv("VISION_KEY", "private-key-sentinel")

    result = _read(pdf, endpoint, env_key="VISION_KEY")
    Endpoint.status = 500
    failure = _failure(pdf, endpoint, env_key="VISION_KEY")

    assert Endpoint.requests[0][0]["Authorization"] == "Bearer private-key-sentinel"
    for record in (repr(result), str(failure), repr(failure.partial_result)):
        assert "private-key-sentinel" not in record


def test_a_named_key_that_is_not_set_is_model_unavailable(pdf, endpoint, monkeypatch):
    monkeypatch.delenv("VISION_KEY", raising=False)

    assert _failure(pdf, endpoint, env_key="VISION_KEY").code == "MODEL_UNAVAILABLE"
    assert Endpoint.requests == []


@pytest.mark.parametrize(
    "base_url",
    ["http://example.org/v1", "https://example.org/v1", "http://10.0.0.2:8080/v1"],
)
def test_other_machines_need_https_and_allow_remote(base_url):
    with pytest.raises(ValueError, match="allow_remote"):
        vision.VisionAdapter(base_url=base_url, model="m")


def test_endpoints_on_this_machine_and_allowed_remote_ones():
    for base_url in ("http://localhost:8080/v1", "http://127.0.0.1/v1", "http://[::1]:8080/v1"):
        assert "remote" not in vision.VisionAdapter(base_url=base_url, model="m").capabilities
    remote = vision.VisionAdapter(base_url="https://example.org/v1", model="m", allow_remote=True)
    assert remote.capabilities == ("ocr", "page_image", "generative", "remote")


def test_a_page_that_cannot_fit_the_request_is_a_render_error(pdf, endpoint, monkeypatch):
    monkeypatch.setattr(vision, "_JPEG_BUDGET", 100)

    assert _failure(pdf, endpoint).code == "IMAGE_RENDER_ERROR"
    assert Endpoint.requests == []


def test_a_missing_renderer_stops_before_any_page(monkeypatch):
    monkeypatch.setattr("pageledger.adapters.shutil.which", lambda name: None)
    adapter = vision.VisionAdapter(base_url="http://127.0.0.1:9/v1", model="m")

    with pytest.raises(PageLedgerDiagnostic) as caught:
        adapter.preflight([])
    assert caught.value.code == "missing_binary"


def test_run_reads_pages_with_the_vision_adapter(pdf, endpoint, tmp_path):
    config = tmp_path / "config.yml"
    options = {"base_url": endpoint, "model": "requested"}
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "run": {"adapter": "vision", "adapter_options": options},
                "taxonomy": {"page_types": {"prose": {"default_action": "transcribe_text"}}},
            }
        )
    )
    out = tmp_path / "out"

    assert main(["run", str(pdf), "--config", str(config), "--out", str(out)]) == 0
    assert (out / "raw" / "doc_0001_page_0001.txt").read_text() == "page text"


def test_a_document_job_can_use_a_local_vision_model_for_ocr(pdf, endpoint, tmp_path):
    config = tmp_path / "config.yml"
    stages = {
        "local_text": {"adapter": "pdf_text"},
        "local_ocr": {
            "adapter": "vision",
            "adapter_options": {"base_url": endpoint, "model": "requested"},
        },
    }
    config.write_text(yaml.safe_dump({"schema_version": "0.1", "processing": stages}))

    result = process(source=pdf, config_path=config, out_dir=tmp_path / "job")

    assert result["status"] == "completed"
    page = json.loads((tmp_path / "job" / "document.json").read_text())["pages"][0]
    assert page["selected_output"]["text"] == "page text"


def test_a_document_job_can_send_empty_pages_to_a_vision_model(pdf, endpoint, tmp_path):
    config = tmp_path / "config.yml"
    processing = {
        "local_text": {"adapter": "pdf_text"},
        "local_ocr": {"adapter": "pdf_text"},
        "image": {
            "adapter": "vision",
            "adapter_options": {"base_url": endpoint, "model": "requested"},
        },
        "limits": {"max_image_pages": 1},
    }
    config.write_text(yaml.safe_dump({"schema_version": "0.1", "processing": processing}))

    result = process(source=pdf, config_path=config, out_dir=tmp_path / "job")

    assert result["status"] == "completed"
    page = json.loads((tmp_path / "job" / "document.json").read_text())["pages"][0]
    assert page["selected_output"]["attempt_id"].startswith("image-")
    assert page["disposition"] == "unconfirmed_model_output"
    assert any((tmp_path / "job" / "attempts").glob("image-*/evidence/*"))
