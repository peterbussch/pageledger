"""Structural page classification and route-map emission."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .adapters import _import_object, adapter_page_count, load_adapter
from .artifacts import build_route_map, read_jsonl, write_jsonl, write_yaml
from .config import PageLedgerConfig, load_config
from .quality import _alphabetic_token_lengths, _quality_text, _text_quality_metrics
from .runner import (
    _apply_adapter_path,
    _expand_inputs,
    _sha256_path,
    _utc_now,
    _utc_now_compact,
    _validate_extraction_result,
)

STRUCTURAL_PAGE_TYPES = ("blank", "sparse", "prose", "table_likely", "unknown")
STRUCTURED_RESULT_FORMATS = frozenset({"json", "csv", "markdown_table"})

DEFAULT_CLASSIFY_THRESHOLDS: dict[str, int | float] = {
    "blank_max_characters": 2,
    "sparse_max_words": 25,
    "table_pipe_line_ratio": 0.3,
    "table_min_lines": 3,
    "table_column_line_ratio": 0.015,
    "table_digit_ratio": 0.25,
    "fragmented_mean_token_length": 3.0,
    "fragmented_min_alpha_tokens": 20,
    "joined_mean_token_length": 10.0,
    "joined_max_token_length": 80,
    "joined_min_alpha_tokens": 20,
    "joined_max_whitespace_ratio": 0.03,
    "joined_min_latin_letter_ratio": 0.8,
    "low_confidence_below_60_ratio": 0.25,
    "low_confidence_penalty": 0.2,
}

_INTEGER_THRESHOLDS = frozenset(
    {
        "blank_max_characters",
        "sparse_max_words",
        "table_min_lines",
        "fragmented_min_alpha_tokens",
        "joined_max_token_length",
        "joined_min_alpha_tokens",
    }
)
_RATIO_THRESHOLDS = frozenset(
    {
        "table_pipe_line_ratio",
        "table_column_line_ratio",
        "table_digit_ratio",
        "low_confidence_below_60_ratio",
        "low_confidence_penalty",
        "joined_max_whitespace_ratio",
        "joined_min_latin_letter_ratio",
    }
)
_COLUMN_RUN = re.compile(r"\S {2,}\S")


@dataclass(frozen=True)
class ClassificationResult:
    type: str
    confidence: float | None
    reason: str
    action: str | None = None
    prompt: str | None = None


def merge_classify_thresholds(value: Any) -> dict[str, int | float]:
    """Validate user overrides and merge them over the structural defaults."""
    if value is None:
        return dict(DEFAULT_CLASSIFY_THRESHOLDS)
    if not isinstance(value, dict):
        raise ValueError("classify.thresholds must be a mapping")
    unknown = sorted(set(value) - set(DEFAULT_CLASSIFY_THRESHOLDS))
    if unknown:
        raise ValueError(f"Unknown classify.thresholds keys: {', '.join(unknown)}")
    merged = dict(DEFAULT_CLASSIFY_THRESHOLDS)
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"classify.thresholds.{key} must be a number")
        if not math.isfinite(raw) or raw < 0:
            raise ValueError(f"classify.thresholds.{key} must be a finite non-negative number")
        if key in _INTEGER_THRESHOLDS and not isinstance(raw, int):
            raise ValueError(f"classify.thresholds.{key} must be an integer")
        if key in _RATIO_THRESHOLDS and raw > 1:
            raise ValueError(f"classify.thresholds.{key} must be between 0 and 1")
        merged[key] = raw
    return merged


def structural_signals(
    text: str,
    *,
    result_format: str | None,
    confidence_detail: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build language-neutral evidence used by the built-in rule cascade."""
    token_lengths = _alphabetic_token_lengths(text)
    character_count = len(text)
    metrics = _text_quality_metrics(
        text,
        character_count=character_count,
        token_lengths=token_lengths,
    )
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    nonempty_count = len(nonempty_lines)
    visible_character_count = sum(1 for char in text if not char.isspace())
    below_60_ratio = None
    if isinstance(confidence_detail, dict):
        candidate = confidence_detail.get("below_60_ratio")
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            if not math.isfinite(candidate) or not 0 <= candidate <= 1:
                raise ValueError(
                    "confidence_detail.below_60_ratio must be between 0 and 1"
                )
            below_60_ratio = float(candidate)
    return {
        "result_format": result_format,
        "character_count": character_count,
        "visible_character_count": visible_character_count,
        "word_count": len(token_lengths),
        **metrics,
        "pipe_line_ratio": (
            0.0
            if nonempty_count == 0
            else round(sum("|" in line for line in nonempty_lines) / nonempty_count, 4)
        ),
        "column_line_ratio": (
            0.0
            if nonempty_count == 0
            else round(
                sum(bool(_COLUMN_RUN.search(line)) for line in nonempty_lines) / nonempty_count,
                4,
            )
        ),
        "digit_ratio": (
            0.0
            if visible_character_count == 0
            else round(sum(char.isdigit() for char in text) / visible_character_count, 4)
        ),
        "nonempty_line_count": nonempty_count,
        "below_60_ratio": below_60_ratio,
    }


