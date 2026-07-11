"""Budget enforcement and cost reporting for PageLedger runs."""

from __future__ import annotations

from typing import Any


class BudgetExceededError(RuntimeError):
    """Raised when a configured budget cap is crossed."""


def _derive_cost(
    usage: dict[str, Any],
    *,
    cost_per_page: float | None,
    cost_per_1k_tokens: float | None,
) -> tuple[float | None, str | None]:
    """Resolve a page's cost and where the number came from, in priority order.

    1. adapter-reported cost_usd (passthrough) — basis adapter_reported
    2. config unit rates — basis configured_rate
    3. (None, None) — cost is unknown; the run still reports raw units.
    """
    adapter_cost = usage.get("cost_usd")
    if adapter_cost is not None:
        return float(adapter_cost), "adapter_reported"
    if cost_per_page is None and cost_per_1k_tokens is None:
        return None, None
    cost = 0.0
    if cost_per_page is not None:
        cost += cost_per_page * int(usage.get("pages") or 0)
    if cost_per_1k_tokens is not None:
        tokens = usage.get("tokens")
        if tokens is None:
            return None, None
        cost += cost_per_1k_tokens * (tokens / 1000)
    return cost, "configured_rate"


def _cost_basis(cost_bases: set[str]) -> str:
    """Summarize where the run's dollar figure came from.

    adapter_reported is real provider-reported spend; configured_rate
    is the user's own accounting rate applied to usage; mixed combines
    both; none means no dollar figure exists (e.g. a free local engine
    with no pricing configured).
    """
    if not cost_bases:
        return "none"
    if len(cost_bases) > 1:
        return "mixed"
    return next(iter(cost_bases))


def _round_cost(value: float) -> float:
    return round(value, 12)


def _usage_rollup(
    usage_entries: list[dict[str, Any]],
    extraction_seconds_values: list[float],
) -> dict[str, Any]:
    return {
        "pages": _sum_usage_field(usage_entries, "pages"),
        "tokens": _sum_usage_field(usage_entries, "tokens"),
        "compute_seconds": _sum_usage_field(usage_entries, "compute_seconds"),
        "extraction_seconds": (
            round(sum(extraction_seconds_values), 3) if extraction_seconds_values else None
        ),
    }


def _build_cost_report(
    *,
    schema_version: str,
    run_id: str,
    execution_mode: str,
    config: Any,
    pages_extracted: int,
    tokens_total: int,
    usage_entries: list[dict[str, Any]],
    estimated_cost_usd: float,
    cost_is_partial: bool,
    cost_bases: set[str],
    extraction_seconds_values: list[float],
) -> dict[str, Any]:
    report = {
        "schema_version": schema_version,
        "run_id": run_id,
        "execution_mode": execution_mode,
        "currency": "USD",
        "canonical_unit": "pages",
        "pages_extracted": pages_extracted,
        "tokens_total": tokens_total,
        "pricing": config.pricing,
        "usage": _usage_rollup(usage_entries, extraction_seconds_values),
        "cost_usd": None if cost_is_partial and estimated_cost_usd == 0.0 else estimated_cost_usd,
        "cost_known": not cost_is_partial,
        "cost_basis": _cost_basis(cost_bases),
    }
    budget = _budget_report(
        config=config,
        pages_total=pages_extracted,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    )
    if budget:
        report["budget"] = budget
    return report


# Budget caps, in (config attr, label, current-value) form. Pages is the
# canonical unit; tokens and dollars are enforced when the config sets them.
def _budget_caps(
    config: Any, *, pages_total: int, tokens_total: int, estimated_cost_usd: float
) -> list[tuple[str, float, float]]:
    caps: list[tuple[str, float, float]] = []
    if config.budget_max_pages is not None:
        caps.append(("pages", config.budget_max_pages, pages_total))
    if config.budget_max_tokens is not None:
        caps.append(("tokens", config.budget_max_tokens, tokens_total))
    if config.budget_max_usd is not None:
        caps.append(("usd", config.budget_max_usd, estimated_cost_usd))
    return caps


def _preflight_budget_error(*, config: Any, pages_total: int) -> str | None:
    if config.budget_max_pages is None or pages_total <= config.budget_max_pages:
        return None
    return (
        "Budget exceeded before extraction: "
        f"pages={pages_total} max_pages={config.budget_max_pages}"
    )


def _budget_report(
    *, config: Any, pages_total: int, tokens_total: int, estimated_cost_usd: float
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    warn_at_percent = config.budget_warn_at_percent
    for unit, cap, current in _budget_caps(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        entry: dict[str, Any] = {"max": cap, "current": current, "exceeded": current > cap}
        if warn_at_percent is not None:
            warn_at = cap * (warn_at_percent / 100)
            entry.update({"warn_at": warn_at, "warning": current >= warn_at})
        report[unit] = entry
    return report


def _budget_error(
    *,
    config: Any,
    page_id: str,
    pages_total: int,
    tokens_total: int,
    estimated_cost_usd: float,
) -> str | None:
    for unit, cap, current in _budget_caps(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        if current > cap:
            return f"Budget exceeded after {page_id}: {unit}={current} max_{unit}={cap}"
    return None


def _budget_warning(
    *,
    config: Any,
    pages_total: int,
    tokens_total: int,
    estimated_cost_usd: float,
) -> str | None:
    warn_at_percent = config.budget_warn_at_percent
    if warn_at_percent is None:
        return None
    for unit, cap, current in _budget_caps(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        warn_at = cap * (warn_at_percent / 100)
        if current >= warn_at:
            return f"{unit}={current} warn_at_{unit}={warn_at}"
    return None


def _sum_usage_field(usage_entries: list[dict[str, Any]], field: str) -> int | float | None:
    values: list[int | float] = []
    for usage in usage_entries:
        value = usage.get(field)
        if isinstance(value, (int, float)):
            values.append(value)
    if not values:
        return None
    return sum(values)
