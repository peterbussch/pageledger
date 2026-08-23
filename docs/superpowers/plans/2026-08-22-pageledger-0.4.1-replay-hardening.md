# PageLedger 0.4.1 Replay Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PageLedger 0.4.1 release-ready by isolating verified replay in a fresh interpreter, binding preflight to the executing adapter object, and making extractor/profile verification fail closed.

**Architecture:** The public replay API becomes a parent coordinator that starts one private `-I -S` Python worker and accepts only a strict atomic response envelope. The worker reuses the existing replay transaction, but passes one loaded adapter and one computed profile through a private runner seam. `verify-run` imports the replay module's canonical profile validator and binds every provenance extractor core to a complete manifest extractor identity.

**Tech Stack:** Python 3.10+, standard-library `subprocess`/`tempfile`/`runpy`/`sysconfig`/JSON, PyYAML, pytest, Ruff, mypy, setuptools, uv, build, Twine.

**Spec:** `docs/superpowers/specs/2026-08-20-pageledger-0.4.1-replay-hardening-design.md`

## Global Constraints

- Preserve the public `bundle` and `replay` commands, flags, result fields, schemas, outcomes, and exit meanings.
- Add no runtime dependency, public command, public option, artifact, schema version, replay outcome, environment installer, signature system, import crawler, container, or filesystem-locking subsystem.
- Run the complete replay transaction in one fresh `sys.executable -I -S` child with a neutral private current directory and no shell.
- Supply only the resolved PageLedger import root and parent interpreter `purelib`/`platlib` roots; do not process `.pth`, `sitecustomize`, or `PYTHONPATH`.
- Load and profile the replay adapter once; the same object must perform every extraction call and supply persisted identity.
- Treat the worker result as untrusted: exact fields, protocol/request agreement, one-megabyte cap, finite JSON, result/exit agreement, allowed outcomes, nonnegative counts, and expected resolved output.
- Preserve valid review-only/skip-only runs with empty provenance. Report raw counts and document that `exact` with zero equal pages contains no extraction evidence.
- Keep core dependency-light: PyYAML only; `pypdf` remains behind `[pdf]`.
- Use portable `Path` and argument-list subprocess APIs; do not add a Windows-only branch or a new CI platform.
- Follow RED-GREEN-REFACTOR. Each task receives a fresh Luna implementer, specification review, code-quality review, and holistic simplicity gate before the next task.
- Do not merge or push. Leave `codex/pageledger-0.4.1` clean.

## File Responsibility Map

| Path | 0.4.1 responsibility |
|---|---|
| `pageledger/replay.py` | Canonical profile validation, in-process replay transaction, isolated parent coordinator, strict worker-response validation |
| `pageledger/_replay_worker.py` | Minimal private child entrypoint and atomic success/error envelope writer |
| `pageledger/runner.py` | Private injection seam for one already loaded adapter and profile |
| `pageledger/verify.py` | Manifest profile validation, manifest-to-provenance identity membership, replay identity reuse |
| `pageledger/cli.py` | Human replay raw-count disclosure only |
| `tests/pageledger/test_replay.py` | Worker isolation, single-instance, envelope, relocation, failure, and regression behavior |
| `tests/pageledger/test_verify.py` | Canonical profile and extractor-evidence forgery regressions |
| `tests/pageledger/test_cli.py` | Human raw-count rendering and unchanged JSON/exit contract |
| `README.md`, `docs/`, `skills/pageledger/SKILL.md` | Honest guarantee and four explicit trust boundaries |
| `pyproject.toml`, `pageledger/_version.py`, `CITATION.cff`, `uv.lock`, `CHANGELOG.md` | 0.4.1 release identity |
| `tests/pageledger/test_dry_run.py`, `tests/pageledger/test_release.py` | Pinned 0.4.1 release evidence |

---

### Task 1: Canonicalize Profile and Extractor Evidence Verification

**Files:**
- Modify: `pageledger/replay.py` (`_validate_profile` and extractor validation)
- Modify: `pageledger/verify.py` (`verify_run`, `_check_replay_linkage`, `_manifest_replay_extractor_identity`)
- Modify: `tests/pageledger/test_verify.py`
- Modify: `tests/pageledger/test_replay.py`

**Interfaces:**
- Consumes: existing `ReplayError`, `profile_sha256()`, manifest extractor entries, and provenance extractor mappings.
- Produces: `validate_reproducibility_profile(profile: object, identity: Mapping[str, object]) -> dict[str, Any]`; one validated manifest-identity list shared by ordinary and replay verification.

- [ ] **Step 1: Add ordinary-run profile attack regressions**

Create a parameterized test in `tests/pageledger/test_verify.py` that starts from an ordinary text run, mutates `manifest.extractors[0].reproducibility_profile`, writes the manifest back, and requires `verify_run()` to fail. Cover these concrete mutations:

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "forged_self_hash",
        "path_material",
        "mutable_alias",
        "hash_only_profile",
        "adapter_identity_mismatch",
    ],
)
def test_verify_run_rejects_invalid_manifest_reproducibility_profile(
    tmp_path: Path, mutation: str
) -> None:
    from pageledger.replay import profile_sha256

    run_dir, _ = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = manifest["extractors"][0]["reproducibility_profile"]
    if mutation == "forged_self_hash":
        profile["runtime"]["release"] = "forged-release"
    elif mutation == "path_material":
        profile["materials"] = [{
            "kind": "asset", "name": "/tmp/model", "version": "1.0",
            "sha256": "0" * 64,
        }]
    elif mutation == "mutable_alias":
        profile["materials"] = [{
            "kind": "model", "name": "model", "version": "latest",
            "sha256": "0" * 64,
        }]
    elif mutation == "hash_only_profile":
        manifest["extractors"][0]["reproducibility_profile"] = {
            "profile_sha256": "0" * 64
        }
    else:
        profile["adapter"]["name"] = "forged-adapter"
    if mutation in {"path_material", "mutable_alias", "adapter_identity_mismatch"}:
        profile["profile_sha256"] = profile_sha256(profile)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = verify_run(run_dir)
    assert report["status"] == "fail"
    assert {item["code"] for item in report["errors"]} & {
        "profile_invalid", "profile_hash_mismatch"
    }
