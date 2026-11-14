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

Run the same command again later (with or without `--images none`) and it says
whether the view there still shows the job as it is: if nothing has changed, it
says the view is up to date and where it is; once decisions have been applied or
the job resumed, it says the view shows the job as it was, and asks for a new
folder with `--out DIR` (or remove the old one).

## What the view shows

The page list comes first, the pages that need a person at the top, each with
why it needs a person in plain words: which reading has what flaw, such as
"Tesseract OCR (rus) wrote a capital Ъ or Ь inside words, as OCR for modern
Russian does with pre-1918 letters" (at most two causes there). Under each page:

- why it needs a person, every cause; its selected text (named by the engine
  that read it, such as "Tesseract OCR (rus)" or "the PDF's own text"); how far
  the engines agree, where the job weighed a comparison; and any warnings on its
  readings;
- "From the record", folded: the page's codes, which
  [warnings.md](warnings.md#in-plain-words) explains, and each reading's ID;
- the page image beside the selected text;
- the other readings, each compared word by word with the selected text: words
  only the selected text has are struck, the other reading's are underlined. A
  reading with no text at all, such as the empty text layer of a scan, is
  listed as empty (one made only of symbols, such as `* * *`, is shown and can be
  chosen); the own text of a page image, which is always empty, is
  left out (the record keeps it).

### When the flawed reading is not the selected one

A scan with an earlier OCR layer (the Internet Archive's, say) is read again by
Tesseract, whose reading becomes the text. When that layer has a flaw of its own
(words run together, `joined_text`) and no other reading agrees with
Tesseract's, the page is held, and the view says: "The selected text is
Tesseract OCR (eng); the PDF's own text (an earlier OCR), the only other reading,
has words run together, so their comparison does not count and nothing confirms
the selected text". The hold is not a formality. In the 2026-11 re-walk, on two
of three such pages of an 1877 book, Tesseract's clean-looking reading had
misread dates and terms that the damaged layer had right. Compare the two
readings with the page image; for a whole book of such pages, add a second OCR
engine (`second_ocr`, which `pageledger init` writes as a commented block), so
that a second reading can confirm the first. The
[processing spec](processing-spec.md#hold-policy-09) says why the rule stays,
under "What holding for the layer's warnings costs".

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

Your choices stay in the browser until you download them. The view keeps them
in the browser's local storage, so closing the page loses nothing. Other HTML
files opened from your disk in the same browser may be able to read that
storage (Chrome treats every local file as one site), so on a shared computer
apply your decisions when you finish, and do not open HTML files you were sent
while a review is under way. Choose "Download my
decisions (review.json)". Your browser saves the file where it saves downloads,
usually your Downloads folder, and may name it `review (1).json` if a
`review.json` is there already. Apply it by that path, first as a dry run:

```bash
pageledger review jobs/book --apply ~/Downloads/review.json --reviewer "Your Name" --dry-run
pageledger review jobs/book --apply ~/Downloads/review.json --reviewer "Your Name"
```

The view shows this command with `DOWNLOADED_FILE` where the path goes.

The dry run checks every decision and says how many words each correction
changes ("Correction of page 60, from Tesseract OCR (eng): 1 word changed, 0
added, 0 removed"), without recording anything. The file is refused if the job has
changed since the view was written: write a new view.

## In a spreadsheet instead

The view's folder also holds `review.csv`, one row per page, and
`review-decisions.txt`, which lists the decisions and the readings each page can
use. The sheet's `disposition`, `attempt_summary` and `review_reasons` cells are
for you to read: each leads with words ("Text may be missing or damaged in the
reading (coverage_defect)", "Tesseract OCR (eng): the first words … (2407
characters; local_ocr-0060:doc_0001_page_0060)"); applying the sheet reads only
`row_id`, `page_number`, `decision` and `note`. Type a decision in each row's `decision` column (the codes in the table
above), save the sheet in the same folder as "CSV UTF-8 (comma delimited)",
and apply it the same way. Another kind of CSV can use semicolons or tabs, or
lose letters outside English. PageLedger refuses a sheet with semicolons or tabs,
or one that is not UTF-8, and says how to save it; but letters a save has already
turned into `?` cannot be noticed, so check a note in Cyrillic after saving.

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
pageledger export jobs/book --out book.md
```

`verify` checks the job, the decisions and the corrections against their
hashes. Every export marks each page with whether a person checked it; add
`--reviewed-only` to keep only the pages a person reviewed. See
[export](export.md) for the formats.
