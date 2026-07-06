"""Example PageLedger adapter skeleton for cloud/VLM OCR.

This file intentionally avoids reading or printing secret values. Real projects
should keep provider-specific clients and model names in their own codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pageledger.adapters import ExtractionResult, pdf_page_count


def _env_status(name: str) -> dict[str, object]:
    return {"name": name, "set": bool(os.environ.get(name)), "value": "<redacted>"}


@dataclass(frozen=True)
class CloudVlmAdapter:
    name: str = "cloud-vlm-example"
    version: str = "example"
    deterministic: bool = False
    input_types: tuple[str, ...] = ("pdf", "image")
    output_types: tuple[str, ...] = ("text", "markdown", "json")
    capabilities: tuple[str, ...] = ("ocr", "layout", "cloud")
    env_key: str = "OPENAI_API_KEY"

    def supports(self, action: str) -> bool:
        return action in {"transcribe_text", "vlm_table"}

    def page_count(self, source: Path) -> int:
        if source.suffix.lower() == ".pdf":
            return pdf_page_count(source)
        return 1

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        if not os.environ.get(self.env_key):
            raise RuntimeError(f"{self.env_key} is not set; env status: {_env_status(self.env_key)}")
        _ = source, page_id, page_number, action, prompt
        raise NotImplementedError("Call your provider here and return an ExtractionResult")
