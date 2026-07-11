from __future__ import annotations

import hashlib
import json
import os
import py_compile
import sys
import types

import pytest
import yaml

import pageledger
import pageledger.adapters as adapters_module
import pageledger.config as config_module
import pageledger.runner as runner_module
from pageledger.adapters import ExtractionResult
from pageledger.cli import main
from pageledger.runner import AdapterExecutionError, BudgetExceededError, run


class CostlyAdapter:
    name = "costly"
    version = "0.1-test"
    deterministic = True

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def extract(
        self,
        source,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        _ = source, page_id, page_number, action, prompt
        return ExtractionResult(
            content="expensive result",
            format="text",
            confidence=None,
            model="cost-model",
            warnings=[],
            usage={
                "pages": 1,
                "tokens": 150,
                "compute_seconds": 0.01,
                "cost_usd": 1.25,
            },
        )


class PromptEchoAdapter:
    name = "prompt-echo"
    version = "0.1-test"
    deterministic = True

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def extract(
        self,
        source,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        _ = source, page_id, page_number, action
        return ExtractionResult(
            content=prompt or "",
            format="text",
            confidence=None,
            model="prompt-model",
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": 0.0},
        )


class BadUsageAdapter:
    name = "bad-usage"
    version = "0.1-test"
    deterministic = True

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def extract(
        self,
        source,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        _ = source, page_id, page_number, action, prompt
        return ExtractionResult(
            content="bad usage",
            format="text",
            confidence=None,
            model="bad-model",
            warnings=[],
            usage={"pages": 1, "cost_usd": object()},
        )


class BadFormatAdapter:
    name = "bad-format"
    version = "0.1-test"
    deterministic = True

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def extract(
        self,
        source,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        _ = source, page_id, page_number, action, prompt
        return ExtractionResult(
            content="bad format",
            format="../json",
            confidence=None,
            model="bad-model",
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": 0.0},
        )


class FlakyAdapter:
    name = "flaky"
    version = "0.1-test"
    deterministic = False

    def __init__(self) -> None:
        self.calls = 0

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def extract(
        self,
        source,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        _ = source, page_id, page_number, action, prompt
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary provider timeout")
        return ExtractionResult(
            content="retried result",
            format="text",
            confidence=None,
            model="flaky-model",
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": 0.0},
        )


def test_dry_run_writes_auditable_artifacts(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        schema:
          name: example
          columns:
            - name: text
        run:
          max_rerun_depth: 2
        """,
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    result = run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert result["dry_run"] is True
    assert (out_dir / "raw").is_dir()
    assert (out_dir / "normalized").is_dir()
    assert (out_dir / "config-snapshot.yml").read_text(encoding="utf-8") == config.read_text(
        encoding="utf-8"
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["execution_mode"] == "dry_run"
    assert manifest["execution_mode"] == "dry_run"
    assert manifest["artifacts"]["config_snapshot"] == "config-snapshot.yml"
    assert manifest["summary"]["pages_total"] == 1
    assert manifest["summary"]["pages_extracted"] == 0
    assert manifest["config"]["source_paths"] == [str(config.resolve())]

    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    page = route_map["documents"][0]["pages"][0]
    assert page["type"] == "prose"
    assert page["action"] == "review"
    assert page["reason"] == "no_classifier_available"

    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["quarantine_queue"] == []
    assert audit["review_queue"][0]["page_id"] == page["page_id"]

    rerun_manifest = yaml.safe_load(
        (out_dir / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert rerun_manifest["parent_run_id"] == manifest["run_id"]
    assert rerun_manifest["reason"] == "dry_run"
    assert rerun_manifest["max_rerun_depth"] == 2
    assert rerun_manifest["items"][0]["source"] == str(source.resolve())
    assert "previous_grade" in rerun_manifest["items"][0]


def test_package_exports_release_version():
    assert pageledger.__version__ == "0.1.7"


def test_dry_run_expands_directory_inputs_in_stable_order(tmp_path):
    input_dir = tmp_path / "scans"
    input_dir.mkdir()
    (input_dir / "b.txt").write_text("second", encoding="utf-8")
    (input_dir / "a.txt").write_text("first", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  max_rerun_depth: 2
""",
        encoding="utf-8",
    )

    run(inputs=[input_dir], config_path=config, out_dir=tmp_path / "run", dry_run=True)

    out_dir = tmp_path / "run"
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_total"] == 2
    assert [entry["path"] for entry in manifest["inputs"]] == [
        str(input_dir / "a.txt"),
        str(input_dir / "b.txt"),
    ]

    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    assert [document["source"] for document in route_map["documents"]] == [
        str((input_dir / "a.txt").resolve()),
        str((input_dir / "b.txt").resolve()),
    ]
    assert [page["page_id"] for document in route_map["documents"] for page in document["pages"]] == [
        "doc_0001_page_0001",
        "doc_0002_page_0001",
    ]


def test_run_rejects_missing_inputs_before_writing_artifacts(tmp_path):
    missing = tmp_path / "missing.txt"
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Input path does not exist"):
        run(inputs=[missing], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_run_rejects_empty_input_directories_before_writing_artifacts(tmp_path):
    input_dir = tmp_path / "empty"
    input_dir.mkdir()
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="No input files found"):
        run(inputs=[input_dir], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_run_rejects_non_empty_output_directory_before_writing_artifacts(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    sentinel = out_dir / "keep.txt"
    sentinel.write_text("existing run state", encoding="utf-8")

    with pytest.raises(ValueError, match="Output directory is not empty"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert sentinel.read_text(encoding="utf-8") == "existing run state"
    assert not (out_dir / "manifest.json").exists()


def test_run_rejects_output_path_that_is_a_file(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_path = tmp_path / "run"
    out_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="Output path exists and is not a directory"):
        run(inputs=[source], config_path=config, out_dir=out_path, dry_run=True)

    assert out_path.read_text(encoding="utf-8") == "not a directory"


def test_run_rejects_missing_config_before_writing_artifacts(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    missing_config = tmp_path / "missing.yml"
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Config path does not exist"):
        run(inputs=[source], config_path=missing_config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_dry_run_reads_dataset_citation_and_numeric_strings(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
        schema_version: "0.1"
        dataset_citation:
          label: census
          text: "USSR Census, 1926: Vol. III"
        taxonomy:
          page_types:
            table_data:
              default_action: vlm_table
        run:
          max_rerun_depth: "4"
        """,
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=True)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_citation"] == {
        "label": "census",
        "text": "USSR Census, 1926: Vol. III",
    }

    route_map = yaml.safe_load((tmp_path / "run" / "route-map.yml").read_text(encoding="utf-8"))
    assert route_map["documents"][0]["pages"][0]["type"] == "table_data"

    rerun_manifest = yaml.safe_load(
        (tmp_path / "run" / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert rerun_manifest["max_rerun_depth"] == 4


def test_dry_run_rejects_split_config_shape_for_consumed_fields(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "run-policy-shaped.yml"
    config.write_text(
        """
        schema_version: "0.1"
        page_types:
          figure:
            default_action: review
        max_rerun_depth: 7
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Use taxonomy.page_types"):
        run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=True)


def test_non_dry_run_extraction_action_requires_adapter(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="No configured adapter"):
        run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)


def test_non_dry_run_counts_configured_skip_pages(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("title page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    structural_metadata:
      default_action: skip
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"] == {
        "pages_total": 1,
        "pages_extracted": 0,
        "pages_skipped": 1,
        "pages_quarantined": 0,
        "records_normalized": 0,
        "estimated_cost_usd": 0.0,
        "quality_warning_pages": 0,
    }
    assert manifest["extractors"] == []
    assert not (tmp_path / "run" / "raw" / "doc_0001_page_0001.txt").exists()

    rerun_manifest = yaml.safe_load(
        (tmp_path / "run" / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert rerun_manifest["items"] == []
    log_event = json.loads((tmp_path / "run" / "run.log").read_text(encoding="utf-8"))
    assert log_event["status"] == "run_complete"
    assert log_event["pages_extracted"] == 0
    assert log_event["pages_skipped"] == 1


def test_non_dry_run_queues_configured_review_pages(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("uncertain page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    figure:
      default_action: review
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    out_dir = tmp_path / "run"
    assert not (out_dir / "raw" / "doc_0001_page_0001.txt").exists()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_total"] == 1
    assert manifest["summary"]["pages_extracted"] == 0
    assert manifest["summary"]["pages_skipped"] == 0

    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["review_queue"][0]["action"] == "review"
    assert audit["review_queue"][0]["reason"] == "configured_review"
    audit_markdown = (out_dir / "audit.md").read_text(encoding="utf-8")
    assert "| doc_0001_page_0001 | 1 | figure | review | configured_review |" in audit_markdown
    rerun_manifest = yaml.safe_load((out_dir / "rerun-manifest.yml").read_text(encoding="utf-8"))
    assert rerun_manifest["reason"] == "audit_policy"
    assert rerun_manifest["items"][0]["action"] == "review"
    log_event = json.loads((out_dir / "run.log").read_text(encoding="utf-8"))
    assert log_event["status"] == "run_complete"
    assert log_event["pages_extracted"] == 0
    assert log_event["pages_skipped"] == 0


def test_non_dry_run_rejects_unsupported_extraction_action_before_writing(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("table-ish text", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    table_data:
      default_action: vlm_table
run:
  adapter: text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Adapter 'text' does not support action 'vlm_table'"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not out_dir.exists()


def test_text_adapter_writes_raw_output_and_provenance(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page\nsecond line", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
          max_rerun_depth: 1
          pricing:
            text:
              input_per_million_usd: 0
              output_per_million_usd: 0
        """,
        encoding="utf-8",
    )

    result = run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    out_dir = tmp_path / "run"
    assert result["dry_run"] is False
    assert (out_dir / "raw" / "doc_0001_page_0001.txt").read_text(encoding="utf-8") == (
        "example page\nsecond line"
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["summary"]["pages_extracted"] == 1

    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    page = route_map["documents"][0]["pages"][0]
    assert page["action"] == "transcribe_text"
    assert page["reason"] == "configured_adapter"

    provenance_lines = (out_dir / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(provenance_lines) == 1
    provenance = json.loads(provenance_lines[0])
    assert provenance["page_id"] == "doc_0001_page_0001"
    assert provenance["extractor"]["adapter"] == "text"
    assert provenance["extractor"]["deterministic"] is True
    assert provenance["extractor"]["prompt_hash"]
    assert manifest["extractors"] == [
        {
            "name": "text",
            "adapter": "text",
            "model": None,
            "version": "0.1",
            "prompt_hash": provenance["extractor"]["prompt_hash"],
            "deterministic": True,
            "input_types": ["text"],
            "output_types": ["text"],
            "capabilities": ["embedded_text", "local"],
        }
    ]
    assert provenance["result"]["raw_artifact"] == "raw/doc_0001_page_0001.txt"
    assert provenance["usage"]["cost_usd"] is None
    assert provenance["usage"]["pages"] == 1
    assert set(provenance["usage"]) == {"pages", "tokens", "compute_seconds", "cost_usd"}
    assert provenance["metrics"] == provenance["usage"]
    cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
    assert cost == {
        "schema_version": "0.1",
        "run_id": manifest["run_id"],
        "execution_mode": "execute",
        "currency": "USD",
        "canonical_unit": "pages",
        "pages_extracted": 1,
        "tokens_total": 0,
        "pricing": {
            "text": {
                "input_per_million_usd": 0,
                "output_per_million_usd": 0,
            },
        },
        "usage": {
            "pages": 1,
            "tokens": None,
            "compute_seconds": None,
            "extraction_seconds": cost["usage"]["extraction_seconds"],
        },
            "cost_usd": None,
        "cost_known": False,
        "cost_basis": "none",
    }
    assert isinstance(cost["usage"]["extraction_seconds"], (int, float))

    log_lines = (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    log_event = json.loads(log_lines[0])
    assert log_event["page_id"] == "doc_0001_page_0001"
    assert log_event["adapter"] == "text"
    assert log_event["status"] == "extracted"
    rerun_manifest = yaml.safe_load((out_dir / "rerun-manifest.yml").read_text(encoding="utf-8"))
    assert rerun_manifest["reason"] == "audit_policy"
    assert rerun_manifest["items"] == []


def test_generated_usage_artifact_keys_match_unit_contract(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    usage_keys = {"pages", "tokens", "compute_seconds", "cost_usd"}
    provenance = json.loads((tmp_path / "run" / "provenance.jsonl").read_text(encoding="utf-8"))
    cost = json.loads((tmp_path / "run" / "cost.json").read_text(encoding="utf-8"))
    assert set(provenance["usage"]) == usage_keys
    assert set(provenance["metrics"]) == usage_keys
    assert set(cost["usage"]) == {"pages", "tokens", "compute_seconds", "extraction_seconds"}


def test_derived_cost_accumulation_is_stable_for_many_pages(tmp_path):
    source = tmp_path / "many.txt"
    source.write_text("\f".join(f"page {index}" for index in range(100)), encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
  pricing:
    cost_per_page: 0.001
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    cost = json.loads((tmp_path / "run" / "cost.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_extracted"] == 100
    assert manifest["summary"]["estimated_cost_usd"] == 0.1
    assert cost["cost_usd"] == 0.1


def test_configured_prompt_is_routed_to_adapter_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: PromptEchoAdapter())
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: PromptEchoAdapter())
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
      prompt: table-default-v1
run:
  adapter: prompt-echo
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert (out_dir / "raw" / "doc_0001_page_0001.txt").read_text(encoding="utf-8") == "table-default-v1"
    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    assert route_map["documents"][0]["pages"][0]["prompt"] == "table-default-v1"
    provenance = json.loads((out_dir / "provenance.jsonl").read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(b"table-default-v1").hexdigest()
    assert provenance["extractor"]["prompt_hash"] == expected_hash
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["extractors"][0]["prompt_hash"] == expected_hash


def test_retry_recovers_from_transient_adapter_failure(tmp_path, monkeypatch):
    adapter = FlakyAdapter()
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: adapter)
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: adapter)
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: flaky
  retry:
    max_retries: 1
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert (out_dir / "raw" / "doc_0001_page_0001.txt").read_text(encoding="utf-8") == "retried result"
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    log_events = [
        json.loads(line)
        for line in (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["status"] for event in log_events] == ["retry", "extracted"]
    assert log_events[0]["attempt"] == 1
    assert log_events[0]["max_retries"] == 1
    assert log_events[0]["error"]["status"] == "retry"
    assert "TimeoutError" in log_events[0]["error"]["message"]
    assert log_events[1]["attempt"] == 2


def test_log_level_filters_run_log_entries(tmp_path, monkeypatch):
    adapter = FlakyAdapter()
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: adapter)
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: adapter)
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: flaky
  retry:
    max_retries: 1
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False, log_level="WARNING")

    log_events = [
        json.loads(line)
        for line in (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["status"] for event in log_events] == ["retry"]
    assert log_events[0]["level"] == "WARNING"


def test_run_rejects_unknown_log_level_before_writing_artifacts(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="log_level must be one of"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True, log_level="LOUD")

    assert not out_dir.exists()


def test_adapter_failure_writes_failed_manifest_and_run_log(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_bytes(b"\xff\xfe\x00")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(AdapterExecutionError, match="Adapter 'text' failed"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["summary"]["pages_total"] == 1
    assert manifest["summary"]["pages_extracted"] == 0
    assert (out_dir / "provenance.jsonl").read_text(encoding="utf-8") == ""

    log_lines = (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    log_event = json.loads(log_lines[0])
    assert log_event["page_id"] == "doc_0001_page_0001"
    assert log_event["adapter"] == "text"
    assert log_event["status"] == "failed"
    assert log_event["error"]["adapter"] == "text"
    assert log_event["error"]["page_id"] == "doc_0001_page_0001"
    assert "UnicodeDecodeError" in log_event["error"]["message"]


def test_failed_run_preserves_full_pre_extraction_route_map(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    first.write_text("first page", encoding="utf-8")
    second.write_bytes(b"\xff\xfe\x00")
    third.write_text("third page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(AdapterExecutionError, match="Adapter 'text' failed"):
        run(inputs=[first, second, third], config_path=config, out_dir=out_dir, dry_run=False)

    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    assert [document["source"] for document in route_map["documents"]] == [
        str(first.resolve()),
        str(second.resolve()),
        str(third.resolve()),
    ]
    assert [page["page_id"] for document in route_map["documents"] for page in document["pages"]] == [
        "doc_0001_page_0001",
        "doc_0002_page_0001",
        "doc_0003_page_0001",
    ]
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["summary"]["pages_total"] == 3
    assert manifest["summary"]["pages_extracted"] == 1
    assert not (out_dir / "raw" / "doc_0003_page_0001.txt").exists()


def test_malformed_adapter_result_writes_failed_manifest_and_run_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: BadUsageAdapter())
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: BadUsageAdapter())
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: bad-usage
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(AdapterExecutionError, match="Adapter 'bad-usage' invalid_result"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not (out_dir / "raw" / "doc_0001_page_0001.txt").exists()
    assert (out_dir / "provenance.jsonl").read_text(encoding="utf-8") == ""
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    log_event = json.loads((out_dir / "run.log").read_text(encoding="utf-8"))
    assert log_event["status"] == "failed"
    assert log_event["error"]["status"] == "invalid_result"
    assert "usage must be JSON-serializable" in log_event["error"]["message"]


def test_path_like_adapter_format_writes_failed_manifest_and_run_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: BadFormatAdapter())
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: BadFormatAdapter())
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: bad-format
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(AdapterExecutionError, match="Adapter 'bad-format' invalid_result"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not any((out_dir / "raw").iterdir())
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    log_event = json.loads((out_dir / "run.log").read_text(encoding="utf-8"))
    assert log_event["status"] == "failed"
    assert "format must contain only letters, numbers, and underscores" in log_event["error"]["message"]


def test_budget_cap_stops_run_after_observed_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: CostlyAdapter())
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: CostlyAdapter())
    source = tmp_path / "sample.txt"
    source.write_text("costly page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: costly
  budget:
    max_usd: 1
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(RuntimeError, match="Budget exceeded after doc_0001_page_0001"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["summary"]["pages_extracted"] == 1
    assert manifest["summary"]["estimated_cost_usd"] == 1.25
    cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
    assert cost["cost_usd"] == 1.25
    assert cost["cost_known"] is True
    assert cost["budget"] == {"usd": {"max": 1.0, "current": 1.25, "exceeded": True}}
    log_lines = (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    log_event = json.loads(log_lines[0])
    assert log_event["status"] == "budget_exceeded"
    assert "max_usd=1.0" in log_event["error"]


def test_budget_warning_is_recorded_without_stopping_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: CostlyAdapter())
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: CostlyAdapter())
    source = tmp_path / "sample.txt"
    source.write_text("costly page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: costly
  budget:
    max_usd: 2
    warn_at_percent: 50
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
    assert cost["budget"] == {
        "usd": {
            "max": 2.0,
            "current": 1.25,
            "exceeded": False,
            "warn_at": 1.0,
            "warning": True,
        }
    }
    log_event = json.loads((out_dir / "run.log").read_text(encoding="utf-8"))
    assert log_event["status"] == "extracted"
    assert log_event["budget_warning"] == "usd=1.25 warn_at_usd=1.0"


def test_config_validation_reports_malformed_page_types(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
        schema_version: "0.1"
        taxonomy:
          page_types:
            - prose
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="taxonomy.page_types must be a mapping"):
        run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=True)


def test_config_validation_reports_malformed_yaml_before_writing(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text("schema_version: '0.1'\ntaxonomy: [\n", encoding="utf-8")
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Could not parse config YAML"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_config_validation_rejects_non_mapping_yaml_before_writing(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Config must be a YAML mapping"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_config_validation_rejects_invalid_budget_before_writing(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
run:
  budget:
    max_usd: -1
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="run.budget.max_usd must be non-negative"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_config_validation_rejects_invalid_budget_warning_before_writing(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
run:
  budget:
    max_usd: 10
    warn_at_percent: 120
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="run.budget.warn_at_percent must be between 0 and 100"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_config_validation_rejects_invalid_retry_before_writing(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
run:
  retry:
    max_retries: -1
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="run.retry.max_retries must be non-negative"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_config_validation_rejects_flat_page_types(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
        schema_version: "0.1"
        page_types:
          prose:
            default_action: transcribe_text
        run:
          adapter: text
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Use taxonomy.page_types"):
        run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)


def test_config_validation_reports_unsupported_adapter(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: imaginary
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported adapter 'imaginary'. Valid adapters: text, pdf_text"):
        run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)


def test_cli_json_output(tmp_path, capsys):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"

    exit_code = main(
        [
            "run",
            str(source),
            "--config",
            str(config),
            "--out",
            str(out_dir),
            "--dry-run",
            "--json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert output["execution_mode"] == "dry_run"
    assert output["out_dir"] == str(out_dir)
    assert output["status"] == "partial"


def test_cli_reports_value_errors_without_traceback(tmp_path, capsys):
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"

    exit_code = main(
        [
            "run",
            str(tmp_path / "missing.txt"),
            "--config",
            str(config),
            "--out",
            str(out_dir),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Input path does not exist" in captured.err
    assert "Traceback" not in captured.err
    assert not out_dir.exists()


def test_cli_run_help_describes_user_inputs(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "Input files or directories" in captured.out
    assert "PageLedger YAML config" in captured.out
    assert "New empty run directory" in captured.out


def test_sequential_runs_have_distinct_run_ids(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")

    first = run(inputs=[source], config_path=config, out_dir=tmp_path / "run-1", dry_run=True)
    second = run(inputs=[source], config_path=config, out_dir=tmp_path / "run-2", dry_run=True)

    assert first["run_id"] != second["run_id"]


def test_cli_reports_runtime_failures_without_traceback(tmp_path, capsys):
    source = tmp_path / "sample.txt"
    source.write_bytes(b"\xff\xfe\x00")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    exit_code = main(
        [
            "run",
            str(source),
            "--config",
            str(config),
            "--out",
            str(out_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Adapter 'text' failed for doc_0001_page_0001" in captured.err
    assert "Traceback" not in captured.err
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_paginates_text_source_on_form_feed(tmp_path):
    source = tmp_path / "multi.txt"
    source.write_text("page one\fpage two\fpage three", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text('schema_version: "0.1"\n', encoding="utf-8")
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_total"] == 3
    assert manifest["inputs"][0]["page_count"] == 3

    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    pages = route_map["documents"][0]["pages"]
    assert [page["page_id"] for page in pages] == [
        "doc_0001_page_0001",
        "doc_0001_page_0002",
        "doc_0001_page_0003",
    ]
    assert [page["page_number"] for page in pages] == [1, 2, 3]

    log_event = json.loads((out_dir / "run.log").read_text(encoding="utf-8"))
    assert log_event["pages_total"] == 3
    assert log_event["execution_mode"] == "dry_run"


def test_text_adapter_extracts_each_page_of_a_multi_page_source(tmp_path):
    source = tmp_path / "multi.txt"
    source.write_text("alpha\fbeta\fgamma", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["summary"]["pages_extracted"] == 3
    assert (out_dir / "raw" / "doc_0001_page_0001.txt").read_text(encoding="utf-8") == "alpha"
    assert (out_dir / "raw" / "doc_0001_page_0002.txt").read_text(encoding="utf-8") == "beta"
    assert (out_dir / "raw" / "doc_0001_page_0003.txt").read_text(encoding="utf-8") == "gamma"

    provenance = [
        json.loads(line)
        for line in (out_dir / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["page_id"] for entry in provenance] == [
        "doc_0001_page_0001",
        "doc_0001_page_0002",
        "doc_0001_page_0003",
    ]
    assert sum(entry["usage"]["pages"] for entry in provenance) == 3


def test_page_budget_cap_stops_multi_page_run(tmp_path):
    source = tmp_path / "multi.txt"
    source.write_text("one\ftwo\fthree\ffour", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
  budget:
    max_pages: 2
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(BudgetExceededError, match="Budget exceeded before extraction"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not out_dir.exists()


def test_dry_run_does_not_load_configured_adapter(tmp_path, monkeypatch):
    def fail_load_adapter(name):
        raise AssertionError(f"adapter should not load during dry-run: {name}")

    monkeypatch.setattr(config_module, "load_adapter", fail_load_adapter)
    monkeypatch.setattr(runner_module, "load_adapter", fail_load_adapter)
    source = tmp_path / "sample.txt"
    source.write_text("example page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: imaginary
""",
        encoding="utf-8",
    )

    result = run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=True)

    assert result["execution_mode"] == "dry_run"


def test_pdf_adapter_reports_missing_optional_dependency_cleanly(tmp_path, monkeypatch):
    def missing_pypdf():
        raise ValueError("PDF support requires the optional dependency: install pageledger[pdf]")

    monkeypatch.setattr(adapters_module, "_load_pypdf", missing_pypdf)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match=r"pageledger\[pdf\]"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not out_dir.exists()


def test_pdf_adapter_extracts_born_digital_pdf_when_extra_is_installed(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "sample.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"][0]["page_count"] == 1
    assert manifest["summary"]["pages_extracted"] == 1
    assert (out_dir / "raw" / "doc_0001_page_0001.txt").exists()
    provenance = json.loads((out_dir / "provenance.jsonl").read_text(encoding="utf-8"))
    assert provenance["extractor"]["adapter"] == "pdf_text"
    assert provenance["usage"]["pages"] == 1


def test_pdf_dry_run_counts_real_pdf_pages_when_extra_is_installed(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "sample.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=True)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    audit = json.loads((tmp_path / "run" / "audit.json").read_text(encoding="utf-8"))
    assert manifest["inputs"][0]["page_count"] == 2
    assert manifest["summary"]["pages_total"] == 2
    assert len(audit["review_queue"]) == 2


def test_pdf_dry_run_without_adapter_counts_real_pdf_pages_when_extra_is_installed(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "sample.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: review
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=True)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    audit = json.loads((tmp_path / "run" / "audit.json").read_text(encoding="utf-8"))
    assert manifest["inputs"][0]["page_count"] == 3
    assert manifest["summary"]["pages_total"] == 3
    assert len(audit["review_queue"]) == 3


def test_pdf_dry_run_reports_missing_pdf_extra_before_writing(tmp_path, monkeypatch):
    def missing_pypdf():
        raise ValueError("PDF support requires the optional dependency: install pageledger[pdf]")

    monkeypatch.setattr(adapters_module, "_load_pypdf", missing_pypdf)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match=r"pageledger\[pdf\]"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_text_adapter_rejects_pdf_before_writing_run_directory(tmp_path):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Adapter 'text' cannot read PDF input"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not out_dir.exists()


def test_pdf_adapter_rejects_non_pdf_before_writing_run_directory(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("sample", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="only reads PDF inputs"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)

    assert not out_dir.exists()


def test_custom_adapter_spec_loads_valid_adapter(monkeypatch):
    module = types.ModuleType("pageledger_test_custom_adapter")

    class CustomAdapter:
        name = "custom-test"
        version = "0.1-test"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("custom",)

        def supports(self, action: str) -> bool:
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            _ = source, page_id, page_number, action, prompt
            return ExtractionResult(
                content="custom",
                format="text",
                confidence=None,
                model="custom-model",
                warnings=[],
                usage={
                    "pages": 1,
                    "tokens": None,
                    "compute_seconds": None,
                    "cost_usd": None,
                },
            )

    module.CustomAdapter = CustomAdapter
    monkeypatch.setitem(sys.modules, module.__name__, module)

    adapter = adapters_module.load_adapter(f"{module.__name__}:CustomAdapter")

    assert adapter.name == "custom-test"
    assert adapter.version == "0.1-test"
    assert adapter.supports("transcribe_text") is True


def test_custom_adapter_spec_rejects_invalid_object(monkeypatch):
    module = types.ModuleType("pageledger_test_bad_custom_adapter")
    module.not_an_adapter = object()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(ValueError, match="must expose supports"):
        adapters_module.load_adapter(f"{module.__name__}:not_an_adapter")


def test_cli_doctor_json_redacts_environment_values(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret-test-value")

    assert main(["doctor", "--json"]) == 0

    captured = capsys.readouterr()
    assert "super-secret-test-value" not in captured.out
    report = json.loads(captured.out)
    assert report["pageledger_version"] == pageledger.__version__
    assert report["optional_packages"]["pypdf"]["available"] in {True, False}
    assert report["external_commands"]["tesseract"]["available"] in {True, False}
    assert report["cloud_environment"]["GOOGLE_API_KEY"]["set"] is True
    assert report["cloud_environment"]["GOOGLE_API_KEY"]["value"] == "<redacted>"
    assert report["cloud_environment"]["GOOGLE_API_KEY"]["explanation"]


def test_builtin_adapters_expose_capability_metadata():
    text = adapters_module.load_adapter("text")
    pdf = adapters_module.load_adapter("pdf_text")

    assert list(text.input_types) == ["text"]
    assert list(text.output_types) == ["text"]
    assert "embedded_text" in text.capabilities
    assert text.page_count is not None
    assert list(pdf.input_types) == ["pdf"]
    assert "embedded_text" in pdf.capabilities
    assert pdf.page_count is not None


def test_custom_adapter_page_count_hook_controls_planned_pages(tmp_path, monkeypatch):
    class ThreePagePdfAdapter:
        name = "three-page-pdf"
        version = "0.1-test"
        deterministic = True
        input_types = ["pdf"]
        output_types = ["text"]
        capabilities = ["ocr", "local"]

        def supports(self, action: str) -> bool:
            return action == "transcribe_text"

        def page_count(self, source):
            _ = source
            return 3

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            _ = source, page_id, action, prompt
            return ExtractionResult(
                content=f"ocr text page {page_number}",
                format="text",
                confidence=None,
                model="test-ocr",
                warnings=[],
                usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            )

    adapter = ThreePagePdfAdapter()
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: adapter)
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: adapter)
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\nplaceholder")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: three-page-pdf
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"][0]["page_count"] == 3
    assert manifest["summary"]["pages_total"] == 3
    assert manifest["summary"]["pages_extracted"] == 3
    assert len(list((tmp_path / "run" / "raw").glob("*.txt"))) == 3


def test_custom_adapter_without_page_count_keeps_one_page_backcompat(tmp_path, monkeypatch):
    class OpaqueAdapter:
        name = "opaque"
        version = "0.1-test"
        deterministic = False

        def supports(self, action: str) -> bool:
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            _ = source, page_id, page_number, action, prompt
            return ExtractionResult(
                content="opaque output",
                format="text",
                confidence=None,
                model=None,
                warnings=[],
                usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            )

    adapter = OpaqueAdapter()
    monkeypatch.setattr(config_module, "load_adapter", lambda name, options=None: adapter)
    monkeypatch.setattr(runner_module, "load_adapter", lambda name, options=None: adapter)
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\nplaceholder")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: opaque
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"][0]["page_count"] == 1
    assert manifest["summary"]["pages_total"] == 1


def test_quality_summary_is_written_for_adapter_runs(tmp_path):
    source = tmp_path / "multi.txt"
    source.write_text("hello world\n\nshort", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    quality_lines = (tmp_path / "run" / "quality.jsonl").read_text(encoding="utf-8").splitlines()
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"]["quality"] == "quality.jsonl"
    assert len(quality_lines) == 1
    quality = json.loads(quality_lines[0])
    assert quality["page_id"] == "doc_0001_page_0001"
    assert quality["character_count"] == len("hello world\n\nshort")
    assert quality["word_count"] == 3
    assert quality["warnings"] == []


def test_quality_summary_flags_short_pages(tmp_path):
    source = tmp_path / "short.txt"
    source.write_text("x", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    quality = json.loads((tmp_path / "run" / "quality.jsonl").read_text(encoding="utf-8"))
    assert quality["warnings"] == ["short_text"]


def test_quality_summary_flags_text_noise(tmp_path):
    source = tmp_path / "noisy.txt"
    source.write_text("hello\ufffdthere\x00ok", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    quality = json.loads((tmp_path / "run" / "quality.jsonl").read_text(encoding="utf-8"))
    assert "replacement_characters" in quality["warnings"]
    assert "control_characters" in quality["warnings"]
    assert quality["text_quality"]["replacement_character_count"] == 1
    assert quality["text_quality"]["control_character_count"] == 1


def test_quality_summary_flags_suspicious_symbol_density(tmp_path):
    source = tmp_path / "symbols.txt"
    source.write_text("TOP SECRET _____ {S==6CQl_ text", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
""",
        encoding="utf-8",
    )

    run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)

    quality = json.loads((tmp_path / "run" / "quality.jsonl").read_text(encoding="utf-8"))
    assert "suspicious_symbol_density" in quality["warnings"]
    assert quality["text_quality"]["suspicious_symbol_count"] >= 5


def test_doctor_reports_command_versions_and_redacted_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-print-me")
    report = pageledger.doctor.build_doctor_report()

    assert "runtime" in report["python"]
    assert report["external_commands"]["pdftoppm"]["version"] is None or isinstance(
        report["external_commands"]["pdftoppm"]["version"], str
    )
    assert report["external_commands"]["pdftoppm"]["install_hint"]
    assert report["external_commands"]["pdftoppm"]["explanation"]
    assert report["cloud_environment"]["OPENAI_API_KEY"]["value"] == "<redacted>"
    assert "do-not-print-me" not in json.dumps(report)


def test_docs_examples_smoke_without_heavy_ocr_installs():
    root = runner_module.Path(__file__).resolve().parents[2]
    for example in [
        root / "examples" / "tesseract_pdftoppm_adapter.py",
        root / "examples" / "cloud_vlm_adapter_skeleton.py",
    ]:
        py_compile.compile(str(example), doraise=True)

    ocrmypdf_example = (root / "examples" / "ocrmypdf_preprocess.sh").read_text(
        encoding="utf-8"
    )
    assert "ocrmypdf --skip-text" in ocrmypdf_example
    assert os.access(root / "examples" / "ocrmypdf_preprocess.sh", os.X_OK)

    ocr_options = (root / "docs" / "ocr-options.md").read_text(encoding="utf-8")
    assert "PageLedger is provider-agnostic" in ocr_options
    assert "Hybrid" in ocr_options
    assert "Do not hard-code provider pricing" in ocr_options

    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "docs/ocr-options.md" in readme

    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include docs" in manifest
    assert "recursive-include examples" in manifest


def test_wheel_configuration_includes_schema_contracts():
    root = runner_module.Path(__file__).resolve().parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert '"share/pageledger/schemas"' in pyproject
    assert '"schemas/*.json"' in pyproject


def test_doctor_reports_ocr_languages(monkeypatch):
    import subprocess as subprocess_module

    import pageledger.adapters

    def fake_which(name):
        return "/fake/bin/tesseract" if name == "tesseract" else None

    def fake_run(argv, **kwargs):
        if "--list-langs" in argv:
            body = 'List of available languages in "/fake/tessdata/" (3):\r\neng\r\nrus\r\n\nosd\n'
            return subprocess_module.CompletedProcess(argv, 0, stdout=body, stderr="")
        return subprocess_module.CompletedProcess(argv, 0, stdout="tesseract 5.5.2\n", stderr="")

    monkeypatch.setattr(pageledger.doctor.shutil, "which", fake_which)
    monkeypatch.setattr(pageledger.adapters.subprocess, "run", fake_run)
    report = pageledger.doctor.build_doctor_report()

    assert report["ocr_languages"]["available"] is True
    assert report["ocr_languages"]["languages"] == ["eng", "osd", "rus"]


def test_doctor_ocr_languages_unavailable_without_tesseract(monkeypatch):
    monkeypatch.setattr(pageledger.doctor.shutil, "which", lambda name: None)
    report = pageledger.doctor.build_doctor_report()

    assert report["ocr_languages"]["available"] is False
    assert report["ocr_languages"]["languages"] == []
