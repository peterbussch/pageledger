# Route map specification

The route map records page-level routing decisions separately from extraction.
`pageledger classify` can produce it from a cheap page probe or retained run
evidence, and a human can inspect or edit it before another extractor is
called. `pageledger run --routes` executes the reviewed map.

## Minimal shape

```yaml
schema_version: "0.1"
run_id: classify-20260717T193000000000Z
generated_at: "2026-07-17T19:30:00Z"
classifier:
  adapter: builtin:structural
  model: pdf_ocr/0.1
  prompt_hash: null
documents:
  - source: /data/scans/volume_01.pdf
    source_sha256: abc123
    page_count: 3
    pages:
      - page_id: doc_0001_page_0001
        page_number: 1
        type: blank
        confidence: 0.95
        action: skip
        reason: blank_text
      - page_id: doc_0001_page_0002
        page_number: 2
        type: table_likely
        confidence: 0.6
        action: review
        reason: column_digit_density
      - page_id: doc_0001_page_0003
        page_number: 3
        type: prose
        confidence: 0.7
        action: transcribe_text
        prompt: Preserve spelling.
        reason: prose_text
```

## Top-level fields

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Artifact schema version. It remains `"0.1"` in PageLedger 0.2.0. |
| `run_id` | string | ✅ | no | Identifier of the classification or planning operation. An extraction run rebinds this to its own run ID. |
| `generated_at` | string | ✅ | no | UTC ISO 8601 timestamp. |
| `classifier` | object | ✅ | no | Classifier metadata. Null values mean no classifier ran; classified and imported maps preserve the supplied identity. |
| `documents` | array | ✅ | no | One entry per input source. |

Each document requires `source` and `pages`. New maps also record optional
`source_sha256` and `page_count`; they remain optional so older 0.1 maps are
readable.

### classifier fields

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `adapter` | string or null | ✅ | yes | `builtin:structural`, a classifier-hook import spec, an external classifier identity, or null when no classifier ran. |
| `model` | string or null | ✅ | yes | Probe identity for the built-in classifier, hook model or `<name>/<version>`, external model, or null. Multiple built-in probe identities use a sorted `mixed:` value. |
| `prompt_hash` | string or null | ✅ | yes | Classifier prompt hash when one applies. The built-in structural classifier records null. |

## Required page fields

| Field | Type | Meaning |
|---|---|---|
| `page_id` | string | Stable page identifier. |
| `page_number` | integer | One-based page number in the source document. |
| `type` | string | Built-in structural type or a type declared by the project's classifier and taxonomy. |
| `confidence` | number or null | Finite classifier evidence score from 0 to 1; null records unresolved uncertainty, including probe failures and ambiguous pages. |
| `action` | string | `skip`, `review`, or an extraction action supported by the configured run adapter. |
| `reason` | string | Nonempty route explanation, such as `blank_text`, `column_digit_density`, `probe_failed:OSError`, `no_classifier_available`, or a hook-defined reason. |

## Optional page fields

| Field | Type | Meaning |
|---|---|---|
| `prompt` | string | Prompt string passed to the extraction adapter. |

## Design notes

- The supported classify-to-run workflow is:

  ```bash
  pageledger classify scans/volume.pdf --config pageledger.yml --out route-map.yml
  pageledger run scans/volume.pdf --config pageledger.yml \
    --routes route-map.yml --out runs/volume
  ```

  The emitted map passes the route-map loader without a conversion step. It
  still must be executed against the same complete inputs, a compatible page
  counter, and a config whose adapter supports the mapped actions.
- `pageledger run --routes FILE` executes a complete reviewed map. The map must
  cover every source page exactly once, use deterministic
  `doc_{NNNN}_page_{MMMM}` IDs, and reference exactly the CLI inputs. Relative
  source paths resolve from the map's directory. `pageledger classify` emits
  absolute source paths.
- When `taxonomy.page_types` is nonempty, every page type must exist in it.
  `pageledger classify` checks up front that a nonempty taxonomy covers every
  type the built-in classifier or hook can emit. An empty taxonomy skips the
  type-membership check and classification maps pages conservatively to
  `review`.
- Every extraction action must be supported by the configured adapter. `skip`
  and `review` do not call the adapter.
- If document hashes or page counts are present, PageLedger verifies them
  before creating the output directory. Missing legacy values produce warnings
  and the executed map records current values.
- The output `route-map.yml` is rebound to the extraction run ID while
  classifier metadata and the original `generated_at` are preserved. The
  manifest `routing` block records the input map's path, hash, and run ID.

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
- PageLedger's built-in taxonomy is structural only: `blank`, `sparse`,
  `prose`, `table_likely`, and `unknown`. Domain taxonomies belong in a
  user-supplied classifier hook rather than core.
- Built-in confidence values are fixed, uncalibrated evidence scores. Hook and
  imported classifier confidences are also evidence unless their project has
  independently calibrated them; none should be read as probability by
  default.
- Preserve the route map that governed extraction. `classify --from-run` can
  create a later map from retained raw evidence, but it does not replace the
  map stored in the parent run.
- Since route-map.yml is YAML, its field contract is documented in this spec
  rather than a JSON Schema file. These field tables define the v0.1 artifact
  contract even when the PageLedger package version is 0.2.0.
- Schema validation tests (manual YAML assertions) are in
  `tests/pageledger/test_schemas.py`.

The built-in rules, thresholds, hook protocol, and evidence JSONL contract are
documented in [`classifier.md`](classifier.md).
