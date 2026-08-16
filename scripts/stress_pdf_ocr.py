#!/usr/bin/env python3
"""Exercise PageLedger from a clean install against a real PDF/OCR workflow."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMMAND_TIMEOUT = 180
OCR_COMMAND_TIMEOUT = 420
CLOUD_ENV_ORDER = [
    ("gemini", "GOOGLE_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
    ("claude", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
]


@dataclass
class CommandResult:
    name: str
    command: list[str]
    returncode: int | None
    duration_seconds: float
    stdout_tail: str
    stderr_tail: str
    skipped: bool = False
    classification: str = "ok"
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped and self.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--max-cloud-pages", type=int, default=1)
    parser.add_argument("--max-heavy-pages", type=int, default=2)
    parser.add_argument("--stress-dir", type=Path, default=None)
    parser.add_argument("--pdftoppm-bin", type=Path, default=None)
    parser.add_argument("--tesseract-bin", type=Path, default=None)
    parser.add_argument("--ocrmypdf-bin", type=Path, default=None)
    parser.add_argument("--docling-bin", type=Path, default=None)
    parser.add_argument("--marker-bin", type=Path, default=None)
    parser.add_argument("--surya-bin", type=Path, default=None)
    parser.add_argument("--skip-heavy-probes", action="store_true")
    args = parser.parse_args()

    pdf = args.pdf.expanduser().resolve()
    if not pdf.exists():
        print(f"PDF does not exist: {pdf}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stress_dir = (args.stress_dir or ROOT / ".stress" / f"pdf-ocr-{stamp}").resolve()
    stress_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "started_at": stamp,
        "repo": str(ROOT),
        "pdf": str(pdf),
        "stress_dir": str(stress_dir),
        "redacted_cloud_environment": {
            key: {"set": bool(os.environ.get(key)), "value": "<redacted>"}
            for _, key in CLOUD_ENV_ORDER
        },
        "commands": [],
        "tool_paths": _tool_paths(args),
        "checks": {},
        "critical_failures": [],
    }

    wheel = build_wheel(stress_dir, summary)
    pageledger_python = create_pageledger_venv(stress_dir, wheel, summary)
    pageledger_bin = pageledger_python.parent / "pageledger"

    clean_install_checks(stress_dir, pageledger_python, pageledger_bin, summary)
    run_pageledger_builtin_checks(stress_dir, pdf, pageledger_python, pageledger_bin, summary)
    run_ocrmypdf_roundtrip(stress_dir, pdf, pageledger_python, pageledger_bin, summary, args.ocrmypdf_bin)
    run_tesseract_adapter_probe(
        stress_dir,
        pdf,
        pageledger_python,
        pageledger_bin,
        summary,
        pdftoppm_bin=args.pdftoppm_bin,
        tesseract_bin=args.tesseract_bin,
        max_pages=args.max_heavy_pages,
    )
    if args.skip_heavy_probes:
        record_skip(summary, "heavy-local-tools", "backlog", "heavy probes skipped by flag")
    else:
        run_heavy_tool_probes(
            stress_dir,
            pdf,
            args.max_heavy_pages,
            summary,
            docling_bin=args.docling_bin,
            marker_bin=args.marker_bin,
            surya_bin=args.surya_bin,
        )
    run_cloud_probe(stress_dir, pdf, args.max_cloud_pages, summary)

    summary["completed_at"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    write_json(stress_dir / "stress-summary.json", summary)
    write_markdown(stress_dir / "stress-report.md", summary)
    print(f"Wrote stress summary to {stress_dir / 'stress-summary.json'}")
    print(f"Wrote stress report to {stress_dir / 'stress-report.md'}")
    if summary["critical_failures"]:
        print("Critical failures:", file=sys.stderr)
        for failure in summary["critical_failures"]:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


def build_wheel(stress_dir: Path, summary: dict[str, Any]) -> Path:
    builder = stress_dir / "venvs" / "builder"
    run(["python3", "-m", "venv", str(builder)], "create-builder-venv", summary)
    python = builder / "bin" / "python"
    run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip", "build"], "install-build", summary, timeout=OCR_COMMAND_TIMEOUT)
    dist = stress_dir / "dist"
    result = run([str(python), "-m", "build", "--wheel", "--outdir", str(dist)], "build-wheel", summary, cwd=ROOT, timeout=OCR_COMMAND_TIMEOUT)
    if result.returncode != 0:
        summary["critical_failures"].append("Could not build PageLedger wheel")
        raise SystemExit(1)
    wheels = sorted(dist.glob("pageledger-*.whl"))
    if not wheels:
        summary["critical_failures"].append("Build completed but no PageLedger wheel was produced")
        raise SystemExit(1)
    return wheels[-1]


def create_pageledger_venv(stress_dir: Path, wheel: Path, summary: dict[str, Any]) -> Path:
    venv = stress_dir / "venvs" / "pageledger"
    run(["python3", "-m", "venv", str(venv)], "create-pageledger-venv", summary)
    python = venv / "bin" / "python"
    run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], "upgrade-pageledger-pip", summary, timeout=OCR_COMMAND_TIMEOUT)
    result = run([str(python), "-m", "pip", "install", "--quiet", f"{wheel}[pdf]"], "install-pageledger-wheel-pdf", summary, timeout=OCR_COMMAND_TIMEOUT)
    if result.returncode != 0:
        summary["critical_failures"].append("Could not install PageLedger wheel with [pdf] extra")
        raise SystemExit(1)
    return python


def clean_install_checks(
    stress_dir: Path,
    python: Path,
    pageledger_bin: Path,
    summary: dict[str, Any],
) -> None:
    run([str(python), "-c", "import pageledger; print(pageledger.__version__)"], "import-pageledger", summary)
    run([str(pageledger_bin), "--help"], "pageledger-help", summary)
    run([str(pageledger_bin), "run", "--help"], "pageledger-run-help", summary)
    doctor = run([str(pageledger_bin), "doctor", "--json"], "pageledger-doctor-json", summary)
    if doctor.ok:
        summary["checks"]["doctor"] = json.loads(doctor.stdout_tail)
        text = doctor.stdout_tail + doctor.stderr_tail
        for _, key in CLOUD_ENV_ORDER:
            value = os.environ.get(key)
            if value and value in text:
                summary["critical_failures"].append(f"doctor output leaked {key}")


def run_pageledger_builtin_checks(
    stress_dir: Path,
    pdf: Path,
    python: Path,
    pageledger_bin: Path,
    summary: dict[str, Any],
) -> None:
    configs = stress_dir / "configs"
    configs.mkdir(exist_ok=True)
    pdf_config = configs / "pageledger-pdf.yml"
    pdf_config.write_text(
        textwrap.dedent(
            """\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: pdf_text
            """
        ),
        encoding="utf-8",
    )
    text_config = configs / "pageledger-text.yml"
    text_config.write_text(
        textwrap.dedent(
            """\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """
        ),
        encoding="utf-8",
    )

    dry_out = stress_dir / "runs" / "pdf-text-dry"
    run([str(pageledger_bin), "run", str(pdf), "--config", str(pdf_config), "--out", str(dry_out), "--dry-run", "--json"], "pageledger-pdf-text-dry-run", summary, timeout=OCR_COMMAND_TIMEOUT)
    dry_manifest = read_manifest(dry_out)
    summary["checks"]["pdf_text_dry_run"] = dry_manifest
    if dry_manifest.get("summary", {}).get("pages_total") != 72:
        summary["critical_failures"].append("pdf_text dry-run did not report 72 pages")

    exec_out = stress_dir / "runs" / "pdf-text-execute"
    run([str(pageledger_bin), "run", str(pdf), "--config", str(pdf_config), "--out", str(exec_out), "--json"], "pageledger-pdf-text-execute", summary, timeout=OCR_COMMAND_TIMEOUT)
    exec_manifest = read_manifest(exec_out)
    raw_count = len(list((exec_out / "raw").glob("*.txt"))) if (exec_out / "raw").exists() else 0
    provenance_lines = line_count(exec_out / "provenance.jsonl")
    summary["checks"]["pdf_text_execute"] = {
        "manifest": exec_manifest,
        "raw_count": raw_count,
        "provenance_lines": provenance_lines,
    }
    if exec_manifest.get("summary", {}).get("pages_extracted") != 72 or raw_count != 72 or provenance_lines != 72:
        summary["critical_failures"].append("pdf_text execute did not produce 72 raw/provenance outputs")

    mismatch_out = stress_dir / "runs" / "text-on-pdf"
    mismatch = run([str(pageledger_bin), "run", str(pdf), "--config", str(text_config), "--out", str(mismatch_out), "--json"], "pageledger-text-on-pdf-preflight", summary)
    summary["checks"]["text_on_pdf_preflight"] = {
        "returncode": mismatch.returncode,
        "out_dir_exists": mismatch_out.exists(),
        "stderr_tail": mismatch.stderr_tail,
    }
    if mismatch.returncode == 0 or mismatch_out.exists():
        summary["critical_failures"].append("text adapter on PDF did not fail before writing output")
    else:
        update_command(summary, "pageledger-text-on-pdf-preflight", classification="ok", note="Expected preflight failure with no output directory")


def run_ocrmypdf_roundtrip(
    stress_dir: Path,
    pdf: Path,
    pageledger_python: Path,
    pageledger_bin: Path,
    summary: dict[str, Any],
    ocrmypdf_bin: Path | None,
) -> None:
    tool_venv = stress_dir / "venvs" / "ocrmypdf"
    run(["python3", "-m", "venv", str(tool_venv)], "create-ocrmypdf-venv", summary)
    python = tool_venv / "bin" / "python"
    run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip", "ocrmypdf"], "install-ocrmypdf", summary, timeout=OCR_COMMAND_TIMEOUT)
    ocrmypdf = ocrmypdf_bin or tool_venv / "bin" / "ocrmypdf"
    if not ocrmypdf.exists():
        record_skip(summary, "ocrmypdf-force-ocr", "external tool/environment", "ocrmypdf command was not installed")
        return

    ocr_pdf = stress_dir / "ocr" / "ocrmypdf-force-pages-1-2.pdf"
    ocr_pdf.parent.mkdir(exist_ok=True)
    result = run([str(ocrmypdf), "--force-ocr", "--pages", "1-2", "--output-type", "pdf", str(pdf), str(ocr_pdf)], "ocrmypdf-force-ocr", summary, timeout=OCR_COMMAND_TIMEOUT)
    if result.returncode != 0:
        update_command(summary, "ocrmypdf-force-ocr", classification="external tool/environment")
        return

    config = stress_dir / "configs" / "pageledger-pdf.yml"
    out_dir = stress_dir / "runs" / "ocrmypdf-roundtrip"
    run([str(pageledger_bin), "run", str(ocr_pdf), "--config", str(config), "--out", str(out_dir), "--json"], "pageledger-over-ocrmypdf-output", summary, timeout=OCR_COMMAND_TIMEOUT)
    summary["checks"]["ocrmypdf_roundtrip"] = {
        "manifest": read_manifest(out_dir),
        "first_page_sample": read_text_sample(out_dir / "raw" / "doc_0001_page_0001.txt"),
    }


def run_tesseract_adapter_probe(
    stress_dir: Path,
    pdf: Path,
    pageledger_python: Path,
    pageledger_bin: Path,
    summary: dict[str, Any],
    *,
    pdftoppm_bin: Path | None,
    tesseract_bin: Path | None,
    max_pages: int,
) -> None:
    adapter_dir = stress_dir / "custom_adapters"
    adapter_dir.mkdir(exist_ok=True)
    adapter_file = adapter_dir / "tesseract_adapter.py"
    adapter_file.write_text(TESSERACT_ADAPTER_CODE, encoding="utf-8")
    config = stress_dir / "configs" / "pageledger-tesseract.yml"
    config.write_text(
        textwrap.dedent(
            """\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: tesseract_adapter:TesseractCliAdapter
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(adapter_dir)
    if pdftoppm_bin is not None:
        env["PAGELEDGER_PDFTOPPM"] = str(pdftoppm_bin)
    if tesseract_bin is not None:
        env["PAGELEDGER_TESSERACT"] = str(tesseract_bin)
    env["PAGELEDGER_MAX_HEAVY_PAGES"] = str(max(1, max_pages))
    out_dir = stress_dir / "runs" / "tesseract-custom-adapter"
    result = run([str(pageledger_bin), "run", str(pdf), "--config", str(config), "--out", str(out_dir), "--json"], "pageledger-tesseract-custom-adapter", summary, env=env, timeout=OCR_COMMAND_TIMEOUT)
    if result.returncode != 0:
        update_command(summary, "pageledger-tesseract-custom-adapter", classification="external tool/environment")
    summary["checks"]["tesseract_adapter"] = {
        "returncode": result.returncode,
        "manifest": read_manifest(out_dir) if out_dir.exists() else {},
        "run_log_sample": read_text_sample(out_dir / "run.log"),
    }


