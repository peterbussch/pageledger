"""Cost provenance (cost_basis), measured extraction time, and retry backoff.

Verifies:
  - cost_basis: "none" for free engines without pricing, "configured_rate"
    when unit rates apply, "adapter_reported" when the adapter passes cost
    through, "mixed" when both occur in one run
  - extraction_seconds appears per-page in provenance and rolled up in cost.json
  - retry.backoff: "exponential" sleeps between attempts; "none" (default) does not
  - invalid retry.backoff values are rejected at config load
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


BASE = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""

PRICED = BASE + """\
  pricing:
    cost_per_page: 0.002
"""


class _CostReportingAdapter:
    """Adapter that reports its own cost_usd, like a cloud provider would."""

    name = "cost-reporter"
    version = "test"
    deterministic = True
    input_types = ("text",)
    output_types = ("text",)
    capabilities = ("test",)

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        from pageledger.adapters import ExtractionResult

        return ExtractionResult(
            content=f"page {page_number} content with plenty of text",
            format="text",
            confidence=None,
            model="test-model",
            warnings=[],
            usage={"pages": 1, "tokens": 100, "compute_seconds": None, "cost_usd": 0.01},
        )


COST_ADAPTER = _CostReportingAdapter()


def _run(inputs, config_text, tmp_path, out_name="out"):
    from pageledger.runner import run

    config_path = tmp_path / f"{out_name}.yml"
    config_path.write_text(config_text, encoding="utf-8")
    out_dir = tmp_path / out_name
    run(inputs=inputs, config_path=config_path, out_dir=out_dir, dry_run=False)
    return out_dir


def _cost(out_dir: Path) -> dict:
    return json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))


def test_cost_basis_none_for_free_engine_without_pricing(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("clean page with plenty of text\n", encoding="utf-8")
    out_dir = _run([source], BASE, tmp_path)
    cost = _cost(out_dir)
    assert cost["cost_basis"] == "none"
    assert cost["cost_usd"] is None
    assert cost["cost_known"] is False


def test_cost_basis_configured_rate(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("clean page with plenty of text\n", encoding="utf-8")
    out_dir = _run([source], PRICED, tmp_path)
    cost = _cost(out_dir)
    assert cost["cost_basis"] == "configured_rate"
    assert cost["cost_usd"] == 0.002
    assert cost["cost_known"] is True


def test_cost_basis_adapter_reported(tmp_path):
    config = BASE.replace("adapter: text", "adapter: test_cost_basis:COST_ADAPTER")
    source = tmp_path / "doc.txt"
    source.write_text("clean page with plenty of text\n", encoding="utf-8")
    out_dir = _run([source], config, tmp_path)
    cost = _cost(out_dir)
    assert cost["cost_basis"] == "adapter_reported"
    assert cost["cost_usd"] == 0.01


def test_adapter_reported_cost_beats_configured_rate(tmp_path):
    """Adapter passthrough wins over unit rates; basis says so."""
    config = PRICED.replace("adapter: text", "adapter: test_cost_basis:COST_ADAPTER")
    source = tmp_path / "doc.txt"
    source.write_text("clean page with plenty of text\n", encoding="utf-8")
    out_dir = _run([source], config, tmp_path)
    cost = _cost(out_dir)
    assert cost["cost_basis"] == "adapter_reported"
    assert cost["cost_usd"] == 0.01  # not 0.002


def test_extraction_seconds_in_provenance_and_rollup(tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("page one text\fpage two text\n", encoding="utf-8")
    out_dir = _run([source], BASE, tmp_path)
    lines = [
        json.loads(line)
        for line in (out_dir / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    for line in lines:
        assert isinstance(line["extraction_seconds"], (int, float))
        assert line["extraction_seconds"] >= 0
    cost = _cost(out_dir)
    assert isinstance(cost["usage"]["extraction_seconds"], (int, float))


def test_backoff_exponential_sleeps_between_retries(tmp_path, monkeypatch):
    from pageledger import runner as runner_module

    sleeps: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: sleeps.append(s))

    config = BASE.replace(
        "adapter: text", "adapter: test_cost_basis:ALWAYS_FAILS"
    ) + """\
  retry:
    max_retries: 3
    backoff: exponential
"""
    source = tmp_path / "doc.txt"
    source.write_text("some page text\n", encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(config, encoding="utf-8")
    with pytest.raises(RuntimeError):
        runner_module.run(
            inputs=[source],
            config_path=config_path,
            out_dir=tmp_path / "out",
            dry_run=False,
        )
    # 3 retries → sleeps before attempts 2, 3, 4: 0.5, 1.0, 2.0
    assert sleeps == [0.5, 1.0, 2.0]


def test_backoff_default_none_does_not_sleep(tmp_path, monkeypatch):
    from pageledger import runner as runner_module

    sleeps: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: sleeps.append(s))

    config = BASE.replace(
        "adapter: text", "adapter: test_cost_basis:ALWAYS_FAILS"
    ) + """\
  retry:
    max_retries: 2
"""
    source = tmp_path / "doc.txt"
    source.write_text("some page text\n", encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(config, encoding="utf-8")
    with pytest.raises(RuntimeError):
        runner_module.run(
            inputs=[source],
            config_path=config_path,
            out_dir=tmp_path / "out",
            dry_run=False,
        )
    assert sleeps == []


def test_backoff_invalid_value_rejected(tmp_path):
    from pageledger.config import load_config

    config = BASE + """\
  retry:
    max_retries: 1
    backoff: fibonacci
"""
    config_path = tmp_path / "config.yml"
    config_path.write_text(config, encoding="utf-8")
    with pytest.raises(ValueError, match="backoff"):
        load_config(config_path, validate_adapter=False)


class _AlwaysFailsAdapter:
    name = "always-fails"
    version = "test"
    deterministic = True
    input_types = ("text",)
    output_types = ("text",)
    capabilities = ("test",)

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        raise RuntimeError("simulated transient failure")


ALWAYS_FAILS = _AlwaysFailsAdapter()
