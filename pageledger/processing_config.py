"""Explicit document processing policy, separate from rerun adapter generations."""

from __future__ import annotations

import math
from typing import Any

STAGES = ("local_text", "local_ocr", "image", "second_opinion")
# Why a page climbs to the next stage. `hold` is 0.6's rule and always applies;
# `rough` and `disagreement` let a page whose reading looks clean climb too.
TRIGGERS = ("hold", "rough", "disagreement")
LEXICON_PROVIDERS = ("pymorphy3",)
PROMPT = (
    "Transcribe only text visible on this source page, preserving page order, headings, "
    "footnotes and named table columns. Do not reconstruct missing or invisible text. "
    "Mark unreadable spans explicitly. Return the transcript without interpretation."
)


def processing_config(data: dict[str, Any], *, pdf: bool) -> dict[str, Any]:
    value = data.get("processing", {})
    if not isinstance(value, dict):
        raise ValueError("processing must be a mapping")
    unknown = set(value) - {*STAGES, "limits", "links", "benchmark", "escalate_on", "lexicon"}
    if unknown:
        raise ValueError(f"Unknown processing key: {sorted(unknown)[0]}")
    result: dict[str, Any] = {}
    for name in STAGES:
        default = None
        if name == "local_text":
            default = {"adapter": "pdf_text" if pdf else "text"}
        elif name == "local_ocr" and pdf:
            default = {"adapter": "pdf_ocr"}
        entry = value.get(name, default)
        if entry is None:
            if name == "local_text":
                raise ValueError("processing.local_text is required")
            result[name] = None
            continue
        if not isinstance(entry, dict) or set(entry) - {"adapter", "adapter_options", "prompt"}:
            raise ValueError(f"Invalid processing.{name} profile")
        adapter = entry.get("adapter")
        options = entry.get("adapter_options", {})
        prompt = entry.get("prompt", PROMPT)
        if (
            not isinstance(adapter, str)
            or not adapter.strip()
            or not isinstance(options, dict)
            or not all(isinstance(k, str) for k in options)
            or not isinstance(prompt, str)
            or not prompt.strip()
        ):
            raise ValueError(f"Invalid processing.{name} adapter, options or prompt")
        if "evidence_dir" in options:
            raise ValueError("processing owns the adapter evidence_dir")
        result[name] = {"adapter": adapter, "adapter_options": dict(options), "prompt": prompt}
    if result["second_opinion"] is not None and result["image"] is None:
        raise ValueError("second_opinion requires an image stage")
    if result["image"] is not None and result["local_ocr"] is None:
        raise ValueError("An image stage requires an explicit local_ocr stage")
    limits = value.get("limits", {})
    keys = {"max_attempt_pages", "max_image_pages", "max_tokens", "max_cost_usd"}
    if not isinstance(limits, dict) or set(limits) - keys:
        raise ValueError("Invalid processing.limits")
    normalized: dict[str, Any] = {key: limits.get(key) for key in keys}
    normalized["max_image_pages"] = limits.get("max_image_pages", 0)
    for key, number in normalized.items():
        if number is None and key != "max_image_pages":
            continue
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number < 0
            or (key != "max_cost_usd" and not isinstance(number, int))
        ):
            raise ValueError(f"processing.limits.{key} must be a nonnegative number")
    if result["image"] is not None and normalized["max_image_pages"] < 1:
        raise ValueError("An image stage requires a positive max_image_pages bound")
    result["limits"] = normalized
    links = value.get("links", {})
    if not isinstance(links, dict) or set(links) - {"article", "custody"}:
        raise ValueError("processing.links accepts article and custody references only")
    if any(not isinstance(v, str) or not v for v in links.values()):
        raise ValueError("processing.links values must be nonempty strings")
    result["links"] = {"article": links.get("article"), "custody": links.get("custody")}
    benchmark = value.get("benchmark")
    if benchmark is not None:
        if (
            not isinstance(benchmark, dict)
            or set(benchmark) != {"stage", "every_nth_page"}
            or benchmark.get("stage") not in STAGES
            or type(benchmark.get("every_nth_page")) is not int
            or benchmark["every_nth_page"] < 1
        ):
            raise ValueError(
                "processing.benchmark must contain a configured stage and a positive integer every_nth_page"
            )
        if result[benchmark["stage"]] is None:
            raise ValueError("processing.benchmark.stage must name an enabled stage")
        result["benchmark"] = dict(benchmark)
    triggers, lexicon = _escalation(value)
    if triggers != ["hold"]:
        # Only when used, so a 0.6 config still compiles to the policy its jobs recorded.
        result["escalate_on"] = triggers
        if lexicon is not None:
            result["lexicon"] = lexicon
    return result


def _escalation(value: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    """Validate escalate_on and lexicon; refuse settings that would do nothing."""
    triggers = value.get("escalate_on", ["hold"])
    if not isinstance(triggers, list) or not all(isinstance(item, str) for item in triggers):
        raise ValueError("processing.escalate_on must be a list of trigger names")
    unknown = sorted(set(triggers) - set(TRIGGERS))
    if unknown:
        raise ValueError(f"Unknown escalation trigger: {unknown[0]} (use {', '.join(TRIGGERS)})")
    if len(set(triggers)) != len(triggers):
        raise ValueError("processing.escalate_on names a trigger more than once")
    if "hold" not in triggers:
        raise ValueError("processing.escalate_on must include hold, the 0.6 rule")
    lexicon = value.get("lexicon")
    if "rough" in triggers and lexicon is None:
        raise ValueError("The rough trigger needs processing.lexicon to judge words")
    if lexicon is None:
        return [name for name in TRIGGERS if name in triggers], None
    if "rough" not in triggers:
        raise ValueError(
            "processing.lexicon is used only by the rough trigger; add rough to escalate_on"
        )
    if (
        not isinstance(lexicon, dict)
        or set(lexicon) != {"provider", "language", "rough_below"}
        or lexicon["provider"] not in LEXICON_PROVIDERS
        or lexicon["language"] != "ru"
    ):
        raise ValueError(
            "processing.lexicon needs provider: pymorphy3, language: ru and rough_below"
        )
    threshold = lexicon["rough_below"]
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 < threshold <= 1
    ):
        raise ValueError("processing.lexicon.rough_below must be a share above 0 and at most 1")
    return [name for name in TRIGGERS if name in triggers], dict(lexicon)
