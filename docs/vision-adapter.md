# Read pages with a vision model

The `vision` adapter sends each page of a PDF, rendered as a JPEG, to a vision
model behind an OpenAI-compatible chat-completions endpoint: a model on your
own machine or a hosted service. Besides Python it needs only Poppler's
`pdftoppm`, which `pdf_ocr` uses too.

A vision model writes its reading rather than reading it off the page. It can
invent, drop or modernize text, so PageLedger marks every `vision` attempt as
generative. A document job holds such a reading for review, as
`unconfirmed_model_output`, until a clean reading from another engine agrees
with it or a person confirms it. A plain `run` does not hold it: a reading
with no quality warning grades well and stays out of the review queue. To
queue every page of a run for a person, add `review: true` to its page type.

## Plug in a model

1. Start a server that offers the OpenAI chat-completions API, or choose a
   hosted one. [Where servers listen](#where-servers-listen) gives the address
   of common ones.
2. Save a config that names the endpoint and the model, for example as
   `vision.yml`:

   ```yaml
   schema_version: "0.1"
   run:
     adapter: vision
     adapter_options:
       base_url: http://127.0.0.1:8080/v1
       model: qwen3.5-9b
   taxonomy:
     page_types:
       prose:
         default_action: transcribe_text
   ```

   `base_url` is the endpoint's API root; the adapter posts to
   `{base_url}/chat/completions`. `model` is the name the server knows the
   model by.
3. Read one page, and look at what came back:

   ```bash
   pageledger run scan.pdf --config vision.yml --pages 1 --out runs/try
   cat runs/try/raw/doc_0001_page_0001.txt
   ```

   If the page fails, the error names a failure code, for example
   `AdapterFailure: MODEL_NETWORK_ERROR`, and
   [Limits and failures](#limits-and-failures) says what it means.
4. Read the document:
   `pageledger run scan.pdf --config vision.yml --out runs/scan`.

For measured results with particular models, see the
[engine recipes](engine-recipes.md).

## Where servers listen

| Server | `base_url` | `model` | Tested with PageLedger |
|---|---|---|---|
| `mlx_vlm.server --port 8080` (macOS) | `http://127.0.0.1:8080/v1` | the model it was started with | yes |
| llama.cpp `llama-server --port 8080` | `http://127.0.0.1:8080/v1` | the name `/v1/models` lists | no |
| Ollama | `http://localhost:11434/v1` | a vision model from `ollama list` | no |
| LM Studio | `http://localhost:1234/v1` | the loaded model's identifier | no |
| vLLM `vllm serve` | `http://127.0.0.1:8000/v1` | the served model's name | no |
| A gateway on this machine that forwards to a hosted model | the gateway's local address | as the gateway names it | yes |
| A hosted service | its `https://` API root | as the service names it | no |

Servers that follow the API list their model names at `{base_url}/models`,
for example `curl http://127.0.0.1:8080/v1/models`. The untested servers offer
the same API; PageLedger has not been run against them.

## A hosted model

```yaml
schema_version: "0.1"
run:
  adapter: vision
  adapter_options:
    base_url: https://gateway.example.org/v1
    model: gemini-3-flash
    env_key: GATEWAY_API_KEY
    allow_remote: true
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
```

Sending pages to another machine takes two deliberate settings: an `https://`
URL and `allow_remote: true`. Plain `http://` is accepted only for
`localhost`, `127.0.0.1` and `::1`. The key is read from the environment
variable that `env_key` names, and never written to any file, log or error. A
gateway on this machine needs neither setting, but if it forwards to a hosted
model, the page images still leave the machine. Use hosted models only for
pages you have the right to send.

## Change what the model is asked

Without a prompt, the adapter asks for an exact transcription that keeps the
original spelling and historical letters, line breaks, running heads and page
numbers. To ask for something else, give the page type a prompt:

```yaml
# fragment
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
      prompt: >-
        Transcribe the prose, headings and notes exactly as printed, keeping
        the original spelling. Replace each table with one line: [TABLE: its
        title as printed; what the rows and columns list]. Add no commentary.
```

Each page's provenance records the prompt's hash, so readings made with
different prompts stay distinguishable. In a document job, the stage's
`prompt` does the same.

## Models that reason before answering

A model that reasons before it answers spends `max_tokens` on the reasoning as
well as the reading. On a dense page it can run out before the reading ends,
and the page fails with `MODEL_OUTPUT_TRUNCATED`. Set `reasoning_effort: none`
or `low` if the server supports it, or raise `max_tokens`. In the evaluation,
a hosted Gemini Flash model that reasons by default ran out on 9 of the 14
pages it read at the default 8192 tokens; with `reasoning_effort: none` it
read all 19 pages.

## Local models: time, heat and loops

A local model keeps the machine's GPU busy for the whole run. On a Mac mini
with an M4 Pro, Qwen3.5-9B took a median of 38 seconds for each page it
finished (17 to 79 seconds), and the GPU ran at 90 °C and above through a
sustained run. Read one document at a time.

A local model can also loop, writing the same dots or table rules until it
reaches `max_tokens`. In the evaluation, 4 of 12 pages did, for several
minutes each, and failed with `MODEL_OUTPUT_TRUNCATED`. The finished readings
needed at most about 3,000 output tokens, so a lower `max_tokens`, such as
4096, ends a loop sooner; it can also cut short an unusually long page.

## Options

| Option | Default | Meaning |
|---|---|---|
| `base_url` | required | API root of an OpenAI-compatible endpoint. |
| `model` | required | Model to request. |
| `env_key` | none | Environment variable holding the API key; omit for servers without keys. |
| `allow_remote` | `false` | Must be `true` for any other machine, which must also use `https://`. |
| `max_tokens` | 8192 | Longest reading to request. |
| `timeout_seconds` | 300 | Longest wait to connect or for the next data, at most 600. |
| `max_image_side` | 2048 | Longest side of the page image in pixels, from 256 to 4096. |
| `reasoning_effort` | not sent | `none`, `minimal`, `low`, `medium` or `high`, sent as the request's `reasoning_effort` for models that reason before answering. |

## What it records

In a plain `run`, and in the local stages of a document job, each page's
`model` in provenance names the requested model, the model the server reported
when that differs, the renderer and the image size, for example
`qwen3.5-9b (served as Qwen3.5-9B-4bit); pdftoppm version 25.01.0; scale-to=2048`.

In a document job's image and second-opinion stages, the adapter also keeps
the exact JPEG it sent, described by an
[image evidence record](image-evidence-spec.md): the image's hash and size,
the renderer and its arguments, the requested model, the model and provider the
server reported, and the prompt's hash. The page's `model` is then the model
the server reported.

Token counts come from the response's `usage.total_tokens`. A cost is recorded
only when the endpoint reports `usage.cost_usd`, as some gateways do.

## Limits and failures

A request is at most 4 MiB, which leaves about 3 MiB for the page image. A page
that renders larger is rendered smaller and compressed harder, twice at most.
A response is at most 8 MiB. Redirects are refused and proxy settings from the
environment are ignored, so neither can carry a key elsewhere.

| What happened | Failure code |
|---|---|
| The model stopped at `max_tokens` | `MODEL_OUTPUT_TRUNCATED`; resumable runs and document jobs keep the partial text |
| The provider filtered the content | `MODEL_CONTENT_FILTERED` |
| The provider declined to recite a source | `MODEL_RECITATION` |
| The reading was empty | `MODEL_EMPTY_RESPONSE` |
| HTTP 402 or 429 | `MODEL_QUOTA` |
| Any other HTTP error, including a redirect | `MODEL_HTTP_ERROR` |
| No answer within `timeout_seconds` | `MODEL_TIMEOUT` |
| No connection | `MODEL_NETWORK_ERROR` |
| The response is not a chat completion, or is too large | `MODEL_INVALID_RESPONSE` |
| `env_key` names a variable that is not set | `MODEL_UNAVAILABLE` |
| The page could not be rendered small enough | `IMAGE_RENDER_ERROR` |

A missing `pdftoppm` stops the run before any page is read, with the
diagnostic `missing_binary`.
