---
name: pageledger
description: This skill should be used when the user asks to "OCR these scans", "extract text from this archive", "track what my extraction run did", "rerun the bad pages", "compare two runs", or otherwise wants document extraction with provenance, quality review, cost budgets, or reproducibility — digitization projects, archival scans, batch PDF extraction, OCR auditing. Also use it when operating or extending the pageledger CLI or writing a PageLedger adapter.
---

# PageLedger

PageLedger is a page-denominated run ledger for document extraction. It
does not extract anything itself: it routes pages to an adapter
(Tesseract, a cloud VLM, anything wrapped in the protocol), enforces
page/token/dollar budgets, and records provenance, quality signals, cost,
review queues, and rerun plans as plain files in a run directory.

Trust the version installed, not memory of an older one. `pageledger
doctor` reports the version; `docs/capabilities-and-limits.md` is the
authoritative scope list.

## Command quick reference

| Task | Command |
|---|---|
| OCR a scan, no config | `pageledger run scan.pdf --adapter pdf_ocr --out runs/a` |
| Born-digital PDF | `pageledger run doc.pdf --adapter pdf_text --out runs/a` (needs `pageledger[pdf]`) |
| Sample pages first | add `--pages "1-10,50-60"` (page ids keep source numbering) |
| Plan without extracting | add `--dry-run` |
| Classify pages into a route map | `pageledger classify inputs/ --config pageledger.yml --out routes.yml` |
| Reclassify retained run evidence | `pageledger classify --from-run runs/a --config pageledger.yml --out routes-v2.yml` |
| Full config run | `pageledger run inputs/ --config pageledger.yml --out runs/a` |
| Execute classified/reviewed routes | add `--routes route-map.yml` to a config run |
| Re-extract flagged pages | `pageledger rerun runs/a --config stronger.yml --out runs/b` |
| Re-align/regrade without re-extracting | `pageledger align runs/a --schema table-v2.yml` |
| Preview re-alignment | add `--dry-run` (no run artifacts change) |
| Diff two runs | `pageledger compare-runs runs/a runs/b` (ranks only same-source, same-adapter evidence) |
| Verify ledger coherence | `pageledger verify-run runs/a` |
| Create a portable verified bundle | `pageledger bundle runs/a --out bundles/a --json` |
| Replay a relocated bundle | `pageledger replay bundles/a --out runs/replayed --json` |
| Summarize a run | `pageledger inspect-run runs/a` (`--csv` for per-page rows, `--json` for scripts) |
| Write a starter config | `pageledger init-config --adapter pdf_ocr --out pageledger.yml` |
| Check environment | `pageledger doctor --json` (includes installed OCR languages) |

`run --out` and `rerun --out` must name a new directory; `classify --out` and
`init-config --out` name files. `--config` and the `run --adapter` shortcut are
mutually exclusive; no config file is ever read implicitly. `pdf_ocr` needs poppler
and Tesseract installed and refuses to run if `adapter_options.lang` names
a missing language pack.

`--routes` requires `--config` and a complete map covering every input page.
It executes decisions from `pageledger classify`, a human, or an external
classifier after validating complete coverage and adapter action support.

`bundle` and `replay` accept only their documented flags. Bundle output and
replay output must be new directories. `replay --adapter-path` is a locally
trusted import directory and must not be inside the bundle. Replay preserves
source bytes and records profile, extractor, and raw-comparison evidence; it
does not install environments or adapter/model materials.

## Operating loop

1. `pageledger doctor` to check tools. When pages need different actions,
   run `classify`, review its route map and evidence sidecar, then pass the
   map unchanged to `run --routes`. Classification is explicit; `run` does
   not invoke it automatically.
2. Run cheap extraction first (`pdf_text` or `pdf_ocr`).
3. Read the evidence: `inspect-run` (grade distribution, records
   normalized), then `quality.jsonl` warnings and grades (`empty_text`,
   `low_confidence`, `historical_orthography`, `instruction_echo`,
   `output_inflation`, and others; grades A–F with
   a basis label — `A (signals)` is weaker evidence than `A (schema)` and
   grades only compare within one adapter), `cost.json` (check
   `cost_basis` — derived rates are not billed spend), and `audit.json`'s
   review queue.