```

Do not recompute the self-hash for `forged_self_hash`; that case proves recomputation. Recompute it for the two semantic-material attacks and the adapter-identity attack so those cases cannot pass solely through a digest mismatch.

- [ ] **Step 2: Add manifest-to-provenance binding regressions**

Add three tests:

1. Change manifest adapter/version and both replay extractor identities coherently, update the manifest profile's adapter name/version, recompute its `profile_sha256`, and leave provenance unchanged; final verification must fail specifically with `extractor_identity_mismatch`, not a profile error.
2. On an ordinary two-page run, append a second complete manifest identity with a different version, update that copied profile's adapter version and canonical self-hash, and change one provenance page's `adapter_version` to the new value. Keep the adapter name unchanged so existing quality/provenance adapter agreement remains valid. Verification must pass, proving membership rather than global uniqueness without weakening replay's stricter one-effective-identity rule.
3. Verify the existing routed review-only fixture with empty provenance still passes.

Canonicalize provenance's `adapter_version` to manifest's `version` explicitly. Membership covers exactly:

```python
CORE_FIELDS = (
    "adapter",
    "version",
    "deterministic",
    "input_types",
    "output_types",
    "capabilities",
)
```

Do not include per-page `model` or `prompt_hash`, and do not include manifest-only `options` or `reproducibility_profile` in page membership.

- [ ] **Step 3: Run the new tests and prove RED**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_verify.py \
  -k "reproducibility_profile or extractor_identity or review_only" -q
```

Expected: the forged self-hash/semantic profiles and coherent manifest/replay forgery currently pass verification, so the new rejection assertions fail. Existing review-only behavior passes.

- [ ] **Step 4: Promote the replay validator without duplicating policy**

In `pageledger/replay.py`, rename `_validate_profile()` to:

```python
def validate_reproducibility_profile(
    profile: object,
    identity: Mapping[str, object],
) -> dict[str, Any]:
    """Validate and return a strict, self-hashing profile envelope."""
```

Keep the existing exact-key, material, path, mutable-alias, identity, and self-hash rules. Return the validated mapping after the existing checks. Update replay's `_validate_extractor_identity()` to call this function. Do not create a second profile module or exception type.

- [ ] **Step 5: Validate manifest identities once in `verify-run`**

Import `ReplayError` and `validate_reproducibility_profile` into `verify.py`. Replace `_manifest_replay_extractor_identity(manifest, errors)` with:

```python
def _manifest_extractor_identities(
    manifest: dict[str, Any], errors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    entries = manifest.get("extractors")
    if not isinstance(entries, list) or not entries:
        _add(errors, "extractor_identity_mismatch", "Run manifest has no extractor entries")
        return []
    identities: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            _add(errors, "extractor_identity_mismatch", "Run manifest extractor is malformed")
            continue
        profile = entry.get("reproducibility_profile")
        profile_hash = None
        if profile is not None:
            try:
                validated_profile = validate_reproducibility_profile(profile, entry)
            except ReplayError as exc:
                _add(errors, exc.code, str(exc), artifact="manifest.json")
                continue
            profile_hash = validated_profile["profile_sha256"]
        candidate = {
            "adapter": entry.get("adapter"),
            "version": entry.get("version"),
            "deterministic": entry.get("deterministic"),
            "input_types": entry.get("input_types"),
            "output_types": entry.get("output_types"),
            "capabilities": entry.get("capabilities"),
            "options": entry.get("options", {}),
            "reproducibility_profile_sha256": profile_hash,
        }
        validated = _validate_replay_extractor_identity(
            candidate, "manifest extractor", errors
        )
        if validated is not None:
            identities.append(validated)
    return identities

def _manifest_replay_extractor_identity(
    identities: list[dict[str, Any]], errors: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not identities:
        return None
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        _add(
            errors,
            "replay_linkage_mismatch",
            "Run manifest extractor entries disagree",
            artifact="manifest.json",
        )
    return first
```

The first function fully validates every manifest extractor, calls the canonical profile validator whenever a profile is present, and returns full replay identities containing `reproducibility_profile_sha256`. It records `profile_invalid` or `profile_hash_mismatch` using the original `ReplayError.code` and a safe message. Call it once from `verify_run()` and pass the result to replay-linkage verification.

- [ ] **Step 6: Bind each provenance extractor to manifest membership**

Add a focused helper in `verify.py`:

```python
def _canonical_extractor_core(
    extractor: object, *, provenance: bool
) -> tuple | None:
    if not isinstance(extractor, dict):
        return None
    version_key = "adapter_version" if provenance else "version"
    adapter = extractor.get("adapter")
    version = extractor.get(version_key)
    deterministic = extractor.get("deterministic")
    if (
        not isinstance(adapter, str)
        or not adapter
        or not isinstance(version, str)
        or not version
        or not isinstance(deterministic, bool)
    ):
        return None
    lists: list[tuple] = []
    for field in ("input_types", "output_types", "capabilities"):
        values = extractor.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            return None
        lists.append(tuple(sorted(values)))
    return (adapter, version, deterministic, *lists)


def _check_provenance_extractor_membership(
    provenance: dict[str, dict[str, Any]],
    manifest_identities: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    manifest_cores = {
        _canonical_extractor_core(identity, provenance=False)
        for identity in manifest_identities
    }
    for page_id, entry in provenance.items():
        extractor = entry.get("extractor")
        core = _canonical_extractor_core(extractor, provenance=True)
        if core is None or core not in manifest_cores:
            _add(
                errors,
                "extractor_identity_mismatch",
                f"Provenance extractor is not declared by the manifest for {page_id}",
                artifact="provenance.jsonl",
                page_id=page_id,
            )
```