def classify_signals(
    signals: dict[str, Any],
    thresholds: dict[str, int | float],
    *,
    pdf_embedded_text_probe: bool = False,
) -> ClassificationResult:
    """Classify structural evidence with a documented first-match cascade."""
    characters = int(signals["visible_character_count"])
    lines = int(signals["nonempty_line_count"])
    if characters <= thresholds["blank_max_characters"]:
        if pdf_embedded_text_probe:
            result = ClassificationResult("unknown", None, "empty_pdf_text_ambiguous")
        else:
            result = ClassificationResult("blank", 0.95, "blank_text")
    elif signals.get("result_format") in STRUCTURED_RESULT_FORMATS:
        result_format = signals["result_format"]
        result = ClassificationResult(
            "table_likely", 0.85, f"structured_payload:{result_format}"
        )
    elif (
        signals["mean_token_length"] is not None
        and signals["mean_token_length"] >= thresholds["joined_mean_token_length"]
        and signals["max_token_length"] >= thresholds["joined_max_token_length"]
        and signals["alpha_token_count"] >= thresholds["joined_min_alpha_tokens"]
        and signals["whitespace_character_ratio"]
        <= thresholds["joined_max_whitespace_ratio"]
        and signals["latin_letter_ratio"]
        >= thresholds["joined_min_latin_letter_ratio"]
    ):
        result = ClassificationResult("unknown", None, "joined_text")
    elif (
        lines >= thresholds["table_min_lines"]
        and signals["pipe_line_ratio"] >= thresholds["table_pipe_line_ratio"]
    ):
        result = ClassificationResult("table_likely", 0.75, "pipe_line_density")
    elif (
        lines >= thresholds["table_min_lines"]
        and signals["column_line_ratio"] >= thresholds["table_column_line_ratio"]
        and signals["digit_ratio"] >= thresholds["table_digit_ratio"]
    ):
        result = ClassificationResult("table_likely", 0.6, "column_digit_density")
    elif (
        signals["mean_token_length"] is not None
        and signals["mean_token_length"] < thresholds["fragmented_mean_token_length"]
        and signals["alpha_token_count"] >= thresholds["fragmented_min_alpha_tokens"]
    ):
        result = ClassificationResult("unknown", None, "fragmented_text")
    elif signals["word_count"] <= thresholds["sparse_max_words"]:
        result = ClassificationResult("sparse", 0.6, "sparse_text")
    else:
        result = ClassificationResult("prose", 0.7, "prose_text")

    below_60 = signals.get("below_60_ratio")
    if (
        result.confidence is not None
        and isinstance(below_60, (int, float))
        and below_60 >= thresholds["low_confidence_below_60_ratio"]
    ):
        confidence = round(
            max(0.0, result.confidence - float(thresholds["low_confidence_penalty"])),
            4,
        )
        result = ClassificationResult(
            result.type,
            confidence,
            f"{result.reason}+low_word_confidence",
            result.action,
            result.prompt,
        )
    return result


def classifier_conformance_check(hook: Any) -> list[str]:
    """Return protocol issues for a user-supplied classifier hook."""
    issues: list[str] = []
    for attr in ("name", "version"):
        value = getattr(hook, attr, None)
        if not isinstance(value, str) or not value:
            issues.append(f"'{attr}' must be a non-empty string")
    page_types = getattr(hook, "page_types", None)
    if not isinstance(page_types, (tuple, list)) or not page_types:
        issues.append("'page_types' must be a non-empty tuple or list of strings")
    elif not all(isinstance(item, str) and item for item in page_types):
        issues.append("'page_types' items must be non-empty strings")
    if not callable(getattr(hook, "classify_page", None)):
        issues.append("missing required method 'classify_page(...)'")
    prompt_hash = getattr(hook, "prompt_hash", None)
    if prompt_hash is not None and not isinstance(prompt_hash, str):
        issues.append("'prompt_hash' must be a string or null")
    model = getattr(hook, "model", None)
    if model is not None and not isinstance(model, str):
        issues.append("'model' must be a string or null")
    return issues


