# Document processing jobs (development)

`pageledger process` owns one document's extraction attempts, page selection,
review state and report. It is available in this development checkout; it does
not change the package version or the published release.

```bash
pageledger process book.pdf --config docs/examples/processing.yml --out jobs/book
pageledger inspect-job jobs/book
pageledger verify-job jobs/book
# After an interruption, using the same source, code and adapter environment:
pageledger resume jobs/book
```

The output directory must be new. PDF processing needs the `pdf` extra. Local
OCR needs installed Poppler and Tesseract; PageLedger does not install engines.
Text inputs use form-feed pagination. `--pages '2-5,19'` keeps those source page
numbers and records both the selected denominator and full document count.
`--adapter-path DIR` loads custom adapters. `--review FILE` supplies existing
source-bound visual decisions before any extraction.

## Policy and configuration

`processing` is separate from `run.adapter_order`. The latter still chooses an
adapter by explicit rerun generation; `process` rejects it. A document job has
ordered stages `local_text`, `local_ocr`, `image`, and optional `second_opinion`.
Each stage is an adapter profile with `adapter`, optional `adapter_options`,
and optional `prompt`. Set a stage to `null` to disable it. Local text defaults
to `pdf_text` for PDFs or `text` otherwise. PDF OCR defaults to `pdf_ocr`.
Image stages are disabled until explicitly configured with a positive
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
| `max_attempt_pages` | Before scheduling: every attempted source page across all stages counts, including failed/uncertain attempts. A local batch must fit in full. |
| `max_image_pages` | Required positive limit when image processing is enabled. Counts both image and second-opinion attempts; checked before every call. |
| `max_tokens` | Accumulates reported usage across stages. Stops at the cap before new work and after a response crosses it. A response can exceed the remaining amount; this is not a provider billing ceiling. Unknown paid token usage prevents another image attempt when this cap is configured. |
| `max_cost_usd` | Accumulates reported dollar charges. Stops after a response crosses the cap. Unknown paid cost stops another image attempt. The first charge can be unknown or exceed the remaining amount; use a provider-side spending limit for a hard monetary ceiling. |

Unknown cost stays `null`; `known_cost_usd` is only the available subtotal.
Local execution is not assigned an invented dollar price. Image calls are
bounded even when a provider does not report prices. Budgets persist across
resume; saved responses are counted once.

## Durable job and attempts

`job.json` uses the checksummed envelope defined by `schemas/job.schema.json`.
Its payload includes the absolute root, source SHA-256, full/selected page
inventory, original configuration and hash, normalized policy, package hash,
ordered child plans, attempt evidence, page selections/reviews, usage and status.
The checksum detects corruption, not malicious rewriting or authorship.

Child plans are saved before launch; their exact YAML snapshots are under
`.job/`. Runs under `attempts/` retain the ordinary generation-zero artifacts
and the resumable checkpoints. `run_id` plus child path, attempt ID and source
page identify an attempt. This does not overload rerun lineage. Existing run
commands, selected denominators and generation-zero text replay keep their
contracts.

Before further calls, resume checks all retained child records, outputs,
source/page identities and configuration snapshots. A child completed before
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
Annotation counts are recorded without copying annotation contents; annotation
presence does not establish recovery of comments, body text, notes or citations.

A `completed` job means its configured processing work finished. It does not
mean its pages have passed human review. A `halted` job retains its evidence;
`resume` reports it without retrying. Reconcile uncertain requests externally
before explicitly starting any new job. `verify-job` is read-only and binds the
report back to validated job/child evidence.

## Reports and review

The job publishes `document.json`, `report.md` and `transcript.md`. See
[document-report-spec.md](document-report-spec.md) for their exact contract and
review receipt examples. `report.md` and the exact final UTF-8 transcript are
rendered from JSON. The transcript digest covers its serialized bytes, including
headings and source-page links; no implicit NFC conversion occurs.

```bash
pageledger review-job jobs/book --review reviewed-pages.json
pageledger inspect-job jobs/book --json
```

Applying review receipts performs no extraction and retains previous receipts
in `review_history`. Changing reviews in an interrupted processing job halts its
old plan, so pending calls cannot ignore a newly recorded source defect. The report links every
selected source page and attempt, retains unresolved states and failed partial
links, and gives one next action. Optional `processing.links.article` and
`processing.links.custody` are references supplied by the caller, not verified
claims of preservation or publication. Source capture, preservation, removal
eligibility and removal are separate fields. This implementation never moves,
deletes, publishes or certifies preservation of source files.

## Image adapter boundary

The optional [OpenAI-compatible example](../examples/openai_image_adapter.py)
accepts explicit Gemini or DeepSeek model names, checks the live model list,
sends one bounded JPEG request, and records returned identity and input-image
provenance. Pillow and Poppler belong to that adapter environment. Core includes
no provider SDK, OCR engine or pricing catalog. See
[image-evidence-spec.md](image-evidence-spec.md).

Image-evidence runs currently refuse `bundle` with an explicit unsupported
error. They remain verifiable and resumable in place. Ordinary generation-zero
text bundles/replay remain supported. Whole-job portable bundles, synthesis,
Zotero/Drive connectors, file retirement and release automation are outside this
implementation.
