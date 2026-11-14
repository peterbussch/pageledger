# PageLedger and extraction tools

PageLedger calls extraction tools through adapters and records their work in a
common page ledger. Choose the extractor for the source material and required
output; use PageLedger when you also need source hashes, adapter provenance,
budgets, review decisions, or selective reruns across those tools.

## Where each tool fits

| Tool | Primary role | Integration with PageLedger |
|---|---|---|
| Tesseract | Local OCR of page images | Built-in `pdf_ocr` adapter with Poppler rendering. |
| RapidOCR, Indra OCR | Local OCR models that run on the CPU | Built-in `rapidocr` and `indra` adapters ([engine recipes](engine-recipes.md)). |
| [Transkribus](https://www.transkribus.org/) | A hosted platform from the READ-COOP cooperative for handwritten and printed text recognition, with model training and transcription in the browser | Its ALTO export, one file per page, can be read with the built-in `alto` adapter as one more reading of the page. |
| [eScriptorium](https://escriptorium.readthedocs.io/) and [Kraken](https://kraken.re/) | An open-source platform, which you host yourself, for transcribing and training models, built on the Kraken engine | eScriptorium's ALTO export (one XML file per page) can be read with the `alto` adapter. |
| [OCR4all](https://github.com/OCR4all/OCR4all) | An open-source OCR workflow from the University of Würzburg for early printed books, with correction in a graphical interface | Keep OCR4all as the workflow; integration requires a custom adapter or artifact conversion. |
| ABBYY FineReader | Commercial OCR software | Its ALTO output, where your edition writes one, can be read with the `alto` adapter. |
| [Docling](https://github.com/docling-project/docling) | Document conversion with layout, table, OCR, and VLM support | [Working example](../examples/docling_adapter.py) produces per-page Markdown. |
| [Marker](https://github.com/datalab-to/marker) | Document conversion to Markdown, JSON, and other formats | Supply a custom extraction adapter. |
| [Surya](https://github.com/datalab-to/surya) | OCR, layout analysis, reading order, and table recognition | Supply a custom extraction adapter. |
| [OCR-D](https://ocr-d.de/en/workflows) | OCR workflows built around METS workspaces and PAGE-XML | Keep OCR-D as the workflow engine; integration requires a custom adapter or artifact conversion. |
| [Unstructured](https://docs.unstructured.io/open-source/core-functionality/partitioning) | Partitioning documents into typed elements | Supply an adapter if page identity can be retained; downstream element processing remains separate. |
| Vision models, local or hosted | A model that writes a page's text from its image | Built-in [`vision`](vision-adapter.md) adapter for any OpenAI-compatible endpoint; other provider APIs need a custom adapter. |

External tool descriptions were checked against the linked project documentation
on September 11, 2026, and those of Transkribus, eScriptorium, Kraken and OCR4all
on November 13, 2026. This table describes integration choices, not comparative
accuracy or speed. The listed custom integrations are not bundled adapters.

Transkribus, eScriptorium and OCR4all are where a project transcribes and
trains models, often for handwriting; PageLedger does neither. It records how
each page of a document was read, by whichever tools, and what a person
decided about it. A project can do its transcription in one of those platforms
and bring the result into a PageLedger job as one reading among others.

## When a ledger helps

A collection may need PDF text extraction for some pages, Tesseract for scans,
and an image model for a few difficult pages. PageLedger gives those attempts
consistent page identities and records the settings, output, usage, and review
reasons for each one. Its jobs also retain selected text and human
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
