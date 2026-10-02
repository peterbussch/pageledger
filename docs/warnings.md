# Warnings and review actions

A warning is evidence that an extraction may need attention. It is not a
verdict that text is wrong. A hold keeps a document-job page in review or sends
it to another configured stage. A disposition describes the page's current
state. A document job can finish while pages remain unresolved; see
[review at scale](processing-spec.md#review-at-scale).

## Quality warnings

| Warning | What it means and likely cause | What to do next |
|---|---|---|
| `empty_text` | No letters or digits were extracted. The page may be blank, an image, or an unsupported scan. | Inspect the source. If it contains text and is a PDF, try `pdf_ocr`; otherwise choose an adapter that supports the input. Confirm a blank page with a review decision. |
| `short_text` | Fewer than ten characters were returned. This may be a short page or incomplete extraction. | Compare with the page image; accept only if the short content is complete. |
| `replacement_characters` | One or more Unicode replacement characters indicate undecodable bytes or font mappings. | Check the source's text layer and encoding. For a scanned PDF, try OCR and compare. |
| `control_characters` | Non-whitespace control characters occur in output. | Inspect the raw output and source. Try another extraction method if the characters replace visible text. |
| `suspicious_symbol_density` | The output contains many unusual symbols, often from a damaged text layer or OCR. | Compare with the page image and try OCR if the source is a scan. |
| `fragmented_text` | Many letter tokens are unusually short. OCR may have broken words into fragments. | Inspect the page and try a suitable language pack, higher render resolution, or another engine. This signal does not detect ordinary word-level substitutions. |
| `joined_text` | Many Latin-script words appear joined with few spaces. | Inspect the image. Try OCR or another text-layer source; do not add spaces automatically. |
| `suspicious_embedded_text_delta` | OCR output length differs greatly from the PDF text layer. | Compare both readings with the image; one layer may omit content or use a different representation. |
| `historical_orthography` | Pre-reform Russian spelling markers suggest that the engine's modern spelling model may not fit. | Use an engine or language model suitable for the historical spelling. Preserve the source spelling and review any normalization separately. |
| `low_confidence` | The engine marked at least a quarter of words as low confidence on a sufficiently long page. | Inspect those passages. Check render resolution and language, then consider a stronger or better-matched engine. Confidence is not an accuracy score. |
| `instruction_echo` | Output contains a specific chat-template marker. A generative model may have returned internal formatting or instructions. | Reject it as a transcript until compared with the source; adjust the prompt or use another model, then review the new output. |
| `output_inflation` | A rerun output is at least four times and 1,000 characters longer than its parent page. The result may contain repetition or invented text. | Compare both outputs with the source. Do not assume longer output is more complete. |
| `digits_only_text` | The text layer contains many digits and almost no letters, as when table words were lost. | Inspect the page; run OCR or another extraction method that can read the table labels. |
| `mixed_script_tokens` | Words mix visually similar letters from Latin, Cyrillic, or Greek scripts. | Inspect the affected words and compare glyphs with the page. Search and ordinary text alignment may miss the substitutions. |
| `foreign_script_characters` | A few letters from a script that doesn't belong on the page (Ethiopic, Oriya, Tamil, Syriac or Hebrew inside Latin or Cyrillic text). Usually a symbol or math font read through the wrong character map. | Treat the text layer's formulas and symbols as lost. Read the page with OCR or a vision model, and check formulas against the page image. |
| `private_use_characters` | Several characters use font-specific private code points. They may stand for lost digits, ligatures, bullets, or icons. | Inspect their positions in the page. Try OCR or a better text extraction; do not guess replacements. |
| `repeated_page_text` | The same short text is the entire output on at least three pages, often a stamp or watermark. | Check whether the page content is missing. Use OCR or another source if it is. |
| `repetition_loop` | Text repeats in a pattern consistent with a vision model looping. | Do not use the output without comparison to the image. Retry with a changed model or prompt only as a new recorded attempt. |
| `script_mismatch` | When a script is configured, fewer than half of the page's letters match it. The wrong language/model or a mixed-script page may be involved. | Check the declared `language.script` and page content. Correct the configuration or review the mixed-script source; PageLedger does not guess a language. |
| `historical_letters_lost` | With `orthography: prereform`, a large Cyrillic output lacks expected historical letters and word-final hard signs. OCR may have modernized the spelling. | Compare against the page and use a model suited to pre-reform spelling. Do not restore letters automatically. |
| `render_dpi_capped` | PDF OCR rendered below the requested DPI to fit the configured pixel limit. | Inspect the rendered output. If it is not legible, increase `max_render_pixels` within available memory or use a different source. |
| `coverage_defect`, `clipped_text`, `truncated_text`, `output_truncated`, `missing_content` | Adapter or reviewer evidence indicates omitted, clipped, or truncated content. | Compare the full page and output. Use another extraction stage or source; a clean later result does not erase the concern. |
| `missing_required_columns` | A required table column was not found in aligned output. | Check the schema aliases and source table. Correct the schema only when the header correspondence is clear; otherwise review the table. |
| `numeric_column_conflict`, `column_conflict` | Numeric values or their column associations conflict. | Compare named columns and row identities with the source. Do not resolve by matching totals alone. |
| `source_defect` | Evidence indicates damage or missing content in the source itself. | Locate a better or alternate source. Further OCR cannot recover marks that are absent. |
| `blank`, `blank_candidate` | Output is empty or the page was classified as blank; blankness has not necessarily been confirmed by a person. | Inspect the page. Record `reviewed_blank` only when the source page is blank. |
| `illustration`, `handwriting`, `unreadable` | The page is described as image-only illustration, handwriting, or unreadable. | Confirm the description against the source and record a review decision. Use a specialized process if needed; these labels alone do not confirm text. |
| `sparse`, `fragmented`, `joined`, `unknown` | Structural classification indicates uncertain or limited text shape. Short correct pages can also be sparse. | Inspect the page. OCR may help if content is absent, but do not treat the classification alone as proof of a defect. |
| `numeric_disagreement`, `engine_disagreement` | Two clean attempts disagree on numbers or on less than 60% of words. Engines can share errors, too. | Compare both readings against the source. Agreement is evidence, not proof. |
| `unconfirmed_model_output` | A generative adapter, such as `vision`, wrote the reading. A plain run holds every such page; a document job holds it until a clean reading from another engine agrees. | Confirm it against the source, or read the page with another engine and compare, before accepting it. |

Adapter-specific warnings may also appear in the page's `warnings` list. Their
meaning depends on the adapter; consult that adapter's documentation and inspect
the source evidence. The built-in PDF OCR currently reports
`render_dpi_capped` when it lowers resolution to fit its pixel limit.

## Document-job dispositions

| Disposition | Meaning | What to do next |
|---|---|---|
| `pending` | No attempt has been recorded. | Run or resume the configured job. |
| `unreviewed_text` | Usable text is selected, but no human receipt confirms it. | Compare it with the source and record a bound review if review is required. |
| `coverage_defect` | A warning or structure check suggests possible missing or damaged content. | Inspect source coverage; use another configured stage or an alternate source. |
| `low_confidence` | The engine doubts some words; this is distinct from missing-content evidence. | Inspect uncertain passages and consider better resolution, language settings, or another engine. |
| `numeric_column_conflict` | Structured numeric evidence disagrees or cannot be safely checked. | Check the named columns and row identities against the source. |
| `engine_disagreement` | Independent readings disagree substantially. | Review the source and both attempts. |
| `numeric_disagreement` | Independent readings differ in one or more numbers. | Verify each number against the source. |
| `unconfirmed_model_output` | Selected generative-model text lacks independent agreement. | Compare with the source or obtain an independent extraction. |
| `contested` | With `processing.contest`, the selected model reading differs from the literal reading in places no rule settled. The spans are in `contested/<page_id>.json`. | Check each open span against the page image. A literal non-word kept open by the misprint guard may be what the page prints. |
| `blank_candidate` | No usable text was extracted; visual blankness is not established. If the hold came only from an empty reading, a later non-generative engine that reads clean text from the page clears it; an engine's own `blank` judgement stays. | Inspect the source; record `reviewed_blank` only if confirmed. |
| `provider_failure` | The latest extraction attempt failed. | Read the safe failure code and adapter guidance. Resolve setup or input problems before a deliberate new attempt. |
| `outcome_unknown` | A request may have been sent, but no durable result was saved. | Investigate adapter/provider records before starting new work; automatic retry is not safe. |
| `source_defect` | A person or adapter identified a source problem. | Seek an alternate source; review does not restore missing content. |
| `illustration`, `handwriting`, `unreadable` | A human classified the page's content. | The classification itself remains unresolved; document any other transcription or preservation workflow separately. |
| `reviewed_text` | A human receipt binds a selected completed output to the source page. | Keep the receipt with the job; it records a review, not a guarantee of perfection. |
| `reviewed_blank` | A human receipt confirms the page is blank. | Retain the receipt and source binding. |

## Run-level outcomes and next actions

A run's `completed` status means configured extraction work finished, not that
every page is accurate. `partial` means work was intentionally incomplete or
some pages failed while processing continued. `failed` means the run stopped on
an error. For a job, `paused_budget` means a configured limit was reached and
can be raised when safe; `halted` means automatic processing stopped and resume
will not retry the failed or uncertain request.

For typed PDF diagnostics such as `missing_binary`, `missing_language_pack`,
`unsupported_encryption`, `malformed_pdf`, or `render_limit`, follow the message
and the [run manifest common-errors table](run-manifest-spec.md#common-errors-and-user-action).
Adapter exceptions are redacted in ordinary run logs; inspect the diagnostic
code and debug the adapter locally without publishing credentials.
