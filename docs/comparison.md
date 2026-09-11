# PageLedger and extraction tools

PageLedger calls extraction tools through adapters and records their work in a
common page ledger. Choose the extractor for the source material and required
output; use PageLedger when you also need source hashes, adapter provenance,
budgets, review decisions, or selective reruns across those tools.

## Where each tool fits

| Tool | Primary role | Integration with PageLedger |
|---|---|---|
| Tesseract | Local OCR of page images | Built-in `pdf_ocr` adapter with Poppler rendering. |
| [Docling](https://github.com/docling-project/docling) | Document conversion with layout, table, OCR, and VLM support | [Working example](../examples/docling_adapter.py) produces per-page Markdown. |
| [Marker](https://github.com/datalab-to/marker) | Document conversion to Markdown, JSON, and other formats | Supply a custom extraction adapter. |
| [Surya](https://github.com/datalab-to/surya) | OCR, layout analysis, reading order, and table recognition | Supply a custom extraction adapter. |
| [OCR-D](https://ocr-d.de/en/workflows) | OCR workflows built around METS workspaces and PAGE-XML | Keep OCR-D as the workflow engine; integration requires a custom adapter or artifact conversion. |
| [Unstructured](https://docs.unstructured.io/open-source/core-functionality/partitioning) | Partitioning documents into typed elements | Supply an adapter if page identity can be retained; downstream element processing remains separate. |
| Hosted OCR or image-model APIs | Provider-managed extraction | Supply credentials and an adapter. The [image example](image-evidence-spec.md) supports explicit Gemini or DeepSeek models over an OpenAI-compatible API. |

External tool descriptions were checked against the linked project documentation
on September 11, 2026. This table describes integration choices, not comparative
accuracy or speed. The listed custom integrations are not bundled adapters.

## When a ledger helps

A collection may need PDF text extraction for some pages, Tesseract for scans,
and an image model for a few difficult pages. PageLedger gives those attempts
consistent page identities and records the settings, output, usage, and review
reasons for each one. Its document jobs also retain selected text and human
review receipts without discarding earlier attempts.

For a single conversion, the extractor's own CLI may be enough. For an existing
OCR-D or managed-cloud workflow, assess whether PageLedger's local artifacts
add useful evidence before introducing another controller. PageLedger does not
import those systems' workflow histories automatically.

## What remains with the project

The project chooses engines and schemas, supplies credentials and any accounting
rates, and reviews output against the source. PageLedger's grades summarize
recorded signals; they do not calibrate accuracy across engines. Token and dollar
budgets also depend on the usage an adapter reports.

Use [OCR options](ocr-options.md) for configuration paths and
[capabilities and limits](capabilities-and-limits.md) for the exact supported
workflows.
