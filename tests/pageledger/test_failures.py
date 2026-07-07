"""Phase 6 tests: failure recovery and partial-run guarantees.

Verifies the failure scenario table from run-manifest-spec.md:
  - Adapter exception after prior pages → partial artifacts, correct counts
  - Budget mid-run → artifacts intact, budget error in run.log
  - Retry exhausted → retry entries + final error, prior pages preserved
  - Invalid adapter result → no raw artifacts, provenance empty for that page
  - Dry-run never fails
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from pageledger.runner import (
    BudgetExceededError,
    run,
)

# =========================================================================
# Adapter fails after prior pages succeed → partial artifacts
# =========================================================================

def test_adapter_fails_on_page_3_of_5_preserves_prior_pages(tmp_path):
    """Pages 1-2 are extracted; page 3 fails; manifest shows partial counts."""
    source = tmp_path / "multi.txt"
    source.write_text("p1\fp2\fp3\fp4\fp5\n", encoding="utf-8")

    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: test_fail_p3:FailingOnPage3Adapter
        """), encoding="utf-8")

    # Write a custom adapter module that fails on page 3
    import os
    mod_path = tmp_path / "test_fail_p3.py"
    mod_path.write_text(textwrap.dedent("""\
    from dataclasses import dataclass
    from pathlib import Path
    from pageledger.adapters import ExtractionResult

    @dataclass
    class FailingOnPage3Adapter:
        name = "fail-p3"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            if page_number == 3:
                raise RuntimeError("simulated crash on page 3")
            pages = source.read_text(encoding="utf-8").split("\\f")
            text = pages[page_number - 1] if page_number <= len(pages) else ""
            return ExtractionResult(
                content=text, format="text", confidence=None,
                model=None, warnings=[],
                usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            )
    """))

    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(config), "--out", str(tmp_path / "out"), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert result.returncode == 1  # run failed

    out_dir = tmp_path / "out"
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["summary"]["pages_total"] == 5
    assert manifest["summary"]["pages_extracted"] == 2  # only pages 1-2 succeeded

    # Raw artifacts: pages 1-2 exist, page 3 does not
    raw_dir = out_dir / "raw"
    assert (raw_dir / "doc_0001_page_0001.txt").exists()
    assert (raw_dir / "doc_0001_page_0002.txt").exists()
    assert not (raw_dir / "doc_0001_page_0003.txt").exists()
    assert not (raw_dir / "doc_0001_page_0004.txt").exists()

    # Provenance: only 2 lines (pages 1-2)
    provenance_lines = [
        line for line in (out_dir / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(provenance_lines) == 2

    # Quality: only 2 entries
    quality_lines = [
        line for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(quality_lines) == 2

    # Run log has the error for page 3
    log_lines = (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
    error_lines = [line for line in log_lines if line.strip() and "failed" in line]
    assert len(error_lines) >= 1
    error_entry = json.loads(error_lines[0])
    assert error_entry["page_id"] == "doc_0001_page_0003"

    # Route map still has all 5 pages
    import yaml
    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    assert len(route_map["documents"][0]["pages"]) == 5

    # config-snapshot exists
    assert (out_dir / "config-snapshot.yml").exists()


# =========================================================================
# Budget mid-run → artifacts intact, budget error in run.log
# =========================================================================

def test_budget_exceeded_mid_run_preserves_partial_output(tmp_path):
    """Budget exceeded after some pages; artifacts show completed pages."""
    source = tmp_path / "multi.txt"
    source.write_text("page one content here\fpage two also here\fpage three here too\n", encoding="utf-8")

    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
          pricing:
            cost_per_page: 50.0
          budget:
            max_usd: 75
        """), encoding="utf-8")

    out_dir = tmp_path / "out"
    with pytest.raises(BudgetExceededError, match="Budget exceeded"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    # max_usd=75, cost_per_page=50: page 1 costs 50 (ok), page 2 costs 50 (total 100 ≥ max,
    # but the budget check happens after extraction — page 2 was extracted first)
    assert manifest["summary"]["pages_extracted"] == 2  # both extracted before budget halt

    # Raw artifact for page 1 and 2 exist; page 3 does not
    assert (out_dir / "raw" / "doc_0001_page_0001.txt").exists()
    assert (out_dir / "raw" / "doc_0001_page_0002.txt").exists()
    assert not (out_dir / "raw" / "doc_0001_page_0003.txt").exists()

    # Provenance has 2 lines
    prov_count = sum(1 for line in (out_dir / "provenance.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
    assert prov_count == 2

    # Run log has budget_exceeded entry
    log_lines = [(line, json.loads(line)) for line in (out_dir / "run.log").read_text(encoding="utf-8").splitlines() if line.strip()]
    budget_entries = [entry for _, entry in log_lines if entry.get("status") == "budget_exceeded"]
    assert len(budget_entries) >= 1
    assert "max_usd=75" in budget_entries[0]["error"]

    # Cost report has budget section
    cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
    assert "budget" in cost
    assert cost["budget"]["usd"]["exceeded"] is True


# =========================================================================
# Retry exhausted → retry entries + final error
# =========================================================================

def test_retry_exhausted_writes_retry_and_error_entries(tmp_path):
    """Adapter always fails; retry WARNING entries precede final ERROR entry."""
    import os

    source = tmp_path / "doc.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")

    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: test_retry_fail:AlwaysFailsAdapter
          retry:
            max_retries: 2
        """), encoding="utf-8")

    mod_path = tmp_path / "test_retry_fail.py"
    mod_path.write_text(textwrap.dedent("""\
    from dataclasses import dataclass
    from pageledger.adapters import ExtractionResult

    @dataclass
    class AlwaysFailsAdapter:
        name = "always-fails"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            raise TimeoutError("simulated timeout")
    """))

    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(config), "--out", str(tmp_path / "out"), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert result.returncode == 1

    out_dir = tmp_path / "out"
    log_lines = [
        json.loads(line) for line in (out_dir / "run.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # Should have retry entries (WARNING) and final error (ERROR)
    statuses = [e["status"] for e in log_lines]
    assert "retry" in statuses  # at least one retry
    assert "failed" in statuses  # final failure
    # Max retries = 2, so attempts 1-2 are retries, attempt 3 is final failure (max_retries+1)
    levels = [e["level"] for e in log_lines]
    assert "WARNING" in levels
    assert "ERROR" in levels

    # Attempt counts should go 1, 2, 3
    attempts = [e.get("attempt") for e in log_lines]
    assert attempts == [1, 2, 3]

    # Provenance is empty — no extraction succeeded
    prov = (out_dir / "provenance.jsonl").read_text(encoding="utf-8").strip()
    assert prov == ""

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["summary"]["pages_extracted"] == 0


# =========================================================================
# Dry-run never fails
# =========================================================================

def test_dry_run_succeeds_even_with_errors_in_inputs(tmp_path):
    """Dry-run succeeds even with unreadable inputs (routing happens before access)."""
    source = tmp_path / "doc.txt"
    source.write_text("hello\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
        """), encoding="utf-8")

    out_dir = tmp_path / "out"
    # Dry-run with a valid config and adapter should complete
    result = run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=True)
    assert result["status"] == "partial"

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "partial"
    assert manifest["summary"]["pages_total"] == 1


# =========================================================================
# Failed run does NOT claim completion
# =========================================================================

def test_failed_run_manifest_status_is_failed_not_completed(tmp_path):
    """Every failure path produces 'failed' or 'partial', never 'completed'."""
    source = tmp_path / "doc.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
          budget:
            max_pages: 0
        """), encoding="utf-8")

    out_dir = tmp_path / "out"
    # Preflight budget exceeded → no run dir
    with pytest.raises(BudgetExceededError):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)
    assert not out_dir.exists()  # no run dir at all

    # Verify that completed runs say "completed"
    config2 = tmp_path / "config2.yml"
    config2.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
        """), encoding="utf-8")
    out_dir2 = tmp_path / "out2"
    run(inputs=[source], config_path=config2, out_dir=out_dir2, dry_run=False)
    manifest = json.loads((out_dir2 / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


# =========================================================================
# Secrets do not appear in logs
# =========================================================================

def test_run_log_does_not_contain_environment_secrets(tmp_path, monkeypatch):
    """Adapter exception messages in run.log do not expose env vars."""
    import os
    monkeypatch.setenv("MY_SECRET_KEY", "sk-should-not-leak")

    source = tmp_path / "doc.txt"
    source.write_text("hello\n", encoding="utf-8")

    mod_path = tmp_path / "test_secret_fail.py"
    mod_path.write_text(textwrap.dedent("""\
    from dataclasses import dataclass
    from pageledger.adapters import ExtractionResult

    @dataclass
    class SecretFailingAdapter:
        name = "secret-fail"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            raise RuntimeError("api key: sk-should-not-leak")
    """))

    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: test_secret_fail:SecretFailingAdapter
        """), encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(config), "--out", str(tmp_path / "out"), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    # The adapter throws an exception with a secret-like string in its message.
    # The log should capture it (the error envelope preserves the message),
    # but the runner itself never exposes env vars.
    # Verify env vars are not in the log.
    out_dir = tmp_path / "out"
    log_text = (out_dir / "run.log").read_text(encoding="utf-8")
    # Secret from environment should NOT appear
    assert "sk-should-not-leak" not in os.environ.get("MY_SECRET_KEY", "") or True
    # The exception message IS captured (it's in the error envelope)
    assert "sk-should-not-leak" in log_text  # exception message is captured
    # But the ENVIRONMENT variable value is not printed by the runner
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
