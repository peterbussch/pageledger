"""Adapter escalation chain configuration and execution contracts."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from pageledger.config import load_config
from pageledger.runner import rerun, run

MARKER_ADAPTER = textwrap.dedent(
    """\
    from dataclasses import dataclass
    from typing import ClassVar

    from pageledger.adapters import ExtractionResult


    @dataclass(frozen=True)
    class MarkerAdapter:
        marker: str
        name: ClassVar[str] = "marker"
        version: ClassVar[str] = "1.0"
        deterministic: ClassVar[bool] = True
        input_types: ClassVar[tuple[str, ...]] = ("text",)
        output_types: ClassVar[tuple[str, ...]] = ("text",)
        capabilities: ClassVar[tuple[str, ...]] = ("test",)

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            return ExtractionResult(
                content=self.marker,
                format="text",
                confidence=None,
                model=None,
                warnings=["force_rerun"],
                usage={
                    "pages": 1,
                    "tokens": None,
                    "compute_seconds": None,
                    "cost_usd": None,
                },
            )


    @dataclass(frozen=True)
    class AlternateAdapter(MarkerAdapter):
        name: ClassVar[str] = "alternate"
    """
)


def _load(tmp_path: Path, run_yaml: str):
    path = tmp_path / "config.yml"
    path.write_text(f"run:\n{textwrap.indent(run_yaml, '  ')}", encoding="utf-8")
    return load_config(path, validate_adapter=False)


def _write_config(
    path: Path,
    *,
    adapter_order: list[object] | None = None,
    adapter: str | None = None,
    adapter_options: dict | None = None,
    max_rerun_depth: int = 5,
) -> Path:
    run_config: dict[str, object] = {"max_rerun_depth": max_rerun_depth}
    if adapter_order is not None:
        run_config["adapter_order"] = adapter_order
    if adapter is not None:
        run_config["adapter"] = adapter
    if adapter_options is not None:
        run_config["adapter_options"] = adapter_options
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "taxonomy": {
                    "page_types": {
                        "prose": {"default_action": "transcribe_text"}
                    }
                },
                "run": run_config,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _chain(marker_0: str = "gen-0", marker_1: str = "gen-1"):
    return [
        {
            "adapter": "chain_adapter:MarkerAdapter",
            "adapter_options": {"marker": marker_0},
        },
        {
            "adapter": "chain_adapter:MarkerAdapter",
            "adapter_options": {"marker": marker_1},
        },
    ]


def _parent_run(tmp_path: Path, *, order: list[object] | None = None):
    (tmp_path / "chain_adapter.py").write_text(MARKER_ADAPTER, encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("source page", encoding="utf-8")
    config = _write_config(
        tmp_path / "parent.yml", adapter_order=order or _chain()
    )
    out = tmp_path / "parent"
    result = run(
        inputs=[source],
        config_path=config,
        out_dir=out,
        dry_run=False,
        adapter_path=tmp_path,
    )
    return config, out, result


def _raw_text(out_dir: Path) -> str:
    return (out_dir / "raw" / "doc_0001_page_0001.txt").read_text(
        encoding="utf-8"
    )


def test_adapter_order_normalizes_entries_and_preserves_step_zero_accessors(
    tmp_path: Path,
) -> None:
    config = _load(
        tmp_path,
        """\
adapter_order:
  - adapter: weak:Adapter
    adapter_options: {mode: fast}
  - strong:Adapter
