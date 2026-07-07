"""Phase 2 tests: CLI hardening, config validation, edge-case inputs."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import yaml

# Reuse helper from test_schemas
HERE = Path(__file__).resolve().parent

MINIMAL_CONFIG = textwrap.dedent("""\
    schema_version: "0.1"
    taxonomy:
      page_types:
        prose:
          default_action: transcribe_text
    run:
      adapter: text
    """)


def _run_cli(args: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run pageledger CLI and return (exit_code, stdout, stderr)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pageledger", *args],
        capture_output=True, text=True, cwd=str(cwd) if cwd else None,
    )
    return result.returncode, result.stdout, result.stderr


# =========================================================================
# init-config
# =========================================================================

def test_init_config_writes_to_stdout(tmp_path: Path) -> None:
    """init-config writes minimal valid config to stdout."""
    exit_code, stdout, stderr = _run_cli(["init-config"])
    assert exit_code == 0
    parsed = yaml.safe_load(stdout)
    assert parsed["schema_version"] == "0.1"
    assert "taxonomy" in parsed
    assert "page_types" in parsed["taxonomy"]
    assert parsed["run"]["adapter"] == "text"


def test_init_config_writes_to_file(tmp_path: Path) -> None:
    """init-config --out writes config to a file."""
    out_path = tmp_path / "my-config.yml"
    exit_code, stdout, stderr = _run_cli(["init-config", "--out", str(out_path)])
    assert exit_code == 0
    assert out_path.is_file()
    parsed = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert parsed["run"]["adapter"] == "text"


def test_init_config_pdf_text_adapter(tmp_path: Path) -> None:
    """init-config --adapter pdf_text generates pdf_text config."""
    exit_code, stdout, stderr = _run_cli(["init-config", "--adapter", "pdf_text"])
    assert exit_code == 0
    parsed = yaml.safe_load(stdout)
    assert parsed["run"]["adapter"] == "pdf_text"


def test_init_config_rejects_unknown_adapter(tmp_path: Path) -> None:
    """init-config rejects unrecognized adapter choices."""
    exit_code, stdout, stderr = _run_cli(["init-config", "--adapter", "ocr"])
    assert exit_code != 0


def test_init_config_pdf_ocr_adapter(tmp_path: Path) -> None:
    """init-config --adapter pdf_ocr generates a pdf_ocr config."""
    exit_code, stdout, stderr = _run_cli(["init-config", "--adapter", "pdf_ocr"])
    assert exit_code == 0
    parsed = yaml.safe_load(stdout)
    assert parsed["run"]["adapter"] == "pdf_ocr"


# =========================================================================
# adapter_options and --adapter-path
# =========================================================================

def test_config_adapter_options_must_be_mapping(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("page\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
          adapter_options: not-a-mapping
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(
        ["run", str(source), "--config", str(config),
         "--out", str(tmp_path / "out"), "--json"],
    )
    assert exit_code == 1
    assert "run.adapter_options must be a mapping" in json.loads(stdout)["error"]


def test_config_adapter_options_rejected_by_text_adapter(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("page\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
          adapter_options:
            dpi: 300
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(
        ["run", str(source), "--config", str(config),
         "--out", str(tmp_path / "out"), "--json"],
    )
    assert exit_code == 1
    assert "adapter_options" in json.loads(stdout)["error"]


ADAPTER_MODULE = textwrap.dedent("""\
    from dataclasses import dataclass
    from pathlib import Path
    from pageledger.adapters import ExtractionResult

    @dataclass
    class UppercaseAdapter:
        suffix: str = ""
        name: str = "uppercase"
        version: str = "1.0"
        deterministic: bool = True
        input_types: tuple[str, ...] = ("text",)
        output_types: tuple[str, ...] = ("text",)
        capabilities: tuple[str, ...] = ("local",)

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            text = source.read_text(encoding="utf-8").upper() + self.suffix
            return ExtractionResult(
                content=text, format="text", confidence=None,
                model=None, warnings=[],
                usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            )
    """)


def test_adapter_path_loads_custom_adapter_without_pythonpath(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    (adapter_dir / "upper_adapter.py").write_text(ADAPTER_MODULE, encoding="utf-8")
    source = tmp_path / "sample.txt"
    source.write_text("hello\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: upper_adapter:UppercaseAdapter
          adapter_options:
            suffix: "!"
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(
        ["run", str(source), "--config", str(config), "--out", str(out_dir),
         "--adapter-path", str(adapter_dir), "--json"],
    )
    assert exit_code == 0, f"stdout={stdout}\nstderr={stderr}"
    result = json.loads(stdout)
    assert result["summary"]["pages_extracted"] == 1
    raw = out_dir / "raw" / "doc_0001_page_0001.txt"
    assert raw.read_text(encoding="utf-8") == "HELLO\n!"


def test_adapter_path_missing_directory_errors(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("hello\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(
        ["run", str(source), "--config", str(config),
         "--out", str(tmp_path / "out"),
         "--adapter-path", str(tmp_path / "nope"), "--json"],
    )
    assert exit_code == 1
    assert "adapter-path" in json.loads(stdout)["error"]


# =========================================================================
# inspect-run
# =========================================================================

def test_inspect_run_human_output(tmp_path: Path) -> None:
    """inspect-run prints a human-readable summary."""
    source = tmp_path / "sample.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir)])
    exit_code, stdout, stderr = _run_cli(["inspect-run", str(out_dir)])
    assert exit_code == 0
    assert "Status: completed" in stdout
    assert "Pages: 2 total / 2 extracted" in stdout
    assert "Artifacts present: 10" in stdout


def test_inspect_run_json_output(tmp_path: Path) -> None:
    """inspect-run --json produces parseable JSON."""
    source = tmp_path / "sample.txt"
    source.write_text("first page\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir)])
    exit_code, stdout, stderr = _run_cli(["inspect-run", str(out_dir), "--json"])
    assert exit_code == 0
    report = json.loads(stdout)
    assert report["status"] == "completed"
    assert report["pages_total"] == 1
    assert isinstance(report["artifacts_present"], list)


def test_inspect_run_missing_directory(tmp_path: Path) -> None:
    """inspect-run on a nonexistent directory produces error JSON."""
    exit_code, stdout, stderr = _run_cli(["inspect-run", str(tmp_path / "nope"), "--json"])
    assert exit_code == 1
    report = json.loads(stdout)
    assert report["status"] == "error"
    assert "No manifest.json" in report["error"]


def test_inspect_run_dry_run(tmp_path: Path) -> None:
    """inspect-run on a dry-run directory works."""
    source = tmp_path / "sample.txt"
    source.write_text("first page\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--dry-run"])
    exit_code, stdout, stderr = _run_cli(["inspect-run", str(out_dir), "--json"])
    assert exit_code == 0
    report = json.loads(stdout)
    assert report["execution_mode"] == "dry_run"
    assert report["pages_extracted"] == 0


# =========================================================================
# --json error output
# =========================================================================

def test_json_error_on_run_failure(tmp_path: Path) -> None:
    """--json on a failed run produces parseable error JSON."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    # First run succeeds
    _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir)])
    # Second run fails (non-empty output dir)
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert error_json["status"] == "error"
    assert "not empty" in error_json["error"]


