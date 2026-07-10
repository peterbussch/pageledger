"""CLI coverage for PageLedger 0.1.4 additions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_top_level_version(capsys: pytest.CaptureFixture[str]) -> None:
    from pageledger import __version__
    from pageledger.cli import main

    with pytest.raises(SystemExit, match="0"):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"pageledger {__version__}"


def test_align_dry_run_reaches_preview_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import pageledger.cli as cli

    called: dict[str, object] = {}

    def fake_align(run_dir: Path, *, schema_path: Path | None, dry_run: bool) -> dict:
        called.update(run_dir=run_dir, schema_path=schema_path, dry_run=dry_run)
        return {
            "run_id": "run-test",
            "schema_name": "table",
            "schema_source": "config_snapshot",
            "pages_aligned": 1,
            "records_normalized": 2,
            "grade_distribution": {"A": 1, "B": 0, "C": 0, "D": 0, "F": 0},
            "review_queue_count": 0,
            "applied": False,
            "before": {},
            "after": {},
        }

    monkeypatch.setattr(cli, "align_run", fake_align)
    exit_code = cli.main(["align", str(tmp_path), "--dry-run", "--json"])

    assert exit_code == 0
    assert called["dry_run"] is True
    assert json.loads(capsys.readouterr().out)["applied"] is False


def test_verify_run_cli_text_json_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger.cli import main
    from pageledger.runner import run

    source = tmp_path / "doc.txt"
    source.write_text("a complete page of archival text", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        'schema_version: "0.1"\n'
        "taxonomy:\n"
        "  page_types:\n"
        "    prose:\n"
        "      default_action: transcribe_text\n"
        "run:\n"
        "  adapter: text\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"
    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert main(["verify-run", str(out_dir)]) == 0
    assert "Run verification: PASS" in capsys.readouterr().out
    assert main(["verify-run", str(out_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"

    (out_dir / "quality.jsonl").unlink()
    assert main(["verify-run", str(out_dir), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "fail"
