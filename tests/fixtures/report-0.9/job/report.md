# book.txt: document job report

Finished: 3 pages read; 1 needs a person.

Source: [book.txt][source]; 3 of its 3 pages selected, 3 read.

- Need a person: 1 page (2):
  - Very little text was read, and nothing else confirms it: the page may be blank: 1 page (2).
- A person checked the text against the page: 2 pages (1, 3).

Next: in this job's folder, `pageledger review .` writes the review view, where you check the page that needs a person and record what you decide.

## Worth knowing

These warnings hold no page, but they bear on how far to trust the text.

- On 1 page (2) (`short_text`): Fewer than ten characters were read. What to do: Accept the text only if the page really holds this little.

## Pages

Pages that need a person come first. The review view (`pageledger review .` in this job's folder) shows each page with every reading of it, and is where decisions and corrections are made.

| Page | Held for | Selected text | Review |
| --- | --- | --- | --- |
| 2 | Very little text was read, and nothing else confirms it: the page may be blank. | [The text file](<attempts/local_text/raw/doc_0001_page_0002.txt>) | not checked |
| 1 | The text file has very little text. | [A person's correction](<corrections/doc_0001_page_0001-dd291b879eceade37f8b6abbd2a242e694051de619ab0deb15f70d911fb46475.txt>) of the text file: 2 words changed, 0 added, 8 removed | A person checked the text against the page: A. Person, 2026-11-13T23:22:48Z, “Review sheet decision: correct:p1.txt” |
| 3 | The text file has many unusual symbols, often from a damaged text layer. | [The text file](<attempts/local_text/raw/doc_0001_page_0003.txt>) | A person checked the text against the page: A. Person, 2026-11-13T23:22:48Z, “Review sheet decision: accept” |

## For the record

Everything below is in document.json, of which this report is a rendering. The codes in backticks are explained in [PageLedger's warnings](https://github.com/peterbussch/pageledger/blob/v0.9.0/docs/warnings.md).

- Job: `job_dbe0b4f320c144528c7b440c5250c872`, created 2026-11-13T23:22:48Z; hold policy 0.9; report format 0.9.
- Source: [../../book.txt][source]
- Source SHA-256: `008f52e0d55142a405da902ec254abb7be8de5e9f62d8fb8e0343918e121f5cc`
- Read by: the `local_text` stage, the text file (`text`).
- Pages: 3 selected of 3 in the source; 3 read; 3 with selected text; 1 not yet reviewed as text or blank (`unresolved_pages`).
- Pages whose text a person corrected: 1.

### Each page

Each page's number in the source, its state and every hold the job recorded; then what caused each hold, on which reading; each reading, by its ID (a link to its text), with its outcome, failure and warnings; and the comparisons the job recorded and whether it counted them.

- Page 1: `reviewed_text`; held for `coverage_defect`; reviewed as `reviewed_text`.
  - Hold: `coverage_defect` from `sparse` on `local_text:doc_0001_page_0001`: the text file has very little text.
  - The text file: [local_text:doc_0001_page_0001](<attempts/local_text/raw/doc_0001_page_0001.txt>) completed, selected.
  - Selected text: a [correction](<corrections/doc_0001_page_0001-dd291b879eceade37f8b6abbd2a242e694051de619ab0deb15f70d911fb46475.txt>) of `local_text:doc_0001_page_0001` by A. Person at 2026-11-13T23:22:48Z.
- Page 2: `blank_candidate`; held for `blank_candidate`.
  - Hold: `blank_candidate` from `blank` on `local_text:doc_0001_page_0002`: the text file judged the page blank.
  - The text file: [local_text:doc_0001_page_0002](<attempts/local_text/raw/doc_0001_page_0002.txt>) completed, `short_text`, selected.
- Page 3: `reviewed_text`; held for `coverage_defect`; reviewed as `reviewed_text`.
  - Hold: `coverage_defect` from `suspicious_symbol_density` on `local_text:doc_0001_page_0003`: the text file has many unusual symbols, often from a damaged text layer.
  - The text file: [local_text:doc_0001_page_0003](<attempts/local_text/raw/doc_0001_page_0003.txt>) completed, `suspicious_symbol_density`, selected.

### How the job ran

| Stage | Pages read | Tokens | Seconds |
| --- | --- | --- | --- |
| `local_text` | 3 | not reported | 0 |

Readings sent on to the next stage for a reason other than a hold, by trigger (a page can be sent on more than once): none.

- Readings made: 3 (one per page and stage).
- Calls to an image model: 0.
- Tokens: unknown: not every engine reported them (reported: 0).
- Cost: unknown: not every engine reported a cost.
- Limits: max_image_pages 0.
- PDF annotations (notes and links attached to pages): none (0). Counting them says nothing about whether the body text or its citations were extracted completely.

### Source custody

PageLedger records what it knows of the source file; it does not preserve or remove it.

- Captured (the file was present when the job ran): present
- Preservation elsewhere verified: unverified
- Assessed for removal: not_assessed
- Removed: no

### Files

- Transcript: [transcript.md](<transcript.md>), SHA-256 `6af86dc7dfded883363abe40949ea20fc2cc0f70f47fb242f4dfac5abab1451f`
- Next action recorded by the job: Check the pages against the source, those that need a person first, and record decisions with pageledger review.

[source]: <../../book.txt>
