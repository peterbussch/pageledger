# PageLedger 0.4.0 verified replay design

Status: historical approved design; implemented for 0.4.0

> This specification is retained as design history, not current operating
> guidance. See [the current documentation index](../../README.md).

Date: 2026-08-17

Target release: 0.4.0

## Summary

PageLedger 0.4.0 adds a verified replay envelope for moving an extraction run to another compatible machine and executing the recorded plan again. It adds exactly two commands:

```bash
pageledger bundle RUN_DIR --out BUNDLE_DIR
pageledger replay BUNDLE_DIR --out NEW_RUN_DIR
```

The bundle contains the verified baseline ledger, source bytes, a portable route map, a file inventory, and the material-runtime evidence needed to decide whether exact replay is admissible. Replay uses the existing run controller, verifies the resulting ledger, and compares it with the baseline.

The product claim is deliberately narrower than hermetic reproducibility:

- A deterministic adapter is eligible for `exact` only when its material-runtime profile matches before extraction and every replayed raw artifact matches byte-for-byte afterward.
- A nondeterministic or cloud adapter is eligible for `evidence_compared`: PageLedger repeats the recorded invocation and records differences without claiming identical output.
- A missing or incompatible deterministic environment fails before extraction. A raw mismatch after a matched deterministic preflight is a replay failure.

PageLedger does not package or install adapters, Python environments, OCR binaries, model weights, provider SDKs, or credentials. Those remain target-machine prerequisites. This preserves PageLedger's role as a dependency-light ledger around extraction rather than turning it into an environment manager.

## Goals

0.4.0 must let a user:

1. Turn an eligible verified generation-zero run into an inspectable directory bundle containing all source bytes.
2. Copy that directory to a different filesystem root where none of the original absolute paths exist.
3. Replay the recorded config, source selection, page identities, and route decisions without reclassification or adapter substitution.
4. Fail before extraction when the bundle is corrupt, unsafe, secret-bearing, or incompatible with a deterministic local runtime.
5. Prove exact raw-output reproduction for eligible deterministic adapters.
6. Produce a durable evidence comparison for nondeterministic and cloud adapters.
7. Preserve compatibility with existing schema-0.1 run readers.

## Non-goals

0.4.0 does not add:

- Hermetic environment or cross-platform reproduction.
- Adapter, wheel, dependency, container, OCR binary, provider SDK, or model packaging.
- Credential capture, credential discovery, value-pattern secret scanning, or redaction.
- Automatic execution of code carried inside a bundle.
- Cryptographic signatures or claims about bundle authorship.
- A PageLedger-owned archive format. Ordinary tools may archive a bundle directory for transport.
- Portable replay of a PageLedger rerun generation (`run_depth > 0`) or an entire rerun lineage.
- A thin bundle that omits source bytes.
- Automatic reclassification, output merging, OCR correction, or accuracy claims.
- Region-level routing, provider registries, a service, database, or web UI.
- Separate `extract` or `audit` commands.

## Product terminology and guarantee

Documentation and CLI output call this feature **verified replay**, not “full,” “hermetic,” or “identical-environment” reproduction.

A **compatible environment** has the recorded PageLedger version, trusted adapter code, effective adapter identity, non-secret options, and material-runtime profile required by the baseline.

Replay outcomes are:

| Outcome | Meaning | Exit code |
|---|---|---:|
| `exact` | Deterministic adapter, matching profile, verified replay run, and every baseline/replay raw SHA-256 matches. | 0 |
| `evidence_compared` | Nondeterministic or cloud adapter completed; the verified replay and its differences are recorded without an identity claim. | 0 |
| `incompatible_environment` | Deterministic preflight could not establish a matching material-runtime profile. Extraction did not start. | 1 |
| `deterministic_mismatch` | Deterministic preflight matched, but one or more replayed raw artifacts differed or were missing. The replay run remains available for inspection. | 1 |

Bundle validation, extraction, and verification failures use structured error codes and exit 1. Argparse usage errors remain exit 2.

## Command surface

### `pageledger bundle`

```bash
pageledger bundle RUN_DIR --out BUNDLE_DIR [--json]
```

Rules:

- `BUNDLE_DIR` must not exist.
- `RUN_DIR` must pass `verify-run`.
- The baseline must be an execute-mode, generation-zero run.
- Failed runs, dry runs, and runs with `pages_failed` or `pages_not_attempted` are rejected.
- Completed runs and partial runs caused only by explicit review routes are eligible.
- Every manifest input must still exist and match its recorded SHA-256.
- A deterministic baseline must contain an admissible reproducibility profile. Pre-0.4 deterministic runs therefore remain readable but are not accepted by `bundle`.
- A nondeterministic/cloud baseline may omit the exact-tier profile and remains eligible for `evidence_compared`.
- Bundle creation writes through a temporary sibling directory, verifies every copied file, and renames only after the bundle is complete.

