# Non-English and historical OCR

PageLedger's quality signals are language-neutral by construction (the
tokenizer is Unicode-aware, and standard European typography like
«guillemets» and em dashes is not treated as garble). This page walks two
real Russian documents through the tool: a modern born-digital report and
an 1850 scan in pre-reform orthography. The commands and numbers are from
actual runs; substitute your own files.

## Language packs come first

`pdf_ocr` passes `run.adapter_options.lang` straight to Tesseract, and a
missing language pack is the most common first failure. Check before you
run:

```bash
pageledger doctor
```

```
Command tesseract: /Users/you/.local/bin/tesseract (tesseract 5.5.2)
OCR languages (125): afr, amh, ara, ..., rus, ...
```

Those names are the valid `lang:` values. If you configure a language that
is not installed, the run refuses before extracting anything and prints
the installed list. Set DPI and language in the config:

```yaml
run:
  adapter: pdf_ocr
  adapter_options:
    dpi: 400
    lang: rus        # or rus+deu for mixed collections
```

## A modern born-digital document

A 259-page Russian policy report with a real text layer needs no OCR at
all. `pdf_text` extracts clean Cyrillic:

```bash
pageledger run report.pdf --adapter pdf_text --out runs/report
```

```
Pages: 259 extracted / 259 total
Quality warning pages: 20
```

The 20 flags are page-structure evidence, not extraction errors:
`suspicious_symbol_density` on chart-heavy pages, `fragmented_text` on
questionnaire pages full of `___` blanks, `short_text` on section
dividers. One defect the signals cannot see: text layers produced by some
layout engines insert mid-word spaces («С анкционная» for «Санкционная»).
That comes from the source PDF, passes every shape heuristic, and is
exactly the kind of thing the review queue and sampling are for.

## An 1850 scan in pre-reform orthography

The second document is the *Военно-статистическое обозрѣніе Харьковской
губерніи* (Military-Statistical Review of Kharkov Governorate, 1850): 178
pages, image-only scan, set in the orthography Russian used before the
1918 reform (ѣ, і, ѳ, word-final ъ). The file itself is not in this
repository, but any pre-1918 Russian printing behaves the same way.

Sample a slice before committing to 178 pages. `--pages` keeps the source
page numbers, so the ledger stays truthful about which physical pages you
extracted:

```bash
pageledger run 1850-review.pdf --config ocr-rus.yml \
    --out runs/review-sample --pages "1-8,81-88"
```

```
Pages: 16 extracted / 16 total
Quality warning pages: 16
```

Every page came back flagged, each with a reason a historian can act on:

```
doc_0001_page_0007  conf=0.31  ['historical_orthography', 'low_confidence']
doc_0001_page_0081  conf=0.70  ['historical_orthography', 'low_confidence']
doc_0001_page_0082  conf=0.78  ['historical_orthography']
doc_0001_page_0004  conf=None  ['empty_text']
```

Two signals are doing the work here.

**`low_confidence`** comes from Tesseract's own per-word confidence
(recorded in `quality.jsonl → confidence_detail`). On this scan, 19–86% of
words per page fell below engine confidence 60. Shape heuristics alone had
flagged only 3 of these 16 pages; the engine knew better, and now the
ledger records it.

**`historical_orthography`** is the interesting one. Tesseract's modern
`rus` model cannot emit the abolished letters, so it substitutes:
«уѣздовъ» comes back as «уфэдовъ», «губернія» as «губерн!я». Zero ѣ/ѳ/ѵ
survived OCR across all 16 pages. What does survive is the word-final hard
sign, which was mandatory before 1918 and is absent from modern Russian:
this scan shows 21 terminal ъ per 100 tokens against 0.00 in the modern
report. That density is the signal. The warning tells you the page is
pre-reform and your OCR model is mismatched with it, which is *why* the
character-level output is degraded.

Two calibration notes. The abolished-letter count deliberately excludes і,
which is standard modern Ukrainian and Belarusian; Ukrainian archives do
not get false-flagged. And no official `rus_old` traineddata exists in the
Tesseract project; community-trained historical models do, with varying
quality, so test one against `quality.jsonl` before trusting it.

## Normalizing pre-reform orthography

For search, matching, and modern-corpus work you often want ѣ→е, і→и,
ѳ→ф, ѵ→и, and word-final ъ dropped (while keeping it in съѣздъ→съезд,
where it carries meaning).
[`examples/prereform_normalizer_adapter.py`](../examples/prereform_normalizer_adapter.py)
wraps `pdf_ocr` with exactly that transform and counts every replacement
into a result warning, so the rewrite is recorded in provenance rather
than applied silently.

Run it as a separate run (or a rerun) so the un-normalized run remains the
original evidence:

```bash
pageledger run 1850-review.pdf --config normalized.yml \
    --out runs/review-normalized --pages "1-8,81-88" --adapter-path examples
pageledger compare-runs runs/review-sample runs/review-normalized
```

Character rules cannot resolve the n:m cases (міръ "world" and миръ
"peace" both normalize to мир), and on OCR output they mostly clean the
hard signs, since the abolished letters were already destroyed upstream.
On born-digital pre-reform text (digital-library transcriptions), the same
adapter does the full canonicalization.

## What to expect elsewhere

Nothing here is Russian-specific except the orthography tables. The
confidence signals, `--pages` sampling, language preflight, and the
escalate-what-failed loop (see
[`examples/jfk-scanned-archive.md`](examples/jfk-scanned-archive.md))
apply to any Tesseract-supported script. Pre-reform detection has obvious
siblings (long s and Fraktur in German, for instance); they are not built
in, but `quality.jsonl` carries the raw per-page evidence a custom check
would need.
