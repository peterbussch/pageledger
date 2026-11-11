# IIIF fetch record

`pageledger fetch` writes `iiif-fetch.json` at the top of a snapshot
folder. It binds each fetched page image to its hash and to the URLs it came
from. See the [how-to](iiif.md) and the
[fetch record schema](../schemas/iiif-fetch.schema.json).

| Field | Meaning |
|---|---|
| `schema_version`, `kind` | Exactly `0.1`, `iiif_fetch` |
| `tool` | `name` (`pageledger`), `version`, and the `user_agent` sent with every request |
| `requested_url` | The URL given on the command line |
| `source_kind` | `manifest`, or `image_service` for an `info.json` or image service URL |
| `manifest` | Null for an image service; otherwise `url`, `final_url` after redirects, `sha256` of the bytes as fetched, `artifact` (`iiif/manifest.json`) and `api_version` (Presentation API `2` or `3`) |
| `canvas_count` | Canvases in the manifest (1 for an image service) |
| `selection` | The `--pages` expression, or null for every canvas |
| `status` | `complete` when every selected canvas was fetched, otherwise `incomplete` |
| `started_at`, `finished_at` | UTC times; `finished_at` is null while a fetch is running or if it was killed |
| `stopped_by` | Optional: the error that stopped the fetch early, such as a server that stayed busy |
| `images` | One entry per fetched image, ordered by canvas |
| `missing` | One entry per selected canvas not fetched, ordered by canvas |

An `images` entry:

| Field | Meaning |
|---|---|
| `file` | Top-level file name, the zero-padded canvas position (`0007.jpg`); at least four digits |
| `sha256`, `bytes` | Hash and size of the JPEG as received |
| `canvas_index` | One-based position of the canvas in the manifest |
| `canvas_id`, `label` | The canvas identifier and label as the manifest gives them (any JSON value for the label), or null |
| `service_id` | The image service base URL |
| `image_api` | Image API version of the service, `2` or `3` |
| `info` | The image information document: `url`, `final_url`, `artifact` (`iiif/0007.info.json`) and `sha256` |
| `native_width`, `native_height` | Size declared in `info.json` |
| `request_url`, `final_url` | The image URL asked for, and the URL that answered after redirects |
| `width`, `height` | Size read from the JPEG header |
| `below_native` | True when the image is narrower or shorter than its native size, because the server limits sizes |
| `fetched_at` | UTC time the image was received |
| `attempts` | Requests made for the image, counting retries |

A `missing` entry has `canvas_index`, `canvas_id`, `label`, the `file` it
would have, and `error`: the reason it failed, or null when it was not tried
before the fetch stopped.

## Rules

- Hashes are lowercase SHA-256 of the exact bytes received. Times are UTC
  ISO 8601 with a `Z` suffix.
- Requests use `full/max/0/default.jpg` (Image API 3) or
  `full/full/0/default.jpg` (Image API 2). When a version 2 service limits the
  size, the request names an explicit width that fits the limits
  (`full/W,/0/default.jpg`), or at compliance level 0 the largest listed size
  that fits. Image API 3 `max` already honours the limits.
  No request asks for more than the native size, a percentage, or upscaling.
- An image is accepted only when the server says `image/jpeg`, the bytes start
  with a JPEG start-of-image marker, the header has a baseline or progressive
  frame with one or three 8-bit components, the bytes end with an end-of-image
  marker, and the image is no larger than its native size. Images larger than
  `--max-image-bytes` are refused while they stream in. The manifest is
  limited to 64 MiB and each `info.json` to 1 MiB.
- Every write replaces its file atomically. The record is written before the
  first image and after each one, so an interrupted fetch leaves a record that
  matches the files on disk, except for an image written in the instant before
  its record entry. `--resume` refuses such an unlisted image.
- The record is not rewritten when `--resume` finds the snapshot complete, so
  its hash stays stable.
- `iiif_below_native` is reported by the command (in its output and `--json`
  summary) for each `below_native` image. It is not a page quality warning.