### `pageledger replay`

```bash
pageledger replay BUNDLE_DIR --out NEW_RUN_DIR \
  [--adapter-path TRUSTED_LOCAL_DIR] [--json]
```

Rules:

- `NEW_RUN_DIR` must not exist.
- The bundle is authoritative. Replay accepts no `--config`, `--routes`, `--pages`, adapter override, or reclassification option.
- `--adapter-path` only makes locally trusted adapter code importable. PageLedger never imports Python from inside the bundle.
- Preflight completes before the output directory is created.
- Replay calls the existing run controller, then `verify-run`, then the existing comparison engine extended with raw-output equality.

## Bundle contract

The bundle is a directory of plain files:

```text
bundle/
├── bundle.json
├── replay-route-map.yml
├── sources/
│   ├── source-0001.pdf
│   └── source-0002.txt
└── baseline/
    ├── manifest.json
    ├── config-snapshot.yml
    ├── route-map.yml
    ├── raw/
    ├── normalized/
    ├── audit.json
    ├── audit.md
    ├── provenance.jsonl
    ├── quality.jsonl
    ├── cost.json
    ├── run.log
    ├── rerun-manifest.yml
    └── align-schema-snapshot.yml  # only when declared by alignment evidence
```

`baseline/` contains only canonical PageLedger artifacts declared by the manifest plus `manifest.json` and any contained alignment-schema snapshot required by verification. Files are copied byte-for-byte. Undeclared files from the original run directory are not transported.

`sources/` contains one regular-file copy per manifest input, in manifest order. Names use the stable index plus the original lowercase suffix; original basenames are not executable identifiers.

`replay-route-map.yml` preserves page IDs, page numbers, types, actions, reasons, confidence, prompts, and source hashes. Only `documents[].source` changes, becoming a relative `sources/source-NNNN.ext` reference. Original absolute paths remain inside unchanged baseline provenance and are never dereferenced during replay.

`bundle.json` uses an independently versioned bundle schema and contains:

- `bundle_schema_version`.
- Baseline `run_id`, manifest path, manifest SHA-256, execution mode, and run depth.
- Replay config and route-map paths.
- Ordered source mappings: manifest index, bundle path, SHA-256, size, page count, and optional recorded page selection.
- Baseline extractor identity and reproducibility profile.
- A sorted file inventory containing relative path, size, and SHA-256 for every transported file except `bundle.json` itself.

Excluding `bundle.json` from its own inventory avoids a self-hash cycle. Replay records the SHA-256 of the exact `bundle.json` bytes.

## Reproducibility profile

### Adapter protocol

Adapters may implement one optional method:

```python
def reproducibility_profile(self) -> dict[str, object]:
    """Return non-secret, JSON-compatible material-runtime identity."""
```

Absence does not prevent ordinary `run`; it prevents a deterministic adapter from being bundled for exact replay.

The adapter-supplied mapping contains a `materials` list. Each material has:

- `kind`: `binary`, `package`, `model`, or `asset`.
- `name`: stable material name.
- `version`: exact version/revision string.
- `sha256`: lowercase SHA-256 of the material or a deterministic aggregate of its files.

Paths, credentials, environment values, and mutable aliases are forbidden in the stored profile. PageLedger validates the mapping, hash shapes, JSON finiteness, stable sorting, and absence of credential-shaped keys.

### PageLedger-owned envelope

During the original run, PageLedger calls the hook once and adds:

- `profile_version`.
- PageLedger package version and deterministic package-code aggregate SHA-256.
- Adapter module-code SHA-256. If PageLedger cannot locate regular source/module bytes, the profile is not exact-eligible.
- Python implementation and exact version.
- Platform system, release, and machine architecture.
- Preferred encoding and filesystem encoding.
- The adapter-supplied sorted material list.
- A canonical profile SHA-256 computed from sorted, compact JSON.

The resulting optional `reproducibility_profile` is stored in each relevant manifest extractor entry. It is not duplicated into every provenance line.

Built-in providers are:

- `text`: PageLedger/adapter/Python/platform identity; no external material.
- `pdf_text`: the above plus deterministic aggregate identity for the installed `pypdf` distribution.
- `pdf_ocr`: the above plus Tesseract and Poppler executable identities, requested Tesseract trained-data hashes, and the effective DPI/language settings already present in adapter options/model evidence. If any required material cannot be located and hashed, the run is not exact-eligible.

