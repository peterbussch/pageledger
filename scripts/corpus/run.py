"""Run one tier of a corpus manifest through PageLedger document jobs.

    python scripts/corpus/run.py --manifest corpus/manifest.public.yml --tier S \\
        --profile scripts/corpus/profiles/local.yml --corpus-root /path/to/corpus

Each item's `path` is read under `--corpus-root` and checked against its
manifest SHA-256 before it runs. The output directory holds one job per item,
`results.jsonl` (one line per page), `summary.md` and `run.json`.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pageledger.checkpoint import file_digest, read_record

REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.corpus_root.expanduser().resolve()
    out = (args.out or _default_out(args.tier, args.profile)).expanduser().resolve()
    for forbidden in (REPO, root):
        if out.is_relative_to(forbidden):
            raise SystemExit(f"corpus runner: the output directory must be outside {forbidden}")
    pageledger = (
        shlex.split(args.pageledger) if args.pageledger else [sys.executable, "-m", "pageledger"]
    )
    version = subprocess.run(
        [*pageledger, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    items = _tier_items(args.manifest, args.tier, args.only)
    out.mkdir(parents=True)

    summaries, rows = [], []
    for item in items:
        summary, pages = _run_item(item, args.tier, root, out, args.profile.resolve(), pageledger)
        summaries.append(summary)
        rows.extend(pages)

    with (out / "results.jsonl").open("w", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (out / "summary.md").write_text(_summary(summaries, len(rows)), encoding="utf-8")
    run_info = {
        "tier": args.tier,
        "pageledger_version": version,
        "pageledger_command": pageledger,
        "profile": str(args.profile),
        "manifests": [{"path": str(m), "sha256": file_digest(m)} for m in args.manifest],
        "items": summaries,
    }
    (out / "run.json").write_text(json.dumps(run_info, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 1 if any(s["status"] == "verification_failed" for s in summaries) else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--tier", choices=("S", "M", "L"), required=True)
    parser.add_argument("--profile", type=Path, required=True, help="Processing config to use")
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="Default: ~/pageledger-corpus-runs/<time>-...")
    parser.add_argument(
        "--pageledger", help="Command that runs PageLedger, e.g. a 0.5.2 install for a baseline"
    )
    parser.add_argument("--only", nargs="+", metavar="ID", help="Run only these item ids")
    return parser


def _default_out(tier: str, profile: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / "pageledger-corpus-runs" / f"{stamp}-{tier}-{profile.stem}"


def _tier_items(manifests: list[Path], tier: str, only: list[str] | None) -> list[dict]:
    items = []
    for manifest in manifests:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        items.extend(item for item in data["items"] if tier in item.get("tiers", {}))
    if only:
        unknown = set(only) - {item["id"] for item in items}
        if unknown:
            raise SystemExit(f"corpus runner: not in tier {tier}: {', '.join(sorted(unknown))}")
        items = [item for item in items if item["id"] in only]
    return items


def _run_item(
    item: dict, tier: str, root: Path, out: Path, profile: Path, pageledger: list[str]
) -> tuple[dict, list[dict]]:
    summary = {"item_id": item["id"], "status": "failed", "pages": 0}
    source = (root / item["path"]).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return {**summary, "failure": "missing_file"}, []
    if not item.get("sha256"):
        return {**summary, "failure": "no_checksum"}, []
    if file_digest(source) != item["sha256"]:
        return {**summary, "failure": "checksum_mismatch"}, []

    job = out / item["id"]
    command = [*pageledger, "process", str(source), "--config", str(profile), "--out", str(job)]
    pages = str(item["tiers"][tier]["pages"])
    if pages != "all":
        command += ["--pages", pages]
    processed = subprocess.run(command, capture_output=True, text=True)
    if not (job / "document.json").is_file():
        return {**summary, "failure": "process_failed", "stderr": processed.stderr[-2000:]}, []
    verified = subprocess.run([*pageledger, "verify-job", str(job)], capture_output=True, text=True)
    rows = _page_rows(item["id"], job, out)
    status = "ok" if verified.returncode == 0 else "verification_failed"
    package = read_record(job / "job.json")["package_sha256"]
    return {**summary, "status": status, "pages": len(rows), "package_sha256": package}, rows


def _page_rows(item_id: str, job: Path, out: Path) -> list[dict]:
    report = json.loads((job / "document.json").read_text(encoding="utf-8"))
    runs = {a["run_path"] for page in report["pages"] for a in page["attempts"]}
    provenance = {run: _provenance(job / run) for run in runs}
    rows = []
    for page in report["pages"]:
        selected = page.get("selected_output")
        rows.append(
            {
                "item_id": item_id,
                "page_number": page["page_number"],
                "disposition": page["disposition"],
                "review_reasons": page["review_reasons"],
                "selected_attempt": selected["attempt_id"] if selected else None,
                "selected_output": _relative(job, selected["path"], out) if selected else None,
                "attempts": [
                    _attempt(
                        attempt,
                        provenance[attempt["run_path"]],
                        _relative(job, attempt["raw_artifact"], out),
                    )
                    for attempt in page["attempts"]
                ],
            }
        )
    return rows


def _relative(job: Path, artifact: str | None, out: Path) -> str | None:
    return None if artifact is None else str((job / artifact).relative_to(out))


def _attempt(attempt: dict, provenance: dict[str, dict], output: str | None) -> dict:
    """An attempt with the engine and timing its child run recorded in provenance."""
    record = provenance.get(attempt["page_id"], {})
    failure = attempt["failure"] or {}
    return {
        "attempt_id": attempt["attempt_id"],
        "stage": attempt["stage"],
        "outcome": attempt["outcome"],
        "failure": failure.get("code") or failure.get("type"),
        "warnings": attempt["warnings"],
        "adapter": record.get("extractor", {}).get("adapter"),
        "model": record.get("extractor", {}).get("model"),
        "seconds": record.get("extraction_seconds"),
        "usage": attempt["usage"],
        "output": output,
    }


def _provenance(run: Path) -> dict[str, dict]:
    path = run / "provenance.jsonl"
    if not path.is_file():
        return {}
    lines = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return {line["page_id"]: line for line in lines}


def _summary(summaries: list[dict], page_count: int) -> str:
    lines = ["# Corpus run", "", "| Item | Status | Pages | Failure |", "|---|---|---:|---|"]
    for s in summaries:
        lines.append(f"| {s['item_id']} | {s['status']} | {s['pages']} | {s.get('failure', '')} |")
    failed = sum(s["status"] != "ok" for s in summaries)
    lines += ["", f"{len(summaries)} items, {page_count} pages, {failed} not ok."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