Use a tuple of scalar values plus tuples for the three list fields so set membership stays simple. Empty provenance performs no membership check and remains valid. Malformed manifest identities leave the membership set incomplete and never produce a false pass.

- [ ] **Step 7: Run focused and neighboring verification tests**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_verify.py tests/pageledger/test_replay.py \
  tests/pageledger/test_schemas.py -q
uv run --frozen --extra dev --extra pdf ruff check \
  pageledger/replay.py pageledger/verify.py \
  tests/pageledger/test_verify.py tests/pageledger/test_replay.py
uv run --frozen --extra dev --extra pdf mypy pageledger/replay.py pageledger/verify.py
```

Expected: PASS. Ordinary manifests without a profile remain readable; every present profile receives strict validation; review-only empty provenance remains valid.

- [ ] **Step 8: Simplicity gate and commit**

Confirm `verify.py` has one profile-policy call path and no duplicate material/path/self-hash rules. Keep the two identity helpers only because one produces validated full identities and the other enforces replay-wide equality.

```bash
git add pageledger/replay.py pageledger/verify.py \
  tests/pageledger/test_verify.py tests/pageledger/test_replay.py
git commit -m "fix: bind replay extractor evidence"
```

---

### Task 2: Reuse One Replay Adapter Through Extraction

**Files:**
- Modify: `pageledger/runner.py` (`run` adapter setup)
- Modify: `pageledger/replay.py` (`replay_bundle` transaction call to `run`)
- Modify: `tests/pageledger/test_replay.py`

**Interfaces:**
- Consumes: effective adapter/options already established by replay preflight.
- Produces: private runner parameters `_loaded_adapter: Any | None = None` and `_reproducibility_profile: dict[str, Any] | None = None`.

- [ ] **Step 1: Add the stateful-construction regression**

Add a custom adapter fixture whose constructor increments a file and whose instance captures `variant-{count}`. Build the baseline, bundle it, reset the counter to `0`, replay it, and assert:

```python
result = replay_bundle(bundle_dir, replayed, adapter_path=adapter_dir)
assert result["outcome"] == "exact"
assert counter.read_text(encoding="utf-8") == "1"
assert (replayed / "raw" / "doc_0001_page_0001.txt").read_text(
    encoding="utf-8"
) == "variant-1"
```

The adapter's class code, identity, and empty material profile remain constant. With current code, preflight constructs `variant-1`, `runner.run()` constructs `variant-2`, the counter becomes `2`, and exact replay fails.

- [ ] **Step 2: Run the stateful regression and prove RED**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py \
  -k "single_adapter_instance" -q
```

Expected: FAIL with two constructions and/or `deterministic_mismatch`.

- [ ] **Step 3: Add the private runner seam**

Extend `run()` after the existing public keyword arguments:

```python
def run(
    *,
    inputs: list[Path],
    config_path: Path,
    out_dir: Path,
    dry_run: bool,
    log_level: str = "INFO",
    pages: str | None = None,
    page_selection: list[dict[str, Any]] | None = None,
    parent_run_id: str | None = None,
    parent_quality_by_page: dict[str, dict[str, Any]] | None = None,
    source_page_counts: dict[Path, int] | None = None,
    run_depth: int = 0,
    adapter_path: Path | None = None,
    routes_path: Path | None = None,
    _loaded_adapter: Any | None = None,
    _reproducibility_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

Select the adapter/profile once:

```python
adapter = _loaded_adapter
if adapter is None and (not dry_run or routes_path is not None):
    if effective_adapter_name is not None:
        adapter = load_adapter(effective_adapter_name, effective_adapter_options)

adapter_profile = (
    _reproducibility_profile
    if _loaded_adapter is not None
    else build_reproducibility_profile(adapter)
    if adapter is not None and not dry_run
    else None
)
```

Preserve all ordinary run behavior. The private seam is not exposed through CLI/config and does not create a wrapper class.

- [ ] **Step 4: Pass the attested adapter and profile into the runner**

In replay's call to `run()`, add:

```python
run_kwargs["_loaded_adapter"] = adapter
run_kwargs["_reproducibility_profile"] = local_profile
```

The replay transaction continues to load the adapter and compute the profile before output creation. It must not call either operation again.

- [ ] **Step 5: Run focused and runner regression suites**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py tests/pageledger/test_dry_run.py \
  tests/pageledger/test_rerun.py -q
uv run --frozen --extra dev --extra pdf ruff check \
  pageledger/runner.py pageledger/replay.py tests/pageledger/test_replay.py
uv run --frozen --extra dev --extra pdf mypy pageledger/runner.py pageledger/replay.py
```

Expected: PASS, including the counter staying at one during replay.

- [ ] **Step 6: Simplicity gate and commit**

Reject an adapter context object, factory registry, or second runner. The two private arguments are the whole seam.

```bash
git add pageledger/runner.py pageledger/replay.py tests/pageledger/test_replay.py
git commit -m "fix: reuse the attested replay adapter"
```

---

### Task 3: Add the Private Replay Worker Protocol

