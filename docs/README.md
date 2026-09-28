# PageLedger documentation

PageLedger records extraction attempts, source identity, cost, and review
work as plain files. Start with a tutorial, then use the how-to guides with
your own documents. The [glossary](glossary.md) defines the terms.

## Tutorials

| Task | Tutorial |
|---|---|
| Try PageLedger without an OCR engine | [First run: text, review, rerun, and replay](first-run.md) |
| Run a complete offline document job | [First document job: process, recover, inspect, verify, and review](document-first-run.md) |
| Extract a PDF text layer or a scanned PDF | [First PDF/OCR run](pdf-ocr-first-run.md) |

## How-to guides

Your documents:

- [Process a collection](process-a-collection.md)
- [Choose OCR settings](choose-ocr-settings.md)
- [Run OCR on non-English and historical documents](multilingual-ocr.md)
- [Plan time and cost](plan-time-and-cost.md)
- [Recover interrupted work](checkpoint-spec.md#get-started)
- [Troubleshoot an error or an odd result](troubleshooting.md)

Engines:

- [Choose an OCR or VLM adapter](ocr-options.md)
- [Engine recipes](engine-recipes.md)
- [Read pages with a vision model](vision-adapter.md)
- [Write a custom extraction adapter](adapter-protocol.md)

Review and results:

- [Review pages in a spreadsheet](processing-spec.md#review-in-a-spreadsheet)
- [Export document text](export.md)
- [Share and cite results](share-and-cite.md)
- [Classify pages and review route evidence](classifier.md)
- [Score transcriptions against references](scoring.md)
- [Work through a scanned government archive](examples/jfk-scanned-archive.md)

## Reference

- [CLI commands and configuration](cli.md)
- [Glossary](glossary.md)
- [Warnings, holds and dispositions](warnings.md)
- [Document processing jobs](processing-spec.md)
- [Document report and human review receipts](document-report-spec.md)
- [Run directory and artifact overview](artifacts.md)
- [Run manifest](run-manifest-spec.md)
- [Route map](route-map-spec.md)
- [Provenance and quality JSONL](provenance-spec.md)
- [Normalized records](normalized-spec.md)
- [Audit queue](audit-spec.md)
- [Rerun manifest](rerun-manifest-spec.md)
- [Checkpoint and page-attempt records](checkpoint-spec.md)
- [Page image input evidence](image-evidence-spec.md)
- [JSON Schemas](../schemas/): the machine-readable artifact contract. Artifacts
  keep `schema_version: "0.1"` across package releases.

## Explanation

- [Architecture and future work](design.md)
- [Capabilities and limits](capabilities-and-limits.md)
- [Comparison with extraction tools](comparison.md)

## Project records

- [60-page source validation for 0.5.1](validation/0.5.1/README.md)
- [Maintainer notes](maintainers/README.md): performance record, reader trial
  protocol, release procedure and tutorial verification
