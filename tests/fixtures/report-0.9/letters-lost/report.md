# source.txt: document job report

Finished: 1 page read; 1 needs a person.

Source: [source.txt][source]; 1 of its 3 pages selected, 1 read.

- Need a person: 1 page (1):
  - The selected text is the engine local_text; the engine local_ocr, another reading of the page, lost the old-spelling letters (such as ѣ) that a different reading kept, so it cannot confirm the selected text: 1 page (1).

Next: in this job's folder, `pageledger review .` writes the review view, where you check the page that needs a person and record what you decide.

## Worth knowing

These warnings hold no page, but they bear on how far to trust the text.

- On 1 page (1) (`historical_orthography`): The text has pre-reform Russian spelling (letters such as ѣ and і), which an engine trained on modern spelling cannot write. What to do: Stock OCR models for modern Russian, such as Tesseract `rus`, cannot write ѣ, і, ѳ or ѵ, so their readings drop or garble these letters. For pre-1918 print, try a model that writes them on a few pages first, such as the community `orus` model: in PageLedger's test it kept more of these letters, but made more errors overall ([engine recipes](https://github.com/peterbussch/pageledger/blob/v0.9.0/docs/engine-recipes.md#tesseract-with-the-community-orus-model-macos-and-linux)).
- On 1 page (1) (`ocr_text_layer`): The PDF's text layer is an earlier OCR of the scan, not typeset text. What to do: Treat the layer as one OCR reading among others; where the job read the page again, it compared the two.

## Pages

Pages that need a person come first. The review view (`pageledger review .` in this job's folder) shows each page with every reading of it, and is where decisions and corrections are made.

| Page | Held for | Selected text | Review |
| --- | --- | --- | --- |
| 1 | The selected text is the engine local_text; the engine local_ocr, another reading of the page, lost the old-spelling letters (such as ѣ) that a different reading kept, so it cannot confirm the selected text. | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0001.txt>) | not checked |

## For the record

Everything below is in document.json, of which this report is a rendering. The codes in backticks are explained in [PageLedger's warnings](https://github.com/peterbussch/pageledger/blob/v0.9.0/docs/warnings.md).

- Job: `job_2de5c00e418049babb5a2c68e029b87e`, created 2026-11-13T23:22:47Z; hold policy 0.9; report format 0.9.
- Source: [../source.txt][source]
- Source SHA-256: `b6f60e4092685ffd893a508ab3704696bc506e06c79c4d28fb679bf692a306c9`
- Read by: the `local_text` stage, the engine local_text (`local_text`); the `local_ocr` stage, the engine local_ocr (`local_ocr`); the `image` stage, the engine image (`image`).
- Pages: 1 selected of 3 in the source; 1 read; 1 with selected text; 1 not yet reviewed as text or blank (`unresolved_pages`).

### Each page

Each page's number in the source, its state and every hold the job recorded; then what caused each hold, on which reading; each reading, by its ID (a link to its text), with its outcome, failure and warnings; and the comparisons the job recorded and whether it counted them.

- Page 1: `coverage_defect`; held for `coverage_defect`.
  - Hold: `coverage_defect` from `letters_lost` on `local_ocr-0001:doc_0001_page_0001`: the engine local_ocr lost the old-spelling letters (such as ѣ) that a different reading kept (0 words spelled with ѣ, against 16 in another reading).
  - The engine local_text: [local_text:doc_0001_page_0001](<attempts/local_text/raw/doc_0001_page_0001.txt>) completed, `ocr_text_layer`, `historical_orthography`, selected.
  - The engine local_ocr: [local_ocr-0001:doc_0001_page_0001](<attempts/local_ocr-0001/raw/doc_0001_page_0001.txt>) completed, `historical_orthography`.
  - Compared `local_text:doc_0001_page_0001` and `local_ocr-0001:doc_0001_page_0001`: 50% of words agree; not counted, because `local_ocr-0001:doc_0001_page_0001` is held itself.

### How the job ran

| Stage | Pages read | Tokens | Seconds |
| --- | --- | --- | --- |
| `local_text` | 1 | 10 | 0 |
| `local_ocr` | 1 | 10 | 0 |
| `image` | 0 (no page met its trigger) | not reported | 0 |

Readings sent on to the next stage for a reason other than a hold, by trigger (a page can be sent on more than once): `ocr_layer` 1.

- Readings made: 2 (one per page and stage).
- Calls to an image model: 0.
- Tokens: 20.
- Cost: unknown: not every engine reported a cost.
- Limits: max_attempt_pages 10, max_image_pages 2.
- PDF annotations (notes and links attached to pages): none (0). Counting them says nothing about whether the body text or its citations were extracted completely.

### Source custody

PageLedger records what it knows of the source file; it does not preserve or remove it.

- Captured (the file was present when the job ran): present
- Preservation elsewhere verified: unverified
- Assessed for removal: not_assessed
- Removed: no

### Files

- Transcript: [transcript.md](<transcript.md>), SHA-256 `45e71634082828d28eb5255ee37887a116b57770420079dfb6cc97464fdd8335`
- Next action recorded by the job: Check the pages against the source, those that need a person first, and record decisions with pageledger review.

[source]: <../source.txt>
