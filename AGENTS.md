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
| `examples/` | Custom adapter examples (Tesseract, OCRmyPDF preprocessing, cloud-VLM skeleton) |
| `tests/pageledger/` | Test suite; fixtures under `tests/fixtures/` |
| `skills/pageledger/SKILL.md` | Claude Code skill for operating PageLedger |

## Run it

```bash
pip install -e ".[dev,pdf]"
pageledger init-config --out pageledger.yml
printf 'first page\fsecond page\n' > sample.txt
pageledger classify sample.txt --config pageledger.yml --out routes.yml --json
pageledger run sample.txt --config pageledger.yml --out runs/demo --json
pageledger run sample.txt --config pageledger.yml --routes routes.yml --out runs/routed
pageledger inspect-run runs/demo
pageledger rerun runs/demo --config pageledger.yml --out runs/demo-2  # if flagged pages exist
pageledger compare-runs runs/demo runs/demo-2
pageledger doctor --json
```

`--dry-run` writes the route map and planning artifacts without extracting.
Output directories must not already exist.

## Test and verify

```bash
python -m pytest tests/pageledger/ -q          # full suite
ruff check pageledger/ tests/ examples/        # lint (CI enforces)
python -m build && twine check dist/*          # packaging
```

Every generated artifact must validate against its schema in `schemas/`;
`tests/pageledger/test_schemas.py` enforces this. If you change an artifact
field, update the schema, the matching `docs/*-spec.md`, and the tests
together — the spec docs and runtime output must agree exactly.

Three assertions that bite during routine changes:

- The release version is pinned in `test_dry_run.py`
  (`test_package_exports_release_version`) — bump it with
  `pyproject.toml`, `pageledger/__init__.py`, `CITATION.cff`, and the editable
  package entry in `uv.lock`.
- `test_docs_examples_smoke_without_heavy_ocr_installs` pins strings in
  README, `docs/ocr-options.md`, `MANIFEST.in`, and `examples/` — docs
  restructuring can fail the suite.
- Tesseract subprocess results are `lru_cache`d in `adapters.py`;
  `tests/pageledger/conftest.py` clears those caches around every test so
  mocked binaries don't leak between tests. Keep new subprocess caches on
  that list.

## Constraints for changes

- Core stays dependency-light: PyYAML only; `pypdf` behind the `[pdf]` extra.
- Adapters are thin wrappers; PageLedger owns the process around extraction,
  not extraction itself. No OCR engines, provider SDKs, or pricing catalogs
  in core.
- Record uncertainty, never silently fix it.
- `audit.md` is a rendering of `audit.json`, never a second source of truth.
- Keep claims honest: docs must not describe unimplemented behavior as
  current (design targets live in `docs/design.md`).
