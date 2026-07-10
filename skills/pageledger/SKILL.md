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
| Full config run | `pageledger run inputs/ --config pageledger.yml --out runs/a` |
| Re-extract flagged pages | `pageledger rerun runs/a --config stronger.yml --out runs/b` |
| Re-align/regrade without re-extracting | `pageledger align runs/a --schema table-v2.yml` |
| Preview re-alignment | add `--dry-run` (no run artifacts change) |
| Diff two runs | `pageledger compare-runs runs/a runs/b` (ranks only same-source, same-adapter evidence) |
| Verify ledger coherence | `pageledger verify-run runs/a` |
| Summarize a run | `pageledger inspect-run runs/a` (`--csv` for per-page rows, `--json` for scripts) |
| Write a starter config | `pageledger init-config --adapter pdf_ocr --out pageledger.yml` |
| Check environment | `pageledger doctor --json` (includes installed OCR languages) |

`--out` must be a new directory. `--config` and `--adapter` are mutually
exclusive; no config file is ever read implicitly. `pdf_ocr` needs poppler
and Tesseract installed and refuses to run if `adapter_options.lang` names
a missing language pack.

## Operating loop

1. `pageledger doctor` to check tools, then dry-run to inspect routing
   before spending anything.
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
5. `rerun` with a stronger adapter re-extracts exactly the flagged pages
   (set `run.grading.review_below_grade: C` to queue low-graded pages),
   preserving page ids and lineage; `compare-runs` ranks improvement only
   when source identity and adapter match.
6. Run `verify-run` before publishing or archiving a ledger. It checks
   cross-artifact coherence, not OCR correctness.
7. Merging parent and rerun output into a corpus is the project's call —
   present the compare-runs evidence rather than deciding silently.

## Configuration essentials

One `pageledger.yml` with `taxonomy`, `schema`, and `run` sections
(`init-config` writes it). The knobs that matter in practice live under
`run`: `adapter`, `adapter_options` (`dpi`, `lang` for pdf_ocr),
`budget` (`max_pages`, `max_tokens`, `max_usd` — preflight refuses
over-budget runs before writing anything), `retry`, `pricing`,
`grading` (`review_below_grade`, `thresholds`), and `max_rerun_depth`.
The `schema` section (columns/aliases/types/checks) is active: it is
strictly validated at load and drives the aligner for structured page
formats (`markdown_table`, `json`, `csv`; plain text is never aligned).
Custom adapters load from `module.path:Object` import strings plus
`--adapter-path DIR`.

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
  taxonomies, provider pricing catalogs, header dictionaries, CV
  heuristics, exporters (TEI/PAGE/ALTO/GIS), dashboards, and ensemble
  voting stay out of core.
- Grades are deterministic evidence summaries, never calibrated accuracy;
  do not compare grades across adapters or drop the basis label.
- Not yet implemented (do not claim otherwise): automatic page
  classification, the full `rerun_if`/`quarantine_if` policy grammar
  (`review_below_grade` is the shipped subset), and the `classify`/
  `extract`/`audit` staged commands (`align` ships).

## Where to read more

| Need | Read |
|---|---|
| Exact scope of this version | `docs/capabilities-and-limits.md` |
| All flags and config keys | `docs/cli.md` |
| What each run artifact means | `docs/artifacts.md`, `docs/*-spec.md`, `schemas/` |
| Writing an adapter | `docs/adapter-protocol.md`, `examples/*.py` |
| Choosing an engine or escalation tier | `docs/ocr-options.md`, `docs/examples/jfk-scanned-archive.md` |
| Non-English or historical documents | `docs/multilingual-ocr.md` |
| Contributing code | `AGENTS.md` (schema + spec + tests change together) |
