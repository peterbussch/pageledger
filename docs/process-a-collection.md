# Process a collection

This guide takes a folder of documents from a small test to reviewed exports.
A document job processes one source file and keeps the attempts, decisions and
limits for that document. Repeat the job for each file; PageLedger does not
combine a folder into one job. See [processing](processing-spec.md) for the
complete config and artifact rules.

## 1. Inventory and choose a representative sample

Keep the source files unchanged. Record their names, page counts, formats and
known problems. PageLedger accepts PDF and text inputs for its built-in paths.
A folder of TIFF, JPEG or PNG page images has no built-in processing path; see
[Image-only collections](#image-only-collections) below.

Choose a few documents that represent the collection, including difficult
layouts and languages. Start with a short range of pages from one file. The
`--pages` option selects source page numbers and preserves them in the ledger:

```bash
pageledger process scans/volume.pdf --config processing.yml \
  --pages "1-5,41-45" --out jobs/volume-sample
pageledger verify-job jobs/volume-sample
```

Inspect the report, transcript, attempts and retained page evidence. A sample
can reveal poor OCR, missing language packs, or a need for a different stage.
For Russian and historical material, see [multilingual OCR](multilingual-ocr.md)
and [choosing OCR settings](choose-ocr-settings.md).

## 2. Dry-run the full document

After the sample, plan a representative full-source run without extraction.
`--dry-run` belongs to `run`, which writes a route map and run-planning
artifacts; `process` has no dry-run mode:

```bash
pageledger run scans/volume.pdf --adapter pdf_ocr \
  --out runs/volume-plan --dry-run
pageledger verify-run runs/volume-plan
```

A dry run writes the route and planning artifacts, but does not call extraction
adapters. It does not measure OCR quality or predict processing time or cost.
Use [Plan time and cost](plan-time-and-cost.md) to estimate from your sample.
The dry-run directory is a run-planning record, not a document job to resume;
choose a new output path for the actual process job. `run --adapter pdf_ocr`
uses the built-in PDF OCR adapter without a YAML config. The actual job needs a
processing config, such as [`docs/examples/processing.yml`](examples/processing.yml).

## 3. Set limits from the sample

Set `processing.limits.max_attempt_pages` high enough for the pages and stages
you intend to attempt, while keeping the total work bounded. Every attempted
page at every stage counts, including failed attempts. A local batch is cut to
fit the remaining limit. Reaching the limit pauses the job; raise it to continue:

```bash
pageledger resume jobs/volume --raise-limit max_attempt_pages=200
```

This example assumes the job was configured with a lower limit and paused.
Choose a new limit based on the work left, not just the number of source pages.
Image attempts have their own required positive `max_image_pages` limit when
the image stage is enabled. Token and cost limits are thresholds, not provider
billing ceilings; a call already in progress can exceed them. Use provider-side
spending controls for a hard monetary ceiling. Details are in
[processing limits](processing-spec.md#one-budget-for-the-job).

## 4. Process each document

Use the tested configuration and a distinct output directory for every source:

```bash
pageledger process scans/volume.pdf --config processing.yml --out jobs/volume
pageledger verify-job jobs/volume
pageledger process scans/letter.pdf --config processing.yml --out jobs/letter
pageledger verify-job jobs/letter
```

Do not overwrite an existing output directory. Check `report.md` and
`document.json` for status, unresolved pages, failures and next actions. A
`completed` job means its configured work finished, not that a person reviewed
every page. A `paused_budget` job can continue after a limit is raised. A
`halted` job retains evidence but is not automatically retried; investigate its
reported cause before starting any new work.

## 5. Triage and review

Start with the job report. Check pages held for review, uncertain or failed
attempts, and disagreements between engines. Compare selected text with the
source image or PDF page. Record review decisions with a spreadsheet:

```bash
pageledger review-sheet jobs/volume --out reviews/volume.csv
```

Open the CSV, fill the `decision` and `note` columns, and save it as CSV. Check
it before recording decisions, then import it:

```bash
pageledger review-job jobs/volume --review reviews/volume.csv \
  --reviewer "A. Reader" --dry-run
pageledger review-job jobs/volume --review reviews/volume.csv \
  --reviewer "A. Reader"
pageledger verify-job jobs/volume
```

The dry run validates the sheet without recording decisions. Review receipts
are bound to the job evidence; do not reuse a sheet after the job or its
selected outputs change. See [review sheets](processing-spec.md#review-in-a-spreadsheet)
for accepted decisions and validation rules.

## 6. Export and verify

Export the reviewed text, then verify the source job again:

```bash
pageledger export jobs/volume --format txt --out exports/volume.txt --reviewed-only
pageledger export jobs/volume --format tei --out exports/volume.xml --reviewed-only
pageledger verify-job jobs/volume
```

Create the `reviews/` and `exports/` parent directories before these commands;
PageLedger writes the named file but does not create arbitrary parent folders.
Exports preserve page boundaries and identify review state. They do not certify
transcription accuracy. See [Export document text](export.md) for formats and
citation guidance.

## Image-only collections

The built-in `pdf_ocr` adapter reads pages in PDFs. There is no built-in path for
a directory of TIFF, JPEG or PNG pages. Convert the images into a PDF before
using the built-in PDF stages, for example with `img2pdf`, or use OCRmyPDF to
produce a searchable PDF and then process its text layer with `pdf_text`. These
are external tools and must be installed separately. Check page order and image
orientation after conversion.

If conversion does not fit the collection, write or adapt a page-image
extraction adapter. The adapter protocol and the
[image evidence contract](image-evidence-spec.md) describe the boundary and
retained evidence. Do not treat a directory of images as a supported built-in
input merely because an adapter can be written for it.
