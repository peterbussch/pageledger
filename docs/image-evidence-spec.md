# Page image input evidence

For a reader, image input evidence identifies the exact page image sent to an
image model. PageLedger retains that JPEG in the job and verifies its hash and
source-page binding. Older runs without image evidence remain readable. See
the [image evidence schema](../schemas/image-evidence.schema.json).

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

The authoring adapter must retain input evidence for an image request. PageLedger
checks its source, page, prompt, and JPEG bindings when saving and verifying
results. Output hashes and usage remain separate provenance records.

Image-evidence bundles and replay are currently unsupported. `pageledger bundle`
rejects them explicitly with `image_evidence_unsupported`; it never emits a
bundle silently omitting the JPEG. Bundle validation rejects transported image
descriptors too. Existing generation-zero text bundles and replay retain their
behavior.

## Optional OpenAI-compatible example

### For adapter authors

`ExtractionResult.input_evidence` is an optional dictionary, defaulting to
`None`. Successful extraction provenance includes it as a top-level field.
Older results and checkpoints without the field remain readable.
Call `pageledger.image_evidence.validate_input_evidence(evidence,
root=run_root, source_sha256=source_hash, page_number=n,
prompt_sha256=prompt_hash)` before submitting a paid request. The same check
runs before checkpointing and during verification. It checks the bindings and
retained JPEG hash, byte count, and dimensions.

`examples/openai_image_adapter.py:OpenAIImageAdapter` is a thin optional
adapter. Install Poppler and Pillow separately; no provider SDK is required.
The controller supplies `evidence_dir` as the absolute child run root followed
by `/evidence`. The parent directory must exist by extraction time. Example
adapter options are:

```yaml
model: gemini-your-explicit-model
base_url: https://your-openai-compatible-endpoint/v1
env_key: YOUR_GATEWAY_API_KEY
evidence_dir: /absolute/path/to/child-run/evidence
max_output_tokens: 8192
timeout: 120
renderer: pdftoppm
dpi: 150
allowed_models: [gemini-your-explicit-model]
max_image_bytes: 3145728
max_image_dimension: 4096
```

The example accepts Gemini and DeepSeek model families only. This is a deliberate
allow-list in the adapter, not a claim that every OpenAI-compatible endpoint
supports those models. The model's final slash-separated component must begin with `gemini-`,
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
`pageledger doctor` checks the standard provider key names; it does not check
`YOUR_GATEWAY_API_KEY` or other custom environment-variable names. Check that
the configured variable is set in the environment that runs the job.

Rendering supports `pdftoppm` and `pdftocairo`, DPI 50–300, and a 4096-pixel
ceiling. Pillow converts to RGB and encodes the retained JPEG. The renderer
version, arguments, Pillow version, JPEG quality, subsampling, and optimization
settings are recorded. The complete serialized JSON request is capped at
4 MiB, including the base64 image and prompts. Responses are bounded at 8 MiB.
`max_image_bytes` can lower the JPEG ceiling from its default of 3 MiB.
For a gateway that accepts at most 1 MiB per image, set it to `1048576`.
The adapter tries JPEG quality levels 90, 80, 70, then 60. It stops before
submission if none fits. Compression can reduce legibility, so review the
retained image as well as the returned text. The chosen quality and byte limit
are recorded in the image evidence.
`max_image_dimension` sets the longest image edge in pixels, from 1 to 4096.
Lower it explicitly if compression alone cannot meet the gateway's byte limit.
The chosen size is recorded as `scale_to`, alongside the actual dimensions.
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
