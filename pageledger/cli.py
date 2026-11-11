"""Command-line entry point for the PageLedger alpha runner."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import sys
import tempfile
import textwrap
from collections.abc import Callable
from pathlib import Path

import yaml

from ._version import __version__
from .adapters import _LANG_PATTERN, PageLedgerDiagnostic, capture_pypdf_repairs, load_adapter
from .aligner import align_run
from .classifier import classify
from .compare import compare_runs, render_comparison
from .corrections import describe
from .doctor import build_doctor_report
from .export import FORMATS, export_job
from .grading import GRADES, grade_basis_label
from .replay import bundle_run, replay_bundle
from .reports import inspect_run, run_pages_csv
from .runner import rerun, resume, run
from .verify import render_verification, verify_run

MINIMAL_CONFIG = textwrap.dedent("""\
    schema_version: "0.1"
    taxonomy:
      page_types:
        blank:
          default_action: skip
        # review: true extracts these pages and still keeps them for review.
        sparse:
          default_action: transcribe_text
          review: true
        prose:
          default_action: transcribe_text
        table_likely:
          default_action: transcribe_text
          review: true
        unknown:
          default_action: transcribe_text
          review: true
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


# The local OCR engines a document job can start with. pdf_ocr is the default, and
# none (no OCR stage) the default for a plain-text source, which has no page images.
OCR_ENGINES = ("pdf_ocr", "rapidocr", "indra", "none")
DEFAULT_OCR_LANG = "eng"
_SPEC = "https://github.com/peterbussch/pageledger/blob/main/docs/processing-spec.md"

PROCESSING_HEADER = f"""\
# A PageLedger document job, as written by `pageledger init`.
# The job reads the text each page already carries{{then_ocr}}
# Nothing below calls a paid service. Every stage and setting is explained in
# {_SPEC}
schema_version: "0.1"
processing:
  # local_text is read first. It is left out so that the source decides it:
  # pdf_text reads a PDF's own text layer, text reads a plain-text file.
"""

PROCESSING_OPTIONAL = f"""\

  # Optional stages. To use one, remove the leading "# " from its lines.
  #
  # second_ocr reads a page with a second literal engine when the first OCR
  # reading is held for review. {_SPEC}#the-ladder-a-second-ocr-engine-and-a-local-model
  # second_ocr:
  #   adapter: {{second_engine}}
  #
  # local_image sends the page image to a vision model on this machine; it needs
  # limits.max_local_image_pages above 0. {_SPEC}#the-ladder-a-second-ocr-engine-and-a-local-model
  # local_image:
  #   adapter: vision
  #   adapter_options:
  #     base_url: http://127.0.0.1:8080/v1
  #     model: YOUR_LOCAL_MODEL
  #
  # image sends the pages the local stages could not settle to a vision model,
  # which may be a paid service; it needs limits.max_image_pages above 0.
  # {_SPEC}#image-adapter-boundary
  # image:
  #   adapter: vision
  #   adapter_options:
  #     base_url: http://127.0.0.1:8080/v1
  #     model: YOUR_VISION_MODEL
  #
  # second_opinion asks a second model to read the pages the image stage read;
  # its calls count against limits.max_image_pages. {_SPEC}#image-adapter-boundary
  # second_opinion:
  #   adapter: vision
  #   adapter_options:
  #     base_url: http://127.0.0.1:8081/v1
  #     model: YOUR_SECOND_MODEL
  #
  # contest compares a model's reading word by word with the best OCR reading
  # and records every place they differ; it needs an image or local_image stage.
  # {_SPEC}#contest-a-models-reading
  # contest:
  #   rules: ru-print-0.2   # Russian print; latin-print-0.1 for Latin-script print
"""


PROCESSING_NO_OCR = f"""\

  # The second_ocr, local_image, image, second_opinion and contest stages all
  # build on a local_ocr stage. {_SPEC}#policy-and-configuration
"""


def _ocr_options(engine: str, lang: str | None, model_dir: str | None) -> str:
    """The adapter_options lines for the chosen engine, or a usage error."""
    if engine == "pdf_ocr":
        if model_dir is not None:
            raise _UsageError("--model-dir applies to rapidocr and indra, not pdf_ocr")
        lang = lang or DEFAULT_OCR_LANG
        if not _LANG_PATTERN.match(lang):
            raise _UsageError("--lang must be Tesseract language codes such as eng or eng+deu")
        return (
            f"      lang: {lang}   # Tesseract languages; `pageledger doctor` lists those installed\n"
            "      dpi: 300\n"
        )
    if lang is not None:
        reason = (
            "its language is the recognizer model in --model-dir"
            if engine == "rapidocr"
            else "it chooses a recognizer for each line's script"
        )
        raise _UsageError(f"--lang applies to pdf_ocr; {engine} takes none, because {reason}")
    if engine == "rapidocr":
        if model_dir is None:
            raise _UsageError(
                "rapidocr needs --model-dir, a PP-OCRv5 recognizer directory with "
                "inference.onnx and keys.txt (see docs/engine-recipes.md)"
            )
        return f"      rec_model_dir: {json.dumps(_absolute(model_dir))}\n"
    options = "      mode: ocr\n"
    if model_dir is not None:
        options += f"      model_dir: {json.dumps(_absolute(model_dir))}\n"
    return options


