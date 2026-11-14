# PageLedger documentation

PageLedger reads a document into text one page at a time and keeps a record of
how each page was read, so that the text can be checked and cited. The
[glossary](glossary.md) defines the terms these pages use.

## Start here

| If you want to | Read |
|---|---|
| See a whole job on a small example that needs no OCR engine | [First job](document-first-run.md) |
| See the run commands, which build and test pipelines, on the same terms | [First run](first-run.md) |

## By task

| I have, or I want to | Read |
|---|---|
| a scanned or born-digital PDF | [Process a collection](process-a-collection.md) |
| a folder of page images | [Page images](process-a-collection.md#page-images) |
| the scans are on a library's IIIF server | [Fetch page images from a IIIF server](iiif.md) |
| text in Russian, pre-reform spelling or another language | [Non-English and historical OCR](multilingual-ocr.md) |
| to choose an OCR engine, or a vision model | [OCR options](ocr-options.md), then [engine recipes](engine-recipes.md) and [read pages with a vision model](vision-adapter.md) |
| to review and correct pages | [Review and correct](review-and-correct.md), then [reports and review](processing-spec.md#reports-and-review) |
| to write the text out | [Export document text](export.md) |
| to cite or deposit a job | [Share and cite results](share-and-cite.md) |
| to open a job, run or bundle someone sent me | [Jobs, runs and bundles you receive](share-and-cite.md#jobs-runs-and-bundles-you-receive) |
| to estimate time and cost before a large job | [Plan time and cost](plan-time-and-cost.md) |
| to recover interrupted work | [Resume an interrupted job](processing-spec.md#resume-an-interrupted-job) |
| to understand an error or an odd result | [Troubleshooting](troubleshooting.md) |
| to connect my own engine | [Adapter protocol](adapter-protocol.md) |
| to build a pipeline over many files | [First PDF and OCR run](pdf-ocr-first-run.md), then the [CLI reference](cli.md#run) |
| to sort pages by kind before extraction | [Classify pages](classifier.md) |
| to score transcriptions against a reference | [Scoring](scoring.md) |
| a worked example on a real archive | [A scanned government archive](examples/jfk-scanned-archive.md) |

## Reference

- [CLI commands and configuration](cli.md)
- [Document processing jobs](processing-spec.md): stages, hold policy, contest,
  tiles, limits, review
- [Warnings, holds and review decisions](warnings.md)
- [Document report and review receipts](document-report-spec.md)
- [Capabilities and limits](capabilities-and-limits.md): what is and is not
  supported
- [Stability and compatibility](stability.md): what stays stable from 1.0, and
  which older runs and jobs still verify
- [Glossary](glossary.md)
- Files a run or job writes: [overview](artifacts.md),
  [run manifest](run-manifest-spec.md), [route map](route-map-spec.md),
  [provenance and quality](provenance-spec.md),
  [normalized records](normalized-spec.md), [audit queue](audit-spec.md),
  [rerun manifest](rerun-manifest-spec.md),
  [checkpoint and recovery records](checkpoint-spec.md),
  [page image evidence](image-evidence-spec.md),
  [IIIF fetch record](iiif-fetch-spec.md)
- [JSON Schemas](../schemas/): the machine-readable contract for those files.
  They keep `schema_version: "0.1"` across package releases.

## Background

- [Design and future work](design.md)
- [Comparison with other extraction tools](comparison.md)
- [60-page source validation for 0.5.1](validation/0.5.1/README.md)
- [Maintainer notes](maintainers/README.md): performance record, reader trial
  protocol, release procedure and tutorial verification
