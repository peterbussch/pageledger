"""CLI for frozen PageLedger benchmark measurements."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import measure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.pageledger_bench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="Measure one frozen workload in this fresh CLI process"
    )
    run_parser.add_argument(
        "--workload", required=True, choices=("primary", "generalization")
    )
    run_parser.add_argument(
        "--out", required=True, type=Path, help="New benchmark evidence directory"
    )
    run_parser.add_argument(
        "--process-state",
        choices=("fresh-process", "cold", "warm"),
        default="fresh-process",
        help="Caller-declared process/system warmup state; OS page cache remains uncontrolled",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parsed_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(parsed_argv)
    command = [sys.executable, "-m", "scripts.pageledger_bench", *parsed_argv]
    try:
        receipt = measure.measure_run(
            args.workload,
            args.out,
            process_state=args.process_state,
            command=command,
        )
    except measure.BenchmarkError as exc:
        print(
            json.dumps({"status": "error", "code": exc.code, "error": str(exc)}),
            flush=True,
        )
        print(f"pageledger-bench: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