def load_classifier_hook(spec: str, options: dict[str, Any] | None = None) -> Any:
    """Load and validate a classifier hook class, factory, or instance."""
    opts = dict(options or {})
    candidate = _import_object(spec, description="Classifier hook")
    hook = candidate
    if isinstance(hook, type) or (
        callable(hook) and not callable(getattr(hook, "classify_page", None))
    ):
        try:
            hook = hook(**opts)
        except TypeError as exc:
            raise ValueError(
                f"Classifier hook '{spec}' could not be constructed with options {sorted(opts)}"
            ) from exc
    elif opts:
        raise ValueError(
            f"Classifier hook '{spec}' is already an instance; "
            "classify.hook_options require a class or factory"
        )
    issues = classifier_conformance_check(hook)
    if issues:
        raise ValueError(f"Classifier hook '{spec}': {issues[0]}")
    return hook


def classify(
    *,
    inputs: list[Path],
    config_path: Path | None,
    out_path: Path,
    from_run: Path | None = None,
    probe_adapter: str | None = None,
    adapter_path: Path | None = None,
) -> dict[str, Any]:
    """Classify source pages or reclassify the retained evidence of a run."""
    if from_run is not None and inputs:
        raise ValueError("--from-run cannot be combined with input paths")
    if from_run is None and not inputs:
        raise ValueError("classify requires input paths or --from-run")
    if from_run is not None and probe_adapter is not None:
        raise ValueError("--adapter cannot be combined with --from-run")
    _apply_adapter_path(adapter_path)
    config = (
        load_config(config_path, validate_adapter=False)
        if config_path is not None
        else PageLedgerConfig(schema_version="0.1", data={})
    )
    hook = (
        load_classifier_hook(config.classify_hook, config.classify_hook_options)
        if config.classify_hook is not None
        else None
    )
    emittable_types = set(STRUCTURAL_PAGE_TYPES if hook is None else hook.page_types)
    emittable_types.add("unknown")
    _validate_taxonomy_gate(config, emittable_types)

    started_at = _utc_now()
    run_id = f"classify-{_utc_now_compact()}"
    warnings: list[str] = list(config.warnings)
    if from_run is None:
        expanded_inputs = _expand_inputs(inputs)
        _validate_unique_inputs(expanded_inputs)
        documents, evidence, probe_identities = _classify_inputs(
            inputs=expanded_inputs,
            config=config,
            hook=hook,
            probe_adapter=probe_adapter,
        )
    else:
        documents, evidence, probe_identities, parent_warnings = _classify_from_run(
            from_run=from_run,
            config=config,
            hook=hook,
        )
        warnings.extend(parent_warnings)

    classifier_identity = _classifier_identity(
        config=config,
        hook=hook,
        probe_identities=probe_identities,
    )
    route_map = build_route_map(
        schema_version=config.schema_version,
        run_id=run_id,
        generated_at=started_at,
        documents=documents,
        classifier=classifier_identity,
    )
    out = out_path.expanduser()
    if out.exists() and out.is_dir():
        raise ValueError(f"Classification output is a directory: {out_path}")
    if not out.parent.exists():
        raise ValueError(f"Classification output directory does not exist: {out.parent}")
    evidence_path = out.with_name(f"{out.stem}.evidence.jsonl")
    write_yaml(out, route_map)
    write_jsonl(evidence_path, evidence)
    return {
        "run_id": run_id,
        "out_path": str(out),
        "evidence_path": str(evidence_path),
        "documents": len(documents),
        "pages": len(evidence),
        "warnings": warnings,
        "classifier": classifier_identity,
    }


