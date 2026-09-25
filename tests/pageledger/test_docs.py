"""The documentation must agree with the code it describes."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import pageledger
from pageledger.config import load_config

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = sorted([ROOT / "README.md", *(ROOT / "docs").rglob("*.md")])
_FENCE = re.compile(r"^(```+|~~~+)")
_LINK = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _prose_lines(text: str) -> list[str]:
    """Lines outside fenced code blocks."""
    lines, fence = [], None
    for line in text.splitlines():
        marker = _FENCE.match(line.strip())
        if marker and (fence is None or marker.group(1).startswith(fence)):
            fence = None if fence else marker.group(1)
            continue
        if fence is None:
            lines.append(line)
    return lines


def _code_blocks(text: str, language: str) -> list[str]:
    blocks, current = [], None
    for line in text.splitlines():
        if current is None and line.strip().startswith(f"```{language}"):
            current = []
        elif current is not None and line.strip() == "```":
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(document: Path) -> set[str]:
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for line in _prose_lines(document.read_text(encoding="utf-8")):
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links(document: Path) -> list[str]:
    targets = []
    for line in _prose_lines(document.read_text(encoding="utf-8")):
        for match in _LINK.finditer(re.sub(r"`[^`]*`", "", line)):
            targets.append(match.group(1).strip("<>"))
    return targets


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(document: Path) -> None:
    broken = []
    for target in _links(document):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue
        path_part, _, anchor = target.partition("#")
        resolved = (document.parent / path_part).resolve() if path_part else document
        if not resolved.exists():
            broken.append(f"{target}: no such file")
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            broken.append(f"{target}: no heading with anchor #{anchor}")
    assert broken == []


# Top-level keys of run artifacts (route maps, rerun manifests), which carry a
# schema_version too but are not configs.
_ARTIFACT_KEYS = {"run_id", "documents", "items"}


def _config_blocks() -> list[tuple[str, str]]:
    blocks = []
    for document in DOCUMENTS:
        for index, block in enumerate(_code_blocks(document.read_text(encoding="utf-8"), "yaml")):
            if "schema_version" not in block or "# fragment" in block:
                continue
            data = yaml.safe_load(block)
            if isinstance(data, dict) and not _ARTIFACT_KEYS & set(data):
                blocks.append((f"{document.relative_to(ROOT)}#{index}", block))
    return blocks


@pytest.mark.parametrize("name,block", _config_blocks(), ids=[name for name, _ in _config_blocks()])
def test_documented_configs_load(tmp_path: Path, name: str, block: str) -> None:
    path = tmp_path / "config.yml"
    path.write_text(block, encoding="utf-8")
    config = load_config(path, validate_adapter=False)
    if config.adapter_name is not None and "processing" not in config.data:
        # A run config without page types extracts nothing (see "No extraction route").
        assert config.has_page_types, f"{name} runs an adapter but has no taxonomy.page_types"


_CURRENT_VERSION_FILES = [
    ROOT / "README.md",
    ROOT / "docs" / "README.md",
    ROOT / "skills" / "pageledger" / "SKILL.md",
    ROOT / "docs" / "route-map-spec.md",
]


@pytest.mark.parametrize("document", _CURRENT_VERSION_FILES, ids=lambda path: path.name)
def test_current_version_strings_match_the_package(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    versions = set(re.findall(r"pageledger(?:\[pdf\])?==(\d+\.\d+\.\d+)", text))
    versions |= set(re.findall(r"PageLedger (\d+\.\d+\.\d+)", text))
    versions |= set(re.findall(r'pageledger_version: "(\d+\.\d+\.\d+)"', text))
    assert versions <= {pageledger.__version__}


def test_readme_first_run_executes(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(ROOT / "README.md"),
            "--work-dir",
            str(tmp_path / "readme"),
            "--python",
            sys.executable,
            "--source-root",
            str(ROOT),
        ],
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    assert (tmp_path / "readme" / "runs" / "second" / "manifest.json").is_file()


_EXAMPLE_CONFIGS = [
    path
    for path in sorted((ROOT / "docs" / "examples").glob("*.yml"))
    if "# fragment" not in path.read_text(encoding="utf-8")
    and isinstance(data := yaml.safe_load(path.read_text(encoding="utf-8")), dict)
    and "schema_version" in data
    and not _ARTIFACT_KEYS & set(data)
]


@pytest.mark.parametrize("example", _EXAMPLE_CONFIGS, ids=lambda path: path.name)
def test_example_config_files_load(example: Path) -> None:
    config = load_config(example, validate_adapter=False)
    if config.adapter_name is not None and "processing" not in config.data:
        assert config.has_page_types, f"{example.name} runs an adapter but has no page types"
