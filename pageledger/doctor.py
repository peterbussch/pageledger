"""Environment diagnostics for PageLedger user workflows."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from typing import Any

from . import __version__

EXTERNAL_COMMANDS = {
    "pdftoppm": (
        ["-v"],
        "Install poppler (for example: brew install poppler, apt install poppler-utils).",
    ),
    "pdfinfo": (
        ["-v"],
        "Install poppler (for example: brew install poppler, apt install poppler-utils).",
    ),
    "tesseract": (
        ["--version"],
        "Install Tesseract OCR separately when you want local OCR.",
    ),
    "ocrmypdf": (
        ["--version"],
        "Install OCRmyPDF separately for external OCR preprocessing.",
    ),
    "docling": (
        ["--version"],
        "Install Docling in a project or tool venv if you choose that backend.",
    ),
    "marker_single": (
        ["--version"],
        "Install Marker in a project or tool venv if you choose that backend.",
    ),
    "surya_ocr": (
        ["--help"],
        "Install Surya and its runtime backend if you choose that backend.",
    ),
}
CLOUD_ENV_EXPLANATIONS = {
    "GOOGLE_API_KEY": "Optional for Gemini/VLM adapters.",
    "ANTHROPIC_API_KEY": "Optional for Claude/VLM adapters.",
    "OPENROUTER_API_KEY": "Optional for OpenRouter VLM adapters.",
    "OPENAI_API_KEY": "Optional for OpenAI VLM adapters.",
}
def build_doctor_report() -> dict[str, Any]:
    return {
        "pageledger_version": __version__,
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "runtime": platform.python_implementation(),
            "path": os.environ.get("PATH", ""),
        },
        "optional_packages": {
            "pypdf": {"available": importlib.util.find_spec("pypdf") is not None}
        },
        "external_commands": {
            name: _command_report(name)
            for name in EXTERNAL_COMMANDS
        },
        "ocr_languages": _ocr_languages_report(),
        "cloud_environment": {
            name: {
                "set": bool(os.environ.get(name)),
                "value": "<redacted>",
                "explanation": CLOUD_ENV_EXPLANATIONS[name],
            }
            for name in CLOUD_ENV_EXPLANATIONS
        },
    }


def _ocr_languages_report() -> dict[str, Any]:
    from .adapters import _tesseract_installed_langs

    path = shutil.which("tesseract")
    langs = _tesseract_installed_langs(path) if path else None
    return {
        "available": langs is not None,
        "languages": sorted(langs) if langs else [],
        "source": "tesseract --list-langs",
        "explanation": (
            "Installed Tesseract language packs; values for run.adapter_options.lang."
            if langs is not None
            else "Tesseract is missing or its language listing failed."
        ),
    }


def _command_report(name: str) -> dict[str, Any]:
    version_args, install_hint = EXTERNAL_COMMANDS[name]
    path = shutil.which(name)
    available = path is not None
    return {
        "available": available,
        "path": path,
        "version": _command_version(path, version_args) if path else None,
        "explanation": "Command is available on PATH." if available else f"{name} was not found on PATH.",
        "install_hint": install_hint,
    }


def _command_version(path: str, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            [path, *args],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr).strip()
    if not text:
        return None
    return text.splitlines()[0][:200]