def _absolute(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _processing_template(engine: str, lang: str | None, model_dir: str | None) -> str:
    limits = (
        "  limits:\n"
        "    max_image_pages: 0   # pages a paid image stage may read; 0 allows none\n"
        "    # max_attempt_pages: 500   # pause after this many page attempts, all stages together\n"
    )
    if engine == "none":
        if lang is not None or model_dir is not None:
            raise _UsageError(
                "--lang and --model-dir need an OCR engine, and the engine is none "
                "(the default for a text file); name one with --engine"
            )
        return (
            PROCESSING_HEADER.replace("{then_ocr}", ".")
            + "  # No OCR: every page is read only from its own text.\n"
            + "  local_ocr: null\n"
            + limits
            + PROCESSING_NO_OCR
        )
    return (
        PROCESSING_HEADER.replace(
            "{then_ocr}", ", then sends the pages\n# that look wrong to OCR on this machine."
        )
        + f"  local_ocr:\n    adapter: {engine}\n    adapter_options:\n"
        + _ocr_options(engine, lang, model_dir)
        + limits
        + "    # max_local_image_pages: 20   # needed by a local_image stage\n"
        + PROCESSING_OPTIONAL.replace(
            "{second_engine}", "indra" if engine == "pdf_ocr" else "pdf_ocr"
        )
    )


def _engine(args: argparse.Namespace) -> str:
    """The chosen engine, or its documented default."""
    if args.engine is not None:
        return args.engine
    source = getattr(args, "source", None)
    if source is None:
        return "pdf_ocr"
    from .processing_source import source_kind

    try:
        return "none" if source_kind(source) == "text" else "pdf_ocr"
    except (OSError, ValueError) as exc:
        raise _UsageError(str(exc)) from exc


def _processing_command(engine: str, lang: str | None, model_dir: str | None) -> str:
    """The init command that writes the config these options select."""
    parts = ["pageledger", "init", "--engine", engine]
    if engine == "pdf_ocr":
        parts += ["--lang", lang or DEFAULT_OCR_LANG]
    if model_dir is not None:
        parts += ["--model-dir", _absolute(model_dir)]
    return shlex.join([*parts, "--out", "processing.yml"])


class _UsageError(Exception):
    """A combination of options that argparse cannot express; exits 2 like other usage errors."""


TUTORIAL = "https://github.com/peterbussch/pageledger/blob/main/docs/document-first-run.md"

# What `pageledger --help` lists, in order, and what each command's own --help
# opens with: (one-line summary, a sentence or two, an example).
COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Read a document and check the result",
        (
            "process",
            "review",
            "export",
            "verify",
            "inspect",
            "adjudicate",
            "init",
            "fetch",
            "doctor",
        ),
    ),
    (
        "Runs: one tool over many files, for building and testing a pipeline",
        ("run", "resume", "rerun", "compare", "classify", "align", "bundle", "replay"),
    ),
)

COMMAND_HELP: dict[str, tuple[str, str, str]] = {
    "process": (
        "Read a PDF or text file into a job, page by page",
        "Reads one document into a new job folder: each page's own text first, then OCR "
        "on this machine for the pages whose text is missing or looks wrong. Every reading "
        "and every page that needs a person is recorded.",
        "pageledger process book.pdf --out jobs/book",
    ),
    "review": (
        "Look at the pages that need a person and record your decisions",
        "Writes a folder of web pages showing each page's image beside its text (--out), "
        "or a spreadsheet to fill in (--sheet). Then --apply records the decisions you made "
        "in either one.",
        "pageledger review jobs/book --out book-review\n"
        "  pageledger review jobs/book --apply book-review/review.csv --reviewer 'Your Name'",
    ),
    "export": (
        "Write a job's text as plain text, Markdown, JSON lines or TEI",
        "Writes the text chosen for each page, with its review state, after checking that "
        "the job is intact.",
        "pageledger export jobs/book --format md --out book.md",
    ),
    "verify": (
        "Check that a job or run is intact and consistent",
        "Checks that the files in a job or run folder agree with one another and with the "
        "source. It does not judge whether the text is right.",
        "pageledger verify jobs/book",
    ),
    "inspect": (
        "Show a job's report or a run's summary",
        "Prints a job's report, pages that need a person first, or a run's summary.",
        "pageledger inspect jobs/book",
    ),
    "adjudicate": (
        "Settle the words where a model's reading and OCR disagree",
        "Writes one packet per page of disputed words for someone to decide (--packets), "
        "then records their answers (--decisions).",
        "pageledger adjudicate jobs/book --packets packets/",
    ),
    "init": (
        "Write a config file to edit, for process (or for run with --run)",
        "Writes a commented config for process: which OCR engine reads the pages, in which "
        "languages, and the optional stages. With --run, writes a config for run instead.",
        "pageledger init --lang rus+eng --out processing.yml",
    ),
    "fetch": (
        "Download a document's page images from a library's IIIF server",
        "Downloads the page images a IIIF manifest lists into a new folder, with a record "
        "of where each came from.",
        "pageledger fetch https://example.org/iiif/item/manifest.json --out sources/item",
    ),
    "doctor": (
        "Show which OCR engines, tools and languages are installed",
        "Reports the optional packages, external tools and OCR languages this machine has. "
        "It changes nothing.",
        "pageledger doctor",
    ),
    "run": (
        "Extract every page of some files with one tool",
        "Extracts every page of the inputs with one adapter (an extraction tool) into a new "
        "run folder, recording each page's text, warnings and cost.",
        "pageledger run scans/ --adapter pdf_ocr --out runs/first",
    ),
    "resume": (
        "Continue an interrupted run or job where it stopped",
        "Continues a job, or a run started with --resumable, in its own folder, without "
        "redoing the pages already read.",
        "pageledger resume jobs/book",
    ),
    "rerun": (
        "Extract a run's flagged pages again, usually with another tool",
        "Extracts again only the pages a run flagged, into a new run that records which "
        "run it continues.",
        "pageledger rerun runs/first --config stronger.yml --out runs/second",
    ),
    "compare": (
        "Compare two runs page by page",
        "Compares the pages two runs share: text length, warnings, grades and cost.",
        "pageledger compare runs/first runs/second",
    ),
    "classify": (
        "Sort pages by kind and write a route map for run",
        "Takes a quick look at every page and writes a route map (which pages to extract, "
        "skip or review) that you can check before run uses it.",
        "pageledger classify scans/ --out route-map.yml",
    ),
    "align": (
        "Match a run's table pages to a schema again, without extracting",
        "Fits a run's table records to a schema again and regrades them, without "
        "extracting anything.",
        "pageledger align runs/first --schema table.yml",
    ),
    "bundle": (
        "Package a run with its sources so it can be replayed",
        "Copies a verified run and its sources into a new folder that replay can run again.",
        "pageledger bundle runs/first --out bundles/first",
    ),
    "replay": (
        "Run a bundle again on this machine and compare the results",
        "Runs a bundle's extraction again here and records how the new pages compare with "
        "the bundled ones.",
        "pageledger replay bundles/first --out runs/replayed",
    ),
}


