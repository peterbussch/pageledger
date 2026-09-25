# Document processing jobs

`pageledger process` manages one document's extraction attempts, selected text,
review state, and report. It first reads local text, then sends pages with defect
evidence to configured OCR and image stages. Every attempt remains available
for inspection.

## Start with local text and OCR

Document jobs require POSIX advisory locks, available on macOS and Linux.
Install `"pageledger[pdf]==0.5.2"`, Poppler, and Tesseract for a PDF job. Create
`processing.yml` with the following configuration, or copy the repository's
[processing example](examples/processing.yml):

```yaml
schema_version: "0.1"
processing:
  local_text:
    adapter: pdf_text
  local_ocr:
    adapter: pdf_ocr
    adapter_options:
      lang: eng
      dpi: 300
  limits:
    max_attempt_pages: 100
    max_image_pages: 0
```

Sample the first ten source pages of a PDF:

```bash
pageledger process book.pdf --config processing.yml --pages "1-10" --out jobs/book
pageledger inspect-job jobs/book
pageledger verify-job jobs/book
```

Use an existing PDF with at least ten pages, adjust `--pages`, or omit the flag
for the full document. The output directory must be new. Open
`jobs/book/transcript.md` for selected text and `jobs/book/report.md` for the
source links, attempts, and unresolved review work. A completed job means its
configured processing finished; human review may still be needed.

For text files, set `processing.local_text.adapter` to `text` and set
`processing.local_ocr` to `null`. Form-feed characters separate pages.

A document job reads only the `processing` section. Budget, pricing, grading
and rerun settings under `run` would be ignored, so `process` rejects them and
names the replacement (for budgets, `processing.limits`). A leftover
`run.adapter` or `taxonomy` section is harmless and produces a warning.
Selections such as `--pages "2-5,19"` preserve source page numbers and record both
the selected count and full document count.

## Resume an interrupted job

Keep the source, job directory, and adapter environment in place:

```bash
pageledger resume jobs/book
pageledger inspect-job jobs/book
pageledger verify-job jobs/book
```

Recovery validates retained attempts before scheduling pending work. Saved
responses are reused. A request with no saved outcome, a recorded provider
failure, or a halted job is not retried automatically. The
[checkpoint specification](checkpoint-spec.md) describes the recovery records.

`--adapter-path DIR` on `process` or `resume` loads custom adapters. Use
`process --review FILE` to supply source-bound human decisions before extraction,
or `review-job` to record them afterward. All job commands accept `--json`.

## Policy and configuration

`processing` is separate from `run.adapter_order`. The latter still chooses an
adapter by explicit rerun generation; `process` rejects it. A document job has
ordered stages `local_text`, `local_ocr`, `image`, and optional `second_opinion`.
Each stage is an adapter profile with `adapter`, optional `adapter_options`,
and optional `prompt`. Set an optional stage to `null` to disable it;
`local_text` is required. Local text defaults to `pdf_text` for PDFs or `text`
otherwise. PDF OCR defaults to `pdf_ocr`.
Image stages are disabled until configured with a positive
`processing.limits.max_image_pages`. An image stage requires an enabled local
OCR stage; a second opinion requires an image stage. Invalid combinations fail
before creating a job.

The local text pass caches document extraction and retains one response per
selected source page. Pages with structural or explicit defect evidence can
proceed to OCR, then an image adapter. OCR and image attempts run serially per
page. A clean later attempt may supply the transcript, while the original
review hold remains. Every attempt is retained. Missing stages leave pages for
review; they never imply success at the missing stage.

A plausible clipped text layer may escape structural checks. Record known
coverage defects through adapter warnings (`coverage_defect`, `clipped_text`,
`missing_content`) or source-bound review. Named numeric columns and aligned
records are compared where possible; arithmetic checks alone do not prove that
numbers belong to the right columns. A disagreement remains a review hold.
There is no reliable automatic test for complete scholarly apparatus, faithful
transcription, or the absence of invisible/missing source strokes.

