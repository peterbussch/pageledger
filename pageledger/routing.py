"""Validation for reviewed route maps supplied to an extraction run."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml


def load_route_map(
    path: Path,
    *,
    inputs: list[Path],
    page_counts: dict[Path, int],
    page_types: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """Load a complete page route map and normalize document source paths."""
    route_path = path.expanduser().resolve()
    if not route_path.is_file():
        raise ValueError(f"Route map does not exist: {path}")
    try:
        loaded = yaml.safe_load(route_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Could not parse route map YAML: {path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Route map must be a YAML mapping")
    if loaded.get("schema_version") != "0.1":
        raise ValueError("Route map schema_version must be '0.1'")
    for field in ("run_id", "generated_at"):
        if not isinstance(loaded.get(field), str) or not loaded[field]:
            raise ValueError(f"Route map {field} must be a non-empty string")
    classifier = loaded.get("classifier")
    if not isinstance(classifier, dict):
        raise ValueError("Route map classifier must be a mapping")
    for field in ("adapter", "model", "prompt_hash"):
        if field not in classifier:
            raise ValueError(f"Route map classifier.{field} is required")
        if classifier.get(field) is not None and not isinstance(classifier[field], str):
            raise ValueError(f"Route map classifier.{field} must be a string or null")

    documents = loaded.get("documents")
    if not isinstance(documents, list):
        raise ValueError("Route map documents must be a list")
    by_source: dict[Path, dict[str, Any]] = {}
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ValueError(f"Route map documents[{index}] must be a mapping")
        source_value = document.get("source")
        if not isinstance(source_value, str) or not source_value:
            raise ValueError(f"Route map documents[{index}].source must be a string")
        source = Path(source_value).expanduser()
        if not source.is_absolute():
            source = route_path.parent / source
        source = source.resolve()
        if source in by_source:
            raise ValueError(f"Route map contains duplicate source: {source}")
        by_source[source] = document

    resolved_inputs = [source.resolve() for source in inputs]
    if set(by_source) != set(resolved_inputs):
        missing = sorted(str(path) for path in set(resolved_inputs) - set(by_source))
        extra = sorted(str(path) for path in set(by_source) - set(resolved_inputs))
        raise ValueError(
            f"Route map sources must exactly match run inputs; missing={missing}, extra={extra}"
        )

    warnings: list[str] = []
    normalized_documents: list[dict[str, Any]] = []
    for document_index, source in enumerate(resolved_inputs, start=1):
        document = by_source[source]
        expected_count = page_counts[source]
        declared_count = document.get("page_count")
        if declared_count is None:
            warnings.append(f"Route map has no page_count for {source}; using {expected_count}")
        elif (
            not isinstance(declared_count, int)
            or isinstance(declared_count, bool)
            or declared_count != expected_count
        ):
            raise ValueError(
                f"Route map page_count for {source} is {declared_count!r}; expected {expected_count}"
            )
        if document.get("source_sha256") is None:
            warnings.append(f"Route map has no source_sha256 for {source}; using current bytes")
        elif not isinstance(document["source_sha256"], str):
            raise ValueError(f"Route map source_sha256 for {source} must be a string")
        pages = document.get("pages")
        if not isinstance(pages, list):
            raise ValueError(f"Route map pages for {source} must be a list")
        expected_numbers = set(range(1, expected_count + 1))
        actual_numbers: set[int] = set()
        normalized_pages: list[dict[str, Any]] = []
        for page_index, page in enumerate(pages):
            prefix = f"Route map {source} pages[{page_index}]"
            if not isinstance(page, dict):
                raise ValueError(f"{prefix} must be a mapping")
            page_number = page.get("page_number")
            if not isinstance(page_number, int) or isinstance(page_number, bool):
                raise ValueError(f"{prefix}.page_number must be an integer")
            if page_number in actual_numbers:
                raise ValueError(f"Route map contains duplicate page {page_number} for {source}")
            actual_numbers.add(page_number)
            expected_id = f"doc_{document_index:04d}_page_{page_number:04d}"
            if page.get("page_id") != expected_id:
                raise ValueError(f"{prefix}.page_id must be '{expected_id}'")
            page_type = page.get("type")
            if not isinstance(page_type, str) or not page_type:
                raise ValueError(f"{prefix}.type must be a non-empty string")
            if page_types and page_type not in page_types:
                raise ValueError(f"{prefix}.type '{page_type}' is not in taxonomy.page_types")
            for field in ("action", "reason"):
                if not isinstance(page.get(field), str) or not page[field]:
                    raise ValueError(f"{prefix}.{field} must be a non-empty string")
            confidence = page.get("confidence")
            if "confidence" not in page:
                raise ValueError(f"{prefix}.confidence is required")
            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ValueError(f"{prefix}.confidence must be between 0 and 1 or null")
            prompt = page.get("prompt")
            if prompt is not None and not isinstance(prompt, str):
                raise ValueError(f"{prefix}.prompt must be a string")
            normalized_pages.append(dict(page))
        if actual_numbers != expected_numbers:
            raise ValueError(
                f"Route map must cover every page of {source}; "
                f"expected 1-{expected_count}, got {sorted(actual_numbers)}"
            )
        normalized_document = dict(document)
        normalized_document["source"] = str(source)
        normalized_document["page_count"] = expected_count
        normalized_document["pages"] = normalized_pages
        normalized_documents.append(normalized_document)

    normalized = dict(loaded)
    normalized["documents"] = normalized_documents
    return normalized, warnings
