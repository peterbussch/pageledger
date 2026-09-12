"""Optional bounded Gemini/DeepSeek image adapter for an OpenAI-compatible gateway.

Requires Poppler and Pillow outside PageLedger core. Authentication comes only
from the explicitly named environment variable. See docs/image-evidence-spec.md.
No constructor/page-count network calls, request retries, or provider fallback.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from pageledger.adapters import AdapterFailure, ExtractionResult, pdf_page_count
from pageledger.checkpoint import atomic_bytes, file_digest
from pageledger.image_evidence import MAX_IMAGE_BYTES, MAX_IMAGE_DIMENSION, validate_input_evidence

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_JPEG_BUDGET = (MAX_REQUEST_BYTES - 64 * 1024) * 3 // 4


def _allowed_family(model: str) -> bool:
    name = model.lower().rsplit("/", 1)[-1]
    return name.startswith(("gemini-", "gemini_", "deepseek-", "deepseek_"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAIImageAdapter:
    name = "openai_image_example"
    version = "0.1"
    deterministic = False
    input_types = ("pdf",)
    output_types = ("text",)
    capabilities = ("ocr", "page_image", "remote")

    def __init__(self, *, model: str, evidence_dir: str,
                 base_url: str = "http://127.0.0.1:20128/v1",
                 env_key: str | None = "OMNIROUTE_API_KEY", max_output_tokens: int = 8192,
                 timeout: float = 120, renderer: str = "pdftoppm", dpi: int = 150,
                 allowed_models: list[str] | None = None, system_prompt: str | None = None,
                 max_image_bytes: int = MAX_IMAGE_BYTES,
                 max_image_dimension: int = MAX_IMAGE_DIMENSION):
        if not isinstance(model, str) or not _allowed_family(model):
            raise ValueError("model must explicitly name a Gemini or DeepSeek model")
        if allowed_models is not None and (not isinstance(allowed_models, list)
                or not all(isinstance(item, str) and _allowed_family(item) for item in allowed_models)
                or model not in allowed_models):
            raise ValueError("model must be in the explicit Gemini/DeepSeek allowed_models list")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 131072:
            raise ValueError("max_output_tokens must be 1..131072")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError("timeout must be a finite number in (0, 600]")
        if type(dpi) is not int or not 50 <= dpi <= 300:
            raise ValueError("dpi must be 50..300")
        if type(max_image_bytes) is not int or not 1 <= max_image_bytes <= MAX_IMAGE_BYTES:
            raise ValueError(f"max_image_bytes must be 1..{MAX_IMAGE_BYTES}")
        if type(max_image_dimension) is not int or not 1 <= max_image_dimension <= MAX_IMAGE_DIMENSION:
            raise ValueError(f"max_image_dimension must be 1..{MAX_IMAGE_DIMENSION}")
        if renderer not in {"pdftoppm", "pdftocairo"}:
            raise ValueError("renderer must be pdftoppm or pdftocairo")
        if env_key is not None and (not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key)):
            raise ValueError("env_key must be an environment variable name or null")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ValueError("system_prompt must be a string or null")
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an HTTP(S) URL without credentials, query, or fragment")
        directory = Path(evidence_dir)
        if not directory.is_absolute() or directory.name != "evidence" or ".." in directory.parts:
            raise ValueError("evidence_dir must be the absolute child run root/evidence directory")
        self.model, self.evidence_dir = model, directory
        self.base_url, self.env_key = base_url.rstrip("/"), env_key
        self.max_output_tokens, self.timeout = max_output_tokens, timeout
        self.renderer, self.dpi, self.system_prompt = renderer, dpi, system_prompt
        self.max_image_bytes = max_image_bytes
        self.max_image_dimension = max_image_dimension
        self._checked_models = False
        self._renderer_version = None

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def page_count(self, source: Path) -> int:
        return pdf_page_count(source)

    def _request(self, path: str, payload: bytes | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.env_key is not None:
            key = os.environ.get(self.env_key)
            if not key:
                raise AdapterFailure("MODEL_UNAVAILABLE")
            headers["Authorization"] = f"Bearer {key}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if len(payload) > MAX_REQUEST_BYTES:
                raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
        request = urllib.request.Request(self.base_url + path, data=payload, headers=headers)
        # Redirects and environment proxies must not forward authentication elsewhere.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise AdapterFailure("MODEL_QUOTA" if exc.code in {402, 429} else "MODEL_HTTP_ERROR",
                                 http_status=exc.code) from None
        except (TimeoutError, subprocess.TimeoutExpired):
            raise AdapterFailure("MODEL_TIMEOUT") from None
        except urllib.error.URLError as exc:
            raise AdapterFailure("MODEL_TIMEOUT" if isinstance(exc.reason, TimeoutError)
                                 else "MODEL_NETWORK_ERROR") from None
        except OSError:
            raise AdapterFailure("MODEL_NETWORK_ERROR") from None
        try:
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("response limit")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return value
        except (ValueError, UnicodeError):
            raise AdapterFailure("MODEL_INVALID_RESPONSE") from None

    def _check_model(self) -> None:
        if self._checked_models:
            return
        response = self._request("/models")
        models = response.get("data")
        if not isinstance(models, list):
            raise AdapterFailure("MODEL_INVALID_RESPONSE")
        if self.model not in {item.get("id") for item in models if isinstance(item, dict)
                              and isinstance(item.get("id"), str)}:
            raise AdapterFailure("MODEL_UNAVAILABLE")
        self._checked_models = True

    def _render(self, source: Path, page_id: str, page_number: int, prompt: str) -> dict[str, Any]:
        try:
            from PIL import Image
            command = shutil.which(self.renderer)
            if command is None:
                raise ValueError("renderer unavailable")
            if self._renderer_version is None:
                version = subprocess.run([command, "-v"], capture_output=True, timeout=10, check=True)
                self._renderer_version = (version.stderr or version.stdout).decode(errors="replace").splitlines()[0]
            root = self.evidence_dir.parent
            if root.is_symlink() or self.evidence_dir.is_symlink() or not root.is_dir():
                raise ValueError("unsafe evidence root")
            self.evidence_dir.mkdir(exist_ok=True)
            artifact = self.evidence_dir / f"{page_id}.jpg"
            if artifact.exists() or artifact.is_symlink():
                raise ValueError("evidence already exists")
            source_sha256 = file_digest(source)
            with tempfile.TemporaryDirectory(prefix="pageledger-image-") as directory:
                prefix = str(Path(directory) / "page")
                argv = [command, "-f", str(page_number), "-l", str(page_number), "-singlefile",
                        "-r", str(self.dpi), "-scale-to", str(self.max_image_dimension), "-jpeg",
                        str(source), prefix]
                subprocess.run(argv, capture_output=True, timeout=self.timeout, check=True)
                with Image.open(prefix + ".jpg") as rendered:
                    image = rendered.convert("RGB")
                    image.thumbnail((self.max_image_dimension, self.max_image_dimension))
                    for quality in (90, 80, 70, 60):
                        buffer = io.BytesIO()
                        image.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=2)
                        jpeg = buffer.getvalue()
                        if len(jpeg) <= min(_JPEG_BUDGET, self.max_image_bytes):
                            break
                    else:
                        raise ValueError("image byte limit")
                    width, height = image.size
            if file_digest(source) != source_sha256:
                raise ValueError("source changed while rendering")
            atomic_bytes(artifact, jpeg)
            evidence = {
                "schema_version": "0.1", "kind": "page_image", "source_sha256": source_sha256,
                "page_number": page_number, "artifact": f"evidence/{page_id}.jpg",
                "sha256": hashlib.sha256(jpeg).hexdigest(), "media_type": "image/jpeg",
                "bytes": len(jpeg), "width": width, "height": height, "color_space": "RGB",
                "crop": {"kind": "full_page"},
                "renderer": {"command": command, "version": self._renderer_version,
                             "parameters": {"argv": argv[1:], "dpi": self.dpi,
                                            "scale_to": self.max_image_dimension,
                                            "jpeg_quality": quality, "subsampling": 2,
                                            "max_image_bytes": self.max_image_bytes,
                                            "optimize": True, "pillow_version": Image.__version__}},
                "requested_model": self.model, "model": None, "provider": None,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
            if self.system_prompt is not None:
                evidence["system_prompt_sha256"] = hashlib.sha256(self.system_prompt.encode()).hexdigest()
            validate_input_evidence(evidence, root=root, source_sha256=source_sha256,
                                    page_number=page_number)
            return evidence
        except (ImportError, OSError, ValueError, IndexError, subprocess.SubprocessError):
            raise AdapterFailure("IMAGE_RENDER_ERROR") from None

    def extract(self, source: Path, *, page_id: str, page_number: int,
                action: str, prompt: str | None = None) -> ExtractionResult:
        if not self.supports(action):
            raise ValueError("Image adapter only supports transcribe_text")
        if not isinstance(page_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", page_id):
            raise ValueError("Invalid image page identity")
        if type(page_number) is not int or page_number < 1:
            raise ValueError("Invalid image page number")
        if prompt is not None and not isinstance(prompt, str):
            raise ValueError("prompt must be a string or null")
        self._check_model()
        prompt = prompt or ""
        started = time.perf_counter()
        evidence = self._render(source, page_id, page_number, prompt)
        from pageledger.image_evidence import read_image_artifact
        jpeg = read_image_artifact(self.evidence_dir.parent, evidence["artifact"])
        if hashlib.sha256(jpeg).hexdigest() != evidence["sha256"]:
            raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
        messages = []
        if self.system_prompt is not None:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}
        ]})
        payload = json.dumps({"model": self.model, "messages": messages,
                              "max_tokens": self.max_output_tokens, "stream": False},
                             ensure_ascii=False, allow_nan=False).encode()
        empty = ExtractionResult(content="", format="text", confidence=None, model=None,
                                 warnings=[], usage={"pages": 1, "tokens": None,
                                                     "compute_seconds": None, "cost_usd": None},
                                 input_evidence=evidence)
        try:
            if len(payload) > MAX_REQUEST_BYTES:
                raise AdapterFailure("IMAGE_EVIDENCE_INVALID")
            validate_input_evidence(evidence, root=self.evidence_dir.parent,
                                    source_sha256=file_digest(source), page_number=page_number,
                                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
            response = self._request("/chat/completions", payload)
        except AdapterFailure as exc:
            raise AdapterFailure(exc.code, http_status=exc.http_status, partial_result=empty) from None
        except ValueError:
            raise AdapterFailure("IMAGE_EVIDENCE_INVALID", partial_result=empty) from None
        model = response.get("model")
        provider = response.get("provider")
        evidence["model"] = model if isinstance(model, str) and model.strip() else None
        evidence["provider"] = provider if isinstance(provider, str) and provider.strip() else None
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        usage = response.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        cost = usage.get("cost_usd") if isinstance(usage, dict) else None
        tokens = tokens if type(tokens) is int and tokens >= 0 else None
        cost = cost if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0 else None
        result = ExtractionResult(
            content=content if isinstance(content, str) else "", format="text", confidence=None,
            model=evidence["model"], warnings=[], input_evidence=evidence,
            usage={"pages": 1, "tokens": tokens, "cost_usd": cost,
                   "compute_seconds": round(time.perf_counter() - started, 3)},
        )
        if not isinstance(content, str) or not choice:
            raise AdapterFailure("MODEL_INVALID_RESPONSE", partial_result=result)
        if choice.get("finish_reason") in {"length", "max_tokens", "max_output_tokens"}:
            raise AdapterFailure("MODEL_OUTPUT_TRUNCATED", partial_result=result)
        if choice.get("finish_reason") != "stop":
            raise AdapterFailure("MODEL_INVALID_RESPONSE", partial_result=result)
        if not content.strip():
            raise AdapterFailure("MODEL_EMPTY_RESPONSE", partial_result=result)
        if evidence["model"] is not None and not _allowed_family(evidence["model"]):
            raise AdapterFailure("MODEL_INVALID_RESPONSE", partial_result=result)
        try:
            validate_input_evidence(evidence, root=self.evidence_dir.parent,
                                    source_sha256=file_digest(source), page_number=page_number)
        except (OSError, ValueError):
            raise AdapterFailure("IMAGE_EVIDENCE_INVALID", partial_result=result) from None
        return result
