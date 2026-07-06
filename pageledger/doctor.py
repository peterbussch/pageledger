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

OPTIONAL_PACKAGES = ["pypdf"]
EXTERNAL_COMMANDS = [
    "pdftoppm",
    "pdfinfo",
    "tesseract",
    "ocrmypdf",
    "docling",
    "marker_single",
    "surya_ocr",
]
COMMAND_VERSION_ARGS = {
    "pdftoppm": ["-v"],
    "pdfinfo": ["-v"],
    "tesseract": ["--version"],
    "ocrmypdf": ["--version"],
    "docling": ["--version"],
    "marker_single": ["--version"],
    "surya_ocr": ["--help"],
}
INSTALL_HINTS = {
    "pdftoppm": "Install poppler (for example: brew install poppler, apt install poppler-utils).",
    "pdfinfo": "Install poppler (for example: brew install poppler, apt install poppler-utils).",
    "tesseract": "Install Tesseract OCR separately when you want local OCR.",
    "ocrmypdf": "Install OCRmyPDF separately for external OCR preprocessing.",
    "docling": "Install Docling in a project or tool venv if you choose that backend.",
    "marker_single": "Install Marker in a project or tool venv if you choose that backend.",
    "surya_ocr": "Install Surya and its runtime backend if you choose that backend.",
}
CLOUD_ENV_EXPLANATIONS = {
    "GOOGLE_API_KEY": "Optional for Gemini/VLM adapters.",
    "ANTHROPIC_API_KEY": "Optional for Claude/VLM adapters.",
    "OPENROUTER_API_KEY": "Optional for OpenRouter VLM adapters.",
    "OPENAI_API_KEY": "Optional for OpenAI VLM adapters.",
}
CLOUD_ENV_KEYS = [
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
]


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
            name: {"available": importlib.util.find_spec(name) is not None}
            for name in OPTIONAL_PACKAGES
        },
        "external_commands": {
            name: _command_report(name)
            for name in EXTERNAL_COMMANDS
        },
        "cloud_environment": {
            name: {
                "set": bool(os.environ.get(name)),
                "value": "<redacted>",
                "explanation": CLOUD_ENV_EXPLANATIONS[name],
            }
            for name in CLOUD_ENV_KEYS
        },
    }


def _command_report(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    available = path is not None
    return {
        "available": available,
        "path": path,
        "version": _command_version(path, COMMAND_VERSION_ARGS[name]) if path else None,
        "explanation": "Command is available on PATH." if available else f"{name} was not found on PATH.",
        "install_hint": INSTALL_HINTS[name],
    }


def _command_version(path: str, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            [path, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr).strip()
    if not text:
        return None
    return text.splitlines()[0][:200]
