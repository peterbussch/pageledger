# Fetch page images from a IIIF server

IIIF (International Image Interoperability Framework) is the way many
libraries and archives publish digitized items: a *manifest* lists the pages
(called *canvases*) of an item, and an *image service* serves each page's
image at the sizes you ask for. What a server delivers can change over time as
images are re-scanned, re-encoded or moved, so a transcription can only be
checked and cited against the exact bytes it was made from; fetching the
images once and keeping them, with a record of where each came from, gives you
those bytes.

`pageledger fetch` makes that copy. It writes a *snapshot* folder: one
JPEG per canvas, the manifest and image information as the server sent them,
and a fetch record (`iiif-fetch.json`) that binds each file's SHA-256 hash to
the URLs it came from.

## Fetch an item

```bash
pageledger fetch https://example.org/iiif/item/manifest.json --out sources/item
```

The URL can be a Presentation API 2 or 3 manifest, an Image API 2 or 3
`info.json`, or an image service's base URL (PageLedger then reads its
`info.json`). Use `--pages` to fetch only some canvases, numbered by their
position in the manifest:

```bash
pageledger fetch https://example.org/iiif/item/manifest.json --pages "1-8,81" --out sources/item
```

The snapshot looks like this:

```text
sources/item/
  0001.jpg ... 0081.jpg      page images, named by canvas position
  iiif/manifest.json         the manifest exactly as fetched
  iiif/0001.info.json ...    each image's information document
  iiif-fetch.json            the fetch record
```

File names come from the canvas position, never from labels or identifiers
in the manifest; labels and identifiers are kept as data in the record. With
`--pages`, the names keep the manifest position, so `0081.jpg` is canvas 81
even when it is the ninth image in the folder.

## Be polite to the server

PageLedger sends one request at a time and waits `--delay` seconds (1 by
default) between requests. When a server answers 429, 502, 503 or 504, it
waits as long as the server's `Retry-After` header asks, up to 60 seconds, or
else 2, 4, 8, 16 and 32 seconds, and gives up after five retries. A server
that keeps refusing stops the fetch, so it does not go on to ask for the
remaining pages. Requests identify themselves as `PageLedger/VERSION (IIIF
fetch)` and carry nothing about you.

## Resume an incomplete fetch

A fetch that stops early (a busy server, a lost connection, or Ctrl-C) leaves
a record with status `incomplete` that lists each missing canvas and why.
Run the same command with `--resume`:

```bash
pageledger fetch https://example.org/iiif/item/manifest.json --out sources/item --resume
```

Resuming first checks every image already fetched against its recorded hash,
then fetches only the missing canvases, using the manifest stored in the
snapshot rather than fetching it again. A file that has changed or been
deleted since it was fetched is an error, not something PageLedger quietly
fetches again: restore it or start a new snapshot. A page image the record
does not list (which can happen if a fetch was killed while writing it) is
also an error; delete that file and resume.

## Process the snapshot

```bash
pageledger fetch https://example.org/iiif/item/manifest.json --pages "1-8,81" --out sources/item
pageledger process sources/item --out jobs/item
```

A snapshot is a [folder of page images](pdf-ocr-first-run.md#9-a-folder-of-page-scans),
and `process` (or `run`) reads it as one: the images are the source and their
hashes its identity, while the fetch record is checked against them and kept
in the job as context, with each page's canvas, so a page can be cited by its
IIIF canvas. Pages are numbered by their place in the folder, so here
`0081.jpg` is page 9 of the job; the report, the transcript and the review view
show both, as `9 (0081.jpg)`. A folder whose images no longer match its record,
or that holds anything else, is refused with what is wrong. An incomplete
snapshot can be processed; the report says it is incomplete, and `verify`
warns about it and, after a later `--resume` adds images, names them (see
[durable job and attempts](processing-spec.md#durable-job-and-attempts)).

## Image size

PageLedger asks for each whole image at its native size, the width and height
in its `info.json`, and never above it: an enlarged image is only a blurred
copy of a smaller one. Some servers allow only smaller images (`maxWidth`,
`maxHeight` or `maxArea` in `info.json`). PageLedger then fetches the largest
image the server allows, marks it `below_native` in the record and prints an
`iiif_below_native` warning, because a reading made from it rests on fewer
pixels than the archive holds.

## Limits

- No authentication: items behind a IIIF login or access-control service
  cannot be fetched.
- JPEG only. A response that is not a complete JPEG is refused.
- Whole images only. PageLedger does not request regions of a page.
- Canvases without an image service (a static image file only) and canvases
  that offer a choice of images are refused, naming the canvas. So are IIIF
  collections and Image API 1 services.
- Institutional OCR (ALTO) linked from a manifest is not fetched. If you have
  the library's ALTO files, read them with the `alto` engine and
  `name: "{stem}.xml"`, which pairs each file with the image of the same name:
  the images keep their canvas numbers, so after `--pages` or a missing canvas
  page 9 of the job can be `0081.jpg`, and `{page}` is refused there (see
  [engine recipes](engine-recipes.md)).
- You choose the URL; the manifest chooses the image services and a server
  chooses its redirects. So only the host of the URL you named may be on your
  own machine or network: any other host a manifest, service or redirect leads
  to must be a public internet address, or that canvas fails with the address it
  refused. To fetch from a IIIF server on your own machine or network, name a
  manifest on the same host as its images (`http://localhost:8080/...` with
  services on `localhost:8182` is fine). Under an `https` URL nothing is
  fetched over plain `http`, so every canvas of an https manifest that lists
  `http://` image services fails; name the manifest's `http://` URL if you
  accept fetching over plain http. PageLedger checks the addresses a name resolves to
  just before asking, but cannot stop a name that resolves differently a moment
  later; fetch only manifests you have reason to trust.
- It follows at most three redirects and never one from https to http. It
  sends no cookies and uses your system's proxy settings, as a browser would.
- A URL with a space or a control character in it, or a non-ASCII character in its path, is
  refused, naming the canvas; a manifest PageLedger could not store again in its
  record (for example one holding `NaN`) is refused before anything is written.
- Keep the snapshot. The record vouches for the files only while they are
  there and unchanged, and anything made from the images can be checked
  against them only as long as you keep them.

The fetch record's fields are described in the
[fetch record specification](iiif-fetch-spec.md). For every option and exit
code, see [`fetch`](cli.md#fetch).