**Files:**
- Create: `pageledger/_replay_worker.py`
- Modify: `pageledger/replay.py` (`replay_bundle` transaction extraction)
- Modify: `tests/pageledger/test_replay.py`

**Interfaces:**
- Consumes: current replay transaction and `ReplayError`.
- Produces: `_replay_bundle_in_process(bundle_dir, out_dir, *, adapter_path=None) -> dict[str, Any]`; worker protocol `0.1`; `pageledger._replay_worker.main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Add direct worker-envelope tests**

Test the private worker module with its in-process function monkeypatched. Require exact success and known-error envelopes:

```python
success = {
    "outcome": "exact",
    "run_id": "run-child",
    "out_dir": str(out_dir.resolve()),
    "baseline_run_id": "run-base",
    "bundle_manifest_sha256": "0" * 64,
    "profile_match": True,
    "raw": {
        "equal": 1,
        "different": 0,
        "missing": 0,
        "different_page_ids": [],
        "missing_page_ids": [],
    },
}
assert worker.main([request_id, str(result_path), str(bundle), str(out_dir), ""]) == 0
assert json.loads(result_path.read_text(encoding="utf-8")) == {
    "protocol_version": "0.1",
    "request_id": request_id,
    "ok": True,
    "result": success,
}
```

For `ReplayError("incompatible_environment", "profile mismatch")`, require exit 1 and exact `error: {code, message}`. For `RuntimeError("secret details")`, require exit 1, code `replay_worker_failed`, the stable generic message, and absence of `secret details`.

- [ ] **Step 2: Run worker tests and prove RED**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py -k "worker_envelope" -q
```

Expected: FAIL because `pageledger._replay_worker` does not exist.

- [ ] **Step 3: Separate the existing in-process transaction**

Rename the current replay body to:

```python
def _replay_bundle_in_process(
    bundle_dir: Path,
    out_dir: Path,
    *,
    adapter_path: Path | None = None,
) -> dict[str, Any]:
```

For this task only, keep `replay_bundle()` as a direct delegating wrapper so public behavior remains green before the process-boundary switch:

```python
def replay_bundle(
    bundle_dir: Path,
    out_dir: Path,
    *,
    adapter_path: Path | None = None,
) -> dict[str, Any]:
    return _replay_bundle_in_process(bundle_dir, out_dir, adapter_path=adapter_path)
```

- [ ] **Step 4: Implement the minimal worker entrypoint**

Define the shared internal protocol constants once in `pageledger/replay.py`:

```python
_WORKER_PROTOCOL_VERSION = "0.1"
_WORKER_GENERIC_CODE = "replay_worker_failed"
_WORKER_GENERIC_MESSAGE = "Replay worker failed without a valid result."
```

Import those constants, `_atomic_write_json`, `_replay_bundle_in_process`, and `ReplayError` into the private worker. This keeps protocol text and atomic JSON writing single-source.

`main()` accepts exactly five positional values after the module name:

```text
REQUEST_ID RESULT_PATH BUNDLE_DIR OUT_DIR ADAPTER_PATH_OR_EMPTY
```

Call `_replay_bundle_in_process()`, write exactly one success or error envelope with the existing `_atomic_write_json()` helper, and return 0 only for success. Catch `ReplayError` separately; catch every other exception without including its representation or traceback. End with:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run direct worker and public replay tests**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py -q
uv run --frozen --extra dev --extra pdf ruff check \
  pageledger/replay.py pageledger/_replay_worker.py tests/pageledger/test_replay.py
uv run --frozen --extra dev --extra pdf mypy \
  pageledger/replay.py pageledger/_replay_worker.py
```

Expected: PASS. Public replay is still in-process at this checkpoint; the private worker protocol is independently executable and packaged by the existing `pageledger*` package discovery.

- [ ] **Step 6: Simplicity gate and commit**

Keep the worker free of argparse, logging, RPC libraries, schema machinery, adapter logic, and replay policy. It only parses fixed arguments, calls one function, and atomically writes one envelope.

```bash
git add pageledger/_replay_worker.py pageledger/replay.py \
  tests/pageledger/test_replay.py
git commit -m "refactor: add the private replay worker"
```

---

### Task 4: Switch Public Replay to the Isolated Worker

**Files:**
- Modify: `pageledger/replay.py`
- Modify: `pageledger/_replay_worker.py`
- Modify: `tests/pageledger/test_replay.py`

**Interfaces:**
- Consumes: worker protocol `0.1` and `_replay_bundle_in_process()` from Task 3.
- Produces: isolated public `replay_bundle()` and strict `_read_worker_response(path: Path, *, expected_root: Path, request_id: str, returncode: int, expected_out: Path) -> dict[str, Any]`.

- [ ] **Step 1: Replace the stale-transitive-dependency test with the real exploit**

Create trusted adapter directories A and B. Both contain byte-identical `adapter_module.py`, which imports `VALUE` from `shared_dependency`. A's dependency defines `VALUE = "A"`; B's defines `VALUE = "B"`. The adapter returns `VALUE` and declares an empty material profile.

Build the baseline with A, cache A's `shared_dependency` in the parent, then replay with B:

```python
result = replay_bundle(bundle_dir, replayed, adapter_path=adapter_b)
assert result["outcome"] == "deterministic_mismatch"
assert result["raw"]["different"] == 1
assert (replayed / "raw" / "doc_0001_page_0001.txt").read_text(
    encoding="utf-8"
) == "B"
assert sys.modules["shared_dependency"] is cached_a_dependency
assert sys.path == original_path
```

The result must not be false `exact`. This test intentionally demonstrates that undeclared transitive dependency bytes are outside profile attestation while proving the requested trusted path actually executes.

- [ ] **Step 2: Add strict parent-response table tests**

Exercise `_read_worker_response()` with:

- missing result file;
- malformed JSON;
- a file larger than `1_048_576` bytes;
- unknown or missing envelope fields;
- protocol mismatch;
- request-ID mismatch;
- exit 0 plus error envelope;
- nonzero exit plus success envelope;
- invalid result outcome/count/path fields;
- valid known `ReplayError` envelope;
- valid success envelope.

Every malformed/contradictory case must raise exactly:

```python
assert error.value.code == "replay_worker_failed"
assert str(error.value) == "Replay worker failed without a valid result."
```

The known error case must preserve its safe code/message. The valid success case returns the existing public result mapping unchanged.

- [ ] **Step 3: Add startup/isolation and source-checkout tests**

Add one test that captures the subprocess argument list and requires `sys.executable`, `-I`, `-S`, `-c`, explicit resolved roots, `stdin/stdout/stderr=subprocess.DEVNULL`, `check=False`, and a private cwd distinct from the caller/bundle/output.

Add a real replay test with `PYTHONPATH` pointing at a directory containing a poison `pageledger` package and a `sitecustomize.py` that would create a marker. Require exact replay and assert the marker does not exist. Retain the existing relocated source-checkout exact replay as the end-to-end import proof.

- [ ] **Step 4: Run the isolation regressions and prove RED**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py \
  -k "transitive_dependency or worker_response or isolated_startup or sitecustomize or relocated_text" -q
```

