# Scoring transcriptions against references

`scripts/corpus/score.py` measures how closely an engine's text agrees with a
reference transcription, line by line. The references PageLedger uses were read
by frontier vision models and stay provisional until a person checks them, so
every number is labelled **against reference**: it is agreement with another
reading, not accuracy.

```bash
python scripts/corpus/score.py references/ engine-output/
python scripts/corpus/score.py references/ ~/pageledger-corpus-runs/RUN --pages pages.json --json
```

`references/` holds one `<page_id>.txt` per page, where a page ID is a document
ID, `_p` and a four-digit page number, such as `OCR-06_p0037`. The engine side
is either a folder in the same layout or a [corpus run](../corpus/README.md),
whose selected text, time and reported cost are then used. Pages without a
reference are not scored.

## How a line is scored

Each reference line is found in the engine's text wherever it is, because
engines order columns and tables differently. The three engine lines sharing
the most letter trigrams with it are searched, each with its neighbours, for
the stretch of text needing the fewest edits. Engine lines longer than 400
characters, such as whole blocks on one line, are searched in overlapping
pieces. Reference lines with fewer than three characters, such as a lone page
number, are not scored.

Before comparing, both sides are NFC-normalized; table rules and dot leaders
become spaces, as do `{hw:…}` and `[illegible]`; the uncertainty mark `[?]` is
removed; runs of spaces become one. Tables that a vision model writes as HTML
are read as rows of cells.

| Metric | Meaning |
|---|---|
| `cer` | Character edits per reference character, counting Unicode code points. |
| `grapheme_cer` | The same, counting a letter and its combining marks as one character. |
| `normalized_cer`, `normalized_grapheme_cer` | The same after case-folding, writing ѣ, і, ѳ and ѵ as е, и, ф and и, and dropping word-final ъ. |
| `exact_lines` | Share of reference lines found without a single edit. |
| `missing_lines` | Share of reference lines needing edits for more than half their characters. |
| `numbers_found` | Share of the numbers in each table cell of the reference that appear in the matched text; `1 850` and `1850` count as the same number. |
| `historical_kept` | Share of the reference's ѣ, і, ѳ, ѵ and word-final ъ also in the matched text. |
| `historical_added` | Such letters in the matched text beyond the reference's. |

Rates are summed over lines, not averaged over pages. The grapheme count
approximates Unicode's grapheme clusters (UAX #29) with base characters and
their combining marks.

## The report

The Markdown report shows the overall rates, the rates for each stratum when
`--pages` names one per page (a JSON list of `page_id` and `stratum`), and the
five pages with the highest `cer`. It also counts pages without text, pages
with a model loop (PageLedger's `repetition_loop` check) and, for a corpus run,
seconds and reported cost. `--json` adds every line: the reference, the text it
matched and its edits.

For a corpus run, `--stage local_ocr` (or any other stage) scores that
stage's latest attempt on each page instead of the text the job selected, so
one engine can be measured even where the job chose another. Pages the stage
never read are left out, and its failed attempts are counted by failure code.

To score only the development or the held-out documents, pass a JSON object
mapping document IDs to `dev` or `holdout`. The split is by document, so
neighbouring pages never fall on both sides:

```bash
python scripts/corpus/score.py references/ engine-output/ \
  --split holdout --splits document-splits.json
```
