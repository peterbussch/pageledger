# Provenance JSONL Specification

`provenance.jsonl` records the evidence trail for a PageLedger run. The run
manifest points to this file; each line records one extractor activity and the
metadata needed to understand or reproduce it.

## Minimal Per-Page Line

```json
{
  "schema_version": "0.1",
  "run_id": "run-20260619-001",
  "page_id": "doc_0001_page_0002",
  "source": {
    "path": "scans/volume_01.pdf",
    "page_number": 2,
    "sha256": "abc123"
  },
  "route": {
    "type": "table_data",
    "action": "vlm_table",
    "route_confidence": 0.91
  },
  "extractor": {
    "adapter": "pageledger.adapters.openai_compatible",
    "adapter_version": "0.1.0",
    "model": "qwen/qwen3-vl-235b-a22b-instruct",
    "prompt_hash": "def456",
    "deterministic": false,
    "input_types": ["pdf"],
    "output_types": ["markdown_table"],
    "capabilities": ["ocr", "tables", "cloud"]
  },
  "result": {
    "format": "markdown_table",
    "confidence": 0.84,
    "warnings": ["missing_optional_column"],
    "raw_artifact": "raw/doc_0001_page_0002.markdown_table"
  },
  "usage": {
    "pages": 1,
    "tokens": 2884,
    "compute_seconds": null,
    "cost_usd": 0.04
  },
  "metrics": {
    "pages": 1,
    "tokens": 2884,
    "compute_seconds": null,
    "cost_usd": 0.04
  },
  "extraction_seconds": 21.833,
  "timestamp": "2026-06-19T19:34:00Z"
}
```

## Optional Per-Record Links

Normalized records should preserve links back to provenance lines:

```json
{
  "record_id": "run-20260619-001:doc_0001_page_0002:row_0017",
  "place_name": "Example",
  "population_total": 1234,
  "produced_by": {
    "run_id": "run-20260619-001",
    "page_id": "doc_0001_page_0002",
    "raw_artifact": "raw/doc_0001_page_0002.markdown_table"
  }
}
```

## Required Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Provenance schema version. |
| `run_id` | string | Run identifier from `manifest.json`. |
| `page_id` | string | Stable page or region id. |
| `source` | object | Source path, page number, and source checksum. |
| `route` | object | Page type, action, and route confidence. |
| `extractor` | object | Adapter, adapter version, model, prompt hash, determinism flag, and adapter capability metadata. |
| `result` | object | Output format, confidence, warnings, and raw artifact path. |
| `usage` | object | Canonical usage fields: `pages`, `tokens`, `compute_seconds`, and `cost_usd`. |
| `metrics` | object | Flat copy of `usage` for analytical workflows. |
| `extraction_seconds` | number or null | Wall-clock seconds for the successful extraction attempt, measured by the runner (independent of adapter-reported `compute_seconds`). |
| `timestamp` | ISO timestamp | Extraction time in UTC. |

## Design Notes

- `record_id` should join `run_id`, `page_id`, and a row or record index with
  colon separators.
- Confidence values may be heuristic. Preserve raw route, extractor, and
  alignment confidence values rather than collapsing them too early.
- `usage.pages` is required. Optional usage fields should serialize unknown
  values as JSON `null`, not disappear from generated artifacts.
- `prompt_hash` is required whenever a prompt influenced output.
- `deterministic` should be `false` unless the adapter can actually guarantee
  stable output for the same input and config.
- `metrics` is a reference copy of `usage` for analytical workflows. It
  shares the same keys and values as `usage` in the current alpha; future
  versions may diverge `metrics` to exclude cost or add derived fields.
- The JSON Schema for this artifact is at `schemas/provenance-line.schema.json`.
- Schema validation tests are in `tests/pageledger/test_schemas.py`.

## Companion Artifact: quality.jsonl

