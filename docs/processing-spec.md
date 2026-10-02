# Document processing jobs

`pageledger process` manages one document's extraction attempts, selected text,
review state, and report. It first reads local text, then sends pages with defect
evidence to configured OCR and image stages. Every attempt remains available
for inspection.

## Start with local text and OCR

Document jobs require POSIX advisory locks, available on macOS and Linux.
Install `"pageledger[pdf]"`, Poppler, and Tesseract for a PDF job. Create
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

Job recovery checks retained attempts before scheduling pending work. Saved
responses are reused. A request with no saved outcome, a recorded provider
failure, or a halted job is not retried automatically. Each attempt is a
resumable run; the [checkpoint specification](checkpoint-spec.md) describes
its recovery records.

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

## Compare engines on a page

When a page has readings from more than one engine, the job compares them word
by word, after NFC normalization and with whitespace collapsed. Each comparison
records the two attempts, their word agreement from 0 to 1, up to 20 passages
where they differ and up to 20 numbers that differ. Comparisons are stored with
the page in `job.json` and the report.

Only readings that are clean themselves count. A page moves on to OCR because
its text layer was held, so that layer disagreeing with the OCR is expected and
says nothing. Comparing the selected reading with another clean one:

- word agreement below 0.60 adds the review reason `engine_disagreement`;
- a number that differs adds `numeric_disagreement`;
- a reading from a generative adapter, such as [`vision`](vision-adapter.md),
  stays in review as `unconfirmed_model_output` unless another clean reading
  agrees at 0.60 or above.

These reasons hold a page for review; they never start another stage. Engine
agreement is evidence, not truth: two engines can make the same mistake.

The 0.60 threshold was set on 24 transcribed calibration pages, where it holds
9 pages when RapidOCR is compared with Apple Vision, 14 for Surya with
RapidOCR and 22 for Tesseract with RapidOCR: the weaker the engines, the more
pages it holds. How many real number errors it catches has not been measured.
Jobs created before 0.6 are not compared and keep their review reasons.

To compare engines on pages that need no second reading, run one on a sample:

```yaml
processing:
  benchmark:
    stage: local_ocr
    every_nth_page: 10
```

This runs `local_ocr` on source pages 10, 20, 30 and so on, even when their
text layer is clean. The stage must be enabled, and its pages count against the
job's limits.

## Climb on roughness and disagreement

By default a page climbs to the next stage only when its reading carries a
warning hold, and two engines that disagree send the page straight to review.
Two problems escape that rule. OCR can be wrong without any warning: an engine
that reads two columns as one glues the halves of neighbouring lines into
non-words. And a disagreement between two clean readings is a question a
stronger engine can help answer. `escalate_on` adds two triggers:

```yaml
schema_version: "0.1"
processing:
  local_text:
    adapter: pdf_text
  local_ocr:
    adapter: pdf_ocr
    adapter_options:
      lang: rus
  escalate_on: [hold, rough, disagreement]
  lexicon:
    provider: pymorphy3
    language: ru
    rough_below: 0.95
  limits:
    max_attempt_pages: 100
    max_image_pages: 0
```

- `rough`: the selected reading's share of words the lexicon knows falls below
  `rough_below`. Words split across a line break are rejoined first, and a page
  with fewer than 20 Cyrillic words is not judged. Install the Russian lexicon
  with `pip install 'pageledger[ru]'`.
- `disagreement`: the selected reading and another clean one agree on fewer than
  60% of their words, or read a number differently. Without this trigger the page
  goes to review, as before.

`hold` is 0.6's rule and must be listed. Triggers lift a page at most to the
`image` stage: a second model is another witness with the same habits, not a
better reader, so `second_opinion` still climbs on holds only. When the rough
trigger is on, a later reading that is not rough replaces a rough one as the
selected text. If every clean reading is rough, the least rough is selected.

Each trigger that fired is recorded on the page with its stage, the reading it
judged and the evidence, for example
`{"trigger": "rough", "stage": "local_ocr", "known_share": 0.89, ...}`. The job
records the lexicon's provider and package versions. `verify-job` recomputes the
triggers and refuses a job whose lexicon is not the installed one. The report
adds the work done at each stage and how many pages each trigger raised.

