#!/usr/bin/env python3
"""Fail closed when release tag and tracked version surfaces disagree."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 release-tool fallback
    import tomli as tomllib


def check_release(root: Path, tag: str) -> list[str]:
    """Return every release metadata disagreement found under *root*."""
    errors: list[str] = []
    root = root.resolve()

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        return ["pyproject.toml does not declare a nonempty project.version"]

    expected_tag = f"v{version}"
    if tag != expected_tag:
        if tag.startswith("v"):
            errors.append(f"tag {tag} does not match package version {version}")
        else:
            errors.append(
                f"tag {tag} does not match package version {version} "
                f"(expected {expected_tag})"
            )

    version_text = (root / "pageledger" / "_version.py").read_text(encoding="utf-8")
    runtime_match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        version_text,
        flags=re.MULTILINE,
    )
    runtime_version = runtime_match.group(1) if runtime_match else None
    if runtime_version != version:
        errors.append(
            f"pageledger/_version.py version {runtime_version!r} does not match {version}"
        )

    citation_text = (root / "CITATION.cff").read_text(encoding="utf-8")
    citation_version = _quoted_field(citation_text, "version")
    citation_date = _quoted_field(citation_text, "date-released")
    if citation_version != version:
        errors.append(
            f"CITATION.cff version {citation_version or '<missing>'} does not match {version}"
        )

    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    editable_versions = [
        package.get("version")
        for package in lock.get("package", [])
        if package.get("name") == "pageledger"
        and package.get("source", {}).get("editable") == "."
    ]
    if editable_versions != [version]:
        errors.append(
            f"uv.lock editable pageledger versions {editable_versions!r} do not match [{version!r}]"
        )

    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_match = re.search(
        rf"^## {re.escape(version)} - (\d{{4}}-\d{{2}}-\d{{2}})$",
        changelog_text,
        flags=re.MULTILINE,
    )
    if changelog_match is None:
        errors.append(f"CHANGELOG.md has no dated heading for {version}")
    elif citation_date != changelog_match.group(1):
        errors.append(
            "CITATION.cff date-released "
            f"{citation_date or '<missing>'} does not match changelog date "
            f"{changelog_match.group(1)}"
        )

    return errors


def _quoted_field(text: str, field: str) -> str | None:
    match = re.search(
        rf'^{re.escape(field)}:\s*["\']([^"\']+)["\']\s*$',
        text,
        flags=re.MULTILINE,
    )
    return match.group(1) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, including the v prefix")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the script's parent repository)",
    )
    args = parser.parse_args(argv)
    errors = check_release(args.root, args.tag)
    if errors:
        for error in errors:
            print(f"release check: {error}", file=sys.stderr)
        return 1
    print(f"release check: {args.tag} metadata agrees")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
