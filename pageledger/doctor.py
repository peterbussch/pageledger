"""Environment diagnostics for PageLedger user workflows."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from ._version import __version__

# Tesseract's own data, not languages a page can be read in: script detection and math.
NOT_LANGUAGES = frozenset({"osd", "equ"})
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
OPTIONAL_PACKAGES: dict[str, dict[str, str]] = {
    "pypdf": {},
    "rapidocr": {},
    "onnxruntime": {},
    "pymorphy3": {"extra": "ru"},
    "indra_ocr": {
        "extra": "indra",
        "license": (
            "Code Apache-2.0; model weights modified OpenRAIL-M: free for research, personal "
            "use and organizations under $20M funding or revenue, not for use competing with "
            "Datalab's API. Check the terms before commercial use."
        ),
    },
}
CLOUD_ENV_EXPLANATIONS = {
    "GOOGLE_API_KEY": "Optional for Gemini/VLM adapters.",
    "ANTHROPIC_API_KEY": "Optional for Claude/VLM adapters.",
    "OPENROUTER_API_KEY": "Optional for OpenRouter VLM adapters.",
    "OPENAI_API_KEY": "Optional for OpenAI VLM adapters.",
}


def build_doctor_report(config_path: Path | None = None) -> dict[str, Any]:
    """Diagnostics; with a config, also the environment variables its adapters name by env_key."""
    cloud = {
        name: {"set": bool(os.environ.get(name)), "value": "<redacted>", "explanation": explanation}
        for name, explanation in CLOUD_ENV_EXPLANATIONS.items()
    }
    if config_path is not None:
        for name in sorted(_env_keys(_read_config(Path(config_path)))):
            cloud[name] = {
                "set": bool(os.environ.get(name)),
                "value": "<redacted>",
                "explanation": f"Named by env_key in {Path(config_path).name}.",
                "named_by_config": True,
            }
    return {
        "pageledger_version": __version__,
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "runtime": platform.python_implementation(),
            "path": os.environ.get("PATH", ""),
        },
        "optional_packages": {
            name: {"available": importlib.util.find_spec(name) is not None, **extra}
            for name, extra in OPTIONAL_PACKAGES.items()
        },
        "external_commands": {name: _command_report(name) for name in EXTERNAL_COMMANDS},
        "ocr_languages": _ocr_languages_report(),
        "cloud_environment": cloud,
    }


def _read_config(path: Path) -> Any:
    """The config's YAML, or a plain sentence when it is missing or does not parse."""
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"Config file not found: {path}") from None
    except UnicodeDecodeError:
        raise ValueError(f"Config file {path} is not UTF-8 text") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Could not parse config YAML in {path}: {exc}") from None


def _env_keys(value: Any) -> set[str]:
    """Every string set as `env_key` anywhere in a loaded config."""
    if isinstance(value, dict):
        found = {value["env_key"]} if isinstance(value.get("env_key"), str) else set()
        return found.union(*(_env_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_env_keys(item) for item in value))
    return set()


def _ocr_languages_report() -> dict[str, Any]:
    from .adapters import _tesseract_installed_langs

    path = shutil.which("tesseract")
    langs = _tesseract_installed_langs(path) if path else None
    return {
        "available": langs is not None,
        "languages": sorted(langs) if langs else [],
        "source": "tesseract --list-langs",
        "explanation": (
            "Installed Tesseract language packs; all but osd and equ are values for --lang."
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
        "explanation": "Command is available on PATH."
        if available
        else f"{name} was not found on PATH.",
        "install_hint": install_hint,
    }


def _command_version(path: str, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            [path, *args],
            text=True,
            capture_output=True,
            # A tool's first run after install can take a while to answer.
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr).strip()
    if not text:
        return None
    return text.splitlines()[0][:200]
