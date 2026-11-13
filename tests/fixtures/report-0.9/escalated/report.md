# source.txt: document job report

Finished: 3 pages read; 2 need a person.

Source: [source.txt][source]; 3 of its 3 pages selected, 3 read.

- Need a person: 2 pages (2-3):
  - The engine local_text may be missing text; so do the engine local_ocr and the engine image: 2 pages (2-3).
- Text selected and nothing held the page; no person has checked it: 1 page (1).

Next: in this job's folder, `pageledger review .` writes the review view, where you check the 2 pages that need a person and record what you decide.

## Pages

Pages that need a person come first. The review view (`pageledger review .` in this job's folder) shows each page with every reading of it, and is where decisions and corrections are made.

| Page | Held for | Selected text | Review |
| --- | --- | --- | --- |
| 2 | The engine local_text may be missing text; so do the engine local_ocr and the engine image. | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0002.txt>) | not checked |
| 3 | The engine local_text may be missing text; so do the engine local_ocr and the engine image. | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0003.txt>) | not checked |
| 1 | nothing | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0001.txt>) | not checked |

## For the record

Everything below is in document.json, of which this report is a rendering. The codes in backticks are explained in PageLedger's docs/warnings.md.

- Job: `job_c6b65e2e7315407c89da81b205b948c8`, created 2026-11-13T23:17:32Z; hold policy 0.9; report format 0.9.
- Source: [../source.txt][source]
- Source SHA-256: `b6f60e4092685ffd893a508ab3704696bc506e06c79c4d28fb679bf692a306c9`
- Read by: the `local_text` stage, the engine local_text (`local_text`); the `local_ocr` stage, the engine local_ocr (`local_ocr`); the `image` stage, the engine image (`image`).
- Pages: 3 selected of 3 in the source; 3 read; 3 with selected text; 3 not yet reviewed as text or blank (`unresolved_pages`).

### Each page

Each page's number in the source, its state and every hold the job recorded; then what caused each hold, on which reading; each reading, by its ID (a link to its text), with its outcome, failure and warnings; and the comparisons the job recorded and whether it counted them.

- Page 1: `unreviewed_text`.
  - The engine local_text: [local_text:doc_0001_page_0001](<attempts/local_text/raw/doc_0001_page_0001.txt>) completed, selected.
- Page 2: `coverage_defect`; held for `coverage_defect`.
  - Hold: `coverage_defect` from `coverage_defect` on `local_text:doc_0001_page_0002`: the engine local_text may be missing text.
  - Hold: `coverage_defect` from `coverage_defect` on `local_ocr-0002:doc_0001_page_0002`: the engine local_ocr may be missing text.
  - Hold: `coverage_defect` from `coverage_defect` on `image-0002:doc_0001_page_0002`: the engine image may be missing text.
  - The engine local_text: [local_text:doc_0001_page_0002](<attempts/local_text/raw/doc_0001_page_0002.txt>) completed, `coverage_defect`, selected.
  - The engine local_ocr: [local_ocr-0002:doc_0001_page_0002](<attempts/local_ocr-0002/raw/doc_0001_page_0002.txt>) completed, `coverage_defect`.
  - The engine image: [image-0002:doc_0001_page_0002](<attempts/image-0002/raw/doc_0001_page_0002.txt>) completed, `coverage_defect`.
  - Compared `local_text:doc_0001_page_0002` and `local_ocr-0002:doc_0001_page_0002`: 100% of words agree; not counted, because `local_ocr-0002:doc_0001_page_0002` is held itself.
  - Compared `local_text:doc_0001_page_0002` and `image-0002:doc_0001_page_0002`: 100% of words agree; not counted, because `image-0002:doc_0001_page_0002` is held itself.
  - Compared `local_ocr-0002:doc_0001_page_0002` and `image-0002:doc_0001_page_0002`: 100% of words agree; neither is the selected reading.
- Page 3: `coverage_defect`; held for `coverage_defect`.
  - Hold: `coverage_defect` from `coverage_defect` on `local_text:doc_0001_page_0003`: the engine local_text may be missing text.
  - Hold: `coverage_defect` from `coverage_defect` on `local_ocr-0003:doc_0001_page_0003`: the engine local_ocr may be missing text.
  - Hold: `coverage_defect` from `coverage_defect` on `image-0003:doc_0001_page_0003`: the engine image may be missing text.
  - The engine local_text: [local_text:doc_0001_page_0003](<attempts/local_text/raw/doc_0001_page_0003.txt>) completed, `coverage_defect`, selected.
  - The engine local_ocr: [local_ocr-0003:doc_0001_page_0003](<attempts/local_ocr-0003/raw/doc_0001_page_0003.txt>) completed, `coverage_defect`.
  - The engine image: [image-0003:doc_0001_page_0003](<attempts/image-0003/raw/doc_0001_page_0003.txt>) completed, `coverage_defect`.
  - Compared `local_text:doc_0001_page_0003` and `local_ocr-0003:doc_0001_page_0003`: 100% of words agree; not counted, because `local_ocr-0003:doc_0001_page_0003` is held itself.
  - Compared `local_text:doc_0001_page_0003` and `image-0003:doc_0001_page_0003`: 100% of words agree; not counted, because `image-0003:doc_0001_page_0003` is held itself.
  - Compared `local_ocr-0003:doc_0001_page_0003` and `image-0003:doc_0001_page_0003`: 100% of words agree; neither is the selected reading.

### How the job ran

| Stage | Pages read | Tokens | Seconds |
| --- | --- | --- | --- |
| `local_text` | 3 | 30 | 0 |
| `local_ocr` | 2 | 20 | 0 |
| `image` | 2 | 20 | 0 |

Readings sent on to the next stage for a reason other than a hold, by trigger (a page can be sent on more than once): none.

- Readings made: 7 (one per page and stage).
- Calls to an image model: 2.
- Tokens: 70.
- Cost: unknown: not every engine reported a cost.
- Limits: max_image_pages 2, max_attempt_pages 10.
- PDF annotations (notes and links attached to pages): none (0). Counting them says nothing about whether the body text or its citations were extracted completely.

### Source custody

PageLedger records what it knows of the source file; it does not preserve or remove it.

- Captured (the file was present when the job ran): present
- Preservation elsewhere verified: unverified
- Assessed for removal: not_assessed
- Removed: no

### Files

- Transcript: [transcript.md](<transcript.md>), SHA-256 `9e68a647a46c32ebf0b5953fdf017d9d93f0a7e2549cdd70fc9c66f001b1f5c0`
- Next action recorded by the job: Check the pages against the source, those that need a person first, and record decisions with pageledger review.

[source]: <../source.txt>
