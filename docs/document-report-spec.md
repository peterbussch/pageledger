# Document report and human review receipts

Schema version: **0.1**. The schemas are
[`document.schema.json`](../schemas/document.schema.json) and
[`job-review.schema.json`](../schemas/job-review.schema.json).

`document.json` is the authoritative document report. `report.md` and
`transcript.md` are deterministic renderings of that JSON. The immutable job
checkpoint and retained attempt evidence remain the sources for reconstructing
the report. A report does not certify transcription accuracy or source preservation.

## Document fields

| Field | Meaning |
| --- | --- |
| `schema_version` | `"0.1"` |
| `job_id`, `created_at` | Job identity and creation timestamp |
| `source` | Original `path`, exact byte `sha256`, `page_count`, and annotation inventory |
| `selected_pages` | Unique, one-based source page numbers requested for this job |
| `status` | `processing`, `completed`, or `halted`; completion does not mean human review |
| `pages` | The selected-page inventory, including pages without attempts or selected text |
| `usage` | `attempt_pages`, `image_calls`, `tokens`, `cost_usd`, and `cost_known`; optional `known_cost_usd`, `tokens_known`, `paid_cost_known`, and `paid_tokens_known` |
| `limits` | The job's recorded execution limits |
| `links` | Nullable `article` and `custody` references supplied by the job |
| `source_retention` | Explicit source capture, preservation, removal eligibility, and removal state |
| `next_action` | One human-readable action for the document |
| `counts` | `source_pages`, `selected_pages`, `processed_pages`, `selected_outputs`, and `unresolved_pages` |
| `transcript` | `path: "transcript.md"` and the SHA-256 of its complete serialized UTF-8 bytes |

`source.page_count` and `counts.source_pages` are null when source inspection
could not establish a page count. No zero-page source is inferred from a failed
preflight. Annotation `status` is `present`, `none`, or `unknown`; `count` is an
integer or null. Finding annotations does not establish body extraction or
citation completeness.

`processed_pages` counts selected pages with at least one recorded attempt,
including failed or unknown attempts. `selected_outputs` counts pages with a
selected completed output. `unresolved_pages` counts pages whose disposition is
neither `reviewed_text` nor `reviewed_blank`. Human receipts identifying
`source_defect`, `handwriting`, `unreadable`, or `illustration` retain that
unresolved status. A reviewed `source_defect` identifies the source problem; it
does not make missing source content recoverable or remove the need for an
alternate source.

`paid_tokens_known` records whether token usage is known for the image/model
attempts used by model-token budget checks. Native engines without token usage
can leave the aggregate `tokens_known` false without making model usage unknown.

The source retention values in this version are `capture: "present"`,
`preservation: "unverified"`, `removal_eligibility: "not_assessed"`, and
`removed: false`. Extraction and report generation do not move or remove the source.

## Page and attempt evidence

Each page includes `page_id`, `page_number`, `source_sha256`, `attempts`,
`selected_attempt`, `disposition`, `review_reasons`, nullable `review`, and
`next_action`. The report adds `source_link` and nullable `selected_output`.
`selected_output` contains the chosen `attempt_id`, relative artifact `path`,
`sha256`, `format`, and exact `text`.

Each attempt records `attempt_id`, `stage`, `outcome`, nullable `raw_artifact`
and `raw_sha256`, `format`, `warnings`, `classification`, nullable `alignment`,
`usage`, and nullable `failure`. Stages are `local_text`, `local_ocr`, `image`,
and `second_opinion`. Outcomes are `completed`, `failed`, `outcome_unknown`,
and `response`. A retained response awaiting completion is still uncertain.
Failed partial outputs remain linked as attempt evidence and cannot be selected.
Child-run attempts also retain `run_path`, `run_id`, `page_id`, `page_number`,
`source_sha256`, and nullable `input_evidence` for image preparation provenance.

For a selected output, the report decodes its verified raw bytes as UTF-8 and
embeds the resulting string without Unicode normalization, whitespace trimming,
newline conversion, or JSON/table reformatting. The transcript adds page headings,
source links, selected-attempt links, and page separators around those strings.
Source paths are percent-encoded before adding the `#page=N` fragment, so
filename characters such as `#`, `?`, and `%` remain part of the path.
It also lists pages without selected text. The transcript hash covers that whole
Markdown serialization, including the added material and final newlines.

## Selection and escalation

`assess_page(page, review=None)` returns `selected_attempt`, `disposition`,
`review_reasons`, and `next_action`. The caller supplies decoded raw text as the
transient `text` field when assessing completed attempts; it is not persisted in
job attempt metadata. Report generation reads and verifies raw files separately.