`quality.jsonl` records per-page diagnostic signals alongside provenance. It is
not a calibrated accuracy score. Fields:

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Quality schema version. `"0.1"`. |
| `page_id` | string | ✅ | no | Links to `provenance.jsonl` `page_id`. |
| `page_number` | integer | ✅ | no | One-based page number. |
| `adapter` | string | ✅ | no | Adapter name for diagnostics attribution. |
| `character_count` | integer | ✅ | no | Total characters in extractor output. |
| `word_count` | integer | ✅ | no | Regex word count (`\w+`). |
| `confidence` | number or null | ✅ | yes | Adapter-reported page confidence, 0–1. Evidence, not calibrated probability. |
| `confidence_detail` | object or null | ✅ | yes | Engine-native confidence evidence, adapter-defined shape. `pdf_ocr` reports Tesseract per-word statistics: `scale`, `word_count`, `mean`, `min`, `below_60_count`, `below_60_ratio` (0–100 scale). |
| `warnings` | array of strings | ✅ | no | Quality warnings (see taxonomy below). |
| `text_quality` | object | ✅ | no | Sub-metrics (see below). |
| `embedded_text_comparison` | object | ❌ | yes | Comparison with PDF embedded text layer. Null for non-PDF sources. |

### text_quality Fields

| Field | Type | Meaning |
|---|---|---|
| `replacement_character_count` | integer | Count of `\ufffd` replacement characters. |
| `control_character_count` | integer | Non-whitespace control characters below U+0020. |
| `suspicious_symbol_count` | integer | Characters flagged as suspicious (non-ASCII, non-alphanumeric, non-common punctuation). |
| `suspicious_symbol_ratio` | number (0–1) | `suspicious_symbol_count / character_count`. |
| `alpha_token_count` | integer | Alphabetic tokens on the page. |
| `mean_token_length` | number or null | Mean alphabetic token length; null with no tokens. Fragment noise collapses toward 1, tested prose sits above 4. |
| `short_token_ratio` | number (0–1) or null | Share of alphabetic tokens with 1–2 characters. |
| `prereform_letter_count` | integer | Cyrillic letters abolished by the 1918 Russian reform (ѣ, ѳ, ѵ). Modern Ukrainian/Belarusian і is deliberately not counted. |
| `terminal_hard_sign_count` | integer | Word-final hard signs (ъ) — mandatory before 1918, absent from modern Russian. This is the pre-reform signal that survives OCR: engines trained on modern text destroy the abolished letters but keep ъ. |

### Warning Taxonomy

| Warning | Trigger |
|---|---|
| `empty_text` | `character_count == 0`. |
| `short_text` | `character_count < 10`. |
| `replacement_characters` | `replacement_character_count > 0`. |
| `control_characters` | `control_character_count > 0`. |
| `suspicious_symbol_density` | `suspicious_symbol_ratio >= 0.03` AND `suspicious_symbol_count >= 5`. |
| `fragmented_text` | `mean_token_length < 3.0` AND `alpha_token_count >= 20`. Catches OCR fragment noise; does not catch word-level misrecognition. |
| `suspicious_embedded_text_delta` | PDF embedded text character ratio < 0.5 or > 1.8, when embedded text is available and adapter does not report `embedded_text` capability. |
| `historical_orthography` | `prereform_letter_count >= 2`, OR `terminal_hard_sign_count >= 2` at a density of ≥1 per 100 alphabetic tokens over ≥20 tokens. The page is pre-1918 Russian orthography and an OCR model trained on modern text is probably mismatched. Measured on an 1850 gubernia review: 21 terminal ъ per 100 tokens vs 0.00 in modern text. |
| `low_confidence` | `confidence_detail.below_60_ratio >= 0.25` over ≥10 words. A quarter of the words under engine confidence 60 marks the page for review; a mean can hide one illegible paragraph on an otherwise clean page. |

The JSON Schema is at `schemas/quality-line.schema.json`.
