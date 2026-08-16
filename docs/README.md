# PageLedger documentation

PageLedger records OCR and document extraction runs one page at a time. Start
with the [CLI reference](cli.md) if you already have an input file, or follow
the [first OCR run guide](pdf-ocr-first-run.md) for a scanned PDF.

## Guides

- [Choose an OCR or VLM adapter](ocr-options.md)
- [Classify pages and review route evidence](classifier.md)
- [Run OCR on non-English and historical documents](multilingual-ocr.md)
- [Work through a scanned government archive](examples/jfk-scanned-archive.md)
- [Write a custom extraction adapter](adapter-protocol.md)
- [Compare PageLedger with document extraction tools](comparison.md)
- [Release PageLedger conservatively](releasing.md)

## Artifact reference

- [Run directory and artifact overview](artifacts.md)
- [Run manifest specification](run-manifest-spec.md)
- [Route map specification](route-map-spec.md)
- [Provenance and quality JSONL specification](provenance-spec.md)
- [Normalized page specification](normalized-spec.md)
- [Audit queue specification](audit-spec.md)
- [Rerun manifest specification](rerun-manifest-spec.md)

## Scope and design

- [Capabilities and limits](capabilities-and-limits.md)
- [Current design and future targets](design.md)

The JSON Schemas in [`schemas/`](../schemas/) are the machine-readable
artifact contract. The Markdown specifications explain the same fields for
people.
