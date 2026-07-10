"""Example PageLedger adapter skeleton for cloud/VLM OCR.

This file intentionally avoids reading or printing secret values. Real projects
should keep provider-specific clients and model names in their own codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pageledger.adapters import ExtractionResult, pdf_page_count


@dataclass(frozen=True)
class CloudVlmAdapter:
    env_key: str = "OPENAI_API_KEY"
    name: ClassVar[str] = "cloud-vlm-example"
    version: ClassVar[str] = "example"
    deterministic: ClassVar[bool] = False
    input_types: ClassVar[tuple[str, ...]] = ("pdf", "image")
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("ocr", "cloud")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

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
        if not self.supports(action):
            raise ValueError(f"{self.name} does not support action: {action}")
        if not os.environ.get(self.env_key):
            raise RuntimeError(f"{self.env_key} is not set")
        _ = source, page_id, page_number, action, prompt
        raise NotImplementedError("Call your provider here and return an ExtractionResult")
