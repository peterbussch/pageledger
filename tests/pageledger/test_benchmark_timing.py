"""Private benchmark timing observations leave run artifacts unchanged."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

import pageledger.runner as runner_module
from pageledger.adapters import ExtractionResult
from pageledger.runner import run
from pageledger.verify import verify_run


class ConstantWorkAdapter:
    name = "constant-work"
    version = "0.1-test"
    deterministic = True
    input_types = ("text",)
    output_types = ("text",)
    capabilities = ("test",)

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
    ) -> ExtractionResult:
        _ = source, page_id, page_number, action, prompt
        return ExtractionResult(
            content="constant work",
            format="text",
            confidence=None,
            model="constant-work-model",
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": None,
                "cost_usd": None,
            },
        )


def _load_all_artifacts(out_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(out_dir).as_posix()
        if path.suffix == ".json":
            artifacts[relative] = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".jsonl":
            artifacts[relative] = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        elif path.suffix in {".yml", ".yaml"}:
            artifacts[relative] = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            artifacts[relative] = path.read_text(encoding="utf-8")
    return artifacts


def _canonicalize_identity_and_timing(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<run-id>"
                if key in {"run_id", "parent_run_id"}
                else "<timestamp>"
                if key in {"timestamp", "started_at", "completed_at", "created_at", "generated_at"}
                else "<timing>"
                if key == "extraction_seconds"
                else _canonicalize_identity_and_timing(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_identity_and_timing(item) for item in value]
    return value


def test_phase_observer_reports_exclusive_phases_without_changing_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "two-pages.txt"
    source.write_text("first page\fsecond page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: constant-work
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_module, "_utc_now", lambda: "2026-08-23T00:00:00Z")
    monkeypatch.setattr(runner_module, "_utc_now_compact", lambda: "20260823T000000000000Z")
    adapter = ConstantWorkAdapter()
    observed_out = tmp_path / "observed"
    plain_out = tmp_path / "plain"
    events: list[tuple[str, int]] = []

    run(
        inputs=[source],
        config_path=config,
        out_dir=observed_out,
        dry_run=False,
        _loaded_adapter=adapter,
        _reproducibility_profile=None,
        _phase_observer=lambda name, duration: events.append((name, duration)),
    )
    run(
        inputs=[source],
        config_path=config,
        out_dir=plain_out,
        dry_run=False,
        _loaded_adapter=adapter,
        _reproducibility_profile=None,
    )

    names = [name for name, _ in events]
    assert all(duration_ns >= 0 for _, duration_ns in events)
    assert names.count("adapter_call") == 2
    assert names[-2:] == ["manifest_commit", "result_return"]
    assert verify_run(observed_out)["status"] == "pass"
    assert "benchmark" not in json.dumps(_load_all_artifacts(observed_out))
    assert _canonicalize_identity_and_timing(_load_all_artifacts(observed_out)) == (
        _canonicalize_identity_and_timing(_load_all_artifacts(plain_out))
    )
