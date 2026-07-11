"""Execution of reviewed, externally produced page route maps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

CONFIG = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose: {default_action: transcribe_text}
    blank: {default_action: skip}
    figure: {default_action: review}
run:
  adapter: text
"""


def _route_map(source: Path) -> dict:
    return {
        "schema_version": "0.1",
        "run_id": "classification-plan",
        "generated_at": "2026-07-10T12:00:00Z",
        "classifier": {
            "adapter": "example.classifier",
            "model": "rules-v1",
            "prompt_hash": None,
        },
        "documents": [
            {
                "source": str(source),
                "page_count": 3,
                "pages": [
                    {
                        "page_id": "doc_0001_page_0001",
                        "page_number": 1,
                        "type": "prose",
                        "confidence": 0.9,
                        "action": "transcribe_text",
                        "reason": "classified",
                        "prompt": "Preserve spelling.",
                    },
                    {
                        "page_id": "doc_0001_page_0002",
                        "page_number": 2,
                        "type": "blank",
                        "confidence": 1.0,
                        "action": "skip",
                        "reason": "classified_blank",
                    },
                    {
                        "page_id": "doc_0001_page_0003",
                        "page_number": 3,
                        "type": "figure",
                        "confidence": 0.6,
                        "action": "review",
                        "reason": "classified_figure",
                        "prompt": "Describe the figure for review.",
                    },
                ],
            }
        ],
    }


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source.txt"
    source.write_text("first page\fsecond page\fthird page", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(CONFIG, encoding="utf-8")
    routes = tmp_path / "routes.yml"
    routes.write_text(yaml.safe_dump(_route_map(source), sort_keys=False), encoding="utf-8")
    return source, config, routes


def test_run_executes_reviewed_routes(tmp_path: Path) -> None:
    from pageledger.runner import run
    from pageledger.verify import verify_run

    source, config, routes = _write_inputs(tmp_path)
    out_dir = tmp_path / "run"
    result = run(
        inputs=[source],
        config_path=config,
        routes_path=routes,
        out_dir=out_dir,
        dry_run=False,
    )

    assert result["summary"]["pages_extracted"] == 1
    assert result["summary"]["pages_skipped"] == 1
    assert (out_dir / "raw/doc_0001_page_0001.txt").read_text() == "first page"
    assert not (out_dir / "raw/doc_0001_page_0002.txt").exists()
    written = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    assert written["run_id"] == result["run_id"]
    assert written["classifier"]["adapter"] == "example.classifier"
    assert [page["action"] for page in written["documents"][0]["pages"]] == [
        "transcribe_text",
        "skip",
        "review",
    ]
    provenance = json.loads((out_dir / "provenance.jsonl").read_text())
    assert provenance["route"] == {
        "type": "prose",
        "action": "transcribe_text",
        "route_confidence": 0.9,
    }
    audit = json.loads((out_dir / "audit.json").read_text())
    assert audit["review_queue"][0]["reason"] == "classified_figure"
    assert "prompt" not in audit["review_queue"][0]
    assert verify_run(out_dir)["status"] == "pass"


def test_route_dry_run_preserves_decisions(tmp_path: Path) -> None:
    from pageledger.runner import run

    source, config, routes = _write_inputs(tmp_path)
    out_dir = tmp_path / "run"
    run(
        inputs=[source],
        config_path=config,
        routes_path=routes,
        out_dir=out_dir,
        dry_run=True,
    )

    written = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    assert written["documents"][0]["pages"][0]["action"] == "transcribe_text"
    assert list((out_dir / "raw").iterdir()) == []


def test_route_map_must_cover_every_source_page(tmp_path: Path) -> None:
    from pageledger.runner import run

    source, config, routes = _write_inputs(tmp_path)
    route_data = yaml.safe_load(routes.read_text())
    route_data["documents"][0]["pages"].pop()
    routes.write_text(yaml.safe_dump(route_data), encoding="utf-8")

    with pytest.raises(ValueError, match="cover every page"):
        run(
            inputs=[source],
            config_path=config,
            routes_path=routes,
            out_dir=tmp_path / "run",
            dry_run=False,
        )
    assert not (tmp_path / "run").exists()


def test_route_action_must_be_supported_before_output_is_created(tmp_path: Path) -> None:
    from pageledger.runner import run

    source, config, routes = _write_inputs(tmp_path)
    route_data = yaml.safe_load(routes.read_text())
    route_data["documents"][0]["pages"][0]["action"] = "vlm_table"
    routes.write_text(yaml.safe_dump(route_data), encoding="utf-8")

    with pytest.raises(ValueError, match="does not support action 'vlm_table'"):
        run(
            inputs=[source],
            config_path=config,
            routes_path=routes,
            out_dir=tmp_path / "run",
            dry_run=False,
        )
    assert not (tmp_path / "run").exists()


def test_cli_accepts_routes_with_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pageledger.cli import main

    source, config, routes = _write_inputs(tmp_path)
    exit_code = main([
        "run",
        str(source),
        "--config",
        str(config),
        "--routes",
        str(routes),
        "--out",
        str(tmp_path / "run"),
        "--json",
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["summary"]["pages_extracted"] == 1
