"""Configuration loading for the PageLedger alpha runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .adapters import load_adapter
from .aligner import load_schema_spec
from .grading import GRADES, merge_thresholds, validate_thresholds

# ---------------------------------------------------------------------------
# Known top-level keys for v0.1.  Unknown keys trigger a warning so users
# who try old flat-config patterns get a clear message.
# ---------------------------------------------------------------------------
_KNOWN_TOP_LEVEL = frozenset({
    "schema_version",
    "dataset_citation",
    "taxonomy",
    "schema",
    "run",
})


@dataclass(frozen=True)
class PageLedgerConfig:
    schema_version: str
    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def default_review_type(self) -> str:
        page_types = _mapping_at(self.data, "taxonomy", "page_types")

        if "prose" in page_types:
            return "prose"
        if page_types:
            return next(iter(page_types))
        return "unclassified"

    @property
    def max_rerun_depth(self) -> int:
        value = _value_at(self.data, "run", "max_rerun_depth")
        if value is None:
            return 2
        try:
            max_rerun_depth = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("run.max_rerun_depth must be an integer") from exc
        if max_rerun_depth < 0:
            raise ValueError("run.max_rerun_depth must be non-negative")
        return max_rerun_depth

    @property
    def adapter_name(self) -> str | None:
        value = _value_at(self.data, "run", "adapter")
        return str(value) if value else None

    @property
    def adapter_options(self) -> dict[str, Any]:
        value = _value_at(self.data, "run", "adapter_options")
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("run.adapter_options must be a mapping")
        for key in value:
            if not isinstance(key, str):
                raise ValueError("run.adapter_options keys must be strings")
        return value

    @property
    def pricing(self) -> dict[str, Any]:
        return _mapping_at(self.data, "run", "pricing")

    @property
    def cost_per_page(self) -> float | None:
        return _nonneg_number(
            _value_at(self.data, "run", "pricing", "cost_per_page"),
            "run.pricing.cost_per_page",
        )

    @property
    def cost_per_1k_tokens(self) -> float | None:
        return _nonneg_number(
            _value_at(self.data, "run", "pricing", "cost_per_1k_tokens"),
            "run.pricing.cost_per_1k_tokens",
        )

    @property
    def budget_max_pages(self) -> int | None:
        return _nonneg_number(
            _value_at(self.data, "run", "budget", "max_pages"),
            "run.budget.max_pages",
            cast=int,
        )

    @property
    def budget_max_tokens(self) -> int | None:
        return _nonneg_number(
            _value_at(self.data, "run", "budget", "max_tokens"),
            "run.budget.max_tokens",
            cast=int,
        )

    @property
    def budget_max_usd(self) -> float | None:
        value = _value_at(self.data, "run", "budget", "max_usd")
        if value is None:
            return None
        try:
            max_usd = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("run.budget.max_usd must be a number") from exc
        if max_usd < 0:
            raise ValueError("run.budget.max_usd must be non-negative")
        return max_usd

    @property
    def budget_warn_at_percent(self) -> float | None:
        value = _value_at(self.data, "run", "budget", "warn_at_percent")
        if value is None:
            return None
        try:
            warn_at_percent = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("run.budget.warn_at_percent must be a number") from exc
        if warn_at_percent < 0 or warn_at_percent > 100:
            raise ValueError("run.budget.warn_at_percent must be between 0 and 100")
        return warn_at_percent

    @property
    def max_retries(self) -> int:
        value = _value_at(self.data, "run", "retry", "max_retries")
        if value is None:
            return 0
        try:
            max_retries = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("run.retry.max_retries must be an integer") from exc
        if max_retries < 0:
            raise ValueError("run.retry.max_retries must be non-negative")
        return max_retries

    @property
    def retry_backoff(self) -> str:
        value = _value_at(self.data, "run", "retry", "backoff")
        if value is None:
            return "none"
        backoff = str(value)
        if backoff not in {"none", "exponential"}:
            raise ValueError("run.retry.backoff must be 'none' or 'exponential'")
        return backoff

    @property
    def default_action(self) -> str:
        page_types = _mapping_at(self.data, "taxonomy", "page_types")

        page_config = page_types.get(self.default_review_type)
        if isinstance(page_config, dict) and page_config.get("default_action"):
            return str(page_config["default_action"])
        return "review"

    @property
    def default_prompt(self) -> str | None:
        page_types = _mapping_at(self.data, "taxonomy", "page_types")
        page_config = page_types.get(self.default_review_type)
        if isinstance(page_config, dict) and page_config.get("prompt"):
            return str(page_config["prompt"])
        return None

    @property
    def review_below_grade(self) -> str | None:
        """Grade threshold below which pages join the review queue.

        Null by default: grading annotates without changing review behavior
        unless the user opts in.
        """
        value = _value_at(self.data, "run", "grading", "review_below_grade")
        if value is None:
            return None
        grade = str(value).upper()
        if grade not in GRADES:
            raise ValueError(
                f"run.grading.review_below_grade must be one of: {', '.join(GRADES)}"
            )
        return grade

    @property
    def grading_thresholds(self) -> dict[str, dict[str, float]]:
        overrides = _value_at(self.data, "run", "grading", "thresholds")
        validate_thresholds(overrides)
        return merge_thresholds(overrides)

    @property
    def dataset_citation(self) -> dict[str, str] | None:
        citation = self.data.get("dataset_citation")
        if not isinstance(citation, dict):
            return None

        label = citation.get("label")
        text = citation.get("text")
        if label or text:
            return {"label": str(label or ""), "text": str(text or "")}
        return None


def load_config(path: Path, *, validate_adapter: bool = True) -> PageLedgerConfig:
    if not path.exists():
        raise ValueError(f"Config path does not exist: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Could not parse config YAML: {path}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config must be a YAML mapping: {path}")

    schema_version = loaded.get("schema_version", "0.1")
    config = PageLedgerConfig(schema_version=str(schema_version), data=loaded)
    _validate_config(config, validate_adapter=validate_adapter)
    return config


def _validate_config(config: PageLedgerConfig, *, validate_adapter: bool) -> None:
    _validate_taxonomy(config)
    load_schema_spec(config.data)
    _reject_flat_keys(config)
    _warn_unknown_top_level(config)
    _warn_impossible_budget(config)

    # Each property raises ValueError on malformed config; touch them all.
    for prop in (
        "max_rerun_depth",
        "budget_max_usd",
        "budget_warn_at_percent",
        "budget_max_pages",
        "budget_max_tokens",
        "cost_per_page",
        "cost_per_1k_tokens",
        "max_retries",
        "retry_backoff",
        "adapter_options",
        "review_below_grade",
        "grading_thresholds",
    ):
        getattr(config, prop)
    if validate_adapter and config.adapter_name is not None:
        load_adapter(config.adapter_name, config.adapter_options)


def _validate_taxonomy(config: PageLedgerConfig) -> None:
    taxonomy = config.data.get("taxonomy")
    if taxonomy is not None and not isinstance(taxonomy, dict):
        raise ValueError("taxonomy must be a mapping")

    if isinstance(taxonomy, dict) and "page_types" in taxonomy:
        page_types = taxonomy["page_types"]
        if not isinstance(page_types, dict):
            raise ValueError("taxonomy.page_types must be a mapping")
        if not page_types:
            config.warnings.append(
                "taxonomy.page_types is empty — every page will be classified as "
                "'unclassified' and routed to 'review'"
            )


def _reject_flat_keys(config: PageLedgerConfig) -> None:
    """Reject old-style flat config keys that moved under taxonomy/run."""
    flat_keys = {
        "page_types": "taxonomy.page_types",
        "adapter": "run.adapter",
        "max_rerun_depth": "run.max_rerun_depth",
    }
    for flat, nested in flat_keys.items():
        if flat in config.data:
            raise ValueError(f"Use {nested}; flat {flat} is not supported")


def _warn_unknown_top_level(config: PageLedgerConfig) -> None:
    """Warn about unknown top-level keys that may be old-style flat config."""
    for key in config.data:
        if key not in _KNOWN_TOP_LEVEL:
            config.warnings.append(
                f"Unknown top-level key '{key}' — ignored. Known keys in "
                f"schema_version 0.1 are: {', '.join(sorted(_KNOWN_TOP_LEVEL))}"
            )


def _warn_impossible_budget(config: PageLedgerConfig) -> None:
    """Warn when budget thresholds make enforcement impossible."""
    budget = _mapping_at(config.data, "run", "budget")
    if not budget:
        return
    warn_pct = config.budget_warn_at_percent
    if warn_pct is not None and warn_pct >= 100:
        config.warnings.append(
            "run.budget.warn_at_percent >= 100 — budget warnings will never fire"
        )
    if warn_pct is not None and warn_pct <= 0:
        config.warnings.append(
            "run.budget.warn_at_percent <= 0 — budget warnings fire on every page"
        )
    # warn_at_percent > 100 is rejected by the property, not warned
    max_usd = config.budget_max_usd
    if max_usd is not None and max_usd == 0:
        config.warnings.append(
            "run.budget.max_usd is 0 — dollar budget will be exceeded immediately"
        )


def _nonneg_number(value: Any, name: str, *, cast: Any = float) -> Any:
    if value is None:
        return None
    try:
        number = cast(value)
    except (TypeError, ValueError) as exc:
        kind = "an integer" if cast is int else "a number"
        raise ValueError(f"{name} must be {kind}") from exc
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _walk(data: dict[str, Any], *path: str) -> Any:
    """Follow *path* through nested dicts; return None on any miss."""
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _mapping_at(data: dict[str, Any], *path: str) -> dict[str, Any]:
    value = _walk(data, *path)
    return value if isinstance(value, dict) else {}


def _value_at(data: dict[str, Any], *path: str) -> Any:
    return _walk(data, *path)
