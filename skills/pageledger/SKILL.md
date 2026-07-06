---
name: pageledger
description: Use when planning, running, or reviewing auditable OCR/VLM extraction workflows with page routing, quality/cost controls, provenance, audit queues, and rerun planning.
---

# PageLedger

Use this skill when the user wants to build or operate a document extraction
pipeline where OCR/VLM output must be auditable, rerunnable, and reviewable.

PageLedger is an alpha package, not a finalized stable library. Treat
the repository docs as design direction. The implementation in
`pageledger/` currently supports dry-run artifact generation, the deterministic
`run.adapter: text` path for UTF-8 fixtures, optional `run.adapter: pdf_text`
for born-digital PDFs through `pageledger[pdf]`, custom adapter import strings,
and `pageledger doctor` diagnostics for optional PDF/OCR/cloud tooling.

## Shape

PageLedger is a small control plane around extractors:

- Route pages before extraction.
- Control the run and decide what needs review or rerun.
- (Design target, not yet implemented: align extractor output to a declared
  schema.)

Keep it boring: one config, one command, one run directory, plain files.

**The canonical unit is the page.** It is the only unit every backend shares
(cloud OCR bills per page; tokens exist only on model paths; self-hosted engines
have no dollar cost), so PageLedger routes, budgets, and audits in pages — which
is what makes runs comparable and reproducible across providers. Adapters must
report `usage.pages`; `tokens`, `compute_seconds`, and `cost_usd` are optional.
Dollar cost is derived by PageLedger (adapter passthrough → configured unit
rates → `null`), never required of the adapter. Budgets cap on pages, tokens, or
dollars — whichever the config sets.

## Operating Principles

- Do not present PageLedger as a replacement for Docling, Marker, Surya,
  olmOCR, OCR-D, Tesseract, or API VLMs. It should orchestrate and audit them.
- Keep v0.1 centered on `pageledger run`; staged commands are future advanced
  interfaces.
- Keep taxonomies, schemas, pricing, and quality rules in user config.
- Preserve uncertainty. Flag, review, and rerun uncertain output; do not
  silently correct it.
- Treat confidence as evidence, not calibrated probability, unless calibration
  is explicitly proven.
- Prefer plain artifacts: YAML config, JSON/JSONL outputs, Markdown rendering,
  and greppable logs.
- For digital humanities use, favor provenance, citation, reviewability, and
  reproducibility over clever extraction tricks.

## Minimal Loop (0.1.0 alpha)

1. Read current PageLedger docs and examples.
2. Define or inspect `pageledger.yml`.
3. Run `pageledger doctor` to check optional PDF/OCR/cloud dependencies.
4. Dry-run routing and inspect the route map.
5. Run extraction through built-in (`text`, `pdf_text`) or custom adapters.
6. Inspect run artifacts: provenance, quality diagnostics, cost rollup
   (check `cost_basis` — derived rates are not billed spend), run log,
   audit queues, and rerun manifest.
7. Re-extract flagged pages with a stronger adapter:
   `pageledger rerun RUN_DIR --config stronger.yml --out NEW_DIR`.
8. Compare the runs: `pageledger compare-runs RUN_DIR NEW_DIR` — warnings
   resolved/introduced, char deltas, cost.

Future stages (not yet implemented in the alpha):
- Schema alignment execution.
- Audit grading.
- Conditional rerun policies (`rerun_if`).

## Keep Out Of Core

- Domain taxonomies such as Soviet census page types.
- Hardcoded provider pricing.
- Hardcoded column/header dictionaries.
- Computer-vision fallback heuristics.
- GIS, TEI/PAGE/ALTO/DDI/DataCite exporters.
- Web UI, dashboards, or ensemble voting.

## Repo Context

Relevant PageLedger modules:

- `pageledger/cli.py`
- `pageledger/config.py`
- `pageledger/runner.py`
- `pageledger/adapters.py`
- `pageledger/artifacts.py`
- `pageledger/doctor.py`
- `docs/adapter-protocol.md`
- `docs/run-manifest-spec.md`
- `docs/route-map-spec.md`
- `docs/provenance-spec.md`
- `docs/rerun-manifest-spec.md`

Keep implementation aligned with these files and avoid domain-specific
assumptions in core.