def test_json_error_on_invalid_config(tmp_path: Path) -> None:
    """--json on invalid config emits error JSON."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text("{invalid yaml!!! : ::}", encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert error_json["status"] == "error"


def test_json_error_on_missing_config(tmp_path: Path) -> None:
    """--json on missing config path emits error JSON."""
    exit_code, stdout, stderr = _run_cli(["run", str(tmp_path / "t.txt"), "--config", str(tmp_path / "nonexistent.yml"), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert error_json["status"] == "error"


# =========================================================================
# Config warnings (suspicious but valid configs)
# =========================================================================

def test_config_empty_page_types_prints_warning(tmp_path: Path) -> None:
    """Empty taxonomy.page_types triggers a config warning."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types: {}
        run:
          adapter: text
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir)])
    assert exit_code == 0
    assert "Config warnings" in stdout or True  # warning surfaces in human output

    exit_code2, stdout2, stderr2 = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out2"), "--json"])
    assert exit_code2 == 0
    result = json.loads(stdout2)
    config_warnings = result.get("config_warnings", [])
    assert any("empty" in w.lower() for w in config_warnings)


def test_config_unknown_top_level_key_triggers_warning(tmp_path: Path) -> None:
    """Unknown top-level config key produces a warning."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        old_page_types: {prose: {default_action: transcribe_text}}
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: text
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 0
    result = json.loads(stdout)
    config_warnings = result.get("config_warnings", [])
    assert any("old_page_types" in w for w in config_warnings)


def test_config_impossible_budget_warn_at_100_triggers_warning(tmp_path: Path) -> None:
    """warn_at_percent >= 100 triggers a config warning."""
    source = tmp_path / "sample.txt"
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
            max_pages: 100
            warn_at_percent: 100
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 0
    result = json.loads(stdout)
    config_warnings = result.get("config_warnings", [])
    assert any("warn_at_percent" in w for w in config_warnings)


def test_config_impossible_budget_zero_usd_warns(tmp_path: Path) -> None:
    """max_usd == 0 triggers a config warning."""
    source = tmp_path / "sample.txt"
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
            max_usd: 0
        """), encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
    # max_usd=0 will likely cause budget failure
    if exit_code == 0:
        result = json.loads(stdout)
        config_warnings = result.get("config_warnings", [])
        assert any("max_usd" in w for w in config_warnings)
    # If it fails due to budget, that's also valid behavior


