# Agent Guide

This repository is intentionally operable by AI coding agents. Everything an
agent needs to run, test, and extend PageLedger is in-repo and plain-text.

## What this is

PageLedger is a page-denominated run ledger for document extraction: it
routes pages, calls an extraction adapter per page, and records provenance,
quality signals, cost, audit queues, and rerun plans as plain files. It is
not an OCR engine — `docs/capabilities-and-limits.md` is the authoritative
scope list.

## Orientation

| Path | Purpose |
|---|---|
| `pageledger/` | The package: CLI/config/runner plus classifier, routing, budget, adapters, artifacts, and verification modules |
| `schemas/` | JSON Schemas for every generated artifact — the output contract |
| `docs/` | User docs and per-artifact specs (`*-spec.md`), adapter protocol |
| `docs/examples/` | Config examples (`pageledger.yml` is the recommended starting point) |
| `examples/` | Custom adapter examples (RapidOCR, Apple Vision, Docling, Tesseract variants, a cloud-VLM skeleton) and the tutorial runner |
| `tests/pageledger/` | Test suite; fixtures under `tests/fixtures/` |
| `skills/pageledger/SKILL.md` | Claude Code skill for operating PageLedger |

## Run it

```bash
uv sync --frozen --extra dev --extra pdf && . .venv/bin/activate
pageledger init-config --out pageledger.yml
printf 'first page\fsecond page\n' > sample.txt
pageledger classify sample.txt --config pageledger.yml --out routes.yml --json
pageledger run sample.txt --config pageledger.yml --routes routes.yml --out runs/demo
pageledger inspect-run runs/demo
pageledger verify-run runs/demo
pageledger doctor --json
```

`--dry-run` writes the route map and planning artifacts without extracting.
Output directories must not already exist. Neither page of this sample is
flagged, so there is nothing to rerun; the tested first run in `README.md`
flags a page and continues with `rerun` and `compare-runs`.

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
  README, `docs/ocr-options.md`, `MANIFEST.in`, and `examples/` — docs
  restructuring can fail the suite.
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
