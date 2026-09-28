# Export document text

`pageledger export` writes the selected text of a document job page by page,
with each page's review state and the attempt that produced its text. It
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
receipt. Links to the source are relative to the exported file, so an export
never contains an absolute path such as your home folder.

## Text and Markdown

A heading names the source file, its SHA-256 and the job. Each page then
states its disposition, its review (reviewer, time and reason, or "none"), the
attempt and engine that produced the text, and the text's SHA-256, followed by
the text exactly as the engine returned it. Markdown also links each page to
the source page.

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

`review` is the page's decision from its
[review receipt](document-report-spec.md#human-review-receipt), or null. A page
without text has null `attempt` and `text`.

## TEI

A minimal TEI P5 document, which PageLedger's tests validate against the TEI
All schema of TEI P5 4.12.0:

- `sourceDesc` names the source file and its SHA-256; `encodingDesc` lists the
  engines behind the exported text and how many pages a person reviewed.
- Each page starts with `<pb n="12" facs="../scans/book.pdf#page=12"/>`,
  followed by `<div type="page" subtype="reviewed">` or
  `subtype="unreviewed"`.
- The text is in `<ab>`, with `<lb/>` between lines. A page without text
  holds `<gap reason="…"/>` naming its disposition, such as `pending` or
  `provider_failure`.
- Control characters, which XML cannot carry, are left out of TEI; the other
  formats keep the text unchanged.

PageLedger records engine confidence for whole pages, not single words, so
uncertain readings are not yet marked with `<unclear>`. PAGE and ALTO layout
exports are not available.

## Citing an export

Cite the source by its file name and SHA-256, and the text by job ID, page
number, attempt ID and output SHA-256. A job that verifies shows that the text
is exactly what the recorded engine returned and what a reviewer approved; it
does not show that the transcription is accurate.
