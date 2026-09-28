# Plan time and cost

Estimate from a representative sample before processing the collection. Measure
wall time for each stage and adapter you expect to use. Divide elapsed stage
time by the number of pages attempted in that stage; multiply by the expected
page count for a rough estimate. This assumes pages resemble the sample and
runs serially. Difficult pages, retries, hardware contention and setup time can
change the result.

## Measured examples

These figures come from different collections and machines. They are reference
points, not a forecast for your hardware:

| Work | Measured time | What was measured |
|---|---:|---|
| Tesseract `pdf_ocr` on a scanned JFK archive | 1.25 seconds/page | A 107-page rerun took 134 seconds at 300 DPI. |
| Tesseract in a 60-page trial | 1.5–19 seconds/page | Variation across pages and trial configurations; estimate from your own sample. |
| Tesseract plus local-LLM cleanup | About 70 seconds/page | A local second stage; the engine and prompt affect timing. |
| Hosted cloud vision model | About 11 seconds/page | Network and provider conditions vary; charges depend on provider pricing and usage. |
| RapidOCR, PP-OCRv5 Cyrillic | About 3 seconds/page | CPU, M4 Pro evaluation; 24-page transcription comparison. |
| Surya OCR 2, GGUF | About 2 minutes/page | Shared GPU evaluation; 24-page transcription comparison. |
| Qwen3.5-9B, 4-bit | About 90 seconds/page | M4 Pro, 8.3 GB model; local inference. |
| dots.ocr, 4-bit | About 120 seconds/page | M4 Pro, 6.1 GB model; local inference. |
| PaddleOCR-VL-1.6, 4-bit | About 15 seconds/page | M4 Pro, tiled native prompt; local inference. |

The benchmark engines and hardware are not interchangeable. The local model
figures include inference time as measured in the evaluation, not setup or model
loading unless stated. Evaluation transcription scores are agreement with a
reference, not accuracy guarantees. See the [JFK example](examples/jfk-scanned-archive.md)
for context.

## What a dry run tells you

`run --dry-run` writes planning artifacts without calling extraction
adapters; `process` has no dry run. It can help inspect the plan, but it does not run OCR, measure your
adapter speed, predict elapsed time, project provider cost, or reserve provider
capacity. It cannot tell you whether the selected pages will need another stage.

Run a representative sample first. Record elapsed time, attempted pages per
stage, model or engine, render settings, and any provider-reported usage or
charge. Estimate each stage separately. Do not extrapolate a small clean sample
as if it represents a volume with damaged, faint, tabular or historical pages.

## Set spending and work limits

For document jobs, `processing.limits.max_attempt_pages` bounds attempted pages
across stages. Set it from the number of pages and expected passes. A 300-page
book with text extraction followed by OCR on every page can require 600 page
attempts. Reaching the limit pauses the job; completed attempts remain available
and `resume --raise-limit max_attempt_pages=N` continues with a higher cap.

`max_image_pages` bounds image and second-opinion attempts. `max_tokens` and
`max_cost_usd` use reported usage and charges. Unknown paid usage stops further
image work when the corresponding limit is set. A request already in progress
can cross a token or cost threshold, and the first reported charge can be
unknown. These settings are not hard provider billing ceilings. Use the
provider's own spending controls where a hard cap is required. Local processing
has no invented dollar cost. See [processing limits](processing-spec.md#one-budget-for-the-job)
for enforcement and recovery details.
