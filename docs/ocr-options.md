# OCR and VLM Options

PageLedger is provider-agnostic. It records page routing, adapter metadata,
provenance, quality signals, usage, cost rollups, and failure evidence. The OCR
or VLM engine is a pluggable choice made by the user.

Use this guide to choose a path. Treat tool names as examples, not blessed
providers.

## Decision Matrix

| Tier | Example path | Runs local | Typical cost | Good fit | PageLedger integration |
|---|---|---:|---|---|---|
| Born-digital PDF | `pdf_text` | yes | free | PDFs with a real embedded text layer | Built-in adapter via `pageledger[pdf]`. |
| Baseline OCR | OCRmyPDF + Tesseract | yes | free, plus compute | Scanned PDFs where searchable text is enough | External preprocessing, then `pdf_text`, or a custom adapter. |
| Local document conversion | Docling | yes | free/open, plus compute | PDF/document conversion with layout-aware output | Custom adapter returning text, Markdown, or JSON. |
| Markdown/JSON extraction | Marker | yes | free/open, plus compute | Markdown, JSON, tables, equations, forms, images | Custom adapter returning Markdown or JSON. |
| Local OCR/layout/tables | Surya | yes | free/open, often heavier compute | OCR, reading order, layout, table recognition | Custom adapter with `capabilities=("ocr", "layout", "tables", "local")`. |
| Cloud OCR/document AI | User-chosen provider | no | provider-defined | Managed OCR, forms, tables, enterprise pipelines | Custom adapter with redacted env checks and configured pricing. |
| Cloud VLM | User-chosen model/API | no | provider-defined | Hard pages, multimodal reasoning, messy forms/tables | Custom adapter, usually capped by page/token/dollar budgets. |
| Hybrid | Local first, cloud only for weak pages | mixed | controlled | Large collections with a small hard subset | Use `quality.jsonl` and review queues to decide reruns. |

## Recommended Workflow

1. Run `pageledger doctor` to see local commands, optional packages, PATH, and
   redacted cloud environment status.
2. Use `pdf_text` for born-digital PDFs.
3. Use OCRmyPDF/Tesseract preprocessing when a scanned PDF only needs a text
   layer.
4. Use Docling, Marker, or Surya through a custom adapter when layout, tables,
   Markdown, or richer JSON matter.
5. Use a cloud OCR/VLM adapter only when local output is weak, the document is
   especially complex, or managed infrastructure is required.
6. Inspect `quality.jsonl`, `provenance.jsonl`, `run.log`, and `cost.json`
   before deciding whether to rerun pages with a stronger adapter.

## Adapter Capability Hints

Adapters should declare what they can do:

```python
input_types = ("pdf", "image")
output_types = ("text", "markdown", "json")
capabilities = ("ocr", "layout", "tables", "cloud")  # or "local"
deterministic = False
```

For PDF-backed adapters, expose:

```python
def page_count(self, source: Path) -> int:
    ...
```

That lets PageLedger plan pages before extraction without knowing the provider.

## Pricing

Do not hard-code provider pricing in adapters. Prefer this order:

1. Pass through `usage.cost_usd` only if the backend returns an actual cost.
2. Configure project-local unit rates such as `run.pricing.cost_per_page`.
3. Leave cost unknown and still report pages, tokens, and compute seconds.

## Trust Model

No OCR/VLM path is automatically trustworthy. PageLedger treats output as
evidence:

- `provenance.jsonl` says what produced each page.
- `quality.jsonl` gives basic per-page signals such as character count, word
  count, short/empty warnings, and embedded-text deltas when available.
- `run.log` classifies operational failures.
- `cost.json` keeps cost and usage separate from provider marketing claims.

The best default is usually hybrid: run cheap local extraction first, then route
only suspicious or high-value pages to a stronger OCR/VLM path.
