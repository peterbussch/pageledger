# Page image input evidence

For a reader, image input evidence identifies the exact page image sent to an
image model. PageLedger retains that JPEG in the job and verifies its hash and
source-page binding. Older runs without image evidence remain readable. See
the [image evidence schema](../schemas/image-evidence.schema.json).

| Field | Meaning |
|---|---|
| `schema_version`, `kind` | Exactly `0.1`, `page_image` |
| `source_sha256`, `page_number` | Hash of the file the image was rendered from, and one-based source page. For a PDF this is the source document's hash. For page images it is the hash of the run's carrier PDF (the manifest input's `carrier.sha256`), or of the page's own image when the adapter reads images itself. |
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
implicitly treating a crop as a full page. The box is in PDF points of the page
as rendered: its media box after `/Rotate`, with the origin at the top-left
corner, `x` to the right and `y` downward, which is how `pdftoppm -x -y -W -H`
counts. The renderer's own arguments stay in `renderer.parameters`.

## Page tiles

A page read in tiles ([Read dense pages in tiles](processing-spec.md#read-dense-pages-in-tiles))
has one descriptor of kind `page_tiles` for all its images:

| Field | Meaning |
|---|---|
| `schema_version`, `kind` | Exactly `0.1`, `page_tiles` |
| `source_sha256`, `page_number`, `requested_model`, `prompt_sha256`, `system_prompt_sha256` | As for `page_image`; every tile is sent with the same prompt |
| `model`, `provider` | The model and provider every tile that answered reported, or null when none did or they differ (a page whose tiles were served by different models fails) |
| `plan_sha256` | SHA-256 of the tile plan the page was read from (`pageledger.tiling.plan_sha256`) |
| `page_box` | `{"units": "pdf_points", "width", "height"}`: the page as rendered |
| `separator` | Exactly `"\n"`: what joins the tiles' texts into the reading |
| `tiles` | The tiles sent, in plan order (tiles never sent are not listed; the plan lists every tile) |

Each tile has the image fields of a `page_image` descriptor (`artifact`,
`sha256`, `bytes`, `media_type`, `color_space`, `width`, `height`, `crop` and
`renderer`, the crop being the box actually rendered), and:

| Field | Meaning |
|---|---|
| `tile_id` | The plan's id, `t01`, `t02`, ... |
| `outcome`, `failure_code` | `completed` with a null code, or `failed` with the adapter failure code (`MODEL_OUTPUT_TRUNCATED`, `MODEL_QUOTA`, ...) |
| `model`, `provider` | What the server reported for this tile, or null |
| `text_offset`, `text_length`, `text_sha256` | Where a completed tile's text lies in the reading (in characters) and the SHA-256 of its UTF-8; null for a failed tile |

A failed tile's text is not in the reading. The completed tiles' texts, in
order and joined by `separator`, are the reading exactly;
`pageledger.image_evidence.validate_tile_text(evidence, text)` checks this
when a run saves the result, when a recovery record is read, and in
`verify-run` against the raw artifact. `validate_input_evidence` checks every
tile image's bytes, hash and dimensions.

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
descriptors too. Original text runs (not reruns) can still be bundled and
replayed.

## Writing an image adapter

`ExtractionResult.input_evidence` is an optional dictionary, defaulting to
`None`. Successful extraction provenance includes it as a top-level field.
Older results and checkpoints without the field remain readable.
Call `pageledger.image_evidence.validate_input_evidence(evidence,
root=run_root, source_sha256=source_hash, page_number=n,
prompt_sha256=prompt_hash)` before submitting a paid request. The same check
runs before checkpointing and during verification. It checks the bindings and
retained JPEG hash, byte count, and dimensions.

The built-in [vision adapter](vision-adapter.md) writes this evidence in a
job's image and second-opinion stages. A custom image adapter receives
`evidence_dir` from the job: the absolute path of the child run's `evidence/`
directory, whose parent exists by extraction time. It keeps there the exact
JPEG it sends and describes that image in `input_evidence`.
