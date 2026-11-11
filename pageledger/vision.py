"""The built-in `vision` adapter: page images read by an OpenAI-compatible vision model."""

from __future__ import annotations

import base64
import email.utils
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
    render_warnings,
)
from .checkpoint import atomic_bytes, file_digest
from .image_evidence import (
    MAX_IMAGE_DIMENSION,
    TILE_SEPARATOR,
    jpeg_dimensions,
    validate_input_evidence,
)
from .tiling import plan_sha256, validate_plan

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The JPEG travels base64-encoded, a third larger, inside the JSON request.
_JPEG_BUDGET = (MAX_REQUEST_BYTES - 64 * 1024) * 3 // 4
# A page too large for the budget is rendered smaller and compressed harder.
_RENDER_TRIES = ((1.0, 90), (0.75, 80), (0.5, 70))
_RENDER_TIMEOUT_SECONDS = 120
# Finish reasons as OpenAI-compatible servers report them, and as some gateways
# pass through Gemini's own (MAX_TOKENS, SAFETY, ...), compared in lower case.
_TRUNCATED = {"length", "max_tokens", "max_output_tokens"}
_FILTERED = {"content_filter", "safety", "blocklist", "prohibited_content", "spii"}
_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")
_PAGE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
DEFAULT_PROMPT = (
    "Transcribe this page exactly as printed. Keep the original spelling and "
    "historical letters (such as ѣ, і, ѳ, ѵ and final ъ), line breaks, running "
    "heads and page numbers. Add no commentary and no Markdown fences."
)


# Failures a second request could repeat: they stop a tiled page rather than skip a tile.
_STOPS_A_PAGE = frozenset(
    {"MODEL_QUOTA", "MODEL_TIMEOUT", "MODEL_HTTP_ERROR", "MODEL_NETWORK_ERROR", "MODEL_UNAVAILABLE"}
)


def _reading(response: dict[str, Any]) -> tuple[str, str | None]:
    """A response's text, and the failure its finish state means, if any."""
    choices = response.get("choices")
    choice = (
        choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    )
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    reason = choice.get("finish_reason")
    reason = reason.lower() if isinstance(reason, str) else reason
    if not isinstance(content, str):
        return "", "MODEL_INVALID_RESPONSE"
    if reason in _TRUNCATED:
        return content, "MODEL_OUTPUT_TRUNCATED"
    if reason in _FILTERED:
        return content, "MODEL_CONTENT_FILTERED"
    if reason == "recitation":
        return content, "MODEL_RECITATION"
    if reason != "stop":
        return content, "MODEL_INVALID_RESPONSE"
    if not content.strip():
        return content, "MODEL_EMPTY_RESPONSE"
    return content, None


def _image_record(
    artifact: str, jpeg: bytes, crop: dict[str, Any], arguments: list[str]
) -> dict[str, Any]:
    width, height = jpeg_dimensions(jpeg)
    return {
        "artifact": artifact,
        "sha256": hashlib.sha256(jpeg).hexdigest(),
        "media_type": "image/jpeg",
        "bytes": len(jpeg),
        "width": width,
        "height": height,
        "color_space": "RGB",
        "crop": crop,
        "renderer": {
            "command": "pdftoppm",
            "version": _pdftoppm_model_string(),
            "parameters": {"arguments": arguments},
        },
    }


