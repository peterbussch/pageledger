# PageLedger 0.4.0 Verified Replay Implementation Plan

> **Historical implementation record.** This completed 2026-08-17 execution
> plan is retained for development history; it is not current user guidance or
> an active checklist. See [the current documentation index](../../README.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship PageLedger 0.4.0 with a fail-closed, cross-machine verified replay envelope that proves byte-identical output for admissible deterministic adapters and records comparison evidence for nondeterministic/cloud adapters.

**Architecture:** Add one `pageledger/replay.py` module that grows in place from reproducibility-profile helpers into bundle validation and replay orchestration. It must reuse the existing `run`, `verify_run`, `compare_runs`, and `align_run` implementations; existing run artifacts stay at schema `0.1`, while `bundle.json` and `replay.json` receive independent schema `0.1` contracts.

**Tech Stack:** Python 3.10+, standard library (`hashlib`, `importlib.metadata`, `inspect`, `json`, `os`, `platform`, `shutil`, `stat`, `tempfile`), PyYAML, pytest, jsonschema in the development extra only.

**Spec:** `docs/superpowers/specs/2026-08-17-pageledger-0.4.0-verified-replay-design.md`

## Global Constraints

- Add exactly two commands: `pageledger bundle RUN_DIR --out BUNDLE_DIR [--json]` and `pageledger replay BUNDLE_DIR --out NEW_RUN_DIR [--adapter-path TRUSTED_LOCAL_DIR] [--json]`.
- Call the feature **verified replay**. Do not claim full, hermetic, cross-platform, cloud-identical, or accuracy reproduction.
- Keep runtime dependencies unchanged: PyYAML only in core; `pypdf` remains behind the `pdf` extra.
- Keep run artifact `schema_version: "0.1"`; bundle and replay contracts each use independent version `"0.1"`.
- Keep all profile, bundle, and replay orchestration in one new `pageledger/replay.py` module until the code demonstrates a real need to split.
- Reuse `run()`, `verify_run()`, `compare_runs()`, and `align_run()`; do not create parallel extraction, verification, comparison, or alignment engines.
- A deterministic result is `exact` only when its preflight profile matches and every common raw SHA-256 is equal. A mismatch is `deterministic_mismatch`, leaves the replay run inspectable, and exits 1.
- A nondeterministic or cloud result is `evidence_compared`; it never claims exactness and exits 0 after successful run verification and comparison.
- A deterministic profile mismatch is `incompatible_environment`; reject before creating `NEW_RUN_DIR` and exit 1.
- Bundle only execute-mode, generation-zero runs with no failed or unattempted pages. Accept partial runs only when the partial state is caused by explicit review routes.
- Treat bundles as untrusted data: reject absolute executable paths, `..`, symlinks, hard links, sockets, devices, FIFOs, duplicate mappings, missing/extra files, and size/hash mismatches.
- Never import adapter code from a bundle. `--adapter-path` must resolve outside the bundle and denotes locally trusted code only.
- Never execute baseline absolute source paths or consume `rerun-manifest.yml` as a replay plan; execute only the ordered bundle source mapping.
- Secret checks inspect only mappings beneath `adapter_options` or `hook_options` in the config snapshot plus persisted extractor `options`. Normalize keys by removing non-alphanumerics and lowercasing; reject exact matches in `{apikey, apitoken, token, accesstoken, authtoken, bearertoken, refreshtoken, clientsecret, secretkey, password, credential, credentials, authorization, privatekey, accesskey}`.
- Do not scan values, source bytes, raw output, citations, logs, budget/pricing/schema text, or arbitrary prose. Add no redaction or override mode.
- Preserve `audit.json` as the source of truth; `audit.md` remains only its rendering.
- Add no archive format, installer, package manager, container, signature system, adapter/model/credential bundling, lineage replay, thin bundle, provider registry, staged `extract`/`audit` command, database, service, or web UI.

## File Structure

| Path | Responsibility in 0.4.0 |
|---|---|
| `pageledger/replay.py` | The sole new runtime module: profile envelope, strict bundle creation/validation, structured replay errors, replay execution, and atomic replay finalization. |
| `pageledger/adapters.py` | Optional adapter hook plus PageLedger-owned built-in material descriptors for text, pypdf, Tesseract, Poppler, and trained data. |
| `pageledger/runner.py` | Compute a profile once per execution and persist it only in manifest extractor entries. |
| `pageledger/compare.py` | Add raw SHA-256 equality to the existing comparison report. |
| `pageledger/verify.py` | Validate optional `replay.json` and its manifest linkage without changing ordinary-run requirements. |
| `pageledger/cli.py` | Expose only the two approved commands and their exit/status contract. |
| `schemas/manifest.schema.json` | Optional profile, replay artifact declaration, and replay linkage fields; legacy schema-0.1 remains valid. |
| `schemas/bundle.schema.json` | Strict schema for `bundle.json`. |
| `schemas/replay.schema.json` | Strict schema for `replay.json`. |
| `tests/pageledger/test_replay.py` | Profile, bundle, replay, relocation, tamper, trust-boundary, and outcome tests. |
| Existing adapter/compare/schema/verify/CLI/release tests | Focused compatibility and integration assertions beside the code they already cover. |

---

### Task 1: Record admissible reproducibility profiles

**Files:**
- Create: `pageledger/replay.py`
- Modify: `pageledger/adapters.py`
- Modify: `pageledger/runner.py`
- Modify: `schemas/manifest.schema.json`
- Modify: `tests/pageledger/test_adapters.py`
- Modify: `tests/pageledger/test_schemas.py`
- Create: `tests/pageledger/test_replay.py`

**Interfaces:**
- Consumes: existing `load_adapter(name, options)`, immutable built-in adapter metadata, and `manifest.extractors[]`.
- Produces: `build_reproducibility_profile(adapter: Any) -> dict[str, Any] | None`, `profile_sha256(profile: Mapping[str, object]) -> str`, and optional `manifest.extractors[].reproducibility_profile` used by Tasks 3 and 4.

- [ ] **Step 1: Add failing profile-contract tests**

At the top of `tests/pageledger/test_replay.py`, add this reusable run fixture for later tasks:

```python
MINIMAL_CONFIG = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""


def _run_text(
    tmp_path: Path,
    *,
    name: str = "run",
    config_text: str = MINIMAL_CONFIG,
    source_text: str = "first page of stable text\fsecond page of stable text\n",
    adapter_path: Path | None = None,
    pages: str | None = None,
    routes_path: Path | None = None,
) -> tuple[Path, Path, Path]:
    from pageledger.runner import run

    source = tmp_path / f"{name}-source.txt"
    source.write_text(source_text, encoding="utf-8")
    config = tmp_path / f"{name}-config.yml"
    config.write_text(config_text, encoding="utf-8")
    out = tmp_path / name
    run(
        inputs=[source],
        config_path=config,
        out_dir=out,
        dry_run=False,
        adapter_path=adapter_path,
        pages=pages,
        routes_path=routes_path,
    )
    return out, source, config
```

Then add tests with these assertions and local adapter doubles:

```python
def test_text_profile_is_stable_and_self_hashing() -> None:
    from pageledger.adapters import TextAdapter
    from pageledger.replay import build_reproducibility_profile, profile_sha256

    profile = build_reproducibility_profile(TextAdapter())
    assert profile is not None
    assert profile["profile_version"] == "0.1"
    assert profile["pageledger"]["version"]
    assert len(profile["pageledger"]["code_sha256"]) == 64
    assert len(profile["adapter"]["code_sha256"]) == 64
    assert profile["materials"] == []
    assert profile["profile_sha256"] == profile_sha256(profile)


def test_custom_deterministic_adapter_without_hook_has_no_profile() -> None:
    class CustomDeterministicAdapter:
        name = "custom"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

    assert build_reproducibility_profile(CustomDeterministicAdapter()) is None


def test_profile_rejects_nonfinite_or_unexpected_hook_data() -> None:
    class AdapterReturning:
        name = "custom"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

        def reproducibility_profile(self) -> dict[str, object]:
            return {"materials": [], "path": "/tmp/x"}

    with pytest.raises(ValueError, match="reproducibility_profile"):
        build_reproducibility_profile(AdapterReturning())


def test_execute_manifest_records_profile_but_provenance_does_not(tmp_path: Path) -> None:
    out, _, _ = _run_text(tmp_path)
    manifest = json.loads((out / "manifest.json").read_text())
    provenance = json.loads((out / "provenance.jsonl").read_text().splitlines()[0])
    assert manifest["extractors"][0]["reproducibility_profile"]["profile_sha256"]
    assert "reproducibility_profile" not in provenance["extractor"]
```

Also assert that the existing minimal legacy manifest fixture still validates after the optional schema field is added.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_adapters.py tests/pageledger/test_replay.py tests/pageledger/test_schemas.py -q
```

Expected: FAIL because `pageledger.replay` and the profile fields do not exist.

- [ ] **Step 3: Add the optional adapter hook and built-in material providers**

Add this optional method to each built-in adapter; absence must remain valid for custom adapters:

```python
def reproducibility_profile(self) -> dict[str, object]:
    return {"materials": []}
```

`PdfTextAdapter` must return one `package` material for the installed `pypdf` distribution. `PdfOcrAdapter` must return `binary` materials for the resolved `tesseract` and `pdftoppm` executables plus one `model` material per requested language's `.traineddata` file. Every material has exactly:

```python
{
    "kind": "binary" | "package" | "model" | "asset",
    "name": str,
    "version": str,
    "sha256": str,  # lowercase 64-character hex
}
```

Use a deterministic aggregate for distributions: iterate `importlib.metadata.distribution(name).files` in normalized relative-path order and hash each relative path, a NUL separator, and the regular file bytes. Hash executable and trained-data bytes directly. Resolve the Tesseract data directory from the first line of `tesseract --list-langs`; split `lang` on `+` and require every requested trained-data file. Never persist any resolved path.

Extend `_adapter_contract_issues()` only to report a non-callable `reproducibility_profile`; do not call the hook during conformance checks.

- [ ] **Step 4: Implement the canonical PageLedger-owned envelope**

In `pageledger/replay.py`, define:

```python
PROFILE_VERSION = "0.1"


def profile_sha256(profile: Mapping[str, object]) -> str:
    payload = dict(profile)
    payload.pop("profile_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_reproducibility_profile(adapter: Any) -> dict[str, Any] | None:
    hook = getattr(adapter, "reproducibility_profile", None)
    if hook is None:
        return None
    if not callable(hook):
        raise ValueError("adapter reproducibility_profile must be callable")
    supplied = hook()
    # Validate exact top-level key {'materials'}, exact material keys/types,
    # finite JSON, unique (kind, name) pairs, lowercase SHA-256, and sorted output.
    materials = _validate_materials(supplied)
    profile = {
        "profile_version": PROFILE_VERSION,
        "pageledger": {
            "version": __version__,
            "code_sha256": _package_code_sha256(),
        },
        "adapter": {
            "module": type(adapter).__module__,
            "name": adapter.name,
            "version": adapter.version,
            "code_sha256": _adapter_code_sha256(adapter),
        },
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "preferred_encoding": locale.getpreferredencoding(False),
            "filesystem_encoding": sys.getfilesystemencoding(),
        },
        "materials": sorted(materials, key=lambda item: (item["kind"], item["name"])),
    }
    profile["profile_sha256"] = profile_sha256(profile)
    return profile
```

`_package_code_sha256()` hashes the relative names and bytes of all regular `pageledger/*.py` files in sorted order. `_adapter_code_sha256()` hashes the regular source/module file returned by `inspect.getsourcefile(type(adapter))`; return a clear `ValueError` if it cannot be located. Reject hook mappings with extra fields rather than inventing forward-compatibility.

- [ ] **Step 5: Persist one computed profile per run without duplicating provenance**

In `runner.run()` compute the profile once after adapter construction and before `_validate_out_dir(out_dir)`, only for execute mode:

```python
adapter_profile = (
    build_reproducibility_profile(adapter)
    if adapter is not None and not dry_run
    else None
)
```

Attach the same mapping to each actual extractor entry. If an execute-mode route contains only `review`/`skip` pages and therefore creates no actual extractor entry, append one planned entry with `model: None`, the configured default-prompt hash, adapter metadata/options, and the profile. This preserves bundle eligibility for review-route partial baselines without pretending a page was extracted.

In `schemas/manifest.schema.json`, add optional `reproducibility_profile` with the exact envelope above, `additionalProperties: false`, unique materials, and 64-character lowercase SHA patterns. Do not add it to `required`.

- [ ] **Step 6: Run focused and adjacent tests**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_adapters.py tests/pageledger/test_replay.py tests/pageledger/test_schemas.py tests/pageledger/test_dry_run.py tests/pageledger/test_routing.py -q
ruff check pageledger/adapters.py pageledger/replay.py pageledger/runner.py tests/pageledger/test_adapters.py tests/pageledger/test_replay.py tests/pageledger/test_schemas.py
.venv/bin/python -m mypy pageledger
```

Expected: PASS; ordinary custom adapters without the hook still run, legacy manifests still validate, and no new runtime dependency appears.

- [ ] **Step 7: Commit**

```bash
git add pageledger/replay.py pageledger/adapters.py pageledger/runner.py schemas/manifest.schema.json tests/pageledger/test_adapters.py tests/pageledger/test_schemas.py tests/pageledger/test_replay.py
git commit -m "feat: record replay environment profiles"
```

### Task 2: Extend comparison with raw-output evidence

**Files:**
- Modify: `pageledger/compare.py`
- Modify: `tests/pageledger/test_compare.py`

**Interfaces:**
- Consumes: provenance `result.raw_sha256` already verified by `verify_run()`.
- Produces: per-page `raw_sha256_a`, `raw_sha256_b`, `raw_equal`; top-level `raw_equal_total`, `raw_different_total`, and `raw_missing_total` used by replay outcome logic in Task 4.

- [ ] **Step 1: Add failing raw-comparison tests**

Add exact assertions:

```python
def test_compare_reports_equal_raw_hashes(tmp_path: Path) -> None:
    source = tmp_path / "doc.txt"
    source.write_text("same text", encoding="utf-8")
    out_a = _run([source], tmp_path, "raw-a")
    out_b = _run([source], tmp_path, "raw-b")
    report = compare_runs(out_a, out_b)
    assert report["raw_equal_total"] == 1
    assert report["raw_different_total"] == 0
    assert report["raw_missing_total"] == 0
    assert report["pages"][0]["raw_equal"] is True
    assert report["pages"][0]["raw_sha256_a"] == report["pages"][0]["raw_sha256_b"]


def test_compare_reports_different_raw_hashes_without_failing(tmp_path: Path) -> None:
    source = tmp_path / "doc.txt"
    source.write_text("same text", encoding="utf-8")
    out_a = _run([source], tmp_path, "raw-a")
    out_b = _run([source], tmp_path, "raw-b")
    raw_b = next((out_b / "raw").iterdir())
    raw_b.write_text("different output", encoding="utf-8")
    entries = [json.loads(line) for line in (out_b / "provenance.jsonl").read_text().splitlines()]
    entries[0]["result"]["raw_sha256"] = hashlib.sha256(raw_b.read_bytes()).hexdigest()
    (out_b / "provenance.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    report = compare_runs(out_a, out_b)
    assert report["raw_equal_total"] == 0
    assert report["raw_different_total"] == 1
    assert report["pages"][0]["raw_equal"] is False


def test_compare_reports_missing_legacy_raw_hash(tmp_path: Path) -> None:
    source = tmp_path / "doc.txt"
    source.write_text("same text", encoding="utf-8")
    out_a = _run([source], tmp_path, "raw-a")
    out_b = _run([source], tmp_path, "raw-b")
    entries = [json.loads(line) for line in (out_b / "provenance.jsonl").read_text().splitlines()]
    entries[0]["result"].pop("raw_sha256")
    (out_b / "provenance.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    report = compare_runs(out_a, out_b)
    assert report["raw_missing_total"] == 1
    assert report["pages"][0]["raw_equal"] is None
```

Extend the CLI rendering test to assert `Raw output: equal 1 / different 0 / missing 0`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_compare.py -q
```

Expected: FAIL on missing raw fields/counts.

- [ ] **Step 3: Add raw evidence to the existing comparison loop**

Read hashes from each page's provenance result without opening a second comparison path:

```python
raw_a = (provenance_a.get("result") or {}).get("raw_sha256")
raw_b = (provenance_b.get("result") or {}).get("raw_sha256")
raw_equal = raw_a == raw_b if isinstance(raw_a, str) and isinstance(raw_b, str) else None
```

For every common page, emit the three per-page fields. Increment exactly one top-level bucket: `raw_missing_total` when either hash is absent/invalid, otherwise `raw_equal_total` or `raw_different_total`. Do not alter extraction/grade comparability or `compare-runs` exit behavior.

Add this rendering line after comparable-page counts:

```python
f"Raw output: equal {report['raw_equal_total']} / "
f"different {report['raw_different_total']} / missing {report['raw_missing_total']}"
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_compare.py tests/pageledger/test_verify.py -q
ruff check pageledger/compare.py tests/pageledger/test_compare.py
```

Expected: PASS; raw differences remain visible reports rather than comparison errors.

- [ ] **Step 5: Commit**

```bash
git add pageledger/compare.py tests/pageledger/test_compare.py
git commit -m "feat: compare raw extraction evidence"
```

### Task 3: Create and validate inspectable replay bundles

**Files:**
- Modify: `pageledger/replay.py`
- Create: `schemas/bundle.schema.json`
- Modify: `tests/pageledger/test_replay.py`
- Modify: `tests/pageledger/test_schemas.py`

**Interfaces:**
- Consumes: `build_reproducibility_profile()`, profile-bearing extractor entries from Task 1, `verify_run(run_dir)`, and canonical manifest artifact declarations.
- Produces: `ReplayError`, `bundle_run(run_dir: Path, out_dir: Path) -> dict[str, Any]`, and `validate_bundle(bundle_dir: Path) -> dict[str, Any]` used by Task 4 and Task 5.

- [ ] **Step 1: Add failing bundle eligibility and layout tests**

Create a text run and assert:

```python
run_dir, _, _ = _run_text(tmp_path)
bundle_dir = tmp_path / "bundle"
manifest = json.loads((run_dir / "manifest.json").read_text())
result = bundle_run(run_dir, bundle_dir)
bundle = json.loads((bundle_dir / "bundle.json").read_text())
assert result["bundle_dir"] == str(bundle_dir.resolve())
assert bundle["bundle_schema_version"] == "0.1"
assert bundle["baseline"]["run_id"] == manifest["run_id"]
assert bundle["baseline"]["manifest"] == "baseline/manifest.json"
assert bundle["replay"]["config"] == "baseline/config-snapshot.yml"
assert bundle["replay"]["route_map"] == "replay-route-map.yml"
assert bundle["sources"][0]["path"] == "sources/source-0001.txt"
assert yaml.safe_load((bundle_dir / "replay-route-map.yml").read_text())["documents"][0]["source"] == "sources/source-0001.txt"
assert all(entry["path"] != "bundle.json" for entry in bundle["files"])
assert validate_bundle(bundle_dir)["baseline"]["run_id"] == manifest["run_id"]
```

Add parameterized rejection tests for dry-run, generation one, failed/incomplete, deterministic-without-profile, changed/missing source, symlink, hard link, FIFO, absolute/`..` inventory path, duplicate source mapping, undeclared file, and exact forbidden credential keys. Add positive assertions that `max_tokens`, source prose containing `token`, raw/log/citation text, and ordinary adapter option values are not scanned.

- [ ] **Step 2: Run the bundle tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_replay.py tests/pageledger/test_schemas.py -q
```

Expected: FAIL because bundle APIs/schema do not exist.

- [ ] **Step 3: Define structured errors and the strict bundle schema**

Add:

```python
class ReplayError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
```

`schemas/bundle.schema.json` must require this shape and reject extra fields:

```json
{
  "bundle_schema_version": "0.1",
  "baseline": {
    "run_id": "run-20260817T120000Z",
    "manifest": "baseline/manifest.json",
    "manifest_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "execution_mode": "execute",
    "run_depth": 0,
    "extractor": {
      "adapter": "text",
      "version": "0.1",
      "deterministic": true,
      "input_types": ["text"],
      "output_types": ["text"],
      "capabilities": ["embedded_text", "local"],
      "options": {},
      "reproducibility_profile": {
        "profile_version": "0.1",
        "pageledger": {
          "version": "0.4.0",
          "code_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        },
        "adapter": {
          "module": "pageledger.adapters",
          "name": "text",
          "version": "0.1",
          "code_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        },
        "runtime": {
          "python_implementation": "CPython",
          "python_version": "3.14.2",
          "system": "Darwin",
          "release": "25.6.0",
          "machine": "arm64",
          "preferred_encoding": "UTF-8",
          "filesystem_encoding": "utf-8"
        },
        "materials": [],
        "profile_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      }
    }
  },
  "replay": {
    "config": "baseline/config-snapshot.yml",
    "route_map": "replay-route-map.yml"
  },
  "sources": [
    {
      "index": 1,
      "path": "sources/source-0001.txt",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "size": 123,
      "page_count": 1,
      "pages": "1-3"
    }
  ],
  "files": [
    {"path": "baseline/manifest.json", "size": 123, "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
  ]
}
```

`sources[].pages` is optional. `baseline.extractor.reproducibility_profile` contains the complete strict profile mapping or null; null is allowed only for nondeterministic/cloud evidence replay. The ordinary identity is derived from all baseline manifest extractor entries and bundle creation rejects conflicting adapter/version/determinism/type/capability/options/profile values. Models and per-page prompt hashes remain in the unchanged manifest/provenance and are checked by the post-run comparison rather than preflight. All path strings are validated again at runtime; JSON Schema alone is not the security boundary.

- [ ] **Step 4: Implement canonical artifact and source copying**

Implement `bundle_run()` in this order:

1. Resolve `RUN_DIR`; reject if `verify_run()` is not `pass`.
2. Parse the manifest and enforce execute mode, run depth 0, no failure/unattempted counts, and `status in {completed, partial}`. For `partial`, require `pages_failed == pages_not_attempted == 0` and `pages_routed_review > 0`.
3. Collapse manifest extractor entries to one ordinary identity (adapter, version, deterministic flag, sorted input/output types/capabilities, canonical options, complete profile). Reject conflicting values. Require a deterministic non-cloud identity to contain one valid self-hashed profile; nondeterministic/cloud identity may record null.
4. Load the config snapshot and apply the narrow credential-key rule only at approved option mappings and manifest extractor options.
5. Resolve every manifest source, require a regular non-symlink/non-hard-linked file, and verify its hash before copying.
6. Select only `manifest.json`, every declared artifact, the contents of declared raw/normalized directories, and `align-schema-snapshot.yml` when external alignment evidence requires it.
7. Create a temporary sibling of `BUNDLE_DIR`; copy baseline files byte-for-byte and sources in manifest order as `source-NNNN` plus the lowercase original suffix.
8. Rewrite only `documents[].source` in a copied route map; write it as `replay-route-map.yml`.
9. Build a lexicographically sorted inventory of every regular transported file except `bundle.json`, then write `bundle.json` with sorted JSON keys.
10. Call `validate_bundle()` on the temporary directory and atomically rename it to the requested, previously nonexistent output.

On any error, remove only the known temporary sibling; never remove an existing requested output.

- [ ] **Step 5: Implement runtime bundle validation as the sole replay trust gate**

`validate_bundle()` must manually validate types and exact keys without importing `jsonschema` at runtime. For every declared path:

```python
relative = Path(value)
if relative.is_absolute() or ".." in relative.parts:
    raise ReplayError("bundle_path_unsafe", f"Unsafe bundle path: {value}")
candidate = bundle_root / relative
st = candidate.lstat()
if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
    raise ReplayError("bundle_file_unsafe", f"Unsafe bundle file: {value}")
```

Reject duplicate inventory paths/source indexes/source paths, invalid ordering, missing files, extra regular files, special files anywhere below the root, size/hash mismatch, a baseline manifest hash mismatch, source/baseline-manifest disagreement, or portable-route disagreement. `bundle.json` is the only file excluded from `files`; hash its exact bytes for the later replay result.

- [ ] **Step 6: Run bundle, schema, security, and compatibility tests**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_replay.py tests/pageledger/test_schemas.py tests/pageledger/test_verify.py tests/pageledger/test_routing.py -q
ruff check pageledger/replay.py tests/pageledger/test_replay.py tests/pageledger/test_schemas.py
.venv/bin/python -m mypy pageledger
```

Expected: PASS. Inspect `git diff --check` and confirm no archive, environment-manager, signature, or general secret-scanner code was added.

- [ ] **Step 7: Commit**

```bash
git add pageledger/replay.py schemas/bundle.schema.json tests/pageledger/test_replay.py tests/pageledger/test_schemas.py
git commit -m "feat: build verified replay bundles"
```

### Task 4: Replay a bundle through existing engines

**Files:**
- Modify: `pageledger/replay.py`
- Create: `schemas/replay.schema.json`
- Modify: `schemas/manifest.schema.json`
- Modify: `tests/pageledger/test_replay.py`
- Modify: `tests/pageledger/test_schemas.py`

**Interfaces:**
- Consumes: `validate_bundle()`, `profile_sha256()`, `runner.run()`, `verify_run()`, `compare_runs()`, `align_run()`, and raw totals from Task 2.
- Produces: `replay_bundle(bundle_dir: Path, out_dir: Path, *, adapter_path: Path | None = None) -> dict[str, Any]`, `replay.json`, and optional manifest replay linkage consumed by verifier/CLI in Task 5.

- [ ] **Step 1: Add failing relocated exact/evidence tests**

Add these direct-API cases:

```python
def test_relocated_text_replay_is_exact_without_original_source(tmp_path: Path) -> None:
    run_dir, source, _ = _run_text(tmp_path)
    bundle_run(run_dir, tmp_path / "bundle")
    source.unlink()
    moved = tmp_path / "other-root" / "bundle"
    shutil.copytree(tmp_path / "bundle", moved)
    result = replay_bundle(moved, tmp_path / "replayed")
    assert result["outcome"] == "exact"
    assert result["raw"]["different"] == 0
    assert verify_run(tmp_path / "replayed")["status"] == "pass"


def test_profile_mismatch_fails_before_output_creation(tmp_path: Path, monkeypatch) -> None:
    run_dir, _, _ = _run_text(tmp_path)
    bundle = tmp_path / "bundle"
    bundle_run(run_dir, bundle)
    original = replay_module.build_reproducibility_profile

    def mismatched_profile(adapter: object) -> dict[str, object] | None:
        profile = original(adapter)
        assert profile is not None
        profile["runtime"] = {**profile["runtime"], "release": "different-release"}
        profile["profile_sha256"] = profile_sha256(profile)
        return profile

    monkeypatch.setattr(replay_module, "build_reproducibility_profile", mismatched_profile)
    with pytest.raises(ReplayError) as error:
        replay_bundle(bundle, tmp_path / "never-created")
    assert error.value.code == "incompatible_environment"
    assert not (tmp_path / "never-created").exists()


def test_nondeterministic_adapter_is_evidence_compared(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    (adapter_dir / "cloudish.py").write_text(NONDETERMINISTIC_ADAPTER, encoding="utf-8")
    config = MINIMAL_CONFIG.replace("adapter: text", "adapter: cloudish:Adapter")
    run_dir, _, _ = _run_text(
        tmp_path,
        config_text=config,
        adapter_path=adapter_dir,
    )
    bundle = tmp_path / "bundle"
    bundle_run(run_dir, bundle)
    result = replay_bundle(bundle, tmp_path / "replayed", adapter_path=adapter_dir)
    assert result["outcome"] == "evidence_compared"
```

Define `NONDETERMINISTIC_ADAPTER` beside `MINIMAL_CONFIG` as this complete module:

```python
NONDETERMINISTIC_ADAPTER = """\
from pageledger.adapters import ExtractionResult

class Adapter:
    name = "cloudish"
    version = "1.0"
    deterministic = False
    input_types = ("text",)
    output_types = ("text",)
    capabilities = ("cloud",)

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        content = source.read_text(encoding="utf-8").split("\\f")[page_number - 1]
        return ExtractionResult(
            content=content,
            format="text",
            confidence=None,
            model="cloudish-fixed-fixture",
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )
"""
```

Also cover matching-profile/raw mismatch, `pdf_text`, recorded `--pages`, routed partial review, ordinary multi-input order, external alignment snapshot, tampered bundle rejection before output creation, and `--adapter-path` resolving inside the bundle.

- [ ] **Step 2: Run replay tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_replay.py tests/pageledger/test_schemas.py -q
```

Expected: FAIL because replay execution/schema/linkage do not exist.

- [ ] **Step 3: Define replay evidence and additive manifest linkage**

`schemas/replay.schema.json` must require and close this shape:

```json
{
  "replay_schema_version": "0.1",
  "bundle_manifest_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "baseline_run_id": "run-20260817T120000Z",
  "replay_run_id": "run-20260817T120100Z",
  "baseline_extractor": {
    "adapter": "text",
    "version": "0.1",
    "deterministic": true,
    "input_types": ["text"],
    "output_types": ["text"],
    "capabilities": ["embedded_text", "local"],
    "options": {},
    "reproducibility_profile_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "local_extractor": {
    "adapter": "text",
    "version": "0.1",
    "deterministic": true,
    "input_types": ["text"],
    "output_types": ["text"],
    "capabilities": ["embedded_text", "local"],
    "options": {},
    "reproducibility_profile_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "profile_match": true,
  "outcome": "exact",
  "raw": {
    "equal": 1,
    "different": 0,
    "missing": 0,
    "different_page_ids": [],
    "missing_page_ids": []
  },
  "comparison": {}
}
```

`profile_match` is boolean or null; `outcome` is one of `exact`, `evidence_compared`, `deterministic_mismatch`. The preflight-only `incompatible_environment` does not produce a replay run or `replay.json`.

In `schemas/manifest.schema.json`, add optional `artifacts.replay` and optional top-level:

```json
{
  "replay_schema_version": "0.1",
  "baseline_run_id": "run-20260817T120000Z",
  "bundle_manifest_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "outcome": "exact"
}
```

Do not add either field to existing required lists.

- [ ] **Step 4: Implement deterministic preflight before output creation**

In `replay_bundle()`:

1. Call `validate_bundle()` and reject an existing `OUT_DIR`.
2. Resolve `adapter_path`; reject it if equal to or contained by the bundle root.
3. Load the bundled config with `validate_adapter=False`, select generation-zero effective adapter/options using the existing runner convention, make only the trusted local adapter importable, and call existing `load_adapter()`.
4. Compare name, version, deterministic flag, input/output types, capabilities, and options with the recorded baseline identity. Any mismatch raises `ReplayError("extractor_identity_mismatch", "Local adapter identity does not match the baseline")`.
5. If the adapter is deterministic and not cloud-capable, compute its local profile and compare `profile_sha256`; missing/mismatch raises `ReplayError("incompatible_environment", "Local reproducibility profile does not match the baseline")` before `OUT_DIR` exists.
6. Nondeterministic/cloud adapters may continue with `profile_match` null/false, but their ordinary identity must still match.

Never read a baseline source path, never import from the bundle, and never use `rerun-manifest.yml`.

- [ ] **Step 5: Invoke existing run/alignment/verification/comparison paths**

Call `run()` with ordered `sources[]` and bundled config:

```python
run_kwargs = {
    "inputs": source_paths,
    "config_path": config_path,
    "out_dir": out_dir,
    "dry_run": False,
    "adapter_path": adapter_path,
}
if "routing" in baseline_manifest:
    run_kwargs["routes_path"] = portable_route_path
elif len(source_paths) == 1 and source_mapping[0].get("pages"):
    run_kwargs["pages"] = source_mapping[0]["pages"]
result = run(**run_kwargs)
```

If baseline `alignment.schema_source != "config_snapshot"`, call `align_run(out_dir, schema_path=bundle_root / "baseline/align-schema-snapshot.yml")`. If alignment used the config snapshot, the normal run already applies that schema; do not align twice.

Require `verify_run(out_dir)["status"] == "pass"`, then call `compare_runs(baseline_dir, out_dir)`. Derive raw page-id lists from comparison pages. Deterministic mode is `exact` only when `raw_different_total == raw_missing_total == 0` and baseline/replay extracted page sets agree; otherwise it is `deterministic_mismatch`. Nondeterministic/cloud mode is always `evidence_compared` after successful verification.

- [ ] **Step 6: Finalize replay evidence atomically and manifest-last**

Write `replay.json` to a sibling temporary file and `os.replace()` it. Then parse `manifest.json`, add the optional artifact/linkage fields, serialize a temporary manifest, and replace `manifest.json` last. Run `verify_run(out_dir)` again after linkage; a failure raises `ReplayError("replay_verification_failed", "Final replay evidence does not verify")` but leaves the directory inspectable.

Return:

```python
{
    "outcome": outcome,
    "run_id": result["run_id"],
    "out_dir": str(out_dir.resolve()),
    "baseline_run_id": baseline_manifest["run_id"],
    "bundle_manifest_sha256": bundle_sha256,
    "profile_match": profile_match,
    "raw": replay_evidence["raw"],
}
```

- [ ] **Step 7: Run replay, alignment, schema, and full focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_replay.py tests/pageledger/test_schemas.py tests/pageledger/test_aligner.py tests/pageledger/test_compare.py tests/pageledger/test_verify.py -q
ruff check pageledger/replay.py tests/pageledger/test_replay.py tests/pageledger/test_schemas.py
.venv/bin/python -m mypy pageledger
```

Expected: PASS; mismatched preflight never creates output, deterministic mismatch preserves an inspectable output, and relocated exact replay does not access original paths.

- [ ] **Step 8: Commit**

```bash
git add pageledger/replay.py schemas/replay.schema.json schemas/manifest.schema.json tests/pageledger/test_replay.py tests/pageledger/test_schemas.py
git commit -m "feat: replay verified extraction bundles"
```

### Task 5: Verify replay linkage and expose the two-command CLI

**Files:**
- Modify: `pageledger/verify.py`
- Modify: `pageledger/cli.py`
- Modify: `tests/pageledger/test_verify.py`
- Modify: `tests/pageledger/test_cli.py`
- Modify: `tests/pageledger/test_replay.py`

**Interfaces:**
- Consumes: `ReplayError`, `bundle_run()`, `replay_bundle()`, manifest replay linkage, and `replay.json` from Tasks 3-4.
- Produces: stable CLI status/exit behavior and `verify_run()` coherence checks for replay evidence.

- [ ] **Step 1: Add failing verifier and parser/exit tests**

Add verifier tests asserting a valid replay passes, while changed `baseline_run_id`, `replay_run_id`, bundle hash, outcome, or schema version yields a replay-linkage error. Assert ordinary manifests without replay fields still pass.

Add CLI assertions:

```python
run_dir, _, _ = _run_text(tmp_path)
bundle_dir = tmp_path / "bundle"
assert main(["bundle", str(run_dir), "--out", str(bundle_dir), "--json"]) == 0
bundle_result = json.loads(capsys.readouterr().out)
assert bundle_result["bundle_dir"] == str(bundle_dir.resolve())

replay_dir = tmp_path / "replayed"
assert main(["replay", str(bundle_dir), "--out", str(replay_dir), "--json"]) == 0
replay_result = json.loads(capsys.readouterr().out)
assert replay_result["outcome"] == "exact"

with pytest.raises(SystemExit) as error:
    main(["replay", str(bundle_dir), "--out", str(tmp_path / "unused"), "--config", "x.yml"])
assert error.value.code == 2
```

Also assert deterministic mismatch returns 1 with JSON outcome, incompatible preflight returns 1 with JSON `code`, human output says `Verified replay outcome: exact`, and neither parser exposes config/routes/pages/adapter override options.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_verify.py tests/pageledger/test_cli.py tests/pageledger/test_replay.py -q
```

Expected: FAIL because the verifier treats replay as text and the CLI has no commands.

- [ ] **Step 3: Teach `verify_run()` the optional replay contract**

Keep `REQUIRED_ARTIFACTS` unchanged. In `_load_artifact()`, parse key `replay` as a JSON mapping. Add `_check_replay_linkage()` that runs only when either `artifacts.replay` or top-level `replay` exists and requires both. Check:

- `replay.json.replay_run_id == manifest.run_id`.
- replay schema version, baseline run ID, bundle manifest SHA-256, and outcome agree exactly with top-level linkage.
- raw counts are nonnegative integers; page-id lists are unique strings; their lengths match `different` and `missing`.
- `comparison.run_a.run_id` and `comparison.run_b.run_id` match baseline and replay IDs.
- `exact` requires `profile_match is True`, zero different/missing raw pages, and no pages-only-in-either comparison list.
- `evidence_compared` is permitted only for a nondeterministic/cloud baseline extractor.
- `deterministic_mismatch` requires at least one different/missing raw page or a page-set difference.

Use existing `_add()` with stable codes `replay_artifact_missing`, `replay_artifact_malformed`, and `replay_linkage_mismatch`. Do not import jsonschema at runtime.

- [ ] **Step 4: Add only the approved CLI arguments and structured error JSON**

In `build_parser()` add:

```python
bundle_parser = subparsers.add_parser("bundle", help="Create an inspectable verified replay bundle")
bundle_parser.add_argument("run_dir", type=Path)
bundle_parser.add_argument("--out", required=True, type=Path)
bundle_parser.add_argument("--json", action="store_true", dest="json_output")

replay_parser = subparsers.add_parser("replay", help="Replay a verified bundle on this machine")
replay_parser.add_argument("bundle_dir", type=Path)
replay_parser.add_argument("--out", required=True, type=Path)
replay_parser.add_argument("--adapter-path", type=Path, default=None)
replay_parser.add_argument("--json", action="store_true", dest="json_output")
```

Dispatch both in `main()`. Extend `_print_error_json()` to add `"code": exc.code` only when the exception has a nonempty string code.

`_cmd_bundle()` returns 0 on success and 1 for `ReplayError`, `RuntimeError`, `ValueError`, or `OSError`. `_cmd_replay()` returns 0 for `exact`/`evidence_compared`, 1 for `deterministic_mismatch`, and 1 for structured failures. Argparse remains the sole source of usage exit 2.

- [ ] **Step 5: Run CLI/verifier tests and an explicit relocated command sequence**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_verify.py tests/pageledger/test_cli.py tests/pageledger/test_replay.py -q
ruff check pageledger/verify.py pageledger/cli.py tests/pageledger/test_verify.py tests/pageledger/test_cli.py tests/pageledger/test_replay.py
.venv/bin/python -m mypy pageledger
```

Then run in a temporary directory:

```bash
tmpdir="$(mktemp -d)"
printf 'first page\fsecond page\n' > "$tmpdir/source.txt"
.venv/bin/pageledger run "$tmpdir/source.txt" --adapter text --out "$tmpdir/run" --json
.venv/bin/pageledger bundle "$tmpdir/run" --out "$tmpdir/bundle" --json
mv "$tmpdir/source.txt" "$tmpdir/original-path-is-gone.txt"
.venv/bin/pageledger replay "$tmpdir/bundle" --out "$tmpdir/replay" --json
.venv/bin/pageledger verify-run "$tmpdir/replay" --json
```

Expected: replay outcome `exact`; final verification `pass`.

- [ ] **Step 6: Commit**

```bash
git add pageledger/verify.py pageledger/cli.py tests/pageledger/test_verify.py tests/pageledger/test_cli.py tests/pageledger/test_replay.py
git commit -m "feat: expose and verify replay workflows"
```

### Task 6: Document and release PageLedger 0.4.0

**Files:**
- Modify: `README.md`
- Modify: `docs/cli.md`
- Modify: `docs/artifacts.md`
- Modify: `docs/run-manifest-spec.md`
- Modify: `docs/adapter-protocol.md`
- Modify: `docs/capabilities-and-limits.md`
- Modify: `docs/design.md`
- Modify: `docs/route-map-spec.md`
- Modify: `docs/audit-spec.md`
- Modify: `docs/examples/pageledger.yml`
- Modify: `skills/pageledger/SKILL.md`
- Modify: `CHANGELOG.md`
- Modify: `CITATION.cff`
- Modify: `pyproject.toml`
- Modify: `pageledger/_version.py`
- Modify: `uv.lock`
- Modify: `tests/pageledger/test_dry_run.py`
- Modify: `tests/pageledger/test_release.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/publish.yml`

**Interfaces:**
- Consumes: final command/artifact/profile/outcome names from Tasks 1-5.
- Produces: synchronized 0.4.0 package metadata, honest user documentation, packaged schemas, and clean-wheel replay smoke gates.

- [ ] **Step 1: Update release/workflow tests first and confirm RED**

Change the current-version assertions to:

```python
assert pageledger.__version__ == "0.4.0"
assert check_release(REPO, "v0.4.0") == []
```

Extend the workflow test to require both new schema filenames and the ordered smoke strings `pageledger bundle`, source relocation/removal, `pageledger replay`, and final `pageledger verify-run`.

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/test_dry_run.py::test_package_exports_release_version tests/pageledger/test_release.py -q
```

Expected: FAIL while metadata/workflows still describe 0.3.0a1.

- [ ] **Step 2: Synchronize release identity without rewriting historical fixtures**

Set version `0.4.0` in `pyproject.toml`, `pageledger/_version.py`, `CITATION.cff`, and the editable PageLedger entry in `uv.lock`. Set `CITATION.cff` release date to `2026-08-17`. Add `## 0.4.0 - 2026-08-17` at the top of `CHANGELOG.md` with bullets for verified bundles/replay, reproducibility profiles, raw comparison, and the explicit non-hermetic boundary.

Update prose that presents 0.3.0a1 as the current writer in the listed docs/examples, but leave historical compatibility fixtures in `tests/pageledger/test_compare.py` and `tests/pageledger/test_verify.py` at 0.3.0a1.

Run `uv lock` to regenerate the lock mechanically, then verify:

```bash
.venv/bin/python scripts/check_release.py v0.4.0
```

Expected: `release check: v0.4.0 metadata agrees`.

- [ ] **Step 3: Write the user-facing verified replay contract**

Document exactly:

- README quickstart: `run -> verify-run -> bundle -> copy/relocate -> replay -> verify-run`.
- `docs/cli.md`: both command syntaxes, accepted flags only, outcomes and exit codes 0/1/2, output-directory preflight, and locally trusted adapter-path behavior.
- `docs/artifacts.md`: the directory bundle tree, source inclusion, inventory, unchanged baseline, portable route map, and `replay.json`.
- `docs/run-manifest-spec.md`: optional extractor profile, `artifacts.replay`, and top-level replay linkage; run schema remains 0.1.
- `docs/adapter-protocol.md`: optional hook returning material hashes, exact allowed keys/kinds, no paths/secrets, absence blocks deterministic bundle exactness but not ordinary runs.
- `docs/capabilities-and-limits.md`: verified replay is shipped; it is not environment installation, hermetic reproduction, cloud identity, adapter/model bundling, or source licensing/privacy review.
- `docs/design.md`: replace the unimplemented staged `extract`/`audit` section with the two-command replay lifecycle and retain `run` as the sole extraction/audit transaction.
- `skills/pageledger/SKILL.md`: add bundle/replay to command table and operating loop; remove staged `extract`/`audit` from future targets.

Do not use “full reproducibility” as a current capability.

- [ ] **Step 4: Extend wheel/sdist package and smoke gates**

The existing `schemas/*.json` packaging rule should include the new files without a new manifest rule. Update CI and publish checks to assert `manifest.schema.json`, `bundle.schema.json`, and `replay.schema.json` are installed.

Extend both clean-wheel smoke paths after `run`:

```bash
pageledger verify-run "$RUN"
pageledger bundle "$RUN" --out "$BUNDLE" --json
mv "$SOURCE" "$SOURCE.moved"
pageledger replay "$BUNDLE" --out "$REPLAY" --json
pageledger verify-run "$REPLAY"
```

Use only each workflow's existing temporary `/tmp` fixtures and keep the publish workflow's build-once/publish-same-artifact guarantees intact.

- [ ] **Step 5: Run docs smoke, full quality, package, and release gates**

Run:

```bash
.venv/bin/python -m pytest tests/pageledger/ -q -m "not stress"
ruff check pageledger/ tests/ examples/ scripts/
.venv/bin/python -m mypy pageledger
.venv/bin/python scripts/check_release.py v0.4.0
.venv/bin/python -m build
.venv/bin/twine check dist/*
git diff --check
```

Inspect wheel and sdist inventories and assert both new schemas are present and `.planning`, `.superpowers`, run directories, sources, and credentials are absent.

Expected: all tests/checks pass; only the known macOS/Python 3.14 permission-cleanup warnings may remain non-fatal.

- [ ] **Step 6: Run the holistic simplicity audit**

Record the audit in the implementation report:

```text
SIMPLICITY REPORT:

Kept:
- Two commands, one replay.py, one bundle index, one replay result: each is required by the verified replay lifecycle.
- Existing run/verify/compare/align engines: they remain the only behavior sources.

Simplified:
- Portable transport uses a directory and stdlib hashing/copying, not an archive or dependency manager.
- Secret handling uses one narrow structural denylist, not value scanning or redaction.

Removed:
- Staged extract/audit roadmap target and any unused helper/configuration introduced during implementation.
- Any speculative archive, signature, thin-bundle, lineage, sandbox, provider registry, or environment-installation code.

Verdict: Pass only if every retained abstraction is used by the shipped two-command workflow and no new runtime dependency exists.
```

- [ ] **Step 7: Commit**

```bash
git add README.md docs skills/pageledger/SKILL.md CHANGELOG.md CITATION.cff pyproject.toml pageledger/_version.py uv.lock tests/pageledger/test_dry_run.py tests/pageledger/test_release.py .github/workflows/ci.yml .github/workflows/publish.yml
git commit -m "release: prepare PageLedger 0.4.0"
```

## Plan Self-Review

- Spec coverage: profiles, exact/evidence tiers, directory bundle, source relocation, route/page preservation, alignment replay, raw comparison, optional linkage verification, trust boundary, CLI exits, documentation, package schemas, and release smoke all map to Tasks 1-6.
- Placeholder scan: no `TBD`, `TODO`, “implement later,” “similar to,” unspecified error-handling step, or unnamed test remains.
- Type consistency: Tasks 3-5 use the same `ReplayError`, `bundle_run`, `validate_bundle`, `replay_bundle`, `profile_sha256`, raw-count, outcome, and schema field names.
- Simplicity: one new runtime module and no dependency/command beyond the approved minimum. Validation is manual at runtime because importing jsonschema would violate the core dependency contract.