def _overview() -> str:
    width = max(len(name) for name in COMMAND_HELP) + 2
    lines = ["Records how each page of a document was read, so the text can be checked and cited."]
    for heading, names in COMMAND_GROUPS:
        lines += ["", f"{heading}:"]
        lines += [f"  {name.ljust(width)}{COMMAND_HELP[name][0]}" for name in names]
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    def _check_value(self, action: argparse.Action, value: object) -> None:
        # Name only the listed commands, not every earlier name that still works.
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            close = difflib.get_close_matches(str(value), list(COMMAND_HELP), n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            raise argparse.ArgumentError(
                action, f"unknown command {value!r}{hint} (pageledger --help lists them)"
            )
        super()._check_value(action, value)


def _command(
    subparsers: argparse._SubParsersAction, name: str, note: str = "", **kwargs
) -> argparse.ArgumentParser:
    """A listed command, whose --help opens with its description and an example."""
    _, about, example = COMMAND_HELP[name]
    return subparsers.add_parser(
        name,
        description=textwrap.fill(about, 79),
        epilog=f"example:\n  {example}" + (f"\n\n{textwrap.fill(note, 79)}" if note else ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        **kwargs,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="pageledger",
        usage="%(prog)s [-h] [--version] COMMAND ...",
        description=_overview(),
        epilog=(
            "Run `pageledger COMMAND --help` for a command's options.\n"
            f"New to PageLedger? Start with the first-run tutorial: {TUTORIAL}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Earlier command names (inspect-job, verify-run, ...) are parsers with no
    # help=, so they still work but are never listed.
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        help=argparse.SUPPRESS,
        prog="pageledger",
        parser_class=argparse.ArgumentParser,
    )

    process_parser = _command(subparsers, "process")
    process_parser.add_argument(
        "source",
        type=Path,
        metavar="SOURCE",
        help="The document: a PDF, a text file, an image or a folder of page images",
    )
    process_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help="A config file from `pageledger init`; without it, the job uses the config "
        "init writes for --engine, --lang and --model-dir",
    )
    process_parser.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="New folder for the job"
    )
    _add_engine_options(process_parser, "without --config")
    process_parser.add_argument(
        "--pages",
        default=None,
        metavar="RANGE",
        help="Only these pages, e.g. '1-10,15'; pages keep their numbers",
    )
    process_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        metavar="DIR",
        help="Folder to import custom adapters from (trusted code)",
    )
    process_parser.add_argument(
        "--review",
        type=Path,
        default=None,
        metavar="FILE",
        help="Review decisions to apply before reading, such as pages known to be blank",
    )
    process_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )
    for command in ("inspect-job", "verify-job", "review-job", "review-sheet", "adjudicate"):
        if command == "adjudicate":
            job_parser = _command(subparsers, command)
        else:
            job_parser = subparsers.add_parser(command)
        job_parser.add_argument("job_dir", type=Path)
        job_parser.add_argument("--json", action="store_true", dest="json_output")
        if command == "review-job":
            job_parser.add_argument("--review", type=Path, required=True)
            job_parser.add_argument(
                "--reviewer",
                help="Name recorded with review-sheet decisions (or PAGELEDGER_REVIEWER)",
            )
            job_parser.add_argument(
                "--dry-run", action="store_true", help="Check every decision without recording any"
            )
        elif command == "review-sheet":
            job_parser.add_argument(
                "--out", type=Path, required=True, help="CSV to write; its binding goes beside it"
            )
        elif command == "adjudicate":
            mode = job_parser.add_mutually_exclusive_group(required=True)
            mode.add_argument(
                "--packets",
                type=Path,
                help="New folder: one packet per page with disputed words still open",
            )
            mode.add_argument(
                "--decisions", type=Path, help="Directory of <page_id>.decisions.json answers"
            )
            job_parser.add_argument(
                "--reviewer",
                default="",
                help="With --decisions: who decided, e.g. person:NAME or agent:NAME",
            )
            job_parser.add_argument(
                "--term",
                action="append",
                default=[],
                help="With --packets: only disputed words near this word or phrase (repeatable)",
            )
            job_parser.add_argument(
                "--dry-run", action="store_true", help="Check every decision without recording any"
            )

    review_parser = _command(subparsers, "review")
    review_parser.add_argument("job_dir", type=Path, metavar="JOB", help="A job folder")
    review_mode = review_parser.add_mutually_exclusive_group(required=True)
    review_mode.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="New folder for the page view: each page's image beside its text, and a blank sheet",
    )
    review_mode.add_argument(
        "--sheet",
        type=Path,
        metavar="FILE",
        help="Write only the review sheet, a CSV for a spreadsheet, here",
    )
    review_mode.add_argument(
        "--apply",
        type=Path,
        metavar="FILE",
        help="Record the decisions in this filled-in review sheet, the review.json the page "
        "view downloads, or a JSON receipt",
    )
    review_parser.add_argument(
        "--images",
        choices=("all", "held", "none"),
        default=None,
        help="With --out: images of every page (all, the default), only the pages that need "
        "a person (held), or none (needs no Poppler)",
    )
    review_parser.add_argument(
        "--reviewer",
        metavar="NAME",
        help="With --apply: your name, recorded with each decision from a sheet "
        "(or set PAGELEDGER_REVIEWER)",
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --apply: check every decision without recording any",
    )
    review_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    export_parser = _command(subparsers, "export")
    export_parser.add_argument("job_dir", type=Path, metavar="JOB", help="A job folder")
    export_parser.add_argument("--format", choices=FORMATS, required=True)
    export_parser.add_argument(
        "--out", type=Path, required=True, metavar="FILE", help="File to write"
    )
    export_parser.add_argument(
        "--reviewed-only", action="store_true", help="Only pages with a human review receipt"
    )

    run_parser = _command(subparsers, "run")
    run_parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help=(
            "Input files or directories; a directory expands to its direct child files, "
            "or is one document when it holds JPEG or PNG page images"
        ),
    )
    run_config = run_parser.add_mutually_exclusive_group(required=True)
    run_config.add_argument("--config", type=Path, help="PageLedger YAML config")
    run_config.add_argument(
        "--adapter",
        choices=BUILTIN_ADAPTERS,
        help="Run a built-in adapter with a generated default config (no YAML needed)",
    )
    run_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="New empty run directory"
    )
    run_parser.add_argument(
        "--pages",
        default=None,
        help="Only extract these source pages, e.g. '1-8,81,100-110'; page ids keep the source numbering",
    )
    run_parser.add_argument(
        "--routes",
        type=Path,
        default=None,
        help="Extract pages as this route map (from classify, checked by you) says; needs --config",
    )
    run_parser.add_argument(
        "--resumable",
        action="store_true",
        help="Keep each page as it is read, so an interrupted run can continue with resume",
    )
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )
    run_parser.add_argument(
        "--log-level",
        default="INFO",
        help="Minimum run.log event level: DEBUG, INFO, WARNING, or ERROR",
    )
    run_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        help="Directory added to sys.path so custom adapter modules can be imported",
    )

    resume_parser = _command(
        subparsers,
        "resume",
        note="A source file that has moved is found by SHA-256 through the source index: "
        "PAGELEDGER_SOURCE_INDEX (TSV files of sha256<TAB>path), or "
        "~/.config/pageledger/source-index.tsv when unset. Records keep the recorded path.",
    )
    resume_parser.add_argument(
        "run_dir", type=Path, metavar="DIR", help="A job folder, or a run started with --resumable"
    )
    resume_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        metavar="DIR",
        help="Folder to import custom adapters from (trusted code)",
    )
    resume_parser.add_argument(
        "--raise-limit",
        action="append",
        default=[],
        metavar="LIMIT=VALUE",
        help="Raise a processing limit of a document job paused by it, e.g. max_attempt_pages=50",
    )
    resume_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    rerun_parser = _command(subparsers, "rerun")
    rerun_parser.add_argument(
        "parent_dir",
        type=Path,
        help="The earlier run, whose rerun-manifest.yml lists the flagged pages",
    )
    rerun_parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="PageLedger YAML config for the rerun (plain adapter or adapter chain)",
    )
    rerun_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="New empty run directory"
    )
    rerun_parser.add_argument("--dry-run", action="store_true")
    rerun_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )
    rerun_parser.add_argument(
        "--log-level",
        default="INFO",
        help="Minimum run.log event level: DEBUG, INFO, WARNING, or ERROR",
    )
    rerun_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        help="Directory added to sys.path so custom adapter modules can be imported",
    )

    classify_parser = _command(subparsers, "classify")
    classify_parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Input files or directories; omit when using --from-run",
    )
    classify_parser.add_argument("--config", type=Path, help="Optional PageLedger YAML config")
    classify_parser.add_argument(
        "--out", required=True, type=Path, help="Route map (YAML) to write"
    )
    classify_parser.add_argument(
        "--from-run",
        type=Path,
        default=None,
        help="Classify again from the pages a finished run kept, without reading the files again",
    )
    classify_parser.add_argument(
        "--adapter",
        default=None,
        help="Probe adapter name or module.path:Object (overrides classify.adapter)",
    )
    classify_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        help="Directory added to sys.path for custom probe adapters or classifier hooks",
    )
    classify_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    doctor_parser = _command(subparsers, "doctor")
    doctor_parser.add_argument(
        "--config",
        type=Path,
        help="Also report the environment variables this config's adapters name by env_key",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    init_parser = subparsers.add_parser("init-config")
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
    init_parser.add_argument(
        "--processing",
        action="store_true",
        help="Write a commented processing.yml for pageledger process instead",
    )
    _add_engine_options(init_parser, "with --processing")

    inspect_parser = subparsers.add_parser("inspect-run")
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

    align_parser = _command(subparsers, "align")
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
    align_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )
    align_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview normalized records, grades, and audit changes without writing",
    )

    compare_parser = subparsers.add_parser("compare-runs")
    compare_parser.add_argument("run_a", type=Path, help="First run directory")
    compare_parser.add_argument("run_b", type=Path, help="Second run directory")
    compare_parser.add_argument("--json", action="store_true", dest="json_output")

    verify_parser = subparsers.add_parser("verify-run")
    verify_parser.add_argument("run_dir", type=Path, help="Path to a run output directory")
    verify_parser.add_argument("--json", action="store_true", dest="json_output")

    bundle_parser = _command(subparsers, "bundle", allow_abbrev=False)
    bundle_parser.add_argument("run_dir", type=Path, metavar="RUN", help="A verified run folder")
    bundle_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="New folder for the bundle"
    )
    bundle_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    replay_parser = _command(subparsers, "replay", allow_abbrev=False)
    replay_parser.add_argument("bundle_dir", type=Path, metavar="BUNDLE", help="A bundle folder")
    replay_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="New folder for the replayed run"
    )
    replay_parser.add_argument(
        "--adapter-path",
        type=Path,
        default=None,
        metavar="DIR",
        help="Folder to import the bundle's adapter from (trusted code)",
    )
    replay_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    iiif_parser = _command(subparsers, "fetch", allow_abbrev=False)
    iiif_parser.add_argument(
        "url", metavar="URL", help="A IIIF manifest, an info.json, or an image service URL"
    )
    iiif_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="New folder for the page images"
    )
    iiif_parser.add_argument(
        "--pages", default=None, help="Canvases by position in the manifest, e.g. '1-8,81'"
    )
    iiif_parser.add_argument(
        "--delay", type=float, default=1.0, help="Seconds between requests (default 1.0)"
    )
    iiif_parser.add_argument(
        "--max-image-bytes",
        type=int,
        default=256 * 1024 * 1024,
        help="Refuse any image larger than this (default 268435456)",
    )
    iiif_parser.add_argument(
        "--resume", action="store_true", help="Fetch only what an earlier fetch into --out missed"
    )
    iiif_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the result as JSON"
    )

    verify_parser = _command(subparsers, "verify")
    verify_parser.add_argument("dir", type=Path, metavar="DIR", help="A job folder or a run folder")
    verify_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the full report as JSON"
    )

    inspect_parser = _command(subparsers, "inspect")
    inspect_parser.add_argument(
        "dir", type=Path, metavar="DIR", help="A job folder or a run folder"
    )
    inspect_format = inspect_parser.add_mutually_exclusive_group()
    inspect_format.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print document.json for a job, or the summary for a run, as JSON",
    )
    inspect_format.add_argument(
        "--csv",
        action="store_true",
        dest="csv_output",
        help="For a run: one CSV row per page (counts, confidence, warnings, cost, timing)",
    )

    compare_parser = _command(subparsers, "compare")
    compare_parser.add_argument("run_a", type=Path, help="First run folder")
    compare_parser.add_argument("run_b", type=Path, help="Second run folder")
    compare_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print the comparison as JSON"
    )

    init_parser = _command(subparsers, "init")
    init_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write the config to this file, not the screen",
    )
    init_parser.add_argument(
        "--run", action="store_true", help="Write a config for pageledger run instead"
    )
    init_parser.add_argument(
        "--adapter",
        choices=BUILTIN_ADAPTERS,
        default=None,
        help="With --run: the extraction tool the config names (default: text)",
    )
    _add_engine_options(init_parser, "without --run")

    return parser