def run_heavy_tool_probes(
    stress_dir: Path,
    pdf: Path,
    max_pages: int,
    summary: dict[str, Any],
    *,
    docling_bin: Path | None,
    marker_bin: Path | None,
    surya_bin: Path | None,
) -> None:
    if max_pages <= 0:
        record_skip(summary, "heavy-local-tools", "backlog", "max heavy pages is 0")
        return
    heavy_dir = stress_dir / "heavy"
    heavy_dir.mkdir(exist_ok=True)

    docling = str(docling_bin) if docling_bin else shutil.which("docling")
    if docling:
        run([docling, "convert", "--to", "md", "--to", "json", "--output", str(heavy_dir / "docling"), str(pdf)], "docling-convert", summary, timeout=OCR_COMMAND_TIMEOUT)
    else:
        record_skip(summary, "docling-convert", "external tool/environment", "docling command not found")

    marker = str(marker_bin) if marker_bin else shutil.which("marker_single")
    if marker:
        run([marker, str(pdf), "--page_range", f"0-{max_pages - 1}", "--output_format", "markdown", "--paginate_output", "--output_dir", str(heavy_dir / "marker")], "marker-single", summary, timeout=OCR_COMMAND_TIMEOUT)
    else:
        record_skip(summary, "marker-single", "external tool/environment", "marker_single command not found")

    surya = str(surya_bin) if surya_bin else shutil.which("surya_ocr")
    has_surya_backend = shutil.which("llama-server") or os.environ.get("SURYA_INFERENCE_URL")
    if surya and has_surya_backend:
        run([surya, str(pdf), "--page_range", f"0-{max_pages - 1}", "--output_dir", str(heavy_dir / "surya")], "surya-ocr", summary, timeout=OCR_COMMAND_TIMEOUT)
    elif surya:
        record_skip(summary, "surya-ocr", "external tool/environment", "surya_ocr found but no llama-server or SURYA_INFERENCE_URL backend was found")
    else:
        record_skip(summary, "surya-ocr", "external tool/environment", "surya_ocr command not found")


