"""0.1.4 output-integrity and artifact-commit regression tests."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import pageledger.runner as runner_module
from pageledger.artifacts import write_json, write_jsonl
from pageledger.runner import rerun, run

CONFIG = textwrap.dedent(
    """\
    schema_version: "0.1"
    taxonomy:
      page_types:
        prose:
          default_action: transcribe_text
    run:
      adapter: text
    """
)


def _quality(run_dir: Path) -> dict:
    return json.loads((run_dir / "quality.jsonl").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "marker",
    [
        "<think>",
        "</think>",
        "<|channel",
        "<|im_start|>",
        "<|im_end|>",
        "[INST]",
        "[/INST]",
    ],
)
def test_instruction_echo_records_high_specificity_marker(marker: str, tmp_path: Path) -> None:
    source = tmp_path / "page.txt"
    source.write_text(f"transcription before {marker} transcript after", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    quality = _quality(out_dir)
    assert "instruction_echo" in quality["warnings"]
    assert marker in quality["output_integrity"]["instruction_markers"]


def test_instruction_echo_does_not_match_generic_prose(tmp_path: Path) -> None:
    source = tmp_path / "page.txt"
    source.write_text(
        "The editor considered the instructions and thought carefully about the channel.",
        encoding="utf-8",
    )
    config = tmp_path / "pageledger.yml"
    config.write_text(CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"

    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    quality = _quality(out_dir)
    assert "instruction_echo" not in quality["warnings"]
    assert quality["output_integrity"]["instruction_markers"] == []


def test_rerun_flags_output_inflation_and_records_parent_evidence(tmp_path: Path) -> None:
    source = tmp_path / "page.txt"
    # Long enough for inflation comparison, but intentionally fragmented so
    # the parent policy derives a real quality-warning rerun candidate.
    source.write_text("x " * 800, encoding="utf-8")
    parent_config = tmp_path / "parent.yml"
    parent_config.write_text(CONFIG, encoding="utf-8")
    parent_dir = tmp_path / "parent"
    run(inputs=[source], config_path=parent_config, out_dir=parent_dir, dry_run=False)

    parent_quality = _quality(parent_dir)
    rerun_path = parent_dir / "rerun-manifest.yml"
    manifest = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    assert manifest["rerun_status"] == "executable"
    assert manifest["items"][0]["reason"] == "quality_warning"

    adapter_module = tmp_path / "amplify_adapter.py"
    adapter_module.write_text(
        textwrap.dedent(
            """\
            from pageledger.adapters import ExtractionResult

            class AmplifyAdapter:
                name = "amplify"
                version = "1"
                deterministic = True
                input_types = ("text",)
                output_types = ("text",)
                capabilities = ("cleanup",)

                def supports(self, action):
                    return action == "transcribe_text"

                def extract(self, source, **kwargs):
                    text = source.read_text(encoding="utf-8") * 5
                    return ExtractionResult(
                        content=text,
                        format="text",
                        confidence=None,
                        model="amplifier",
                        warnings=[],
                        usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
                    )
            """
        ),
        encoding="utf-8",
    )
    child_config = tmp_path / "child.yml"
    child_config.write_text(
        CONFIG.replace("adapter: text", "adapter: amplify_adapter:AmplifyAdapter"),
        encoding="utf-8",
    )
    child_dir = tmp_path / "child"

    rerun(
        parent_dir=parent_dir,
        config_path=child_config,
        out_dir=child_dir,
        adapter_path=tmp_path,
    )

    quality = _quality(child_dir)
    integrity = quality["output_integrity"]
    assert "output_inflation" in quality["warnings"]
    assert integrity["parent_character_count"] == parent_quality["character_count"]
    assert integrity["character_delta"] >= 1000
    assert integrity["character_ratio"] == 5.0
    audit = json.loads((child_dir / "audit.json").read_text(encoding="utf-8"))
    assert any(item["reason"] == "quality_warning" for item in audit["review_queue"])


@pytest.mark.parametrize(
    ("parent_count", "child_count", "warns"),
    [
        (500, 1999, False),
        (500, 2000, True),
        (1000, 4000, True),
        (1000, 3999, False),
        (0, 999, False),
        (0, 1000, True),
    ],
)
def test_output_inflation_thresholds(
    parent_count: int, child_count: int, warns: bool
) -> None:
    evidence, warnings = runner_module._output_integrity(
        "x" * child_count,
        {"character_count": parent_count},
    )
    assert ("output_inflation" in warnings) is warns
    assert evidence["parent_character_count"] == parent_count


def test_missing_parent_evidence_cannot_trigger_output_inflation() -> None:
    evidence, warnings = runner_module._output_integrity("x" * 5000, None)

    assert "output_inflation" not in warnings
    assert evidence == {
        "instruction_markers": [],
        "parent_character_count": None,
        "character_delta": None,
        "character_ratio": None,
    }


def test_manifest_is_not_committed_when_later_artifact_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "page.txt"
    source.write_text("a complete archival transcription", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(CONFIG, encoding="utf-8")
    out_dir = tmp_path / "run"
    real_write_yaml = runner_module.write_yaml

    def fail_on_rerun_manifest(path: Path, data: dict, **kwargs) -> None:
        if path.name == "rerun-manifest.yml":
            raise OSError("simulated disk failure")
        real_write_yaml(path, data, **kwargs)

    monkeypatch.setattr(runner_module, "write_yaml", fail_on_rerun_manifest)

    with pytest.raises(OSError, match="simulated disk failure"):
        run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    assert not (out_dir / "manifest.json").exists()


def test_artifact_json_writers_reject_non_finite_numbers(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_json(tmp_path / "bad.json", {"value": float("nan")})
    with pytest.raises(ValueError):
        write_jsonl(tmp_path / "bad.jsonl", [{"value": float("inf")}])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"confidence": float("nan")}, "confidence"),
        ({"confidence": True}, "confidence"),
        ({"confidence": 1.1}, "confidence"),
        ({"model": 42}, "model"),
        ({"warnings": ["clear", 42]}, "warnings"),
        ({"confidence_detail": {"mean": float("inf")}}, "confidence_detail"),
        ({"usage": {"pages": True}}, "usage.pages"),
        ({"usage": {"pages": 1, "compute_seconds": True}}, "compute_seconds"),
    ],
)
def test_adapter_result_validation_rejects_dishonest_values(
    changes: dict, message: str
) -> None:
    result = SimpleNamespace(
        content="text",
        format="text",
        confidence=None,
        confidence_detail=None,
        model=None,
        warnings=[],
        usage={"pages": 1},
    )
    for key, value in changes.items():
        setattr(result, key, value)

    with pytest.raises(ValueError, match=message):
        runner_module._validate_extraction_result("dishonest", result)