Expected: current public replay fails the command/isolation tests and reports false `exact` for the stale transitive dependency.

- [ ] **Step 5: Build the static isolated command**

In `replay.py`, add a constant bootstrap that does only this:

```python
_WORKER_BOOTSTRAP = """\
import runpy
import sys
count = int(sys.argv[1])
roots = sys.argv[2:2 + count]
worker_args = sys.argv[2 + count:]
sys.path[:0] = [root for root in roots if root not in sys.path]
sys.argv = ["pageledger._replay_worker", *worker_args]
runpy.run_module("pageledger._replay_worker", run_name="__main__")
"""
```

Build a deduplicated resolved root list from `Path(__file__).resolve().parent.parent` and `sysconfig.get_paths()` keys `purelib` and `platlib`. Do not include cwd, environment paths, bundle paths, or adapter paths in this bootstrap list.

- [ ] **Step 6: Implement the parent coordinator**

Resolve input paths in the parent. Create a private `TemporaryDirectory`, request ID, and result path. Invoke:

```python
completed = subprocess.run(
    command,
    cwd=temporary_root,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
```

Use an argument list with no shell. Convert subprocess startup failures into the generic `ReplayError`. The child receives absolute bundle/output/adapter paths, so neutral cwd cannot change their meaning.

- [ ] **Step 7: Implement exact fail-closed response validation**

`_read_worker_response()` must:

1. require `path.parent.resolve() == expected_root.resolve()` and a regular non-symlink result file;
2. reject size greater than `1_048_576` before reading;
3. parse finite JSON and require an object;
4. require exact envelope fields and protocol/request match;
5. require exit/envelope agreement;
6. validate exact public result/error fields;
7. require allowed outcome, boolean/null profile match, SHA-256 bundle hash, nonempty IDs, nonnegative non-bool raw counts, sorted unique page-ID lists, and expected resolved `out_dir`;
8. return success or raise the known safe worker error.

Use the stable generic error for every boundary contradiction. Do not infer success from the existence of the requested run directory.

- [ ] **Step 8: Move trusted adapter-path activation wholly into the child**

Inside `_replay_bundle_in_process()`, resolve and reject a trusted path equal to, inside, or above the bundle before inserting it at the start of child `sys.path`. Pass the already loaded adapter/profile to `run()` and do not pass an adapter path that would trigger a second import decision.

- [ ] **Step 9: Delete the obsolete in-process import simulator**

Delete these functions and their direct implementation tests:

```text
_bundle_import_boundary
_custom_adapter_module
_evict_module_root
_paths_related
_reject_cached_bundle_modules
_origin_values
_origin_in_bundle
_bundle_import_forbidden
```

Remove imports used only by that machinery: `_NamespacePath`, `contextmanager`, `ModuleSpec`, `FunctionType`, `ModuleType`, and `NoReturn`. Retain public bundle-safety tests and replace direct import-boundary tests with worker-boundary tests.

Rewrite the old parent-monkeypatch profile-mismatch test as a real custom adapter fixture whose declared material/profile differs between baseline and replay; child process boundaries cannot inherit parent monkeypatches.

- [ ] **Step 10: Run the full replay/CLI/verifier neighborhood**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py tests/pageledger/test_verify.py \
  tests/pageledger/test_cli.py tests/pageledger/test_schemas.py \
  tests/pageledger/test_aligner.py tests/pageledger/test_compare.py -q
uv run --frozen --extra dev --extra pdf ruff check \
  pageledger/replay.py pageledger/_replay_worker.py pageledger/runner.py \
  tests/pageledger/test_replay.py tests/pageledger/test_verify.py
uv run --frozen --extra dev --extra pdf mypy pageledger/
```

Expected: PASS. The parent import state is unchanged, B executes in the exploit, malformed workers fail closed, and relocated replay remains exact.

- [ ] **Step 11: Simplicity gate and commit**

Compare removed and added machinery. The worker/response code must replace—not wrap—the module descriptor scanner. Reject configurable protocols, timeouts, logging channels, child classes, and transport abstractions.

```bash
git add pageledger/replay.py pageledger/_replay_worker.py \
  tests/pageledger/test_replay.py