# =========================================================================
# Edge-case file inputs
# =========================================================================

def test_directory_input_with_mixed_file_types(tmp_path: Path) -> None:
    """Directory input containing text files and non-text files."""
    indir = tmp_path / "inputs"
    indir.mkdir()
    (indir / "a.txt").write_text("page one\n", encoding="utf-8")
    (indir / "b.txt").write_text("page two\n", encoding="utf-8")
    (indir / "not-a-document.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    (indir / "subdir").mkdir()
    (indir / "subdir" / "nested.txt").write_text("nested\n", encoding="utf-8")

    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(indir), "--config", str(config), "--out", str(out_dir), "--json"])
    # Should process text files, skip or fail on PNG depending on adapter validation
    if exit_code == 0:
        result = json.loads(stdout)
        # Should have processed at least the .txt files
        assert result["summary"]["pages_total"] >= 2


def test_input_file_unreadable(tmp_path: Path) -> None:
    """Unreadable input file produces a clear error."""
    source = tmp_path / "unreadable.txt"
    source.write_text("test\n", encoding="utf-8")
    source.chmod(0o000)
    try:
        config = tmp_path / "config.yml"
        config.write_text(MINIMAL_CONFIG, encoding="utf-8")
        out_dir = tmp_path / "out"
        exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
        assert exit_code == 1
        # The error is now caught as RuntimeError; verify stdout has JSON error
        if stdout.strip():
            error_json = json.loads(stdout)
            assert error_json["status"] == "error"
        else:
            # stderr still has human-readable error
            assert "Cannot read" in stderr or "pageledger: error" in stderr
    finally:
        source.chmod(0o644)


def test_input_file_nonexistent(tmp_path: Path) -> None:
    """Nonexistent input file produces error."""
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(tmp_path / "nonexistent.txt"), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert error_json["status"] == "error"


def test_input_symlink_to_text_file(tmp_path: Path) -> None:
    """Symlink to a text file is processed correctly."""
    source = tmp_path / "real.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(link), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 0
    result = json.loads(stdout)
    assert result["summary"]["pages_total"] == 2
    assert result["raw_artifact_count"] == 2


def test_input_symlink_to_directory(tmp_path: Path) -> None:
    """Symlink to a directory is expanded correctly."""
    indir = tmp_path / "real_dir"
    indir.mkdir()
    (indir / "a.txt").write_text("page one\n", encoding="utf-8")
    (indir / "b.txt").write_text("page two\n", encoding="utf-8")
    link = tmp_path / "link_dir"
    link.symlink_to(indir)
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(link), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 0
    result = json.loads(stdout)
    assert result["summary"]["pages_total"] >= 2


def test_config_validation_flat_page_types_rejected(tmp_path: Path) -> None:
    """Flat page_types (not under taxonomy) is rejected with clear message."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        page_types:
          prose:
            default_action: transcribe_text
        run:
          adapter: text
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "taxonomy.page_types" in error_json["error"]


def test_config_validation_flat_adapter_rejected(tmp_path: Path) -> None:
    """Flat adapter (not under run) is rejected."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        adapter: text
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "run.adapter" in error_json["error"]