4. For tabular work, declare a `schema` section (columns, aliases,
   arithmetic checks) so structured adapter output lands in `normalized/`
   and grades gain column/check evidence. Iterating on aliases is free:
   `pageledger align runs/a --schema v2.yml --dry-run` previews the change;
   remove `--dry-run` to re-derive records and grades from raw/.
5. `rerun` re-extracts exactly the flagged pages. Either supply a stronger
   plain `run.adapter` config or define `run.adapter_order`; chain entry N is
   used by rerun generation N. Exhausted chains leave pages in review.
   `compare-runs` ranks improvement only when source identity and adapter
   match.
6. Run `verify-run` before publishing or archiving a ledger. It checks
   cross-artifact coherence, not OCR correctness.
7. For transport, run `bundle` only after verification, relocate or remove
   the original source, then run `replay` and `verify-run` on the new run.
   Treat `exact`, `evidence_compared`, and `deterministic_mismatch` as evidence
   outcomes, not accuracy claims.
8. Merging parent and rerun output into a corpus is the project's call —
   present the compare-runs evidence rather than deciding silently.

## Configuration essentials

One `pageledger.yml` with optional `classify`, plus `taxonomy`, `schema`, and `run` sections
(`init-config` writes it). The knobs that matter in practice live under
`run`: either `adapter`/`adapter_options` or generation-indexed
`adapter_order`; `budget` (`max_pages`, `max_tokens`, `max_usd` caps plus
capless `warn_pages`, `warn_tokens`, `warn_usd` alerts), `retry`, `pricing`,
`on_page_error`, `max_consecutive_failures`, `grading`
(`review_below_grade`, `thresholds`), `rerun_if`, `quarantine_if`, and
`max_rerun_depth`.
The `schema` section (columns/aliases/types/checks) is active: it is
strictly validated at load and drives the aligner for structured page
formats (`markdown_table`, `json`, `csv`; plain text is never aligned).
Custom adapters and classifier hooks load from `module.path:Object` import
strings plus `--adapter-path DIR`. The built-in classifier emits only the
structural types `blank`, `sparse`, `prose`, `table_likely`, and `unknown`;
domain types belong in a hook.

## Boundaries the design enforces

- Adapters are thin wrappers; PageLedger owns the process around
  extraction, never extraction itself. Do not present it as a replacement
  for Docling, Marker, Surya, olmOCR, Tesseract, or API VLMs.
- Record uncertainty; never silently correct it. Uncertain output gets
  flagged, reviewed, or rerun. Transforms that rewrite text (like the
  pre-reform orthography normalizer in `examples/`) run as their own
  recorded run so the original remains evidence.
- Confidence values are evidence, not calibrated probability.
- Core stays PyYAML-only (`pypdf` behind the `[pdf]` extra). Domain
  taxonomies, provider pricing catalogs, header dictionaries, CV heuristics,
  exporters (TEI/PAGE/ALTO/GIS), dashboards, and ensemble
  voting stay out of core.
- Grades are deterministic evidence summaries, never calibrated accuracy;
  do not compare grades across adapters or drop the basis label.
- Still out of scope: classification invoked implicitly inside `run`, region-
  level routing, same-run adapter fallback, environment installation, adapter
  or model bundling, signatures, and cloud identity. Explicit `classify`,
  generation-indexed rerun chains, and the verified `bundle`/`replay` lifecycle
  ship in 0.4.0.

## Where to read more

| Need | Read |
|---|---|
| Exact scope of this version | `docs/capabilities-and-limits.md` |
| Classifier signals, hooks, and evidence | `docs/classifier.md` |
| All flags and config keys | `docs/cli.md` |
| What each run artifact means | `docs/artifacts.md`, `docs/*-spec.md`, `schemas/` |
| Writing an adapter | `docs/adapter-protocol.md`, `examples/*.py` |
| Choosing an engine or escalation tier | `docs/ocr-options.md`, `docs/examples/jfk-scanned-archive.md` |
| Non-English or historical documents | `docs/multilingual-ocr.md` |
| Contributing code | `AGENTS.md` (schema + spec + tests change together) |
