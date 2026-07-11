"""Policy grammar validation, evaluation, and run integration."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from pageledger.config import load_config
from pageledger.policy import evaluate_policies


def _load(tmp_path: Path, policy_yaml: str):
    path = tmp_path / "pageledger.yml"
    path.write_text(
        f'schema_version: "0.1"\nrun:\n{policy_yaml}',
        encoding="utf-8",
    )
    return load_config(path, validate_adapter=False)


def test_policy_rules_default_to_empty_lists(tmp_path: Path) -> None:
    config = _load(tmp_path, "  adapter: text\n")
    assert config.rerun_rules == []
    assert config.quarantine_rules == []


def test_all_policy_predicates_validate(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        """\
  rerun_if:
    - grade_below: C
    - missing_required_columns: true
    - arithmetic_failure_rate_above: 0.05
  quarantine_if:
    - grade_below: F
""",
    )
    assert config.rerun_rules == [
        {"grade_below": "C"},
        {"missing_required_columns": True},
        {"arithmetic_failure_rate_above": 0.05},
    ]
    assert config.quarantine_rules == [{"grade_below": "F"}]
    assert not any("rerun_if" in warning for warning in config.warnings)
    assert not any("quarantine_if" in warning for warning in config.warnings)


@pytest.mark.parametrize(
    ("policy_yaml", "path"),
    [
        ("  rerun_if: {grade_below: C}\n", "run.rerun_if"),
        ("  rerun_if: [grade_below]\n", "run.rerun_if[0]"),
        (
            "  rerun_if:\n    - {grade_below: C, missing_required_columns: true}\n",
            "run.rerun_if[0]",
        ),
        (
            "  rerun_if:\n    - future_predicate: true\n",
            "run.rerun_if[0].future_predicate",
        ),
        (
            "  quarantine_if:\n    - grade_below: c\n",
            "run.quarantine_if[0].grade_below",
        ),
        (
            "  quarantine_if:\n    - missing_required_columns: false\n",
            "run.quarantine_if[0].missing_required_columns",
        ),
        (
            "  rerun_if:\n    - arithmetic_failure_rate_above: true\n",
            "run.rerun_if[0].arithmetic_failure_rate_above",
        ),
        (
            "  rerun_if:\n    - arithmetic_failure_rate_above: .nan\n",
            "run.rerun_if[0].arithmetic_failure_rate_above",
        ),
        (
            "  rerun_if:\n    - arithmetic_failure_rate_above: -0.01\n",
            "run.rerun_if[0].arithmetic_failure_rate_above",
        ),
        (
            "  rerun_if:\n    - arithmetic_failure_rate_above: 1.01\n",
            "run.rerun_if[0].arithmetic_failure_rate_above",
        ),
    ],
)
def test_invalid_policy_rules_report_the_key_path(
    tmp_path: Path,
    policy_yaml: str,
    path: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(path)):
        _load(tmp_path, policy_yaml)


@pytest.mark.parametrize(
    ("rules", "grade", "alignment", "matches"),
    [
        ([{"grade_below": "C"}], "D", None, ["grade_below"]),
        ([{"grade_below": "C"}], "C", None, []),
        (
            [{"missing_required_columns": True}],
            "A",
            {"columns": {"missing_required": ["population_total"]}, "metrics": {}},
            ["missing_required_columns"],
        ),
        ([{"missing_required_columns": True}], "A", None, []),
        (
            [{"arithmetic_failure_rate_above": 0.05}],
            "A",
            {"columns": {}, "metrics": {"arithmetic_pass_rate": 0.94}},
            ["arithmetic_failure_rate_above"],
        ),
        (
            [{"arithmetic_failure_rate_above": 0.05}],
            "A",
            {"columns": {}, "metrics": {"arithmetic_pass_rate": 0.95}},
            [],
        ),
        (
            [{"arithmetic_failure_rate_above": 0.05}],
            "A",
            {"columns": {}, "metrics": {"arithmetic_pass_rate": None}},
            [],
        ),
    ],
)
def test_evaluate_policies(
    rules: list[dict],
    grade: str,
    alignment: dict | None,
    matches: list[str],
) -> None:
    assert evaluate_policies(rules, grade=grade, alignment=alignment) == matches


_TABLE_ADAPTER = '''\
from pageledger.adapters import ExtractionResult


class TableAdapter:
    name = "policy_table"
    version = "0.1"
    deterministic = True
    input_types = ("text",)
    output_types = ("markdown_table",)
    capabilities = ("local",)

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        pages = source.read_text(encoding="utf-8").split("\\f")
        return ExtractionResult(
            content=pages[page_number - 1],
            format="markdown_table",
            confidence=0.95,
            model=None,
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": None,
                "cost_usd": None,
            },
        )
'''

_POLICY_CONFIG = """\
schema_version: "0.1"
taxonomy:
  page_types:
    table:
      default_action: transcribe_text
