# PageLedger documentation

PageLedger records extraction attempts, source identity, cost, and review
work as plain files. Choose an entry point below.

## Getting started

| Task | Guide |
|---|---|
| Try PageLedger without an OCR engine | [First run: text, review, rerun, and replay](first-run.md) |
| Run a complete offline document job | [First document job: process, recover, inspect, verify, and review](document-first-run.md) |
| Extract a PDF text layer or scanned PDF | [First PDF/OCR run](pdf-ocr-first-run.md) |
| Process a document through local text, OCR, and optional image stages | [Document processing jobs](processing-spec.md) |
| Recover interrupted extraction in place | [Checkpoint recovery](checkpoint-spec.md#get-started) |
| Review selected text and record human decisions | [Document reports and review receipts](document-report-spec.md) |
| Review pages in a spreadsheet | [Review in a spreadsheet](processing-spec.md#review-in-a-spreadsheet) |
| Export verified document text | [Document export formats and citation](export.md) |

## Working with a collection

- [Choose an OCR or VLM adapter](ocr-options.md)
- [Read pages with a vision model](vision-adapter.md)
- [Classify pages and review route evidence](classifier.md)
- [Run OCR on non-English and historical documents](multilingual-ocr.md)
- [Score transcriptions against references](scoring.md)
- [Work through a scanned government archive](examples/jfk-scanned-archive.md)
- [Write a custom extraction adapter](adapter-protocol.md)
- [Compare PageLedger with extraction tools](comparison.md)

## Reference

- [CLI commands and configuration](cli.md)
- [Run directory and artifact overview](artifacts.md)
- [Run manifest](run-manifest-spec.md)
- [Route map](route-map-spec.md)
- [Provenance and quality JSONL](provenance-spec.md)
- [Normalized records](normalized-spec.md)
- [Audit queue](audit-spec.md)
- [Rerun manifest](rerun-manifest-spec.md)
- [Checkpoint and page-attempt records](checkpoint-spec.md)
- [Document job](processing-spec.md#durable-job-and-attempts)
- [Document report and human review receipts](document-report-spec.md)
- [Document export formats and citation](export.md)
- [Reader trial protocol and results form](reader-trial.md)
- [Page image input evidence](image-evidence-spec.md)

The [JSON Schemas](../schemas/) define the machine-readable artifact contract.
Package 0.5.2 retains artifact `schema_version: "0.1"`.

## Scope and maintenance

- [Capabilities and limits](capabilities-and-limits.md)
- [60-page source validation for 0.5.1](validation/0.5.1/README.md)
- [Measured performance](performance.md)
- [Architecture and future work](design.md)
- [Release procedure](releasing.md)
