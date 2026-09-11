# Page image input evidence

`ExtractionResult.input_evidence` is an optional dictionary, defaulting to
`None`. Successful extraction provenance includes the descriptor as a top-level
`input_evidence` field. Older results and checkpoints without the field remain
readable. See the [image evidence schema](../schemas/image-evidence.schema.json).

| Field | Meaning |
|---|---|
| `schema_version`, `kind` | Exactly `0.1`, `page_image` |
| `source_sha256`, `page_number` | Source document hash and one-based source page |
| `artifact` | Relative JPEG path below the child run's `evidence/` directory |
| `sha256`, `bytes` | Hash and byte count of the exact transmitted JPEG |
| `media_type`, `color_space` | Exactly `image/jpeg`, `RGB` |
| `width`, `height` | Encoded image dimensions, each 1–4096 pixels |
| `crop` | `{"kind":"full_page"}` or an exact crop described below |
| `renderer` | `command`, `version`, and a JSON `parameters` dictionary |
| `requested_model` | Explicit model identifier sent to the gateway |
| `model`, `provider` | Actual returned identifiers, or null when absent |
| `prompt_sha256` | Hash of the exact resolved user prompt, matching provenance `extractor.prompt_hash` |
| `system_prompt_sha256` | Optional hash of the separately transmitted system prompt |

All hashes are lowercase SHA-256. The descriptor accepts no other top-level
fields. A crop is `{"kind":"box","units":"pdf_points","x":0,"y":0,
"width":100,"height":200}`: x/y are nonnegative, width/height positive, and all
coordinates finite. They describe the actual source box transmitted, without
implicitly treating a crop as a full page. The optional example currently
renders full pages only. Crop origin and transformation details belong in the
renderer parameters when an adapter uses crops.

JPEGs must contain three 8-bit color components, have a supported baseline or
progressive frame and a scan header, and stay at or below 3 MiB. Core inspects
JPEG framing and dimensions using bounded standard-library reads; it does not
decode pixels. The renderer is responsible for producing valid RGB input.
Artifact traversal, absolute paths, nonregular files, and symlinks inside the
evidence path are rejected. A root path supplied to validation must itself be
a real directory.

Call `pageledger.image_evidence.validate_input_evidence(evidence, root=run_root,
source_sha256=source_hash, page_number=n, prompt_sha256=prompt_hash)` before
submitting a paid request. The same helper runs before a successful checkpoint
response is saved, when a retained response or partial failure is read, and
during final `verify-run`. It checks the bindings and retained JPEG hash,
byte count, and dimensions. Output content hashes and usage remain in ordinary
provenance; input evidence does not replace them. Completed checkpoint
provenance must agree with its saved input descriptor.

Image-evidence bundles and replay are currently unsupported. `pageledger bundle`
rejects them explicitly with `image_evidence_unsupported`; it never emits a
bundle silently omitting the JPEG. Bundle validation rejects transported image
descriptors too. Existing generation-zero text bundles and replay retain their
behavior.

## Optional OpenAI-compatible example

`examples/openai_image_adapter.py:OpenAIImageAdapter` is a thin optional
adapter. Install Poppler and Pillow separately; no provider SDK is required.
The controller supplies `evidence_dir` as the absolute child run root followed
by `/evidence`. The parent directory must exist by extraction time. Example
adapter options are:

```yaml
model: gemini-your-explicit-model
base_url: http://127.0.0.1:20128/v1
env_key: OMNIROUTE_API_KEY
evidence_dir: /absolute/path/to/child-run/evidence
max_output_tokens: 8192
timeout: 120
renderer: pdftoppm
dpi: 150
allowed_models: [gemini-your-explicit-model]
```

The model's final slash-separated component must begin with `gemini-`,
`gemini_`, `deepseek-`, or `deepseek_`. An optional `allowed_models` list further
restricts those families. The adapter performs one `GET /models` availability
check per successful adapter instance check, followed by one nonstreaming
`POST /chat/completions` per page; there is no request retry or provider
fallback. `max_output_tokens` maps to the compatible API's `max_tokens` field.
It records returned model/provider fields without inferring a provider from a
requested model name. A third-family returned model fails validation.

Authentication is read only from the named environment variable. Use
`env_key: null` only for a gateway configured without authentication. No vault,
credential file, or local auth scaffold is read. Constructors and page-count
hooks make no network requests. Redirects and environment proxies are disabled.

Rendering supports `pdftoppm` and `pdftocairo`, DPI 50–300, and a 4096-pixel
ceiling. Pillow converts to RGB and encodes the retained JPEG. The renderer
version, arguments, Pillow version, JPEG quality, subsampling, and optimization
settings are recorded. The complete serialized JSON request is capped at
4 MiB, including the base64 image and prompts. Responses are bounded at 8 MiB.
`system_prompt` is an optional separate string; the user prompt remains exactly
the string supplied by the runner (empty when null).

Timeouts, quota responses, HTTP/network errors, invalid/empty responses, and
truncation raise `AdapterFailure` with a safe diagnostic code. After an image
request, failures carry the available text, usage and image descriptor as a
partial result. Truncated text is retained verbatim and is never accepted as
a completed page. Missing Pillow or a renderer fails safely before a paid call.

The local tests use synthetic PDF pages and a loopback HTTP fixture. They
verify bytes, limits, response evidence, and failure control flow without
calling a live provider or establishing transcription accuracy.
