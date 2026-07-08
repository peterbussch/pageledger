"""Command-line entry point for the PageLedger alpha runner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

from .aligner import align_run
from .compare import compare_runs, render_comparison
from .doctor import build_doctor_report
from .grading import GRADES
from .runner import inspect_run, rerun, run, run_pages_csv

MINIMAL_CONFIG = textwrap.dedent("""\
    schema_version: "0.1"
    taxonomy:
      page_types:
        prose:
          default_action: transcribe_text
    # For tabular work, add a schema section (columns, aliases, checks) so
    # structured adapter output lands in normalized/ with graded evidence,
    # and a run.grading section to act on grades. Commented reference:
    # docs/examples/pageledger.yml
    run:
      adapter: text
    """)

# Adapters whose knobs a first-time user should see in a generated config.
ADAPTER_OPTION_TEMPLATES = {
    "pdf_ocr": "  adapter_options:\n    dpi: 300\n    lang: eng\n",
}

BUILTIN_ADAPTERS = ["text", "pdf_text", "pdf_ocr"]


def _config_template(adapter: str) -> str:
    text = MINIMAL_CONFIG
    if adapter != "text":
        text = text.replace("adapter: text", f"adapter: {adapter}")
    return text + ADAPTER_OPTION_TEMPLATES.get(adapter, "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pageledger")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run extraction with run.adapter: text, pdf_text, pdf_ocr, or module.path:Object",
    )
    run_parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Input files or directories; directories are expanded to direct child files",
    )
    run_config = run_parser.add_mutually_exclusive_group(required=True)
    run_config.add_argument("--config", type=Path, help="PageLedger YAML config")
    run_config.add_argument(
        "--adapter",
        choices=BUILTIN_ADAPTERS,
        help="Run a built-in adapter with a generated default config (no YAML needed)",
    )
    run_parser.add_argument("--out", required=True, type=Path, help="New empty run directory")
    run_parser.add_argument(
        "--pages",
        default=None,
        help="Only extract these source pages, e.g. '1-8,81,100-110'; page ids keep the source numbering",
    )
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--json", action="store_true", dest="json_output")
    run_parser.add_argument(
        "--log-level",
        default="INFO",
        help="Minimum run.log event level: DEBUG, INFO, WARNING, or ERROR",
    )
    run_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        help="Directory added to sys.path so a custom run.adapter module can be imported",
    )

    rerun_parser = subparsers.add_parser(
        "rerun",
        help="Re-extract the pages listed in a previous run's rerun-manifest.yml",
    )
    rerun_parser.add_argument(
        "parent_dir",
        type=Path,
        help="Previous run directory containing manifest.json and rerun-manifest.yml",
    )
    rerun_parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="PageLedger YAML config for the rerun (may name a different adapter)",
    )
    rerun_parser.add_argument("--out", required=True, type=Path, help="New empty run directory")
    rerun_parser.add_argument("--dry-run", action="store_true")
    rerun_parser.add_argument("--json", action="store_true", dest="json_output")
    rerun_parser.add_argument(
        "--log-level",
        default="INFO",
        help="Minimum run.log event level: DEBUG, INFO, WARNING, or ERROR",
    )
    rerun_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        help="Directory added to sys.path so a custom run.adapter module can be imported",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Report optional dependencies, external tools, and redacted OCR/VLM env state",
    )
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")

    init_parser = subparsers.add_parser(
        "init-config",
        help="Write a minimal valid pageledger.yml to stdout or a file",
    )
    init_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write config to this file instead of stdout",
    )
    init_parser.add_argument(
        "--adapter",
        choices=["text", "pdf_text", "pdf_ocr"],
        default="text",
        help="Default adapter for the generated config (default: text)",
    )

    inspect_parser = subparsers.add_parser(
        "inspect-run",
        help="Summarize a completed or failed run directory",
    )
    inspect_parser.add_argument(
        "run_dir",
        type=Path,
        help="Path to a run output directory",
    )
    inspect_format = inspect_parser.add_mutually_exclusive_group()
    inspect_format.add_argument("--json", action="store_true", dest="json_output")
    inspect_format.add_argument(
        "--csv",
        action="store_true",
        dest="csv_output",
        help="One CSV row per page: counts, confidence, warnings, cost, timing",
    )

    align_parser = subparsers.add_parser(
        "align",
        help="Re-align an existing run's raw pages against a schema and regrade, without re-extracting",
    )
    align_parser.add_argument(
        "run_dir",
        type=Path,
        help="Path to a run output directory",
    )
    align_parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="Schema YAML (bare schema mapping or a config with a schema: section); defaults to the run's config-snapshot.yml",
    )
    align_parser.add_argument("--json", action="store_true", dest="json_output")

    compare_parser = subparsers.add_parser(
        "compare-runs",
        help="Compare two run directories page-by-page (quality, warnings, cost)",
    )
    compare_parser.add_argument("run_a", type=Path, help="First run directory")
    compare_parser.add_argument("run_b", type=Path, help="Second run directory")
    compare_parser.add_argument("--json", action="store_true", dest="json_output")

    return parser


def _print_error_json(exc: Exception, args: argparse.Namespace) -> None:
    if args.json_output:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "init-config":
        return _cmd_init_config(args)

    if args.command == "inspect-run":
        return _cmd_inspect_run(args)

    if args.command == "compare-runs":
        return _cmd_compare_runs(args)

    if args.command == "align":
        return _cmd_align(args)

    if args.command == "run":
        return _cmd_run(args)

    if args.command == "rerun":
        return _cmd_rerun(args)

    if args.command == "doctor":
        return _cmd_doctor(args)

    return 2


# -- init-config --------------------------------------------------------------

def _cmd_init_config(args: argparse.Namespace) -> int:
    config_text = _config_template(args.adapter)
    if args.out:
        args.out.write_text(config_text, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(config_text)
    return 0


# -- inspect-run --------------------------------------------------------------

def _cmd_inspect_run(args: argparse.Namespace) -> int:
    try:
        if args.csv_output:
            sys.stdout.write(run_pages_csv(args.run_dir))
            return 0
        report = inspect_run(args.run_dir)
    except (ValueError, FileNotFoundError) as exc:
        _print_error_json(exc, args)
        print(f"pageledger: error: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_inspect_report(report)
    return 0


def _print_inspect_report(report: dict) -> None:
    print(f"Run: {report['run_id']}")
    print(f"Status: {report['status']}")
    print(f"Execution mode: {report['execution_mode']}")
    print(f"Pages: {report['pages_total']} total / "
          f"{report['pages_extracted']} extracted / "
          f"{report['pages_skipped']} skipped")
    print(f"Quality warnings: {report['quality_warning_pages']}")
    print(f"Failed pages: {report['failed_page_count']}")
    print(f"Review queue: {report['review_queue_count']}")
    print(f"Records normalized: {report['records_normalized']}")
    distribution = report["grade_distribution"]
    if distribution:
        print("Grades: " + " ".join(f"{g}={distribution[g]}" for g in GRADES))
    print(f"Cost known: {report['cost_known']}")
    if report["estimated_cost_usd"] is not None:
        print(f"Estimated cost USD: {report['estimated_cost_usd']}")
    artifacts = report["artifacts_present"]
    missing = report["artifacts_missing"]
    print(f"Artifacts present: {len(artifacts)}")
    if missing:
        print(f"Artifacts missing: {len(missing)}")
        for name in missing:
            print(f"  - {name}")


# -- align ---------------------------------------------------------------------

def _cmd_align(args: argparse.Namespace) -> int:
    try:
        report = align_run(args.run_dir, schema_path=args.schema)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        _print_error_json(exc, args)
        print(f"pageledger: error: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Aligned run {report['run_id']} against schema '{report['schema_name']}'")
        print(f"Schema source: {report['schema_source']}")
        print(f"Pages aligned: {report['pages_aligned']}")
        print(f"Records normalized: {report['records_normalized']}")
        distribution = report["grade_distribution"]
        print("Grades: " + " ".join(f"{g}={distribution[g]}" for g in GRADES))
        print(f"Review queue: {report['review_queue_count']}")
    return 0


# -- compare-runs --------------------------------------------------------------

def _cmd_compare_runs(args: argparse.Namespace) -> int:
    try:
        report = compare_runs(args.run_a, args.run_b)
    except (ValueError, FileNotFoundError) as exc:
        _print_error_json(exc, args)
        print(f"pageledger: error: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        sys.stdout.write(render_comparison(report))
    return 0


# -- run ---------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    config_path = args.config
    temp_config: str | None = None
    if config_path is None:
        # --adapter mode: synthesize the same config init-config would write.
        # Never read an implicit config file from the working directory.
        fd, temp_config = tempfile.mkstemp(prefix="pageledger-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_config_template(args.adapter))
        config_path = Path(temp_config)
    try:
        result = run(
            inputs=args.inputs,
            config_path=config_path,
            out_dir=args.out,
            dry_run=args.dry_run,
            log_level=args.log_level,
            pages=args.pages,
            adapter_path=args.adapter_path,
        )
    except (RuntimeError, ValueError) as exc:
        _print_error_json(exc, args)
        print(f"pageledger: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_config is not None:
            os.unlink(temp_config)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger run {result['run_id']} wrote {result['out_dir']}")
        summary = result["summary"]
        print(
            "Pages: "
            f"{summary['pages_extracted']} extracted / {summary['pages_total']} total"
        )
        print(f"Raw artifacts: {result['raw_artifact_count']}")
        print(f"Quality warning pages: {result['quality_warning_pages']}")
        print(f"Estimated cost USD: {summary['estimated_cost_usd']}")
        config_warnings = result.get("config_warnings", [])
        if config_warnings:
            print(f"Config warnings ({len(config_warnings)}):")
            for w in config_warnings:
                print(f"  - {w}")
    return 0


# -- rerun ---------------------------------------------------------------------

def _cmd_rerun(args: argparse.Namespace) -> int:
    try:
        result = rerun(
            parent_dir=args.parent_dir,
            config_path=args.config,
            out_dir=args.out,
            dry_run=args.dry_run,
            log_level=args.log_level,
            adapter_path=args.adapter_path,
        )
    except (RuntimeError, ValueError) as exc:
        _print_error_json(exc, args)
        print(f"pageledger: error: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger rerun {result['run_id']} wrote {result['out_dir']}")
        print(f"Parent run: {result['parent_run_id']} (generation {result['rerun_depth']})")
        summary = result["summary"]
        print(
            "Pages: "
            f"{summary['pages_extracted']} extracted / {summary['pages_total']} total"
        )
        print(f"Quality warning pages: {result['quality_warning_pages']}")
        print(f"Estimated cost USD: {summary['estimated_cost_usd']}")
        for warning in result.get("source_integrity_warnings", []):
            print(f"WARNING: {warning}")
    return 0


# -- doctor -------------------------------------------------------------------

def _cmd_doctor(args: argparse.Namespace) -> int:
    report = build_doctor_report()
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger {report['pageledger_version']}")
        print(f"Python: {report['python']['executable']}")
        for name, info in report["optional_packages"].items():
            status = "available" if info["available"] else "missing"
            print(f"Optional package {name}: {status}")
        for name, info in report["external_commands"].items():
            status = info["path"] or "missing"
            version = f" ({info['version']})" if info.get("version") else ""
            print(f"Command {name}: {status}{version}")
            if not info["available"]:
                print(f"  {info['explanation']} {info['install_hint']}")
        ocr_langs = report["ocr_languages"]
        if ocr_langs["available"]:
            print(
                f"OCR languages ({len(ocr_langs['languages'])}): "
                + ", ".join(ocr_langs["languages"])
            )
        else:
            print(f"OCR languages: unavailable ({ocr_langs['explanation']})")
        for name, info in report["cloud_environment"].items():
            status = "set" if info["set"] else "missing"
            print(f"Env {name}: {status} ({info['explanation']})")
    return 0