def _add_engine_options(parser: argparse.ArgumentParser, when: str) -> None:
    parser.add_argument(
        "--engine",
        choices=OCR_ENGINES,
        default=None,
        help=f"Local OCR engine or none, {when} (default: pdf_ocr; process uses none for a text file)",
    )
    parser.add_argument(
        "--lang",
        default=None,
        help=f"Tesseract languages for pdf_ocr, {when} (default: {DEFAULT_OCR_LANG})",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help=f"Recognizer directory for rapidocr (required) or model directory for indra, {when}",
    )


def _engine_options_given(args: argparse.Namespace) -> bool:
    return any(value is not None for value in (args.engine, args.lang, args.model_dir))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _check_usage(args)
    except _UsageError as exc:
        parser.error(str(exc))
    handlers = {
        "process": _cmd_job,
        "inspect-job": _cmd_job,
        "verify-job": _cmd_job,
        "review-job": _cmd_job,
        "review-sheet": _cmd_job,
        "adjudicate": _cmd_job,
        "review": _cmd_review,
        "export": _cmd_export,
        "verify": _cmd_verify,
        "inspect": _cmd_inspect,
        "init": _cmd_init,
        "init-config": _cmd_init_config,
        "inspect-run": _cmd_inspect_run,
        "compare": _cmd_compare_runs,
        "compare-runs": _cmd_compare_runs,
        "verify-run": _cmd_verify_run,
        "bundle": _cmd_bundle,
        "replay": _cmd_replay,
        "align": _cmd_align,
        "run": _cmd_run,
        "classify": _cmd_classify,
        "resume": _cmd_resume,
        "rerun": _cmd_rerun,
        "doctor": _cmd_doctor,
        "fetch": _cmd_fetch_iiif,
    }
    try:
        return handlers[args.command](args)
    except (RuntimeError, ValueError, OSError) as exc:
        if getattr(args, "json_output", False):
            result = {"status": "error", "error": str(exc)}
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code:
                result["code"] = code
            print(json.dumps(result, ensure_ascii=False))
        print(f"pageledger: error: {exc}", file=sys.stderr)
        return 1


