# Agent Guide

This repository is intentionally operable by AI coding agents. Everything an
agent needs to run, test, and extend PageLedger is in-repo and plain-text.

## What this is

PageLedger is a page-denominated run ledger for document extraction: it
routes pages, calls an extraction adapter per page, and records provenance,
quality signals, cost, audit queues, and rerun plans as plain files. It is
not an OCR engine — see `README.md` for scope and non-goals.

## Orientation

| Path | Purpose |
|---|---|
| `pageledger/` | The package: `cli.py`, `config.py`, `runner.py`, `adapters.py`, `artifacts.py`, `doctor.py` |
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
pageledger run sample.txt --config pageledger.yml --out runs/demo --json
pageledger inspect-run runs/demo
pageledger rerun runs/demo --config pageledger.yml --out runs/demo-2  # if flagged pages exist
pageledger compare-runs runs/demo runs/demo-2
pageledger doctor --json
```

`--dry-run` writes the route map and planning artifacts without extracting.
Output directories must not already exist.

## Test and verify

```bash
python -m pytest tests/pageledger/ -q   # full suite
python -m build && twine check dist/*   # packaging
```

Every generated artifact must validate against its schema in `schemas/`;
`tests/pageledger/test_schemas.py` enforces this. If you change an artifact
field, update the schema, the matching `docs/*-spec.md`, and the tests
together — the spec docs and runtime output must agree exactly.

## Constraints for changes

- Core stays dependency-light: PyYAML only; `pypdf` behind the `[pdf]` extra.
- Adapters are thin wrappers; PageLedger owns the process around extraction,
  not extraction itself. No OCR engines, provider SDKs, or pricing catalogs
  in core.
- Record uncertainty, never silently fix it.
- `audit.md` is a rendering of `audit.json`, never a second source of truth.
- Keep claims honest: docs must not describe unimplemented behavior as
  current (design targets live in `docs/design.md`).