git commit -m "fix: isolate replay in a fresh interpreter"
```

---

### Task 5: Expose the Honest 0.4.1 Contract and Bump the Release

**Files:**
- Modify: `pageledger/cli.py`
- Modify: `tests/pageledger/test_cli.py`
- Modify: `README.md`
- Modify: `docs/cli.md`
- Modify: `docs/artifacts.md`
- Modify: `docs/capabilities-and-limits.md`
- Modify: `docs/design.md`
- Modify: `docs/adapter-protocol.md`
- Modify: `docs/run-manifest-spec.md`
- Modify: `docs/examples/pageledger.yml`
- Modify: `skills/pageledger/SKILL.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `pageledger/_version.py`
- Modify: `CITATION.cff`
- Modify: `uv.lock`
- Modify: `tests/pageledger/test_dry_run.py`
- Modify: `tests/pageledger/test_release.py`
- Modify if its example remains stale: `.github/workflows/publish.yml`

**Interfaces:**
- Consumes: unchanged replay result mapping from Task 4.
- Produces: human raw-count disclosure and consistent PageLedger `0.4.1` release metadata/documentation.

- [ ] **Step 1: Change the CLI expectation first**

Update `test_replay_human_exact_output` to require:

```text
Verified replay outcome: exact
Raw comparison: 1 equal / 0 different / 0 missing
```

Add or extend the routed review-only CLI test to require:

```text
Raw comparison: 0 equal / 0 different / 0 missing
```

Keep JSON output byte-shape expectations unchanged.

- [ ] **Step 2: Run the CLI tests and prove RED**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_cli.py -k "replay" -q
```

Expected: human-output assertions fail because only the outcome is currently printed.

- [ ] **Step 3: Print raw counts without changing exit behavior**

After the existing outcome line in `_cmd_replay()`:

```python
raw = result["raw"]
print(
    "Raw comparison: "
    f"{raw['equal']} equal / {raw['different']} different / "
    f"{raw['missing']} missing"
)
```

Return 0 only for `exact`/`evidence_compared`, 1 for deterministic mismatch/errors, and leave argparse exit 2 unchanged.

- [ ] **Step 4: Document the four trust boundaries once clearly and reference them elsewhere**

Make `docs/capabilities-and-limits.md` the concise authoritative user boundary and ensure `README.md`, `docs/cli.md`, `docs/artifacts.md`, `docs/design.md`, `docs/adapter-protocol.md`, `docs/run-manifest-spec.md`, and `skills/pageledger/SKILL.md` do not contradict it. State explicitly:

1. bundle hashes prove internal consistency, not authenticity/authorship;
2. profiles attest PageLedger evidence plus adapter-declared materials, not every imported dependency;
3. the worker is not a credential/network/cloud-side-effect sandbox;
4. checks are at-rest observations, not a lock/snapshot against concurrent mutation;
5. `exact` with `raw.equal == 0` proves no extraction bytes;
6. isolated startup does not process editable-install `.pth` hooks; adapter code must be normally installed or supplied with `--adapter-path`.

Do not duplicate long threat-model prose in every document. Use short links to the authoritative boundary where possible.

- [ ] **Step 5: Bump every release identity to 0.4.1**

Update:

```text
pyproject.toml project.version
pageledger/_version.py __version__
CITATION.cff version and date-released (2026-08-22)
uv.lock editable pageledger version
tests/pageledger/test_dry_run.py pinned exported version
tests/pageledger/test_release.py current release tag
docs/capabilities-and-limits.md heading
docs/examples/pageledger.yml package-version comment
```

Add `## 0.4.1 - 2026-08-22` to `CHANGELOG.md`, describing the fresh-interpreter replay boundary, single adapter instance, canonical verification, provenance binding, raw-count disclosure, and unchanged public/schema contract.

Change the publish-workflow example tag from `v0.4.0` to `v0.4.1` only if it is still release-specific rather than intentionally generic.

- [ ] **Step 6: Run release, docs-smoke, CLI, and metadata tests**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_cli.py tests/pageledger/test_dry_run.py \
  tests/pageledger/test_metadata.py tests/pageledger/test_release.py -q
uv run --frozen --extra dev python scripts/check_release.py v0.4.1
uv run --frozen --extra dev --extra pdf ruff check \
  pageledger/cli.py tests/pageledger/test_cli.py
```

Expected: PASS and `release check: v0.4.1 metadata agrees`.

- [ ] **Step 7: Search for stale claims and simplify the documentation diff**

```bash
rg -n "0\.4\.0|hermetic|authenticity|authorship|sitecustomize|raw\.equal|Raw comparison" \
  README.md docs skills/pageledger/SKILL.md pyproject.toml pageledger \
  tests/pageledger CHANGELOG.md CITATION.cff uv.lock .github/workflows
git diff --check
```

Classify remaining `0.4.0` references: retain historical changelog/spec/plan entries; update only current release claims. Remove repeated limitation paragraphs in favor of one authoritative paragraph plus links.

- [ ] **Step 8: Commit the release contract**

Stage only files actually changed:

```bash
git add pageledger/cli.py tests/pageledger/test_cli.py README.md \
  docs/cli.md docs/artifacts.md docs/capabilities-and-limits.md \
  docs/design.md docs/adapter-protocol.md docs/run-manifest-spec.md \
  docs/examples/pageledger.yml skills/pageledger/SKILL.md \
  CHANGELOG.md pyproject.toml \
  pageledger/_version.py CITATION.cff uv.lock \
  tests/pageledger/test_dry_run.py tests/pageledger/test_release.py
