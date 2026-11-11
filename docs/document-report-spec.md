# Document report and human review receipts

Schema version: `0.1`. The schemas are
[`document.schema.json`](../schemas/document.schema.json) and
[`job-review.schema.json`](../schemas/job-review.schema.json).

`document.json` is the authoritative document report. `report.md` and
`transcript.md` are deterministic renderings of that JSON. The saved job state
and retained attempt evidence are the sources for reconstructing the report. A report does not certify transcription accuracy or source preservation.

The report shows the selected text, each extraction attempt, review status,
concerns, and the next action. `unresolved_pages` counts pages not explicitly
reviewed as text or blank. For meanings and suggested actions, see the
[warnings reference](warnings.md).

## Document fields

| Field | Meaning |
| --- | --- |
| `schema_version` | `"0.1"` |
| `report_format` | `"0.9"`: `report.md` is written for a reader (see [Report format 0.9](#report-format-09)) and pages carry `hold_causes`; the source is named as in 0.6. `"0.6"`: the source is named by its path relative to the job directory. `"0.5.1"`: by its absolute path. Absent on 0.5.0 reports, which use the legacy renderer. |
| `job_id`, `created_at` | Job identity and creation timestamp |
| `source` | Original `path`, exact byte `sha256`, `page_count`, and annotation inventory; for page images also `kind` and the page manifest `images`, and `sha256` is the manifest's hash; for a IIIF snapshot also `iiif` (see [durable job and attempts](processing-spec.md#durable-job-and-attempts)) |
| `selected_pages` | Unique, one-based source page numbers requested for this job |
| `status` | `processing` (not finished: interrupted or still running), `completed`, `halted`, or `paused_budget`; completion does not mean human review |
| `pages` | The selected-page inventory, including pages without attempts or selected text |
| `usage` | `attempt_pages`, `image_calls`, `tokens`, `cost_usd`, and `cost_known`; optional `known_cost_usd`, `tokens_known`, `paid_cost_known`, `paid_tokens_known`, `local_image_calls` and `image_requests` (jobs with `processing.tiling`) |
| `limits` | The job's recorded execution limits |
| `stage_accounting` | Optional configured-stage attempt counts; a stage with zero pages includes why it did not run. Absent from older reports, which still verify with their original fields. |
| `links` | Nullable `article` and `custody` references supplied by the job |
| `relocations` | Optional. Each `review --apply` or `adjudicate --decisions` change made to the job outside the directory it was written in: `root` (where it was made), `command` (`review-job`, the earlier name of `review --apply`, or `adjudicate`), and `at`. Rendered as a "Changed elsewhere" line. Absent until such a change. |
| `source_retention` | Explicit source capture, preservation, removal eligibility, and removal state |
| `next_action` | One human-readable action for the document |
| `counts` | `source_pages`, `selected_pages`, `processed_pages`, `selected_outputs`, and `unresolved_pages`; `adjudicated_pages` in contested jobs; `corrected_pages`, the pages whose selected text is a person's [correction](#corrected-text), only when there is one |
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

Each `stage_accounting` entry names a configured stage, counts its attempts,
and gives a reason when no page reached it. A zero-page stage reports no pages
selected, a reached attempt or stage limit, a prior job halt, or that no page
met its trigger. The Markdown report renders this count and reason in its
stage-work table.

The source retention values in this version are `capture: "present"`,
`preservation: "unverified"`, `removal_eligibility: "not_assessed"`, and
`removed: false`. Extraction and report generation do not move or remove the source.

## Page and attempt evidence

Each page includes `page_id`, `page_number`, `source_sha256`, `attempts`,
`selected_attempt`, `disposition`, `review_reasons`, nullable `review`, and
`next_action`. The report adds `source_link` and nullable `selected_output`.
`selected_output` contains the chosen `attempt_id`, relative artifact `path`,
`sha256`, `format`, and exact `text`.

Reports in format 0.9 add `hold_causes` to a page whose readings are held: one
entry per hold on each reading, with `attempt_id`, `hold` and `cause`. The cause
is the warning or classification code that files the hold, `empty_reading` (a
reading with no text) or `alignment` (the table check); or a hold the
[hold policy](processing-spec.md#hold-policy-09) places itself:
`layer_outvoted` (two later readings agree with each other better than with the
PDF's text layer), `reader_short` (a model's reading has under half the words of
the OCR) or `letters_lost` (a literal reading spells far fewer words with ѣ than
another reading of the page; `yat_words` and `most_yat_words` give the two
counts). The job derives them from the same steps that set its holds, while the
readings' text is at hand, so that the report can name every hold's evidence; it
does not change what the job selects or holds. Absent when no reading is held,
and from reports in older formats.

Each attempt records `attempt_id`, `stage`, `outcome`, nullable `raw_artifact`
and `raw_sha256`, `format`, `warnings`, `classification`, nullable `alignment`,
`usage`, and nullable `failure`. Stages are `local_text`, `local_ocr`, `image`,
and `second_opinion`. Outcomes are `completed`, `failed`, `outcome_unknown`,
and `response`. A retained response awaiting completion is still uncertain.
Failed partial outputs remain linked as attempt evidence and cannot be selected.
Child-run attempts also retain `run_path`, `run_id`, `page_id`, `page_number`,
`source_sha256`, and nullable `input_evidence` for image preparation provenance.
An attempt whose engine returned a layout adds `layout_sha256`, and a reading in
tiles adds `tiling`: the plan's `plan_sha256`, the `layout_attempt` it was cut
along, how many `tiles` were planned and `sent`, and the ids of the tiles that
`failed` or whose edge cut through text (`edge_cut`). The report's attempt line
then says how many tiles were planned, sent (and so paid for) and read, and names those tiles, and its usage line
adds the image requests (`usage.image_requests`). A contested page read in tiles
adds `contested.witness_projection`. Each is present only when it applies, so
older jobs render as before.

For a selected output, the report decodes its verified raw bytes as UTF-8 and
embeds the resulting string without Unicode normalization, whitespace trimming,
newline conversion, or JSON/table reformatting. The transcript adds page headings,
source links, selected-attempt links, and page separators around those strings.
Source paths are percent-encoded before adding the `#page=N` fragment, so
filename characters such as `#`, `?`, and `%` remain part of the path. For page
images the link goes to the page's own image instead, `folder/page-12.jpg`,
with no fragment, and the page's file name follows its number in the link text:
`9 (0081.jpg)` in a `report.md` row and `Source page (0081.jpg)` in the
transcript, because a folder numbers pages by position and the file name may
carry another number. For a IIIF snapshot `report.md` adds, after the source
hash, a line naming the requested URL and whether the snapshot was complete,
and the fetch record's SHA-256. Both additions appear only for sources that
record page images, so reports of other jobs render as before.
It also lists pages without selected text. The transcript hash covers that whole
Markdown serialization, including the added material and final newlines.

## Selection and escalation

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
`suspicious_symbol_density`, `instruction_echo`, `digits_only_text`,
`mixed_script_tokens`, `private_use_characters`, `foreign_script_characters`
and `repeated_page_text` quality warnings also create coverage holds even when the output is classified
as prose or has a high grade. `low_confidence` creates its own hold, reported
as "The engine was unsure of some words": the engine doubted its reading, which
is not evidence of missing content. Both kinds of hold escalate to the next
stage. An explicit quality warning is evidence requiring review; numeric model
confidence does not rank candidates. `historical_orthography` alone does not
force a rewrite or extraction escalation.

Jobs record the mapping they used as `hold_policy`. Under `"0.6.1"`, a blank
candidate raised only because an engine returned no text is settled when another,
non-generative engine reads clean text from the page: a scan with no text layer
that OCR reads without concern is not held as a possible blank. An engine's own
judgement that a page is blank is never settled this way. Jobs marked `"0.6"` kept that hold. Jobs marked `"0.8"` add the ladder rules in [processing](processing-spec.md#climb-on-roughness-and-disagreement); jobs marked `"0.9"` keep an inherited OCR layer as a witness and add the rules in [hold policy 0.9](processing-spec.md#hold-policy-09). Jobs and reports written before 0.6
have no `hold_policy`; they filed `low_confidence` under `coverage_defect`.
Verification rebuilds every job with the mapping it recorded, so older jobs
still verify unchanged.

Numeric comparison uses records from alignment or explicitly structured JSON,
CSV, and Markdown tables. It compares numeric cells under the same named columns
and aligned row identities. Unique nonnumeric row labels align reordered records;
otherwise all-numeric rows are compared in their supplied order. Unsupported or
ambiguous structures are not silently interpreted. Equal totals or equal numeric
multisets cannot clear changed column associations. A successful comparison is
not proof of fidelity to the source.

Dispositions are `pending`, `unreviewed_text`, `coverage_defect`,
`low_confidence`, `numeric_column_conflict`, `blank_candidate`, `reviewed_blank`, `reviewed_text`,
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
| `correction` | Optional, only on `reviewed_text`: the person's [corrected text](#corrected-text) |

After inspecting a page against its source, create `reviewed-pages.json`:

```json
{
  "schema_version": "0.1",
  "source_sha256": "<source.sha256 from document.json>",
  "decisions": [
    {
      "page_id": "doc_0001_page_0001",
      "page_number": 1,
      "disposition": "reviewed_text",
      "selected_attempt": "<page.selected_output.attempt_id>",
      "output_sha256": "<page.selected_output.sha256>",
      "reason": "Checked the selected text against source page 1.",
      "reviewer": "<reviewer identity>",
      "reviewed_at": "2026-09-11T14:00:00Z"
    }
  ]
}
```

Replace the angle-bracket placeholders with values from the job report and the
reviewer's identity. Set the page, reason, and timestamp to the review performed.
Then apply and verify it:

```bash
pageledger review jobs/book --apply reviewed-pages.json
pageledger verify jobs/book
```

`reviewed_text` always requires a completed artifact and its matching output
digest. Other dispositions allow a null selection, including preflight confirmation
of blankness, illustration, or source damage before any extraction call. A nonnull
selection always requires the exact completed artifact hash. A receipt cannot
bind a failed or unknown partial response. The report checks the source, page,
output, and required reviewer fields; the job layer validates
the complete envelope and duplicate-page inventory. `page.review` stores this
envelope, commonly with only the decision for that page.

Review changes the decision, not the evidence. Original attempts, warnings,
classification and alignment evidence, and accumulated `review_reasons` remain.
Optional `page.review_history` retains the ordered review envelopes when a human
replaces a decision. The active `page.review` equals the latest receipt; earlier
receipts remain source/page/output-bound evidence. Applying the same latest
receipt again does not append a duplicate. Reports preserve the full history in
`document.json` while the human summary displays the active decision.

### Corrected text

When a reviewer finds the text wrong, the job does not edit the machine
reading: it keeps the reviewer's corrected text as a new reading beside it,
bound by a receipt to the reading it was made from. Anyone can then see what
the engine read, what the person changed, and who changed it when, and a
later reader can check both against the page.

The decision is `reviewed_text`. Its `selected_attempt` and `output_sha256`
keep their meaning: the attempt and the output (its own text, or its edition)
the page's text descends from. The optional `correction` object adds:

| Field | Binding |
| --- | --- |
| `artifact` | `corrections/<page_id>-<sha256>.txt`, the corrected text as UTF-8 |
| `sha256` | SHA-256 of that file, which is also its name |
| `from_sha256` | SHA-256 of the text the correction started from: `output_sha256`, or the `sha256` of a correction recorded earlier for the same page |
| `words` | `changed`, `added` and `removed`: whitespace-separated words compared exactly, counted when the correction was recorded |

```json
"correction": {
  "artifact": "corrections/doc_0001_page_0003-9c1f…e2.txt",
  "sha256": "9c1f…e2",
  "from_sha256": "34fc…a9",
  "words": {"changed": 1, "added": 0, "removed": 0}
}
```

A correction of a correction is a chain: the later receipt's `from_sha256` is
the earlier correction's `sha256`, and both receipts stay in
`review_history`. A file name is its content's hash, so no correction
overwrites another. Accepting a corrected page records a receipt with the same
`correction`, so acceptance never quietly returns the page to the machine
reading; `use:ATTEMPT` does that, explicitly. Verification checks every
correction any receipt in a page's history names: the file must exist with its
recorded hash, and each must start from the receipt's output or an earlier
correction. A changed, missing or unanchored correction fails `verify`.

In `document.json`, a corrected page's `selected_output` has the corrected
text: `path` and `sha256` name the correction file, `attempt_id` is still the
attempt beneath it, and an optional `correction` object gives `from_path` (the
attempt's output, its edition, or the earlier correction), `from_sha256`,
`words`, and the `reviewer`, `reviewed_at` and `reason` of the receipt that
first recorded the correction. A later `accept` carries the correction on in a
receipt of its own, but the correction stays credited to the person who made
it. `edition` is then absent. `report.md` shows the page's selected text as
"Correction by a person of" the attempt, with the word counts, who made it and
when, and, for a chain, a link to the correction it revised, and adds a line "Pages whose text
a person corrected: N." `transcript.md` labels the page "Correction by a person
of attempt …". None of these fields or lines appear in a job without a
correction, so earlier jobs rebuild byte for byte.

## Human summary rendering

### Report format 0.9

Jobs written by 0.9 render `report.md` for a reader who has not seen this
specification: what happened and what to do first, every page that needs a
person with the evidence for its holds, and the ledger detail last. Its
sections, in order:

1. **Title and opening.** The source's file name, then the job's status in
   plain words: finished; paused at a limit, with the limit and the command
   that raises it; halted, with the reason the job recorded; or not finished
   (interrupted or still running). Then how many pages were selected and read,
   and the pages grouped by where they stand, the group with most pages first:
   the pages that need a person, under each hold reason; the pages with text
   that nothing held; the pages a person reviewed, under each decision; and the
   pages not finished, under the stage they wait for. Each group names its page
   numbers. The opening ends with the one next command. Commands name the job
   folder as `JOB_DIR`; `process` prints the same opening with the real folder.
2. **Pages that need a person.** One subsection per page. Each hold reason is
   given in plain words with its code, followed by its evidence: the warning on
   the reading that filed it (code and meaning), the comparison that fell below
   the agreement threshold, the open contested spans, a hold the policy placed
   itself (from `hold_causes`), or the latest failed attempt. Then the selected
   text, and every comparison the job recorded on the page: which two readings,
   the share of words that agree, how many numbers differ, and whether the job
   counted it, by the rules the job's hold policy used: a comparison with a
   reading that is held itself is not counted, nor one with an earlier OCR
   layer that the policy sets aside or that two of the job's own engines
   outvote. A comparison below 60% is called a disagreement only on a page held
   for one. On a held page the comparisons are introduced as not clearing the
   holds.
3. **Pages not finished.** In a job that has not finished, the pages still
   waiting for a configured stage, the stages that have read them so far, and
   their selected text and holds so far, marked as provisional.
4. **Worth knowing.** Each warning that holds no page, once, with the pages it
   is on, its meaning and what to do about it (for `historical_orthography`:
   that stock modern-Russian models cannot write ѣ, і, ѳ or ѵ, and where the
   engine recipes are).
5. **Other pages.** A table of the pages that need no one now: selected text,
   the lowest word agreement the job weighed, the holds a review settled (or
   `nothing`), and any review with its reviewer, time and reason.
6. **Usage and cost.** Readings made, image-model calls (and image requests in
   tiled jobs), local image-model readings, tokens and cost. A cost or token
   count that not every engine reported is unknown, with the reported part
   beside it; a job that read nothing spent nothing.
7. **For the record.** Job identity, hold policy and report format; source path
   and SHA-256 (and the IIIF snapshot record); the page counts, with
   `unresolved_pages` under its own name; corrected pages; the PDF annotation
   inventory; limits and limit raises; changes made elsewhere; work by stage
   with the reason a configured stage read nothing, and the triggers that sent
   pages on; contested-span totals; every reading with its stage, outcome,
   failure code, warning codes and tile counts; source custody; links; the
   transcript and its hash; and the next action the job recorded.

A page **needs a person** when no one has reviewed it, it is not waiting for a
stage, and it has a hold reason or no selected text. A page is **not finished**
when it was never read, or when the job has not completed and the page's next
step is a configured stage. These two definitions are the ones `process`,
`inspect`, the review view and the JSONL and TEI exports use.

The words for every code come from one table, `pageledger/wording.py`, which
[warnings.md](warnings.md#in-plain-words) repeats. A shortened example, for a
job with one clean page and one held page:

```markdown
# source.txt: document job report

Status: finished. Every stage this job is configured to run has run.

Source: source.txt; 2 of its 3 pages selected, 2 read.

- Need a person: 1 page (2):
  - Text may be missing or damaged in the reading (`coverage_defect`): 1 page (2).
- Text selected and nothing held the page; no person has checked it (`unreviewed_text`): 1 page (1).

Next: `pageledger review JOB_DIR --out source-review` shows the page that needs a person beside the page images.
JOB_DIR stands for the folder that holds this report.

## Pages that need a person

### Page [2](<../source.txt#page=2>)

- Text may be missing or damaged in the reading (`coverage_defect`).
  - `coverage_defect` on the Local text reading [local_text:doc_0001_page_0002](<attempts/local_text/raw/doc_0001_page_0002.txt>): Text may be missing from the reading.
- Selected text: the Local text reading [local_text:doc_0001_page_0002](<attempts/local_text/raw/doc_0001_page_0002.txt>).
- Comparisons the job recorded (they do not clear the holds above):
  - The Local text reading `local_text:doc_0001_page_0002` and the Local OCR reading `local_ocr-0002:doc_0001_page_0002`: 100% of words agree; not counted, because the Local OCR reading `local_ocr-0002:doc_0001_page_0002` is held itself.
```

The status line comes first because it changes what the rest means. The groups
under it count pages, and a page held for two reasons is named under both. The
held page's code, `coverage_defect`, is the one to look up in
[warnings.md](warnings.md); the warning under it says which reading raised it.
The comparison agrees, but the reading it agrees with is held too, so the job
did not count it.

### Older formats

Reports marked with `report_format` `"0.5.1"` retain separate page-summary
columns for `Current output`, `Review status`, and `Recorded concerns`. Current
output links to the selected completed artifact and identifies its extraction
stage. Review status describes whether source review is pending or records the
bound human receipt, including source-only decisions. Recorded concerns retain
the readable meaning of every stored review reason. A concern is attributed to
an attempt only when that attempt records matching warning, classification, or
alignment evidence; otherwise it is shown as a retained review concern without
inventing an origin. An explicit `empty_text` warning is rendered as empty text
returned by the named extraction stage.

Reports marked `"0.6"` list first the pages that need a person, then the rest,
each group in page order. A page needs a person when no one has reviewed it and
it has a review reason or no selected text; the line above the table says how
many pages that is. The table shows four facts separately:

- **Selected text**: the stage and attempt the policy selected, linked to its
  output, or `none`.
- **Engine agreement**: `agree` or `disagree`, with the lowest word agreement
  between the selected reading and another clean one, or `not compared`. See
  [Compare engines on a page](processing-spec.md#compare-engines-on-a-page).
- **Human review**: who reviewed the page and when, with the recorded
  disposition and, for a source-only decision, its reason; or `not reviewed`.
- **Recorded concerns**: as in the 0.5.1 layout.

Under the table the report says that engine agreement is evidence, not proof:
engines can share a mistake.

These labels describe presentation only. They do not change `selected_attempt`,
`disposition`, `review_reasons`, review receipts, or the unresolved-page count.
For example, an OCR output can be the current selected output while a prior
empty native-text attempt remains a recorded `blank_candidate` concern awaiting
source review. Failed and unknown latest attempts remain visible even when a
previous completed output is selected. A source-only human receipt is shown as
such and does not acquire a selected output.

Reports marked `"0.6"` name the source by its path relative to the job
directory it was written in (`root` in `job.json`), in `source.path` and in
every page link, so a shared report does not reveal where its owner keeps
files, and the report reads the same after the job is moved. A source on another Windows drive is named
by its file name alone. `job.json` keeps the absolute path, which verification
needs. Reports marked `"0.5.1"` keep the absolute path and still verify.

Reports without `report_format` use the 0.5.0 renderer. `verify_document_report`
and `verify_job` select that renderer for those reports so existing report bytes
remain verifiable; marked reports select their versioned renderer.

## For adapter authors

`assess_page(page, review=None)` returns `selected_attempt`, `disposition`,
`review_reasons`, and `next_action`. The caller supplies decoded raw text as the
transient `text` field when assessing completed attempts; it is not persisted in
job attempt metadata. Report generation reads and verifies raw files separately.

`validate_review(review, page)` checks source, page, output, and reviewer fields.

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