def _summed(usages: list[Any]) -> dict[str, Any]:
    """A tiled page's usage: each total only when every request reported it."""
    total: dict[str, Any] = {}
    for key in ("total_tokens", "cost_usd"):
        values = [usage.get(key) if isinstance(usage, dict) else None for usage in usages]
        numbers = [v for v in values if type(v) in {int, float} and math.isfinite(float(v or 0))]
        if values and len(numbers) == len(values):
            total[key] = sum(numbers)
    return total


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
        tiles: dict[str, Any] | None = None,
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
        if tiles is not None:
            try:
                validate_plan(tiles)
            except ValueError as exc:
                raise ValueError(f"vision tiles: {exc}") from None
            if evidence_dir is None:
                raise ValueError("vision reads tiles only with an evidence_dir")
            if tiles["settings"]["max_image_side"] != max_image_side:
                raise ValueError("vision tiles were planned for another max_image_side")
        self.tiles = tiles
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.env_key = env_key
        self.max_tokens = max_tokens
        self.timeout_seconds = float(timeout_seconds)
        self.max_image_side = max_image_side
        self.reasoning_effort = reasoning_effort
        self.evidence_dir = None if evidence_dir is None else Path(evidence_dir)
        self.capabilities = (
            "ocr",
            "page_image",
            "generative",
            "page_tiles",
            *(("remote",) if remote else ()),
        )
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
        if self.tiles is not None:
            return self._extract_tiles(source, page_id, page_number, prompt, started)
        jpeg, side, arguments = self._render(source, page_number)
        # -scale-to fits the page's long side to `side` pixels.
        warnings = render_warnings(source, page_number, pixels=(side, side))
        evidence = (
            None
            if self.evidence_dir is None
            else self._keep_evidence(source, page_id, page_number, jpeg, arguments, prompt)
        )
        try:
            response = self._post(self._request_body(prompt, jpeg))
        except AdapterFailure as failure:
            nothing = self._result("", None, None, side, evidence, started, warnings)
            raise AdapterFailure(
                failure.code, http_status=failure.http_status, partial_result=nothing
            ) from None

        served = _text(response.get("model"))
        if evidence is not None:
            evidence["model"], evidence["provider"] = served, _text(response.get("provider"))
        content, code = _reading(response)
        result = self._result(
            content, served, response.get("usage"), side, evidence, started, warnings
        )
        if code is not None:
            raise AdapterFailure(code, partial_result=result)
        return result

    def _extract_tiles(
        self, source: Path, page_id: str, page_number: int, prompt: str, started: float
    ) -> ExtractionResult:
        """Read each planned tile as its own image and request; one reading for the page.

        A tile the model will not read (truncated, filtered, invalid, or empty where the
        layout found text) is marked failed and the others are still read. A failure
        another request could repeat (quota, timeout, network, unavailable) stops the
        page, with the tiles read so far as a partial result.
        """
        assert self.tiles is not None and self.evidence_dir is not None
        root = self.evidence_dir.parent
        if root.is_symlink() or not root.is_dir() or self.evidence_dir.is_symlink():
            raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
        self.evidence_dir.mkdir(exist_ok=True)
        source_sha256 = _read_once(self._source_hashes, source, file_digest)
        evidence: dict[str, Any] = {
            "schema_version": "0.1",
            "kind": "page_tiles",
            "source_sha256": source_sha256,
            "page_number": page_number,
            "requested_model": self.model,
            "model": None,
            "provider": None,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "plan_sha256": plan_sha256(self.tiles),
            "page_box": dict(self.tiles["page_box"]),
            "separator": TILE_SEPARATOR,
            "tiles": [],
        }
        texts: list[str] = []
        usages: list[Any] = []
        dpis: list[float] = []

        def result() -> ExtractionResult:
            sent = evidence["tiles"]
            models = {tile["model"] for tile in sent if tile["model"] is not None}
            providers = {tile["provider"] for tile in sent}
            evidence["model"] = next(iter(models)) if len(models) == 1 else None
            evidence["provider"] = next(iter(providers)) if len(providers) == 1 else None
            validate_input_evidence(
                evidence, root=root, source_sha256=source_sha256, page_number=page_number
            )
            warnings = render_warnings(source, page_number, dpi=max(dpis)) if dpis else []
            return self._result(
                TILE_SEPARATOR.join(texts),
                evidence["model"],
                _summed(usages),
                0,
                evidence,
                started,
                warnings,
            )

        for tile in self.tiles["tiles"]:
            artifact = self.evidence_dir / f"{page_id}-{tile['tile_id']}.jpg"
            try:
                if artifact.exists() or artifact.is_symlink():
                    raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
                jpeg, dpi, arguments, crop = self._render_tile(source, page_number, tile)
            except (AdapterFailure, PageLedgerDiagnostic) as failure:
                if not evidence["tiles"]:
                    raise
                # Tiles already sent were paid for: keep them as the page's partial result.
                code = failure.code if isinstance(failure, AdapterFailure) else "IMAGE_RENDER_ERROR"
                raise AdapterFailure(code, partial_result=result()) from None
            dpis.append(dpi)
            atomic_bytes(artifact, jpeg)
            record = {
                "tile_id": tile["tile_id"],
                "outcome": "failed",
                "failure_code": None,
                **_image_record(f"evidence/{artifact.name}", jpeg, crop, arguments),
                "model": None,
                "provider": None,
                "text_offset": None,
                "text_length": None,
                "text_sha256": None,
            }
            evidence["tiles"].append(record)
            try:
                response = self._post(self._request_body(prompt, jpeg))
            except AdapterFailure as failure:
                usages.append(None)
                record["failure_code"] = failure.code
                if failure.code in _STOPS_A_PAGE:
                    raise AdapterFailure(
                        failure.code, http_status=failure.http_status, partial_result=result()
                    ) from None
                continue
            usages.append(response.get("usage"))
            record["model"] = _text(response.get("model"))
            record["provider"] = _text(response.get("provider"))
            content, failure_code = _reading(response)
            # A tile where the layout found nothing may be blank; without a layout it is unknown.
            if (
                failure_code == "MODEL_EMPTY_RESPONSE"
                and self.tiles["layout"] is not None
                and not tile["layout_items"]
            ):
                failure_code = None
            if failure_code is not None:
                record["failure_code"] = failure_code
                continue
            offset = sum(map(len, texts)) + len(TILE_SEPARATOR) * len(texts)
            texts.append(content)
            record.update(
                outcome="completed",
                text_offset=offset,
                text_length=len(content),
                text_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        if len({tile["model"] for tile in evidence["tiles"] if tile["model"] is not None}) > 1:
            raise AdapterFailure("MODEL_INVALID_RESPONSE", partial_result=result())
        if not any(text.strip() for text in texts):
            failed = [tile["failure_code"] for tile in evidence["tiles"] if tile["failure_code"]]
            raise AdapterFailure(
                failed[0] if failed else "MODEL_EMPTY_RESPONSE", partial_result=result()
            )
        return result()

    def _render_tile(
        self, source: Path, page_number: int, tile: dict[str, Any]
    ) -> tuple[bytes, float, list[str], dict[str, Any]]:
        """Render one planned box as a JPEG, with the crop actually rendered.

        ``pdftoppm`` crops in pixels of the rotated page at the given resolution,
        from its top-left corner. It renders the whole page when a crop starts
        outside the page and clips one that runs past it, so the size is checked.
        """
        pdftoppm = _require_binary("pdftoppm")
        # Two pixels spare: the crop widens to whole pixels on both sides.
        base = (self.max_image_side - 2) * 72 / max(tile["width"], tile["height"])
        if tile["max_dpi"] is not None:
            base = min(base, tile["max_dpi"])
        with tempfile.TemporaryDirectory(prefix="pageledger-vision-") as directory:
            output = Path(directory) / "tile"
            for scale, quality in _RENDER_TRIES:
                dpi = float(f"{base * scale:.4f}")
                x, y = math.floor(tile["x"] * dpi / 72), math.floor(tile["y"] * dpi / 72)
                width = math.ceil((tile["x"] + tile["width"]) * dpi / 72) - x
                height = math.ceil((tile["y"] + tile["height"]) * dpi / 72) - y
                arguments = [
                    *("-f", str(page_number), "-l", str(page_number), "-singlefile"),
                    *("-r", f"{dpi:.4f}", "-x", str(x), "-y", str(y)),
                    *("-W", str(width), "-H", str(height)),
                    *("-jpeg", "-jpegopt", f"quality={quality}"),
                ]
                try:
                    _run_ocr_command(
                        [pdftoppm, *arguments, str(source), str(output)],
                        timeout=_RENDER_TIMEOUT_SECONDS,
                        context="pdftoppm could not render the tile",
                    )
                    jpeg = output.with_suffix(".jpg").read_bytes()
                    rendered = jpeg_dimensions(jpeg)
                except PageLedgerDiagnostic:
                    raise
                except (RuntimeError, OSError, ValueError):
                    raise AdapterFailure("IMAGE_RENDER_ERROR") from None
                # At the page's right or bottom edge the renderer may stop a pixel short.
                if not (width - 1 <= rendered[0] <= width and height - 1 <= rendered[1] <= height):
                    raise AdapterFailure("IMAGE_RENDER_ERROR")
                if len(jpeg) <= _JPEG_BUDGET:
                    crop = {
                        "kind": "box",
                        "units": "pdf_points",
                        "x": round(x * 72 / dpi, 4),
                        "y": round(y * 72 / dpi, 4),
                        "width": round(rendered[0] * 72 / dpi, 4),
                        "height": round(rendered[1] * 72 / dpi, 4),
                    }
                    return jpeg, dpi, arguments, crop
        raise AdapterFailure("IMAGE_RENDER_ERROR")

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
        source_sha256 = _read_once(self._source_hashes, source, file_digest)
        evidence = {
            "schema_version": "0.1",
            "kind": "page_image",
            "source_sha256": source_sha256,
            "page_number": page_number,
            **_image_record(f"evidence/{page_id}.jpg", jpeg, {"kind": "full_page"}, arguments),
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
        deadline = time.monotonic() + self.timeout_seconds
        for attempt in range(3):
            try:
                with opener.open(
                    request, timeout=max(0.001, deadline - time.monotonic())
                ) as response:
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except urllib.error.HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                delay = float(2**attempt)
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = (
                                email.utils.parsedate_to_datetime(retry_after).timestamp()
                                - time.time()
                            )
                        except (ValueError, TypeError, OverflowError):
                            pass
                    if not math.isfinite(delay):
                        delay = float(2**attempt)
                    delay = max(0.0, delay)
                if status == 429 and attempt < 2 and delay < min(60, deadline - time.monotonic()):
                    time.sleep(delay)
                    continue
                code = "MODEL_QUOTA" if status in {402, 429} else "MODEL_HTTP_ERROR"
                raise AdapterFailure(code, http_status=status) from None
            except urllib.error.URLError as error:
                timed_out = isinstance(error.reason, TimeoutError)
                raise AdapterFailure(
                    "MODEL_TIMEOUT" if timed_out else "MODEL_NETWORK_ERROR"
                ) from None
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
        warnings: list[str],
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
            warnings,
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
