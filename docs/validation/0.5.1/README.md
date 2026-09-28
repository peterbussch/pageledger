# What the 60-page trial showed

PageLedger 0.5.1 processed 60 selected PDF pages from three scanned documents.
All three jobs completed and passed `verify-job`. They retained 120 extraction
attempts and selected text for 57 pages. The other three pages appeared blank
in the source images. All 60 pages still need a human review decision.

The main finding is simple: **a verified job can contain incorrect text.**
Verification checks the saved evidence and file fingerprints. It does not
compare every word or number with the source image.

## Sample and results

The page numbers below are positions in the PDF, starting at 1. A census scan
that contains two printed pages counts as one PDF page. The sample was chosen
before extraction to include archival typescript, old Cyrillic printing,
front matter, blank pages, and tables. It is a small, fixed sample, not a
random sample of archives or OCR tools.

| Source | PDF pages checked | Selected text | Processing time | Job verification |
| --- | --- | --- | --- | --- |
| NARA record 180-10147-10163 | 1–5, 21–25, 51–55, 101–105 | 20 of 20 | 30.82 seconds | Passed |
| Kharkov military-statistical survey, 1850 | 1–10, 81–90 | 17 of 20 | 136.01 seconds | Passed |
| *1939 census: Main results*, local scan | 10–29 | 20 of 20 | 388.51 seconds | Passed |

Each page first used `pdf_text`, then `pdf_ocr` with Tesseract at 300 DPI.
The languages were `eng` for NARA and `rus` for the other sources. Each job
allowed 40 attempt-pages and no image-model calls. An attempt-page is one
extraction attempt on one source page. The trial used local tools; recorded
cost is unknown because the adapters do not report a monetary charge.

Times are single wall-clock measurements of each `process` command on an
ARM64 Mac with Python 3.13.13, Tesseract 5.5.2, and Poppler 26.05.0. Other work
was running on the machine. These times include processing and writing the
job, exclude the separate verification command, and are not a speed benchmark
or a measurement of human review time.

The trial used the 0.5.1 development checkout. During the census run, a report
validation fix changed the package code fingerprint. It rejects an invalid
format marker; extraction code and settings did not change. The evidence keeps
both fingerprints. This trial is separate from the installed-package tutorial
checks required for the release.

## What needs source review

On NARA page 1, the source clearly ends a line with “that”. OCR mixes the word
with a handwritten margin note. The selected OCR attempt has no warning.
On Kharkov page 83, a visible table value of `796,808` becomes `196,308` in
OCR. Census page 11 changes a count of coding bureaus from `62` to `68`, also
without a selected-attempt warning. A general warning can call attention to a
page without identifying which number is wrong. Use the page image to settle
important readings.

Of 57 selected outputs, 35 have at least one warning: 17 Kharkov pages and 18
census pages. These warnings are not all error findings. Seven Kharkov pages
have only `historical_orthography`, an informational signal about old spelling.
Census pages 23–24 lose readable table structure; their selected attempts do
report fragmentation, suspicious symbols, and low confidence. The per-page
files keep those warnings separate from what the source check observed.

All 60 pages retained an `empty_text` concern from the first attempt because
these scans had no extracted text layer. That concern does not mean all 60
source pages are blank. The 0.5.1 report now separates **Current output**,
**Review status**, and **Recorded concerns**, and links an empty-text concern
to the attempt that produced it. Earlier evidence remains visible after OCR
provides usable text.

The review queue therefore still contains all 60 pages. We have not measured
how many of those reviews are unnecessary, how long they take, or how many
errors the checks miss. Those questions require people checking the source
and recording their decisions. The [reader trial](../../maintainers/reader-trial.md)
provides a separate test of whether newcomers can use and explain the workflow.
Its results are still pending.

## How the source checks were made

AI assistants inspected rendered source images and recorded short readings.
They then compared selected text with the images. This was a spot check of
visible text and layout, not a complete transcription of each page. No real
source page was marked `reviewed_text` or `reviewed_blank` from these AI checks.