schema:
  name: population
  columns:
    - {name: place, type: string, required: true}
    - {name: total, type: integer, required: true}
    - {name: male, type: integer}
    - {name: female, type: integer}
  checks:
    - {name: population_sum, expression: total == male + female}
run:
  adapter: policy_adapter:TableAdapter
  grading:
    review_below_grade: C
  rerun_if:
    - grade_below: C
    - missing_required_columns: true
    - arithmetic_failure_rate_above: 0.05
  quarantine_if:
    - missing_required_columns: true
"""


def test_run_policies_populate_queues_and_quarantine_beats_rerun(
    tmp_path: Path,
) -> None:
    from pageledger.runner import run
    from pageledger.verify import verify_run

    (tmp_path / "policy_adapter.py").write_text(_TABLE_ADAPTER, encoding="utf-8")
    good = "| place | total | male | female |\n| - | - | - | - |\n| A | 10 | 4 | 6 |\n"
    missing = "| place | male | female |\n| - | - | - |\n| B | 4 | 6 |\n"
    arithmetic_failure = (
        "| place | total | male | female |\n"
        "| - | - | - | - |\n"
        "| C | 99 | 4 | 6 |\n"
    )
    source = tmp_path / "tables.txt"
    source.write_text(
        "\f".join((good, missing, arithmetic_failure)),
        encoding="utf-8",
    )
    config_path = tmp_path / "policy.yml"
    config_path.write_text(_POLICY_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"

    run(
        inputs=[source],
        config_path=config_path,
        out_dir=out_dir,
        dry_run=False,
        adapter_path=tmp_path,
    )

    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    quarantined = audit["quarantine_queue"]
    assert len(quarantined) == 1
    assert quarantined[0]["page_number"] == 2
    assert quarantined[0]["reason"] == "quarantine_if:missing_required_columns"
    assert quarantined[0]["action"] == "quarantine"

    review_reasons = {
        item["reason"]
        for item in audit["review_queue"]
        if item["page_number"] == 2
    }
    assert {
        "grade_below_threshold",
        "rerun_if:grade_below",
        "rerun_if:missing_required_columns",
    } <= review_reasons

    rerun = yaml.safe_load(
        (out_dir / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert {item["page_number"] for item in rerun["items"]} == {3}
    assert "rerun_if:arithmetic_failure_rate_above" in rerun["items"][0]["reason"]

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_quarantined"] == 1
    audit_markdown = (out_dir / "audit.md").read_text(encoding="utf-8")
    assert "quarantine_if:missing_required_columns" in audit_markdown
    assert verify_run(out_dir)["status"] == "pass"


def test_align_recomputes_rerun_and_quarantine_policies(tmp_path: Path) -> None:
    from pageledger.aligner import align_run
    from pageledger.runner import run

    (tmp_path / "policy_adapter.py").write_text(_TABLE_ADAPTER, encoding="utf-8")
    source = tmp_path / "table.txt"
    source.write_text(
        "| place | male | female |\n| - | - | - |\n| B | 4 | 6 |\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "policy.yml"
    config_path.write_text(_POLICY_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    run(
        inputs=[source],
        config_path=config_path,
        out_dir=out_dir,
        dry_run=False,
        adapter_path=tmp_path,
    )
    before = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert before["quarantine_queue"]

    schema_path = tmp_path / "schema-v2.yml"
    schema_path.write_text(
        """\
name: population_v2
columns:
  - {name: place, type: string, required: true}
  - {name: male, type: integer}
  - {name: female, type: integer}
""",
        encoding="utf-8",
    )
    align_run(out_dir, schema_path=schema_path)

    after = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert after["quarantine_queue"] == []
    assert not any(
        item["reason"] == "rerun_if:missing_required_columns"
        for item in after["review_queue"]
    )
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_quarantined"] == 0
    from pageledger.verify import verify_run
    assert verify_run(out_dir)["status"] == "pass"
