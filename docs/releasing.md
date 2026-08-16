# Release procedure

PageLedger publication is deliberately manual and tag-only. A GitHub release
does not trigger package publication: source verification, the exact built
artifact, TestPyPI rehearsal, and production approval come first.

## Prepare the release commit

1. Choose a version that is not already present on PyPI or TestPyPI.
2. Update `pyproject.toml`, `pageledger/_version.py`, `CITATION.cff`, the dated
   `CHANGELOG.md` heading, and the editable PageLedger entry in `uv.lock`.
3. Run the complete local release gate:

   ```bash
   uv sync --frozen --extra dev --extra pdf
   uv run --frozen --extra dev --extra pdf ruff check pageledger/ tests/ examples/ scripts/
   uv run --frozen --extra dev --extra pdf mypy pageledger/
   uv run --frozen --extra dev --extra pdf python -m pytest tests/pageledger/ -q -m "not stress"
   uv run --frozen --extra dev python scripts/check_release.py vX.Y.Z
   uv run --frozen --extra dev python -m build
   uv run --frozen --extra dev twine check dist/*
   ```

4. Review `git diff`, `git status --ignored`, and the distributions' file
   lists. No run directories, PDFs, rendered pages, credentials, planning
   notes, or local research corpora belong in the public commit or packages.
5. Merge the reviewed release commit to `main`, then create and push an
   annotated (preferably signed) `vX.Y.Z` tag pointing at that exact commit.

The release checker fails if the tag, package/runtime versions, citation,
changelog date, or committed lock disagree.

## Rehearse, then publish

1. In GitHub Actions, dispatch **Publish** from the release tag with target
   `testpypi`. The workflow recreates the committed lock, runs the suite and
   static checks, builds once, checks package contents, installs and verifies
   the exact wheel, and records distribution hashes before the TestPyPI OIDC
   job can run.
2. Install the TestPyPI artifact into a new environment and run a representative
   PageLedger workflow. Use PyPI only as the dependency fallback; the PageLedger
   package itself must come from TestPyPI.
3. Review the retained `dist-vX.Y.Z` artifact and `SHA256SUMS`. Confirm the
   repository's `testpypi` and `pypi` environments require the intended human
   approvals and branch/tag protections.
4. Dispatch the same workflow from the same tag with target `pypi`. Approve the
   protected `pypi` environment only after the TestPyPI result is accepted.
5. Create the GitHub release from that tag only after PyPI shows the expected
   files and metadata. Attach or publish the recorded SHA-256 values with the
   release notes.

PyPI files and public Git tags are effectively immutable. If any identity,
artifact, or smoke test differs, stop and prepare a new version; never move a
published tag or overwrite a release file.
