# Warnings and review actions

A warning is evidence that an extraction may need attention. It is not a
verdict that text is wrong. A hold keeps a job page in review or sends
it to another configured stage. A disposition describes the page's current
state. A job can finish while pages remain unresolved; see
[review at scale](processing-spec.md#review-at-scale). The report, the terminal
and the review view describe each code in the short words listed under
[in plain words](#in-plain-words), with the code beside them.

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
| `multi_column_rows` | RapidOCR `reading_order: rows` joined at least four detected text lines in more than half of the visual rows. This often means several page columns were read side by side. | Set `reading_order: columns` for columnar pages. This warning is evidence only and does not hold a job page. |
| `historical_orthography` | Pre-reform Russian spelling markers suggest that the engine's modern spelling model may not fit. | Use an engine or language model suitable for the historical spelling. Preserve the source spelling and review any normalization separately. |
| `low_confidence` | The engine marked at least a quarter of words as low confidence on a sufficiently long page. | Inspect those passages. Check render resolution and language, then consider a stronger or better-matched engine. Confidence is not an accuracy score. |
| `instruction_echo` | Output contains a specific chat-template marker. A generative model may have returned internal formatting or instructions. | Reject it as a transcript until compared with the source; adjust the prompt or use another model, then review the new output. |
| `output_inflation` | A rerun output is at least four times and 1,000 characters longer than its parent page. The result may contain repetition or invented text. | Compare both outputs with the source. Do not assume longer output is more complete. |
| `digits_only_text` | The text layer contains many digits and almost no letters, as when table words were lost. | Inspect the page; run OCR or another extraction method that can read the table labels. |
| `mixed_script_tokens` | Words mix visually similar letters from Latin, Cyrillic, or Greek scripts. | Inspect the affected words and compare glyphs with the page. Search and ordinary text alignment may miss the substitutions. |
| `foreign_script_characters` | A few letters from a script that doesn't belong on the page (Ethiopic, Oriya, Tamil, Syriac or Hebrew inside Latin or Cyrillic text). Usually a symbol or math font read through the wrong character map. A spacing caron (ˇ, written by some LaTeX fonts beside the letter for "č") is not counted from 0.9. Under hold policy 0.9 a typeset layer held only by this warning or `private_use_characters`, on at most 1% of its letters, stays the selected text while the page is held. | Treat the text layer's formulas and symbols as lost. Read the page with OCR or a vision model, and check formulas against the page image. |
| `private_use_characters` | Several characters use font-specific private code points. They may stand for lost digits, ligatures, bullets, or icons. | Inspect their positions in the page. Try OCR or a better text extraction; do not guess replacements. |
| `repeated_page_text` | The same short text is the entire output on at least three pages, often a stamp or watermark. | Check whether the page content is missing. Use OCR or another source if it is. |
| `repetition_loop` | Text repeats in a pattern consistent with a vision model looping. | Do not use the output without comparison to the image. Retry with a changed model or prompt only as a new recorded attempt. |
| `partial_transcription` | A generative reader says, in brackets, that it left text out ("[Remaining lines … are omitted]"). | Treat the reading as incomplete. Read the page in pieces (tiles or columns) or with another engine; do not publish it as the page. |
| `model_commentary` | A generative reader added a note about the image or its own reading ("Note: the page is a low-resolution microfilm image …"). The note is not on the page. | Check how much of the page the reading covers, and remove nothing by hand: read the page again or with another engine. |
| `ocr_text_layer` | The `alto` adapter writes it on every reading with text: ALTO is an earlier OCR of the scan. From `pdf_text`: the PDF's text layer is an earlier OCR of a scanned image, not text the author typeset. `pdf_text` recognises one by any of four signals, and records which fired in the page's `confidence_detail.ocr_text_layer` in `quality.jsonl`: `font` (drawn in `GlyphLessFont`, used by Tesseract, OCRmyPDF and the Internet Archive); `invisible_text` (most of the text drawn invisibly, on a page an image covers); `text_under_image` (most of the text painted before an opaque image that covers the page, as ABBYY FineReader does; an image drawn with transparency, at an alpha below 1, through a soft mask or in a blend mode other than Normal, as a watermark is stamped, hides nothing); `producer` (the PDF's Producer or Creator names OCR software such as ABBYY, FineReader, LuraDocument, Tesseract, OCRmyPDF, OmniPage or Readiris, on a page an image covers; the name is recorded). A born-digital page with a background picture under visible text, or a see-through watermark over it, raises none of them unless its Producer names OCR software. Not a hold. In jobs written by 0.8 or later, such a page is read again by local OCR (the `ocr_layer` trigger), so the two readings confirm or challenge each other. In a plain `run` it puts the page in the review queue like any warning, so every page of such a scan read with `pdf_text` is queued. | Treat the layer as one OCR reading among others. A layer that was re-typeset in ordinary fonts with no scan behind it (an OCR-converted e-book) is not recognised. |
| `split_words` | More than one in ten Cyrillic words is a lone letter that is not a one-letter word of Russian, Ukrainian or Belarusian (а, б, в, ж, и, к, о, с, у, я, і, й, з, ў) and not an abbreviation closed by a full stop or a unit (a small letter a space after a number or after млн, млрд, тыс or трлн on the same line, as in "5 м" or "млн т", or joined by · or /, as in "кВт·ч"): words broken by stray spaces, as in a layer that splits off each word's last letter ("Сырны й комбина т"). A letter keeps its combining marks, the titlo of Church Slavonic numerals and abbreviations included ("а҃", "Бг҃ъ"). Judged on pages with 50 or more Cyrillic words. | Treat the reading as damaged even when it looks complete. Read the page with OCR or another engine; do not rejoin the pieces by hand. |
| `capital_sign_in_word` | At least five times, and on at least one word in a hundred, a lower-case Cyrillic letter is followed by a capital Ъ or Ь ("благочестивЪйшихЪ"). No word of either Russian orthography is written so: capitals start a word or fill it. OCR trained on modern text reads ѣ and a final ъ this way. A letter keeps its combining marks, the titlo of Church Slavonic numerals and abbreviations included ("а҃", "Бг҃ъ"). Judged on pages with 50 or more Cyrillic words. | The engine does not fit the page's spelling. For pre-1918 print use a model that writes ѣ, such as `orus` (see [engine recipes](engine-recipes.md)). |
| `labels_without_values` | Ten or more lines end in a dot leader (four dots, or three spaced ones) with no number after it (a page number in roman numerals counts as one), and the page has fewer lines of numbers than half of those: a table whose labels were kept and whose values were lost. A blank form, its labels followed by empty leaders, reads the same and is held too: its text cannot tell it from a table that lost its values. | Read the table with OCR or another engine, and check the values against the page. |
| `alto_missing` | The `alto` adapter found no ALTO file for the page under its `dir` and `name` template, so it returned an empty reading. Some collections publish no ALTO for some reels or pages. The empty reading is held as a blank candidate, so in a job the page climbs to local OCR, which settles the hold if it reads clean text. | Check that `name` gives the files' real names (for example `{page:04d}.xml` for `0001.xml`) and that the page numbering matches. Otherwise the page is read by OCR only. |
| `alto_invalid` | The page's ALTO file could not be used: it resolves outside `dir` through a symbolic link (refused), is larger than 32 MiB, holds more than 20,000 words, is not well-formed XML, a DOCTYPE or entity declaration (refused for safety), more or fewer than one `Page`, or a `MeasurementUnit` other than `pixel`, `mm10` or `inch1200`. The adapter returned an empty reading, held like `alto_missing`. | Open the file and check it is the ALTO for this page. A file holding several pages has to be split first. |
| `alto_geometry_mismatch` | The ALTO page's word boxes cannot be placed on the PDF page: the ALTO page declares no size, its shape differs from the page's by more than 2%, a word box is empty or reaches outside the page, a word has no box (its words then keep the file's order), or `pdfinfo` could not give the page's size. The text is kept; the boxes are not trusted. Not a hold. | Check that the ALTO file belongs to this page image and was made from the same scan (not a crop or a different master). The text is still one OCR reading among others. |
| `script_mismatch` | When a script is configured, fewer than half of the page's letters match it. The wrong language/model or a mixed-script page may be involved. | Check the declared `language.script` and page content. Correct the configuration or review the mixed-script source; PageLedger does not guess a language. |
| `historical_letters_lost` | With `orthography: prereform`, a large Cyrillic output lacks expected historical letters and word-final hard signs. OCR may have modernized the spelling. | Compare against the page and use a model suited to pre-reform spelling. Do not restore letters automatically. |
| `render_dpi_capped` | PDF OCR rendered below the requested DPI to fit the configured pixel limit. | Inspect the rendered output. If it is not legible, increase `max_render_pixels` within available memory or use a different source. |
| `render_upsampled` | An engine rendered the page at more than 1.5 times the pixels of its source image along the long side: a small scan, such as a 1,600-pixel JPEG, read as if it were large. The extra pixels hold no extra detail. Not a hold; in a plain run it puts the page in the review queue like any warning. | Judge the reading by the source image, not by the render. Look for a higher-resolution copy of the page (an archive's master, a IIIF image at full size) and read that. If the image declares a wrong, low density (some write 72 pixels per inch), `pdf_ocr` renders it larger than it is; a lower `dpi` avoids that. |
| `tile_failed` | A job read the page in tiles ([Read dense pages in tiles](processing-spec.md#read-dense-pages-in-tiles)) and the model would not read one or more of them: its answer was cut off, filtered, invalid, or empty where the layout found text. The reading joins the tiles that were read; the attempt's `tiling.failed` names the others. Held as `coverage_defect`. | Look at the named tiles' images in the attempt's `evidence/` folder and read those parts of the page another way; the reading lacks them. |
| `tile_edge_cut` | A tile's edge runs through a line that a literal engine (the job's OCR, or ALTO) found on the page, by more than a quarter of a line's height, so that line may be read half in one tile and half in the next, or not at all. The attempt's `tiling.edge_cut` names the tiles. Held as `coverage_defect`. | Compare the lines at those tile edges with the page image. Dense print with no whitespace between lines causes it; a higher `max_tiles` or a larger `max_image_side` gives the planner more room. |
| `tile_text_small` | Some tile's lines render below `min_line_pixels` (a merged column, or a page with more tiles needed than `max_tiles` allows). Not a hold. | Expect more misreadings in those tiles; raise `max_tiles` or `max_image_side` if the model misreads small print. |
| `tile_layout_missing` | The page had no layout to cut along (the layout stage read nothing there, or failed), so it was read as one tile, the whole page. Not a hold. | Nothing to fix in the reading; the page was simply not cut up. Check why the layout stage found no text if the page is dense. |
| `alto_layout_rejected` | The layout stage's boxes (an ALTO file, say) disagree with where the job's own OCR found text: fewer than nine in ten of its word centres fall inside an OCR line. The tiles were cut along the OCR's layout instead. Not a hold. | Check that the ALTO files belong to these page images (the same scan, not a crop or another master). |
| `no_text_layer` | The source is page images, which have no text layer, so `pdf_text` read nothing. In a job the page goes on to OCR. | Nothing; read the page with OCR. |
| `coverage_defect`, `clipped_text`, `truncated_text`, `output_truncated`, `missing_content` | Adapter or reviewer evidence indicates omitted, clipped, or truncated content. | Compare the full page and output. Use another extraction stage or source; a clean later result does not erase the concern. |
| `missing_required_columns` | A required table column was not found in aligned output. | Check the schema aliases and source table. Correct the schema only when the header correspondence is clear; otherwise review the table. |
| `numeric_column_conflict`, `column_conflict` | Numeric values or their column associations conflict. | Compare named columns and row identities with the source. Do not resolve by matching totals alone. |
| `source_defect` | Evidence indicates damage or missing content in the source itself. | Locate a better or alternate source. Further OCR cannot recover marks that are absent. |
| `blank`, `blank_candidate` | Output is empty or the page was classified as blank; blankness has not necessarily been confirmed by a person. | Inspect the page. Record `reviewed_blank` only when the source page is blank. |
| `illustration`, `handwriting`, `unreadable` | The page is described as image-only illustration, handwriting, or unreadable. | Confirm the description against the source and record a review decision. Use a specialized process if needed; these labels alone do not confirm text. |
| `sparse`, `fragmented`, `joined`, `unknown` | Structural classification indicates uncertain or limited text shape. Short correct pages can also be sparse. | Inspect the page. OCR may help if content is absent, but do not treat the classification alone as proof of a defect. |
| `numeric_disagreement`, `engine_disagreement` | Two clean attempts disagree on numbers or on less than 60% of words. Engines can share errors, too. In jobs written by 0.9, an inherited OCR layer without a warning of its own is one of the attempts compared for word agreement (not for numbers); its disagreement holds the page but does not send it to another stage. | Compare both readings against the source. Agreement is evidence, not proof. |
| `unconfirmed_model_output` | A generative adapter, such as `vision`, wrote the reading. A plain run holds every such page; a job holds it until a clean reading from another engine agrees. | Confirm it against the source, or read the page with another engine and compare, before accepting it. |

Adapter-specific warnings may also appear in the page's `warnings` list. Their
meaning depends on the adapter; consult that adapter's documentation and inspect
the source evidence. The built-in PDF OCR reports `render_dpi_capped` when it
lowers resolution to fit its pixel limit, the `alto` adapter reports
`alto_missing`, `alto_invalid` and `alto_geometry_mismatch`, and the rendering
engines report `render_upsampled` when they render beyond the source image's pixels.

## Document-job dispositions

| Disposition | Meaning | What to do next |
|---|---|---|
| `pending` | No attempt has been recorded. | Run or resume the configured job. |
| `unreviewed_text` | Usable text is selected, but no human receipt confirms it. | Compare it with the source and record a bound review if review is required. |
| `coverage_defect` | A warning or structure check suggests possible missing or damaged content. In jobs written by 0.8 or later, also: two later readings that agree with each other outvoted the PDF's text layer, or a model's reading has under half the words of the literal OCR. In jobs written by 0.9, also: a literal reading lost the pre-reform letters another reading of the page kept (see [hold policy 0.9](processing-spec.md#hold-policy-09)). | Inspect source coverage; use another configured stage or an alternate source. |
| `low_confidence` | The engine doubts some words; this is distinct from missing-content evidence. | Inspect uncertain passages and consider better resolution, language settings, or another engine. |
| `numeric_column_conflict` | Structured numeric evidence disagrees or cannot be safely checked. | Check the named columns and row identities against the source. |
| `engine_disagreement` | Independent readings disagree substantially. | Review the source and both attempts. |
| `numeric_disagreement` | Independent readings differ in one or more numbers. | Verify each number against the source. |
| `unconfirmed_model_output` | Selected generative-model text lacks independent agreement. | Compare with the source or obtain an independent extraction. |
| `contested` | With `processing.contest`, the selected model reading differs from the literal reading in places no rule or high-confidence adjudication settled. The spans are in `contested/<page_id>.json`. | Answer the open spans with `pageledger adjudicate`, or review the page. A literal non-word kept open by the misprint guard may be what the page prints. |
| `adjudicated_text` | Every word-level difference between a model's reading and the literal reading is settled, or there is none. Punctuation is not compared. No person has read the page. | Publish it if adjudicated text is enough for your use; review it to make it `reviewed_text`. |
| `blank_candidate` | No usable text was extracted; visual blankness is not established. If the hold came only from an empty reading, a later non-generative engine that reads clean text from the page clears it; an engine's own `blank` judgement stays. | Inspect the source; record `reviewed_blank` only if confirmed. |
| `provider_failure` | The latest extraction attempt failed. | Read the safe failure code and adapter guidance. Resolve setup or input problems before a deliberate new attempt. |
| `outcome_unknown` | A request may have been sent, but no durable result was saved. | Investigate adapter/provider records before starting new work; automatic retry is not safe. |
| `source_defect` | A person or adapter identified a source problem. | Seek an alternate source; review does not restore missing content. |
| `illustration`, `handwriting`, `unreadable` | A human classified the page's content. | The classification itself remains unresolved; document any other transcription or preservation workflow separately. |
| `reviewed_text` | A human receipt binds a selected completed output, or a person's correction of it, to the source page. | Keep the receipt with the job; it records a review, not a guarantee of perfection. |
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

## In plain words

The report, the lines `process` prints and the review view describe each code in
these words. Where a person reads first (the report's opening and page table, the
terminal, the review view's page list) only the words are shown; the report's record
section and the review view's page details give the codes, to look up here. The words
come from `pageledger/wording.py`, and a test keeps this section equal to it.

### Hold reasons and page states

| Code | Words |
|---|---|
| `source_defect` | The source page itself is damaged or incomplete |
| `numeric_column_conflict` | Numbers in a table do not fit their columns or rows |
| `numeric_disagreement` | Two readings give different numbers |
| `engine_disagreement` | Two readings share fewer than 60% of their words |
| `unconfirmed_model_output` | A generative model wrote the text, and no other engine confirmed it |
| `contested` | The model's and the OCR's readings differ in places not yet settled |
| `coverage_defect` | Text may be missing or damaged in the reading |
| `low_confidence` | The OCR engine was unsure of many words |
| `handwriting` | An engine judged the page to be handwriting |
| `unreadable` | An engine judged the page unreadable |
| `illustration` | An engine judged the page to be an illustration, not text |
| `blank_candidate` | No reading found text, or one came back empty: the page may be blank |
| `provider_failure` | The last attempt to read the page failed |
| `outcome_unknown` | A request to read the page may have been sent, but no result was saved |
| `pending` | Not read yet |
| `unreviewed_text` | Text selected and nothing held the page; no person has checked it |
| `adjudicated_text` | Every difference between the model's and the OCR's readings is settled; no person has read the page |

On a page whose selected reading has text, `blank_candidate` is shown only when
nothing else holds the page; the record still lists it. It then reads: An earlier
reading was empty, and no reading without a hold of its own confirms the page's text.
When the empty reading is the page's own text on a scanned PDF or a page image, it
reads: The page is a scan with no text of its own, and no other reading confirms the
OCR. When no reading was empty, it reads: Very little text was read, and nothing
else confirms it: the page may be blank (a reading had fewer than ten characters);
or else: An engine judged the page blank, though a reading of it has text.

Where the job recorded what caused a hold (report format 0.9), the hold is said by its
cause instead: which reading, by its name, and what is wrong with it, in the words of the
table under [What is wrong with a reading](#what-is-wrong-with-a-reading). When the
flawed reading is not the selected one, the words say so: "the selected text is
Tesseract OCR (eng); the PDF's own text (an earlier OCR), the only other reading, has
words run together, so their comparison does not count and nothing confirms the selected
text". An earlier OCR's own warnings hold a page only while nothing confirms the selected
reading (see [hold policy 0.9](processing-spec.md#hold-policy-09)); any other reading's
hold holds the page whatever the selected reading says, and is said as "another reading of
the page". Where a person reads first, at most two causes are given; the review view's
page section and the report's record give every one.

### Review decisions

| Code | Words |
|---|---|
| `reviewed_text` | A person checked the text against the page |
| `reviewed_blank` | A person confirmed the page is blank |
| `illustration` | A person recorded the page as an illustration |
| `handwriting` | A person recorded the page as handwriting |
| `unreadable` | A person recorded the page as unreadable |
| `source_defect` | A person recorded a problem with the source page |

### Warnings

A warning that holds no page is listed once in the report's "Worth knowing" section,
with what to do about it.

| Code | Words | What to do, for a warning that holds nothing |
|---|---|---|
| `alto_geometry_mismatch` | The ALTO file's word boxes do not fit this page, so they are not trusted. | Check that the ALTO files come from the same scan as the pages. |
| `alto_invalid` | The page's ALTO file could not be used. | Open the ALTO file and check that it is this page's. |
| `alto_layout_rejected` | The ALTO layout disagreed with where the OCR found text, so the tiles followed the OCR. | Check that the ALTO files belong to these page images. |
| `alto_missing` | No ALTO file was found for this page. | Check the adapter's `name` template against the files' real names. |
| `blank` | An engine judged the page blank. | |
| `blank_candidate` | The page may be blank. | |
| `capital_sign_in_word` | Capital Ъ or Ь stand inside lower-case words: OCR trained on modern spelling misreading pre-reform print. | |
| `clipped_text` | The engine reports that some text was clipped. | |
| `column_conflict` | Values and their table columns conflict. | |
| `control_characters` | The reading holds control characters that stand for no visible text. | |
| `coverage_defect` | Text may be missing from the reading. | |
| `digits_only_text` | The reading is nearly all digits, as when a table's words were lost. | |
| `empty_text` | The reading is empty: it has no letters or digits. | |
| `engine_disagreement` | Two readings share fewer than 60% of their words. | |
| `foreign_script_characters` | A few letters from an unrelated script appear, usually symbols read through the wrong font map. | |
| `fragmented` | The page's text is broken into fragments. | |
| `fragmented_text` | Many words are unusually short, as if broken into pieces. | |
| `handwriting` | An engine judged the page to be handwriting. | |
| `historical_letters_lost` | The job expects pre-reform spelling, but the reading lacks its letters (ѣ, і, final ъ). | |
| `historical_orthography` | The text has pre-reform Russian spelling (letters such as ѣ and і), which an engine trained on modern spelling cannot write. | Stock OCR models for modern Russian, such as Tesseract `rus`, cannot write ѣ, і, ѳ or ѵ, so their readings drop or garble these letters. For pre-1918 print, try a model that writes them on a few pages first, such as the community `orus` model: in PageLedger's test it kept more of these letters, but made more errors overall ([engine recipes](https://github.com/peterbussch/pageledger/blob/v0.9.0/docs/engine-recipes.md#tesseract-with-the-community-orus-model-macos-and-linux)). |
| `illustration` | An engine judged the page to be an illustration. | |
| `instruction_echo` | The reading holds a chat-template marker: a model may have returned its own instructions. | |
| `joined` | Words appear run together. | |
| `joined_text` | Many Latin-script words are run together, with few spaces. | |
| `labels_without_values` | Table labels end in dot leaders with no number after them: the values seem lost. | |
| `low_confidence` | The engine marked at least a quarter of the words as uncertain. | |
| `missing_content` | Content is reported missing from the reading. | |
| `missing_required_columns` | A required table column was not found. | |
| `mixed_script_tokens` | Some words mix look-alike Latin, Cyrillic or Greek letters. | |
| `model_commentary` | A generative reader added a note of its own that is not on the page. | |
| `multi_column_rows` | The engine read several columns side by side as single lines. | Check the line order against the page; for RapidOCR set `reading_order: columns` and read the page again. |
| `no_text_layer` | The source is page images, which carry no text of their own. | Nothing: these pages are read by OCR. |
| `numeric_column_conflict` | Numbers conflict with their table columns. | |
| `numeric_disagreement` | Two readings give different numbers. | |
| `ocr_text_layer` | The PDF's text layer is an earlier OCR of the scan, not typeset text. | Treat the layer as one OCR reading among others; where the job read the page again, it compared the two. |
| `output_inflation` | A rerun's reading is far longer than the earlier one, which can mean repeated or invented text. | Compare both readings with the page; longer is not necessarily more complete. |
| `output_truncated` | The engine's output was cut off. | |
| `partial_transcription` | A generative reader says it left some text out. | |
| `private_use_characters` | Several characters use a font's private codes, which may stand for lost digits or letters. | |
| `render_dpi_capped` | The page was rendered for OCR at a lower resolution than asked, to stay within the pixel limit. | If a reading is poor, raise `max_render_pixels` as far as memory allows and read the page again. |
| `render_upsampled` | The page was rendered at more pixels than its source image has; the extra pixels add no detail. | Judge the reading by the source image, and look for a higher-resolution copy of the page. |
| `repeated_page_text` | The same short text makes up the whole reading of several pages, often a stamp or watermark. | |
| `repetition_loop` | The text repeats in a loop, as a vision model sometimes does. | |
| `replacement_characters` | The reading has replacement characters where bytes could not be decoded. | |
| `script_mismatch` | Fewer than half the letters are in the configured script. | |
| `short_text` | Fewer than ten characters were read. | Accept the text only if the page really holds this little. |
| `source_defect` | The source itself appears damaged or incomplete. | |
| `sparse` | Very little text was found on the page. | |
| `split_words` | Many words are broken by stray spaces. | |
| `suspicious_embedded_text_delta` | The OCR reading and the PDF's text layer differ greatly in length. | |
| `suspicious_symbol_density` | The reading has many unusual symbols, often from a damaged text layer. | |
| `tile_edge_cut` | A tile's edge cuts through a line of text, which may be read twice or not at all. | |
| `tile_failed` | The model would not read one or more tiles of the page; their text is missing. | |
| `tile_layout_missing` | There was no layout to cut the page along, so it was read whole. | Nothing to fix: the page was read whole rather than in tiles. |
| `tile_text_small` | In some tiles the print is smaller than the configured minimum. | Expect more misreadings there; a higher `max_tiles` or `max_image_side` helps. |
| `truncated_text` | The reading is truncated. | |
| `unconfirmed_model_output` | A generative model wrote the reading, and no other engine confirmed it. | |
| `unknown` | The page's text could not be classified. | |
| `unreadable` | An engine judged the page unreadable. | |

A code not in this table, from a custom adapter, is shown as: A warning from the engine's adapter; see that adapter's documentation.

### What is wrong with a reading

Each cause of a hold, as the words that follow a reading's name ("Tesseract OCR (rus)
wrote a capital Ъ or Ь inside words, …"). Most are warning or classification codes;
`empty_reading` and `alignment` are a reading with no text and a failed table check, and
`letters_lost`, `layer_outvoted` and `reader_short` are holds the job's own rules place.

| Cause | After the reading's name |
|---|---|
| `alignment` | does not fit the table structure the job expected |
| `blank` | judged the page blank |
| `blank_candidate` | found that the page may be blank |
| `capital_sign_in_word` | wrote a capital Ъ or Ь inside words, as OCR for modern Russian does with pre-1918 letters |
| `clipped_text` | had some of its text clipped, its engine reports |
| `column_conflict` | has values that conflict with their table columns |
| `control_characters` | holds control characters that stand for no visible text |
| `coverage_defect` | may be missing text |
| `digits_only_text` | is nearly all digits, as when a table's words are lost |
| `empty_reading` | is empty |
| `empty_text` | is empty, with no letters or digits |
| `foreign_script_characters` | has a few letters from an unrelated script, usually symbols read through the wrong font map |
| `fragmented` | has its text broken into fragments |
| `fragmented_text` | has many words broken into pieces |
| `handwriting` | judged the page to be handwriting |
| `historical_letters_lost` | lacks the pre-reform letters (ѣ, і, final ъ) the job expects |
| `illustration` | judged the page to be an illustration |
| `instruction_echo` | holds a chat-template marker: the model may have returned its own instructions |
| `joined` | has words run together |
| `joined_text` | has words run together |
| `labels_without_values` | has table labels that end in dot leaders with no number after them: the values seem lost |
| `layer_outvoted` | disagrees with two of the job's readings that agree with each other |
| `letters_lost` | lost the old-spelling letters (such as ѣ) that a different reading kept |
| `low_confidence` | was unsure of many words |
| `missing_content` | is missing content, its engine reports |
| `missing_required_columns` | lacks a table column the job requires |
| `mixed_script_tokens` | has words that mix look-alike Latin, Cyrillic or Greek letters |
| `model_commentary` | added a note of its own that is not on the page |
| `numeric_column_conflict` | has numbers that conflict with their table columns |
| `output_truncated` | was cut off |
| `partial_transcription` | says it left some text out |
| `private_use_characters` | uses a font's private codes for several characters, which may stand for lost digits or letters |
| `reader_short` | has far fewer words than the OCR reading |
| `repeated_page_text` | is the same short text as several other pages, often a stamp or watermark |
| `repetition_loop` | repeats itself in a loop |
| `replacement_characters` | has replacement characters where bytes could not be decoded |
| `script_mismatch` | has fewer than half its letters in the configured script |
| `source_defect` | found the source page damaged or incomplete |
| `sparse` | has very little text |
| `split_words` | has many words broken by stray spaces |
| `suspicious_embedded_text_delta` | is far longer or shorter than the PDF's own text |
| `suspicious_symbol_density` | has many unusual symbols, often from a damaged text layer |
| `tile_edge_cut` | has a line of text cut by a tile's edge, which may be read twice or not at all |
| `tile_failed` | lacks the text of one or more tiles the model would not read |
| `truncated_text` | is truncated |
| `unknown` | could not be classified |
| `unreadable` | judged the page unreadable |