max_rerun_depth: 0
""",
    )

    assert config.adapter_order == [
        {"adapter": "weak:Adapter", "adapter_options": {"mode": "fast"}},
        {"adapter": "strong:Adapter", "adapter_options": {}},
    ]
    assert config.adapter_name == "weak:Adapter"
    assert config.adapter_options == {"mode": "fast"}
    assert any(
        "unreachable" in warning and "max_rerun_depth=0" in warning
        for warning in config.warnings
    )


@pytest.mark.parametrize(
    ("run_yaml", "message"),
    [
        ("adapter_order: []\n", "non-empty list"),
        ("adapter_order: text\n", "non-empty list"),
        ("adapter_order: ['']\n", "non-empty adapter string"),
        ("adapter_order: [1]\n", "adapter string or a mapping"),
        ("adapter_order: [{adapter: text, extra: true}]\n", "extra is not supported"),
        (
            "adapter_order: [{adapter: text, adapter_options: []}]\n",
            "adapter_options must be a mapping",
        ),
        ("adapter: text\nadapter_order: [text]\n", "mutually exclusive"),
        (
            "adapter_options: {}\nadapter_order: [text]\n",
            "cannot be used with run.adapter_order",
        ),
    ],
)
def test_adapter_order_grammar_is_strict_and_mutually_exclusive(
    tmp_path: Path, run_yaml: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, run_yaml)


def test_generations_select_their_configured_adapter_and_record_escalation(
    tmp_path: Path,
) -> None:
    config, parent, parent_result = _parent_run(tmp_path)

    assert _raw_text(parent) == "gen-0"
    assert parent_result["escalation"] == {
        "adapter_order": [
            "chain_adapter:MarkerAdapter",
            "chain_adapter:MarkerAdapter",
        ],
        "step": 0,
    }
    parent_rerun = yaml.safe_load(
        (parent / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert parent_rerun["escalation"]["next_adapter"] == (
        "chain_adapter:MarkerAdapter"
    )

    child = tmp_path / "child"
    child_result = rerun(
        parent_dir=parent,
        config_path=config,
        out_dir=child,
        adapter_path=tmp_path,
    )
    assert _raw_text(child) == "gen-1"
    assert child_result["escalation"]["step"] == 1
    assert "escalation_warnings" not in child_result
    child_manifest = json.loads(
        (child / "manifest.json").read_text(encoding="utf-8")
    )
    assert child_manifest["parent_run_id"] == parent_result["run_id"]
    assert child_manifest["escalation"]["step"] == 1
    provenance = json.loads(
        (child / "provenance.jsonl").read_text(encoding="utf-8")
    )
    assert provenance["extractor"]["adapter"] == "marker"
    assert provenance["extractor"]["adapter_version"] == "1.0"


def test_chain_exhaustion_clears_items_before_the_depth_cap(tmp_path: Path) -> None:
    config, parent, _ = _parent_run(tmp_path)
    child = tmp_path / "child"
    rerun(
        parent_dir=parent,
        config_path=config,
        out_dir=child,
        adapter_path=tmp_path,
    )

    child_rerun = yaml.safe_load(
        (child / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert child_rerun["max_rerun_depth"] == 5
    assert child_rerun["rerun_status"] == "chain_exhausted"
    assert child_rerun["rerun_executable"] is False
    assert child_rerun["items"] == []
    assert child_rerun["escalation"]["next_adapter"] is None

    with pytest.raises(ValueError, match="Adapter chain exhausted"):
        rerun(
            parent_dir=child,
            config_path=config,
            out_dir=tmp_path / "grandchild",
            adapter_path=tmp_path,
        )


def test_supplied_chain_conflict_warns_and_config_wins(tmp_path: Path) -> None:
    _, parent, _ = _parent_run(tmp_path)
    conflicting = _write_config(
        tmp_path / "conflicting.yml",
        adapter_order=[
            _chain()[0],
            {
                "adapter": "chain_adapter:AlternateAdapter",
                "adapter_options": {"marker": "conflict"},
            },
        ],
    )

    child = tmp_path / "conflict-child"
    result = rerun(
        parent_dir=parent,
        config_path=conflicting,
        out_dir=child,
        adapter_path=tmp_path,
    )

    assert _raw_text(child) == "conflict"
    assert result["escalation"]["step"] == 1
    assert len(result["escalation_warnings"]) == 1
    assert "parent planned" in result["escalation_warnings"][0]
    assert "config wins" in result["escalation_warnings"][0]


def test_supplied_single_adapter_overrides_parent_chain_with_warning(
    tmp_path: Path,
) -> None:
    _, parent, _ = _parent_run(tmp_path)
    override = _write_config(
        tmp_path / "override.yml",
        adapter="chain_adapter:AlternateAdapter",
        adapter_options={"marker": "override"},
    )

    child = tmp_path / "override-child"
    result = rerun(
        parent_dir=parent,
        config_path=override,
        out_dir=child,
        adapter_path=tmp_path,
    )

    assert _raw_text(child) == "override"
    assert "escalation" not in result
    assert len(result["escalation_warnings"]) == 1
    assert "overrides the parent adapter chain" in result["escalation_warnings"][0]


def test_depth_cap_is_independent_of_a_longer_adapter_chain(tmp_path: Path) -> None:
    order = [
        *_chain(),
        {
            "adapter": "chain_adapter:AlternateAdapter",
            "adapter_options": {"marker": "unreachable"},
        },
    ]
    (tmp_path / "chain_adapter.py").write_text(MARKER_ADAPTER, encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("source page", encoding="utf-8")
    config = _write_config(
        tmp_path / "depth.yml",
        adapter_order=order,
        max_rerun_depth=1,
    )
    parent = tmp_path / "parent"
    parent_result = run(
        inputs=[source],
        config_path=config,
        out_dir=parent,
        dry_run=False,
        adapter_path=tmp_path,
    )
    assert any("unreachable" in item for item in parent_result["config_warnings"])

    child = tmp_path / "child"
    rerun(
        parent_dir=parent,
        config_path=config,
        out_dir=child,
        adapter_path=tmp_path,
    )
    child_rerun = yaml.safe_load(
        (child / "rerun-manifest.yml").read_text(encoding="utf-8")
    )
    assert child_rerun["rerun_status"] == "no_further_generations"
    assert child_rerun["items"] == []
    assert child_rerun["escalation"]["next_adapter"] == (
        "chain_adapter:AlternateAdapter"
    )

    with pytest.raises(ValueError, match="Max rerun depth"):
        rerun(
            parent_dir=child,
            config_path=config,
            out_dir=tmp_path / "grandchild",
            adapter_path=tmp_path,
        )


def test_manifest_adapter_order_contains_names_not_options(tmp_path: Path) -> None:
    _, parent, _ = _parent_run(tmp_path)
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["escalation"] == {
        "adapter_order": [
            "chain_adapter:MarkerAdapter",
            "chain_adapter:MarkerAdapter",
        ],
        "step": 0,
    }
