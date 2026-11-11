# Review and correct

A job holds a page when something about its text looks wrong: text may be
missing, the OCR engine was unsure of many words, two engines disagree. Such a
page *needs a person*: someone who looks at the page and decides whether its
text is right. This page shows how, from the review view to the exported text.
The [glossary](glossary.md) defines the terms; the
[processing spec](processing-spec.md#reports-and-review) is the reference.

## Write the review view

```bash
pageledger review jobs/book
```

This writes the folder `jobs/book-review` beside the job (`--out DIR` names another), with `index.html` in it. Open it in a browser: it needs no
server and nothing from the network. It changes nothing in the job. Page
images need Poppler's `pdftoppm`; add `--images none` to write the view
without them, or `--images held` for only the pages that need a person.

## What the view shows

The page list comes first, the pages that need a person at the top, each with
why it needs a person in plain words. Under each page:

- why it needs a person, its selected text (named by the engine that read it,
  such as "Tesseract OCR (rus)" or "the PDF's own text"), how far the engines
  agree, and any warnings on its readings;
- "From the record", folded: the page's codes, which
  [warnings.md](warnings.md#in-plain-words) explains, and each reading's ID;
- the page image beside the selected text;
- the other readings, each compared word by word with the selected text: words
  only the selected text has are struck, the other reading's are underlined. A
  reading with no letters or digits, such as the empty text layer of a scan, is
  listed as empty.

## Decide each page

Choose a decision under the page, and add a note if you like: it is recorded as
the reason. "What each decision records", above the page list, says the same as
this table.

| In the view | Records | In a sheet |
|---|---|---|
| The selected text is right | that you checked the text against the page | `accept` |
| Use (another reading) instead | that another reading is right; it becomes the text | `use:READING` (the reading's ID) |
| My corrected text, below | your text as a new reading; the engines' readings stay as they are | `correct:FILE` |
| Blank page | that the page has no text | `blank` |
| Illustration, map or chart | that the page is a picture, not text | `illustration` |
| Handwriting | that the page is handwritten, left for a specialist | `handwriting` |
| Unreadable | that the page cannot be read in this copy | `unreadable` |
| Damaged or missing source page | that the source page is damaged or incomplete; another copy is needed | `source_defect` |

A page left without a decision stays as it is. Decisions on the pages that
nothing held are welcome too: they record that a person checked them.

## Correct the text

When the text is nearly right, correct it rather than accept a known error.
Edit the text in the box under the page; the view marks your changes. You can
start from the selected text or from another reading ("Start from"). What you
type is kept exactly, as a new reading beside the engines' readings, which are
never edited, with who changed it and when.

## Apply the decisions

Your choices stay in the browser until you download them. Choose "Download my
decisions (review.json)", save the file in the view's folder, and apply it,
first as a dry run:

```bash
pageledger review jobs/book --apply jobs/book-review/review.json --reviewer "Your Name" --dry-run
pageledger review jobs/book --apply jobs/book-review/review.json --reviewer "Your Name"
```

The dry run checks every decision and says how many words each correction
changes, without recording anything. The file is refused if the job has
changed since the view was written: write a new view.

## In a spreadsheet instead

The view's folder also holds `review.csv`, one row per page, and
`review-decisions.txt`, which lists the decisions and the readings each page can
use. Type a decision in each row's `decision` column (the codes in the table
above), save the sheet as CSV in the same folder, and apply it the same way:

```bash
pageledger review jobs/book --apply jobs/book-review/review.csv --reviewer "Your Name"
```

A cell cannot carry a corrected page: save the text in a file beside the sheet,
such as `p0003.txt`, and write `correct:p0003.txt`. Keep
`review.csv.binding.json` beside the sheet: it ties each row to the page's text
as it was. `pageledger review jobs/book --sheet FILE` writes a sheet on its own.

## Check and export

```bash
pageledger verify jobs/book
pageledger export jobs/book --format md --out book.md
```

`verify` checks the job, the decisions and the corrections against their
hashes. Every export marks each page with whether a person checked it; add
`--reviewed-only` to keep only the pages a person reviewed. See
[export](export.md) for the formats.
