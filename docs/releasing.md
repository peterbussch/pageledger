# Release procedure

PageLedger publication is deliberately manual and tag-only. A GitHub release
does not trigger package publication: source verification, inspection of the
exact built artifact, and a protected production approval come first. The
current repository does not assume that a PageLedger TestPyPI project exists.

## Prepare the release commit

1. Choose a version that is not already present on PyPI.
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
   annotated (preferably signed) version tag such as `vX.Y.Z` or `vX.Y.ZaN`
   pointing at that exact commit.

The release checker fails if the tag, package/runtime versions, citation,
changelog date, or committed lock disagree.

## Verify, then publish

1. In GitHub Actions, dispatch **Publish** from the release tag with the default
   target, `verify`. The workflow recreates the committed lock, runs the suite
   and static checks, builds once, checks package contents, installs and verifies
   the exact wheel, records distribution hashes, and retains `dist-vX.Y.Z`.
2. Download that artifact, inspect `SHA256SUMS` and both archive inventories,
   then install its wheel into a new environment and run a representative
   PageLedger workflow. The verification dispatch cannot upload a package.
3. Before production, configure the repository's `pypi` environment with all
   three controls below. The workflow checks them through the GitHub API and
   refuses to publish if any control is absent:

   - at least one required reviewer;
   - administrator bypass disabled;
   - the sole custom deployment pattern is `v*`; the environment job enforces
     that pattern against the verified tag ref.

4. After explicit release-owner approval, dispatch **Publish** again from the
   same tag with target `pypi` and type that exact tag into
   `production_confirmation`. This production run repeats every source gate,
   builds once, records and retains the hashes, and passes that same run's
   artifact to the protected `pypi` job. Approve the environment only after
   inspecting the run and its retained artifact.
5. Create the GitHub release from that tag only after PyPI shows the expected
   files and metadata. Attach or publish the production run's recorded SHA-256
   values with the release notes.

If a PageLedger TestPyPI project and trusted publisher are configured later,
add a rehearsal lane that promotes the same retained artifact by digest. Do not
claim a TestPyPI gate or rebuild between rehearsal and production.

PyPI files and public Git tags are effectively immutable. If any identity,
artifact, or smoke test differs, stop and prepare a new version; never move a
published tag or overwrite a release file.
