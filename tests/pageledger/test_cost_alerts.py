"""First-crossing budget alerts and provenance-derived cost rollups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml


class MeteredAdapter:
    """Deterministic adapter with configurable usage for cost assertions."""

    name = "metered"
    version = "test"
    deterministic = True
    input_types = ("text",)
    output_types = ("text",)
    capabilities = ("test",)

    def __init__(
        self,
        *,
        cost_usd: float = 0.4,
        tokens: int = 20,
        compute_seconds: float = 0.2,
    ) -> None:
        self.cost_usd = cost_usd
        self.tokens = tokens
        self.compute_seconds = compute_seconds

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> Any:
        from pageledger.adapters import ExtractionResult

        _ = source, page_id, action, prompt
        return ExtractionResult(
            content=f"metered page {page_number}",
            format="text",
            confidence=None,
            model="metered-test",
            warnings=[],
            usage={
                "pages": 1,
                "tokens": self.tokens,
                "compute_seconds": self.compute_seconds,
                "cost_usd": self.cost_usd,
            },
        )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_config(
    path: Path,
    *,
    budget: dict[str, Any] | None = None,
    cost_usd: float = 0.4,
    tokens: int = 20,
    compute_seconds: float = 0.2,
) -> Path:
    run_config: dict[str, Any] = {
        "adapter": "test_cost_alerts:MeteredAdapter",
        "adapter_options": {
            "cost_usd": cost_usd,
            "tokens": tokens,
            "compute_seconds": compute_seconds,
        },
    }
    if budget is not None:
        run_config["budget"] = budget
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "taxonomy": {
                    "page_types": {
                        "prose": {"default_action": "transcribe_text"},
                        "blank": {"default_action": "skip"},
                        "table_likely": {"default_action": "transcribe_text"},
                    }
                },
                "run": run_config,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _run_pages(
    tmp_path: Path,
    *,
    page_count: int,
    budget: dict[str, Any] | None = None,
    cost_usd: float = 0.4,
    tokens: int = 20,
    compute_seconds: float = 0.2,
    routes: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    from pageledger.runner import run

    source = tmp_path / "source.txt"
    source.write_text(
        "\f".join(f"source page {number}" for number in range(1, page_count + 1)),
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path / "pageledger.yml",
        budget=budget,
        cost_usd=cost_usd,
        tokens=tokens,
        compute_seconds=compute_seconds,
    )
    routes_path = None
    if routes is not None:
        routes["documents"][0]["source"] = str(source)
        routes_path = tmp_path / "routes.yml"
        routes_path.write_text(yaml.safe_dump(routes, sort_keys=False), encoding="utf-8")
    out_dir = tmp_path / "run"
    result = run(
        inputs=[source],
        config_path=config,
        routes_path=routes_path,
        out_dir=out_dir,
        dry_run=False,
    )
    return out_dir, result


@pytest.mark.parametrize(
    ("budget", "message"),
    [
        ({"warn_pages": 1.5}, "run.budget.warn_pages must be an integer"),
        ({"warn_tokens": True}, "run.budget.warn_tokens must be an integer"),
        ({"warn_usd": "many"}, "run.budget.warn_usd must be a number"),
    ],
)
def test_absolute_warning_thresholds_are_validated(
    tmp_path: Path, budget: dict[str, Any], message: str
) -> None:
    from pageledger.config import load_config

    config_path = _write_config(tmp_path / "pageledger.yml", budget=budget)

    with pytest.raises(ValueError, match=message):
        load_config(config_path, validate_adapter=False)


def test_warning_above_cap_is_an_impossible_combo_warning(tmp_path: Path) -> None:
    from pageledger.config import load_config

    config_path = _write_config(
        tmp_path / "pageledger.yml",
        budget={
            "max_pages": 5,
            "warn_pages": 6,
            "max_tokens": 50,
            "warn_tokens": 60,
            "max_usd": 1.0,
            "warn_usd": 1.5,
        },
    )

    warnings = load_config(config_path, validate_adapter=False).warnings
    assert len(warnings) == 3
    for unit in ("pages", "tokens", "usd"):
        assert any(
            f"warn_{unit}" in warning
            and f"max_{unit}" in warning
            and "cannot provide advance warning" in warning
            for warning in warnings
        )


def test_capless_warn_usd_alerts_once_on_first_crossing_page(tmp_path: Path) -> None:
    out_dir, result = _run_pages(
        tmp_path,
        page_count=4,
        budget={"warn_usd": 1.0},
    )

    cost = _read_json(out_dir / "cost.json")
    assert "budget" not in cost
    assert result["budget_alerts"] == cost["alerts"]
    assert len(cost["alerts"]) == 1
    alert = cost["alerts"][0]
    assert {key: alert[key] for key in ("unit", "threshold", "kind", "current", "page_id")} == {
        "unit": "usd",
        "threshold": 1.0,
        "kind": "absolute",
        "current": 1.2,
        "page_id": "doc_0001_page_0003",
    }

    log_entries = _read_jsonl(out_dir / "run.log")
    warning_entries = [entry for entry in log_entries if entry["budget_warning"]]
    assert [entry["page_id"] for entry in warning_entries] == [
        "doc_0001_page_0003",
        "doc_0001_page_0004",
    ]
    assert all(entry["level"] == "INFO" for entry in warning_entries)
    assert "usd=1.2" in warning_entries[0]["budget_warning"]
    assert alert["timestamp"] == warning_entries[0]["timestamp"]


def test_human_cli_prints_the_first_crossing_alert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger.cli import main

    source = tmp_path / "source.txt"
    source.write_text("page one\fpage two\fpage three", encoding="utf-8")
    config = _write_config(tmp_path / "pageledger.yml", budget={"warn_usd": 1.0})

    exit_code = main(
        [
            "run",
            str(source),
            "--config",
            str(config),
            "--out",
            str(tmp_path / "run"),
        ]
    )

    assert exit_code == 0
    assert (
        "WARNING: Budget alert at doc_0001_page_0003: usd=1.2 reached absolute threshold 1.0"
    ) in capsys.readouterr().out


def test_human_cli_prints_persisted_alert_before_terminal_budget_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger.cli import main

    source = tmp_path / "source.txt"
    source.write_text("page one\fpage two\fpage three", encoding="utf-8")
    config = _write_config(
        tmp_path / "pageledger.yml",
        budget={"warn_usd": 0.5, "max_usd": 1.0},
    )

    exit_code = main(
        [
            "run",
            str(source),
            "--config",
            str(config),
            "--out",
            str(tmp_path / "run"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        "WARNING: Budget alert at doc_0001_page_0002: "
        "usd=0.8 reached absolute threshold 0.5"
    ) in captured.out
    assert "Budget exceeded after doc_0001_page_0003" in captured.err


def test_warn_at_percent_still_emits_alert_and_budget_status(tmp_path: Path) -> None:
    out_dir, _result = _run_pages(
        tmp_path,
        page_count=4,
        budget={"max_usd": 2.0, "warn_at_percent": 50},
    )

    cost = _read_json(out_dir / "cost.json")
    assert cost["alerts"][0] | {"timestamp": "ignored"} == {
        "unit": "usd",
        "threshold": 1.0,
        "kind": "percent",
        "current": 1.2,
        "page_id": "doc_0001_page_0003",
        "timestamp": "ignored",
    }
    assert cost["budget"]["usd"] == {
        "max": 2.0,
        "current": 1.6,
        "exceeded": False,
        "warn_at": 1.0,
        "warning": True,
    }
    warning_entries = [
        entry for entry in _read_jsonl(out_dir / "run.log") if entry["budget_warning"]
    ]
    assert [entry["page_id"] for entry in warning_entries] == [
        "doc_0001_page_0003",
        "doc_0001_page_0004",
    ]


def test_alert_precedence_is_per_unit_and_records_only_first_crossing(
    tmp_path: Path,
) -> None:
    out_dir, _result = _run_pages(
        tmp_path,
        page_count=5,
        cost_usd=0.6,
        tokens=20,
        budget={
            "max_pages": 10,
            "max_tokens": 100,
            "max_usd": 4.0,
            "warn_at_percent": 50,
            "warn_pages": 2,
            "warn_tokens": 60,
            "warn_usd": 2.0,
        },
    )

    alerts = _read_json(out_dir / "cost.json")["alerts"]
    assert len(alerts) == 3
    by_unit = {alert["unit"]: alert for alert in alerts}
    assert set(by_unit) == {"pages", "tokens", "usd"}
    assert {key: by_unit["pages"][key] for key in ("threshold", "kind", "current", "page_id")} == {
        "threshold": 2,
        "kind": "absolute",
        "current": 2,
        "page_id": "doc_0001_page_0002",
    }
    assert {key: by_unit["tokens"][key] for key in ("threshold", "kind", "current", "page_id")} == {
        "threshold": 50.0,
        "kind": "percent",
        "current": 60,
        "page_id": "doc_0001_page_0003",
    }
    assert {key: by_unit["usd"][key] for key in ("threshold", "kind", "current", "page_id")} == {
        "threshold": 2.0,
        "kind": "absolute",
        "current": 2.4,
        "page_id": "doc_0001_page_0004",
    }
    assert [
        entry["page_id"] for entry in _read_jsonl(out_dir / "run.log") if entry["budget_warning"]
    ] == [
        "doc_0001_page_0002",
        "doc_0001_page_0003",
        "doc_0001_page_0004",
        "doc_0001_page_0005",
    ]


def test_routed_rollups_reconcile_extracted_pages_and_cost(tmp_path: Path) -> None:
    routes = {
        "schema_version": "0.1",
        "run_id": "mixed-route-plan",
        "generated_at": "2026-07-17T12:00:00Z",
        "classifier": {
            "adapter": "test:classifier",
            "model": "structural-test",
            "prompt_hash": None,
        },
        "documents": [
            {
                "source": "replaced-by-helper",
                "page_count": 4,
                "pages": [
                    {
                        "page_id": "doc_0001_page_0001",
                        "page_number": 1,
                        "type": "prose",
                        "confidence": 0.9,
                        "action": "transcribe_text",
                        "reason": "test_prose",
                    },
                    {
                        "page_id": "doc_0001_page_0002",
                        "page_number": 2,
                        "type": "blank",
                        "confidence": 0.95,
                        "action": "skip",
                        "reason": "test_blank",
                    },
                    {
                        "page_id": "doc_0001_page_0003",
                        "page_number": 3,
                        "type": "table_likely",
                        "confidence": 0.85,
                        "action": "transcribe_text",
                        "reason": "test_table",
                    },
                    {
                        "page_id": "doc_0001_page_0004",
                        "page_number": 4,
                        "type": "prose",
                        "confidence": 0.8,
                        "action": "transcribe_text",
                        "reason": "test_prose",
                    },
                ],
            }
        ],
    }
    out_dir, result = _run_pages(
        tmp_path,
        page_count=4,
        cost_usd=0.25,
        tokens=10,
        compute_seconds=0.2,
        routes=routes,
    )

    cost = _read_json(out_dir / "cost.json")
    assert result["summary"]["pages_extracted"] == 3
    assert result["summary"]["pages_skipped"] == 1
    assert cost["pages_extracted"] == 3
    assert cost["cost_usd"] == 0.75
    assert cost["cost_basis"] == "adapter_reported"
    assert cost["cost_known"] is True
    assert "alerts" not in cost

    assert set(cost["by_adapter"]) == {"metered"}
    adapter_rollup = cost["by_adapter"]["metered"]
    assert set(adapter_rollup) == {
        "pages",
        "tokens",
        "compute_seconds",
        "cost_usd",
        "cost_known",
    }
    assert adapter_rollup["pages"] == cost["pages_extracted"]
    assert adapter_rollup["tokens"] == cost["tokens_total"] == 30
    assert adapter_rollup["compute_seconds"] == pytest.approx(0.6)
    assert adapter_rollup["cost_usd"] == cost["cost_usd"]
    assert adapter_rollup["cost_known"] is True

    assert set(cost["by_page_type"]) == {"prose", "table_likely"}
    assert "blank" not in cost["by_page_type"]
    assert cost["by_page_type"]["prose"] == {
        "pages": 2,
        "tokens": 20,
        "compute_seconds": pytest.approx(0.4),
        "cost_usd": 0.5,
        "cost_known": True,
    }
    assert cost["by_page_type"]["table_likely"] == {
        "pages": 1,
        "tokens": 10,
        "compute_seconds": 0.2,
        "cost_usd": 0.25,
        "cost_known": True,
    }
    assert (
        sum(bucket["pages"] for bucket in cost["by_page_type"].values())
        == (cost["pages_extracted"])
    )
    assert sum(bucket["cost_usd"] for bucket in cost["by_page_type"].values()) == pytest.approx(
        cost["cost_usd"]
    )