# -- init ---------------------------------------------------------------------


def _check_usage(args: argparse.Namespace) -> None:
    if args.command == "init":
        if args.run:
            if _engine_options_given(args):
                raise _UsageError(
                    "--engine, --lang and --model-dir write a config for process; "
                    "they do not go with --run"
                )
        elif args.adapter is not None:
            raise _UsageError("--adapter goes with --run, which writes a config for pageledger run")
        else:
            _processing_template(_engine(args), args.lang, args.model_dir)
    elif args.command == "review":
        if args.images is not None and args.out is None:
            raise _UsageError("--images goes with --out, which writes the page view")
        if (args.reviewer is not None or args.dry_run) and args.apply is None:
            raise _UsageError("--reviewer and --dry-run go with --apply, which records decisions")
    elif args.command == "init-config":
        if args.processing:
            if args.adapter != "text":
                raise _UsageError("--adapter writes a run config; it does not go with --processing")
            _processing_template(_engine(args), args.lang, args.model_dir)
        elif _engine_options_given(args):
            raise _UsageError("--engine, --lang and --model-dir go with --processing")
    elif args.command == "process":
        if args.config is None:
            _processing_template(_engine(args), args.lang, args.model_dir)
        elif _engine_options_given(args):
            raise _UsageError(
                "--engine, --lang and --model-dir choose the config when there is no --config; "
                "set them in the config file instead"
            )


def _cmd_init(args: argparse.Namespace) -> int:
    args.processing = not args.run
    args.adapter = args.adapter or "text"
    return _cmd_init_config(args)


