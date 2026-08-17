# Page classifier

`pageledger classify` assigns a structural type to every source page and writes
an executable route map. It does not extract a corpus or create a run
directory. The command also writes a text-free evidence sidecar so each
decision can be inspected without retaining another copy of the page text.

The built-in taxonomy is deliberately small:

- `blank`
- `sparse`
- `prose`
- `table_likely`
- `unknown`

These are layout and text-shape labels, not subject labels. Use a classifier
hook for types such as `letter`, `invoice`, or `map`.

## Classify, review, then run

Use the same input paths and config for classification and execution:

```bash
pageledger classify scans/volume.pdf \
  --config pageledger.yml \
  --out route-map.yml

# Inspect or edit route-map.yml before spending on extraction.
pageledger run scans/volume.pdf \
  --config pageledger.yml \
  --routes route-map.yml \
  --out runs/volume

pageledger verify-run runs/volume
```

The first command writes `route-map.yml` and
`route-map.evidence.jsonl`. The map uses artifact `schema_version: "0.1"` and
is accepted by the same route-map loader used by `run --routes`. PageLedger
checks source coverage, page IDs, page counts, source hashes, taxonomy types,
confidence values, prompts, and adapter action support before extraction.

An empty or omitted taxonomy is allowed. In that case the classifier keeps its
structural types but maps every page to `review`. If
`taxonomy.page_types` is nonempty, it must contain every type that the
classifier can emit. For the built-in classifier, that means all five types
listed above.

## Probe source pages

With input paths, `classify` runs one extraction probe per page without writing
a run ledger.
Input files and directories are expanded in the same order as `pageledger run`,
and page counts use the selected probe adapter's `page_count` contract. The
probe adapter is chosen in this order:

1. `--adapter` on the command line
2. `classify.adapter` in the config
3. `pdf_text` for `.pdf` files, or `text` for other files

The probe must support `transcribe_text`. `classify.adapter_options` applies
when the adapter comes from the config; a command-line `--adapter` override
uses that adapter's defaults. `--adapter-path` can expose a custom probe adapter
or classifier-hook module.

Probe mode has no retries, run budget, cost ledger, or run directory. A probe
exception is contained to that page and emits `unknown` with null confidence,
action `review`, and reason `probe_failed:<ExceptionType>`.

## Reclassify retained run evidence

`--from-run` classifies the raw artifacts already retained by a completed run:

```bash
pageledger classify \
  --from-run runs/volume \
  --config pageledger.yml \
  --out route-map-from-run.yml
```

This mode reads the parent `manifest.json`, `route-map.yml`,
`provenance.jsonl`, `quality.jsonl`, and `raw/` artifacts. It preserves the
parent manifest's recorded source hash and page count in the new route map. A
changed or unavailable current source produces a warning because the decision
still comes from retained evidence.

Input paths and `--adapter` cannot be combined with `--from-run`. The parent
must be a full, non-dry-run generation-zero run; reruns and `--pages` partial
runs are rejected because they cannot supply complete route-map coverage. A
page with missing provenance or a missing raw artifact becomes
`unknown`/`review` with reason `no_parent_evidence`.

## Map types to actions

The classifier proposes a type. Config maps that type to an extraction action
and optional prompt:

```yaml
schema_version: "0.1"

taxonomy:
  page_types:
    blank:
      default_action: skip
    sparse:
      default_action: review
    prose:
      default_action: transcribe_text
      prompt: Preserve spelling and line breaks.
    table_likely:
      default_action: review
    unknown:
      default_action: review

classify:
  adapter: pdf_ocr
  adapter_options:
    dpi: 300
    lang: eng
  min_confidence: 0.5
  thresholds:
    table_column_line_ratio: 0.015

run:
  adapter: pdf_ocr
  adapter_options:
    dpi: 300
    lang: eng
```

For each page, an action or prompt returned by a custom hook takes precedence.
Otherwise PageLedger uses the matching taxonomy entry. An unmapped action
defaults to `review`. After mapping, a null confidence or a confidence below
`classify.min_confidence` also forces `review`; the default minimum is `0.5`.
The selected run adapter must support every non-`skip`, non-`review` action in
the route map.

## Structural signals

The sidecar records every signal, including fields retained for inspection but
not currently used by the built-in cascade.