These numbers came from one test with real documents. Clean born-digital
Russian scored 0.98 to 0.995. OCR of a two-column article read as one column
scored 0.87 to 0.91. Treat 0.95 as a starting point and measure your own
collection.

## Contest a model's reading

A vision model writes fluent text and silently corrects what it sees: it fixes
the printer's misprints and swaps in a likelier word. An OCR engine misreads
glyphs but does not invent words. With `contest`, a page whose selected reading
came from a model (an adapter with the `generative` capability) is compared word
by word with the best literal reading of the same page, and each place they
differ is recorded:

```yaml
processing:
  # ... stages as above, with an image stage
  contest:
    rules: ru-print-0.1
  lexicon:            # optional; lets the glyph rule judge Russian words
    provider: pymorphy3
    language: ru
```

The model's text is the base, and every span is anchored in its offsets. Words
one engine placed elsewhere on the page count as reading order, not as
differences. Unpaired words are grouped, so a line one engine dropped is one
span. A formula (`$...$`), a `[Figure: ...]` line, `[illegible]` and a word the
model marked `[?]` are each one `region` span, because the model wrote them in a
form no literal engine can confirm word by word.

| Kind | Literal reading against the model's | Settled by |
|---|---|---|
| `homoglyph` | Look-alike letters of another script (HAYK for НАУК), two letters or more | Rule H: the model's |
| `glyph` | A non-word whose difference from the model's known word is only confusions listed for that engine (RapidOCR's н for п) | Rule G: the model's |
| `misprint_guard` | A non-word the engine's listed confusions do not explain (назависимости) | Open: it may be what the page prints |
| `word` | Any other difference, or words only one side has | Open |
| `number` | Either side has a digit | Open |
| `region` | A formula, figure line or the model's own doubt | Open |

The rule set is versioned data. `ru-print-0.1` lists the confusions RapidOCR's
Cyrillic model made at least twice in one dogfood, checked against the page
images; its pairs never cross case, and they apply only to the `rapidocr`
adapter. Without a lexicon only rule H applies. On two fully checked Russian
documents (61 pages) the rules settled 175 spans with no error against the
image-checked text, and the open spans covered 303 of the 319 corrections the
model's text needed. The misses were accents and figure labels that neither
engine read.

The spans are kept in `contested/<page_id>.json` ([schema](../schemas/contested.schema.json)),
and the page records its hash and counts. A page with an open span is held as
`contested`. That replaces `engine_disagreement`, `numeric_disagreement` and
`unconfirmed_model_output` on that page, because the spans say exactly where the
readings differ. `verify-job` rebuilds the spans from the retained readings and fails if the
artifact differs. The report counts spans by kind, settlements, and the pages
they hold.

On a contested page, the coverage, low-confidence and blank holds of readings
the page no longer rests on are dropped: the model and the literal engine, and
the spans between them, have read what those readings missed. Holds on the two
readings themselves stay.

## Settle contested spans

Rules settle what the two readings alone decide. The rest is settled against the
page image, by an agent or a person, outside PageLedger:

```bash
pageledger adjudicate jobs/book --packets packets/book
# an adjudicator answers each packet
pageledger adjudicate jobs/book --decisions packets/book --reviewer agent:sol
```

`--packets` writes, for every page with open spans, `<page_id>.json`: both
readings, the open spans, and the exact image the model read (`<page_id>.jpg`),
with `INSTRUCTIONS.md`. The adjudicator answers in `<page_id>.decisions.json`:

```json
{"page_id": "doc_0001_page_0003", "contested_sha256": "…",
 "decisions": [{"span_id": "sp_9f…", "text": "поныне", "confidence": "high", "note": "print reads поныне"}]}
```

`text` is what the page prints at that place: the model's reading, the literal
one, other text, or nothing. A printed misprint is written as printed.
`--decisions` checks each answer against the spans it names and records it as an
adjudication receipt (schema 0.2) on the page, with the reviewer, the time and
the hash of the image. Answers for spans that have since changed are refused;
write packets again. A later answer for a span overrides an earlier one.

