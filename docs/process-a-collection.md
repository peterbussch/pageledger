# Process a collection

This guide takes real documents, scanned PDFs or folders of page images, from
a small test to reviewed text. A *job* processes one document and keeps every
reading, decision and limit for it, so a collection of documents is a set of
jobs, one per document. See [document processing](processing-spec.md) for the
complete configuration and the files a job writes.

## 1. Inventory and choose a sample

Keep the source files unchanged: a job records each file's SHA-256 hash and
`verify` later checks it. Record their names, page counts, formats and known
problems. A document can be a PDF, a UTF-8 text file, one JPEG or PNG image, or
a folder of page images ([below](#page-images)).

Choose a few documents that represent the collection, including difficult
layouts and languages, and start with a short range of pages from one of them.
`--pages` selects source page numbers and keeps them in the record:

```bash
pageledger init --out processing.yml
pageledger process scans/volume.pdf --config processing.yml \
  --pages "1-5,41-45" --out jobs/volume-sample
pageledger inspect jobs/volume-sample
```

`init` writes the configuration a job without `--config` would use, with
comments describing each optional stage; edit it as the sample suggests.
Read the report and compare the transcript with the pages. A sample can reveal
poor OCR, a missing language pack or the need for another stage. For Russian
and historical material, see [non-English and historical OCR](multilingual-ocr.md).

`process` has no dry run. To see a document's page count and route plan
without reading anything, use `run --dry-run`, which writes planning records
for a [run](cli.md#run), not a job:

```bash
pageledger run scans/volume.pdf --adapter pdf_ocr --out runs/volume-plan --dry-run
```

A dry run does not measure OCR quality or predict time or cost;
[plan time and cost](plan-time-and-cost.md) estimates those from your sample.

## 2. Set limits from the sample

`processing.limits.max_attempt_pages` counts readings, not pages: every page
read at every stage counts, failed readings included. Reaching the limit pauses
the job, keeping everything done so far; raise it to continue:

```bash
pageledger resume jobs/volume --raise-limit max_attempt_pages=200
```

Choose the new limit from the work left, not from the number of pages. Paid
image stages have their own limits, and token and cost limits are thresholds,
not provider billing ceilings. See [plan time and cost](plan-time-and-cost.md#set-spending-and-work-limits)
and [one budget for the job](processing-spec.md#one-budget-for-the-job).

## 3. Process each document

Use the tested configuration and a new output folder for every document:

```bash
pageledger process scans/volume.pdf --config processing.yml --out jobs/volume
pageledger process scans/letter.pdf --config processing.yml --out jobs/letter
```

Each command ends by saying how many pages need a person and what to run next;
`report.md` in the job folder says the same at greater length. A finished job
means its configured stages ran, not that anyone reviewed its pages. A job
paused by a limit continues after the limit is raised; a halted job keeps its
evidence and names the cause in its report.

## 4. Review

Start with the pages that need a person. The review view shows each page's
image beside its text:

```bash
pageledger review jobs/volume --out reviews/volume
```

Open `reviews/volume/index.html` in a browser, choose a decision for each page
or correct its text, and download `review.json` into that folder. If you
prefer a spreadsheet, `pageledger review jobs/volume --sheet reviews/volume.csv`
writes a sheet to fill in. Check the decisions, then record them:

```bash
pageledger review jobs/volume --apply reviews/volume/review.json \
  --reviewer "A. Reader" --dry-run
pageledger review jobs/volume --apply reviews/volume/review.json \
  --reviewer "A. Reader"
pageledger verify jobs/volume
```

The dry run checks every decision without recording any. Decisions are bound
to the job as it was when the view or sheet was written; write a new one if the
job has changed since. See [reports and review](processing-spec.md#reports-and-review)
for each decision and what it does.

## 5. Export

```bash
pageledger export jobs/volume --format txt --out exports/volume.txt --reviewed-only
pageledger export jobs/volume --format tei --out exports/volume.xml --reviewed-only
```

Exports keep page boundaries and say each page's review state. They do not
certify the transcription. See [export document text](export.md).

## Page images

Archives often deliver a book or a reel as one image file per page. Give
PageLedger the folder, and it reads the images as one document:

```bash
ls scans/
# page-1.jpg  page-2.jpg  ...  page-10.jpg
pageledger process scans/ --out jobs/scans
```

The pages follow the file names in natural order, so `page-2.jpg` comes before
`page-10.jpg`: number your files so that this order is the page order. The
folder must hold only JPEG (`.jpg`, `.jpeg`) or PNG (`.png`) files. PageLedger
skips hidden files such as `.DS_Store` and stops, before reading any page, at
anything else, so a stray note or a subfolder of thumbnails is never read as a
page. Convert TIFF or JPEG 2000 masters to PNG first; PNG keeps every pixel,
and the error message names a command that does it. A single image file is a
one-page document.

What PageLedger records is the images themselves. Each image is hashed, and
the source's identity is a hash over the list of file names, hashes and pixel
sizes, so `verify` later names any image that changed, went missing or was
added.

A page's number is its place in the folder, which need not be the number in
its file name: if the folder starts at `0081.jpg`, that image is page 1. The
report, the transcript and the review view show both, as `1 (0081.jpg)`.

Page images have no text of their own, so the job's first stage records an
empty reading with the warning `no_text_layer` and the page goes on to OCR.
The OCR engines read PDF pages, so PageLedger hands them a temporary PDF, the
*carrier*, holding the images' own bytes; the run records its hash and deletes
it. Its page size comes from the density the image declares, or 300 pixels per
inch when it declares none. If an engine renders a page at much more than the
image's own pixels, the page gets the warning `render_upsampled`: a small image
read at high resolution holds no more detail, and a better scan is the fix.

A folder that [`fetch`](iiif.md) wrote from a library's IIIF server is a
folder of page images too; its fetch record, `iiif-fetch.json`, and the `iiif/`
folder beside it are the only other things it may hold.