An adapter whose capabilities include `cloud` is never exact-eligible in 0.4.0, regardless of its `deterministic` flag or profile.

At replay preflight, PageLedger loads only the locally trusted adapter, computes its current profile, and compares the canonical profile SHA-256 with the baseline. A mismatch produces `incompatible_environment` before extraction.

## Bundle trust and secret boundary

Bundle data is untrusted input.

Bundle creation and replay accept only contained regular files. They reject:

- Absolute executable paths and any path component equal to `..`.
- Symlinks, hard-linked files, sockets, devices, FIFOs, and other special files.
- Duplicate inventory paths or source mappings.
- Missing, extra, size-mismatched, or hash-mismatched inventory files.
- Any attempt to use baseline or rerun absolute paths as replay sources.
- An `--adapter-path` that resolves inside the bundle root.

No authenticity claim is made. SHA-256 proves agreement with `bundle.json`, not who created the bundle or whether its source material is safe or lawful.

### Narrow credential-key rejection

PageLedger does not attempt general secret detection. At bundle creation it recursively inspects only mappings beneath `adapter_options` or `hook_options` in `config-snapshot.yml`, plus persisted manifest extractor options. Budget, pricing, schema, citation, source, raw, log, and other arbitrary text fields are not scanned. Keys are lowercased and normalized by removing non-alphanumeric characters. A normalized key exactly matching one of these names rejects the bundle:

- `apikey`
- `apitoken`
- `token`
- `accesstoken`
- `authtoken`
- `bearertoken`
- `refreshtoken`
- `clientsecret`
- `secretkey`
- `password`
- `credential`
- `credentials`
- `authorization`
- `privatekey`
- `accesskey`

There is no override and no redaction mode. Values, source documents, raw outputs, citations, and logs are not scanned. Documentation states that the structural check cannot prove a bundle contains no sensitive data. Running `bundle` is an explicit request to copy the source documents and canonical ledger evidence.

## Replay algorithm

1. Parse `bundle.json` and validate it against the bundle schema.
2. Resolve every declared path against the bundle root; reject escapes and non-regular files.
3. Verify the sorted inventory, rejecting missing and undeclared transported files.
4. Run `verify-run` against `baseline/`. Missing original external sources remain non-executable historical evidence; bundle source copies are checked strictly.
5. Validate the ordered source mapping against the baseline manifest and portable route map.
6. Load the bundled config snapshot without permitting CLI overrides.
7. Load the locally trusted adapter and compare effective identity.
8. For deterministic adapters, compute and match the reproducibility profile before extraction. For nondeterministic/cloud adapters, require complete ordinary extractor identity but do not claim exactness.
9. Invoke the existing `run()` implementation. A routed baseline supplies `replay-route-map.yml`; an unrouted single-input baseline with a recorded `pages` expression supplies that expression; an ordinary unrouted baseline supplies only the sources and config. The replay path must not call `rerun()` or consume `rerun-manifest.yml`.
10. If the baseline records an applied external alignment schema, apply the contained alignment-schema snapshot to the replay using the existing alignment implementation.
11. Verify the replay run.
12. Compare baseline and replay, including raw SHA-256 equality.
13. Write `replay.json` atomically, then add optional replay linkage to `manifest.json` as the last commit step.

If replay finalization crashes before the manifest update, the extraction output remains an ordinary verifiable run without a completed replay claim.

## Comparison and replay evidence

`compare-runs` gains additive raw-evidence fields for each common extracted page:

- `raw_sha256_a` and `raw_sha256_b`.
- `raw_equal`.

It also gains top-level counts for equal, different, and missing raw evidence. Ordinary `compare-runs` continues to produce a report even when raw outputs differ.

`replay.json` has its own schema and contains:

- Replay schema version.
- Bundle manifest SHA-256.
- Baseline and replay run IDs.
- Baseline and local effective extractor identities.
- Reproducibility-profile match status.
- Replay outcome.
- Raw equal/different/missing counts and affected page IDs.
- The existing evidence comparison result or a contained reference to its data.

After `replay.json` exists, the replay manifest receives:

- Optional `artifacts.replay: "replay.json"`.
- Optional top-level `replay` metadata with baseline run ID, bundle manifest SHA-256, outcome, and replay schema version.

`verify-run` validates the replay artifact and its agreement with manifest linkage whenever declared. These fields are additive; ordinary runs do not declare them.

## Compatibility

The existing run artifact schema remains `0.1`.