The first usable completed candidate without recognized blocking defects is
selected. If none qualifies, the earliest usable completed output is retained
provisionally. A clean retry can replace defective native text, but all previous
review holds remain recorded. Grades and model confidence never rank candidates
or establish review status.

Explicit coverage warnings, missing required columns, failed structure checks,
and weak structural classifications create coverage holds. Explicit numerical
conflict warnings, coercion failures, failed or unchecked arithmetic checks, and
observable numeric disagreements create numerical holds. Plain prose can be
plausible but clipped; this policy does not reliably detect that failure without
additional evidence. Sparse/unknown structural output is a reason to inspect or
escalate, not a claim that content is definitely missing.
Existing `replacement_characters`, `control_characters`,
`suspicious_symbol_density`, `low_confidence`, and `instruction_echo` quality
warnings also create coverage holds even when the output is classified as prose
or has a high grade. An explicit quality warning is evidence requiring review;
numeric model confidence does not rank candidates. `historical_orthography`
alone does not force a rewrite or extraction escalation.

Numeric comparison uses records from alignment or explicitly structured JSON,
CSV, and Markdown tables. It compares numeric cells under the same named columns
and aligned row identities. Unique nonnumeric row labels align reordered records;
otherwise all-numeric rows are compared in their supplied order. Unsupported or
ambiguous structures are not silently interpreted. Equal totals or equal numeric
multisets cannot clear changed column associations. A successful comparison is
not proof of fidelity to the source.

Dispositions are `pending`, `unreviewed_text`, `coverage_defect`,
`numeric_column_conflict`, `blank_candidate`, `reviewed_blank`, `reviewed_text`,
`illustration`, `handwriting`, `unreadable`, `source_defect`, `provider_failure`,
and `outcome_unknown`. Empty extraction is a `blank_candidate`, not confirmed
visual blankness. A latest failed/unknown attempt takes precedence in the
displayed disposition; earlier selected text and review reasons remain available.

`next_action` is the next stage name, `review`, or `none`. Defective candidates
advance through the stage sequence. Clean candidates and numerical disagreements
await review. Source defects, illustration classifications, and unknown/failed
outcomes stop automatic escalation. A bound human receipt sets `next_action` to
`none`; only the receipt can establish `reviewed_text` or `reviewed_blank`.

## Human review receipt

The review envelope has `schema_version`, `source_sha256`, and `decisions`.
Each decision contains:

| Field | Binding |
| --- | --- |
| `page_id`, `page_number` | Exact job page identity and one-based source page number |
| `disposition` | `reviewed_text`, `reviewed_blank`, `illustration`, `handwriting`, `unreadable`, or `source_defect` |
| `selected_attempt` | Exact completed attempt ID, or null for source-only/preflight review |
| `output_sha256` | Exact selected raw artifact digest, or null when no attempt is selected |
| `reason`, `reviewer` | Nonempty human review explanation and reviewer identity |
| `reviewed_at` | ISO-8601 timestamp with timezone |

`reviewed_text` always requires a completed artifact and its matching output
digest. Other dispositions allow a null selection, including preflight confirmation
of blankness, illustration, or source damage before any extraction call. A nonnull
selection always requires the exact completed artifact hash. A receipt cannot
bind a failed or unknown partial response. `validate_review(review, page)` checks
the source, page, output, and required reviewer fields; the job layer validates
the complete envelope and duplicate-page inventory. `page.review` stores this
envelope, commonly with only the decision for that page.

Review changes the decision, not the evidence. Original attempts, warnings,
classification and alignment evidence, and accumulated `review_reasons` remain.
Optional `page.review_history` retains the ordered review envelopes when a human
replaces a decision. The active `page.review` equals the latest receipt; earlier
receipts remain source/page/output-bound evidence. Applying the same latest
receipt again does not append a duplicate. Reports preserve the full history in
`document.json` while the human summary displays the active decision.

## Integrity and rendering APIs

`build_document_report(job, root)` verifies raw references and constructs the
complete report without writing. `write_document_report(job, root)` publishes
`transcript.md`, `report.md`, then `document.json`, using atomic byte writes.
`render_transcript(report)` and `render_document_report(report)` consume JSON
only. `verify_document_report(root)` checks both renderings, the transcript hash,
all linked raw hashes, and the selected text bytes.

The job verifier additionally rebuilds the report from the verified job and
compares the entire JSON. Rendering consistency alone does not bind a report to
its source job. Raw paths must remain relative to the job root; absolute paths,
parent traversal, and symlink components are rejected. Each output file is atomic;
a crash between files is detectable through the JSON/rendering checks and can be
repaired by regenerating the report from the verified job.