Grades and model confidence do not choose the winning attempt or certify a
page. Blank candidates, reviewed blanks, illustrations, handwriting,
unreadable text and source defects have separate dispositions. Human receipts
can clear a hold only when they bind the source/page and, for reviewed text,
the selected attempt and output hash. A known source defect directs the user to
an alternate source and prevents further automatic extraction.

## One budget for the job

`processing.limits` accepts:

| Field | Enforcement |
|---|---|
| `max_attempt_pages` | Before scheduling: every attempted source page across all stages counts, including failed/uncertain attempts. A local batch larger than the remaining allowance is cut to fit; the job then pauses. |
| `max_image_pages` | Required positive limit when image processing is enabled. Counts both image and second-opinion attempts; checked before every call. |
| `max_tokens` | Accumulates reported usage across stages. Stops at the cap before new work and after a response crosses it. A response can exceed the remaining amount; this is not a provider billing ceiling. Unknown paid token usage prevents another image attempt when this cap is configured. |
| `max_cost_usd` | Accumulates reported dollar charges. Stops at the cap before new work and after a response crosses it. Unknown paid cost stops another image attempt. The first charge can be unknown or exceed the remaining amount; use a provider-side spending limit for a hard monetary ceiling. |

A job that reaches `max_attempt_pages`, `max_image_pages`, `max_tokens` or
`max_cost_usd` ends with status `paused_budget`, keeping everything it has
done. It continues only when resumed with a higher limit:

```bash
pageledger resume jobs/book --raise-limit max_attempt_pages=200
```

`resume` checks the retained attempts before it records the raise. Each raise
is appended to `limits_history` in `job.json` and the report, with its time and
the previous value, and `verify-job` checks that the history leads from the
configured limits to the current ones. Limits cannot be lowered. Money and
token limits are thresholds checked between calls, not reservations: a call
already under way can take usage past them. Unknown paid usage while a token or
cost limit is set still halts the job, because a higher limit cannot make that
usage known.

Unknown cost stays `null`; `known_cost_usd` is only the available subtotal.
Local execution is not assigned an invented dollar price. Image calls are
bounded even when a provider does not report prices. Budgets persist across
resume; saved responses are counted once. Local batches check the remaining
budget before each pending call, including after recovering a saved response.

## Durable job and attempts

`job.json` uses the checksummed envelope defined by `schemas/job.schema.json`.
Its payload includes the absolute root, source SHA-256, full/selected page
inventory, original configuration and hash, normalized policy, package hash,
ordered child plans, attempt evidence, page selections/reviews, usage and status.
The checksum detects inconsistent content. It does not establish authorship
or prevent deliberate rewriting.

Child plans are saved before launch; their exact YAML snapshots are under
`.job/`. Runs under `attempts/` retain the ordinary generation-zero artifacts
and the resumable checkpoints. `run_id` plus child path, attempt ID and source
page identify an attempt. This does not overload rerun lineage. Existing run
commands, selected denominators and generation-zero text replay keep their
contracts.

Before further calls, resume checks all retained child records, outputs,
source/page identities and configuration snapshots. Each child's configuration
digest must match its saved stage plan. Refresh hashes the shared source once,
then checks every child's source identity against that digest while still
verifying all retained child artifacts. A child completed before
the job index was saved is adopted without extraction. A response saved before
publication is recovered by the child runner. A started request without a
response becomes `outcome_unknown`; no automatic retry or fallback is allowed.
Typed provider failures, quota errors, and truncated output stop the whole
queue. Failed partial output is retained under `partials/` and linked in the
report, but cannot be selected as the transcript. Missing image input evidence
also stops the job and excludes that response from selection.

An interruption before a child's durable plan exists produces an explicit
`initialization_incomplete` halt. No extraction is scheduled without that plan.
The incomplete directory is retained for inspection rather than overwritten.