- `reproducibility_profile`, `artifacts.replay`, and top-level replay linkage are optional additive manifest fields.
- Existing readers and commands continue to accept older schema-0.1 runs.
- Existing deterministic runs without profiles remain inspectable, verifiable, alignable, rerunnable, and comparable, but 0.4 `bundle` rejects them because their original environment cannot be established retroactively.
- New `bundle.json` and `replay.json` schemas have independent `0.1` contract versions and ship with the package.
- No migration command is added.

## Error handling

All validation that does not require extraction completes before `NEW_RUN_DIR` is created. Errors identify the violated contract and artifact path without printing adapter-controlled diagnostics or credential values.

Representative structured codes include:

- `baseline_verification_failed`
- `baseline_not_replayable`
- `source_missing`
- `source_hash_mismatch`
- `bundle_path_unsafe`
- `bundle_file_unsafe`
- `bundle_inventory_mismatch`
- `credential_key_forbidden`
- `adapter_missing`
- `extractor_identity_mismatch`
- `incompatible_environment`
- `replay_verification_failed`
- `deterministic_mismatch`

Replay never silently substitutes a config, adapter, model, route, or source.

## Testing and release gates

### Required behavior tests

- Relocated-root `text` replay reaches `exact` with original paths unavailable.
- `pdf_text` exact replay succeeds with a matching `pypdf` profile.
- Deterministic profile mismatch fails before output creation.
- Matching profile plus changed deterministic raw output produces `deterministic_mismatch`, a nonzero exit, and inspectable replay evidence.
- Nondeterministic/cloud fixture reaches `evidence_compared` and never claims exactness.
- Custom deterministic adapter without a profile is rejected by `bundle`.
- Generation-one/rerun, dry-run, failed, and extraction-incomplete baselines are rejected.
- A partial baseline caused only by review routes remains replayable.
- Recorded `--pages` selection and page IDs survive relocation.
- External post-run alignment is reapplied from the contained snapshot.
- Tampered baseline, source, config, portable route map, bundle manifest, or inventory fails before extraction.
- Absolute paths, `..`, symlinks, hard links, special files, duplicate mappings, undeclared files, and bundled adapter code are rejected.
- Credential-shaped config keys are rejected while similar words in source text, raw output, logs, and citations are not scanned.
- Replay manifest linkage and `replay.json` validate against schemas and `verify-run`.
- Existing schema-0.1 fixtures remain readable and valid.

### Existing project gates

The release must pass:

```bash
.venv/bin/python -m pytest tests/pageledger/ -q
ruff check pageledger/ tests/ examples/
.venv/bin/python -m mypy pageledger
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

The built wheel must pass a clean-install smoke sequence:

```text
run -> verify-run -> bundle -> relocate -> replay -> verify-run
```

Version surfaces, changelog, citation metadata, `uv.lock`, package schema contents, and tag-only release verification remain synchronized under the existing release process.

## Documentation changes

- Add `bundle -> replay` to the README quickstart.
- Document both commands and their exit/status contract in `docs/cli.md`.
- Document bundle layout, transported data, and trust boundaries in `docs/artifacts.md`.
- Add the optional profile and replay linkage to the run-manifest spec.
- Add the optional adapter profile hook to the adapter protocol.
- State verified replay capabilities and limits without hermetic claims.
- Replace the unimplemented staged `extract`/`audit` target in `docs/design.md` with the shipped replay lifecycle.
- Update `skills/pageledger/SKILL.md` with the verified replay workflow.

## Implementation boundaries

Use one new `pageledger/replay.py` module for profile canonicalization, bundle creation/validation, and replay orchestration until distinct responsibilities demonstrably require a split. Extend existing artifact, comparison, verification, runner, and CLI seams rather than introducing parallel engines.

No runtime dependency is added. Standard-library filesystem, hashing, JSON, import metadata, inspection, temporary-directory, and atomic-replacement primitives are sufficient.

## Simplicity report

Kept:

- Two commands, one directory bundle manifest, one replay evidence artifact, and one optional adapter hook.
- Existing runner, verifier, comparison, aligner, and schema compatibility model.
- Exactness only where the evidence can support it.

Simplified:

- Three release themes become one verified replay lifecycle.
- A self-declared determinism bit becomes a determinism bit plus one canonical material-runtime profile.
- Secret handling is a narrow structural key rejection, not a scanner or redactor.
- Transport is an ordinary directory, not an archive subsystem.

Removed:

- Staged `extract`/`audit` commands.
- Environment installation, bundled code execution, containers, signatures, archives, thin bundles, and lineage replay.
- Claims of hermetic, cross-platform, cloud-identical, or accuracy reproduction.

Verdict: Pass. The design adds only the machinery required to make PageLedger-controlled replay portable, fail-closed, and auditable.
