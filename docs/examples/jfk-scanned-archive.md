# Worked Example: OCR a Scanned Government Archive

This walkthrough runs a real declassified document through PageLedger:
HSCA record 180-10147-10163 from the JFK Assassination Records Collection.
107 pages of scanned typescript, no text layer at all — every page is one
TIFF image. The workflow: a cheap first pass, quality signals that catch
what it missed, and a page-scoped OCR rerun, all recorded as plain files.

All command output below is from an actual run (macOS, Tesseract 5.5.2,
poppler 26.05).

## Get the document

```bash
curl -O https://www.archives.gov/files/research/jfk/releases/2018/180-10147-10163.pdf
```

About 4 MB. NARA has re-released this record in several waves (2017–2025),
so your checksum may differ from the one shown in the manifests below.

## Check your tools

`pdf_ocr` shells out to `pdftoppm` (poppler) and `tesseract`. PageLedger
never installs them for you:

```bash
pageledger doctor
```

```
Command pdftoppm: /Users/you/.local/bin/pdftoppm (pdftoppm version 26.05.0)
Command tesseract: /Users/you/.local/bin/tesseract (tesseract 5.5.2)
```

If either is missing, doctor prints the install hint
(`brew install poppler tesseract` on macOS, `apt install poppler-utils
tesseract-ocr` on Debian/Ubuntu).

## Dry run

```bash
pageledger init-config --adapter pdf_ocr --out jfk-ocr.yml
pageledger run 180-10147-10163.pdf --config jfk-ocr.yml --out runs/jfk-dry --dry-run
```

```
Pages: 0 extracted / 107 total
Estimated cost USD: 0.0
```

107 pages planned, nothing extracted, nothing spent. `runs/jfk-dry/`
already contains the route map and planning artifacts.

## The cheap pass, and why the ledger matters

Suppose you didn't know this was a scan. The obvious first try is the free
`pdf_text` adapter:

```bash
pageledger init-config --adapter pdf_text --out jfk-text.yml
pageledger run 180-10147-10163.pdf --config jfk-text.yml --out runs/jfk-text
```

```
Pages: 107 extracted / 107 total
Quality warning pages: 107
```

Every page "succeeded" — and every page came back empty. Without quality
signals this would look like a completed run. Instead, all 107 pages are
flagged `empty_text`, land in `audit.json → review_queue`, and
`rerun-manifest.yml` lists them as an executable rerun plan.

## Rerun the flagged pages with OCR

```bash
pageledger rerun runs/jfk-text --config jfk-ocr.yml --out runs/jfk-ocr
```

```
PageLedger rerun run-20260707T033255988183Z wrote runs/jfk-ocr
Parent run: run-20260707T033247789368Z (generation 1)
Pages: 107 extracted / 107 total
Quality warning pages: 0
```

The rerun took 134 seconds, 1.25 s per page at the default 300 DPI. Page
ids are preserved and the parent run id is recorded, so provenance chains
across both runs.

## Compare

```bash
pageledger compare-runs runs/jfk-text runs/jfk-ocr
```

```
Pages compared: 107
Warning pages: A=107 B=0
Warnings resolved in B: 107
Warnings introduced in B: 0

| page_id             | chars A→B | resolved   | introduced |
|---------------------|-----------|------------|------------|
| doc_0001_page_0001  | 0→1236    | empty_text | -          |
| doc_0001_page_0002  | 0→1066    | empty_text | -          |
| ...                 |           |            |            |
```

## What the ledger recorded

`manifest.json` names the engine exactly:

```json
"extractors": [{
  "adapter": "pdf_ocr",
  "model": "tesseract 5.5.2",
  "version": "0.1",
  "capabilities": ["ocr", "local"]
}]
```

`cost.json` refuses to invent dollars for a free local engine:

```json
{"cost_usd": null, "cost_basis": "none", "cost_known": false,
 "usage": {"pages": 107, "compute_seconds": 133.5}}
```

Each line of `provenance.jsonl` carries the page's source checksum, engine,
measured `extraction_seconds`, and the raw artifact path. Months later,
`grep doc_0001_page_0042 runs/jfk-ocr/provenance.jsonl` answers what
produced that page, with what, and how long it took.

## Tuning

DPI and language go in `run.adapter_options`:

```yaml
run:
  adapter: pdf_ocr
  adapter_options:
    dpi: 400
    lang: eng
```

Options used are recorded in `manifest.extractors[].options`, so a 300 DPI
run and a 400 DPI run are distinguishable in the ledger, not just in your
memory.

## Honest limits

- Tesseract output on 1960s–70s typescript is usable but rough — stamps,
  handwriting, and degraded pages come back garbled. The ledger records the
  extraction; it does not make it correct. Route hard pages to a stronger
  engine (see `docs/ocr-options.md`).
- The `suspicious_embedded_text_delta` signal compares OCR output against an
  embedded text layer. This document has none, so that signal cannot fire
  here. On scans with a noisy embedded layer it flags pages where OCR and
  the layer disagree sharply.
- Zero quality warnings on the OCR run means none of the seven heuristics
  tripped, not that the text is accurate. This run's output contains
  word-level misrecognitions ("matericl" for "material") that no shape-based
  heuristic can catch — `quality.jsonl` records each page's lexical shape
  (`mean_token_length`, `short_token_ratio`) as sortable evidence, but
  sampling raw pages against the scan is still on you.
