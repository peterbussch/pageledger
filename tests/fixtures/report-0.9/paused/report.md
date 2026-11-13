# source.txt: document job report

Paused at a limit. Reached max_image_pages (1). Review the retained results, or continue with: pageledger resume . --raise-limit max_image_pages=N

Source: [source.txt][source]; 3 of its 3 pages selected, 3 read.

- Need a person: 1 page (2):
  - The engine local_text may be missing text; so do the engine local_ocr and the engine image: 1 page (2).
- Text selected and nothing held the page; no person has checked it: 1 page (1).
- Not finished: waiting for the engine image: 1 page (3).

Next, for the pages read so far: in this job's folder, `pageledger review .` writes the review view, where you check the page that needs a person and record what you decide.

## Pages

Pages that need a person come first. The review view (`pageledger review .` in this job's folder) shows each page with every reading of it, and is where decisions and corrections are made.

| Page | Held for | Selected text | Review |
| --- | --- | --- | --- |
| 2 | The engine local_text may be missing text; so do the engine local_ocr and the engine image. | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0002.txt>) | not checked |
| 3 | Not finished: waiting for the engine image. Held so far: The engine local_text may be missing text; so does the engine local_ocr. | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0003.txt>) | not checked |
| 1 | nothing | [The engine local_text](<attempts/local_text/raw/doc_0001_page_0001.txt>) | not checked |

For pages not finished, the text and holds are those so far: the stages still to run may select another reading, and add or settle holds.

## For the record

Everything below is in document.json, of which this report is a rendering. The codes in backticks are explained in PageLedger's docs/warnings.md.

- Job: `job_cd39b54c65974630aec8fc127565b389`, created 2026-11-13T23:17:33Z; hold policy 0.9; report format 0.9.
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
  - The engine local_text: [local_text:doc_0001_page_0003](<attempts/local_text/raw/doc_0001_page_0003.txt>) completed, `coverage_defect`, selected.
  - The engine local_ocr: [local_ocr-0003:doc_0001_page_0003](<attempts/local_ocr-0003/raw/doc_0001_page_0003.txt>) completed, `coverage_defect`.
  - Compared `local_text:doc_0001_page_0003` and `local_ocr-0003:doc_0001_page_0003`: 100% of words agree; not counted, because `local_ocr-0003:doc_0001_page_0003` is held itself.
  - Waiting for the `image` stage.

### How the job ran

| Stage | Pages read | Tokens | Seconds |
| --- | --- | --- | --- |
| `local_text` | 3 | 30 | 0 |
| `local_ocr` | 2 | 20 | 0 |
| `image` | 1 | 10 | 0 |

Readings sent on to the next stage for a reason other than a hold, by trigger (a page can be sent on more than once): none.

- Readings made: 6 (one per page and stage).
- Calls to an image model: 1.
- Tokens: 60.
- Cost: unknown: not every engine reported a cost.
- Limits: max_image_pages 1, max_attempt_pages 10.
- PDF annotations (notes and links attached to pages): none (0). Counting them says nothing about whether the body text or its citations were extracted completely.

### Source custody

PageLedger records what it knows of the source file; it does not preserve or remove it.

- Captured (the file was present when the job ran): present
- Preservation elsewhere verified: unverified
- Assessed for removal: not_assessed
- Removed: no

### Files

- Transcript: [transcript.md](<transcript.md>), SHA-256 `5473c55370b17553249e95f5d8e2870c52b92fec51a05cac27173bbf52081e6d`
- Next action recorded by the job: Reached max_image_pages (1). Review the retained results, or continue with: pageledger resume JOB_DIR --raise-limit max_image_pages=N

[source]: <../source.txt>