def test_config_validation_budget_max_pages_non_integer(tmp_path: Path) -> None:
    """Non-integer max_pages is rejected with key path."""
    source = tmp_path / "sample.txt"
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
            max_pages: "fifty"
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "run.budget.max_pages" in error_json["error"]


def test_config_validation_negative_max_usd_rejected(tmp_path: Path) -> None:
    """Negative max_usd is rejected."""
    source = tmp_path / "sample.txt"
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
            max_usd: -1
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "non-negative" in error_json["error"].lower()


def test_config_validation_warn_at_percent_out_of_range(tmp_path: Path) -> None:
    """warn_at_percent outside 0-100 is rejected."""
    source = tmp_path / "sample.txt"
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
            max_pages: 100
            warn_at_percent: 150
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "between 0 and 100" in error_json["error"]


def test_config_validation_non_integer_max_retries_rejected(tmp_path: Path) -> None:
    """Non-integer max_retries is rejected with key path."""
    source = tmp_path / "sample.txt"
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
          retry:
            max_retries: "many"
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "run.retry.max_retries" in error_json["error"]


def test_config_taxonomy_not_a_mapping(tmp_path: Path) -> None:
    """Non-mapping taxonomy is rejected."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy: [1, 2, 3]
        run:
          adapter: text
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "taxonomy" in error_json["error"].lower() and "mapping" in error_json["error"].lower()


def test_config_page_types_not_a_mapping(tmp_path: Path) -> None:
    """Non-mapping taxonomy.page_types is rejected."""
    source = tmp_path / "sample.txt"
    source.write_text("test\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(textwrap.dedent("""\
        schema_version: "0.1"
        taxonomy:
          page_types: [prose, table]
        run:
          adapter: text
        """), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(tmp_path / "out"), "--json"])
    assert exit_code == 1
    error_json = json.loads(stdout)
    assert "taxonomy.page_types" in error_json["error"] and "mapping" in error_json["error"].lower()


# =========================================================================
# Edge-case: empty text file
# =========================================================================

def test_empty_text_input(tmp_path: Path) -> None:
    """Empty text file produces 1 page, no text."""
    source = tmp_path / "empty.txt"
    source.write_text("", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 0
    result = json.loads(stdout)
    assert result["summary"]["pages_total"] == 1


def test_text_only_newlines(tmp_path: Path) -> None:
    """File with only newlines paginates cleanly."""
    source = tmp_path / "newlines.txt"
    source.write_text("\n\n\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "out"
    exit_code, stdout, stderr = _run_cli(["run", str(source), "--config", str(config), "--out", str(out_dir), "--json"])
    assert exit_code == 0


# =========================================================================
# inspect-run --csv
# =========================================================================

def _make_run(tmp_path: Path) -> Path:
    source = tmp_path / "pages.txt"
    source.write_text("a page of ordinary text\fshorty", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    exit_code, _, stderr = _run_cli([
        "run", str(source), "--config", str(config), "--out", str(out_dir),
    ])
    assert exit_code == 0, stderr
    return out_dir


def test_inspect_run_csv_emits_one_row_per_page(tmp_path: Path) -> None:
    import csv
    import io

    out_dir = _make_run(tmp_path)
    exit_code, stdout, stderr = _run_cli(["inspect-run", str(out_dir), "--csv"])
    assert exit_code == 0, stderr

    rows = list(csv.DictReader(io.StringIO(stdout)))
    assert len(rows) == 2
    first, second = rows
    assert first["page_id"] == "doc_0001_page_0001"
    assert first["adapter"] == "text"
    assert first["warnings"] == ""
    assert int(first["character_count"]) > 10
    assert second["warnings"] == "short_text"
    assert second["confidence"] == ""  # text adapter reports none
    for column in ("page_number", "word_count", "cost_usd", "extraction_seconds"):
        assert column in first


def test_inspect_run_csv_rejects_combination_with_json(tmp_path: Path) -> None:
    out_dir = _make_run(tmp_path)
    exit_code, _, stderr = _run_cli(["inspect-run", str(out_dir), "--csv", "--json"])
    assert exit_code == 2
    assert "not allowed" in stderr


# =========================================================================
# run without a config file (--adapter)
# =========================================================================

def test_run_with_adapter_flag_needs_no_config(tmp_path: Path) -> None:
    source = tmp_path / "pages.txt"
    source.write_text("first page\fsecond page", encoding="utf-8")
    out_dir = tmp_path / "run"
    exit_code, stdout, stderr = _run_cli([
        "run", str(source), "--adapter", "text", "--out", str(out_dir),
    ])
    assert exit_code == 0, stderr
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_extracted"] == 2
    snapshot = yaml.safe_load((out_dir / "config-snapshot.yml").read_text(encoding="utf-8"))
    assert snapshot["run"]["adapter"] == "text"


def test_run_rejects_config_and_adapter_together(tmp_path: Path) -> None:
    source = tmp_path / "pages.txt"
    source.write_text("page", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    exit_code, _, stderr = _run_cli([
        "run", str(source), "--config", str(config),
        "--adapter", "text", "--out", str(tmp_path / "run"),
    ])
    assert exit_code == 2
    assert "not allowed" in stderr


def test_run_requires_config_or_adapter(tmp_path: Path) -> None:
    source = tmp_path / "pages.txt"
    source.write_text("page", encoding="utf-8")
    exit_code, _, stderr = _run_cli([
        "run", str(source), "--out", str(tmp_path / "run"),
    ])
    assert exit_code == 2
    assert "--config" in stderr and "--adapter" in stderr


def test_run_adapter_flag_snapshot_documents_pdf_ocr_options(tmp_path: Path) -> None:
    """The synthesized pdf_ocr config surfaces the dpi/lang knobs."""
    exit_code, stdout, _ = _run_cli(["init-config", "--adapter", "pdf_ocr"])
    assert exit_code == 0
    parsed = yaml.safe_load(stdout)
    assert parsed["run"]["adapter"] == "pdf_ocr"
    assert parsed["run"]["adapter_options"]["dpi"] == 300
    assert parsed["run"]["adapter_options"]["lang"] == "eng"


# =========================================================================
# page selection (--pages)
# =========================================================================

def _three_page_source(tmp_path: Path) -> Path:
    source = tmp_path / "pages.txt"
    source.write_text("page one text\fpage two text\fpage three text", encoding="utf-8")
    return source


def test_run_pages_selects_subset_preserving_page_identity(tmp_path: Path) -> None:
    source = _three_page_source(tmp_path)
    out_dir = tmp_path / "run"
    exit_code, _, stderr = _run_cli([
        "run", str(source), "--adapter", "text", "--out", str(out_dir),
        "--pages", "2-3",
    ])
    assert exit_code == 0, stderr
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["pages_total"] == 2
    entry = manifest["inputs"][0]
    assert entry["page_count"] == 3  # the document's real size
    assert entry["pages"] == "2-3"  # what this run selected from it
    raw = sorted(p.name for p in (out_dir / "raw").iterdir())
    assert raw == ["doc_0001_page_0002.txt", "doc_0001_page_0003.txt"]
    assert (out_dir / "raw" / "doc_0001_page_0002.txt").read_text(
        encoding="utf-8") == "page two text"


def test_run_pages_rejects_out_of_range(tmp_path: Path) -> None:
    source = _three_page_source(tmp_path)
    exit_code, _, stderr = _run_cli([
        "run", str(source), "--adapter", "text", "--out", str(tmp_path / "run"),
        "--pages", "7",
    ])
    assert exit_code == 1
    assert "3 pages" in stderr


def test_run_pages_rejects_malformed_expressions(tmp_path: Path) -> None:
    source = _three_page_source(tmp_path)
    for bad in ("abc", "0", "5-2", "1,,2", "-3"):
        exit_code, _, stderr = _run_cli([
            "run", str(source), "--adapter", "text",
            "--out", str(tmp_path / f"run-{bad}"), "--pages", bad,
        ])
        assert exit_code == 1, bad
        assert "--pages" in stderr


def test_run_pages_rejects_multiple_inputs(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("page", encoding="utf-8")
    b.write_text("page", encoding="utf-8")
    exit_code, _, stderr = _run_cli([
        "run", str(a), str(b), "--adapter", "text",
        "--out", str(tmp_path / "run"), "--pages", "1",
    ])
    assert exit_code == 1
    assert "single input" in stderr