def _classify_inputs(
    *,
    inputs: list[Path],
    config: PageLedgerConfig,
    hook: Any,
    probe_adapter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    documents: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    identities: set[str] = set()
    for document_index, source in enumerate(inputs, start=1):
        adapter_spec = probe_adapter or config.classify_adapter or (
            "pdf_text" if source.suffix.lower() == ".pdf" else "text"
        )
        options = {} if probe_adapter is not None else config.classify_adapter_options
        adapter = load_adapter(adapter_spec, options)
        if not adapter.supports("transcribe_text"):
            raise ValueError(
                f"Classification probe adapter '{adapter.name}' does not support "
                "action 'transcribe_text'"
            )
        page_count = adapter_page_count(adapter, source)
        identity = f"{adapter.name}/{adapter.version}"
        identities.add(identity)
        source_sha256 = _sha256_path(source)
        pages: list[dict[str, Any]] = []
        for page_number in range(1, page_count + 1):
            page_id = f"doc_{document_index:04d}_page_{page_number:04d}"
            try:
                result = adapter.extract(
                    source,
                    page_id=page_id,
                    page_number=page_number,
                    action="transcribe_text",
                    prompt=None,
                )
                _validate_extraction_result(adapter.name, result)
            except Exception as exc:
                signals = structural_signals(
                    "", result_format=None, confidence_detail=None
                )
                decision = ClassificationResult(
                    "unknown", None, f"probe_failed:{type(exc).__name__}"
                )
                result_format = None
                model = None
            else:
                result_format = result.format
                model = result.model
                signals = structural_signals(
                    _quality_text(result.content),
                    result_format=result.format,
                    confidence_detail=result.confidence_detail,
                )
                builtin = classify_signals(
                    signals,
                    config.classify_thresholds,
                    pdf_embedded_text_probe=(
                        source.suffix.lower() == ".pdf"
                        and "embedded_text" in getattr(adapter, "capabilities", ())
                    ),
                )
                decision = _apply_hook(
                    hook=hook,
                    builtin=builtin,
                    page_id=page_id,
                    page_number=page_number,
                    source=source,
                    text=_quality_text(result.content),
                    signals=signals,
                )
            routed = _route_decision(config, decision)
            pages.append(_page_route(page_id, page_number, routed))
            evidence.append(
                _evidence_entry(
                    config=config,
                    page_id=page_id,
                    page_number=page_number,
                    source=source,
                    signals=signals,
                    adapter=adapter.name,
                    adapter_version=adapter.version,
                    model=model,
                    decision=routed,
                    result_format=result_format,
                )
            )
        documents.append(
            {
                "source": str(source.resolve()),
                "source_sha256": source_sha256,
                "page_count": page_count,
                "pages": pages,
            }
        )
    return documents, evidence, identities


def _classify_from_run(
    *,
    from_run: Path,
    config: PageLedgerConfig,
    hook: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], list[str]]:
    parent = from_run.expanduser().resolve()
    manifest = _load_json_object(parent / "manifest.json", "parent manifest")
    if manifest.get("execution_mode") == "dry_run":
        raise ValueError("Cannot classify --from-run evidence from a dry-run parent")
    if manifest.get("parent_run_id") is not None:
        raise ValueError("Cannot classify --from-run evidence from a rerun parent")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        raise ValueError("Parent manifest inputs must be a list")
    if any(isinstance(entry, dict) and "pages" in entry for entry in inputs):
        raise ValueError("Cannot classify --from-run evidence from a --pages partial parent")
    route_map = _load_yaml_object(parent / "route-map.yml", "parent route map")
    quality_by_page = {
        entry["page_id"]: entry
        for entry in read_jsonl(parent / "quality.jsonl")
        if isinstance(entry.get("page_id"), str)
    }
    provenance_by_page = {
        entry["page_id"]: entry
        for entry in read_jsonl(parent / "provenance.jsonl")
        if isinstance(entry.get("page_id"), str)
    }
    documents: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    identities: set[str] = set()
    warnings: list[str] = []
    parent_documents = route_map.get("documents")
    if not isinstance(parent_documents, list):
        raise ValueError("Parent route map documents must be a list")
    if len(parent_documents) != len(inputs):
        raise ValueError("Parent manifest and route map document counts do not match")
    raw_root = (parent / "raw").resolve()
    for document_index, document in enumerate(parent_documents):
        if not isinstance(document, dict) or not isinstance(document.get("source"), str):
            raise ValueError("Parent route map contains an invalid document")
        source = Path(document["source"]).expanduser()
        if not source.is_absolute():
            source = parent / source
        source = source.resolve()
        manifest_input = inputs[document_index]
        if not isinstance(manifest_input, dict):
            raise ValueError("Parent manifest contains an invalid input entry")
        recorded_hash = manifest_input.get("sha256")
        if isinstance(recorded_hash, str):
            if not source.is_file():
                warnings.append(f"Source is unavailable since parent run: {source}")
            elif _sha256_path(source) != recorded_hash:
                warnings.append(f"Source changed since parent run: {source}")
        pages: list[dict[str, Any]] = []
        for page in document.get("pages", []):
            page_id = page.get("page_id")
            page_number = page.get("page_number")
            if not isinstance(page_id, str) or not isinstance(page_number, int):
                raise ValueError(f"Parent route map has an invalid page for {source}")
            provenance = provenance_by_page.get(page_id)
            quality = quality_by_page.get(page_id)
            raw_path: Path | None = None
            if provenance is not None:
                raw_artifact = (provenance.get("result") or {}).get("raw_artifact")
                if isinstance(raw_artifact, str):
                    declared_raw = Path(raw_artifact)
                    candidate_raw = (parent / declared_raw).resolve()
                    if (
                        declared_raw.is_absolute()
                        or candidate_raw.parent != raw_root
                        or candidate_raw.stem != page_id
                    ):
                        raise ValueError(
                            f"Parent provenance raw_artifact for {page_id} must be "
                            f"raw/{page_id}.<format>"
                        )
                    raw_path = candidate_raw
            if provenance is None or raw_path is None or not raw_path.is_file():
                signals = structural_signals(
                    "", result_format=None, confidence_detail=None
                )
                decision = ClassificationResult("unknown", None, "no_parent_evidence")
                adapter_name = None
                adapter_version = None
                model = None
                result_format = None
            else:
                extractor = provenance.get("extractor") or {}
                result_info = provenance.get("result") or {}
                adapter_name = extractor.get("adapter")
                adapter_version = extractor.get("adapter_version")
                model = extractor.get("model")
                result_format = result_info.get("format")
                if isinstance(adapter_name, str) and isinstance(adapter_version, str):
                    identities.add(f"{adapter_name}/{adapter_version}")
                text = raw_path.read_text(encoding="utf-8")
                confidence_detail = (
                    quality.get("confidence_detail") if quality is not None else None
                )
                signals = structural_signals(
                    text,
                    result_format=result_format if isinstance(result_format, str) else None,
                    confidence_detail=(
                        confidence_detail if isinstance(confidence_detail, dict) else None
                    ),
                )
                builtin = classify_signals(
                    signals,
                    config.classify_thresholds,
                    pdf_embedded_text_probe=(
                        source.suffix.lower() == ".pdf"
                        and "embedded_text" in (extractor.get("capabilities") or [])
                    ),
                )
                decision = _apply_hook(
                    hook=hook,
                    builtin=builtin,
                    page_id=page_id,
                    page_number=page_number,
                    source=source,
                    text=text,
                    signals=signals,
                )
            routed = _route_decision(config, decision)
            pages.append(_page_route(page_id, page_number, routed))
            evidence.append(
                _evidence_entry(
                    config=config,
                    page_id=page_id,
                    page_number=page_number,
                    source=source,
                    signals=signals,
                    adapter=adapter_name if isinstance(adapter_name, str) else None,
                    adapter_version=(
                        adapter_version if isinstance(adapter_version, str) else None
                    ),
                    model=model if isinstance(model, str) else None,
                    decision=routed,
                    result_format=(
                        result_format if isinstance(result_format, str) else None
                    ),
                )
            )
        documents.append(
            {
                "source": str(source),
                "source_sha256": manifest_input.get("sha256"),
                "page_count": manifest_input.get("page_count"),
                "pages": pages,
            }
        )
    return documents, evidence, identities, warnings