def run_cloud_probe(stress_dir: Path, pdf: Path, max_pages: int, summary: dict[str, Any]) -> None:
    if max_pages <= 0:
        record_skip(summary, "cloud-vlm-ocr", "backlog", "max cloud pages is 0")
        return
    provider_info = next(((name, key) for name, key in CLOUD_ENV_ORDER if os.environ.get(key)), None)
    if provider_info is None:
        record_skip(summary, "cloud-vlm-ocr", "external tool/environment", "no supported cloud OCR/VLM env var is set")
        return
    provider, _key = provider_info
    image = render_first_page(stress_dir, pdf, summary)
    if image is None:
        record_skip(summary, f"cloud-vlm-ocr-{provider}", "external tool/environment", "could not render page 1 for cloud probe")
        return

    venv = stress_dir / "venvs" / f"cloud-{provider}"
    run(["python3", "-m", "venv", str(venv)], f"create-cloud-{provider}-venv", summary)
    python = venv / "bin" / "python"
    package_map = {
        "gemini": ["google-genai", "pillow"],
        "openrouter": ["openai"],
        "claude": ["anthropic"],
        "openai": ["openai"],
    }
    run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip", *package_map[provider]], f"install-cloud-{provider}-deps", summary, timeout=OCR_COMMAND_TIMEOUT)
    probe = stress_dir / f"cloud_{provider}_probe.py"
    output = stress_dir / "cloud" / f"{provider}-page-1.json"
    output.parent.mkdir(exist_ok=True)
    probe.write_text(CLOUD_PROBES[provider], encoding="utf-8")
    result = run([str(python), str(probe), str(image), str(output)], f"cloud-vlm-ocr-{provider}", summary, timeout=OCR_COMMAND_TIMEOUT)
    if result.returncode != 0:
        update_command(summary, f"cloud-vlm-ocr-{provider}", classification="external tool/environment")
    summary["checks"][f"cloud_{provider}"] = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}


