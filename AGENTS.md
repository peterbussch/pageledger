# Agent Guide

This repository is intentionally operable by AI coding agents. Everything an
agent needs to run, test, and extend PageLedger is in-repo and plain-text.

## What this is

PageLedger reads documents into text page by page and records how each page
was read, as plain files. `process` makes a *job* for one document: staged
readings (text layer, OCR, optional models), holds on pages that need a person,
review receipts and corrections, a report and exports. `run` and its sibling
commands make *runs*: one extraction adapter over the pages of some files, with
provenance, quality signals, cost, audit queues and rerun plans. It is
not an OCR engine — `docs/capabilities-and-limits.md` is the authoritative
scope list.

## Orientation

| Path | Purpose |
|---|---|
| `pageledger/` | The package: `cli.py`; jobs (`processing*.py`, `policy.py`, `contest.py`, `tiling.py`, `document_report.py`, `review_html.py`, `review_sheet.py`, `corrections.py`, `export.py`, `wording.py`); runs (`runner.py`, `classifier.py`, `routing.py`, `budget.py`, `replay.py`); sources (`image_source.py`, `iiif.py`); adapters (`adapters.py`, `vision.py`, `rapidocr_adapter.py`, `indra_adapter.py`, `alto_adapter.py`); `verify.py` |
| `schemas/` | JSON Schemas for every generated artifact — the output contract |
| `docs/` | User docs arranged by task (`docs/README.md`), per-artifact specs (`*-spec.md`), adapter protocol, `stability.md` (the 1.0 contract) |
| `docs/examples/` | Config examples: `processing.yml` (what `init` writes, for jobs) and `pageledger.yml` (for runs) |
| `examples/` | Custom adapter examples (RapidOCR, Apple Vision, Docling, Tesseract variants, a cloud-VLM skeleton) and the tutorial runner |
| `tests/pageledger/` | Test suite; fixtures under `tests/fixtures/` |
| `skills/pageledger/SKILL.md` | Claude Code skill for operating PageLedger |

## Run it

```bash
uv sync --frozen --extra dev --extra pdf && . .venv/bin/activate
printf 'first page\fsecond page\n' > sample.txt
pageledger process sample.txt --out jobs/demo
pageledger inspect jobs/demo
pageledger verify jobs/demo
pageledger init --run --out pageledger.yml
pageledger classify sample.txt --config pageledger.yml --out routes.yml --json
pageledger run sample.txt --config pageledger.yml --routes routes.yml --out runs/demo
pageledger inspect runs/demo
pageledger verify runs/demo
pageledger doctor --json
```

`--dry-run` writes a run's route map and planning artifacts without
extracting. Output directories must not already exist. The executed tutorials
are `docs/document-first-run.md` (a job) and `docs/first-run.md` (runs,
`rerun`, `compare`, `bundle`, `replay`). Commands have single-word names; the
earlier names are hidden aliases listed in `docs/cli.md`.

## Test and verify

Gate: `scripts/check.sh` (what CI's `locked` job runs); add `--all-lanes`
before pushing to also run the suite without the `pdf` extra. Packaging checks
are part of the release procedure in `docs/maintainers/releasing.md`.

Every generated artifact must validate against its schema in `schemas/`;
`tests/pageledger/test_schemas.py` enforces this. If you change an artifact
field, update the schema, the matching `docs/*-spec.md`, and the tests
together — the spec docs and runtime output must agree exactly.

Three assertions that bite during routine changes:

- A version bump touches `pyproject.toml`, `pageledger/_version.py`,
  `CITATION.cff`, the editable package entry in `uv.lock`, and the
  `--expected-version` smoke commands in `.github/workflows/{ci,publish}.yml`.
  Tests read the version from the package; `test_release.py` and
  `scripts/check_release.py` fail when those surfaces disagree.
- `test_docs_examples_smoke_without_heavy_ocr_installs` pins strings in
  README, `docs/ocr-options.md`, `MANIFEST.in`, and `examples/`, and
  `tests/pageledger/test_docs.py` checks every link and anchor under `docs/`,
  runs the README's `pageledger-tutorial` blocks; `test_reader_report.py`
  holds `docs/warnings.md` to `pageledger/wording.py`; `test_stability.py`
  fails on any removal from the 1.0 contract.
- Tesseract subprocess results are `lru_cache`d in `adapters.py`;
  `tests/pageledger/conftest.py` clears those caches around every test so
  mocked binaries don't leak between tests. Keep new subprocess caches on
  that list.

## GitHub workflow

Use GitHub's native features throughout work on this repository, including
issues, pull requests, Actions checks, review threads, and releases where they
fit the task. Read and assess existing Copilot review findings during development
and review: verify them against the code and address substantive issues. Request
or re-request a Copilot review only when Peter asks.

Before merging, read all reviews and inline comments alongside the check
results. Check again before publishing through the protected release environment.
Passing checks do not mean reviews are complete or their findings resolved.

## Constraints for changes
- Core stays dependency-light: PyYAML only; `pypdf[crypto]` behind the `[pdf]` extra.
- Adapters are thin wrappers; PageLedger owns the process around extraction,
  not extraction itself. No OCR engines, provider SDKs, or pricing catalogs
  in core.
- Record uncertainty, never silently fix it.
- `audit.md` is a rendering of `audit.json`, never a second source of truth.
- Keep claims honest: docs must not describe unimplemented behavior as
  current (design targets live in `docs/design.md`).