def _apply_hook(
    *,
    hook: Any,
    builtin: ClassificationResult,
    page_id: str,
    page_number: int,
    source: Path,
    text: str,
    signals: dict[str, Any],
) -> ClassificationResult:
    if hook is None:
        return builtin
    hook_signals = dict(signals)
    hook_signals["builtin_type"] = builtin.type
    try:
        result = hook.classify_page(
            page_id=page_id,
            page_number=page_number,
            source=source,
            text=text,
            signals=hook_signals,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Classifier hook failed for {page_id}: {type(exc).__name__}: {exc}"
        ) from exc
    return _validate_hook_result(hook, result, page_id)


def _validate_hook_result(hook: Any, result: Any, page_id: str) -> ClassificationResult:
    if not isinstance(result, ClassificationResult):
        raise ValueError(
            f"Classifier hook returned invalid output for {page_id}: "
            "expected ClassificationResult"
        )
    if result.type not in set(hook.page_types):
        raise ValueError(
            f"Classifier hook returned type '{result.type}' for {page_id}; "
            "it is not declared in hook.page_types"
        )
    if result.confidence is not None and (
        isinstance(result.confidence, bool)
        or not isinstance(result.confidence, (int, float))
        or not math.isfinite(result.confidence)
        or not 0 <= result.confidence <= 1
    ):
        raise ValueError(
            f"Classifier hook confidence for {page_id} must be between 0 and 1 or null"
        )
    if not isinstance(result.reason, str) or not result.reason:
        raise ValueError(f"Classifier hook reason for {page_id} must be a non-empty string")
    for field_name in ("action", "prompt"):
        value = getattr(result, field_name)
        if value is not None and not isinstance(value, str):
            raise ValueError(
                f"Classifier hook {field_name} for {page_id} must be a string or null"
            )
    if result.action == "":
        raise ValueError(f"Classifier hook action for {page_id} must be non-empty or null")
    return result


