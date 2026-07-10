"""Strict configuration and adapter contracts for the 0.1 artifact line."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pageledger.config import load_config


def _load(tmp_path: Path, text: str):
    path = tmp_path / "pageledger.yml"
    path.write_text(text, encoding="utf-8")
    return load_config(path, validate_adapter=False)


def test_schema_version_defaults_to_0_1_when_omitted(tmp_path: Path) -> None:
    assert _load(tmp_path, "{}\n").schema_version == "0.1"


@pytest.mark.parametrize("version", ["0.2", "1", "latest"])
def test_explicit_unsupported_schema_version_is_rejected(
    tmp_path: Path, version: str
) -> None:
    with pytest.raises(ValueError, match="Unsupported schema_version"):
        _load(tmp_path, f"schema_version: '{version}'\n")


@pytest.mark.parametrize(
    ("yaml_text", "path"),
    [
        ("run: []\n", "run"),
        ("run:\n  budget: []\n", "run.budget"),
        ("run:\n  retry: []\n", "run.retry"),
        ("run:\n  pricing: []\n", "run.pricing"),
        ("run:\n  grading: []\n", "run.grading"),
        ("dataset_citation: []\n", "dataset_citation"),
        ("schema: []\n", "schema"),
    ],
)
def test_owned_config_sections_must_be_mappings(
    tmp_path: Path, yaml_text: str, path: str
) -> None:
    with pytest.raises(ValueError, match=rf"{path} must be a mapping"):
        _load(tmp_path, yaml_text)


def test_unknown_run_keys_warn_but_adapter_options_remain_open(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        """
run:
  adapter_options:
    provider_private:
      any_shape: [is, allowed]
  future_switch: true
  future_policy: {}
""",
    )

    assert config.adapter_options == {
        "provider_private": {"any_shape": ["is", "allowed"]}
    }
    assert any("Unknown run key 'future_switch'" in item for item in config.warnings)
    assert any("Unknown run key 'future_policy'" in item for item in config.warnings)
    assert not any("provider_private" in item for item in config.warnings)


def test_canonical_numeric_strings_are_accepted(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        """
run:
  max_rerun_depth: "4"
  budget:
    max_pages: "12"
    max_tokens: "500"
    max_usd: "1.25"
    warn_at_percent: "80"
  retry:
    max_retries: "2"
  pricing:
    cost_per_page: "0.002"
    cost_per_1k_tokens: "1e-2"
""",
    )

    assert config.max_rerun_depth == 4
    assert config.budget_max_pages == 12
    assert config.budget_max_tokens == 500
    assert config.budget_max_usd == 1.25
    assert config.budget_warn_at_percent == 80.0
    assert config.max_retries == 2
    assert config.cost_per_page == 0.002
    assert math.isclose(config.cost_per_1k_tokens or 0, 0.01)


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("run: {max_rerun_depth: true}\n", "must be an integer"),
        ("run: {max_rerun_depth: 1.5}\n", "must be an integer"),
        ("run: {budget: {max_pages: true}}\n", "must be an integer"),
        ("run: {budget: {max_pages: 1.5}}\n", "must be an integer"),
        ("run: {retry: {max_retries: true}}\n", "must be an integer"),
        ("run: {budget: {max_usd: .nan}}\n", "must be a finite number"),
        ("run: {budget: {max_usd: .inf}}\n", "must be a finite number"),
        ("run: {pricing: {cost_per_page: .inf}}\n", "must be a finite number"),
    ],
)
def test_bool_fractional_integer_and_nonfinite_numbers_are_rejected(
    tmp_path: Path, yaml_text: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, yaml_text)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "run: {max_rerun_depth: '01'}\n",
        "run: {budget: {max_usd: ' 1.0'}}\n",
        "run: {pricing: {cost_per_page: 'NaN'}}\n",
    ],
)
def test_noncanonical_numeric_strings_are_rejected(
    tmp_path: Path, yaml_text: str
) -> None:
    with pytest.raises(ValueError, match="must be"):
        _load(tmp_path, yaml_text)


def test_dataset_citation_values_must_be_strings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dataset_citation.label must be a string"):
        _load(tmp_path, "dataset_citation: {label: true}\n")