| Signal | Meaning |
|---|---|
| `result_format` | Probe result format, such as `text`, `json`, `csv`, or `markdown_table`; null when unavailable. |
| `character_count` | All characters, including whitespace. |
| `visible_character_count` | Characters for which `str.isspace()` is false. The blank rule uses this count. |
| `word_count` | Count of Unicode letter tokens; currently the same token count as `alpha_token_count`. |
| `replacement_character_count` | Unicode replacement characters (`U+FFFD`). |
| `control_character_count` | Control characters other than newline, carriage return, tab, and form feed. |
| `suspicious_symbol_count` | Characters matched by the quality module's conservative symbol test. |
| `suspicious_symbol_ratio` | Suspicious symbols divided by `character_count`. |
| `alpha_token_count` | Unicode letter-token count, with combining marks kept with their base token. |
| `mean_token_length` | Mean Unicode letter-token length, or null when there are no letter tokens. |
| `max_token_length` | Maximum Unicode letter-token length, or `0` when there are no letter tokens. |
| `short_token_ratio` | Share of letter tokens whose length is at most two, or null when there are none. |
| `whitespace_character_ratio` | Share of all output characters for which `str.isspace()` is true. |
| `latin_letter_ratio` | Share of Unicode letters identified as Latin-script letters. |
| `prereform_letter_count` | Pre-1918 Russian letters retained as quality evidence. |
| `terminal_hard_sign_count` | Russian word-final hard signs retained as quality evidence. |
| `pipe_line_ratio` | Fraction of nonempty lines containing `|`. |
| `column_line_ratio` | Fraction of nonempty lines containing non-whitespace text separated by a run of at least two spaces. |
| `digit_ratio` | Unicode digits divided by visible characters. |
| `nonempty_line_count` | Lines containing at least one non-whitespace character. |
| `below_60_ratio` | Engine-reported share of words below confidence 60, or null when the probe does not report it. |

## Thresholds

Set overrides under `classify.thresholds`. Omitted values retain these defaults:

| Key | Default | Used by |
|---|---:|---|
| `blank_max_characters` | `2` | Maximum visible characters for the empty/blank branch. |
| `sparse_max_words` | `25` | Maximum letter-token count for `sparse`. |
| `table_pipe_line_ratio` | `0.3` | Minimum share of nonempty lines containing pipes. |
| `table_min_lines` | `3` | Minimum nonempty lines for either table-density branch. |
| `table_column_line_ratio` | `0.015` | Minimum share of nonempty lines containing a two-space column run. |
| `table_digit_ratio` | `0.25` | Minimum digit share paired with column evidence. |
| `fragmented_mean_token_length` | `3.0` | Exclusive upper bound for fragmented text. |
| `fragmented_min_alpha_tokens` | `20` | Minimum letter-token count for fragmented text. |
| `joined_mean_token_length` | `10.0` | Inclusive mean-token floor for joined-text evidence. |
| `joined_max_token_length` | `80` | Inclusive maximum-token floor for joined-text evidence. |
| `joined_min_alpha_tokens` | `20` | Minimum letter-token count for joined-text evidence. |
| `joined_max_whitespace_ratio` | `0.03` | Maximum whitespace share for joined-text evidence. |
| `joined_min_latin_letter_ratio` | `0.8` | Minimum Latin-letter share for joined-text evidence. |
| `low_confidence_below_60_ratio` | `0.25` | Engine-confidence tail that triggers the confidence penalty. |
| `low_confidence_penalty` | `0.2` | Amount subtracted from a non-null fixed confidence. |

The `table_column_line_ratio` default was lowered from `0.4` to `0.015` after
retained OCR from six known census-table spreads showed that visual columns
often survive on only a small share of lines. The accompanying
`table_digit_ratio` remains the guard against ordinary prose. This is a tuned
heuristic, not a general table-accuracy calibration.

Unknown threshold names are rejected. All values must be finite and
non-negative. Count thresholds must be integers; ratio and penalty thresholds
must be between zero and one.

## Built-in decision cascade

The first matching rule wins. The confidence values are fixed evidence scores,
not probabilities.

| Order | Condition | Type | Confidence | Reason |
|---:|---|---|---:|---|
| 1 | Visible characters are at most `blank_max_characters`, and the source is a PDF probed through an adapter with `embedded_text` capability | `unknown` | null | `empty_pdf_text_ambiguous` |
| 1 | The same character condition for any other probe | `blank` | `0.95` | `blank_text` |
| 2 | `result_format` is `json`, `csv`, or `markdown_table` | `table_likely` | `0.85` | `structured_payload:<format>` |
| 3 | Mean/max token length, token count, low whitespace, and Latin-letter share meet the joined-text thresholds | `unknown` | null | `joined_text` |
| 4 | Enough lines and pipe density meets its threshold | `table_likely` | `0.75` | `pipe_line_density` |
| 5 | Enough lines, column-run density, and digit density meet their thresholds | `table_likely` | `0.6` | `column_digit_density` |
| 6 | Mean token length is below its threshold and the minimum token count is met | `unknown` | null | `fragmented_text` |
| 7 | `word_count` is at most `sparse_max_words` | `sparse` | `0.6` | `sparse_text` |
| 8 | No earlier rule matched | `prose` | `0.7` | `prose_text` |