Source bytes are hashed before work and rechecked before each publication and
resume. PDF page-tree counts must match the actual inventory. Container failure
produces a halted report with an unknown page count and no invented pages.
Its `halt_reason` names the cause, such as
`source_container_invalid:unsupported_encryption` for a PDF that needs a
password, and `next_action` says what to do. An encrypted PDF that opens
without a password (one that restricts only printing or copying) is processed
normally.
Annotation counts are recorded without copying annotation contents; annotation
presence does not establish recovery of comments, body text, notes or citations.

A `completed` job means its configured processing work finished. It does not
mean its pages have passed human review. A `paused_budget` job continues when
resumed with a higher limit. A `halted` job retains its evidence; `resume`
reports it without retrying. Reconcile uncertain requests externally
before explicitly starting any new job. `verify-job` is read-only and binds the
report back to validated job/child evidence.

## Reports and review

The job publishes `document.json`, `report.md` and `transcript.md`. See
[document-report-spec.md](document-report-spec.md) for their exact contract and
review receipt examples. `report.md` and the exact final UTF-8 transcript are
rendered from JSON. The transcript digest covers its serialized bytes, including
headings and source-page links; no implicit NFC conversion occurs.
Reports name the source by its path relative to the job directory. To use the
text elsewhere, [export it](export.md) as plain text, Markdown, JSONL or TEI.

```bash
pageledger review-job jobs/book --review reviewed-pages.json
pageledger inspect-job jobs/book --json
```

Applying review receipts performs no extraction and retains previous receipts
in `review_history`. Changing reviews in an interrupted processing job halts its
old plan, so pending calls cannot ignore a newly recorded source defect. The
report links every selected source page and attempt, retains unresolved states
and failed partial links, and gives one next action. Optional `processing.links.article` and
`processing.links.custody` are references supplied by the caller, not verified
claims of preservation or publication. Source capture, preservation, removal
eligibility and removal are separate fields. This implementation never moves,
deletes, publishes or certifies preservation of source files.

### Review in a spreadsheet

```bash
pageledger review-sheet jobs/book --out review.csv
pageledger review-job jobs/book --review review.csv --reviewer "A. Reader" --dry-run
pageledger review-job jobs/book --review review.csv --reviewer "A. Reader"
```

`review-sheet` writes one row per page: a link to the page in the source, its
disposition, the start of its selected text, its review reasons, and empty
`decision` and `note` columns. Fill those in a spreadsheet and save as CSV. A
decision is one of:

| Decision | Records |
|---|---|
| `accept` | The selected text, as `reviewed_text` |
| `blank` | The page as blank, `reviewed_blank` |
| `illustration`, `handwriting`, `unreadable`, `source_defect` | That disposition |
| `use:ATTEMPT` | Another completed attempt's text, named by its attempt ID from the report |

A blank decision, or a deleted row, leaves the page unreviewed; the note becomes
the receipt's reason. `review-job` checks the whole sheet before recording
anything: every row must belong to the job, appear once, hold no spreadsheet
formula, and still match the evidence it was written from, which
`review.csv.binding.json` records beside the sheet. A sheet written before the
job's evidence changed is refused; write a new one. `--dry-run` checks and
counts the decisions without recording them. Scripts can keep writing JSON
receipts.

## Image adapter boundary

The optional [OpenAI-compatible example](../examples/openai_image_adapter.py)
accepts explicit Gemini or DeepSeek model names, checks the live model list,
sends one bounded JPEG request, and records returned identity and input-image
provenance. Pillow and Poppler belong to that adapter environment. Core includes
no provider SDK, OCR engine or pricing catalog. See
[image-evidence-spec.md](image-evidence-spec.md).

Image-evidence runs currently refuse `bundle` with an explicit unsupported
error. They remain verifiable and resumable in place. Ordinary generation-zero
text bundles/replay remain supported. Whole-job portable bundles, synthesis, Zotero/Drive connectors, and file
retirement are outside the processing workflow.