git commit -m "chore: prepare PageLedger 0.4.1"
```

If `.github/workflows/publish.yml` needed the example update, include it explicitly in the same commit.

---

### Task 6: Dogfood and Prove 0.4.1 Release Readiness

**Files:**
- Verify: all tracked source, tests, docs, schemas, workflows, and distributions
- Modify only when a gate exposes a real defect; add the smallest regression with each fix

**Interfaces:**
- Consumes: Tasks 1-5 and the approved 0.4.1 spec.
- Produces: requirement-by-requirement readiness evidence on a clean unpushed branch.

Dogfood is a falsification gate, not a demonstration script. Any mismatch between documented behavior and recorded artifacts blocks readiness until reproduced, fixed with a regression, and rerun.

- [ ] **Step 1: Run the complete source gates**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest tests/pageledger/ -q
uv run --frozen --extra dev --extra pdf ruff check pageledger/ tests/ examples/ scripts/
uv run --frozen --extra dev --extra pdf mypy pageledger/
uv run --frozen --extra dev python scripts/check_release.py v0.4.1
git diff --check
```

Expected: all tests pass with only the seven already-known macOS subprocess permission-cleanup warnings; Ruff, mypy, release metadata, and whitespace checks pass.

- [ ] **Step 2: Build and inspect both distributions**

Use a fresh temporary distribution directory so stale artifacts cannot satisfy the gate:

```bash
PAGELEDGER_DIST_DIR="$(mktemp -d /tmp/pageledger-0.4.1-dist.XXXXXX)"
uv run --frozen --extra dev python -m build --outdir "$PAGELEDGER_DIST_DIR"
uv run --frozen --extra dev twine check "$PAGELEDGER_DIST_DIR"/*
```

Inspect wheel and sdist member lists and require `pageledger/_replay_worker.py`, the package modules, and all three installed replay/manifest/bundle schemas.

- [ ] **Step 3: Re-derive and dogfood the public JFK scan across source and wheel environments**

Do not treat the ignored `runs/jfk-4p.pdf` file as self-authenticating. In a fresh `/tmp/pageledger-0.4.1-dogfood.XXXXXX` workspace, download the documented NARA record from its authoritative public URL:

```text
https://www.archives.gov/files/research/jfk/releases/2018/180-10147-10163.pdf
JFK/HSCA record: 180-10147-10163
```

Record the retrieval date, final URL, HTTP validators when supplied, complete parent byte size, SHA-256, page count, encryption/JavaScript status, and PDF metadata. Deterministically derive physical page indices 1-4 with the exact installed `pypdf` version; record the explicit selection, derivation command, fresh parent hash, and fresh slice hash. Use that freshly derived slice for dogfood. The ignored historical slice is comparison evidence, not the source of truth:

```text
SHA-256: b0d91411d1cf6bf2afbfeec56aca82b52a0bc65c4daaecf5d00041250293bbdc
PDF pages: 4
JavaScript: no
Encrypted: no
```

NARA has re-released this record and PDF serialization varies across `pypdf` versions, so byte inequality with the historical slice is expected evidence rather than failure. At a fixed local render tool/version/DPI, compare per-page render hashes for the fresh physical pages 1-4 and the historical slice. Require four matching rendered pages in order; otherwise stop because the historical selection is unproven. Never substitute the ignored slice for the freshly retrieved input.

Require local Tesseract, Poppler, and English trained data; record their versions and material hashes without recording machine paths. Keep every source, run, bundle, virtual environment, log, and raw artifact outside the repository.

Run this real lifecycle:

1. From the source checkout, generate a `pdf_ocr` config and extract all four pages.
2. Require `4` extracted, `0` failed, `0` not attempted, four nonempty raw OCR artifacts, and `verify-run` pass.
3. Inspect manifest/provenance/profile evidence: adapter `pdf_ocr`; exactly four matching page identities; source/raw hashes present; declared Tesseract, Poppler, and trained-data materials present with exact revisions/hashes.
4. Record `sys.executable`; Python implementation/version; system/release/machine; PageLedger, PyYAML, and pypdf installed versions; `pageledger.__file__`; and the baseline profile hash. Paths are reduced to source-checkout versus installed-wheel provenance in the sanitized report.
5. Bundle the verified run, move the temporary source copy so the path recorded by the baseline no longer exists, and move the bundle beneath a separate transport root.
6. Read the exact PyYAML and pypdf versions resolved in `uv.lock`. Create a fresh venv with the exact baseline interpreter; install the exact built wheel plus those literal direct-runtime pins; and assert `pageledger.__file__` is inside that venv rather than the checkout. Record the wheel SHA-256 and complete `uv pip freeze` output in sanitized package-name/version form.
7. From a neutral directory outside the checkout and with the source path still absent, replay the relocated bundle with that venv's `pageledger` executable.
8. Machine-assert PageLedger `0.4.1`, outcome `exact`, `profile_match: true`, the expected profile hash, raw counts `4 equal / 0 different / 0 missing`, exactly four nonempty replay raw artifacts, and final `verify-run` pass.
9. Run `compare-runs --json` and parse its result. Assert identical page-ID sets, four page comparisons, `4/0/0` raw equality totals, matching per-page raw SHA-256 values, and unchanged effective extractor identity. Separately parse baseline/replay provenance JSONL for four matching page/source/extractor identities and each `run.log` for exactly four terminal `extracted` statuses with no failure/not-attempted status. Inspect bounded representative OCR text plus manifest/replay/provenance linkage after those assertions; counters alone are insufficient.
10. Confirm the original temporary source path remained absent throughout replay and `git status --short` is unchanged by dogfood.

