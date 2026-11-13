# Export document text

`pageledger export` writes the selected text of a job page by page,
with each page's review state and the reading that produced its text. It
verifies the job first and refuses one that does not verify.

```bash
pageledger export jobs/book --format txt --out book.txt
pageledger export jobs/book --format md --out book.md
pageledger export jobs/book --format jsonl --out book.jsonl
pageledger export jobs/book --format tei --out book.xml
pageledger export jobs/book --format tei --out reviewed.xml --reviewed-only
```

Every format keeps the source's page order and page boundaries, and lists
pages that have no text. `--reviewed-only` keeps only pages with a human review
receipt. A page a person reviewed as blank, an illustration, handwriting,
unreadable or a source defect is exported with its disposition and review but
without text, in every format: the reviewer did not accept the machine text as
the page. Links to the source are relative to the exported file, so an export
never contains an absolute path such as your home folder.

`--out` must be outside the job's folder and must not be the job's source. An
existing file is replaced only when it is an earlier export of the same job;
any other file is left alone and the export is refused.

## Text and Markdown

A heading names the source file, its SHA-256 and the job. Each page then
states where it stands (`State`, in the report's words with its code), its
review (reviewer, time and reason, or "none"), and the reading that produced
the text: what made it as the report names it (for a PDF whose text layer is an
earlier OCR, "the PDF's own text (an earlier OCR)"), its engine and its ID, then
its SHA-256. A page whose text is an edition (a reading with its settled
contested spans applied) names the edition and its SHA-256 as well. A page
whose text a person corrected says so first: its review line says who
corrected the text (or accepted it as corrected) and when, with their note; a
`Text from` line says whose correction of which reading the text is and how
many words changed; a `Correction` line names the corrected text's file and the
file it was corrected from, with the corrected text's SHA-256 and the SHA-256
of the text it was corrected from; and `Reading` lines give the reading
beneath it. The text that follows is the person's.

In the text format, a `Text` line gives the page text's length in characters
and lines, and the text follows after one empty line, exactly as recorded: a
line in the text that looks like a page heading cannot be taken for one. In
Markdown, each page links to the source page, and its text is in a fenced code
block, as in [the transcript](document-report-spec.md#report-format-09), so
that nothing in it is read as Markdown or HTML.

## JSONL

One object per line and page, described by
[`export-page.schema.json`](../schemas/export-page.schema.json). A reviewed
page, shown here across several lines:

```json
{
  "job_id": "job_2856780d4f884bd5a25550e4ea4265ba",
  "source_sha256": "a1a0218dab89cbd7…",
  "page_number": 12,
  "page_id": "doc_0001_page_0012",
  "disposition": "reviewed_text",
  "state": "reviewed",
  "review_reasons": [],
  "warnings": [],
  "attempt": {
    "attempt_id": "local_ocr:doc_0001_page_0012",
    "stage": "local_ocr",
    "adapter": "pdf_ocr",
    "model": "tesseract 5.5.0; pdftoppm version 25.01.0; dpi=300; lang=rus",
    "output_sha256": "34fca983f52e2918…"
  },
  "text": "…",
  "review": {
    "page_id": "doc_0001_page_0012",
    "page_number": 12,
    "disposition": "reviewed_text",
    "selected_attempt": "local_ocr:doc_0001_page_0012",
    "output_sha256": "34fca983f52e2918…",
    "reason": "checked against the scan",
    "reviewer": "A. Reader",
    "reviewed_at": "2026-09-11T12:00:00Z"
  }
}
```

`state` says where the page stands, by the report's definitions:
`needs_person` (the job holds it), `pending` (the job has not finished reading
it), `unreviewed` (nothing held it and no person checked it) or `reviewed`.
`review_reasons` lists the page's hold reasons and `warnings` the warning codes
on the reading the text comes from; see [warnings](warnings.md#in-plain-words).
The three fields were added in 0.9. `edition` is present only when the page's
text is the edition. The line separators U+2028, U+2029 and U+0085 are written
as `\u` escapes, so that each record is one line for any tool.

`review` is the page's decision from its
[review receipt](document-report-spec.md#human-review-receipt), or null. A page
without text, including one reviewed as anything but text, has null `attempt`
and `text`.

When a person corrected the page's text, `text` is the correction and an
extra `correction` object says so: `path` and `sha256` of the corrected text,
`from_path` and `from_sha256` of the reading it was made from (the attempt's
output, its edition, or an earlier correction), and `words` with the counts
`changed`, `added` and `removed`, and the `reviewer`, `reviewed_at` and
`reason` of the receipt that made the correction (a later acceptance does not
change who is credited). `attempt` still names the attempt beneath the
correction, and `review` carries the receipt's own `correction`; see
[corrected text](document-report-spec.md#corrected-text).

## TEI

A minimal TEI P5 document, which PageLedger's tests validate against the TEI
All schema of TEI P5 4.12.0:

- `sourceDesc` names the source file and its SHA-256; `encodingDesc` lists the
  readings behind the exported text, by what made them and their engine, and
  how many pages a person reviewed.
- Each page starts with `<pb n="12" facs="../scans/book.pdf#page=12"/>` (for
  a folder of page images, `facs="../scans/page-12.jpg"`),
  followed by `<div type="page" subtype="reviewed">` or
  `subtype="unreviewed"`.
- Each page division also points with `ana` to a category of a `<taxonomy>`
  in the header's `<classDecl>`, which says where the page stands:
  `#pageledger-needs-person` (the job holds it for a person),
  `#pageledger-pending`, `#pageledger-unreviewed` or `#pageledger-reviewed`.
  A held page opens with one `<note type="hold" n="coverage_defect">` per
  hold reason, and any page with text with one `<note type="warning" n="…">`
  per warning on its reading, each in plain words. A TEI reader can so tell
  the pages the job distrusts from those nothing held, though both are
  `subtype="unreviewed"`.
- A page with text has a `<note type="provenance">` naming the reading (or
  edition or correction) its text comes from, with its ID, engine and the
  SHA-256 of the text before characters XML cannot carry were left out, and
  the reviewer and time when a person reviewed it.
- The text is in `<ab>`, with `<lb/>` at each line break (LF, CR or CRLF);
  other separators such as U+2028 are kept as characters. A page without text
  holds `<gap reason="…"/>` naming its disposition, such as `pending`,
  `provider_failure` or, for a page a person reviewed as not text,
  `unreadable` or `reviewed_blank`.
- A page whose text a person corrected keeps `subtype="reviewed"` and points
  with `change="#correction-12"` to a `<change>` in the header's
  `<revisionDesc>`. The change has `when` (when the correction was made) and
  says who corrected the page, from which reading, how many words changed, and the
  reviewer's reason.
- Control characters, which XML cannot carry, are left out of TEI; the other
  formats keep the text unchanged.

PageLedger records engine confidence for whole pages, not single words, so
uncertain readings are not yet marked with `<unclear>`. PAGE and ALTO layout
exports are not available.

## Citing an export

Cite the source by its file name and SHA-256, and the text by job ID, page
number, reading ID and its SHA-256 (the `Reading` lines, or `attempt` in JSONL); for
an edition or a corrected page, add the edition's or the correction's SHA-256. A job that verifies shows that the text is exactly what
the recorded engine returned, or what the named reviewer typed, and what a
reviewer approved; it does not show that the transcription is accurate.
