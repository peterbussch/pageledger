from __future__ import annotations

import re
from pathlib import Path

import yaml

import pageledger


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_citation_metadata_matches_release_version() -> None:
    root = _repo_root()
    citation = yaml.safe_load((root / "CITATION.cff").read_text(encoding="utf-8"))
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    version_match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)
    assert version_match is not None
    pyproject_version = version_match.group(1)

    assert citation["cff-version"] == "1.2.0"
    assert citation["type"] == "software"
    assert citation["title"]
    assert citation["message"]
    assert citation["license"] == "MIT"
    assert citation["repository-code"] == "https://github.com/peterbussch/pageledger"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", citation["date-released"])

    authors = citation["authors"]
    assert isinstance(authors, list)
    assert authors
    first_author = authors[0]
    assert first_author["family-names"] == "Busscher"
    assert first_author["given-names"] == "Peter"
    assert first_author["orcid"] == "https://orcid.org/0000-0002-0902-8195"

    assert citation["version"] == pyproject_version
    assert citation["version"] == pageledger.__version__