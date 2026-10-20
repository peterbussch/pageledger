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
`mlx_vlm.server --model PATH --host 127.0.0.1 --port 8081`. Use the
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

Indra is a literal engine, but when it is unsure of a line it rescores the
candidates with a character language model, which can prefer a common spelling
to the printed one. In a document job it can be the first OCR (`local_ocr`),
with Tesseract or RapidOCR as `second_ocr`, a literal witness from another
engine.

The code is Apache-2.0. The model weights use a modified OpenRAIL-M licence:
free for research, personal use and organizations under $20M in funding or
revenue, and not for use that competes with Datalab's API. PageLedger never
ships them; read the licence before commercial use.

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


