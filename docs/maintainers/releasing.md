# Release procedure

Publish from an annotated version tag after reviewing and testing its source
and built packages. The manual **Publish** workflow verifies the release;
uploading to PyPI also requires approval through the `pypi` environment.

## Prepare the release commit

1. Choose a version that is not already present on PyPI.
2. Update `pyproject.toml`, `pageledger/_version.py`, `CITATION.cff`, the dated
   `CHANGELOG.md` heading, and the editable PageLedger entry in `uv.lock`.
   Update version assertions, workflow smoke commands and active documentation
   examples too. Artifact schema versions change only when their contracts do.
3. Run the complete local source gate:

   ```bash
   uv sync --frozen --extra dev --extra pdf
   uv run --frozen --extra dev --extra pdf ruff check pageledger/ tests/ examples/ scripts/
   uv run --frozen --extra dev --extra pdf mypy pageledger/
   uv run --frozen --extra dev --extra pdf python -m pytest tests/pageledger/ -q -m "not stress"
   uv run --frozen --extra dev python scripts/check_release.py vX.Y.Z
   ```

4. Review `git diff` and `git status --ignored`. No run directories, PDFs,
   rendered pages, credentials, planning notes, or local research corpora
   belong in the public commit. Commit the reviewed source and record its full
   SHA.
5. Build wheel and sdist once from that exact clean source into a fresh `dist/`,
   run `twine check`, and inspect both archive inventories. The sdist must carry
   both maintained first-run tutorials, the reader-trial and validation
   reports, performance and release documentation, and both tutorial helpers.
   It must exclude local planning state, proposals and historical execution
   reports. The wheel must carry every schema in `schemas/`.
   Run `python scripts/check_distributions.py dist` for the shared archive checks.
6. Install the exact wheel into a fresh environment and, from outside the
   checkout with `PYTHONPATH` cleared, run each maintained reader journey:

   ```bash
   PYTHONPATH= /path/to/wheel-venv/bin/python /absolute/checkout/examples/run_first_run.py \
     --document /absolute/checkout/docs/document-first-run.md \
     --work-dir /fresh/scratch/document-first-run \
     --python /path/to/wheel-venv/bin/python \
     --expected-version X.Y.Z \
     --forbid-import-root /absolute/checkout
   ```

   Run the same helper with `docs/first-run.md` and a different fresh scratch
   directory too. Record the imported module path/version, saved-response
   recovery, review checks, selected-page rerun, and relocated replay results.
   Repeat both tutorials with the exact sdist installed in a second fresh
   environment. Neither tutorial needs an OCR engine or network service.
7. Record SHA-256 hashes for both distributions alongside the source SHA and
   installed-package smoke results.
8. Fast-forward or merge the reviewed release commit to `main`, then create and push an
   annotated (preferably signed) version tag such as `vX.Y.Z` or `vX.Y.ZaN`
   pointing at that exact commit.

The release checker fails if the tag, package/runtime versions, citation,
changelog date, or committed lock disagree.

Use a pull request for the release. Before merging, read its reviews and inline
comments, including Copilot's findings, as well as the check results. Confirm
that substantive findings are fixed or have a documented technical response.
Request a fresh Copilot review after fixes when available, and wait for pending
reviews. Check the review threads again immediately before approving publication;
a review can finish after CI or after the merge. A green check list does not
replace reading the reviews.

## Verify, then publish

1. In GitHub Actions, dispatch **Publish** from the release tag with the default
   target, `verify`. The workflow recreates the committed lock, runs the suite
   and static checks, builds once, checks the wheel schema and sdist documentation
   inventories, runs both maintained tutorials against the wheel and sdist
   outside the checkout, records distribution hashes, and retains
   `dist-vX.Y.Z` for 14 days.
2. Download that artifact, inspect `SHA256SUMS` and both archive inventories,
   then install its wheel into a new environment and run a representative
   PageLedger workflow. The verification dispatch cannot upload a package.
3. Before production, configure the repository's `pypi` environment with all
   three controls below. The workflow checks them through the GitHub API and
   refuses to publish if any control is absent:

   - at least one required reviewer;
   - administrator bypass disabled;
   - the sole custom deployment policy is the `v*` tag pattern.

4. After explicit release-owner approval, dispatch **Publish** again from the
   same tag with target `pypi` and type that exact tag into
   `production_confirmation`. This production run repeats every source gate,
   builds once, records and retains the hashes, and passes that same run's
   artifact to the protected `pypi` job. Approve the environment only after
   inspecting the run and its retained artifact.
5. Verify the public PyPI version, filenames, metadata, and hashes independently.
   Record public status separately as `not published`, `PyPI verified`, and
   `GitHub release verified`; neither a local build nor an uploaded Actions
   artifact is public publication evidence. Create the GitHub release from that
   tag only after PyPI shows the expected files and metadata. Attach or publish
   the production run's recorded SHA-256 values with the release notes, then
   verify the release page and tag target.

PyPI files and public Git tags are effectively immutable. If any identity,
artifact, or smoke test differs, stop and prepare a new version; never move a
published tag or overwrite a release file.