def _route_decision(
    config: PageLedgerConfig, decision: ClassificationResult
) -> ClassificationResult:
    page_types = ((config.data.get("taxonomy") or {}).get("page_types") or {})
    page_config = page_types.get(decision.type)
    action = decision.action
    prompt = decision.prompt
    if action is None and isinstance(page_config, dict):
        configured_action = page_config.get("default_action")
        if isinstance(configured_action, str) and configured_action:
            action = configured_action
    if prompt is None and isinstance(page_config, dict):
        configured_prompt = page_config.get("prompt")
        if isinstance(configured_prompt, str):
            prompt = configured_prompt
    if action is None:
        action = "review"
    if decision.confidence is None or decision.confidence < config.classify_min_confidence:
        action = "review"
    return ClassificationResult(
        decision.type,
        decision.confidence,
        decision.reason,
        action,
        prompt,
    )


def _page_route(
    page_id: str, page_number: int, decision: ClassificationResult
) -> dict[str, Any]:
    page = {
        "page_id": page_id,
        "page_number": page_number,
        "type": decision.type,
        "confidence": decision.confidence,
        "action": decision.action,
        "reason": decision.reason,
    }
    if decision.prompt is not None:
        page["prompt"] = decision.prompt
    return page


def _evidence_entry(
    *,
    config: PageLedgerConfig,
    page_id: str,
    page_number: int,
    source: Path,
    signals: dict[str, Any],
    adapter: str | None,
    adapter_version: str | None,
    model: str | None,
    decision: ClassificationResult,
    result_format: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "page_id": page_id,
        "page_number": page_number,
        "source": str(source.resolve()),
        "signals": signals,
        "probe": {
            "adapter": adapter,
            "adapter_version": adapter_version,
            "model": model,
            "result_format": result_format,
        },
        "decision": {
            "type": decision.type,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "action": decision.action,
            "prompt": decision.prompt,
        },
    }


def _classifier_identity(
    *,
    config: PageLedgerConfig,
    hook: Any,
    probe_identities: set[str],
) -> dict[str, str | None]:
    if hook is not None:
        return {
            "adapter": config.classify_hook,
            "model": getattr(hook, "model", None) or f"{hook.name}/{hook.version}",
            "prompt_hash": getattr(hook, "prompt_hash", None),
        }
    if not probe_identities:
        model = "unknown"
    elif len(probe_identities) == 1:
        model = next(iter(probe_identities))
    else:
        model = "mixed:" + ",".join(sorted(probe_identities))
    return {
        "adapter": "builtin:structural",
        "model": model,
        "prompt_hash": None,
    }


def _validate_taxonomy_gate(config: PageLedgerConfig, emittable_types: set[str]) -> None:
    taxonomy = config.data.get("taxonomy") or {}
    page_types = taxonomy.get("page_types") or {}
    if not page_types:
        return
    missing = sorted(emittable_types - set(page_types))
    if missing:
        raise ValueError(
            "taxonomy.page_types is missing classifier-emittable types: "
            + ", ".join(missing)
        )


def _validate_unique_inputs(inputs: list[Path]) -> None:
    seen: set[Path] = set()
    for source in inputs:
        resolved = source.resolve()
        if resolved in seen:
            raise ValueError(f"Classification inputs contain duplicate source: {resolved}")
        seen.add(resolved)


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"No {path.name} found in {path.parent}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label.capitalize()} must be a mapping")
    return value


def _load_yaml_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"No {path.name} found in {path.parent}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label.capitalize()} must be a mapping")
    return value
