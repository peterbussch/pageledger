# Checkpoint specification

`pageledger run --resumable` retains per-page recovery records for an original
(generation-zero) execution. `pageledger resume RUN_DIR` continues that run in
place. Document jobs created with `process` retain checkpoints automatically.
These workflows ship in 0.5.0 with artifact schema version 0.1.

## Get started

Recovery requires a POSIX system such as macOS or Linux.

Try recovery with the built-in text adapter:

```bash
printf 'A first source page.\fA second source page.\n' > sample.txt
pageledger run sample.txt --adapter text --resumable --out runs/recoverable
pageledger resume runs/recoverable
pageledger verify-run runs/recoverable
```

This small run will usually finish before you can interrupt it. Resuming a
finished run verifies and returns its existing result. For a longer run, use
`--adapter pdf_ocr` with a PDF, or supply a config with zero retries and
`on_page_error: stop`. After an interruption, run `resume` in the same environment
with the same source files and output directory.

Resume reuses verified responses and completed pages. If a request started but
no response was saved, it records `outcome_unknown` and stops. Investigate that
request with the adapter or provider before starting any new work; resume does
not automatically retry an uncertain call.

For document jobs, use `pageledger resume jobs/book` followed by
`pageledger inspect-job jobs/book` and `pageledger verify-job jobs/book`. See
[document processing](processing-spec.md) for job-level status and review.

## Files and authority

`checkpoint.json` records immutable job identity and the complete ordered page
plan. `.checkpoint/pages/PAGE_ID.json` records one page's durable attempt and
response. Every planned page receives an initial record before the job
checkpoint is committed and before any extraction call. A missing page record
is corruption, not permission to call an adapter again.

Each record is a JSON envelope:

```json
{
  "schema_version": "0.1",
  "sha256": "<SHA-256 of canonical payload JSON>",
  "payload": {}
}
```

The digest uses UTF-8 JSON with sorted keys, unescaped Unicode, compact
separators and no NaN or infinite values. The hash detects inconsistent bytes;
it is not a signature or proof of authorship. See
[`checkpoint.schema.json`](../schemas/checkpoint.schema.json) and
[`checkpoint-page.schema.json`](../schemas/checkpoint-page.schema.json).

Writes use a temporary file in the destination directory, file flush/fsync,
atomic replacement and directory fsync. Responses live in individual page
files so advancing one page does not rewrite every earlier transcript.
`.resume.lock` uses a nonblocking POSIX advisory lock to exclude another
recovery writer. Process termination releases the lock; the retained lock file
does not mean a writer is still active. Resumable execution currently requires
POSIX support.

The recovery journal is authoritative for continuing an interrupted run. Final
`provenance.jsonl`, `quality.jsonl`, cost, audit and rerun artifacts are derived
from the saved responses and frozen context. The final manifest is published
last. Neither page completion nor manifest publication certifies extraction
accuracy; quality warnings and review queues remain independent evidence.

Once the final manifest exists, the ordinary run artifacts are authoritative.
Resume rechecks original source bytes and verifies a finalized successful run
without replaying its historical checkpoint or loading an adapter. Valid later
`align` changes are preserved. Failed finalized runs are refused. `verify-run` does not
certify the retained historical recovery records of a finalized run; an
interrupted run with valid recovery evidence is still reported as incomplete.

## Frozen job identity

All of these `checkpoint.json.payload` fields are required:

| Field | Shape |
| --- | --- |
| `run_id`, `started_at` | Original run identifier and UTC run start time. |
| `root` | Absolute original output directory. |
| `config_sha256`, `config_source_path` | Exact retained config digest and original config reference. |
| `identity` | Adapter `name`, `version`, `code_sha256`, PageLedger `package_sha256`, optional `profile`, and declared `metadata`. Adapter/profile values may be null for route-only work. |
| `inputs` | Ordered source objects with `path`, `sha256`, full `page_count`, and optional `pages` selection expression. |
| `documents` | Ordered source objects with `source`, `source_sha256`, `page_count` and planned `pages`; each page retains its route fields. |
| `imported_routes` | Retained imported route-map object, or null. |
| `route_warnings` | List of routing warnings. |
| `routing` | Original imported route reference (`source_path`, `sha256`, `source_run_id`), or null. |
| `log_level` | One of DEBUG, INFO, WARNING or ERROR. |
| `skipped_inputs` | Optional. Names of hidden files skipped while expanding input directories, carried so a resumed run reports the same list. |

`identity.metadata` contains `input_types`, `output_types`, `capabilities`
and `deterministic`. The config snapshot pins adapter options, prompt, grading and budget
policy. Resume reads that retained snapshot even when the original config was
a temporary file generated by `run --adapter`.

