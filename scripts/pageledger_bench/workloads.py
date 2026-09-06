"""Frozen synthetic workloads for ledger-only PageLedger measurements."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pageledger.adapters import ExtractionResult

GENERATOR_VERSION = "1.0.0"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = Path(__file__).with_name("benchmark_manifest.json")
_CATEGORY_COUNTS = {
    "primary": {
        "structured": 1_600,
        "noisy": 1_200,
        "historical-multiscript": 1_200,
        "clean-control": 1_000,
    },
    "generalization": {
        "structured": 300,
        "noisy": 250,
        "historical-multiscript": 250,
        "clean-control": 200,
    },
}
_NORMALIZED_CANONICAL_SHA256 = {
    "primary": "27456a42ffdca57c30b3a13b7daf183fc315911d7a3d5444fc2d9b98a9f34a66",
    "generalization": "fab1dfd53e08ffc03f4500a2aa0ec65c0b8af15bff2acc71a2bb83fe7d9fc400",
}


@dataclass(frozen=True)
class PageSpec:
    """One deterministic adapter result and its provenance category."""

    content: str | dict[str, Any] | list[dict[str, Any]]
    format: Literal["text", "markdown", "json", "csv", "markdown_table"]
    confidence: float | None
    warnings: tuple[str, ...]
    tokens: int | None
    cost_usd: float | None
    category: str


@dataclass(frozen=True)
class WorkloadSpec:
    """Materialized frozen recipe, paths, and expected aggregate receipts."""

    name: str
    page_specs: tuple[PageSpec, ...]
    source_path: Path
    config_path: Path
    membership_path: Path
    membership: tuple[dict[str, Any], ...]
    generated_paths: dict[str, Path]
    expected: dict[str, Any]


@dataclass(frozen=True)
class _FrozenResult:
    """Immutable adapter template; public result containers are recreated per call."""

    content_encoding: Literal["text", "json"]
    content_payload: str
    format: Literal["text", "markdown", "json", "csv", "markdown_table"]
    confidence: float | None
    warnings: tuple[str, ...]
    tokens: int | None
    cost_usd: float | None

    @classmethod
    def from_page_spec(cls, page: PageSpec) -> _FrozenResult:
        if isinstance(page.content, str):
            encoding: Literal["text", "json"] = "text"
            payload = page.content
        else:
            encoding = "json"
            payload = json.dumps(page.content, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return cls(
            content_encoding=encoding,
            content_payload=payload,
            format=page.format,
            confidence=page.confidence,
            warnings=page.warnings,
            tokens=page.tokens,
            cost_usd=page.cost_usd,
        )

    def to_extraction_result(self) -> ExtractionResult:
        content: str | dict[str, Any] | list[dict[str, Any]]
        if self.content_encoding == "text":
            content = self.content_payload
        else:
            content = json.loads(self.content_payload)
        return ExtractionResult(
            content=content,
            format=self.format,
            confidence=self.confidence,
            model="pageledger-zero-work-v1",
            warnings=list(self.warnings),
            usage={
                "pages": 1,
                "tokens": self.tokens,
                "compute_seconds": 0.0,
                "cost_usd": self.cost_usd,
            },
        )


class ZeroWorkAdapter:
    """A deterministic adapter that only indexes preloaded result templates."""

    name = "zero-work"
    version = "1.0.0"
    deterministic = True
    input_types = ("text",)
    output_types = ("text", "markdown", "json", "csv", "markdown_table")
    capabilities = ("benchmark", "local", "preloaded-results")

    def __init__(self, page_specs: tuple[PageSpec, ...]) -> None:
        self._results = tuple(_FrozenResult.from_page_spec(page) for page in page_specs)

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def reproducibility_profile(self) -> dict[str, object]:
        return {"materials": []}

    def page_count(self, source: Path) -> int:
        _ = source
        return len(self._results)

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        _ = source, page_id, prompt
        if not self.supports(action):
            raise ValueError(f"ZeroWorkAdapter does not support action: {action}")
        if page_number < 1 or page_number > len(self._results):
            raise ValueError(f"page_number {page_number} is outside the frozen workload")
        return self._results[page_number - 1].to_extraction_result()


def sha256_path(path: Path) -> str:
    """Return the stable SHA-256 hash of one regular generated file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_frozen_manifest() -> dict[str, Any]:
    """Load the checked-in manifest without normalizing its frozen values."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def generate_workload(name: str, root: Path) -> WorkloadSpec:
    """Write one exact recipe below ``root`` and verify all frozen hashes."""
    manifest = load_frozen_manifest()
    if name not in _CATEGORY_COUNTS:
        raise ValueError(f"Unknown frozen workload: {name}")
    if manifest["generator"]["version"] != GENERATOR_VERSION:
        raise ValueError("benchmark manifest generator version does not match the recipe")

    page_specs = _page_specs(name)
    membership = tuple(_membership_entry(index, page) for index, page in enumerate(page_specs, 1))
    expected = _expected_aggregates(name)
    workload_root = root / name
    workload_root.mkdir(parents=True, exist_ok=True)
    source_path = workload_root / "source.txt"
    config_path = workload_root / "pageledger.yml"
    membership_path = workload_root / "membership.json"
    source_path.write_text(_source_text(membership), encoding="utf-8")
    config_path.write_text(_config_text(), encoding="utf-8")
    membership_path.write_text(
        json.dumps(
            {
                "generator_version": GENERATOR_VERSION,
                "workload": name,
                "pages": membership,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    generated_paths = {
        "source.txt": source_path,
        "pageledger.yml": config_path,
        "membership.json": membership_path,
    }
    _verify_manifest(manifest, name, page_specs, expected, generated_paths)
    return WorkloadSpec(
        name=name,
        page_specs=page_specs,
        source_path=source_path,
        config_path=config_path,
        membership_path=membership_path,
        membership=membership,
        generated_paths=generated_paths,
        expected=expected,
    )


def _page_specs(name: str) -> tuple[PageSpec, ...]:
    pages: list[PageSpec] = []
    for category, count in _CATEGORY_COUNTS[name].items():
        for ordinal in range(1, count + 1):
            page_number = len(pages) + 1
            pages.append(_page_spec(category, ordinal, page_number))
    return tuple(pages)


def _page_spec(category: str, ordinal: int, page_number: int) -> PageSpec:
    if category == "structured":
        variant = ordinal % 3
        if variant == 1:
            content: str | dict[str, Any] | list[dict[str, Any]] = {
                "amount": page_number,
                "expected_amount": page_number,
                "record": f"structured-{ordinal:05d}",
            }
            result_format: Literal["json", "csv", "markdown_table"] = "json"
        elif variant == 2:
            content = (
                "record,amount,expected_amount\n"
                f"structured-{ordinal:05d},not-an-integer,{page_number}\n"
            )
            result_format = "csv"
        else:
            content = (
                "| record | amount | expected_amount |\n|---|---:|---:|\n"
                f"| structured-{ordinal:05d} | {page_number + 1} | {page_number} |\n"
            )
            result_format = "markdown_table"
        return PageSpec(
            content=content,
            format=result_format,
            confidence=0.99,
            warnings=(),
            tokens=32,
            cost_usd=0.0004,
            category=category,
        )
    if category == "noisy":
        return PageSpec(
            content=(
                f"Noisy OCR receipt {ordinal:05d}: � characters survived extraction."
            ),
            format="text",
            confidence=0.42,
            warnings=("adapter_low_confidence",),
            tokens=17,
            cost_usd=0.0002,
            category=category,
        )
    if category == "historical-multiscript":
        return PageSpec(
            content=(
                f"Въ лѣто {ordinal:05d} года: мѣсяц مخطوطة — पाठ — 旧紀録."
            ),
            format="markdown",
            confidence=0.91,
            warnings=(),
            tokens=25,
            cost_usd=0.0003,
            category=category,
        )
    if category == "clean-control":
        return PageSpec(
            content=(
                f"Clean control page {ordinal:05d} contains ordinary readable prose for ledger work."
            ),
            format="text",
            confidence=0.99,
            warnings=(),
            tokens=20,
            cost_usd=0.0001,
            category=category,
        )
    raise AssertionError(f"Unrecognized frozen category: {category}")


def _membership_entry(index: int, page: PageSpec) -> dict[str, Any]:
    return {
        "category": page.category,
        "confidence": page.confidence,
        "content": page.content,
        "cost_usd": page.cost_usd,
        "format": page.format,
        "page_number": index,
        "tokens": page.tokens,
        "warnings": list(page.warnings),
    }


def _source_text(membership: tuple[dict[str, Any], ...]) -> str:
    return "\f".join(
        f"[{page['category']}] deterministic source page {page['page_number']:05d}"
        for page in membership
    ) + "\n"


def _config_text() -> str:
    return """schema_version: \"0.1\"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
