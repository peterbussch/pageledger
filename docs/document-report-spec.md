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
| `report_format` | `"0.9"`: `report.md` is written for a reader (see [Report format 0.9](#report-format-09)), the report records `engines`, pages carry `hold_causes`, and `transcript.md` names each selected reading in words; the source is named as in 0.6. `"0.6"`: the source is named by its path relative to the job directory. `"0.5.1"`: by its absolute path. Absent on 0.5.0 reports, which use the legacy renderer. |
| `job_id`, `created_at` | Job identity and creation timestamp |
| `source` | Original `path`, exact byte `sha256`, `page_count`, and annotation inventory; for page images also `kind` and the page manifest `images`, and `sha256` is the manifest's hash; for a IIIF snapshot also `iiif` (see [durable job and attempts](processing-spec.md#durable-job-and-attempts)) |
| `selected_pages` | Unique, one-based source page numbers requested for this job |
| `status` | `processing` (not finished: interrupted or still running), `completed`, `halted`, or `paused_budget`; completion does not mean human review |
| `pages` | The selected-page inventory, including pages without attempts or selected text |
| `usage` | `attempt_pages`, `image_calls`, `tokens`, `cost_usd`, and `cost_known`; optional `known_cost_usd`, `tokens_known`, `paid_cost_known`, `paid_tokens_known`, `local_image_calls` and `image_requests` (jobs with `processing.tiling`) |
| `limits` | The job's recorded execution limits |
| `stage_accounting` | Optional configured-stage attempt counts; a stage with zero pages includes why it did not run. Absent from older reports, which still verify with their original fields. |
| `engines` | Report format 0.9. For each configured stage, the engine it ran, copied from the job's policy so that `report.md` and the review view can name each reading by what made it: `adapter`, and `lang` for `pdf_ocr` (its Tesseract language, `eng` when the config names none) or `model` for `vision` (the model the stage asked for; the model the endpoint served is in the attempt's image evidence). No other adapter option is copied. Absent from older reports. |
| `links` | Nullable `article` and `custody` references supplied by the job |
| `relocations` | Optional. Each `review --apply` or `adjudicate --decisions` change made to the job outside the directory it was written in: `root` (where it was made), `command` (`review-job`, the earlier name of `review --apply`, or `adjudicate`), and `at`. Rendered as a "Changed elsewhere" line, which names `review --apply` for `review-job`. Absent until such a change. |
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

Jobs written by 0.9 render `report.md` as the citable record of how the text was
made, with a short summary on top for a reader who has not seen this
specification. The [review view](processing-spec.md#review-beside-the-page-image)
is where a person works through the pages; the report says where they stand and
records how each was read. Two rules hold throughout the parts a person reads
first (the opening, "Worth knowing" and the page table):

- **Readings are named by what made them**, from the report's `engines`: "the
  PDF's own text" (`pdf_text`; "(an earlier OCR)" when the layer carries
  `ocr_text_layer`; "the page image's own text" for page images), "the text file"
  (`text`), "Tesseract OCR (rus)" (`pdf_ocr` with its language), "RapidOCR",
  "Indra OCR", "the page's ALTO file", "the vision model MODEL" ("the local
  vision model MODEL" at `local_image`), "the engine NAME" for a custom adapter,
  and "a person's correction". A reading at `second_ocr` is "the second OCR
  engine (…)", at `second_opinion` "the second opinion (…)". A report without
  `engines` names the stage in words ("the OCR engine"). Attempt IDs appear only
  in the record, where each is the link to its reading's text.
- **Plain words, codes once.** Holds and page states are given in the words of
  `pageledger/wording.py`; their codes are in the record, once per page. A page
  whose selected reading has text and that another reason holds is not also
  called blank there: the record still lists `blank_candidate`. Held only as a
  blank candidate with text, the page reads "The page is a scan with no text of
  its own, and no other reading confirms the OCR" when the empty reading is the
  page's own text (a scanned PDF or a page image), and otherwise "An earlier
  reading was empty, and no reading without a hold of its own confirms the
  page's text".

Its sections, in order:

1. **Title and opening.** The source's file name; the job's state in plain
   words: finished; paused at a limit, with the limit and the command that
   raises it; halted, with the reason the job recorded; or not finished
   (interrupted or still running). Then the source, linked once, how many pages
   were selected and read, and the pages grouped by where they stand, the group
   with most pages first: the pages that need a person, under each hold shown;
   the pages with text that nothing held; the pages a person reviewed, under
   each decision; and the pages not finished, under the engine they wait for.
   The opening ends with the one next command. Commands are written to be run
   in the job's folder (`pageledger review .`, `pageledger resume .`,
   `pageledger export . --format md --out ../NAME.md`), because a report cannot
   name its own folder after the job is moved; a paused job's recorded next
   action names the folder as `JOB_DIR`, which the opening shows as `.`.
2. **Worth knowing.** Each warning that holds no page, once, with the pages it
   is on, its code, its meaning and what to do about it (for
   `historical_orthography`: that stock modern-Russian models cannot write ѣ, і,
   ѳ or ѵ, and where the engine recipes are).
3. **Pages.** One table of every page, those that need a person first, then those
   not finished, then the rest: what holds the page (or "nothing"; for a page not
   finished, the engine it waits for and its holds so far), its selected text
   (the reading that made it, linked to the text; a correction with the words it
   changed; an edition with its settled differences applied), and its review
   (the decision in words, who made it, when, and why). Pages not finished are
   marked as provisional.
4. **For the record.** Everything below is in `document.json`:
   - job identity, hold policy and report format; the source path and SHA-256
     (and the IIIF snapshot record); which engine each stage ran, with its
     adapter; the page counts, with `unresolved_pages` under its own name; the
     corrected pages;
   - **Each page**, one line per page: the page number linked to the source page,
     its `disposition` and every hold reason the job recorded, the review decision
     code; then each reading by its attempt ID, linked to its text, with its
     outcome, failure code, warning codes, tile counts and whether it is the
     selected one; then the correction (who, when, and the correction it revised)
     or edition, the open contested spans, every comparison the job recorded with
     the share of words that agree, how many numbers differ and whether the job
     counted it, and any hold the hold policy placed (from `hold_causes`) that no
     warning on the reading already names. A comparison is counted by the rules
     the job's hold policy used: one with a reading that is held itself is not
     counted, nor one with an earlier OCR layer that the policy sets aside or that
     two of the job's own engines outvote. A comparison below 60% is called a
     disagreement only on a page held for one;
   - **How the job ran**: work by stage, with the reason a configured stage read
     nothing, and the triggers that sent pages on; contested-span totals;
     readings made, image-model calls (and image requests in tiled jobs), local
     image-model readings, tokens and cost (a cost or token count that not every
     engine reported is unknown, with the reported part beside it; a job that
     read nothing spent nothing); limits and limit raises; the PDF annotation
     inventory; changes made elsewhere;
   - **Source custody**; **Files**: links, the transcript and its hash, and the
     next action the job recorded.

`transcript.md` of a format 0.9 job names each page's selected reading the same
way ("Selected reading: Tesseract OCR (rus)"), and a page without selected text
by its state in words. Older formats keep their transcript as written.

A page **needs a person** when no one has reviewed it, it is not waiting for a
stage, and it has a hold reason or no selected text. A page is **not finished**
when it was never read, or when the job has not completed and the page's next
step is a configured stage. These two definitions are the ones `process`,
`inspect`, the review view and the JSONL and TEI exports use.

`process` prints its own ending, not the report's opening: where the job is,
how many pages were read and need a person, the commonest reasons in words, the
next command (`pageledger review JOB`, or `pageledger resume JOB` for a job that
did not finish) and the export command, with no codes.

The words for every code come from one table, `pageledger/wording.py`, which
[warnings.md](warnings.md#in-plain-words) repeats. The report of a real
two-page job, a scan whose first page is blank and whose second carries an
earlier OCR layer, read with Tesseract in English:

```markdown
# book.pdf: document job report

Finished: every stage this job is configured to run has run.

Source: [book.pdf](<../../book.pdf>); 2 of its 3 pages selected, 2 read.

- Need a person: 1 page (1):
  - No reading found text, or one came back empty: the page may be blank: 1 page (1).
- Text selected and nothing held the page; no person has checked it: 1 page (2).

Next: in this job's folder, `pageledger review .` writes the review view, where you check the page that needs a person beside its page image and record what you decide.

## Worth knowing

These warnings hold no page, but they bear on how far to trust the text.

- On 2 pages (1-2) (`render_upsampled`): The page was rendered at more pixels than its source image has; the extra pixels add no detail. What to do: Judge the reading by the source image, and look for a higher-resolution copy of the page.
- On 1 page (2) (`ocr_text_layer`): The PDF's text layer is an earlier OCR of the scan, not typeset text. What to do: Treat the layer as one OCR reading among others; where the job read the page again, it compared the two.

## Pages

Pages that need a person come first. The review view (`pageledger review .` in this job's folder) shows each page beside its image, with every reading of it, and is where decisions and corrections are made.

| Page | Held for | Selected text | Review |
| --- | --- | --- | --- |
| 1 | No reading found text, or one came back empty: the page may be blank | none | not checked |
| 2 | nothing | [Tesseract OCR (eng)](<attempts/local_ocr-0002/raw/doc_0001_page_0002.txt>) | not checked |

## For the record

Everything below is in document.json, of which this report is a rendering. The codes in backticks are explained in PageLedger's docs/warnings.md.

- Job: `job_054421fc6021478b9af15276d91f32e4`, created 2026-11-11T03:52:33Z; hold policy 0.9; report format 0.9.
- Source: [../../book.pdf](<../../book.pdf>)
- Source SHA-256: `02ddb0de5ff5e25b55a359b16a22b7427cb0ce3525d60e37bec357feff257855`
- Read by: the `local_text` stage, the PDF's own text (`pdf_text`); the `local_ocr` stage, Tesseract OCR (eng) (`pdf_ocr`).
- Pages: 2 selected of 3 in the source; 2 read; 1 with selected text; 2 not yet reviewed as text or blank (`unresolved_pages`).

### Each page

Each page's state and every hold the job recorded; then each reading, by its ID (a link to its text), with its outcome, failure and warnings; then the comparisons the job recorded and whether it counted them, and any hold its hold policy placed.

- Page [1](<../../book.pdf#page=1>): `blank_candidate`; held for `blank_candidate`. Readings: [local_text:doc_0001_page_0001](<attempts/local_text/raw/doc_0001_page_0001.txt>) completed, `empty_text`; [local_ocr-0001:doc_0001_page_0001](<attempts/local_ocr-0001/raw/doc_0001_page_0001.txt>) completed, `render_upsampled`, `empty_text`.
- Page [2](<../../book.pdf#page=2>): `unreviewed_text`. Readings: [local_text:doc_0001_page_0002](<attempts/local_text/raw/doc_0001_page_0002.txt>) completed, `ocr_text_layer`; [local_ocr-0002:doc_0001_page_0002](<attempts/local_ocr-0002/raw/doc_0001_page_0002.txt>) completed, `render_upsampled`, selected. Compared: `local_text:doc_0001_page_0002` and `local_ocr-0002:doc_0001_page_0002`: 98% of words agree; counted as agreement.

### How the job ran

| Stage | Pages read | Tokens | Seconds |
| --- | --- | --- | --- |
| `local_text` | 2 | not reported | 0 |
| `local_ocr` | 2 | not reported | 2.7 |

Readings sent on to the next stage for a reason other than a hold, by trigger (a page can be sent on more than once): `ocr_layer` 1.

- Readings made: 4 (one per page and stage).
- Calls to an image model: 0.
- Tokens: unknown: not every engine reported them (reported: 0).
- Cost: unknown: not every engine reported a cost.
- Limits: max_image_pages 0.
- PDF annotations (notes and links attached to pages): none (0). Counting them says nothing about whether the body text or its citations were extracted completely.

### Source custody

PageLedger records what it knows of the source file; it does not preserve or remove it.

- Captured (the file was present when the job ran): present
- Preservation elsewhere verified: unverified
- Assessed for removal: not_assessed
- Removed: no

### Files

- Transcript: [transcript.md](<transcript.md>), SHA-256 `1bc450b926ac75b32e8e7e13428fb8f59940988f70a0c541e93deaf2955d0406`
- Next action recorded by the job: Check the pages against the source, those that need a person first, and record decisions with pageledger review.
```

The status line comes first because it changes what the rest means. The groups
under it count pages, and a page held for two reasons is named under both. The
table says, for each page, what holds it and which reading is its text; the
record says the same with every code and ID, so a reader can look the codes up
in [warnings.md](warnings.md) and follow each ID to its reading. Page 2's
earlier OCR layer and Tesseract's reading share 98% of their words, so the job
counted the comparison as agreement and selected Tesseract's reading.

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