After the cascade, `below_60_ratio` at or above its threshold subtracts the
configured penalty from a non-null confidence, with a floor of zero. The
reason gains the suffix `+low_word_confidence`.

An empty result from `pdf_text` is not classified as blank. The adapter can see
only the embedded text layer, so an image-only page and a visually blank page
produce the same evidence.

## Classifier hooks

A hook replaces the built-in type decision while retaining PageLedger's page
ordering, route emission, evidence sidecar, taxonomy gate, and confidence
review gate. Configure it with an import string:

```yaml
classify:
  hook: my_project.classifier:DomainClassifier
  hook_options:
    collection: correspondence
```

The imported object may be a class, factory, or ready instance. Options are
passed only to a class or factory. A conforming hook has nonempty `name` and
`version` strings, a nonempty list or tuple of `page_types`, and a
`classify_page` method:

```python
from pathlib import Path
from typing import Any

from pageledger.classifier import ClassificationResult


class DomainClassifier:
    name = "correspondence-rules"
    version = "1.0"
    page_types = ("letter", "envelope", "unknown")
    model = None
    prompt_hash = None

    def __init__(self, collection: str) -> None:
        self.collection = collection

    def classify_page(
        self,
        *,
        page_id: str,
        page_number: int,
        source: Path,
        text: str,
        signals: dict[str, Any],
    ) -> ClassificationResult:
        proposed = signals["builtin_type"]
        if "Dear " in text:
            return ClassificationResult("letter", 0.9, "salutation_rule")
        return ClassificationResult("unknown", None, f"no_domain_rule:{proposed}")
```

`signals` contains all structural fields plus `builtin_type`, the built-in
classifier's proposed type. The hook must return `ClassificationResult` with a
declared type, confidence between zero and one or null, and a nonempty reason.
It may also return an action and prompt. Invalid output aborts classification.
Hook exceptions also abort and report the page ID; unlike a probe failure, a
hook failure is not converted to `unknown`.

A nonempty taxonomy used with a hook must cover every `hook.page_types` value
and `unknown`, since probe or retained-evidence failures can still emit
`unknown`. Route-map classifier metadata records the hook import spec plus its
optional `model` and `prompt_hash`; when `model` is absent it records
`<name>/<version>`.

## Evidence sidecar contract

For an output named `routes.yml`, the sidecar is
`routes.evidence.jsonl`. It has one JSON object per page and validates against
[`schemas/classify-evidence-line.schema.json`](../schemas/classify-evidence-line.schema.json).

| Field | Meaning |
|---|---|
| `schema_version` | Artifact schema version, currently `"0.1"`. |
| `page_id`, `page_number`, `source` | The page identity and absolute source path used by the route map. |
| `signals` | All structural and quality-derived values listed above. |
| `probe` | Probe adapter, adapter version, model, and result format; values may be null when evidence is missing. |
| `decision` | Final type, confidence, reason, mapped action, and prompt after the confidence gate. |

The sidecar does not retain raw text. A `--from-run` classification continues
to rely on the parent run's `raw/` artifacts; the sidecar alone cannot be used
to reconstruct or rerun classification.

## Limits

- The built-in classifier recognizes structure, not document meaning. It has no
  computer-vision model, language model, or domain taxonomy.
- Fixed confidences rank the rule outcomes; they are not calibrated accuracy or
  comparable to a custom hook's confidence scale.
- Pipe, spacing, and digit rules can miss tables whose extraction lost layout,
  and can flag digit-heavy prose. Review the route map and evidence before an
  expensive run.
- Probe mode does not enforce `run.budget`. A `pdf_ocr` probe still spends OCR
  time on every page.
- A custom probe and the later run adapter can disagree on page count. The
  complete-coverage check in `run --routes` fails rather than silently changing
  page IDs or dropping routes.
- `--from-run` needs a full-coverage generation-zero ledger. Missing retained
  evidence for an individual page becomes `unknown`/`review`; a dry run,
  rerun, or `--pages` partial parent is rejected because it cannot define the
  full source-page set.

See [`route-map-spec.md`](route-map-spec.md) for the YAML contract and
[`capabilities-and-limits.md`](capabilities-and-limits.md) for the wider runtime
scope.
