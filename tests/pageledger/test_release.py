"""Release metadata and workflow gates."""

from __future__ import annotations

import re
from pathlib import Path
from zipfile import ZipFile

import yaml

from scripts.check_release import check_release

REPO = Path(__file__).resolve().parents[2]


def _write_release_fixture(root: Path, *, citation_version: str = "1.2.3") -> None:
    (root / "pageledger").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pageledger"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (root / "pageledger" / "_version.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (root / "CITATION.cff").write_text(
        f'version: "{citation_version}"\ndate-released: "2026-08-16"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pageledger"\nversion = "1.2.3"\n'
        'source = { editable = "." }\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3 - 2026-08-16\n", encoding="utf-8"
    )


def test_release_metadata_agrees_for_current_version() -> None:
    assert check_release(REPO, "v0.4.0") == []


def test_release_check_rejects_tag_and_metadata_mismatches(tmp_path: Path) -> None:
    _write_release_fixture(tmp_path, citation_version="1.2.2")

    errors = check_release(tmp_path, "v1.2.4")

    assert "tag v1.2.4 does not match package version 1.2.3" in errors
    assert "CITATION.cff version 1.2.2 does not match 1.2.3" in errors


def test_release_check_requires_v_prefixed_tag(tmp_path: Path) -> None:
    _write_release_fixture(tmp_path)

    assert check_release(tmp_path, "1.2.3") == [
        "tag 1.2.3 does not match package version 1.2.3 (expected v1.2.3)"
    ]


def test_publish_is_manual_tag_only_and_verifies_before_upload() -> None:
    workflow = (REPO / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )

    assert "release:\n" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "default: verify" in workflow
    assert "production_confirmation:" in workflow
    assert "required_reviewers" in workflow
    assert "can_admins_bypass" in workflow
    assert "deployment-branch-policies" in workflow
    assert 'policies.get("total_count") != 1' in workflow
    assert "or len(entries) != 1" in workflow
    assert 'entries[0].get("type") != "tag"' in workflow
    assert 'allowed_policy_names != {"v*"}' in workflow
    assert "github.ref_type" in workflow
    assert 'python scripts/check_release.py "$RELEASE_TAG"' in workflow
    assert 'test "$PRODUCTION_CONFIRMATION" = "$RELEASE_TAG"' in workflow
    assert "uv sync --frozen" in workflow
    assert "Publish to TestPyPI" not in workflow
    assert workflow.index("needs: verify") < workflow.index("Publish to PyPI")

    for workflow_path in (
        REPO / ".github" / "workflows" / "ci.yml",
        REPO / ".github" / "workflows" / "publish.yml",
    ):
        smoke = workflow_path.read_text(encoding="utf-8")
        for schema_name in (
            "manifest.schema.json",
            "bundle.schema.json",
            "replay.schema.json",
        ):
            assert schema_name in smoke
        assert smoke.index("pageledger verify-run") < smoke.index("pageledger bundle")
        assert smoke.index("pageledger bundle") < smoke.index("mv \"$SOURCE\"")
        assert smoke.index("mv \"$SOURCE\"") < smoke.index("pageledger replay")
        assert smoke.index("pageledger replay") < smoke.rindex("pageledger verify-run")

    parsed = yaml.safe_load(workflow)
    jobs = parsed["jobs"]
    assert jobs["build"]["needs"] == "verify"
    assert jobs["publish-pypi"]["needs"] == "build"
    assert jobs["publish-pypi"]["environment"] == "pypi"
    assert jobs["publish-pypi"]["if"] == "inputs.target == 'pypi'"
    uploaded_name = next(
        step["with"]["name"]
        for step in jobs["build"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    downloaded_name = next(
        step["with"]["name"]
        for step in jobs["publish-pypi"]["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert uploaded_name == downloaded_name == "dist-${{ github.ref_name }}"


def test_all_github_actions_are_pinned_and_ci_has_a_frozen_lane() -> None:
    workflows = [
        (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("ci.yml", "publish.yml")
    ]

    uses = re.findall(r"^\s*- uses: [^@\s]+@([^\s#]+)", "\n".join(workflows), re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    assert "uv sync --frozen" in workflows[0]


def test_brand_archive_carries_font_license_and_no_release_version() -> None:
    archive_path = REPO / "brand" / "pageledger-logo-design.zip"
    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "brand/fonts/OFL.txt" in names
        license_text = archive.read("brand/fonts/OFL.txt").decode("utf-8")
        readme = archive.read("brand/README.txt").decode("utf-8")
        preview = archive.read("brand/index.html").decode("utf-8")

    assert "Copyright 2020 The Archivo Project Authors" in license_text
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
    assert "brand/fonts/OFL.txt" in readme
    assert not re.search(r"\b\d+\.\d+\.\d+(?:a\d+)?\b", preview)

    manifest_rules = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune brand" in manifest_rules
    assert "include THIRD_PARTY_NOTICES.md" in manifest_rules
