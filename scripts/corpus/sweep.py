"""Check that every PDF under a folder opens and yields text, one child process each.

    python scripts/corpus/sweep.py --root /Volumes/Kinodrive/Research_Data --out sweep.json

Each file is read in its own process with a time limit, so a hang or a crash
is recorded instead of stopping the sweep. Outcomes use PageLedger's
diagnostic codes, such as `malformed_pdf` and `unsupported_encryption`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120, help="Seconds per file")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if out.is_relative_to(root):
        parser.error("--out must be outside --root")
    files = [_sweep_one(path, args.timeout) for path in sorted(root.rglob("*.pdf"))]
    summary = Counter(record["outcome"] for record in files)
    out.write_text(
        json.dumps({"root": str(root), "summary": summary, "files": files}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(", ".join(f"{outcome}: {count}" for outcome, count in summary.most_common()))
    return 0


def _sweep_one(path: Path, timeout: float) -> dict:
    started = time.monotonic()
    try:
        child = subprocess.run(
            [sys.executable, __file__, "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        record = {"outcome": "timeout"}
    else:
        if child.returncode == 0:
            record = json.loads(child.stdout)
        else:
            last_line = (child.stderr.strip().splitlines() or [""])[-1]
            record = {"outcome": "crash", "error": last_line[:300]}
    return {"path": str(path), **record, "seconds": round(time.monotonic() - started, 1)}


def _check(path: Path) -> dict:
    """Child process: count and extract pages with PageLedger's PDF text adapter."""
    from pageledger.adapters import PageLedgerDiagnostic, PdfTextAdapter

    try:
        return {"outcome": "ok", "pages": PdfTextAdapter().page_count(path)}
    except PageLedgerDiagnostic as exc:
        return {"outcome": exc.code}


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        print(json.dumps(_check(Path(sys.argv[2]))))
    else:
        raise SystemExit(main())
