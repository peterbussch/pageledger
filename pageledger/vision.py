"""The built-in `vision` adapter: page images read by an OpenAI-compatible vision model."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .adapters import (
    AdapterFailure,
    ExtractionResult,
    PageLedgerDiagnostic,
    _pdftoppm_model_string,
    _read_once,
    _require_binary,
    _run_ocr_command,
    ocr_pdf_page_count,
)
from .checkpoint import atomic_bytes, file_digest
from .image_evidence import MAX_IMAGE_DIMENSION, jpeg_dimensions, validate_input_evidence

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The JPEG travels base64-encoded, a third larger, inside the JSON request.
_JPEG_BUDGET = (MAX_REQUEST_BYTES - 64 * 1024) * 3 // 4
# A page too large for the budget is rendered smaller and compressed harder.
_RENDER_TRIES = ((1.0, 90), (0.75, 80), (0.5, 70))
_RENDER_TIMEOUT_SECONDS = 120
_TRUNCATED = {"length", "max_tokens", "max_output_tokens"}
_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")
_PAGE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
DEFAULT_PROMPT = (
    "Transcribe this page exactly as printed. Keep the original spelling and "
    "historical letters (such as ѣ, і, ѳ, ѵ and final ъ), line breaks, running "
    "heads and page numbers. Add no commentary and no Markdown fences."
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _on_this_machine(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class VisionAdapter:
    """Send each page as one JPEG to a chat-completions endpoint and keep its reading."""

    name = "vision"
    version = "0.1"
    deterministic = False
    input_types = ("pdf",)
    output_types = ("text",)

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        env_key: str | None = None,
        allow_remote: bool = False,
        max_tokens: int = 8192,
        timeout_seconds: float = 300,
        max_image_side: int = 2048,
        reasoning_effort: str | None = None,
        evidence_dir: str | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
        if (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "vision base_url must be an http:// or https:// URL without credentials, "
                "query or fragment, e.g. http://127.0.0.1:8080/v1"
            )
        if type(allow_remote) is not bool:
            raise ValueError("vision allow_remote must be true or false")
        remote = not _on_this_machine(parsed.hostname)
        if remote and not (parsed.scheme == "https" and allow_remote):
            raise ValueError(
                "vision sends pages to another machine only over https:// with "
                "allow_remote: true; plain http:// is for localhost, 127.0.0.1 and ::1"
            )
        if not isinstance(model, str) or not model.strip():
            raise ValueError("vision model must name the model the endpoint serves")
        if env_key is not None and (
            not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key)
        ):
            raise ValueError("vision env_key must name an environment variable, or be omitted")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 131072:
            raise ValueError("vision max_tokens must be an integer from 1 to 131072")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600
        ):
            raise ValueError("vision timeout_seconds must be a number above 0 and at most 600")
        if type(max_image_side) is not int or not 256 <= max_image_side <= MAX_IMAGE_DIMENSION:
            raise ValueError(
                f"vision max_image_side must be an integer from 256 to {MAX_IMAGE_DIMENSION}"
            )
        if reasoning_effort is not None and reasoning_effort not in _REASONING_EFFORTS:
            raise ValueError(
                "vision reasoning_effort must be one of none, minimal, low, medium or high, "
                "or be omitted"
            )
        if evidence_dir is not None and (
            not isinstance(evidence_dir, str)
            or not Path(evidence_dir).is_absolute()
            or Path(evidence_dir).name != "evidence"
        ):
            raise ValueError("vision evidence_dir must be the absolute path of a run's evidence/")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.env_key = env_key
        self.max_tokens = max_tokens
        self.timeout_seconds = float(timeout_seconds)
        self.max_image_side = max_image_side
        self.reasoning_effort = reasoning_effort
        self.evidence_dir = None if evidence_dir is None else Path(evidence_dir)
        self.capabilities = ("ocr", "page_image", "generative", *(("remote",) if remote else ()))
        self._source_hashes: dict = {}

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def preflight(self, sources: list[Path]) -> None:
        """Refuse before any page runs when the renderer is missing."""
        _require_binary("pdftoppm")

    def page_count(self, source: Path) -> int:
        return ocr_pdf_page_count(source)

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        if not self.supports(action):
            raise ValueError("The vision adapter only transcribes pages (transcribe_text)")
        if not _PAGE_ID.match(page_id):
            raise ValueError(f"Unsafe page id: {page_id!r}")
        prompt = prompt if prompt and prompt.strip() else DEFAULT_PROMPT
        started = time.perf_counter()
        jpeg, side, arguments = self._render(source, page_number)
        evidence = (
            None
            if self.evidence_dir is None
            else self._keep_evidence(source, page_id, page_number, jpeg, arguments, prompt)
        )
        try:
            response = self._post(self._request_body(prompt, jpeg))
        except AdapterFailure as failure:
            nothing = self._result("", None, None, side, evidence, started)
            raise AdapterFailure(
                failure.code, http_status=failure.http_status, partial_result=nothing
            ) from None

        served = _text(response.get("model"))
        if evidence is not None:
            evidence["model"], evidence["provider"] = served, _text(response.get("provider"))
        choices = response.get("choices")
        choice = (
            choices[0]
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else {}
        )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        result = self._result(
            content if isinstance(content, str) else "",
            served,
            response.get("usage"),
            side,
            evidence,
            started,
        )
        reason = choice.get("finish_reason")
        if not isinstance(content, str):
            raise AdapterFailure("MODEL_INVALID_RESPONSE", partial_result=result)
        if reason in _TRUNCATED:
            raise AdapterFailure("MODEL_OUTPUT_TRUNCATED", partial_result=result)
        if reason == "content_filter":
            raise AdapterFailure("MODEL_CONTENT_FILTERED", partial_result=result)
        if reason == "recitation":
            raise AdapterFailure("MODEL_RECITATION", partial_result=result)
        if reason != "stop":
            raise AdapterFailure("MODEL_INVALID_RESPONSE", partial_result=result)
        if not content.strip():
            raise AdapterFailure("MODEL_EMPTY_RESPONSE", partial_result=result)
        return result

    def _render(self, source: Path, page_number: int) -> tuple[bytes, int, list[str]]:
        """Render one page as a JPEG small enough for the request."""
        pdftoppm = _require_binary("pdftoppm")
        with tempfile.TemporaryDirectory(prefix="pageledger-vision-") as directory:
            output = Path(directory) / "page"
            for scale, quality in _RENDER_TRIES:
                side = round(self.max_image_side * scale)
                arguments = [
                    *("-f", str(page_number), "-l", str(page_number), "-singlefile"),
                    *("-scale-to", str(side), "-jpeg", "-jpegopt", f"quality={quality}"),
                ]
                try:
                    _run_ocr_command(
                        [pdftoppm, *arguments, str(source), str(output)],
                        timeout=_RENDER_TIMEOUT_SECONDS,
                        context="pdftoppm could not render the page",
                    )
                    jpeg = output.with_suffix(".jpg").read_bytes()
                except PageLedgerDiagnostic:
                    raise
                except (RuntimeError, OSError):
                    raise AdapterFailure("IMAGE_RENDER_ERROR") from None
                if len(jpeg) <= _JPEG_BUDGET:
                    return jpeg, side, arguments
        raise AdapterFailure("IMAGE_RENDER_ERROR")

    def _keep_evidence(
        self,
        source: Path,
        page_id: str,
        page_number: int,
        jpeg: bytes,
        arguments: list[str],
        prompt: str,
    ) -> dict[str, Any]:
        """Retain the exact JPEG sent, described as image stages require."""
        assert self.evidence_dir is not None
        root, artifact = self.evidence_dir.parent, self.evidence_dir / f"{page_id}.jpg"
        if root.is_symlink() or not root.is_dir() or self.evidence_dir.is_symlink():
            raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
        self.evidence_dir.mkdir(exist_ok=True)
        if artifact.exists() or artifact.is_symlink():
            raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
        atomic_bytes(artifact, jpeg)
        width, height = jpeg_dimensions(jpeg)
        source_sha256 = _read_once(self._source_hashes, source, file_digest)
        evidence = {
            "schema_version": "0.1",
            "kind": "page_image",
            "source_sha256": source_sha256,
            "page_number": page_number,
            "artifact": f"evidence/{page_id}.jpg",
            "sha256": hashlib.sha256(jpeg).hexdigest(),
            "media_type": "image/jpeg",
            "bytes": len(jpeg),
            "width": width,
            "height": height,
            "color_space": "RGB",
            "crop": {"kind": "full_page"},
            "renderer": {
                "command": "pdftoppm",
                "version": _pdftoppm_model_string(),
                "parameters": {"arguments": arguments},
            },
            "requested_model": self.model,
            "model": None,
            "provider": None,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
        validate_input_evidence(
            evidence, root=root, source_sha256=source_sha256, page_number=page_number
        )
        return evidence

    def _request_body(self, prompt: str, jpeg: bytes) -> bytes:
        image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image}},
        ]
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.reasoning_effort is not None:
            request["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("The vision prompt is too long to send with a page image")
        return body

    def _post(self, body: bytes) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.env_key is not None:
            key = os.environ.get(self.env_key)
            if not key:
                raise AdapterFailure("MODEL_UNAVAILABLE")
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, headers=headers
        )
        # Neither a redirect nor a proxy from the environment may carry the key elsewhere.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            code = "MODEL_QUOTA" if error.code in {402, 429} else "MODEL_HTTP_ERROR"
            raise AdapterFailure(code, http_status=error.code) from None
        except urllib.error.URLError as error:
            timed_out = isinstance(error.reason, TimeoutError)
            raise AdapterFailure("MODEL_TIMEOUT" if timed_out else "MODEL_NETWORK_ERROR") from None
        except TimeoutError:
            raise AdapterFailure("MODEL_TIMEOUT") from None
        except (OSError, http.client.HTTPException):
            raise AdapterFailure("MODEL_NETWORK_ERROR") from None
        try:
            value = json.loads(data) if len(data) <= MAX_RESPONSE_BYTES else None
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise AdapterFailure("MODEL_INVALID_RESPONSE")
        return value

    def _result(
        self,
        text: str,
        served: str | None,
        usage: Any,
        side: int,
        evidence: dict[str, Any] | None,
        started: float,
    ) -> ExtractionResult:
        """The page's reading; with evidence, the model is the one the endpoint reported."""
        if evidence is not None:
            model = served
        else:
            name = (
                self.model if served in (None, self.model) else f"{self.model} (served as {served})"
            )
            model = f"{name}; {_pdftoppm_model_string()}; scale-to={side}"
        usage = usage if isinstance(usage, dict) else {}
        tokens, cost = usage.get("total_tokens"), usage.get("cost_usd")
        return ExtractionResult(
            text,
            "text",
            None,
            model,
            [],
            {
                "pages": 1,
                "tokens": tokens if type(tokens) is int and tokens >= 0 else None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": cost
                if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0
                else None,
            },
            input_evidence=evidence,
        )
