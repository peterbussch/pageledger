# Route map specification

The route map records page-level or region-level routing decisions before
extraction. It is reviewable evidence: a human can inspect what will be
extracted before money or model calls are spent.

## Minimal shape

```yaml
schema_version: "0.1"
run_id: run-20260619-001
generated_at: "2026-06-19T19:30:00Z"
classifier:
  adapter: null
  model: null
  prompt_hash: null
documents:
  - source: scans/volume_01.pdf
    pages:
      - page_id: doc_0001_page_0001
        page_number: 1
        type: structural_metadata
        confidence: null
        action: skip
        reason: configured_skip
      - page_id: doc_0001_page_0002
        page_number: 2
        type: table_data
        confidence: null
        action: vlm_table
        reason: configured_adapter
      - page_id: doc_0001_page_0003
        page_number: 3
        type: index
        confidence: null
        action: review
        reason: configured_review
```

## Top-level fields

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Route map schema version. `"0.1"`. |
| `run_id` | string | ✅ | no | Run identifier from `manifest.json`. |
| `generated_at` | string | ✅ | no | UTC ISO 8601 timestamp. |
| `classifier` | object | ✅ | no | Classifier metadata (all null in alpha). |
| `documents` | array | ✅ | no | One entry per input source. |

### classifier fields

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `adapter` | string or null | ✅ | yes | Classifier adapter. Null in alpha. |
| `model` | string or null | ✅ | yes | Classifier model. Null in alpha. |
| `prompt_hash` | string or null | ✅ | yes | Classifier prompt hash. Null in alpha. |

## Required page fields

| Field | Type | Meaning |
|---|---|---|
| `page_id` | string | Stable page identifier. |
| `page_number` | integer | One-based page number in the source document. |
| `type` | string | Page type from the project taxonomy. |
| `confidence` | number or null | Classifier confidence, 0 to 1 where available; null when no classifier ran. |
| `action` | string | Extraction action from the project config, or `review` during dry runs. |
| `reason` | string | Current alpha route explanation, such as `no_classifier_available`, `configured_adapter`, `configured_skip`, or `configured_review`. |

## Optional page fields

| Field | Type | Meaning |
|---|---|---|
| `prompt` | string | Prompt profile to use for extraction. |

## Design notes

- Each route map entry's `page_id` joins to the matching `provenance.jsonl`
  line's `page_id`.
- Route map page `confidence` should be copied to provenance as
  `route.route_confidence`.
- Per-page `prompt` routing is allowed in v0.1. Per-page schema routing is not;
  v0.1 uses one primary schema per run.
- v0.1 assumes one primary schema per run. Per-page schema routing is reserved
  for a later multi-schema release.
- v0.1 route maps are page-level. Region-level routing can be added later once
  page routing is stable.
- The page taxonomy should be user-provided. PageLedger can ship examples,
  but projects should not inherit Soviet Corpus labels by accident.
- Example taxonomies are not calibrated or domain-appropriate until a project
  reviews them against its own documents.
- Confidence is currently `null` because no classifier ships in the alpha.
  Future classifiers should record confidence even when not calibrated, and
  audit policies can decide how to interpret it.
- The route map should be generated before extraction and preserved after
  extraction.
- Since route-map.yml is YAML, its field contract is documented in this spec
  rather than a JSON Schema file. The field tables below define the v0.1 contract.
- Schema validation tests (manual YAML assertions) are in
  `tests/pageledger/test_schemas.py`.