schema:
  name: ledger_benchmark_record
  columns:
    - {name: record, type: string, required: true}
    - {name: amount, type: integer, required: true}
    - {name: expected_amount, type: integer, required: true}
  checks:
    - {name: amount_matches_expected, expression: amount == expected_amount}
  quality:
    minimum_required_column_coverage: 1.0
run:
  adapter: zero-work
  grading:
    review_below_grade: C
  rerun_if:
    - arithmetic_failure_rate_above: 0
"""


def _expected_aggregates(name: str) -> dict[str, Any]:
    counts = _CATEGORY_COUNTS[name]
    structured = counts["structured"]
    noisy = counts["noisy"]
    historical = counts["historical-multiscript"]
    clean = counts["clean-control"]
    format_counts = {
        "json": (structured + 2) // 3,
        "csv": (structured + 1) // 3,
        "markdown_table": structured // 3,
        "text": noisy + clean,
        "markdown": historical,
    }
    tokens = structured * 32 + noisy * 17 + historical * 25 + clean * 20
    cost = round(
        structured * 0.0004 + noisy * 0.0002 + historical * 0.0003 + clean * 0.0001,
        12,
    )
    warning_pages = noisy + historical
    structured_json = (structured + 2) // 3
    structured_csv = (structured + 1) // 3
    structured_markdown = structured // 3
    schema_aware = {
        "A": structured_json,
        "B": structured_csv,
        "C": 0,
        "D": structured_markdown,
        "F": 0,
    }
    signals_only = {
        "A": clean,
        "B": historical,
        "C": 0,
        "D": 0,
        "F": noisy,
    }
    return {
        "output": {
            "format_counts": format_counts,
            "pages_extracted": sum(counts.values()),
            "raw_extension_counts": {
                "csv": format_counts["csv"],
                "json": format_counts["json"],
                "markdown": format_counts["markdown"],
                "markdown_table": format_counts["markdown_table"],
                "txt": format_counts["text"],
            },
        },
        "quality": {"warning_pages": warning_pages},
        "grades": {
            "schema_aware": schema_aware,
            "signals_only": signals_only,
        },
        "audit": {
            "review_queue_by_reason": {
                "grade_below_threshold": noisy + structured_markdown,
                "quality_warning": warning_pages,
                "rerun_if:arithmetic_failure_rate_above": structured_markdown,
            },
            "review_queue_items": warning_pages + noisy + 2 * structured_markdown,
            "rerun_items": warning_pages + structured_markdown,
        },
        "cost": {
            "basis": "adapter_reported",
            "cost_known": True,
            "cost_usd": cost,
            "tokens_total": tokens,
        },
        "normalized": {
            "canonical_sha256": _NORMALIZED_CANONICAL_SHA256[name],
            "checks": {
                "amount_matches_expected": {
                    "rows_checked": structured_json + structured_markdown,
                    "rows_passed": structured_json,
                    "rows_failed": structured_markdown,
                    "rows_unchecked": structured_csv,
                }
            },
            "coercion_errors": structured_csv,
            "files": structured,
            "grade_basis_counts": {
                "schema_aware": structured,
                "signals_only": noisy + historical + clean,
            },
            "records_normalized": structured,
            "schema_name": "ledger_benchmark_record",
            "source_format_counts": {
                "csv": structured_csv,
                "json": structured_json,
                "markdown_table": structured_markdown,
            },
        },
    }


def _verify_manifest(
    manifest: dict[str, Any],
    name: str,
    page_specs: tuple[PageSpec, ...],
    expected: dict[str, Any],
    generated_paths: dict[str, Path],
) -> None:
    frozen = manifest["workloads"][name]
    if frozen["page_count"] != len(page_specs):
        raise ValueError(f"{name} page count does not match the frozen manifest")
    if frozen["category_counts"] != _CATEGORY_COUNTS[name]:
        raise ValueError(f"{name} category counts do not match the frozen manifest")
    if frozen["expected"] != expected:
        raise ValueError(f"{name} expected aggregates do not match the frozen manifest")
    for filename, path in generated_paths.items():
        if sha256_path(path) != frozen["hashes"][filename]:
            raise ValueError(f"{name} generated {filename} does not match its frozen SHA-256")
    for filename, expected_sha256 in manifest["schema_sha256"].items():
        if sha256_path(_REPOSITORY_ROOT / "schemas" / filename) != expected_sha256:
            raise ValueError(f"schema {filename} does not match its frozen SHA-256")
    for relative_path, expected_sha256 in manifest["recipe_sha256"].items():
        if sha256_path(_REPOSITORY_ROOT / relative_path) != expected_sha256:
            raise ValueError(f"recipe {relative_path} does not match its frozen SHA-256")
