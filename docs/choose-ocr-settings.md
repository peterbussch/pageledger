# Choose OCR settings

Choose settings from a sample, not from a claim that one language pack fits all
pages. An OCR language pack is the engine's trained data for recognizing a
script or language. PageLedger passes the configured language value to the
adapter; it does not install or select packs for you.

## Language checklist

- Modern Russian: start with Tesseract `rus`. Set `language.script: Cyrillic`
  when you want the script-mismatch check. Do not set pre-reform orthography
  for modern text.
- Pre-reform Russian: test `rus` on a sample, but expect it to modernize or
  misread abolished letters. Set `language.orthography: prereform` to detect
  likely loss; it does not make the OCR model historical. Compare a community
  historical model against the page image and reference text before relying on
  it. See [multilingual OCR](multilingual-ocr.md#an-1850-scan-in-pre-reform-orthography).
- Spanish: install Tesseract's `spa` pack and test accents, punctuation and
  names on representative pages. For Spanish mixed with another language,
  configure both installed codes, such as `spa+eng`.
- Other Latin-script languages: select the language's Tesseract code, such as
  `fra`, `deu` or `ita`, only after checking that pack is installed. Use a
  combined code such as `eng+fra` for genuinely mixed pages. Latin script alone
  does not identify the language.

For language names supported by the installed Tesseract build, run
`pageledger doctor`. It reports installed packs. A configured but missing pack
stops the OCR run before extraction and identifies the installed list.

## Install Tesseract language packs

PageLedger does not install Tesseract or its language data. On macOS with
Homebrew, install the engine and language data separately:

```bash
brew install tesseract
brew install tesseract-lang
```

On Debian or Ubuntu, install the engine and the required trained data packages.
For example:

```bash
sudo apt install tesseract-ocr tesseract-ocr-rus tesseract-ocr-spa
```

Package names and availability vary by distribution and Tesseract build. Check
the installed codes with `tesseract --list-langs`, then run
`pageledger doctor`. Keep only the language packs needed for the collection if
storage or deployment size matters.

## Choose a render resolution

`dpi` is the number of rendered pixels per inch sent to OCR. Begin around
300 DPI for ordinary printed pages, then compare a sample at a lower or higher
resolution when the scan is small, faint, or unusually detailed. Higher DPI can
make small type more legible, but increases rendering time, memory use and image
size. It does not restore detail absent from the source. PageLedger caps
oversized PDF renders at the adapter's `max_render_pixels` setting and records
when the requested DPI was lowered. See [OCR options](ocr-options.md) for the
adapter settings.

Use the same page sample for comparisons. Check transcription against the
image, including diacritics, numerals, columns and historical characters. A
higher confidence score alone does not establish a more accurate result.

## Bound job work

`processing.limits.max_attempt_pages` counts attempted source-page extractions
across all enabled stages. Failed and uncertain attempts count too. For a
300-page PDF with one local OCR pass, a cap of 300 allows one attempt per page;
if local text and OCR both run on all pages, plan up to 600 attempts. Add room
for retries or other stages only when the sample shows they will be used.

Choose a cap that fits the amount of work you authorize, while allowing a
complete local batch when possible. The job processes the pages the limit
allows and pauses with `paused_budget`; it does not discard completed work.
Continue by raising the limit:

```bash
pageledger resume jobs/book --raise-limit max_attempt_pages=600
```

The value must be higher than the current limit. Raises are recorded in the job
history. A resumed job keeps prior work and limits; it does not repeat completed
attempts. See [Plan time and cost](plan-time-and-cost.md) for estimating runtime
from measured samples and [processing limits](processing-spec.md#one-budget-for-the-job)
for other caps.
