"""The corpus harness: public manifest, runner, comparison and sweep."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

from pageledger.config import load_config

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "corpus"))

import compare  # noqa: E402
import run  # noqa: E402
import sweep  # noqa: E402


def test_profiles_load():
    for profile in (ROOT / "scripts" / "corpus" / "profiles").glob("*.yml"):
        load_config(profile, validate_adapter=False)


def test_public_manifest_lists_only_verifiable_public_items():
    items = yaml.safe_load((ROOT / "corpus" / "manifest.public.yml").read_text())["items"]
    assert items
    for item in items:
        assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), item["id"]
        assert item["rights"]["public"] is True, item["id"]
        assert item["source"]["url"].startswith("https://"), item["id"]
        assert all(tier["pages"] for tier in item["tiers"].values()), item["id"]


def _corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "ok.txt").write_text("First page of prose.\fSecond page of prose.")
    (root / "changed.txt").write_text("changed")
    (root / "unhashed.txt").write_text("no checksum")
    items = [
        {
            "id": "ok",
            "path": "ok.txt",
            "sha256": hashlib.sha256(b"First page of prose.\fSecond page of prose.").hexdigest(),
        },
        {"id": "changed", "path": "changed.txt", "sha256": "0" * 64},
        {"id": "missing", "path": "absent.txt", "sha256": "1" * 64},
        {"id": "unhashed", "path": "unhashed.txt", "sha256": None},
    ]
    for item in items:
        item["tiers"] = {"S": {"pages": "all", "purpose": "test"}}
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(yaml.safe_dump({"schema_version": "0.1", "items": items}))
    profile = tmp_path / "profile.yml"
    profile.write_text('schema_version: "0.1"\n')
    return root, manifest, profile


def _run(tmp_path: Path, out: Path) -> int:
    root, manifest, profile = _corpus(tmp_path)
    return run.main(
        [
            "--manifest", str(manifest), "--tier", "S", "--profile", str(profile),
            "--corpus-root", str(root), "--out", str(out),
        ]
    )  # fmt: skip


def test_runner_checks_sources_before_running_them(tmp_path):
    out = tmp_path / "runs" / "one"
    assert _run(tmp_path, out) == 0

    items = {i["item_id"]: i for i in json.loads((out / "run.json").read_text())["items"]}
    assert items["ok"]["status"] == "ok" and items["ok"]["pages"] == 2
    assert len(items["ok"]["package_sha256"]) == 64
    assert items["changed"]["failure"] == "checksum_mismatch"
    assert items["missing"]["failure"] == "missing_file"
    assert items["unhashed"]["failure"] == "no_checksum"
    assert not (out / "changed").exists()

    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert [(r["item_id"], r["page_number"]) for r in rows] == [("ok", 1), ("ok", 2)]
    assert (out / rows[0]["selected_output"]).read_text() == "First page of prose."
    attempt = rows[0]["attempts"][0]
    assert attempt["adapter"] == "text" and attempt["failure"] is None
    assert (out / attempt["output"]).read_text() == "First page of prose."
    assert "| changed | failed | 0 | checksum_mismatch |" in (out / "summary.md").read_text()


def test_runner_refuses_output_inside_the_corpus(tmp_path):
    with pytest.raises(SystemExit, match="outside"):
        _run(tmp_path, tmp_path / "corpus" / "runs")


def test_compare_shows_only_changed_pages(tmp_path):
    for name, second in (("a", "Second page of prose."), ("b", "Second page, now changed.")):
        run_dir = tmp_path / name
        (run_dir / "job").mkdir(parents=True)
        info = {"pageledger_version": f"pageledger {name}", "items": [{"package_sha256": name}]}
        (run_dir / "run.json").write_text(json.dumps(info))
        rows = []
        for number, text in ((1, "First page of prose."), (2, second)):
            (run_dir / "job" / f"{number}.txt").write_text(text)
            attempt = {"stage": "local_text", "model": None, "adapter": "text", "seconds": 1.0}
            rows.append(
                {
                    "item_id": "ok",
                    "page_number": number,
                    "disposition": "unreviewed_text",
                    "review_reasons": [],
                    "selected_output": f"job/{number}.txt",
                    "attempts": [attempt],
                }
            )
        (run_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    report = compare.compare(tmp_path / "a", tmp_path / "b")

    assert "PageLedger: pageledger a → pageledger b; code changed." in report
    assert "| ok | 2 |" in report
    assert "| ok | 1 |" not in report


def test_sweep_records_outcomes_by_diagnostic_code(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    root = tmp_path / "pdfs"
    root.mkdir()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(root / "good.pdf")
    (root / "damaged.pdf").write_bytes(b"not a pdf at all")
    (root / "notes.txt").write_text("ignored")
    out = tmp_path / "sweep.json"

    assert sweep.main(["--root", str(root), "--out", str(out)]) == 0

    files = {Path(f["path"]).name: f for f in json.loads(out.read_text())["files"]}
    assert files["good.pdf"]["outcome"] == "ok"
    assert files["damaged.pdf"]["outcome"] == "malformed_pdf"
    assert "notes.txt" not in files
