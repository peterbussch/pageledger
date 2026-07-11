# PageLedger comparison

PageLedger should be positioned as a control plane around extraction tools,
not as a replacement for them.

| Tool | Strong At | PageLedger Difference |
|---|---|---|
| Mistral OCR | Hosted OCR/document understanding with Markdown, images, and table reconstruction. | PageLedger records why pages went to Mistral, what it cost, and which pages need review or rerun. |
| Google Document AI / Gemini layout parser | Managed OCR, layout parsing, table structure, and RAG-oriented chunking. | PageLedger keeps provider-neutral run artifacts and project-local audit queues outside a cloud workflow. |
| Azure Document Intelligence | Managed text, key-value, table, and field extraction for enterprise document workflows. | PageLedger can track Azure runs beside local or other-provider runs with the same page-denominated manifest. |
| AWS Bedrock Data Automation | Managed classification, extraction, validation, HITL review, and business-rule workflows. | PageLedger stays lighter and filesystem-native for research projects that do not want a cloud stack. |
| Docling / Granite Docling | Converting PDFs and documents into structured output, with OCR and VLM support. | PageLedger records which pages went to Docling, the usage and cost evidence, and which pages need review or rerun. |
| Marker | High-quality document conversion to Markdown/JSON/chunks/HTML, with schema extraction in beta. | PageLedger handles routing, run manifests, cost controls, and review queues around conversion. |
| Surya | OCR, layout analysis, reading order, and table recognition. | PageLedger can call Surya as an extractor, then audit the result across a full archive run. |
| olmOCR | LLM-based PDF extraction with document-oriented output and strong benchmark results. | PageLedger adds page routing, quality diagnostics, cost evidence, and rerun manifests. |
| OCR-D | Mature OCR workflow model with METS/PAGE/ALTO conventions. | PageLedger aims to be lighter, VLM-aware, and friendlier to local research workflows that need JSON/YAML artifacts before full library infrastructure. |
| Unstructured | Document partitioning and preprocessing for downstream use. | PageLedger focuses on extraction run control: what ran, what passed, what failed, what cost money, and what should be rerun. |

## 2026 threat model

The short answer is: modern OCR is swallowing the naive version of this niche,
but not the whole PageLedger niche.

Capabilities that are becoming commodity:

- High-quality PDF/image to Markdown or structured text conversion.
- Table reconstruction, reading order, layout regions, equations, and image
  extraction.
- Multilingual OCR and VLM-assisted handling of difficult scans.
- Hosted document extraction APIs that combine OCR, classification, schema-like
  extraction, validation, and human review.

This means PageLedger should not claim that extraction quality, Markdown
conversion, table detection, or generic OCR workflow orchestration are its core
defensible wedge. Those claims age quickly.

The remaining wedge is narrower and more useful: a local, provider-neutral run
ledger for projects that need to compare heterogeneous extraction runs and
reconstruct their recorded methodology. PageLedger's alpha should emphasize:

- Page-denominated routing and budgeting across tools with incompatible native
  usage metrics.
- Filesystem artifacts that survive without a service, database, or cloud
  account.
- Review and rerun queues that are tied to page ids, source checksums, prompts,
  adapter versions, and cost evidence.
- Research and archive workflows where source citation, reproducibility, and
  selective reruns matter more than one-shot conversion.

## Competitor workflow features

Several competitors already reach beyond raw OCR:

- AWS Bedrock Data Automation includes document classification, extraction,
  validation, human review patterns, and business-rule workflows.
- Google Document AI's Gemini layout parser targets structured layout parsing,
  complex tables, reduced hallucinations, and context-aware chunks.
- Marker has moved beyond PDF-to-Markdown into JSON/chunks/HTML and beta
  structured extraction from a JSON schema.
- Docling includes OCR, advanced PDF understanding, VLM support, and integrations
  with common GenAI frameworks.
- OCR-D remains the serious workflow reference for library-grade OCR, especially
  where METS/PAGE/ALTO conventions matter.

PageLedger should therefore present itself as complementary infrastructure, not
as a workflow category owner. Its first public release is strongest when it says:
bring your extractor; PageLedger records the run, the route, the cost evidence,
and the review/rerun queue.

## Distinctive claim

Most document-AI packages optimize for extraction output. PageLedger optimizes
for the extraction process:

- routing before extraction,
- evidence and provenance around extraction,
- rerun decisions after audit,
- schema alignment after extraction, with uncertainty and discarded structure
  recorded rather than silently repaired.

That process orientation is the reason the package could be interesting to
archives, libraries, digital-humanities labs, and research projects.

## Non-goal

PageLedger is not an OCR engine, PDF converter, layout detector, or hosted
document-AI platform. It is useful when a project needs to decide what to run,
check whether outputs meet project rules, record what happened, and rerun only
the uncertain parts.

## Sources checked

- [Mistral OCR 3](https://mistral.ai/news/mistral-ocr-3/)
- [Ai2 olmOCR 2](https://allenai.org/blog/olmocr-2)
- [Docling](https://github.com/docling-project/docling)
- [Marker](https://github.com/datalab-to/marker)
- [Surya](https://github.com/datalab-to/surya)
- [Google Document AI Gemini layout parser](https://docs.cloud.google.com/document-ai/docs/layout-parse-chunk)
- [Azure AI Document Intelligence](https://azure.microsoft.com/en-us/products/ai-foundry/tools/document-intelligence)
- [AWS Bedrock Data Automation IDP](https://aws.amazon.com/blogs/machine-learning/accelerate-intelligent-document-processing-with-generative-ai-on-aws/)
- [OCR-D workflow guide](https://ocr-d.de/en/workflows)
- [Unstructured partitioning docs](https://docs.unstructured.io/open-source/core-functionality/partitioning)
