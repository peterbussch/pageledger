"""Compare two corpus runs page by page, as Markdown.

    python scripts/corpus/compare.py RUN_A RUN_B [--out FILE]

Rows show pages whose text, disposition or review reasons changed. The engines
column tells code changes (same engines) from engine changes.
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_a", type=Path)
    parser.add_argument("run_b", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = compare(args.run_a, args.run_b)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


def compare(run_a: Path, run_b: Path) -> str:
    info_a, pages_a = _load(run_a)
    info_b, pages_b = _load(run_b)
    shared = sorted(pages_a.keys() & pages_b.keys())
    lines = [
        "# Corpus run comparison",
        "",
        f"PageLedger: {info_a['pageledger_version']} → {info_b['pageledger_version']}; "
        f"code {'changed' if _packages(info_a) != _packages(info_b) else 'unchanged'}.",
        f"Pages compared: {len(shared)}; only in A: {len(pages_a.keys() - pages_b.keys())}; "
        f"only in B: {len(pages_b.keys() - pages_a.keys())}.",
        "",
        "| Item | Page | Text similarity | Disposition | Reasons added | Reasons removed "
        "| Seconds | Engines |",
        "|---|---:|---:|---|---|---|---|---|",
    ]
    for key in shared:
        a, b = pages_a[key], pages_b[key]
        similarity = difflib.SequenceMatcher(
            None, _text(run_a, a), _text(run_b, b), autojunk=False
        ).ratio()
        added = sorted(set(b["review_reasons"]) - set(a["review_reasons"]))
        removed = sorted(set(a["review_reasons"]) - set(b["review_reasons"]))
        if similarity == 1 and a["disposition"] == b["disposition"] and not added + removed:
            continue
        engines = _engines(a) if _engines(a) == _engines(b) else f"{_engines(a)} → {_engines(b)}"
        lines.append(
            f"| {key[0]} | {key[1]} | {similarity:.3f} | {a['disposition']} → {b['disposition']} "
            f"| {', '.join(added)} | {', '.join(removed)} | {_seconds(a)} → {_seconds(b)} "
            f"| {engines} |"
        )
    return "\n".join(lines) + "\n"


def _load(run: Path) -> tuple[dict, dict[tuple[str, int], dict]]:
    info = json.loads((run / "run.json").read_text(encoding="utf-8"))
    lines = (run / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = (json.loads(line) for line in lines if line)
    return info, {(row["item_id"], row["page_number"]): row for row in rows}


def _packages(info: dict) -> set[str]:
    return {item["package_sha256"] for item in info["items"] if "package_sha256" in item}


def _text(run: Path, row: dict) -> str:
    path = row["selected_output"]
    return (run / path).read_text(encoding="utf-8") if path else ""


def _engines(row: dict) -> str:
    engines = {f"{a['stage']}: {a['model'] or a['adapter']}" for a in row["attempts"]}
    return "; ".join(sorted(engines))


def _seconds(row: dict) -> str:
    return f"{sum(a['seconds'] or 0 for a in row['attempts']):.1f}"


if __name__ == "__main__":
    raise SystemExit(main())