The first AI notes contained errors too: some changed capitalization, omitted
reference marks, or assigned text to the wrong page. We kept those local notes,
reopened the affected images, and recorded corrections separately. We excluded
the initial phrase-match totals from the published results. They would mix OCR
errors with errors in the reading notes. The observations remain AI-assisted;
they are not independent human ground truth or an OCR accuracy score.

## Evidence and reproduction

- [summary.json](summary.json) records source fingerprints, exact page lists,
  configuration fingerprints, tool identities, timings, and job totals.
- [page-results.csv](page-results.csv) records each page's selected output
  fingerprint, warnings, review reasons, and source-image fingerprint.
- [observations.json](observations.json) records the scope and limits of the
  source comparisons, including uncertain readings.
- [nara.yml](nara.yml) and [cyrillic.yml](cyrillic.yml) are the exact processing
  configurations used. A SHA-256 fingerprint identifies the bytes of a file.

The [NARA PDF](https://www.archives.gov/files/research/jfk/releases/2018/180-10147-10163.pdf)
is publicly available. Download it as `nara.pdf`, check its fingerprint against
`summary.json`, and run these commands from this directory with PageLedger
0.5.1, its PDF extra, Poppler, and Tesseract installed:

```bash
shasum -a 256 nara.pdf
pageledger process nara.pdf --config nara.yml \
  --pages '1-5,21-25,51-55,101-105' --out jobs/nara --json
pageledger verify-job jobs/nara --json
```

Use a new output directory for each run. For the two local scans, use the
fingerprints and page lists in `summary.json` with `cyrillic.yml`. We do not
have a verified download URL for those exact files, so full public reproduction
of those two samples remains limited. Source PDFs, page images, and full
transcripts are not included here. The retained local jobs hold the complete
attempt evidence; the public files are a summary, not a portable job bundle.

## Separate live VLM check

We also tried image-model escalation on census PDF page 23, a spread with
two dense tables. **No live VLM transcription completed.** These attempts
are separate from the 60-page local OCR trial above.

| Attempt | Result |
| --- | --- |
| Antigravity Gemini, first account | HTTP 422: account had no project. Google later confirmed that account was ineligible. |
| Cursor Gemini, 4096-pixel image | HTTP 400: image exceeded the route's 1 MiB limit. |
| Antigravity Gemini, eligible account | HTTP 429: authorization and project were present, but Gemini quota was exhausted. |
| Cursor config, 1 MiB image limit | Stopped locally: the 4096-pixel image did not fit, even at JPEG quality 60. No image request was sent. |
| Cursor Gemini, 3072-pixel image | The retained image fit the byte limit; the gateway returned HTTP 503. |

Each fresh job allowed three extraction attempts and one image stage.
All five jobs halted, preserved the completed local OCR and available failure
evidence, and passed `verify-job`. The adapter did not retry requests or switch
providers. Recorded token use and monetary cost remain unknown.

The final JPEG was 2206 × 3072 pixels and 965,580 bytes, at quality 90.
An AI assistant inspected that exact image before the final job. The table
rows used for the planned spot check remained readable. There was no returned
VLM text to compare, so this test establishes neither transcription quality
nor a working live provider route.

[vlm-results.json](vlm-results.json) retains each result, image and output
fingerprints, and the readings recorded before testing. [vlm.yml](vlm.yml)
is the exact final configuration. It requires the optional example adapter,
Pillow, Poppler, and an authorized OpenAI-compatible gateway. The model name
and gateway in that file describe this test; they are not a promise of access.
See the [image adapter specification](../../image-evidence-spec.md) for setup
and the new `max_image_bytes` and `max_image_dimension` options.

## What this changes next

The trial supports clearer reports and continued source review. It does not
support automatic approval, a new OCR accuracy claim, or ranking extraction
engines. The next useful check is to watch two newcomers work through the
[tutorial](../../document-first-run.md) and record where they need help.

Portable document jobs would make that handoff easier. Existing run bundles
already support relocation and replay, but complete document-job bundles are
still future work. A bundle is a way to carry evidence; the larger goal is
that another person can find the source, understand a concern, and reuse the
result without losing its history.
