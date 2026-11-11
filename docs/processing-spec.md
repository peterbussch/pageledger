# Document processing jobs

`pageledger process` manages one document's extraction attempts, selected text,
review state, and report. It first reads local text, then sends pages with defect
evidence to configured OCR and image stages. Every attempt remains available
for inspection.

## Start with local text and OCR

Document jobs require POSIX advisory locks, available on macOS and Linux.
Install `"pageledger[pdf]"`, Poppler, and Tesseract for a PDF job. The
simplest job needs no config file:

```bash
pageledger process book.pdf --out jobs/book
```

It runs with the configuration `pageledger init` writes:
local text, then Tesseract OCR in English, and no image stage. `--lang`,
`--engine` and `--model-dir` choose the OCR engine and its language the same
way for both commands (see [the CLI reference](cli.md#process)); a `.txt`
source gets no OCR stage. The job records that configuration exactly as it
would record the file, and nothing is read from the working directory.

To change it, write the configuration out with
`pageledger init --out processing.yml` and pass
`--config processing.yml`. The file, the repository's
[processing example](examples/processing.yml), reads without its comments:

```yaml
schema_version: "0.1"
processing:
  local_ocr:
    adapter: pdf_ocr
    adapter_options:
      lang: eng
      dpi: 300
  limits:
    max_image_pages: 0
```

`local_text` is left out so that the source decides it (`pdf_text` for a
PDF, `text` otherwise). Its comments list the optional stages described
below, each with a link to its section.

Sample the first ten source pages of a PDF:

```bash
pageledger process book.pdf --config processing.yml --pages "1-10" --out jobs/book
pageledger inspect jobs/book
pageledger verify jobs/book
```

Use an existing PDF with at least ten pages, adjust `--pages`, or omit the flag
for the full document. The output directory must be new. Open
`jobs/book/transcript.md` for selected text and `jobs/book/report.md` for the
source links, attempts, and unresolved review work. A completed job means its
configured processing finished; human review may still be needed.

For text files, set `processing.local_ocr` to `null`, as
`init --engine none` does. Form-feed characters separate
pages.

The source can also be an image file or a folder of page images, read as one
document (see [a folder of page scans](pdf-ocr-first-run.md#9-a-folder-of-page-scans)).
The same configuration works: `pdf_text` and `pdf_ocr` are the defaults, as for
a PDF. Page images have no text layer, so the local text stage records an empty
reading with the warning `no_text_layer` for each page, without needing the
`pdf` extra; the empty reading holds the page as a blank candidate, the page
climbs to OCR, and a clean OCR reading settles the hold. The empty reading stays
in the ledger as the record that there was nothing to read.

So a source is one of three kinds, and `process` decides which before the job
exists: a PDF (by its `.pdf` suffix, so a damaged PDF still becomes a job that
records why it cannot be read), page images (an image file, or a folder holding
them), or text (any other file that reads as UTF-8, such as `.txt` or `.md`).
Anything else, such as a `.docx` file or a folder without page images, is
refused with a message that says what a source can be. Without `--config`,
`process` chooses OCR (`pdf_ocr`) for PDFs and page images and no OCR for text.
A [IIIF snapshot](iiif.md#process-the-snapshot) is a folder of page images.

A document job reads only the `processing` section. Budget, pricing, grading
and rerun settings under `run` would be ignored, so `process` rejects them and
names the replacement (for budgets, `processing.limits`). A leftover
`run.adapter` or `taxonomy` section is harmless and produces a warning.
Selections such as `--pages "2-5,19"` preserve source page numbers and record both
the selected count and full document count.

## Resume an interrupted job

Keep the source, job directory, and adapter environment in place:

```bash
pageledger resume jobs/book
pageledger inspect jobs/book
pageledger verify jobs/book
```

Until it is resumed, an interrupted job's `job.json` still says `processing`.
`verify` on such a job fails with `"reason": "unfinished"` in its JSON result and
says to run `resume` (`Status: fail (unfinished)` on screen), rather than
comparing work in progress with a finished job's evidence. Its `report.md` says
the job is not finished and lists the pages still waiting for a stage.

If the source has moved, add its new path to a
[source index](checkpoint-spec.md#moved-source-files) rather than editing
`job.json`; resume and `verify` then find it by SHA-256.

A job resumes only in the directory it was written in, the `root` that
`job.json` records. Its stage runs are bound to that directory, and an image
stage writes its evidence there, so `resume` on a moved or copied job stops
and names the directory to move it back to; nothing is extracted. This also
keeps two copies of one job from both paying for the same pages.
`verify`, `inspect`, `export`, `review` and
`adjudicate` work wherever the job is (see [moved jobs](#moved-and-copied-jobs)).

Job recovery checks retained attempts before scheduling pending work. Saved
responses are reused. An interrupted built-in local stage is retried once in
a separate retained child run, only for its unfinished pages. Paid or custom
adapter requests with no saved outcome are never retried automatically. Each
attempt is a resumable run; the [checkpoint specification](checkpoint-spec.md) describes
its recovery records.

`--adapter-path DIR` on `process` or `resume` loads custom adapters. Use
`process --review FILE` to supply source-bound human decisions before extraction,
or `review --apply` to record them afterward. All job commands accept `--json`.

## Policy and configuration

`processing` is separate from `run.adapter_order`. The latter still chooses an
adapter by explicit rerun generation; `process` rejects it. A document job has
ordered stages `local_text`, `local_ocr`, `image`, and optional `second_opinion`,
and two optional stages between local OCR and the image stage, `second_ocr` and
`local_image` (see [the ladder](#the-ladder-a-second-ocr-engine-and-a-local-model)).
Each stage is an adapter profile with `adapter`, optional `adapter_options`,
and optional `prompt`. Set an optional stage to `null` to disable it;
`local_text` is required. Local text defaults to `pdf_text` for PDFs or `text`
otherwise. PDF OCR defaults to `pdf_ocr`.
Image stages are disabled until configured with a positive
`processing.limits.max_image_pages`. An image stage requires an enabled local
OCR stage; a second opinion requires an image stage. Invalid combinations fail
before creating a job.

The local text pass caches document extraction and retains one response per
selected source page. Pages with structural or explicit defect evidence can
proceed to OCR, then an image adapter. OCR and image attempts run serially per
page. A clean later attempt may supply the transcript, while the original
review hold remains. Every attempt is retained. Missing stages leave pages for
review; they never imply success at the missing stage.

A plausible clipped text layer may escape structural checks. Record known
coverage defects through adapter warnings (`coverage_defect`, `clipped_text`,
`missing_content`) or source-bound review. Named numeric columns and aligned
records are compared where possible; arithmetic checks alone do not prove that
numbers belong to the right columns. A disagreement remains a review hold.
There is no reliable automatic test for complete scholarly apparatus, faithful
transcription, or the absence of invisible/missing source strokes.

Grades and model confidence do not choose the winning attempt or certify a
page. Blank candidates, reviewed blanks, illustrations, handwriting,
unreadable text and source defects have separate dispositions. Human receipts
can clear a hold only when they bind the source/page and, for reviewed text,
the selected attempt and output hash. A known source defect directs the user to
an alternate source and prevents further automatic extraction.

## The ladder: a second OCR engine and a local model

Between local OCR and a paid image stage, a job can try two cheaper rungs:

```yaml
schema_version: "0.1"
processing:
  local_text:
    adapter: pdf_text
  local_ocr:
    adapter: indra
    adapter_options:
      mode: ocr
  second_ocr:            # a second literal engine, read when the first is held or unconfirmed
    adapter: pdf_ocr
  local_image:           # a vision model on this machine
    adapter: vision
    adapter_options:
      base_url: http://127.0.0.1:8080/v1
      model: YOUR_LOCAL_MODEL
      timeout_seconds: 900
  image:                 # the paid reader, called only when the local one is not enough
    adapter: vision
    adapter_options:
      base_url: https://gateway.example.org/v1
      model: YOUR_HOSTED_MODEL
      env_key: GATEWAY_API_KEY
      allow_remote: true
  escalate_on: [hold, disagreement]
  limits:
    max_image_pages: 20
    max_local_image_pages: 100
```

A page climbs one rung at a time, on the same triggers as before. `second_ocr`
is another literal engine: its reading is compared with the first OCR's like
any other, and either can be a contested page's literal witness. The example
reads first with the built-in [`indra`](engine-recipes.md#indra-ocr-macos-and-linux-cpu)
adapter, which was the stronger and much faster engine in the measurement
there, and asks Tesseract when Indra's reading is held or when nothing else
confirms it (the `uncorroborated` trigger below): a page whose text layer
agrees with Indra stops there, a scan without a layer is read twice. `local_image` reads the page
image with a model on this machine through the `vision` adapter; it keeps the
exact image it sent, like the image stage, and has its own page budget,
`max_local_image_pages`, which `max_image_pages` does not count.

The local model is a reader, so its text is held as `unconfirmed_model_output`
until another engine agrees with it, as any model's is. From `local_image` a
page climbs to `image` only on evidence against the local reading: a warning
hold on it, or, with the `disagreement` trigger, word agreement with a clean
literal reading below 60% or a number read differently. `always` does not lift
a clean local reading to the paid one. In a contested job the local reader's
disagreement still counts, though the spans replace the page's disagreement
reasons. When both have read a page, the paid reader is the contested base.

A local model that fails (its server is down, it times out) costs nothing and
leaves no reading: its attempt is kept as failed and the page goes on to the
next stage. With no stage left, the page waits for review with
`provider_failure` among its reasons.

The two stages appear in a job's policy only when configured, so a job made
without them keeps the policy it recorded and verifies as before. Each needs
an explicit `local_ocr` stage.

## Compare engines on a page

When a page has readings from more than one engine, the job compares them word
by word, after NFC normalization and with whitespace collapsed. Each comparison
records the two attempts, their word agreement from 0 to 1, up to 20 passages
where they differ and up to 20 numbers that differ. Comparisons are stored with
the page in `job.json` and the report.

Only readings that are clean themselves count. A page moves on to OCR because
its text layer was held, so that layer disagreeing with the OCR is expected and
says nothing. Comparing the selected reading with another clean one:

- word agreement below 0.60 adds the review reason `engine_disagreement`;
- a number that differs adds `numeric_disagreement`;
- a reading from a generative adapter, such as [`vision`](vision-adapter.md),
  stays in review as `unconfirmed_model_output` unless another clean reading
  agrees at 0.60 or above.

These reasons hold a page for review; they never start another stage. Engine
agreement is evidence, not truth: two engines can make the same mistake.

The 0.60 threshold was set on 24 transcribed calibration pages, where it holds
9 pages when RapidOCR is compared with Apple Vision, 14 for Surya with
RapidOCR and 22 for Tesseract with RapidOCR: the weaker the engines, the more
pages it holds. How many real number errors it catches has not been measured.
Jobs created before 0.6 are not compared and keep their review reasons.

To compare engines on pages that need no second reading, run one on a sample:

```yaml
processing:
  benchmark:
    stage: local_ocr
    every_nth_page: 10
```

This runs `local_ocr` on source pages 10, 20, 30 and so on, even when their
text layer is clean. The stage must be enabled, and its pages count against the
job's limits.

## Climb on roughness and disagreement

By default a page climbs to the next stage only when its reading carries a
warning hold, and two engines that disagree send the page straight to review.
Two problems escape that rule. OCR can be wrong without any warning: an engine
that reads two columns as one glues the halves of neighbouring lines into
non-words. And a disagreement between two clean readings is a question a
stronger engine can help answer. `escalate_on` adds two triggers:

```yaml
schema_version: "0.1"
processing:
  local_text:
    adapter: pdf_text
  local_ocr:
    adapter: pdf_ocr
    adapter_options:
      lang: rus
  escalate_on: [hold, rough, disagreement]
  lexicon:
    provider: pymorphy3
    language: ru
    rough_below: 0.95
  limits:
    max_attempt_pages: 100
    max_image_pages: 0
```

- `rough`: the selected reading's share of words the lexicon knows falls below
  `rough_below`. Words split across a line break are rejoined first, and a page
  with fewer than 20 Cyrillic words is not judged. Install the Russian lexicon
  with `pip install 'pageledger[ru]'`, or name a word list for any language
  (below).
- `disagreement`: the selected reading and another clean one agree on fewer than
  60% of their words, or read a number differently. Without this trigger the page
  goes to review, as before.
- `ocr_layer` (no configuration; jobs written by 0.8): a page whose text
  layer is an earlier OCR of the scan (warning `ocr_text_layer`, written by
  `pdf_text` for an OCR font and by `alto` for a library's ALTO file) climbs from
  local text to local OCR, whatever its text looks like. An inherited OCR layer
  is a reading of unknown quality, and 0.7 accepted it unchecked. In jobs
  written by 0.8, once local OCR has read the page, the layer is kept but set
  aside: it is not selected, its own holds are not the page's, and its
  disagreement with later readings does not count. On 9 of the 10 Internet
  Archive pages checked in the 0.8 dogfood the layer read worse than a fresh
  OCR (on the tenth, slightly better), and its misreadings made each better
  reading look like a disagreement. Jobs written by 0.9 keep the layer as a
  witness instead; see [hold policy 0.9](#hold-policy-09).
- `uncorroborated` (no configuration; jobs written by 0.8 whose ladder has a
  `second_ocr` stage): a page whose selected reading is one local OCR engine's,
  with no clean reading to compare it with and none agreeing with it on at least
  60% of words, climbs to the second OCR engine; while each later engine's own
  reading is held (so it can neither confirm nor challenge), the page climbs on
  to the next reader, up to the image stage. An inherited OCR layer that agrees counts as confirmation; its
  disagreement still does not count against the reading, and a typeset text
  layer with no warning is not read again. On the 20 truth pages of the 0.8
  dogfood, five of the six pages that were wrong but not held were one engine's
  unconfirmed reading (Indra, after the Internet Archive layer was set aside).
  When two clean readings disagree, that is `disagreement`'s evidence: the page
  climbs if that trigger is on, and is otherwise held as `engine_disagreement`.
- `always`: every page climbs to the image stage. Use it with
  [`contest`](#contest-a-models-reading) when the goal is an edition: the model
  reads every page and each place it differs from the OCR is checked.

`hold` is 0.6's rule and must be listed. Triggers lift a page at most to the
`image` stage: a second model is another witness with the same habits, not a
better reader, so `second_opinion` still climbs on holds only. When the rough
trigger is on, a later reading that is not rough replaces a rough one as the
selected text. If every clean reading is rough, the least rough is selected.

Each trigger that fired is recorded on the page with its stage, the reading it
judged and the evidence, for example
`{"trigger": "rough", "stage": "local_ocr", "known_share": 0.89, ...}`. The job
records the lexicon's provider and package versions, or a word list's hash.
`verify` recomputes the triggers and refuses a job whose lexicon is not the
installed one. The report
adds the work done at each stage and how many pages each trigger raised.

These numbers came from one test with real documents. Clean born-digital
Russian scored 0.98 to 0.995. OCR of a two-column article read as one column
scored 0.87 to 0.91. Treat 0.95 as a starting point and measure your own
collection. `rough` catches a reading that has gone wrong, not scattered
misreadings: read in column order, the same article scored 0.95 to 0.97 while
its OCR still misread about one word in a hundred, and six of its seven pages
stopped at OCR. Names, abbreviations and a linguist's examples are unknown
words too; a hand-checked 1962 monograph scored a median 0.97.

### A word list as the lexicon

For a language without a built-in lexicon, name a plain word list: a UTF-8 text
file with one word per line.

```yaml
  lexicon:
    provider: wordlist
    path: /usr/share/dict/words
    rough_below: 0.9
```

A relative `path` is read from the directory `process` runs in. A word on the
page counts as known when it matches a line of the list after both are put in
Unicode normal form NFC and case-folded (`str.casefold`), so «Paris», «paris»
and «PARIS» are one word and «café» written with a combining accent matches
«café», but «cafe» does not match «café». Blank lines and surrounding spaces are
ignored; a list must hold at least one word. A word is a run of three or more
letters in any script; digits and punctuation split words, and a page with fewer
than 20 such words is not judged. A word hyphenated across a line break is
rejoined when the next line goes on in lower case.

PageLedger ships no word list. On macOS and many Unix systems
`/usr/share/dict/words` is an English list (on macOS, Webster's Second
International, 235,976 lines, many of them rare words); on Debian and Ubuntu the
`wamerican` or `wbritish` package installs one. A list built from a project's own authority file (the names, places and
terms its sources use) can sit beside a general one: concatenate them. The job
records the list's resolved path, SHA-256, size in bytes, number of distinct
words and the matching rule (`nfc-casefold`) under `lexicon` in `job.json`.
`resume` and `verify` read the list again from that path and refuse a job
whose list is missing or has changed; restore the recorded list to continue.

A word list says nothing about names and abbreviations it does not hold. On the
1934 Washington telephone directory tiles of the 2026-10 dogfood (53 judged
tiles), the macOS list knew a median 58% of the words in the model's readings
and 56% in RapidOCR's: exchange names (`NA tional`), abbreviations (`Natl`, `bg`)
and surnames are unknown words. There `rough` cannot tell a good reading from a
bad one, and the directory needs `always` instead. Measure your own pages before
choosing `rough_below`.

## Hold policy 0.9

A job records the rules it was written under as `hold_policy`, and verification
rebuilds it by those rules (see the [document report spec](document-report-spec.md)).
Jobs written by 0.9 record `"0.9"`. Its rules came from a dogfood on 74 pages of 20
research PDFs (historical Russian print, directories, gazettes, scholarship) in
which no held page was right, but 28 of the 62 pages with any OCR history left the
job wrong and not held. Each rule below names the page that motivated it.

**An OCR layer in any font is recognised.** `pdf_text` marks a layer
`ocr_text_layer` not only when it is drawn in `GlyphLessFont` but also when most of
its text is drawn invisibly on a page an image covers, when most of it is painted
before an opaque page-covering image that hides it, or when the PDF's Producer or
Creator names OCR software on a page an image covers. The signals that fired are
recorded with the reading (see the [warnings reference](warnings.md)). The page
then climbs to local OCR, as the `ocr_layer` trigger describes. This is not a hold
policy rule: any job, plain runs included, gets the warning from 0.9 on. Layers
from ABBYY FineReader 11 and ABBYY Recognition Server (an 1859 gazetteer, a 1788
geographic dictionary), LuraDocument (an 1847 address calendar, an 1880
gazetteer) and Ghostscript's invisible text (a 1939 directory) were read as
typeset text: a page went to OCR only when the layer carried a warning of its own.
A born-digital page with a background picture under visible text raises none of
the signals unless its Producer names OCR software; nor does an OCR-converted
book re-typeset in ordinary fonts with no scan behind it.

**The layer and the job's own OCR are two witnesses.** Under 0.8 the layer was set
aside once local OCR had read the page. Under 0.9:

- The job's own reading is still preferred when both are clean, but the layer is
  compared with it: a clean layer that agrees on under 60% of words holds the
  page (`engine_disagreement`), unless another of our engines' clean readings
  agrees with the selected one: two engines that agree outvote the layer. Its
  objection holds the page for a person but is not a `disagreement` trigger, so
  it never buys a paid reading. The layer's numbers are not compared: rebuilt
  under 0.9 with them, all 14 pages of a 1914 book in the 0.8 dogfood, where the
  Internet Archive layer agreed with the OCR, were held because the layer read
  a number differently somewhere on each, including the four with reference
  transcriptions, which the OCR had read with under 4% error.
- When our readings are held and the layer is not, the layer is selected.
- The layer's own warnings hold the page until another clean reading agrees with
  the selected one. A layer that failed on the page says the page is hard to read,
  and one more reading that nothing confirms does not settle it. The 1939
  directory's layer split each word ("Сырны й комбина т"); the one OCR reading of
  the same page misread names too. With a `second_ocr` stage the page goes there
  (`uncorroborated`), and an agreeing second reading settles it.
- `suspicious_embedded_text_delta` measures a reading against the PDF's text
  layer. When that layer is an earlier OCR, the warning holds neither reading;
  their comparison does. With one OCR engine and a junk layer (teledir finding 10,
  where the layer won because the two good readings carried the warning), the
  OCR is selected and the page held for their disagreement.

**A literal reading that lost the pre-reform letters another kept is held.**
When one literal reading of a page (a text layer or an OCR engine, not a model)
spells ten or more words with ѣ, any literal reading with under a quarter as many
is held (`coverage_defect`). Stock Tesseract `rus` cannot write ѣ. On the 1913
Kiev directory the Internet Archive layer had 26, 16 and 14 such words on three
pages and the `rus` reading none; 0.8 selected the `rus` reading and held
nothing, though the job had recorded 69–72% agreement. Only ѣ counts: garbled
layers in the dogfood wrote ѳ for е and stray ѵ, but put ѣ inside a well-formed
word at most twice a page. The rule compares readings of the same page, so it
needs a reading that kept the letters: an image-only scan read once by `rus` is
not judged by it (`capital_sign_in_word` often is).

**When every reading is held, the best supported one is shown.** Readings are
ranked by evidence rather than by order: first those that did not lose the
pre-reform letters, then those held only for the engine's low confidence, then
fewer stray symbols (rounded to a hundredth); ties keep our own readings first.
On an 1880 gazette with a second OCR stage running the community `orus` model,
`rus` kept no pre-reform letters and `orus` 95, both at low confidence; 0.8 showed
`rus` because it came first. On a typewritten archival inventory the garbled
LuraDocument layer was shown over an OCR reading held only for low confidence.

**An attempt that never finished is not a reading.** An attempt whose outcome is
unknown (the process was killed mid-page) raises no hold. After `resume`, 0.8 held
such a page as a possible blank because the dead attempt had no text.

**No climb is recorded toward a stage the job does not run.** A page read by a
second OCR engine and still unconfirmed was recorded as climbing toward an image
stage the job did not have, so the report counted it twice.

### Measured on the 74 dogfood pages

This is a measurement on these 74 pages only, not an accuracy figure. The 12 right
pages are all born-digital text layers, so they test the layer detector and the
reading warnings, not the OCR rules: no page with OCR history in the sample was
right. Each page was classed against its scan by the selected reading and whether
it was held.

| Class | 0.8 | 0.9 |
|---|---|---|
| Right, not held | 12 | 12 |
| Wrong, held | 34 | 54 |
| Right, held (a false alarm) | 0 | 0 |
| Wrong, not held | 28 | 8 |

Where 0.9 extracts the same readings, the 0.8 jobs were rebuilt under 0.9; the
eight documents whose layer is now recognised were run again with the same pages
and `--lang rus` (Tesseract 5.5.2; the five OCR readings it repeated were
byte-identical to the dogfood's). The 20 pages now held:

| Pages | Held by |
|---|---|
| 1913 Kiev directory, 3 | The `rus` reading lost the layer's ѣ; the layer is selected. |
| 1788 geographic dictionary (ABBYY Recognition Server), 4 | Layer recognised (invisible text; producer); both it and the OCR write a capital Ъ inside words (`capital_sign_in_word`). |
| 1859 gazetteer (ABBYY FineReader 11), 4 | Layer recognised (text under the page image; producer); the OCR lost the layer's ѣ, and on 3 pages the layer splits its words. |
| 1888 gazette (ABBYY FineReader 11), 2 | As the 1859 gazetteer. |
| 1847 address calendar (LuraDocument), 4 | Layer recognised (invisible text; producer); the OCR reading has low confidence. |
| 1939 directory (Ghostscript), 2 | Layer recognised (invisible text); its words are split and the one OCR reading is unconfirmed (on one page also low confidence). |
| 1880 gazetteer table (LuraDocument), 1 | Layer recognised; it kept the table's 34 row labels and almost none of its values (`labels_without_values`), and the one OCR reading is unconfirmed. |

The 34 pages held under 0.8 stay held. The eight still wrong and not held are one
OCR engine's unconfirmed reading of an image-only scan (a 1917 directory whose
italic surnames are garbled while 24.8% of its words fall under confidence 60, and
four pages of an English article photographed as two-page spreads, read at
confidence 0.85 to 0.94 with line ends cut and, on one page, the two pages'
lines interleaved), and three pages of a book re-typeset from OCR in ordinary
fonts, with a few slips per page. A `second_ocr` stage sends a page of the first
kind to another engine; nothing in 0.9 recognises the second kind.

The rules were also checked against the 0.8 dogfood's jobs (10 documents, 135
pages, 40 of them with reference transcriptions, read with Indra, Tesseract,
dots.ocr and a paid model), rebuilt under 0.9 from their retained readings. That
evidence has right OCR pages, which this sample lacks: the 5 right pages that
passed under 0.8 still pass, no page held under 0.8 is released, and 105 pages
are held under either policy. Those jobs were not read again, so the new layer
detector is not part of that check.

The reading warnings and the detector were also run on three random pages each of
80 PDFs from the same research library: after two fixes (a stressed textbook word
"е́хать" split by its accent, and dialogue ending lines in "..." read as table
leaders), the reading warnings fired on none. The detector flagged pages in 15 of
the files; the four of those looked at against their images were all scans.

## Contest a model's reading

A vision model writes fluent text and silently corrects what it sees: it fixes
the printer's misprints and swaps in a likelier word. An OCR engine misreads
glyphs but does not invent words. With `contest`, a page whose selected reading
came from a model (an adapter with the `generative` capability) is compared word
by word with the best literal reading of the same page, and each place they
differ is recorded:

```yaml
schema_version: "0.1"
processing:
  local_text:
    adapter: pdf_text
  local_ocr:
    adapter: rapidocr
  image:
    adapter: vision
    adapter_options:
      base_url: http://127.0.0.1:20128/v1
      model: codex/gpt-6.1-sol
      env_key: OMNIROUTE_API_KEY
    prompt: |
      Transcribe this printed page exactly as printed, for a scholarly digital edition.
      Keep the original spelling, punctuation and line breaks; do not correct or modernise.
      Write formulas in LaTeX between $...$ or $$...$$. Write each figure as one line
      "[Figure: <its printed caption or labels>]". Mark a word you cannot read with
      confidence with [?] after it, and an illegible passage as [illegible].
  escalate_on: [hold, always]   # the model reads every page
  contest:
    rules: ru-print-0.1
  lexicon:                      # optional; lets the glyph rule judge words
    provider: pymorphy3
    language: ru
  limits:
    max_image_pages: 200
```

Regions rely on the model marking them, as this prompt asks; with the default
prompt a formula is compared word by word.

The model's text is the base, and every span is anchored in its offsets. Words
one engine placed elsewhere on the page count as reading order, not as
differences, when at least two words moved together or the word stands alone on
its line; a single word in another place is a difference, because a model may
move a negation. A word hyphenated across a line that the other engine read as
its two halves, side by side or with the hyphen kept, is one word. Any other split or joined word stays a
difference: a model that writes «гос средств» for the printed «госсредств» has
changed the text. Unpaired words are grouped, so a line one engine dropped is one
span. A formula (`$...$`), a `[Figure: ...]` line, `[illegible]` and a word the
model marked `[?]` are each one `region` span, because the model wrote them in a
form no literal engine can confirm word by word.

| Kind | Literal reading against the model's | Settled by |
|---|---|---|
| `homoglyph` | Look-alike letters of another script (HAYK for НАУК), two letters or more | Rule H: the model's |
| `glyph` | A non-word whose difference from the model's known word is only confusions listed for that engine (RapidOCR's н for п, or e for c in English print) | Rule G: the model's |
| `misprint_guard` | A non-word the engine's listed confusions do not explain (назависимости) | Open: it may be what the page prints |
| `word` | Any other difference, or words only one side has | Open |
| `number` | Either side has a digit (from `ru-print-0.2`, grouped spans too) | Open |
| `region` | A formula, figure line or the model's own doubt | Open |

The rule set is versioned data. `ru-print-0.1` lists the confusions RapidOCR's
Cyrillic model made at least twice in one dogfood, checked against the page
images; its pairs never cross case, and they apply only to the `rapidocr`
adapter. Without a lexicon only rule H applies. On two fully checked Russian
documents (61 pages) the rules settled 178 spans with no error against the
image-checked text, and the open spans covered 306 of the 319 corrections the
model's text needed. The misses were accents and figure labels that neither
engine read. `ru-print-0.2` is the same, except that a grouped span (words only
one side has, or an unpaired run) with a digit on either side is `number`, as a
paired one always was; 0.1 called it `word`.

`latin-print-0.1` is for Latin-script print. It treats spacing, line-break
hyphenation and punctuation as the Russian sets do, files grouped spans with
digits as `number` as `ru-print-0.2` does, and judges words with Latin letters
(rule G needs a lexicon, such as a [word list](#a-word-list-as-the-lexicon)).
Its glyph pairs are the misreadings RapidOCR's English model made at least three
times, where the reader's word was the printed one, on the 1934 Washington
telephone directory tiles of the 2026-10 dogfood. Unlike the Russian pairs, each
instance was judged from its line and the directory's recurring exchange names
(`DI strict`, `NA tional`), not checked against the image one by one. The pairs
are the literal engine's e for the
reader's c (`striet` for `strict`), i for l, I or t, I for l, l for i or I, t
for l, h for b, r for t, f for t, y for v, m for n and u for n. Pairs are listed
exactly, so a capital E for C is not settled. Left out on purpose: a for o,
because «Soloman» and «Solomon» are both printed surnames on the same page and a
model that writes the common spelling must not be settled by rule; c for o, rn
for m and cl for d, seen twice or less; and the digit confusions (O for 0, l or I
for 1), because a span with a digit is never settled by rule. Several pairs
(I for l in `NatI`, m for n in `tiomal`) occur mostly in abbreviations and
exchange names no general word list holds, so they settle little there. In a
job without the `rough` trigger the lexicon has no `rough_below`:

```yaml
  contest:
    rules: latin-print-0.1
  lexicon:
    provider: wordlist
    path: /usr/share/dict/words
```

Measured on those pages only (54 column tiles of three pages, Claude Haiku 5.5
as the reader against RapidOCR with the English model, the macOS word list):
`ru-print-0.2` settled none of 1,118 spans; `latin-print-0.1` settled 81 (7%)
and left 1,037 open (662 `word`, 211 `number`, 127 `misprint_guard`, 37
`region`). The pairs were chosen on these same pages, so this is not an
independent test. Of the 81, 56 are the exchange name `DI strict` read as
`striet`. Within the one hand-transcribed column (three printed lines, read in
two overlapping tiles), the four spans the rules settled were all right, and the
model's lines had no error there for a rule to hide. Four more settlements were
checked against the tile images (Harold, Stern and two `strict`) and were right;
the other 73 were read against their lines only. On the 20 crops of later
directories (1946-1975) kept from that dogfood, the same rules settled 1 of 720
spans. Most open spans on directories are numbers, exchange names split
or joined differently (`NA tional` against `NAtional`, where the page prints the
split) and columns read in another order; the rules leave all of those open.

The spans are kept in `contested/<page_id>.json` ([schema](../schemas/contested.schema.json)),
and the page records its hash and counts. A page with an open span is held as
`contested`. That replaces `engine_disagreement`, `numeric_disagreement` and
`unconfirmed_model_output` on that page, because the spans say exactly where the
readings differ. `verify` rebuilds the spans from the retained readings and fails if the
artifact differs. The report counts spans by kind, settlements, and the pages
they hold.

On a contested page, the coverage, low-confidence and blank holds of readings
the page no longer rests on are dropped: the model and the literal engine, and
the spans between them, have read what those readings missed. Holds on the two
readings themselves stay.

## Settle contested spans

Rules settle what the two readings alone decide. The rest is settled against the
page image, by an agent or a person, outside PageLedger:

```bash
pageledger adjudicate jobs/book --packets packets/book
# an adjudicator answers each packet
pageledger adjudicate jobs/book --decisions packets/book --reviewer agent:sol
```

`--packets` writes, for every page with open spans, `<page_id>.json` with the
open spans, and the exact image the model read (`<page_id>.jpg`), with
`INSTRUCTIONS.md`. Packets are blind: each span gives the two readings in sorted
order and the text around the place, never which engine read which. The
surrounding text is the model's, so the blinding is partial. In a test
on Большаков (7 pages, 60 open spans, Sol as the reader), Sol adjudicating
spans labelled as its own reading kept three of its own substitutions (мощными
for the printed модными) and called each one plainly printed; blind, it did no
better. Gemini 3.1 Pro, blind, left one error, a printed misprint it corrected.
Claude Sonnet 4.6 corrected three misprints. Use an adjudicator from another
model family than the reader, and check its answers on pages you have verified. The adjudicator answers in `<page_id>.decisions.json`:

```json
{"page_id": "doc_0001_page_0003", "contested_sha256": "…",
 "decisions": [{"span_id": "sp_9f…", "text": "поныне", "confidence": "high", "note": "print reads поныне"}]}
```

`text` is what the page prints at that place: the model's reading, the literal
one, other text, or nothing. `note` is optional and recorded as empty when absent. A printed misprint is written as printed.
Packets and answers have schemas
([packet](../schemas/adjudication-packet.schema.json),
[decisions](../schemas/adjudication-decisions.schema.json)).

A large job can have thousands of open spans (a directory of 69 column crops had
3,511). To work the ones that matter first, name terms:

```bash
pageledger adjudicate jobs/directory --packets packets/soviet --term Soviet --term USSR --term "U S S R" --term Tass
```

A packet then holds only the open spans that touch a term: the term appears in
either reading of the span, or in the text the packet shows around it (80
characters of the model's reading on each side). A term matches whole words in
any case, and a space in it matches any spaces or a line break, so `U S S R`
finds the name split across two lines. An entry whose term is printed further
away, under a block heading for example, is not matched; name the heading's
words as terms too. The filter changes nothing in the job: every other open span
stays open, counted and holding its page, and answers for a subset of a page's
spans are recorded as usual. The command reports the open spans, how many went
into packets and how many were left out, and each packet lists its `terms`. Run
it again with other terms, or with none, into a new directory for the rest.
`--decisions` checks each answer against the spans it names and records it as an
adjudication receipt (schema 0.2) on the page, with the reviewer, the time and
the hash of the image. Answers for spans that have since changed are refused;
write packets again. A later answer for a span overrides an earlier one.

Only a `high` answer settles a span. When no span on a page is open, the page is
`adjudicated_text`, and, if any answer changed the text, PageLedger writes
`editions/<page_id>.txt`: the model's reading with each change applied at its
offsets. The attempt itself is never edited. The transcript, `document.json` and
every export use the edition and say so. `verify` rebuilds each edition from
its receipts and fails if the file differs.

`adjudicated_text` is not `reviewed_text`. It means every word-level difference
from the literal reading was settled, by a rule or an adjudicator, or there was
none; punctuation and the order of words that moved together are not compared,
and no person has read the page. The report counts adjudicated pages on their own line,
and `unresolved_pages` keeps its meaning: pages without a person's review. A
person can answer packets too, under their own `--reviewer` name, or review the
page as before.

## Read dense pages in tiles

A vision model asked to transcribe a dense page of many columns, such as a
directory or a newspaper, often reads one column and stops. Cut along the white
space between columns and between lines into pieces small enough for the print
to stay legible, the same page is read almost whole. In a test on a telephone
directory, a model given a whole spread transcribed one column and stopped;
from pieces cut at the gutters it read 138 of 141 lines exactly (four pieces of
three pages, checked by hand). The ledger keeps the plan of the cuts, the exact image
of every piece sent, and where each piece's text lies in the page's reading,
so every line can be traced to the piece of the page it was read from.

```yaml
processing:
  local_ocr: {adapter: rapidocr, adapter_options: {rec_model_dir: ..., reading_order: columns}}
  image: {adapter: vision, adapter_options: {base_url: ..., model: ..., max_image_side: 2048}}
  tiling:
    stages: [image]          # the stages read in tiles: image only, for now
    layout: local_ocr        # the stage whose boxes the page is cut along; or local_text for alto
    min_line_pixels: 32      # optional: how tall a median line should render, in pixels
    max_tiles: 24            # optional: most tiles for one page (at most 99)
  limits: {max_image_pages: 80, max_image_requests: 1500}
```

How a page is cut. Before a tiled stage reads a page, PageLedger plans its
tiles from the layout of the `layout` stage's reading of that page: the boxes
where that engine found lines (`rapidocr`) or words (`alto`). The planner
(`pageledger/tiling.py`) finds the gutters between columns and gives a line
that runs across them (a heading) a full-width band, then cuts each column into
pieces short enough that their lines render at `min_line_pixels` within the
reader's `max_image_side`, cutting in the whitespace between lines and
preferring a gap above an unindented line, the start of an entry. Tiles share
edges, never overlap and cover the whole page, so text the layout missed still
falls in some tile. They are read in order: bands and column regions from top
to bottom, columns from left to right, a column's pieces from top to bottom.
Each tile is rendered at no more than the page image's own resolution, sent as
its own image with the stage's prompt, and its text joined to the others with
a line break. A tile whose first line is indented records, for review, the
heading of the entry it continues; the heading is not sent to the model. When
the layout stage is `local_text` (an ALTO file) and fewer than nine in ten of
its word centres lie inside a line the job's own OCR found, its boxes are taken
to point elsewhere: the page is cut along the OCR's layout and the reading is
marked `alto_layout_rejected`. A page with no layout is read as one tile, the
whole page, and marked `tile_layout_missing`.

What the ledger keeps. The plan goes into the stage's configuration
(`.job/<stage_id>.yml`, `adapter_options.tiles`, which the job hashes), and the
stage records its `tiles`: the plan's SHA-256, the number of tiles and the
reading whose layout it was cut along. The layout itself stays in that
reading's recovery record, bound by the `layout_sha256` its provenance holds.
The reading's image evidence is of kind `page_tiles`
([image evidence](image-evidence-spec.md#page-tiles)): every tile image sent,
under `evidence/<page_id>-tNN.jpg`, with its hash, the box actually rendered,
the renderer's arguments, the model that answered, and where its text lies in
the reading. The attempt in `job.json` records `tiling`: the plan's hash, the
layout's reading, how many tiles were planned and sent, and which tiles failed
or cut through text. `verify` plans every page again from the retained
layout and refuses a different plan, checks each tile image against its hash
and each rendered box against its planned one, and checks that the reading is
exactly its tiles' texts in order.

Warnings and holds. `tile_failed` (a tile the model would not read) and
`tile_edge_cut` (a tile edge runs through a line some literal engine found on
the page) hold the page as `coverage_defect`. `tile_text_small`,
`tile_layout_missing` and `alto_layout_rejected` are recorded only. See
[warnings](warnings.md#quality-warnings).

Contest and adjudication. A tiled reading follows its tiles' order, which is
not the literal engine's. When the job contests readings, the witness is the
literal reading's own layout regrouped tile by tile
(`tiling.project_witness`), written to `witnesses/<page_id>.txt` and named in
the contested record as `witness.projection` and on the page as
`contested.witness_projection`; spans are then about words, not about order.
This needs a witness with a layout (`rapidocr`, `alto`); a witness without one,
such as a PDF text layer, is compared in its own order. Adjudication packets for a tiled page have no single image: each span names
its `tile_id`, the tile images are copied beside the packet as
`<page_id>-tNN.jpg` and listed under `images`, and the receipt's
`image_sha256` is null.

Budget. Each tile is one request, and `max_image_requests` bounds them; a page
whose planned tiles would pass it pauses the job (`paused_budget`) before any
of its tiles is sent, and `resume --raise-limit max_image_requests=N` goes on.
`max_image_pages` still counts pages. A page whose read stops after some tiles
were paid for (a timeout, say) is held as `provider_failure` with those tiles
kept, never read again automatically; see
[Failed remote reads](#failed-remote-reads) for what happens and what to do.

Limits. Tiles are cut only along a layout; there is no blind grid. A tiled stage
reads every tile of a page; it cannot read only some. A page that is retried is
read again whole, tiles already read are not reused. Each tile gets the stage's
own prompt, without the heading or any word of where the tile lies. The literal
OCR still reads the whole page, not the tiles. Column order assumes
left-to-right print. Only the `image` stage can be read in tiles so far, not
`local_image` or `second_opinion`. `vision` is the only built-in adapter that reads tiles
(capability `page_tiles`); `rapidocr` and `alto` are the built-in layouts
(capability `layout`). Engine comparisons that are not contested (`engine_disagreement`)
compare the tiled reading with the literal one in each reading's own order: use
`reading_order: columns` for the OCR, or contest the job.

## One budget for the job

`processing.limits` accepts:

| Field | Enforcement |
|---|---|
| `max_attempt_pages` | Counts attempts, not unique pages: every attempted source page at every stage counts, including failed/uncertain attempts. A local batch larger than the remaining allowance is cut to fit; the job then pauses. At process start, PageLedger prints the maximum possible attempts for the selected pages and configured stages when that projection exceeds this limit. |
| `max_image_pages` | Required positive limit when image processing is enabled. Counts both image and second-opinion attempts; checked before every call. |
| `max_local_image_pages` | Required positive limit when a `local_image` stage is configured, and accepted only then. Counts that stage's attempts; checked before every call. |
| `max_image_requests` | Required positive limit when `processing.tiling` is configured, and accepted only then. Counts the requests the `image` and `second_opinion` stages send: one per tile of a page read in tiles, one per other page. Checked before every page against the tiles planned for it. `usage.image_requests` in `job.json` holds the count. |
| `max_tokens` | Accumulates reported usage across stages. Stops at the cap before new work and after a response crosses it. A response can exceed the remaining amount; this is not a provider billing ceiling. Unknown paid token usage prevents another image attempt when this cap is configured. |
| `max_cost_usd` | Accumulates reported dollar charges. Stops at the cap before new work and after a response crosses it. Unknown paid cost stops another image attempt. The first charge can be unknown or exceed the remaining amount; use a provider-side spending limit for a hard monetary ceiling. |

A job that reaches `max_attempt_pages`, `max_image_pages`, `max_local_image_pages`,
`max_image_requests`, `max_tokens` or `max_cost_usd` ends with status `paused_budget`, keeping everything it has
done. It continues only when resumed with a higher limit:

```bash
pageledger resume jobs/book --raise-limit max_attempt_pages=200
```

`resume` checks the retained attempts before it records the raise. Each raise
is appended to `limits_history` in `job.json` and the report, with its time and
the previous value, and `verify` checks that the history leads from the
configured limits to the current ones. Limits cannot be lowered. Money and
token limits are thresholds checked between calls, not reservations: a call
already under way can take usage past them. Unknown paid usage while a token or
cost limit is set still halts the job, because a higher limit cannot make that
usage known.

Unknown cost stays `null`; `known_cost_usd` is only the available subtotal.
Local execution is not assigned an invented dollar price. Image calls are
bounded even when a provider does not report prices. Budgets persist across
resume; saved responses are counted once. Local batches check the remaining
budget before each pending call, including after recovering a saved response.

## Batches and concurrent readers

A job rebuilds its index from every child run after each page by default. On a
long document that rebuild costs more than the pages: in a 274-page vault job
the gap between image pages grew from 18 seconds to 100 while each call took
about 15. `processing.batch` rebuilds once per batch, and lets the remote
readers (`image` and `second_opinion`) read the batch's pages at once:

```yaml
processing:
  batch:
    pages: 16       # pages a stage runs between rebuilds
    concurrent: 4   # remote reader pages in flight; local stages stay one at a time
```

Both are positive integers, `concurrent` no more than `pages`. Every page is
still its own child run, planned in page order and recorded in `job.json`
before it starts, so a job interrupted mid-batch resumes as before and its
attempts, selections and reports are those of a job without `batch`. After the
first failure in a batch nothing new starts; pages already in flight finish and
are kept. Budgets count the whole batch before it starts. A job with
`max_tokens` or `max_cost_usd` set ignores `batch`, because those limits are
checked against usage measured after each page.

## Failed remote reads

A remote reader's page that fails with `MODEL_TIMEOUT`, `MODEL_HTTP_ERROR`,
`MODEL_NETWORK_ERROR` or `MODEL_UNAVAILABLE` without reading anything is
requested once more as a retry attempt (`image-0159-retry-1`), whether or not
the job uses `batch`; the retry counts against `max_image_pages`. "Without
reading anything" means the failed request kept no text: no partial output,
or one that is empty or only whitespace. The built-in `vision` adapter keeps
the exact image it sent and an empty reading on every failed request; that
image is evidence, not a reading. A page whose retry fails the same way is held
as `provider_failure` for review, and the job goes on to its other pages. Any
failure of the optional `second_opinion` witness holds only its page. A quota
failure (`MODEL_QUOTA`, HTTP 429) of the `image` stage, a partial output that
kept text (a reading cut off at `max_tokens`, say), a refusal, and an outcome
the job cannot know still halt the job.

A page read in tiles follows the same rules, with two additions. A tile the
model will not read (cut off, filtered, invalid, or empty where the layout
found text) does not stop the page: the page completes with the other tiles
and is held for `tile_failed`. A request that fails in a way a second request
could repeat (timeout, network, unavailable, an HTTP error) stops the page at
that tile. If no tile had been read yet, the page is requested once more like
any other. Otherwise the tiles read so far are kept as the page's partial
output, every tile sent is counted in `max_image_requests`, and the page is
held as `provider_failure` while the job goes on to its other pages. The
partial reading is evidence, never selected as the page's text; the report's
line for the attempt says how many tiles were planned, sent and paid for, and
read. A retry of a tiled page requests every tile of its plan again, and each
counts against `max_image_requests`: tiles already read are not reused.

Nothing is bought again without you. `resume` does not read a held page again:
the page waits for `review`, where you can record a decision on it from the
kept tiles and the source. To have the model read it again, start a new job for
that page (`process SOURCE --pages N --config FILE --out NEW_DIR`); in 0.9 that
reads and pays for every tile of the page again. A job that 0.8 halted after a
single timeout is rebuilt and verified as it was recorded and stays halted; to
read its remaining pages, start a new job for them.

## Durable job and attempts

`job.json` uses the checksummed envelope defined by `schemas/job.schema.json`.
Its payload includes the absolute root, source SHA-256, full/selected page
inventory, original configuration and hash, normalized policy, package hash,
ordered child plans, attempt records, page selections/reviews, usage and status.
The checksum detects inconsistent content. It does not establish authorship
or prevent deliberate rewriting.

### Moved and copied jobs

`root` is the directory the job was written in. It is a recorded fact, not
part of the job's identity: every command that reads a job reads it where it
is now, and every stage run's `checkpoint.json` must still name `root` plus
its `attempts/` path, so a stage run taken from another job or tree is
refused. `verify` on a job that is not at `root` checks the same evidence
as in place and adds warning `job_relocated` with `recorded_root` and `root`
(the current directory). `verify` does not read the source's bytes for
anything but this check: a missing source is warning `source_missing`, one
found through the source index is `source_relocated`, and other bytes at the
recorded path fail verification.

`review --apply` and `adjudicate --decisions` work on a job that is not at
`root`, and each such change appends `{root, command, at}` to the optional
`relocations` list, with the directory it was made in. `relocations` is
copied into `document.json` and shown in `report.md` as a "Changed elsewhere"
line; `verify` reports each entry as `job_changed_elsewhere`. A job
without the field reads and renders as before. `resume` refuses a job that is
not at `root` (see [Resume an interrupted job](#resume-an-interrupted-job)).

Child plans are saved before launch; their exact YAML snapshots are under
`.job/`. Runs under `attempts/` retain the ordinary generation-zero artifacts
and the resumable checkpoints. An attempt is identified by its run ID, child
path, attempt ID and source page; these are separate from rerun lineage IDs.
Existing run commands, selected denominators and generation-zero text replay
keep their contracts.

Before further calls, resume checks retained child records, outputs,
source/page identities and configuration snapshots. A child completed before
the job index was saved is adopted without extraction. The child runner recovers
a saved response that has not yet been published. A request without a saved
response becomes `outcome_unknown`. For built-in `text`, `pdf_text`, `pdf_ocr`
and `rapidocr` in local stages, the job retains that child and retries its
unfinished pages once in a new child; completed pages are reused. The retry
counts toward the existing attempt budget and requires unchanged package code.
Paid and custom adapter uncertainty still stops all later calls. Typed provider
failures and truncated output stop the queue. A second-opinion HTTP 429 that
exhausts the vision adapter's bounded backoff instead leaves its page held as
`provider_failure` and lets other pages continue; the rejected attempt remains
in the ledger. Unknown cost or token usage still respects configured budgets.
Failed partial output is kept under `partials/` and linked in the report, but cannot be selected as the transcript.
Missing image input evidence also stops the job and excludes that response from
selection. The [checkpoint specification](checkpoint-spec.md) describes
individual-run recovery checks and limits.

An interruption before a child's durable plan exists produces an explicit
`initialization_incomplete` halt. No extraction is scheduled without that plan.
The incomplete directory is retained for inspection rather than overwritten.

Source bytes are hashed before work and rechecked before each publication and
resume. For page images the job's `source` also records `kind: page_images`
and the page manifest as `images`, its `sha256` is the manifest's hash (see
[page-image sources](run-manifest-spec.md#page-image-sources)), and each page
records `source_image_sha256`, the hash of its own image; `verify` fails,
naming each image that changed, went missing or was added. A page's number is
its position in the folder; `source.images` lists each page's file name in page
order, and the report shows both.

When the folder is a IIIF snapshot, `source` also records `iiif`: `sha256`, the
hash of `iiif-fetch.json` when the job was written; `requested_url`; `status`,
`complete` or `incomplete`; and `canvases`, one entry per page in page order with
the image's `name`, its `canvas_index` in the manifest and its `canvas_id` (null
when the source was a single image service), so a page can be cited by its
canvas. The record is context, not custody: `sha256` of the source is the image
manifest's hash, as for any folder. `report.md` names the snapshot and its
status. `verify` adds warnings, and still passes, for
`iiif_snapshot_incomplete` (the snapshot was incomplete when the job was written),
`iiif_record_changed` (the record's bytes differ now, while every image the job
read is unchanged) and `iiif_snapshot_grown` (a later `fetch --resume` added
the images listed in `added`, and every recorded image is still there unchanged).
A grown folder numbers its pages differently, so `verify`, `export`,
`review --sheet` and `review --out` work on the job, but `resume`, `review --apply` and
`adjudicate`, which change it, refuse the grown folder as a changed source; start
a new job for the added pages. A changed or missing image the job read still fails verification. PDF page-tree counts
must match the actual inventory. Container failure
produces a halted report with an unknown page count and no invented pages.
Its `halt_reason` names the cause, such as
`source_container_invalid:unsupported_encryption` for a PDF that needs a
password, and `next_action` says what to do. An encrypted PDF that opens
without a password (one that restricts only printing or copying) is processed
normally.
Annotation counts are recorded without copying annotation contents; annotation
presence does not establish recovery of comments, body text, notes or citations.

A `completed` job means its configured processing work finished. It does not
mean its pages have passed human review. A `paused_budget` job continues when
resumed with a higher limit. A `halted` job keeps its records, and `resume`
reports it without retrying, except for the bounded local recovery above;
settle any uncertain paid request with the provider before starting a new job.
`verify` is read-only and binds the report back to validated job and child
records.

## Reports and review

The job publishes `document.json`, `report.md` and `transcript.md`. See
[document-report-spec.md](document-report-spec.md) for their exact contract and
review receipt examples. `report.md` and the exact final UTF-8 transcript are
rendered from JSON. The transcript digest covers its serialized bytes, including
headings and source-page links; no implicit NFC conversion occurs.
Reports name the source by its path relative to the job directory. To use the
text elsewhere, [export it](export.md) as plain text, Markdown, JSONL or TEI.

```bash
pageledger review jobs/book --apply reviewed-pages.json
pageledger inspect jobs/book --json
```

Applying review receipts performs no extraction and retains previous receipts
in `review_history`. Changing reviews in an interrupted processing job halts its
old plan, so pending calls cannot ignore a newly recorded source defect. The
report links every selected source page and attempt, retains unresolved states
and failed partial links, and gives one next action. Optional `processing.links.article` and
`processing.links.custody` are references supplied by the caller, not verified
claims of preservation or publication. Source capture, preservation, removal
eligibility and removal are separate fields. This implementation never moves,
deletes, publishes or certifies preservation of source files.

### Review in a spreadsheet

```bash
pageledger review jobs/book --sheet review.csv
pageledger review jobs/book --apply review.csv --reviewer "A. Reader" --dry-run
pageledger review jobs/book --apply review.csv --reviewer "A. Reader"
```

`review --sheet` writes one row per page: a link to the page in the source, its
disposition, the start of its selected text, its review reasons, and empty
`decision` and `note` columns. Fill those in a spreadsheet and save as CSV. A
decision is one of:

| Decision | Records |
|---|---|
| `accept` | The selected text, as `reviewed_text` |
| `blank` | The page as blank, `reviewed_blank` |
| `illustration`, `handwriting`, `unreadable`, `source_defect` | That disposition |
| `use:ATTEMPT` | Another completed attempt's text, named by its attempt ID from the report |
| `correct:FILE` | The text in FILE, as `reviewed_text` with a [correction](#correct-the-text) |

A blank decision, or a deleted row, leaves the page unreviewed; the note becomes
the receipt's reason. `review --apply` checks the whole sheet before recording
anything: every row must belong to the job, appear once, hold no spreadsheet
formula, and still match the saved job records it was written from, which
`review.csv.binding.json` records beside the sheet. A sheet written before the
job's records changed is refused; write a new one. `--dry-run` checks and
counts the decisions without recording them. Both warn (`blank_with_text`) when
a page recorded as blank has a selected reading of 50 or more words: in the
2026-11 dogfood a page reviewed as blank carried 1,295 words of OCR. Check the
page; the decision is still recorded as made. Scripts can keep writing JSON
receipts.

### Correct the text

When the selected text is wrong, `accept` would approve a known error and
`unreadable` would discard a page that can be read. A correction records the
right text instead. It is kept as a new reading beside the engines' readings,
which are never edited, and a receipt binds it to the reading it was made
from, so the job shows what the engine read, what a person changed, and who
changed it when.

There are two ways to supply it. In a review sheet, save the page's corrected
text in a UTF-8 file and write `correct:FILE` as the decision. FILE is a path
relative to the sheet's folder and must stay inside it. The correction starts
from the page's selected text.

```bash
pageledger review jobs/book --sheet review/review.csv
# copy page 3's text into review/p0003.txt, fix it, and write correct:p0003.txt in its row
pageledger review jobs/book --apply review/review.csv --reviewer "A. Reader" --dry-run
pageledger review jobs/book --apply review/review.csv --reviewer "A. Reader"
```

The [review view](#review-beside-the-page-image) instead downloads a decisions
file, `review.json` ([schema](../schemas/review-decisions.schema.json)): the
sheet's bindings and the rows with a decision, each with `row_id`,
`page_number`, `decision` and `note`. A row whose decision is `correct` carries
the corrected text in `text`, and may name in `from` the attempt whose own
reading it starts from; without `from` it starts from the selected text. Rows
in a decisions file may also use `correct:FILE`, relative to the file's
folder. `review --apply` checks it as it checks a sheet, takes `--reviewer`
the same way, and refuses it if the job has changed since it was written.

What is recorded is exactly what the reviewer wrote, after one normalization:
Windows (CRLF) and old Mac (CR) line endings become LF. Nothing is trimmed or
Unicode-normalized; ѣ, combining accents, tabs and trailing spaces are kept. A
byte-order mark at the start of a correction file marks the file as UTF-8 and
is dropped. A file that is not UTF-8 is refused, and so is a correction that is
empty (decide `blank` instead) or identical to the reading it started from
apart from line endings (decide `accept`). A change of spacing alone is a
correction.

`review --apply` writes the text once to
`corrections/<page_id>-<sha256>.txt` and records the page as `reviewed_text`
with a `correction` in its receipt: the file, its hash, the hash of the text it
started from, and how many words it changed, added and removed (see the
[receipt](document-report-spec.md#corrected-text)). The text becomes the
page's selected text in the report, transcript and exports. Both `--dry-run`
and the recording print each correction's size, and warn, without refusing,
when a correction changes more than half the words of the reading it started
from, which usually means another page's text was pasted.

A page can be corrected again. The new correction starts from the earlier one
and the report links the correction it revised; both stay in
`review_history`. Accepting a corrected page keeps its correction; to go back
to a machine reading, decide `use:ATTEMPT`. A plain JSON receipt carries no
binding, so it cannot show that its author saw the correction: `review --apply`
refuses one that decides a corrected page unless its decision carries that same
`correction`. Decide such a page in a review sheet or the review view, which
show the corrected text. A job that was moved or copied
accepts corrections where it is now, like other reviews, and records the
change in `relocations`. `verify` checks every correction's file against its
hash and refuses a correction that does not start from a recorded text.

### Review beside the page image

A transcript can only be checked against the page it claims to read: a dropped
line, a misread number or a word a model supplied is visible in the image and
nowhere else. Reading the text beside the image, rather than with the scan open
in another program, keeps the eye on one line at a time.

```bash
pageledger review jobs/book --out review/
pageledger review jobs/book --out review-held/ --images held
pageledger review jobs/book --out review-text/ --images none
```

`review --out` verifies the job, as `export` does, then writes a new folder:
`index.html`, a blank `review.csv` with its `review.csv.binding.json` (the same
files `review --sheet` writes), and with `--images all` (the default) or `held`, a
JPEG of each page, or only of the pages that need a person, in `images/`.
Open `index.html` in a browser; it needs no server and loads nothing from the
network. It lists the pages, those that need a person first, with their state,
what holds them and how far the engines agree. Under each page are its image
beside the selected text, the source page and any image a model was sent, the
warnings recorded on each reading (each code with its meaning in plain words,
from the same table the report uses), and the other readings, each shown in full
and compared word by word with the selected text. On a contested page, the open
spans are marked in the reading they were found in and listed with what each
engine read. Transcripts are shown exactly, with their line breaks and spacing;
characters a reader cannot see appear as marked symbols: control characters as
symbols such as ␍, and format characters (text-direction overrides, zero-width
spaces, soft hyphens and the like) as their code, such as `U+202E`. Zero-width
joiners, which shape Arabic, Persian and Indic script, are left as written. Two
readings whose word counts multiply to more than 25,000,000 (about 5,000 words
each) are not compared word by word; the view says so and shows both in full.

Under each page a reviewer can choose one of the decisions in the table above
and add a note, or correct the text. The text box starts from the selected
text, or from another reading chosen under "Start from" (choosing
`use:ATTEMPT` first starts it from that reading); editing it chooses
`correct`, and the changes are marked below it, struck words the reading's and
underlined words the reviewer's. A reading that a browser text box cannot hold
exactly is not offered for editing; correct it with `correct:FILE`. A note cannot start with `=`, `+`,
`@`, a tab or a carriage return, which a spreadsheet program could run as a
formula: the view says so and `review --apply` refuses such a sheet. A note may
start with a dash, because such notes are ordinary ("-1 word illegible"); so a
review sheet received from someone else should be treated like any spreadsheet
from outside, opened with formulas disabled. The cells PageLedger itself writes
never start like a formula: a source path or file name that begins with `=`,
`+`, `-`, `@`, a tab or a carriage return is written with `./` in front, which
names the same file. A reading holding characters a text box does not show
(soft hyphens, zero-width spaces and the like) can be corrected in the page,
which says how many there are and which; they stay where they are unless the
text around them is deleted. One holding control characters or text-direction
controls, which a text box would drop or which would reorder the text while it
is edited, is corrected through a file. For a PDF source, the view renders a copy of the source
checked against its recorded hash, so a file changed while the view is written
stops the command instead of showing another page. The
browser keeps the choices and the edited texts for that job, if it allows the
view to store them, until "Forget decisions in this browser" clears them.
Nothing is recorded in the job until the downloaded file is applied:

```bash
pageledger review jobs/book --apply review/review.json --reviewer "A. Reader" --dry-run
pageledger review jobs/book --apply review/review.json --reviewer "A. Reader"
```

`review.json` is the [decisions file](#correct-the-text): it carries its own
bindings, so it can be saved anywhere. Without corrections, "Download
review.csv" still writes the review sheet; save it in the folder in place of
the blank `review.csv`, so that the binding is beside it, or download the
binding too and keep the two together. The sheet download refuses while any
page is corrected, because a sheet cannot carry the text. `review --apply`
checks either file as it checks any review sheet, and refuses it if the job has
changed since the view was written. Like `report.md`, the view is a
rendering: it is not evidence, and writing it changes nothing in the job. Page
images need Poppler's `pdftoppm`; `--images none` writes the view without
them. For a PDF source, a missing `pdftoppm` stops the command. For a source of
page images, each page's own image is found by its recorded name, checked
against its recorded hash and rendered through a one-page carrier, so the view
holds a JPEG of at most 1,600 pixels on its long side with the image's EXIF
rotation applied, not a copy of the master; without `pdftoppm` the view is
written without images and each page links to its image file instead. Pages are
headed with their number in the job and their file name, as `Page 9
(0081.jpg)`. The images are copies inside the view's folder, so a view keeps
working when the job is moved; the links to the source are relative to where
the view was written. A text source has no page images.

### Review at scale

See the [warnings reference](warnings.md) for warning and disposition meanings
and suggested next steps.

`unresolved_pages` counts pages whose disposition is not `reviewed_text` or
`reviewed_blank`. A page remains unresolved when it is labeled `illustration`,
`handwriting`, `unreadable`, or `source_defect`: those decisions describe the
page, but do not establish that its text was checked or recover missing source
content. A completed job can therefore have unresolved pages.

For a long document, define and record a sampling policy before review. For
example, review every page with a warning or disagreement, then inspect a
fixed sample from each remaining section, including the first and last pages.
Record the section boundaries, sample rule, reviewer, and pages checked in the
review notes or project records. A sample does not make uninspected pages
reviewed; their dispositions and unresolved count remain unchanged. Use a
review sheet to collect decisions for sampled pages, but keep other rows with
blank decisions so those pages remain unreviewed.

## Image adapter boundary

The built-in [vision adapter](vision-adapter.md) reads a page image with a
model behind an OpenAI-compatible endpoint: on this machine, or over HTTPS
with `allow_remote: true`. In the image and second-opinion stages it keeps the
exact JPEG it sent, as described in [image evidence](image-evidence-spec.md):

```yaml
# fragment
processing:
  image:
    adapter: vision
    adapter_options:
      base_url: http://127.0.0.1:8080/v1
      model: YOUR_VISION_MODEL
```

A model writes its reading rather than reading it off the page. Unless a
clean reading from another engine agrees with it, the job holds that text for
review as `unconfirmed_model_output`; see [warnings and holds](warnings.md).

Core includes no provider SDK, OCR engine or pricing catalog.

Image-evidence runs currently refuse `bundle` with an explicit unsupported
error. They remain verifiable and resumable in place. Original text runs (not
reruns) can still be bundled and replayed. Whole-job portable bundles, synthesis, Zotero/Drive connectors, and file
retirement are outside the processing workflow.
