# Provenance JSONL specification

`provenance.jsonl` records the evidence trail for a PageLedger run. The run
manifest points to this file; each line records one extractor activity and the
metadata needed to understand and reconstruct the recorded method.

## Minimal per-page line

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
    "raw_artifact": "raw/doc_0001_page_0002.markdown_table",
    "raw_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
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
  "cost": {
    "usd": 0.04,
    "basis": "adapter_reported"
  },
  "extraction_seconds": 21.833,
  "timestamp": "2026-06-19T19:34:00Z"
}
```

## Optional per-record links

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

## Required fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Provenance schema version. |
| `run_id` | string | Run identifier from `manifest.json`. |
| `page_id` | string | Stable page or region id. |
| `source` | object | Source path, page number, and source checksum. |
| `route` | object | Page type, action, and route confidence. |
| `extractor` | object | Adapter, adapter version, model, prompt hash, determinism flag, and adapter capability metadata. |
| `result` | object | Output format, confidence, warnings, raw artifact path, and (for current writers) the exact raw artifact SHA-256. |
| `usage` | object | Canonical usage fields: `pages`, `tokens`, `compute_seconds`, and `cost_usd`. |
| `metrics` | object | Flat copy of `usage` for analytical workflows. |
| `cost` | object | Optional PageLedger-resolved per-page cost: `usd` plus `basis` (`adapter_reported`, `configured_rate`, or null). |
| `extraction_seconds` | number or null | Wall-clock seconds for the successful extraction attempt, measured by the runner (independent of adapter-reported `compute_seconds`). |
| `timestamp` | ISO timestamp | Extraction time in UTC. |

## Design notes

- `record_id` should join `run_id`, `page_id`, and a row or record index with
  colon separators.
- Confidence values may be heuristic. Preserve raw route, extractor, and
  alignment confidence values rather than collapsing them too early.
- `usage.pages` is required. Optional usage fields should serialize unknown
  values as JSON `null`, not disappear from generated artifacts.
- `prompt_hash` is required whenever a prompt influenced output.
- `deterministic` should be `false` unless the adapter can actually guarantee
  stable output for the same input and config.
- `usage` is the authoritative adapter-facing record. `metrics` is retained in
  schema version `"0.1"` as a compatibility copy for spreadsheet and JSONL
  analysis workflows that flatten page-level rows. If these fields diverge in a
  future artifact schema, the schema version must change.
- `usage.cost_usd` remains adapter-reported evidence. `cost.usd` is the value
  PageLedger actually uses after applying adapter-reported cost first and then
  configured unit rates. Missing token usage never becomes a known zero cost.
- `result.raw_sha256` is optional for compatibility with older schema-0.1
  ledgers. Current writers always emit it. `verify-run` fails if current raw
  bytes differ and warns when legacy provenance has no digest. This detects
  accidental or local modification; it is not authenticity without an
  externally trusted or signed manifest/provenance set.
- The JSON Schema for this artifact is at `schemas/provenance-line.schema.json`.
- Schema validation tests are in `tests/pageledger/test_schemas.py`.

## Companion artifact: quality.jsonl

`quality.jsonl` records per-page diagnostic signals alongside provenance. It is
not a calibrated accuracy score. Fields:

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Quality schema version. `"0.1"`. |
| `page_id` | string | ✅ | no | Links to `provenance.jsonl` `page_id`. |
| `page_number` | integer | ✅ | no | One-based page number. |
| `adapter` | string | ✅ | no | Adapter name for diagnostics attribution. |
| `character_count` | integer | ✅ | no | Total characters in extractor output. |
| `word_count` | integer | ✅ | no | Count of Unicode letter tokens; combining marks remain attached to their base-letter token. |
| `confidence` | number or null | ❌ | yes | Adapter-reported page confidence, 0–1. Emitted by current runs; optional so original 0.1 lines remain valid. |
| `confidence_detail` | object or null | ❌ | yes | Engine-native confidence evidence. Emitted by current runs; optional for original 0.1 compatibility. |
| `warnings` | array of strings | ✅ | no | Adapter-native warnings plus PageLedger-derived quality warnings (see taxonomy below). Adapter warnings are also retained in the provenance result. |
| `text_quality` | object | ✅ | no | Sub-metrics (see below). |
| `embedded_text_comparison` | object | ❌ | yes | Comparison with PDF embedded text layer. Null for non-PDF sources. |
| `output_integrity` | object | ❌ | no | Conservative chat-template-marker and parent-rerun size evidence. Present on new 0.1.4 quality lines; optional so older lines remain valid. |
| `grade` | string | ❌ | no | `A`–`F`. Emitted by current runs; absent from pre-0.1.3 lines. |
| `grade_basis` | string | ❌ | no | `signals_only` or `schema_aware`. Emitted by current runs; absent from pre-0.1.3 lines. |
| `grade_detail` | object | ❌ | no | Grade evidence detail. Emitted by current runs; absent from pre-0.1.3 lines. |

### Grade bands

Grades combine two axes, taking the worst letter of the two:

- **Signals axis**: worst of the confidence band (defaults: A ≥ 0.90,
  B ≥ 0.80, C ≥ 0.70, D ≥ 0.55, else F; skipped when the adapter reports
  no confidence) and the warning-count band (0 → A, 1 → B, 2 → C, 3+ → D).
  `empty_text` forces F.
- **Schema axis** (only when a normalized record exists): worst of the
  required-column-coverage band (A ≥ 1.0, B ≥ 0.9, C ≥ 0.7, else D; F when
  parsing failed, no rows, or all required columns missing) and the
  arithmetic-pass-rate band (A ≥ 0.98, B ≥ 0.90, C ≥ 0.75, else D).
  Coercion errors or recorded structural loss cap the axis at B without
  lifting an already-worse grade.

Confidence/coverage/pass-rate thresholds are overridable under
`run.grading.thresholds`. The schema `quality` section adds floors:
coverage below `minimum_required_column_coverage` forces the schema axis
to F, and page confidence under `low_confidence_threshold` caps the final
grade at C. The structured-format prose heuristics
(`suspicious_symbol_density`, `fragmented_text`, `joined_text`) do not fire on
`markdown_table`/`json`/`csv` pages: pipes and braces are construction,
not garble.

### text_quality fields

| Field | Type | Meaning |
|---|---|---|
| `replacement_character_count` | integer | Count of `\ufffd` replacement characters. |
| `control_character_count` | integer | Non-whitespace control characters below U+0020. |
| `suspicious_symbol_count` | integer | Explicit extraction-garble markers plus Unicode symbols outside letter, mark, number, punctuation, and separator categories, except documented common typography. |
| `suspicious_symbol_ratio` | number (0–1) | `suspicious_symbol_count / character_count`. |
| `alpha_token_count` | integer | Unicode letter tokens on the page; combining marks remain attached to their base-letter token. |
| `mean_token_length` | number or null | Mean Unicode letter-plus-mark token length; null with no tokens. The `<3` warning boundary is a fragment-noise heuristic, not a cross-language quality score. |
| `max_token_length` | integer | Maximum Unicode letter-plus-mark token length. |
| `short_token_ratio` | number (0–1) or null | Share of Unicode letter-plus-mark tokens with 1–2 code points. |
| `whitespace_character_ratio` | number (0–1) | Share of output characters for which `str.isspace()` is true. |
| `latin_letter_ratio` | number (0–1) | Share of Unicode letters identified as Latin-script letters; used only as a conservative joined-text guard. |
| `prereform_letter_count` | integer | Cyrillic letters abolished by the 1918 Russian reform (ѣ, ѳ, ѵ). Modern Ukrainian/Belarusian і is deliberately not counted. |
| `terminal_hard_sign_count` | integer | Word-final hard signs (ъ): mandatory before 1918, absent from modern Russian. This is the pre-reform signal that survives OCR: engines trained on modern text destroy the abolished letters but keep ъ. |

### Warning taxonomy

| Warning | Trigger |
|---|---|
| `empty_text` | Output contains no Unicode letters or digits (including truly empty, whitespace-only, and OCR-speck/punctuation-only output). |
| `short_text` | `character_count < 10`. |
| `replacement_characters` | `replacement_character_count > 0`. |
| `control_characters` | `control_character_count > 0`. |
| `suspicious_symbol_density` | `suspicious_symbol_ratio >= 0.03` AND `suspicious_symbol_count >= 5`. |
| `fragmented_text` | `mean_token_length < 3.0` AND `alpha_token_count >= 20`. Catches OCR fragment noise; does not catch word-level misrecognition. |
| `joined_text` | `mean_token_length >= 10`, `max_token_length >= 80`, `alpha_token_count >= 20`, `whitespace_character_ratio <= 0.03`, and `latin_letter_ratio >= 0.8`. Catches collapsed Latin word boundaries; it is review evidence, not proof of corruption. |
| `suspicious_embedded_text_delta` | PDF embedded text character ratio < 0.5 or > 1.8, when embedded text is available and adapter does not report `embedded_text` capability. |
| `historical_orthography` | `prereform_letter_count >= 2`, OR `terminal_hard_sign_count >= 2` at a density of ≥1 per 100 alphabetic tokens over ≥20 tokens. The page is pre-1918 Russian orthography and an OCR model trained on modern text is probably mismatched. Measured on an 1850 gubernia review: 21 terminal ъ per 100 tokens vs 0.00 in modern text. |
| `low_confidence` | `confidence_detail.below_60_ratio >= 0.25` over ≥10 words. A quarter of the words under engine confidence 60 marks the page for review; a mean can hide one illegible paragraph on an otherwise clean page. |
| `instruction_echo` | Output contains one of the high-specificity chat-template markers `<think>`, `</think>`, `<|channel`, `<|im_start|>`, `<|im_end|>`, `[INST]`, or `[/INST]`. Generic words such as “instructions” or “channel” do not trigger it. |
| `output_inflation` | On a rerun, output is at least 4× and at least 1,000 characters longer than the same parent page. Parent counts, delta, and ratio are recorded in `output_integrity`; this is review evidence, not proof of hallucination. |

### output_integrity fields

| Field | Type | Meaning |
|---|---|---|
| `instruction_markers` | array of strings | Exact marker labels found in the output; empty when none match. |
| `parent_character_count` | integer or null | Parent page character count when usable rerun evidence exists. |
| `character_delta` | integer or null | Child count minus parent count. |
| `character_ratio` | number or null | Child count divided by parent count, rounded to four decimals; null when the parent is absent or empty. |

An empty parent can still trigger `output_inflation` when the child is at
least 1,000 characters: the 4× condition is satisfied, but the undefined
ratio remains null. Missing or incomplete legacy parent evidence leaves all
three comparison fields null and cannot trigger inflation.

The JSON Schema is at `schemas/quality-line.schema.json`.