Only a `high` answer settles a span. When no span on a page is open, the page is
`adjudicated_text`, and, if any answer changed the text, PageLedger writes
`editions/<page_id>.txt`: the model's reading with each change applied at its
offsets. The attempt itself is never edited. The transcript, `document.json` and
every export use the edition and say so. `verify-job` rebuilds each edition from
its receipts and fails if the file differs.

`adjudicated_text` is not `reviewed_text`. It means every difference from the
literal reading was settled, by a rule or an adjudicator, or there was none; no
person has read the page. The report counts adjudicated pages on their own line,
and `unresolved_pages` keeps its meaning: pages without a person's review. A
person can answer packets too, under their own `--reviewer` name, or review the
page as before.

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
ordered child plans, attempt records, page selections/reviews, usage and status.
The checksum detects inconsistent content. It does not establish authorship
or prevent deliberate rewriting.

Child plans are saved before launch; their exact YAML snapshots are under
`.job/`. Runs under `attempts/` retain the ordinary generation-zero artifacts
and the resumable checkpoints. An attempt is identified by its run ID, child
path, attempt ID and source page; these are separate from rerun lineage IDs.
Existing run commands, selected denominators and generation-zero text replay
keep their contracts.

Before further calls, resume checks retained child records, outputs,
source/page identities and configuration snapshots. A child completed before
the job index was saved is adopted without extraction. The child runner recovers
a saved response that has not yet been published. A request without a saved
response becomes `outcome_unknown`; no automatic retry or fallback is allowed.
Typed provider failures, quota errors and truncated output stop the whole
queue. Failed partial output is kept under
`partials/` and linked in the report, but cannot be selected as the transcript.
Missing image input evidence also stops the job and excludes that response from
selection. The [checkpoint specification](checkpoint-spec.md) describes
individual-run recovery checks and limits.

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
resumed with a higher limit. A `halted` job keeps its records, and `resume`
reports it without retrying; settle any uncertain request with the provider
before starting a new job.
`verify-job` is read-only and binds the report back to validated job and child
records.

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
formula, and still match the saved job records it was written from, which
`review.csv.binding.json` records beside the sheet. A sheet written before the
job's records changed is refused; write a new one. `--dry-run` checks and
counts the decisions without recording them. Scripts can keep writing JSON
receipts.

### Review at scale

See the [warnings reference](warnings.md) for warning and disposition meanings
and suggested next steps.

`unresolved_pages` counts pages whose disposition is not `reviewed_text` or
`reviewed_blank`. A page remains unresolved when it is labeled `illustration`,
`handwriting`, `unreadable`, or `source_defect`: those decisions describe the
page, but do not establish that its text was checked or recover missing source
content. A completed job can therefore have unresolved pages.

For a long document, define and record a sampling policy before review. For
example, review every page with a warning or disagreement, then inspect a
fixed sample from each remaining section, including the first and last pages.
Record the section boundaries, sample rule, reviewer, and pages checked in the
review notes or project records. A sample does not make uninspected pages
reviewed; their dispositions and unresolved count remain unchanged. Use a
review sheet to collect decisions for sampled pages, but keep other rows with
blank decisions so those pages remain unreviewed.

## Image adapter boundary

The built-in [vision adapter](vision-adapter.md) reads a page image with a
model behind an OpenAI-compatible endpoint: on this machine, or over HTTPS
with `allow_remote: true`. In the image and second-opinion stages it keeps the
exact JPEG it sent, as described in [image evidence](image-evidence-spec.md):

```yaml
# fragment
processing:
  image:
    adapter: vision
    adapter_options:
      base_url: http://127.0.0.1:8080/v1
      model: YOUR_VISION_MODEL
```

A model writes its reading rather than reading it off the page. Unless a
clean reading from another engine agrees with it, the job holds that text for
review as `unconfirmed_model_output`; see [warnings and holds](warnings.md).

Core includes no provider SDK, OCR engine or pricing catalog.

Image-evidence runs currently refuse `bundle` with an explicit unsupported
error. They remain verifiable and resumable in place. Original text runs (not
reruns) can still be bundled and replayed. Whole-job portable bundles, synthesis, Zotero/Drive connectors, and file
retirement are outside the processing workflow.
