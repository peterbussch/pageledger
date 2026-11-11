# PageLedger: text from scanned documents, with a record of how each page was read

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/peterbussch/pageledger/main/assets/pageledger-lockup-horizontal-reversed.png">
    <img src="https://raw.githubusercontent.com/peterbussch/pageledger/main/assets/pageledger-lockup-horizontal.png" alt="PageLedger: the tallied page" width="440">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/pageledger/"><img src="https://img.shields.io/pypi/v/pageledger" alt="PyPI"></a>
  <a href="https://github.com/peterbussch/pageledger/actions/workflows/ci.yml"><img src="https://github.com/peterbussch/pageledger/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/pageledger/"><img src="https://img.shields.io/pypi/pyversions/pageledger" alt="Python versions"></a>
  <a href="https://github.com/peterbussch/pageledger/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/pageledger" alt="License"></a>
  <a href="https://doi.org/10.5281/zenodo.21340651"><img src="https://zenodo.org/badge/DOI/10.5281/zenodo.21340651.svg" alt="DOI"></a>
</p>

PageLedger turns a scanned or born-digital document into text, one page at a
time. For every page it keeps a record of how the text was read, and it lists
the pages a person should look at before the text is trusted. Everything it
writes is plain files in one folder: no service, no database.

It grew out of a Soviet census digitization project, and is written for
historians, archivists and anyone else who has to say where a transcription
came from.

## Read a document

```bash
pageledger process book.pdf --out jobs/book
```

`process` reads each page's own text first (a born-digital PDF carries it),
and sends the pages whose text is missing or looks wrong to an OCR engine on
your machine: Tesseract, in English, unless you choose otherwise. It never
calls a paid service unless you configure one. The folder `jobs/book` is
the *job*: the record of everything done to this one document.

On a three-page scan, the screen shows:

```text
pageledger: no --config given, so this job uses the default document config: the text layer, then pdf_ocr (lang eng) for pages that need OCR, and no paid stages.
To change it, write it out with `pageledger init --engine pdf_ocr --lang eng --out processing.yml` and pass --config processing.yml.
Status: finished. Every stage this job is configured to run has run.

Source: book.pdf; 3 of its 3 pages selected, 3 read.

- Text selected and nothing held the page; no person has checked it (unreviewed_text): 3 pages (1-3).

Next: pageledger export jobs/book --format md --out book.md writes the selected text out.

The full report: pageledger inspect jobs/book
```

When something looks wrong on a page (text missing, an engine unsure of many
words, two engines disagreeing), the job *holds* the page, and the screen says
how many pages need a person and why. Then:

- `pageledger inspect jobs/book` shows the job's report, `report.md`: each
  page that needs a person, with the evidence for it.
- `pageledger review jobs/book --out book-review` writes a review view to open
  in a browser: each page's image beside its text. Choose a decision for each
  page or correct its text there, and record the decisions with
  `pageledger review jobs/book --apply book-review/review.json --reviewer "Your Name"`.
- `pageledger export jobs/book --format md --out book.md` writes the text out
  as plain text, Markdown, JSON lines or TEI, each page marked with whether a
  person has checked it.

The [first document job](https://github.com/peterbussch/pageledger/blob/main/docs/document-first-run.md)
tutorial goes through these steps on a small example that needs no OCR engine.

## What a source can be

A PDF, a UTF-8 text file (pages separated by form feeds), one JPEG or PNG
page image, or a folder of them read as one document, pages in the order of
their file names. `pageledger fetch` downloads an item's page images from a
library's IIIF server into such a folder, with a record of where each image
came from. See [page images](https://github.com/peterbussch/pageledger/blob/main/docs/process-a-collection.md#page-images)
and [fetching from IIIF](https://github.com/peterbussch/pageledger/blob/main/docs/iiif.md).

## Choosing an OCR engine

Tesseract reads many languages: name them with `--lang` in Tesseract's codes,
such as `rus` or `deu+eng` (`pageledger doctor` lists the ones installed).
`--engine rapidocr` and `--engine indra` choose two other engines that run on
your machine. A job can also use a second engine, a vision model, or the OCR
a library published with its scans (ALTO), to check the first reading. The
[OCR options](https://github.com/peterbussch/pageledger/blob/main/docs/ocr-options.md)
compare the engines,
[non-English and historical OCR](https://github.com/peterbussch/pageledger/blob/main/docs/multilingual-ocr.md)
covers language packs and pre-reform spelling, and the
[engine recipes](https://github.com/peterbussch/pageledger/blob/main/docs/engine-recipes.md)
give a working setup for each.

## Why a ledger

OCR makes mistakes that look like text, and a transcription that cannot be
traced is hard to cite or correct. PageLedger records, for every page, which
file it came from (by its SHA-256 hash), which engine read it with which
settings, what each engine produced, why the page was held, and who reviewed
or corrected it. `pageledger verify jobs/book` checks that the record is
intact, here or after the job folder has been deposited elsewhere, so a reader
of your edition can check any page against its source. It does not certify
that the text is right: that is what review is for.

## Runs: for building and testing pipelines

A job manages one document. The run commands (`run`, `rerun`, `compare`,
`classify`, `bundle`, `replay`) apply one extraction tool to the pages of
some files and record the same evidence; they are for building and testing a
pipeline over many files. This example needs no OCR engine:

```bash pageledger-tutorial
printf 'first page\fsecond page with a replacement character �\n' > sample.txt
pageledger init --run --out pageledger.yml
pageledger run sample.txt --config pageledger.yml --out runs/first
pageledger inspect runs/first
pageledger verify runs/first
```

The [first run](https://github.com/peterbussch/pageledger/blob/main/docs/first-run.md)
tutorial continues from here, and the
[CLI reference](https://github.com/peterbussch/pageledger/blob/main/docs/cli.md)
lists every command.

## Install

PageLedger needs Python 3.10 or later. For PDFs, install the `pdf` extra:

```bash
pip install "pageledger[pdf]"
pageledger doctor
```

OCR also needs Poppler and Tesseract, installed separately: on macOS
`brew install poppler tesseract`, on Debian or Ubuntu
`sudo apt-get install poppler-utils tesseract-ocr`. `pageledger doctor` says
which engines, tools and languages it finds. Jobs use POSIX file locks: use
macOS, Linux or, on Windows, WSL.

The [documentation](https://github.com/peterbussch/pageledger/blob/main/docs/README.md)
is arranged by task.

## Citing

Cite the software with
[CITATION.cff](https://github.com/peterbussch/pageledger/blob/main/CITATION.cff);
[share and cite results](https://github.com/peterbussch/pageledger/blob/main/docs/share-and-cite.md)
explains how to cite and deposit a job. Development setup is in
[CONTRIBUTING.md](https://github.com/peterbussch/pageledger/blob/main/CONTRIBUTING.md);
to report how PageLedger did on a collection, [open a corpus
report](https://github.com/peterbussch/pageledger/issues/new?template=corpus-report.yml).

MIT license.