Write sanitized evidence to `.planning/2026-08-17-pageledger-0-4-0-planning/dogfood_0_4_1.md`: authoritative input provenance, parent/slice hashes and derivation, package/runtime/tool identities, wheel hash/import provenance, commands, durations, manifest summaries, parsed comparison assertions, raw hashes/counts, verifier results, bounded observations, and every discrepancy. Do not copy raw OCR, absolute machine paths, the PDF, run directories, or credentials into tracked files.

- [ ] **Step 4: Prove a positive exact replay on a second physical host**

Use the `laptop-remote-access` runbook and `Competer-2.local` as the second machine. This is a hard readiness gate, not an optional smoke. Start with `peter-laptop status` and a read-only capacity/runtime/tool probe; the laptop is near capacity, so use one explicit bounded scratch directory and copy only the exact wheel, the compact four-page JFK bundle from Step 3, and result metadata. Never copy credentials, the repository, Zotero, or the vault.

1. Reuse the verified/bundled four-page `pdf_ocr` baseline from Step 3. Record the wheel SHA-256, bundle SHA-256, baseline profile hash, interpreter/runtime tuple, exact PyYAML/pypdf pins, Tesseract/Poppler/trained-data material identities, and installed `pageledger.__file__` provenance.
2. Keep the original temporary source path absent. Its recorded mini path must not exist on the laptop.
3. Copy the exact wheel and compact four-page bundle to the laptop's bounded scratch directory. Check hashes after transport.
4. Require a laptop interpreter matching every canonical runtime field and existing Tesseract, Poppler, and English trained-data materials matching the recorded revisions and SHA-256 values. A package-manager or system-tool mutation needs its own explicit authorization; do not change the remote host merely to force the gate green.
5. Create a fresh laptop venv and install the exact wheel with literal `PyYAML==...` and `pypdf==...` pins taken from `uv.lock`. Assert the three distributions' versions, complete installed package inventory, and that PageLedger imports from the venv rather than a checkout. Do not use an editable install.
6. From a neutral laptop cwd, replay the transported OCR bundle and machine-assert `0.4.1`, `exact`, `profile_match: true`, `4 equal / 0 different / 0 missing`, identical profile hash, four nonempty replay raw artifacts, and `verify-run` pass.
7. Parse `compare-runs --json`, both provenance files, and both run logs on the laptop using the same exact assertions as Step 3. This proves external OCR material portability, not only PageLedger's dependency-light text path.
8. Copy only the JSON result, verifier/comparison results, environment/material identities, hashes, and bounded logs back to the mini. Record the remote scratch path and a safe cleanup command, but preserve evidence until the readiness review is accepted.

If SSH is unavailable, no sufficiently matching runtime can be provisioned, or the canonical profile differs, record the exact mismatch and leave 0.4.1 readiness blocked. An expected `incompatible_environment` result is useful negative evidence but does **not** substitute for this positive cross-machine replay. Revisit which runtime facts are materially required before changing the guarantee; do not waive or narrow it during release review.

- [ ] **Step 5: Smoke-test the exact sdist in a second fresh environment**

In a different fresh venv installed from the exact built `.tar.gz`, run the dependency-light two-page text lifecycle from a neutral cwd. Require PageLedger `0.4.1`, exact replay, two equal raw pages, and final `verify-run` pass. This isolates sdist packaging from the heavier real-PDF lane without duplicating the OCR dogfood.

- [ ] **Step 6: Run the relocated source-checkout proof**

```bash
uv run --frozen --extra dev --extra pdf python -m pytest \
  tests/pageledger/test_replay.py \
  -k "relocated_text_replay_is_exact_without_original_source or pdf_text_exact_replay" -q
```

Expected: PASS through the real child worker.

- [ ] **Step 7: Commission independent Luna reviews**

Dispatch two read-only reviewers after all source gates pass:

1. a whole-branch correctness/security reviewer checking every approved spec requirement and attempting the stale-import, worker-spoof, profile-forgery, provenance-forgery, and review-only attacks;
2. a holistic simplicity reviewer comparing `1a6823b..HEAD`, looking for dead import-boundary code, duplicate validators, one-use layers, public-surface expansion, and unnecessary docs/test duplication.

Accept findings only after reproducing them. Fix release blockers with a failing regression, rerun the affected task gates, and commit the focused correction.

- [ ] **Step 8: Run the final completion audit**

For every numbered requirement in the design spec, record its proving file/test/command in `.planning/2026-08-17-pageledger-0-4-0-planning/progress.md`. Then verify:

```bash
git status --short --branch
git log --oneline --decorate 1a6823b..HEAD
git diff --stat 1a6823b..HEAD
rg -n "_bundle_import_boundary|_custom_adapter_module|_evict_module_root|_paths_related|_reject_cached_bundle_modules|_origin_values|_origin_in_bundle|_bundle_import_forbidden" \
  pageledger tests/pageledger
```

Expected: clean `codex/pageledger-0.4.1`, no upstream/push, no obsolete import simulator, and only deliberate 0.4.1 commits.

- [ ] **Step 9: Final simplicity report**

Report exactly:

```text
SIMPLICITY REPORT:

Kept:
- private replay worker: required for a real interpreter boundary
- private runner seam: required to attest and execute one object
- canonical profile validator: removes verifier drift
- strict response validator: required to fail closed across the process boundary

Simplified:
- replay import safety: parent sys.modules simulation -> fresh -I -S child
- extractor verification: replay-only hash-shape check -> one canonical validation pass

Removed:
- module eviction, descriptor inspection, import-origin scanning, and restoration code
- direct tests coupled to deleted import machinery

Verdict: Pass
```

If the code cannot honestly support that report, continue simplifying and rerun the full gates before claiming readiness.