def render_first_page(stress_dir: Path, pdf: Path, summary: dict[str, Any]) -> Path | None:
    configured = summary.get("tool_paths", {}).get("pdftoppm")
    pdftoppm = configured or shutil.which("pdftoppm")
    if not pdftoppm:
        return None
    image_dir = stress_dir / "cloud" / "rendered"
    image_dir.mkdir(parents=True, exist_ok=True)
    prefix = image_dir / "page"
    result = run([pdftoppm, "-f", "1", "-l", "1", "-r", "120", "-png", str(pdf), str(prefix)], "render-cloud-page-1", summary)
    if result.returncode != 0:
        return None
    images = sorted(image_dir.glob("page-*.png"))
    return images[0] if images else None


def run(
    command: list[str],
    name: str,
    summary: dict[str, Any],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> CommandResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        result = CommandResult(
            name=name,
            command=command,
            returncode=completed.returncode,
            duration_seconds=round(time.monotonic() - start, 3),
            stdout_tail=tail(completed.stdout),
            stderr_tail=tail(completed.stderr),
            classification="ok" if completed.returncode == 0 else "package UX gap",
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            name=name,
            command=command,
            returncode=None,
            duration_seconds=round(time.monotonic() - start, 3),
            stdout_tail=tail(exc.stdout or ""),
            stderr_tail=tail(exc.stderr or ""),
            classification="external tool/environment",
            note=f"Timed out after {timeout} seconds",
        )
    summary["commands"].append(asdict(result))
    print(f"[{name}] rc={result.returncode} class={result.classification}")
    return result


def _tool_paths(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "pdftoppm": str(args.pdftoppm_bin) if args.pdftoppm_bin else None,
        "tesseract": str(args.tesseract_bin) if args.tesseract_bin else None,
        "ocrmypdf": str(args.ocrmypdf_bin) if args.ocrmypdf_bin else None,
        "docling": str(args.docling_bin) if args.docling_bin else None,
        "marker_single": str(args.marker_bin) if args.marker_bin else None,
        "surya_ocr": str(args.surya_bin) if args.surya_bin else None,
    }


def record_skip(summary: dict[str, Any], name: str, classification: str, note: str) -> None:
    result = CommandResult(
        name=name,
        command=[],
        returncode=None,
        duration_seconds=0.0,
        stdout_tail="",
        stderr_tail="",
        skipped=True,
        classification=classification,
        note=note,
    )
    summary["commands"].append(asdict(result))
    print(f"[{name}] skipped class={classification}: {note}")


def update_command(
    summary: dict[str, Any],
    name: str,
    *,
    classification: str | None = None,
    note: str | None = None,
) -> None:
    for command in reversed(summary["commands"]):
        if command["name"] == name:
            if classification is not None:
                command["classification"] = classification
            if note is not None:
                command["note"] = note
            return


def read_manifest(run_dir: Path) -> dict[str, Any]:
    manifest = run_dir / "manifest.json"
    if not manifest.exists():
        return {}
    return json.loads(manifest.read_text(encoding="utf-8"))


def read_text_sample(path: Path, limit: int = 500) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, summary: dict[str, Any]) -> None:
    commands = summary["commands"]
    lines = [
        "# PageLedger PDF/OCR Stress Run",
        "",
        f"- PDF: `{summary['pdf']}`",
        f"- Stress directory: `{summary['stress_dir']}`",
        f"- Started: `{summary['started_at']}`",
        f"- Completed: `{summary.get('completed_at', '')}`",
        "",
        "## Command Results",
        "",
        "| name | status | classification | note |",
        "|---|---:|---|---|",
    ]
    for command in commands:
        status = "skipped" if command["skipped"] else command["returncode"]
        note = command["note"].replace("|", "\\|")
        lines.append(f"| `{command['name']}` | {status} | {command['classification']} | {note} |")
    lines.extend(
        [
            "",
            "## Key Checks",
            "",
            "```json",
            json.dumps(summary["checks"], indent=2, ensure_ascii=False, sort_keys=True)[:12000],
            "```",
            "",
            "## Critical Failures",
            "",
        ]
    )
    if summary["critical_failures"]:
        lines.extend(f"- {failure}" for failure in summary["critical_failures"])
    else:
        lines.append("None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tail(text: str | bytes, limit: int = 4000) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[-limit:]


TESSERACT_ADAPTER_CODE = r'''
from __future__ import annotations

import subprocess
import tempfile
import os
from dataclasses import dataclass
from pathlib import Path

from pageledger.adapters import ExtractionResult, pdf_page_count


@dataclass(frozen=True)
class TesseractCliAdapter:
    name: str = "tesseract-cli"
    version: str = "cli-probe"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("pdf",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("ocr", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def page_count(self, source: Path) -> int:
        cap = int(os.environ.get("PAGELEDGER_MAX_HEAVY_PAGES", "2"))
        return min(pdf_page_count(source), max(1, cap))

    def extract(self, source: Path, *, page_id: str, page_number: int, action: str, prompt: str | None = None) -> ExtractionResult:
        if not self.supports(action):
            raise ValueError(f"Tesseract adapter does not support action: {action}")
        _ = page_id, prompt
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prefix = tmp_path / "page"
            subprocess.run(
                [os.environ.get("PAGELEDGER_PDFTOPPM", "pdftoppm"), "-f", str(page_number), "-l", str(page_number), "-r", "150", "-png", str(source), str(prefix)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            images = sorted(tmp_path.glob("page-*.png"))
            if not images:
                raise RuntimeError("pdftoppm did not render a page image")
            output_prefix = tmp_path / "ocr"
            subprocess.run(
                [os.environ.get("PAGELEDGER_TESSERACT", "tesseract"), str(images[0]), str(output_prefix), "-l", "eng", "--psm", "6"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            text = output_prefix.with_suffix(".txt").read_text(encoding="utf-8", errors="replace")
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model="tesseract-cli",
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )
'''


CLOUD_PROBES = {
    "gemini": r'''
from __future__ import annotations

import json
import sys
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

image_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=[
        "Extract the visible title and the first short paragraph from this report page. Return plain text only.",
        Image.open(image_path),
    ],
    config=types.GenerateContentConfig(max_output_tokens=256, temperature=0.0),
)
output_path.write_text(
    json.dumps(
        {
            "provider": "gemini",
            "model": "gemini-2.0-flash",
            "text_sample": (response.text or "")[:1000],
            "usage": getattr(response, "usage_metadata", None).model_dump()
            if getattr(response, "usage_metadata", None)
            else None,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
''',
    "openrouter": r'''
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from openai import OpenAI

image_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
data = base64.b64encode(image_path.read_bytes()).decode("ascii")
client = OpenAI(api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
model = "qwen/qwen3-vl-235b-a22b-instruct"
response = client.chat.completions.create(
    model=model,
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract the visible title and the first short paragraph from this report page. Return plain text only."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
            ],
        }
    ],
    max_tokens=256,
    temperature=0,
)
usage = response.usage.model_dump() if getattr(response, "usage", None) else None
output_path.write_text(
    json.dumps(
        {
            "provider": "openrouter",
            "model": model,
            "text_sample": (response.choices[0].message.content or "")[:1000],
            "usage": usage,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
''',
    "claude": r'''
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import anthropic

image_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
data = base64.b64encode(image_path.read_bytes()).decode("ascii")
model = "claude-3-5-haiku-20241022"
client = anthropic.Anthropic()
response = client.messages.create(
    model=model,
    max_tokens=256,
    temperature=0,
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract the visible title and the first short paragraph from this report page. Return plain text only."},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
            ],
        }
    ],
)
usage = response.usage.model_dump() if getattr(response, "usage", None) else None
text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
output_path.write_text(
    json.dumps(
        {
            "provider": "claude",
            "model": model,
            "text_sample": text[:1000],
            "usage": usage,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
''',
    "openai": r'''
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from openai import OpenAI

image_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
data = base64.b64encode(image_path.read_bytes()).decode("ascii")
model = "gpt-4o-mini"
client = OpenAI()
response = client.chat.completions.create(
    model=model,
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract the visible title and the first short paragraph from this report page. Return plain text only."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
            ],
        }
    ],
    max_tokens=256,
    temperature=0,
)
usage = response.usage.model_dump() if getattr(response, "usage", None) else None
output_path.write_text(
    json.dumps(
        {
            "provider": "openai",
            "model": model,
            "text_sample": (response.choices[0].message.content or "")[:1000],
            "usage": usage,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
''',
}


if __name__ == "__main__":
    raise SystemExit(main())
