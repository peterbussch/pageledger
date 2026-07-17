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
    provenance_entries: list[dict[str, Any]] | None = None,
    budget_alerts: list[dict[str, Any]] | None = None,
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
    if budget_alerts:
        report["alerts"] = [dict(alert) for alert in budget_alerts]
    if provenance_entries:
        report["by_adapter"] = _provenance_rollups(
            provenance_entries, section="extractor", field="adapter"
        )
        report["by_page_type"] = _provenance_rollups(
            provenance_entries, section="route", field="type"
        )
    return report


def _provenance_rollups(
    entries: list[dict[str, Any]], *, section: str, field: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = str(entry[section][field])
        grouped.setdefault(key, []).append(entry)
    return {key: _provenance_rollup(grouped[key]) for key in sorted(grouped)}


def _provenance_rollup(entries: list[dict[str, Any]]) -> dict[str, Any]:
    usage_entries = [entry["usage"] for entry in entries]
    known_costs: list[float] = []
    cost_is_partial = False
    for entry in entries:
        cost = entry.get("cost")
        value = cost.get("usd") if isinstance(cost, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            known_costs.append(float(value))
        else:
            cost_is_partial = True
    cost_total = _round_cost(sum(known_costs))
    return {
        "pages": sum(int(usage["pages"]) for usage in usage_entries),
        "tokens": _sum_usage_field(usage_entries, "tokens"),
        "compute_seconds": _sum_usage_field(usage_entries, "compute_seconds"),
        "cost_usd": None if cost_is_partial and not known_costs else cost_total,
        "cost_known": not cost_is_partial,
    }


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
    for unit, threshold, _kind, current in _budget_warning_thresholds(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        if current >= threshold:
            return f"{unit}={current} warn_at_{unit}={threshold}"
    return None


def _new_budget_alerts(
    *,
    config: Any,
    page_id: str,
    timestamp: str,
    pages_total: int,
    tokens_total: int,
    estimated_cost_usd: float,
    alerted_units: set[str],
) -> list[dict[str, Any]]:
    """Return each unit whose effective warning threshold crossed now.

    Absolute and cap-relative thresholds can coexist. The lower threshold is
    the useful first warning; an exact tie is attributed to the explicit
    absolute setting. ``alerted_units`` makes the first-crossing policy
    visible to the caller without hidden mutable state.
    """
    alerts: list[dict[str, Any]] = []
    for unit, threshold, kind, current in _budget_warning_thresholds(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        if unit in alerted_units or current < threshold:
            continue
        alerts.append(
            {
                "unit": unit,
                "threshold": threshold,
                "kind": kind,
                "current": current,
                "page_id": page_id,
                "timestamp": timestamp,
            }
        )
    return alerts


def _budget_warning_thresholds(
    config: Any, *, pages_total: int, tokens_total: int, estimated_cost_usd: float
) -> list[tuple[str, int | float, str, int | float]]:
    currents: tuple[tuple[str, int | float, int | float | None, int | float | None], ...] = (
        ("pages", pages_total, config.budget_warn_pages, config.budget_max_pages),
        ("tokens", tokens_total, config.budget_warn_tokens, config.budget_max_tokens),
        ("usd", estimated_cost_usd, config.budget_warn_usd, config.budget_max_usd),
    )
    warn_at_percent = config.budget_warn_at_percent
    thresholds: list[tuple[str, int | float, str, int | float]] = []
    for unit, current, absolute, cap in currents:
        candidates: list[tuple[int | float, str]] = []
        if absolute is not None:
            candidates.append((absolute, "absolute"))
        if warn_at_percent is not None and cap is not None:
            candidates.append((cap * (warn_at_percent / 100), "percent"))
        if not candidates:
            continue
        threshold, kind = min(
            candidates,
            key=lambda candidate: (
                candidate[0],
                0 if candidate[1] == "absolute" else 1,
            ),
        )
        thresholds.append((unit, threshold, kind, current))
    return thresholds


def _sum_usage_field(usage_entries: list[dict[str, Any]], field: str) -> int | float | None:
    values: list[int | float] = []
    for usage in usage_entries:
        value = usage.get(field)
        if isinstance(value, (int, float)):
            values.append(value)
    if not values:
        return None
    return sum(values)