Resume verifies these identities, every expected page record and retained
output before further adapter calls. Changed source bytes, a changed config,
adapter/package changes, altered output, missing evidence or incompatible
lineage fail closed. Sources are hashed again after processing, before final
publication. Full source page counts remain distinct from selected-page
counts. Recovery is in place: moving a run directory or replacing its source
paths is outside this contract.

## Page states

| State | Durable evidence and next action |
| --- | --- |
| `pending` | The planned page has no started extraction request. A route that calls an adapter may execute. Review/skip routes do not call it. |
| `started` | A request-start record was committed before adapter invocation. No durable outcome exists. |
| `outcome_unknown` | Resume found a started request without a durable outcome. All further calls are refused. |
| `response` | The complete valid `ExtractionResult`, original start time and measured duration are saved. Resume can publish page artifacts without calling the adapter. |
| `completed` | The response plus raw byte identity, provenance, quality signals and optional alignment are saved. Resume verifies them before reuse. |
| `failed` | A typed, redacted failure is retained, including HTTP status when available and unknown cost as null. Queued calls are stopped. |

Every page payload includes `run_id`, `page_id` and `state`. All states except
`pending` include `started_at`. `response` and `completed` add `result` and
`extraction_seconds`; `completed` also adds `completion`, with `provenance`,
`quality` and nullable `alignment`. `failed` adds `error`, with the safe
exception `type`, normalized `code`, nullable integer `http_status` (100–599)
and nullable `cost_usd`. A typed failure can also include `partial_result` and
`extraction_seconds` together. The partial result has the same shape and
validation as a successful result, including optional `input_evidence`; its
cost, if reported, must equal the error cost. Without a validated partial
result, error cost remains null. These state-specific fields are required; extra envelope
and page-payload fields are rejected. The schemas define the complete shapes
and allowed diagnostic codes.

Successful response records preserve content, format, confidence, actual
returned model, warnings, usage, optional confidence detail and optional image
input evidence. Reported numeric usage values are finite and non-negative;
unknown values remain null. Partial failure results are retained in the failure receipt,
never counted as completed or eligible for automatic retry. Final
provenance still contains one successful extraction record per page; recovery
does not add duplicate page rows or count reused responses as new calls.
Aggregates reconstruct the original successful-page usage, timing and budget
crossings. A process interruption cannot reset the budget. Resumable runs stop
before a pending request when accumulated usage has reached a configured cap;
saved responses and completed pages can still be recovered at that cap.

A raw file written before completion is checked against the saved response.
A valid saved response can be published after interruption. Raw output without
a valid response does not establish success. Completion evidence that is
missing or disagrees with its output blocks recovery rather than silently
re-extracting or replacing the record.

## Failure boundary

Resumable mode requires zero automatic retries and stop-on-error policy.
`started` is persisted before the adapter call, so even an interruption just
before invocation is conservatively unknown. Once a provider might have
received a request, a missing response is not evidence that nothing happened.
No automatic possibly charged retry or provider-side exactly-once guarantee
is made.

Keep an unknown or failed run and investigate its provider/adapter evidence
before deliberately starting new work. There is no force-retry,
outcome-reconciliation or budget-override flag. Adapter-controlled error
messages and stdout/stderr are not retained. A successful later request does
not establish the cause of an earlier failure.

## Compatibility and limits

- Only generation-zero execute runs started with `--resumable` can recover.
  Dry runs, ordinary interrupted runs and rerun generations cannot be adopted.
- Resume retains the run id and generation; `run.adapter_order` continues to
  select one adapter per generation. It is not a per-page fallback list.
- The frozen config and code must remain compatible. Resume cannot account
  for unreported provider state, undeclared adapter dependencies or hidden
  adapter retry loops.
- Recovery data is operational history outside `ARTIFACT_PATHS`. A bundle
  transports the finalized legacy artifact set and its ordinary replay
  behavior, not the live recovery journal.
- Checks detect inconsistent evidence, not maliciously rewritten complete
  histories. Advisory locks coordinate cooperating recovery writers; they
  do not prevent unrelated tools from editing files or remote execution.
- Input image evidence follows [`image-evidence-spec.md`](image-evidence-spec.md).
  Recovery validates provenance and byte identity; it does not certify OCR
  accuracy or storage custody.

## Verification evidence

Focused recovery tests cover process termination, saved-response reuse,
unknown outcomes, altered or missing evidence, budgets, imported routes,
concurrent writers and finalized alignment. A separate local acceptance check
sent one synthetic page to an image model, terminated after response
persistence and again after page completion, then resumed to a verified final
run without another model request. This establishes the tested recovery
control flow. It is not a provider accuracy comparison, proof of hidden
provider execution counts, or physical power-loss certification.