def _cmd_init_config(args: argparse.Namespace) -> int:
    if args.processing:
        config_text = _processing_template(_engine(args), args.lang, args.model_dir)
    else:
        config_text = _config_template(args.adapter)
    if args.out:
        args.out.write_text(config_text, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(config_text)
    return 0


# -- verify and inspect: a job or a run ---------------------------------------


def _directory_kind(path: Path) -> str:
    """ "job" or "run", from the record that marks each; anything else is refused."""
    looked_for = (
        "looked for job.json (a document job, from process) or manifest.json or "
        "checkpoint.json (a run, from run or a job's stage under attempts/)"
    )
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    if not path.is_dir():
        raise ValueError(f"{path} is a file; give the folder of a job or a run ({looked_for})")
    job = (path / "job.json").is_file()
    run = (path / "manifest.json").is_file() or (path / "checkpoint.json").is_file()
    if job and run:
        raise ValueError(f"{path} has the records of both a job and a run; {looked_for}")
    if not job and not run:
        raise ValueError(f"{path} is not a job or a run folder: {looked_for}")
    return "job" if job else "run"


def _cmd_verify(args: argparse.Namespace) -> int:
    if _directory_kind(args.dir) == "job":
        args.command, args.job_dir = "verify-job", args.dir
        return _cmd_job(args)
    args.run_dir = args.dir
    return _cmd_verify_run(args)


def _cmd_inspect(args: argparse.Namespace) -> int:
    if _directory_kind(args.dir) == "job":
        if args.csv_output:
            raise ValueError("--csv lists a run's pages; for a job, use review --sheet")
        args.command, args.job_dir = "inspect-job", args.dir
        return _cmd_job(args)
    args.run_dir = args.dir
    return _cmd_inspect_run(args)


# -- inspect-run --------------------------------------------------------------


def _cmd_inspect_run(args: argparse.Namespace) -> int:
    if args.csv_output:
        sys.stdout.write(run_pages_csv(args.run_dir))
        return 0
    report = inspect_run(args.run_dir)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        cost = _read_cost_report(args.run_dir)
        _print_inspect_report(
            report,
            cost_basis=cost.get("cost_basis") if cost is not None else None,
        )
    return 0


def _print_inspect_report(report: dict, *, cost_basis: str | None = None) -> None:
    print(f"Run: {report['run_id']}")
    print(f"Status: {report['status']}")
    print(f"Execution mode: {report['execution_mode']}")
    print(
        f"Pages: {report['pages_total']} total / "
        f"{report['pages_extracted']} extracted / "
        f"{report['pages_skipped']} skipped / "
        f"{report['pages_routed_review']} routed to review"
    )
    print(f"Quality warnings: {report['quality_warning_pages']}")
    print(f"Failed pages: {report['failed_page_count']}")
    if report["pages_not_attempted"]:
        print(f"Pages not attempted: {report['pages_not_attempted']}")
    print(f"Review queue: {report['review_queue_count']}")
    print(f"Records normalized: {report['records_normalized']}")
    for basis, distribution in report["grade_distribution_by_basis"].items():
        print(
            f"Grades ({grade_basis_label(basis)}): "
            + " ".join(f"{grade}={distribution[grade]}" for grade in GRADES)
        )
    _print_human_cost(
        cost_known=report["cost_known"],
        cost_usd=report["estimated_cost_usd"],
        dry_run=report["execution_mode"] == "dry_run",
        cost_basis=cost_basis,
    )
    artifacts = report["artifacts_present"]
    missing = report["artifacts_missing"]
    print(f"Artifacts present: {len(artifacts)}")
    if missing:
        print(f"Artifacts missing: {len(missing)}")
        for name in missing:
            print(f"  - {name}")


# -- align ---------------------------------------------------------------------


def _cmd_align(args: argparse.Namespace) -> int:
    report = align_run(args.run_dir, schema_path=args.schema, dry_run=args.dry_run)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        verb = "Aligned" if report["applied"] else "Previewed"
        print(f"{verb} run {report['run_id']} against schema '{report['schema_name']}'")
        print(f"Schema source: {report['schema_source']}")
        print(f"Pages aligned: {report['pages_aligned']}")
        print(f"Records normalized: {report['records_normalized']}")
        for basis, distribution in report["grade_distribution_by_basis"].items():
            print(
                f"Grades ({grade_basis_label(basis)}): "
                + " ".join(f"{grade}={distribution[grade]}" for grade in GRADES)
            )
        print(f"Review queue: {report['review_queue_count']}")
    return 0


# -- verify-run ---------------------------------------------------------------


def _cmd_verify_run(args: argparse.Namespace) -> int:
    report = verify_run(args.run_dir)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        sys.stdout.write(render_verification(report))
    return 0 if report["status"] == "pass" else 1


# -- bundle ------------------------------------------------------------------


def _cmd_bundle(args: argparse.Namespace) -> int:
    result = bundle_run(args.run_dir, args.out)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Bundle created: {result['bundle_dir']}")
    return 0


# -- replay ------------------------------------------------------------------


def _cmd_replay(args: argparse.Namespace) -> int:
    result = replay_bundle(args.bundle_dir, args.out, adapter_path=args.adapter_path)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Verified replay outcome: {result['outcome']}")
        raw = result["raw"]
        print(
            "Raw comparison: "
            f"{raw['equal']} equal / {raw['different']} different / "
            f"{raw['missing']} missing"
        )
    return 0 if result.get("outcome") in {"exact", "evidence_compared"} else 1


# -- fetch --------------------------------------------------------------------


def _cmd_fetch_iiif(args: argparse.Namespace) -> int:
    from .iiif import fetch_iiif

    def progress(message: str) -> None:
        print(message, file=sys.stderr)

    summary = fetch_iiif(
        args.url,
        args.out,
        pages=args.pages,
        delay=args.delay,
        max_image_bytes=args.max_image_bytes,
        resume=args.resume,
        progress=None if args.json_output else progress,
    )
    if args.json_output:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{summary['status'].capitalize()}: {summary['fetched']} of "
            f"{summary['fetched'] + len(summary['missing'])} selected canvases in {summary['out']} "
            f"({summary['fetched_now']} fetched now)"
        )
        for warning in summary["warnings"]:
            print(f"warning: {warning['code']}: {warning['message']}")
        for item in summary["missing"]:
            print(f"missing: canvas {item['canvas_index']}: {item['error'] or 'not fetched yet'}")
    return 0 if summary["status"] == "complete" else 1


# -- compare ------------------------------------------------------------------


def _cmd_compare_runs(args: argparse.Namespace) -> int:
    report = compare_runs(args.run_a, args.run_b)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        sys.stdout.write(render_comparison(report))
    return 0


# -- run ---------------------------------------------------------------------


def _read_cost_report(out_dir: Path) -> dict | None:
    try:
        report = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return report if isinstance(report, dict) else None


def _print_run_cost(out_dir: Path, *, dry_run: bool) -> None:
    cost = _read_cost_report(out_dir)
    if cost is None:
        print("Cost USD: unavailable (cost.json could not be read)")
        return
    _print_human_cost(
        cost_known=cost.get("cost_known", False),
        cost_usd=cost.get("cost_usd"),
        dry_run=dry_run,
        cost_basis=cost.get("cost_basis"),
    )


def _print_human_cost(
    *,
    cost_known: bool,
    cost_usd: int | float | None,
    dry_run: bool,
    cost_basis: str | None,
) -> None:
    if dry_run and cost_known and cost_usd == 0.0:
        print("Cost USD: 0.0 (dry run; no extraction performed)")
        return
    if cost_usd is None:
        print("Cost USD: unknown")
        return

    label = "Cost USD"
    amount = str(cost_usd)
    qualifications = []
    if not cost_known:
        amount = f"at least {cost_usd}"
        qualifications.append("partial; some page costs unknown")
    if cost_basis == "configured_rate":
        label = "Estimated cost USD"
        qualifications.append("configured rate; not a provider charge")
    elif cost_basis == "adapter_reported":
        label = "Adapter-reported cost USD"
    elif cost_basis == "mixed":
        label = "Cost evidence USD"
        qualifications.append("mixed adapter-reported and configured-rate evidence")
    suffix = f" ({'; '.join(qualifications)})" if qualifications else ""
    print(f"{label}: {amount}{suffix}")


