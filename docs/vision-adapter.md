# Read pages with a vision model

The `vision` adapter sends each page of a PDF, rendered as a JPEG, to a vision
model behind an OpenAI-compatible chat-completions endpoint: a model running on
your own machine, for example under llama.cpp's `llama-server` or
`mlx_vlm.server`, or a hosted service. Besides Python it needs only Poppler's
`pdftoppm`, which `pdf_ocr` uses too.

A vision model generates its reading. It can invent, drop or modernize text,
so PageLedger marks every `vision` attempt as generative and keeps its pages
in review until a person or a second engine confirms them.

## A model on this machine

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
`{base_url}/chat/completions`. `model` is the name the server knows the model
by.

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
variable that `env_key` names, and never written to any file, log or error.
Use hosted models only for pages you have the right to send.

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
| `reasoning_effort` | none sent | `none`, `minimal`, `low`, `medium` or `high`, sent as the request's `reasoning_effort` for models that reason before answering. |

The prompt comes from the page type or the processing stage. Without one, the
adapter asks for an exact transcription that keeps the original spelling and
historical letters, line breaks, running heads and page numbers.

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
| The model stopped at `max_tokens` | `MODEL_OUTPUT_TRUNCATED`; the partial text is kept |
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

A model that reasons before it answers spends `max_tokens` on the reasoning as
well as the reading. On a dense page it can run out before the reading ends,
and the page fails with `MODEL_OUTPUT_TRUNCATED`. Set `reasoning_effort: none`
or `low` if the server supports it, or raise `max_tokens`. In the evaluation,
a hosted Gemini Flash model that reasons by default ran out on 9 of the 14
pages it read at the default 8192 tokens.
