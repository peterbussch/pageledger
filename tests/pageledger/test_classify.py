"""End-to-end classification, evidence, and route-map execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pageledger.classifier import classify
from pageledger.runner import run
from pageledger.verify import verify_run

FULL_CONFIG = """\
schema_version: "0.1"
taxonomy:
  page_types:
    blank: {default_action: skip}
    sparse: {default_action: review}
    prose: {default_action: transcribe_text, prompt: Preserve spelling.}
    table_likely: {default_action: review}
    unknown: {default_action: review}
run:
  adapter: text
"""


def _write_config(tmp_path: Path, text: str = FULL_CONFIG) -> Path:
    path = tmp_path / "pageledger.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_classify_round_trip_executes_and_verifies(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text(" ".join(["ordinary"] * 26) + "\f\fshort page", encoding="utf-8")
    config = _write_config(tmp_path)
    route_path = tmp_path / "rm.yml"

    result = classify(
        inputs=[source], config_path=config, out_path=route_path
    )
    route_map = yaml.safe_load(route_path.read_text(encoding="utf-8"))

    assert result["pages"] == 3
    assert Path(result["evidence_path"]) == tmp_path / "rm.evidence.jsonl"
    assert route_map["classifier"] == {
        "adapter": "builtin:structural",
        "model": "text/0.1",
        "prompt_hash": None,
    }
    assert [page["type"] for page in route_map["documents"][0]["pages"]] == [
        "prose",
        "blank",
        "sparse",
    ]
    assert route_map["documents"][0]["pages"][0]["prompt"] == "Preserve spelling."

    out_dir = tmp_path / "run"
    run_result = run(
        inputs=[source],
        config_path=config,
        routes_path=route_path,
        out_dir=out_dir,
        dry_run=False,
    )
    assert run_result["summary"]["pages_extracted"] == 1
    assert run_result["summary"]["pages_skipped"] == 1
    assert verify_run(out_dir)["status"] == "pass"


def test_no_taxonomy_emits_conservative_review_routes_that_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text(" ".join(["ordinary"] * 26), encoding="utf-8")
    config = _write_config(
        tmp_path,
        'schema_version: "0.1"\ntaxonomy:\n  page_types: {}\nrun:\n  adapter: text\n',
    )
    route_path = tmp_path / "rm.yml"
    classify(inputs=[source], config_path=config, out_path=route_path)
    route_map = yaml.safe_load(route_path.read_text(encoding="utf-8"))
    assert route_map["documents"][0]["pages"][0]["action"] == "review"

    out_dir = tmp_path / "run"
    run(
        inputs=[source],
        config_path=config,
        routes_path=route_path,
        out_dir=out_dir,
        dry_run=False,
    )
    assert verify_run(out_dir)["status"] == "pass"


def test_nonempty_taxonomy_must_cover_all_emittable_types(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("ordinary prose", encoding="utf-8")
    config = _write_config(
        tmp_path,
        'schema_version: "0.1"\ntaxonomy:\n  page_types:\n    prose: {default_action: transcribe_text}\nrun:\n  adapter: text\n',
    )

    with pytest.raises(ValueError, match="blank, sparse, table_likely, unknown"):
        classify(inputs=[source], config_path=config, out_path=tmp_path / "rm.yml")


def test_min_confidence_forces_review(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text(" ".join(["ordinary"] * 26), encoding="utf-8")
    config = _write_config(tmp_path, FULL_CONFIG + "classify:\n  min_confidence: 0.8\n")
    route_path = tmp_path / "rm.yml"

    classify(inputs=[source], config_path=config, out_path=route_path)
    page = yaml.safe_load(route_path.read_text(encoding="utf-8"))["documents"][0]["pages"][0]
    assert page["type"] == "prose"
    assert page["confidence"] == 0.7
    assert page["action"] == "review"


def test_probe_failure_becomes_unknown_review(tmp_path: Path) -> None:
    adapter_module = tmp_path / "failing_probe.py"
    adapter_module.write_text(
        "class Probe:\n"
        "    name = 'failing'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def page_count(self, source): return 1\n"
        "    def extract(self, *args, **kwargs): raise OSError('probe broke')\n",
        encoding="utf-8",
    )
    source = tmp_path / "fixture.txt"
    source.write_text("content", encoding="utf-8")
    route_path = tmp_path / "rm.yml"

    classify(
        inputs=[source],
        config_path=None,
        out_path=route_path,
        probe_adapter="failing_probe:Probe",
        adapter_path=tmp_path,
    )
    page = yaml.safe_load(route_path.read_text(encoding="utf-8"))["documents"][0]["pages"][0]
    assert page == {
        "page_id": "doc_0001_page_0001",
        "page_number": 1,
        "type": "unknown",
        "confidence": None,
        "action": "review",
        "reason": "probe_failed:OSError",
    }


def test_custom_hook_replaces_builtin_and_bad_output_is_rejected(tmp_path: Path) -> None:
    hook_module = tmp_path / "domain_hook.py"
    hook_module.write_text(
        "from pageledger.classifier import ClassificationResult\n"
        "class Hook:\n"
        "    name = 'domain'\n"
        "    version = '2.0'\n"
        "    page_types = ('letter',)\n"
        "    def __init__(self, bad=False): self.bad = bad\n"
        "    def classify_page(self, **kwargs):\n"
        "        if self.bad: return {'type': 'letter'}\n"
        "        assert kwargs['signals']['builtin_type']\n"
        "        return ClassificationResult('letter', 0.9, 'domain_rule')\n",
        encoding="utf-8",
    )
    source = tmp_path / "fixture.txt"
    source.write_text("letter body", encoding="utf-8")
    good_config = _write_config(
        tmp_path,
        'schema_version: "0.1"\n'
        "taxonomy:\n  page_types:\n"
        "    letter: {default_action: transcribe_text}\n"
        "    unknown: {default_action: review}\n"
        "classify:\n  hook: domain_hook:Hook\n"
        "run:\n  adapter: text\n",
    )
    route_path = tmp_path / "rm.yml"
    classify(
        inputs=[source],
        config_path=good_config,
        out_path=route_path,
        adapter_path=tmp_path,
    )
    route_map = yaml.safe_load(route_path.read_text(encoding="utf-8"))
    assert route_map["documents"][0]["pages"][0]["type"] == "letter"
    assert route_map["classifier"] == {
        "adapter": "domain_hook:Hook",
        "model": "domain/2.0",
        "prompt_hash": None,
    }

    bad_config = tmp_path / "bad.yml"
    bad_config.write_text(
        good_config.read_text(encoding="utf-8").replace(
            "hook: domain_hook:Hook", "hook: domain_hook:Hook\n  hook_options: {bad: true}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected ClassificationResult"):
        classify(
            inputs=[source],
            config_path=bad_config,
            out_path=tmp_path / "bad-rm.yml",
            adapter_path=tmp_path,
        )


def test_probe_and_from_run_decisions_are_equal(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text(" ".join(["ordinary"] * 26), encoding="utf-8")
    config = _write_config(tmp_path)
    direct_route = tmp_path / "direct.yml"
    classify(inputs=[source], config_path=config, out_path=direct_route)
    parent = tmp_path / "parent"
    run(inputs=[source], config_path=config, out_dir=parent, dry_run=False)
    from_run_route = tmp_path / "from-run.yml"
    classify(
        inputs=[],
        config_path=config,
        out_path=from_run_route,
        from_run=parent,
    )

    direct = _read_jsonl(tmp_path / "direct.evidence.jsonl")[0]
    reclassified = _read_jsonl(tmp_path / "from-run.evidence.jsonl")[0]
    assert reclassified["signals"] == direct["signals"]
    assert reclassified["decision"] == direct["decision"]


def test_from_run_rejects_dry_rerun_and_partial_parents(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("first page\fsecond page", encoding="utf-8")
    config = _write_config(tmp_path)

    dry_parent = tmp_path / "dry"
    run(inputs=[source], config_path=config, out_dir=dry_parent, dry_run=True)
    with pytest.raises(ValueError, match="dry-run parent"):
        classify(
            inputs=[], config_path=config, out_path=tmp_path / "dry.yml", from_run=dry_parent
        )

    partial_parent = tmp_path / "partial"
    run(
        inputs=[source],
        config_path=config,
        out_dir=partial_parent,
        dry_run=False,
        pages="1",
    )
    with pytest.raises(ValueError, match="--pages partial parent"):
        classify(
            inputs=[],
            config_path=config,
            out_path=tmp_path / "partial.yml",
            from_run=partial_parent,
        )

    manifest_path = partial_parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"][0].pop("pages")
    manifest["parent_run_id"] = "earlier-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="rerun parent"):
        classify(
            inputs=[],
            config_path=config,
            out_path=tmp_path / "rerun.yml",
            from_run=partial_parent,
        )


def test_from_run_missing_raw_is_unknown_and_changed_source_warns(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("short page", encoding="utf-8")
    config = _write_config(tmp_path)
    parent = tmp_path / "parent"
    run(inputs=[source], config_path=config, out_dir=parent, dry_run=False)
    raw_path = next((parent / "raw").iterdir())
    raw_path.unlink()
    source.write_text("changed bytes", encoding="utf-8")

    route_path = tmp_path / "from-run.yml"
    result = classify(
        inputs=[], config_path=config, out_path=route_path, from_run=parent
    )
    page = yaml.safe_load(route_path.read_text(encoding="utf-8"))["documents"][0]["pages"][0]
    assert page["type"] == "unknown"
    assert page["reason"] == "no_parent_evidence"
    assert any("Source changed since parent run" in warning for warning in result["warnings"])


def test_cli_json_success_and_source_mode_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger.cli import main

    source = tmp_path / "fixture.txt"
    source.write_text("short page", encoding="utf-8")
    exit_code = main(
        ["classify", str(source), "--out", str(tmp_path / "rm.yml"), "--json"]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["pages"] == 1

    exit_code = main(["classify", "--out", str(tmp_path / "bad.yml"), "--json"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "requires input paths" in json.loads(captured.out)["error"]


def test_generated_config_is_ready_for_classify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pageledger.cli import main

    config = tmp_path / "generated.yml"
    assert main(["init-config", "--out", str(config)]) == 0
    capsys.readouterr()
    source = tmp_path / "fixture.txt"
    source.write_text("short page", encoding="utf-8")

    assert main(
        [
            "classify",
            str(source),
            "--config",
            str(config),
            "--out",
            str(tmp_path / "rm.yml"),
            "--json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["pages"] == 1


def test_from_run_relative_inputs_do_not_depend_on_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    config = _write_config(work_dir)
    monkeypatch.chdir(work_dir)
    Path("fixture.txt").write_text(" ".join(["ordinary"] * 26), encoding="utf-8")
    run(
        inputs=[Path("fixture.txt")],
        config_path=Path("pageledger.yml"),
        out_dir=Path("parent"),
        dry_run=False,
    )
    monkeypatch.chdir(tmp_path)

    result = classify(
        inputs=[],
        config_path=config,
        out_path=tmp_path / "rm.yml",
        from_run=work_dir / "parent",
    )
    assert result["pages"] == 1


def test_from_run_rejects_raw_artifact_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text(" ".join(["ordinary"] * 26), encoding="utf-8")
    config = _write_config(tmp_path)
    parent = tmp_path / "parent"
    run(inputs=[source], config_path=config, out_dir=parent, dry_run=False)
    provenance_path = parent / "provenance.jsonl"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["result"]["raw_artifact"] = "../outside.txt"
    provenance_path.write_text(json.dumps(provenance) + "\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("not page evidence", encoding="utf-8")

    with pytest.raises(ValueError, match="must be raw/doc_0001_page_0001"):
        classify(
            inputs=[],
            config_path=config,
            out_path=tmp_path / "rm.yml",
            from_run=parent,
        )


def test_duplicate_classification_sources_fail_before_emission(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate source"):
        classify(
            inputs=[source, tmp_path],
            config_path=None,
            out_path=tmp_path / "rm.yml",
        )
    assert not (tmp_path / "rm.yml").exists()