def _cmd_run(args: argparse.Namespace) -> int:
    if args.routes is not None and args.config is None:
        raise ValueError("--routes requires --config; it cannot be used with --adapter")
    config_path = args.config
    temp_config: str | None = None
    if config_path is None:
        # --adapter mode: synthesize the same config init --run would write.
        # Never read an implicit config file from the working directory.
        fd, temp_config = tempfile.mkstemp(prefix="pageledger-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_config_template(args.adapter))
        config_path = Path(temp_config)
    cost_existed = (args.out / "cost.json").exists()
    try:
        with capture_pypdf_repairs():
            result = run(
                inputs=args.inputs,
                config_path=config_path,
                out_dir=args.out,
                dry_run=args.dry_run,
                log_level=args.log_level,
                pages=args.pages,
                adapter_path=args.adapter_path,
                routes_path=args.routes,
                resumable=args.resumable,
            )
    except (RuntimeError, ValueError, OSError):
        if not args.json_output and not cost_existed:
            _print_persisted_budget_alerts(args.out)
        raise
    finally:
        if temp_config is not None:
            os.unlink(temp_config)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger run {result['run_id']} wrote {result['out_dir']}")
        summary = result["summary"]
        print(f"Pages: {summary['pages_extracted']} extracted / {summary['pages_total']} total")
        skipped = result.get("skipped_inputs", [])
        if skipped:
            print(f"Skipped hidden files ({len(skipped)}): {', '.join(skipped)}")
        print(f"Raw artifacts: {result['raw_artifact_count']}")
        print(f"Quality warning pages: {result['quality_warning_pages']}")
        _print_run_cost(args.out, dry_run=result["dry_run"])
        escalation = result.get("escalation")
        if escalation is not None:
            step = escalation["step"]
            adapter = escalation["adapter_order"][step]
            print(f"Escalation step: {step} ({adapter})")
        _print_budget_alerts(result)
        config_warnings = result.get("config_warnings", [])
        if config_warnings:
            print(f"Config warnings ({len(config_warnings)}):")
            for w in config_warnings:
                print(f"  - {w}")
    summary = result["summary"]
    execution_failed = bool(summary.get("pages_failed") or summary.get("pages_not_attempted"))
    return 1 if result["status"] == "partial" and execution_failed else 0


# -- classify -----------------------------------------------------------------


def _cmd_classify(args: argparse.Namespace) -> int:
    result = classify(
        inputs=args.inputs,
        config_path=args.config,
        out_path=args.out,
        from_run=args.from_run,
        probe_adapter=args.adapter,
        adapter_path=args.adapter_path,
    )
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger classification {result['run_id']} wrote {result['out_path']}")
        print(f"Evidence: {result['evidence_path']}")
        print(f"Pages: {result['pages']} across {result['documents']} documents")
        for warning in result.get("warnings", []):
            print(f"WARNING: {warning}")
    return 0


def _parse_limit_raises(items: list[str]) -> dict[str, int | float]:
    raised: dict[str, int | float] = {}
    for item in items:
        name, _, text = item.partition("=")
        try:
            value: int | float = int(text) if text.strip().isdigit() else float(text)
        except ValueError:
            raise ValueError(f"--raise-limit expects LIMIT=NUMBER, got {item!r}") from None
        raised[name.strip()] = value
    return raised


def _cmd_resume(args: argparse.Namespace) -> int:
    raised = _parse_limit_raises(args.raise_limit)
    if (args.run_dir / "job.json").exists():
        from .processing import resume_job

        result = resume_job(args.run_dir, adapter_path=args.adapter_path, raise_limits=raised)
    elif raised:
        raise ValueError("--raise-limit applies to document jobs made by pageledger process")
    else:
        result = resume(args.run_dir, adapter_path=args.adapter_path)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger {result.get('job_id', result.get('run_id'))} wrote {result['out_dir']}")
        print(f"Status: {result['status']}")
        if result.get("next_action"):
            print(result["next_action"])
    return 1 if result["status"] in {"failed", "halted"} else 0


def _cmd_job(args: argparse.Namespace) -> int:
    from .processing import (
        adjudicate_job,
        adjudication_packets,
        create_review_sheet,
        process,
        review_job,
        verify_job,
    )

    if args.command == "process":
        with capture_pypdf_repairs():
            if args.config is None:
                result = _process_without_config(args, process)
            else:
                result = process(
                    source=args.source,
                    config_path=args.config,
                    out_dir=args.out,
                    pages=args.pages,
                    adapter_path=args.adapter_path,
                    review_path=args.review,
                )
    elif args.command == "review-job":
        result = review_job(args.job_dir, args.review, reviewer=args.reviewer, dry_run=args.dry_run)
    elif args.command == "review-sheet":
        result = create_review_sheet(args.job_dir, args.out)
    elif args.command == "adjudicate" and args.packets:
        if args.reviewer or args.dry_run:
            raise ValueError("--reviewer and --dry-run go with --decisions, not --packets")
        result = adjudication_packets(args.job_dir, args.packets, args.term)
    elif args.command == "adjudicate":
        if args.term:
            raise ValueError("--term goes with --packets: it chooses which spans to send")
        result = adjudicate_job(
            args.job_dir, args.decisions, reviewer=args.reviewer, dry_run=args.dry_run
        )
    elif args.command == "verify-job":
        result = verify_job(args.job_dir)
    else:
        if not args.json_output:
            print((args.job_dir / "report.md").read_text(encoding="utf-8"), end="")
            return 0
        result = json.loads((args.job_dir / "document.json").read_text(encoding="utf-8"))
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif args.command == "process" and result.get("report"):
        _print_job_summary(args.out)
        for warning in result.get("config_warnings", []):
            print(f"Config warning: {warning}")
    else:
        reason = f" ({result['reason']})" if result.get("reason") else ""
        print(f"Status: {result['status']}{reason}")
        if "decisions" in result:
            counts = sorted(result["decisions"].items())
            print("Decisions: " + (", ".join(f"{name} {n}" for name, n in counts) or "none"))
        for item in result.get("corrections", []):
            page, start = item["page_number"], item["from"]
            print(f"Correction of page {page}, from {start}: {describe(item['words'])}")
            if item["large"]:
                print(
                    f"Warning: the correction of page {page} changes more than half the words "
                    f"of {start}. Check that it is this page's text."
                )
        if "packets" in result:
            print(f"Packets: {result['packets']} pages in {result['out']}")
            print(f"Open spans: {result['open_spans']}; in packets: {result['spans']}")
            if "terms" in result:
                print(
                    f"Left out (no term nearby, still open): {result['left_out']}; "
                    f"terms: {', '.join(result['terms'])}"
                )
        if "decisions_high" in result:
            print(f"Pages answered: {result['pages']}; spans settled: {result['decisions_high']}")
        if "binding" in result:
            print(f"Review sheet: {result['out']} ({result['rows']} pages)")
            print(f"Binding: {result['binding']}")
        if result.get("report"):
            print(f"Report: {result['report']}")
        if result.get("next_action"):
            print(result["next_action"])
        for warning in result.get("config_warnings", []):
            print(f"Config warning: {warning}")
        for issue in result.get("warnings", []):
            print(f"Warning: [{issue['code']}] {issue['message']}")
        if result.get("error"):
            print(result["error"], file=sys.stderr)
    return 1 if result.get("status") in {"halted", "failed", "fail"} else 0


def _process_without_config(args: argparse.Namespace, process: Callable[..., dict]) -> dict:
    """Run a job with the config init writes for these options.

    Nothing is read from the working directory or guessed from the machine; the
    job records this config exactly as it would record the same file.
    """
    _check_process_paths(args.source, args.out)
    engine = _engine(args)
    config_text = _processing_template(engine, args.lang, args.model_dir)
    if engine == "none":
        described = "the text each page carries, no OCR"
    else:
        options = yaml.safe_load(config_text)["processing"]["local_ocr"]["adapter_options"]
        _check_engine(engine, options, args.source)
        described = "the text layer, then " + (
            f"{engine} (lang {options['lang']})" if engine == "pdf_ocr" else engine
        )
        described += " for pages that need OCR"
    print(
        f"pageledger: no --config given, so this job uses the default document config: "
        f"{described}, and no paid stages.",
        file=sys.stderr,
    )
    print(
        f"To change it, write it out with `{_processing_command(engine, args.lang, args.model_dir)}`"
        " and pass --config processing.yml.",
        file=sys.stderr,
    )
    fd, temp_config = tempfile.mkstemp(prefix="pageledger-", suffix=".yml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(config_text)
        return process(
            source=args.source,
            config_path=Path(temp_config),
            out_dir=args.out,
            pages=args.pages,
            adapter_path=args.adapter_path,
            review_path=args.review,
        )
    finally:
        os.unlink(temp_config)


def _check_engine(engine: str, options: dict, source: Path) -> None:
    """Refuse before the job exists when the engine or its tools are missing."""
    try:
        adapter = load_adapter(engine, options)
        preflight = getattr(adapter, "preflight", None)
        if callable(preflight):
            preflight([source])
    except PageLedgerDiagnostic:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc).rstrip(".")
        if "install" in message and "doctor" not in message:
            message += ". Run 'pageledger doctor' to see which tools are installed"
        raise ValueError(f"{engine}: {message}.") from exc


def _check_process_paths(source: Path, out: Path) -> None:
    source = source.expanduser()
    if not source.exists():
        raise ValueError(f"Source file was not found: {source}")
    if out.expanduser().exists():
        raise ValueError(
            f"A job or another file is already at {out}. Use `pageledger inspect {out}`, "
            f"`pageledger resume {out}`, or choose another --out directory."
        )


def _print_job_summary(out: Path) -> None:
    """The opening of the job's report, with the job's own folder in its commands."""
    from .document_report import summary_lines

    document = json.loads((out / "document.json").read_text(encoding="utf-8"))
    job = shlex.quote(str(out))
    print("\n".join(summary_lines(document, job)).replace("`", ""))
    print(f"\nThe full report: pageledger inspect {job}")


def _cmd_review(args: argparse.Namespace) -> int:
    if args.apply is not None:
        args.command, args.review = "review-job", args.apply
        return _cmd_job(args)
    if args.sheet is not None:
        args.command, args.out = "review-sheet", args.sheet
        return _cmd_job(args)
    from .review_html import write_review_html

    result = write_review_html(args.job_dir, args.out, images=args.images or "all")
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Review view: {result['out']} ({result['pages']} pages, {result['images']} images)")
        print(f"Blank review sheet: {result['sheet']}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    count = export_job(args.job_dir, args.out, format=args.format, reviewed_only=args.reviewed_only)
    print(f"Exported {count} pages to {args.out}")
    return 0


# -- rerun ---------------------------------------------------------------------


def _cmd_rerun(args: argparse.Namespace) -> int:
    cost_existed = (args.out / "cost.json").exists()
    try:
        result = rerun(
            parent_dir=args.parent_dir,
            config_path=args.config,
            out_dir=args.out,
            dry_run=args.dry_run,
            log_level=args.log_level,
            adapter_path=args.adapter_path,
        )
    except (RuntimeError, ValueError, OSError):
        if not args.json_output and not cost_existed:
            _print_persisted_budget_alerts(args.out)
        raise
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger rerun {result['run_id']} wrote {result['out_dir']}")
        print(f"Parent run: {result['parent_run_id']} (generation {result['rerun_depth']})")
        summary = result["summary"]
        print(f"Pages: {summary['pages_extracted']} extracted / {summary['pages_total']} total")
        print(f"Quality warning pages: {result['quality_warning_pages']}")
        _print_run_cost(args.out, dry_run=result["dry_run"])
        escalation = result.get("escalation")
        if escalation is not None:
            step = escalation["step"]
            adapter = escalation["adapter_order"][step]
            print(f"Escalation step: {step} ({adapter})")
        _print_budget_alerts(result)
        for warning in result.get("config_warnings", []):
            print(f"WARNING: {warning}")
        for warning in result.get("escalation_warnings", []):
            print(f"WARNING: {warning}")
    return 1 if result["status"] == "partial" and not result["dry_run"] else 0


def _print_budget_alerts(result: dict) -> None:
    for alert in result.get("budget_alerts", result.get("alerts", [])):
        print(
            "WARNING: Budget alert at "
            f"{alert['page_id']}: {alert['unit']}={alert['current']} reached "
            f"{alert['kind']} threshold {alert['threshold']}"
        )


def _print_persisted_budget_alerts(out_dir: Path) -> None:
    try:
        report = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(report, dict):
        _print_budget_alerts(report)


# -- doctor -------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = build_doctor_report(args.config)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"PageLedger {report['pageledger_version']}")
        print(f"Python: {report['python']['executable']}")
        for name, info in report["optional_packages"].items():
            status = "available" if info["available"] else "missing"
            print(f"Optional package {name}: {status}")
            if info["available"] and info.get("license"):
                print(f"  License: {info['license']}")
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
