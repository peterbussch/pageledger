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
