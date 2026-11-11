# Engine recipes

These examples use the built-in `vision` adapter or custom adapters in
`examples/`. Scores are agreement with a model-made reference on 24 transcribed
pages of historical print, not accuracy; later runs through PageLedger used 19
of those pages. They describe that evaluation set, not a guarantee for other
documents.

## Local Qwen3.5-9B with mlx-vlm (macOS)

Qwen3.5-9B 4-bit was the strongest local model on prose (8.4% character error)
and directories and dictionaries (2.0%) in the evaluation; its overall error
was 16.0%, exact numbers 60%, and it kept 90% of pre-reform letters. It looped
on some pages. On an M4 Pro, it took about 90 seconds per page and used 8.3 GB
of memory. In a later run through PageLedger, the pages it finished took a
median of 38 seconds and measured 1.6% character error, but 4 of the 12 pages
it attempted looped until `max_tokens`; see
[time, heat and loops](vision-adapter.md#local-models-time-heat-and-loops). Use
it selectively and review every generated reading.

Install in an MLX-capable Python environment with `pip install mlx-vlm`
(tested with 0.7.3), then start
`mlx_vlm.server --model mlx-community/Qwen3.5-9B-4bit --port 8080`. The
config's model identifier is the same one listed by the server at `/v1/models`.
Qwen's thinking mode stays off unless you pass `--enable-thinking`. No separate
OCR package is needed. The model weights use the publisher's license; check
that license before redistribution.

```yaml
schema_version: "0.1"
run:
  adapter: vision
  adapter_options:
    base_url: http://127.0.0.1:8080/v1
    model: mlx-community/Qwen3.5-9B-4bit
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

## dots.ocr with mlx-vlm as the local reader (macOS)

In the 0.8 dogfood (20 hand-checked pages from 10 difficult PDFs: degraded
typescript, handwriting, a broadsheet newspaper, Cyrillic print, census tables,
a math paper, a Japanese-English dictionary, tilted pages), the 4-bit MLX
conversion of dots.ocr was the strongest model that runs on the Mac: median
character error 0.061 against Claude Haiku 5.5's 0.032, at a median of 31
seconds a page on an M4 Pro. It looped until `max_tokens` on 6 of the 20 pages
(both handwriting pages, both newspaper pages, both pages of a tilted
typescript), 70 to 86 seconds each. In a document job that is a failed
`local_image` attempt and the page goes on to the next stage. PaddleOCR-VL 1.6
read whole pages badly (it repeated lines and invented headlines) and is not
recommended as a page reader.

Start the server with mlx-vlm (tested with 0.7.3) on a free port, from a
local copy of `mlx-community/dots.ocr-4bit`:
`mlx_vlm.server --model PATH --host 127.0.0.1 --port 8081 --trust-remote-code`
(dots.ocr ships its own model code). Use the
model's own prompt:

```yaml
schema_version: "0.1"
run:
  adapter: vision
  adapter_options:
    base_url: http://127.0.0.1:8081/v1
    model: PATH
    timeout_seconds: 600
    max_tokens: 8192
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
      prompt: Extract the text content from this image.
```

In a document job it fits as `local_image`, with its own
`max_local_image_pages`. dots.ocr is MIT-licensed.

## GGUF vision model through llama.cpp (macOS and Linux)

This path was not run in the evaluation. A GGUF vision model runs locally
through llama.cpp's `llama-server`; quality, speed, memory and license depend
on the selected model and quantization. Install llama.cpp, download a compatible
GGUF model and its matching multimodal projector, then use llama.cpp's documented
command: `llama-server -m MODEL.gguf --mmproj MMPROJ.gguf --port 8080`.
Review the model's license and hardware requirements.

```yaml
schema_version: "0.1"
run:
  adapter: vision
  adapter_options:
    base_url: http://127.0.0.1:8080/v1
    model: local-gguf
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

## RapidOCR with PP-OCRv5 Cyrillic (macOS and Linux, CPU)

RapidOCR was the best permissively licensed engine in this evaluation: 11.0%
character error, 91% exact numbers, and 28% of pre-reform letters kept. It took
about 3 seconds per page on the M4 Pro CPU. PageLedger's built-in `rapidocr`
adapter runs it: install `pip install 'pageledger[rapidocr]'` (tested with
RapidOCR 3.9.2 and ONNX Runtime 1.30.0) and Poppler. Download `inference.onnx`
and `keys.txt` from
[PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec_onnx](https://huggingface.co/PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec_onnx)
into a recognizer directory; RapidOCR's default Russian model is not a
substitute, because it lacks ѣ. For a pinned detector, download
`inference.onnx` from
[PaddlePaddle/PP-OCRv5_mobile_det_onnx](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det_onnx)
into a detection directory. RapidOCR and both PaddleOCR models are Apache-2.0.
If `det_model_dir` is omitted, RapidOCR may download its default detector on
first use. Any PP-OCRv5 recognizer works the same way; each page's provenance
records the recognizer's hash.

```yaml
schema_version: "0.1"
run:
  adapter: rapidocr
  adapter_options:
    rec_model_dir: /models/ppocrv5-cyrillic
    det_model_dir: /models/ppocrv5-mobile-det
    max_side: 3200
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

Run with `pageledger run book.pdf --config pageledger.yml --out runs/rapidocr`.

## RapidOCR with PP-OCRv5 English (macOS and Linux, CPU)

The English [PaddlePaddle/en_PP-OCRv5_mobile_rec_onnx](https://huggingface.co/PaddlePaddle/en_PP-OCRv5_mobile_rec_onnx)
recognizer publishes its character list as `PostProcess.character_dict` in
`inference.yml` instead of `keys.txt`. Put `inference.onnx` and `inference.yml` in the recognizer
directory; PageLedger reads that list when `keys.txt` is absent and records
hashes for both the recognizer weights and the dictionary actually used. The
adapter uses the same RapidOCR and Poppler setup described above. The model is
Apache-2.0.

```yaml
schema_version: "0.1"
run:
  adapter: rapidocr
  adapter_options:
    rec_model_dir: /models/ppocrv5-english
    det_model_dir: /models/ppocrv5-mobile-det
    max_side: 3200
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

In the 2026 telephone-directory dogfood, RapidOCR read 24 of 32 lines exactly
on one hand-checked directory column. This is one column sample, not a general
accuracy estimate.

RapidOCR finds lines of text, and the adapter puts them in order. By default
(`reading_order: rows`) it reads each row across the whole page, which keeps
the cells of a table together. On a page set in two columns, such as a journal
article, that reads line by line across the gutter and glues the halves of
neighbouring lines together. Set `reading_order: columns` for such pages: the
adapter then cuts the page at its whitespace and reads each column from top to
bottom. Do not use it for tables, whose columns it would read one at a time.
The page's provenance records the choice (`reading-order=columns`).

Each page is rendered with `pdftoppm` before recognition, and a render that
takes longer than `render_timeout_seconds` (default 120) fails the page. Raise
it for very large scans or sources on a slow disk.

## Indra OCR (macOS and Linux, CPU)

[Indra OCR](https://huggingface.co/datalab-to/indra-ocr) is a small OCR model
from Datalab that runs on the CPU through ONNX Runtime. It finds the lines of a
page, reads them with a recognizer for the line's script (Latin and Cyrillic,
CJK, right-to-left, and the Indic and Southeast Asian scripts), labels layout
blocks and puts them in reading order. It also straightens tilted pages and
turns pages set sideways. PageLedger's built-in `indra` adapter reads one page
at a time:

```yaml
schema_version: "0.1"
run:
  adapter: indra
  adapter_options:
    mode: ocr          # or auto: Indra reads a sound text layer itself
    dpi: 300
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

Install with `pip install 'pageledger[indra]'`. The first page downloads the
models (about 76 MB) from Hugging Face into its cache; if that download stalls,
`HF_HUB_DISABLE_XET=1` has worked. To pin a downloaded copy, set `model_dir`
or `INDRA_OCR_MODEL_DIR` to the folder. `math: true` writes formulas as LaTeX
and downloads the math models; `output: markdown` keeps headings and tables
(as HTML) instead of plain lines. Each page's provenance records the indra-ocr
version, a hash of the model files, the mode and the dpi; the line confidences
become the page's quality evidence, and the layout blocks, rotation and skew
are kept with them.

On the 20 hand-checked pages of the 0.8 dogfood, Indra in `ocr` mode measured
a median character error of 0.159 against Tesseract's 0.242, at a median of 0.7
seconds a page against 1.8 (M4 Pro, one thread per page). It was much better
on a typescript tilted 3 to 4.5 degrees (0.04 against 0.48), on a born-digital
paper whose fonts have no Unicode map (0.012 against 0.146) and on Cyrillic print
(0.004 and 0.033), and weak on a degraded carbon typescript (0.37 and 0.66) and
cursive handwriting (0.32 and 0.39), where it finds lines but leaves many of them
empty. The adapter counts those as unread, so such a page is held as
`low_confidence` and climbs.

Indra is a literal engine, but when it is unsure of a line it rescores the
candidates with a character language model, which can prefer a common spelling
to the printed one. In a document job it can be the first OCR (`local_ocr`),
with Tesseract or RapidOCR as `second_ocr`, a literal witness from another
engine.

The code is Apache-2.0. The model weights use a modified OpenRAIL-M licence:
free for research, personal use and organizations under $20M in funding or
revenue, and not for use that competes with Datalab's API. PageLedger never
ships them; read the licence before commercial use.

## ALTO from a library (any platform, no engine)

ALTO is the XML format libraries use to publish the OCR they ran on their own
scans: each word of a page with its text, its position on the page image and
usually the engine's confidence. The built-in `alto` adapter reads one such file
per page, so the library's OCR enters a run or a document job as one more
reading, with the file and its digest in the page's provenance.

The Library of Congress offers an ALTO file beside each page image of many
digitized items, and Chronicling America one per newspaper page (`ocr.xml`);
many national libraries publish ALTO for their newspapers too. Download the
files for the pages you read into one folder, named so that a page number
finds its file, and point the adapter at it:

```yaml
schema_version: "0.1"
processing:
  local_text:
    adapter: alto
    adapter_options:
      dir: /data/reel04673/alto
      name: "{page:04d}.xml"    # page 1 reads 0001.xml
  local_ocr:
    adapter: rapidocr
    adapter_options: {rec_model_dir: models/en-rec, reading_order: columns}
```

`name` is a template: `{page}` is the page number counted from 1, written
with an optional zero-padded width (`{page:04d}`); `{{` and `}}` are literal
braces. The template must name a file inside `dir`, and a file that resolves
outside it is refused: `..` in the template when the job starts, a symbolic
link out of `dir` as an `alto_invalid` page. The adapter compares each ALTO
page's shape with the page as rendered and needs `pdfinfo` from Poppler for
that.

When the source is a [folder of page images](process-a-collection.md#page-images),
libraries usually name each ALTO file after its image: `0081.xml` for
`0081.jpg`. Use `{stem}`, the page image's file name without its extension:

```yaml
    adapter_options:
      dir: /data/reel04673/alto
      name: "{stem}.xml"        # the page read from 0081.jpg reads 0081.xml
```

This matters when the images keep their numbers from a larger item, as a
[IIIF snapshot](iiif.md) fetched with `--pages` does, or when a number is
missing, as in an incomplete snapshot or a folder with a page left out: there the
ninth page of the job can be `0081.jpg`, and `{page:04d}.xml` would read
`0009.xml`, another page's text. So `{page}` is refused for page images whose
names carry a number (the last run of digits in the name) that is not their page
number, before the job or run starts, and the message suggests `{stem}`. It
cannot tell when the images were renumbered after a page was dropped, or when
their names carry no numbers at all; check the pairing on the first pages. Keep the
ALTO files in their own folder, not beside the images: a folder of page images
holds nothing but page images. `{stem}` works in a plain `run` and in a job; a
PDF's pages have no file names, so with a PDF source `{stem}` is refused before
the job or run starts.

The text is built from the words, not from ALTO's text lines, which can run
across columns: words in one row that stand less than a word's height apart are
joined, the page is cut into columns at the whitespace between them as
RapidOCR's `reading_order: columns` does, and each column is read top to
bottom. So a gutter narrower than a word's height is read as a word space (the
columns interleave row by row), and text set at a tab stop inside a column, such
as names with their numbers aligned far to the right, is read as two columns:
all the names, then all the numbers. If any word has no box, or one that is negative or too large to measure, the page keeps the file's order. Words are kept as printed: a word broken
at a line end keeps its hyphen (`HYP`) and the library's suggested whole word
(`SUBS_CONTENT`) is not used. Word confidence comes from `WC` (0 unsure, 1
sure) or else from `CC` (one digit a character, 0 sure, 9 unsure), and feeds
`low_confidence` like RapidOCR's.

Every reading with text carries `ocr_text_layer`, so in a document job the
rules for an inherited OCR layer apply and the page climbs to local OCR. In a
job written by 0.8 the ALTO reading is then set aside: its agreement still
confirms the OCR while its disagreement does not count against it. In a job
written by 0.9 it stays a witness ([hold policy 0.9](processing-spec.md#hold-policy-09)):
its disagreement holds the page for a person, it is selected when the OCR
reading is held and it is not, and an OCR reading that lost the ѣ it kept is
held. An ALTO reading held for a reason of its own, such as `low_confidence`
when a quarter of its words are under 60%, confirms nothing. A page
without ALTO (`alto_missing`) or with an unusable file (`alto_invalid`) gets an
empty reading and climbs the same way; boxes that do not fit the page are
reported as `alto_geometry_mismatch` (see [warnings](warnings.md)).

The model string names the file, its SHA-256 and the software the file
declares, for example `alto; file 0001.xml sha256:…; software ABBYY FineReader
8.0`, so a changed file is a different reading. `verify`
checks the recorded text against that record but do not read the ALTO file
again: it lies outside the source and the run, so keep the folder with the run
if the reading must be checked later, and compare its digest by hand.

Not supported: ALTO files that hold several pages, hOCR and PAGE XML, ALTO
used as the contest witness, and finding ALTO links in a IIIF manifest. The
word boxes are returned as the reading's layout (when they fit the page), so a
job can cut dense pages into tiles along them with `tiling.layout: local_text`;
see [Read dense pages in tiles](processing-spec.md#read-dense-pages-in-tiles).

## Tesseract with the community orus model (macOS and Linux)

The `orus` model source is [AButon-8/iskra_ocr](https://github.com/AButon-8/iskra_ocr),
which also contains the *Iskra* evaluation. The model measured 20.9% character error overall and kept 58%
of pre-reform letters, compared with 18.9% error and 35% retained letters for
stock `rus`. On the *Iskra* 1900–05 corpus, its publisher reports 1.30% versus
9.45%; that publisher result is not the 24-page evaluation. Stock `rus`
recognizes modern Russian and therefore loses historical letters such as ѣ,
ѳ and ѵ. Time per page and memory for `orus` were not measured; both depend on
render DPI, page size and hardware. Install Tesseract and Poppler, then install
the community `orus.traineddata`
in a tessdata directory, and set `TESSDATA_PREFIX` to that directory before
running PageLedger. Tesseract is Apache-2.0; verify the community model's own
license and source.

```yaml
schema_version: "0.1"
run:
  adapter: pdf_ocr
  adapter_options:
    lang: orus
    dpi: 300
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

## Apple Vision (macOS, post-1918 print)

Apple Vision measured 15.7% character error, 74% exact numbers, and retained
25% of pre-reform letters. It is suitable for post-1918 material, not
historical orthography: it never outputs ѣ, ѳ or ѵ and changes final ъ to ь.
The evaluation did not measure its per-page time or memory; both depend on
the Mac and render size.
Install Xcode command-line tools and Poppler, then build the helper:
`swiftc examples/apple_vision_ocr.swift -o apple_vision_ocr`. Apple Vision is
part of macOS; Apple's platform terms apply.

```yaml
schema_version: "0.1"
run:
  adapter: apple_vision_adapter:AppleVisionAdapter
  adapter_options:
    helper: ./apple_vision_ocr
    languages: ru-RU
    max_side: 3000
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

Use `--adapter-path examples` when running the config.

## Hosted OpenAI-compatible model

Gemini 3.8 Flash, read through an OpenAI-compatible gateway with
`reasoning_effort: none`, measured 3.8% character error and found 97% of
numbers on 19 of the pages, at a median of 16 seconds per page. On the same 19
pages, Claude Sonnet 4.6 measured 7.2%; on all 24 it measured 6.7%, with 92%
exact numbers and 87% of pre-reform letters kept. Without `reasoning_effort`,
Gemini spent its token budget reasoning and most dense pages failed; see
[models that reason before answering](vision-adapter.md#models-that-reason-before-answering).
Hosted speed and cost vary by provider; consult its service terms and pricing.
Install Poppler, set `GATEWAY_API_KEY` in the environment, and use the model
name your gateway lists at `/v1/models`:

```yaml
schema_version: "0.1"
run:
  adapter: vision
  adapter_options:
    base_url: https://gateway.example.org/v1
    model: gemini-3.8-flash
    env_key: GATEWAY_API_KEY
    allow_remote: true
    reasoning_effort: none
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

A gateway on this machine that forwards requests to a hosted provider still
sends page images off the machine. Use this path only for pages you have the
right to send there.

Surya OCR 2 also measured well, at 9.9% character error on the 24 pages. Its
weights, like Chandra's, are licensed only for research and small
organizations; check their terms. Chandra was not run on these pages. Neither
has a recipe here.


