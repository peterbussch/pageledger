#!/usr/bin/env python3
"""Check the wheel schemas and source-distribution documentation before release."""
from __future__ import annotations

import argparse
from pathlib import Path
from tarfile import open as open_tar
from zipfile import ZipFile

REQUIRED_SDIST_FILES = (
    "docs/document-first-run.md",
    "docs/first-run.md",
    "docs/reader-trial.md",
    "docs/performance.md",
    "docs/releasing.md",
    "examples/run_document_recovery.py",
    "examples/run_first_run.py",
    "scripts/check_distributions.py",
    "scripts/check_release.py",
    "docs/validation/0.5.1/README.md",
    "docs/validation/0.5.1/summary.json",
    "docs/validation/0.5.1/page-results.csv",
    "docs/validation/0.5.1/observations.json",
    "docs/validation/0.5.1/nara.yml",
    "docs/validation/0.5.1/cyrillic.yml",
    "docs/validation/0.5.1/vlm-results.json",
    "docs/validation/0.5.1/vlm.yml",
)


def check_distributions(root: Path, dist: Path) -> None:
    wheel, = dist.glob("pageledger-*.whl")
    sdist, = dist.glob("pageledger-*.tar.gz")
    schemas = sorted(path.name for path in (root / "schemas").glob("*.schema.json"))
    if not schemas:
        raise ValueError("No source schemas found")
    with ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    for schema in schemas:
        if not any(name.endswith(".data/data/share/pageledger/schemas/" + schema)
                   for name in wheel_names):
            raise ValueError(f"Wheel is missing schema: {schema}")

    with open_tar(sdist, "r:gz") as archive:
        sdist_names = archive.getnames()
    prefix = sdist.name.removesuffix(".tar.gz") + "/"
    for path in REQUIRED_SDIST_FILES:
        if prefix + path not in sdist_names:
            raise ValueError(f"Source distribution is missing: {path}")
    for name in sdist_names:
        if any(part in name for part in (
            "/docs/superpowers/", "/docs/proposals/", "/docs/reports/",
            "/.planning/", "/.superpowers/", "/runs/", "/.venv/",
        )):
            raise ValueError(f"Source distribution contains local-only material: {name}")
    print("wheel schema and sdist documentation inventories verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, help="directory containing exactly one wheel and sdist")
    args = parser.parse_args()
    check_distributions(Path(__file__).resolve().parents[1], args.dist)
