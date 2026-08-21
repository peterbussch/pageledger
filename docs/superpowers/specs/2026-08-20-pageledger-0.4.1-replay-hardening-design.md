# PageLedger 0.4.1 replay hardening design

Status: approved architecture; written-spec review pending

Date: 2026-08-20

Target release: 0.4.1

Parent contract: `2026-08-17-pageledger-0.4.0-verified-replay-design.md`

## Summary

PageLedger 0.4.1 is a patch hardening release for verified replay. It keeps the
0.4.0 command, bundle, schema, and outcome contracts, while closing three
integrity gaps found during adversarial review:

1. Replay currently executes in the caller's Python process, where a transitive
   adapter dependency imported from one trusted path can survive and be reused
   after replay selects another trusted path.
2. Replay preflight and extraction can construct different adapter instances,
   allowing a stateful factory to attest one implementation and execute another.
3. Run verification validates replay evidence more weakly than replay creation
   and does not bind ordinary page provenance to a manifest extractor identity.

The fix is deliberately narrow:

- Run the complete replay transaction in one fresh, isolated child interpreter.
- Load and profile the effective adapter once, then give that same object and
  profile to the existing runner.
- Reuse one canonical profile validator and require every nonempty provenance
  extractor identity to be represented in the manifest.
- Treat the worker result as an untrusted, versioned, fail-closed envelope.

No public command, flag, dependency, artifact schema, replay outcome, or adapter
protocol is added.

## Guarantee

The 0.4.0 verified-replay guarantee remains the product contract:

- A deterministic replay is `exact` only after compatible material-runtime
  preflight, a verified replay run, and byte equality for every compared raw
  artifact.
- A nondeterministic or cloud replay is `evidence_compared`; PageLedger records
  what was equal, different, or missing without claiming output identity.
- An incompatible deterministic environment fails before extraction.
- A deterministic raw mismatch fails after preserving the replay run for audit.

0.4.1 strengthens the trustworthiness of that decision. It does not claim a
hermetic environment, adapter authenticity, process sandboxing, or immunity to
concurrent filesystem mutation.

## Compatibility

The following remain unchanged:

- `pageledger bundle RUN_DIR --out BUNDLE_DIR [--json]`
- `pageledger replay BUNDLE_DIR --out NEW_RUN_DIR [--adapter-path PATH] [--json]`
- Bundle schema and directory layout.
- Manifest, provenance, comparison, and replay-evidence schemas.
- Adapter `reproducibility_profile()` protocol.
- Replay outcomes and exit-code meanings.
- Generation-zero eligibility and review-only partial-run eligibility.
- Python support declared by the package.

The only user-visible refinement is human replay output: it reports the outcome
plus raw equal, different, and missing counts. JSON output remains the structured
replay result already defined by 0.4.0.

## Threat model

0.4.1 must resist these in-scope failures:

- A same-named or independently named transitive adapter dependency lingering in
  `sys.modules` from an earlier trusted adapter path.
- Caller `sys.path`, current directory, `PYTHONPATH`, `.pth` files, or
  `sitecustomize` influencing replay imports.
- One adapter instance passing preflight while another performs extraction.
- A forged or malformed profile passing the verifier's weaker duplicate logic.
- Manifest and replay evidence agreeing with each other while page provenance
  records a different extractor identity.
- A missing, malformed, oversized, stale, or contradictory child result being
  interpreted as successful replay.
- Child output flooding or accidental success inference from stdout/stderr.

The following remain out of scope and must be documented honestly:

- Cryptographic signatures, authorship, or authenticity.
- Containers, environment capture, dependency installation, or package locking.
- Automatic transitive import-closure discovery or hashing.
- Credential isolation, network isolation, or suppression of cloud side effects.
- Filesystem snapshots, run-directory locks, or a general concurrent-mutation
  solution.
- New Windows CI solely for this patch. The implementation must nevertheless use
  portable Python APIs and argument lists without shell parsing.

## Architecture

### Parent replay coordinator

The public `replay_bundle()` function becomes a parent-side coordinator. It does
not parse the bundle, load adapters, create the replay output, or perform replay
verification itself.

It:

1. Resolves the bundle, output, and optional adapter-path arguments to absolute
   paths.
2. Creates a private temporary working directory outside the bundle and output.
3. Generates an unpredictable request identifier and selects a result-file path
   within that temporary directory.
4. Starts a child using `sys.executable`, argument-list invocation, a neutral
   temporary current directory, closed standard input, and discarded standard
   output/error.
5. Reads and validates the result envelope only after the child exits.
6. Returns the validated replay result or raises a stable `ReplayError`.

The parent never infers success from console text. It applies a one-megabyte cap
before parsing the result file.

### Isolated bootstrap

The child command uses both isolated and no-site modes:

```text
sys.executable -I -S -c STATIC_BOOTSTRAP ...
```

`-I` removes caller-controlled path and environment influence. `-S` prevents
automatic `site` processing, including `.pth` files and `sitecustomize`.

The static bootstrap receives only explicit arguments. It retains the standard
library entries already supplied by the interpreter and prepends these resolved,
deduplicated roots:

1. The trusted PageLedger package root required to import the exact running
   PageLedger code, including source/editable-checkout execution.
2. The parent interpreter's `purelib` and `platlib` roots from `sysconfig`, needed
   for installed runtime dependencies such as PyYAML.

It then invokes the private `pageledger._replay_worker` module through `runpy`.
It does not call `site`, evaluate `.pth` files, read `PYTHONPATH`, import adapter
code, or embed bundle data in executable source.

An adapter installed only through editable-install `.pth` behavior may therefore
be unavailable. The replay fails closed; users must install it normally or name a
trusted local directory with `--adapter-path`.

### Private worker

`pageledger/_replay_worker.py` is an internal process entrypoint, not a public CLI
command or library API. The existing in-process replay body moves behind a
private `_replay_bundle_in_process()` function and is called only by the worker.

The worker:

1. Parses exact positional arguments supplied by the parent.
2. Executes the existing bundle validation, preflight, runner, verifier, compare,
   and replay-linkage transaction.
3. Writes one versioned response envelope atomically using a temporary sibling
   and `os.replace()`.
4. Exits zero only after a valid success envelope is committed.
5. Converts `ReplayError` to a structured error envelope preserving its public
   code and safe message.
6. Converts unexpected exceptions to a generic `replay_worker_failed` response;
   exception representations and tracebacks do not cross the boundary.

### Worker envelope

The internal protocol starts at version `0.1`. It is not a generated PageLedger
artifact and does not receive a public JSON Schema.

A success envelope has exactly:

```json
{
  "protocol_version": "0.1",
  "request_id": "...",
  "ok": true,
  "result": {}
}
```

An error envelope has exactly:

```json
{
  "protocol_version": "0.1",
  "request_id": "...",
  "ok": false,
  "error": {"code": "...", "message": "..."}
}
```

The parent rejects unknown or missing fields, wrong types, non-finite JSON,
protocol or request mismatches, an excessive file, and malformed result/error
objects. It also requires agreement between process return code and envelope
state:

- Exit zero requires a valid success envelope.
- Nonzero exit requires a valid error envelope.
- Every other combination fails as `replay_worker_failed`.

The success result must satisfy the existing replay result shape, name an allowed
outcome, contain nonnegative integer raw counts, and resolve `out_dir` to the
requested output directory. This is defense at the process boundary, not a new
public serialization contract.

Missing, malformed, oversized, contradictory, or unexpected-crash results raise:

```text
ReplayError("replay_worker_failed", "Replay worker failed without a valid result.")
```

Known worker `ReplayError` codes and safe messages remain available to the CLI.

## One adapter object from preflight through extraction

Replay loads the effective adapter once and computes its reproducibility profile
once before the output directory exists. The exact same adapter object and
computed profile are used for extraction and persisted identity.

The runner gains a private injection seam: optional internal-only arguments for
an already loaded adapter and its already computed profile. When supplied, the
runner must not call `load_adapter()` or the profile hook again. Ordinary CLI and
library calls omit these arguments and retain existing behavior.

This is a narrow dependency injection seam, not a replay context class, adapter
registry, or second runner. It closes the attestation/execution split while
keeping the existing run controller authoritative.

## Canonical evidence validation

### Reproducibility profile

The current strict validator in `replay.py` becomes the single callable validator
for both replay creation and `verify-run`. It continues to enforce:

- Exact envelope and material field shapes.
- Supported profile version.
- Lowercase SHA-256 shapes.
- Stable material order and uniqueness.
- No stored paths, credentials, mutable aliases, or path-like material values.
- Valid PageLedger, adapter, Python, platform, and encoding evidence.
- Canonical self-hash recomputation from the profile without its hash field.

`verify-run` converts validation failures into its normal structured verification
errors. It must not implement a second subset of these rules.

An ordinary pre-0.4 run may lack a profile and remain readable. If a manifest
extractor contains a profile, however, that profile must be fully valid. Existing
replay rules still decide when a profile is required for exact eligibility.

### Manifest-to-provenance binding

For verification, PageLedger canonicalizes the extractor core identity already
present in each manifest extractor and each provenance page:

- adapter name and adapter version;
- deterministic flag;
- declared page types;
- declared capabilities.

Every nonempty provenance entry must match one complete manifest extractor core
identity. Field-by-field mixing between manifest entries is forbidden. Malformed
or conflicting manifest identities fail verification.

This is membership, not forced global uniqueness: a valid run may contain more
than one manifest extractor identity when its existing semantics support that.
Runs with no provenance remain valid when their run semantics legitimately
performed no extraction, including review-only or skip-only partial runs.

Replay verification retains its stricter rule that the effective baseline,
manifest, and replay-evidence identities agree for the replay transaction.

## Review-only replay and raw evidence

The 0.4.0 specification deliberately permits completed partial runs caused only
by explicit review routes. 0.4.1 preserves that behavior. A replay may therefore
finish with no extracted raw artifacts.

Such a replay keeps its contract-defined outcome, but the human CLI output must
include:

```text
raw equal=0 different=0 missing=0
```

Documentation must say that `exact` with `raw.equal == 0` contains no extraction
evidence. It proves the recorded no-extraction route decision repeated under the
verified replay transaction; it does not prove an extractor reproduced bytes.

## Documentation boundaries

The user documentation must state all four limitations together:

1. A bundle is unsigned. Internal hashes establish consistency with
   `bundle.json`, not authenticity, authorship, safety, or legality.
2. Material-runtime evidence covers PageLedger-owned evidence and materials an
   adapter declares. It is not automatic attestation of every imported module.
3. Replay is not a sandbox. Ambient credentials, network access, and cloud side
   effects available to the trusted adapter remain available in the worker.
4. Integrity checks validate files at inspection time. PageLedger does not lock or
   snapshot a bundle against concurrent mutation during replay.

These are contract boundaries, not deferred implementation promises.

## Failure and cleanup semantics

- Preflight incompatibility still occurs before the replay output directory is
  created.
- If extraction or finalization fails after output creation, existing run cleanup
  and inspectability semantics remain authoritative.
- The temporary worker directory is parent-owned and removed after result
  handling.
- The worker result contains no secrets, tracebacks, raw adapter output, or
  arbitrary stdout/stderr.
- The parent process's `sys.path` and `sys.modules` are never mutated by replay.
- Existing import-boundary snapshot, module-eviction, and restoration machinery
  is deleted after the child transaction replaces it.

## Test strategy

Implementation follows RED-GREEN-REFACTOR. Required regressions are:

1. Two trusted adapter directories contain identical adapter module code that
   imports the same independently named dependency, whose bytes differ between A
   and B. After A is cached in the parent, replay with B must execute B and must
   not report a false `exact`; parent import state remains unchanged.
2. A stateful adapter factory returns implementation A on first construction and
   B on second. Replay constructs and profiles exactly one object, and that object
   performs extraction.
3. The child uses `-I -S`, a neutral current directory, and explicit trusted
   roots. `PYTHONPATH`, `.pth`, and `sitecustomize` fixtures cannot influence it.
4. Missing, malformed, oversized, request-mismatched, return-code-contradictory,
   and unexpected-crash worker results fail closed. Known `ReplayError` responses
   retain their public code.
5. Source-checkout and relocated-bundle exact replay succeed through the child.
   Existing wheel and sdist replay smoke checks prove the private worker is
   packaged.
6. A coherent forgery that changes manifest plus replay evidence while leaving
   provenance unchanged fails verification.
7. Profiles with malformed materials, path values, mutable aliases, hash-only
   claims, or forged self-hashes fail in ordinary and replay verification.
8. Valid multi-identity manifest membership and empty-provenance review-only runs
   pass; missing, conflicting, or unmatched identities fail.
9. Existing deterministic mismatch, nondeterministic comparison, relocation,
   PDF-adapter, bundle-safety, and public CLI behavior remain covered.

Direct unit tests for the deleted in-process import-boundary implementation are
removed. Tests assert public behavior or the narrow worker protocol rather than
preserving obsolete machinery.

## Release gates

0.4.1 readiness requires:

- Focused replay and verification regressions.
- Full PageLedger test suite.
- Ruff and mypy.
- Release metadata consistency at `0.4.1`.
- Wheel and sdist build plus Twine checks.
- Fresh-environment wheel and sdist replay smoke tests.
- Relocated source-checkout replay smoke test.
- Independent adversarial whole-branch review.
- Holistic simplicity audit and deletion of superseded import machinery.
- Clean worktree on `codex/pageledger-0.4.1`, with no merge or push absent explicit
  authorization.

## Simplicity audit

The design earns one private worker module and one private runner seam because a
process boundary and single adapter object cannot be expressed safely with the
current functions. Everything else reuses existing replay, runner, comparison,
verification, schema, and CLI contracts.

Explicitly rejected complexity:

- no new public command or option;
- no new artifact or schema version;
- no worker framework or RPC library;
- no replay context class;
- no duplicated profile validator;
- no signature or trust store;
- no environment manager, container, or dependency crawler;
- no filesystem locking subsystem;
- no fourth replay outcome;
- no Windows-only branch or CI expansion.

The net runtime change should be simplification-positive: the private worker and
small envelope validator replace the larger, brittle `sys.modules` inspection,
eviction, descriptor-defense, and restoration code.

## Red Cell disposition

The Directorate Red Cell conditionally approved this architecture after testing
import contamination, attestation/execution separation, verifier disagreement,
worker spoofing, zero-evidence replay, packaging, platform behavior, trust claims,
and time-of-check/time-of-use exposure.

All release-blocking recommendations are incorporated here:

- `-I -S` with explicit trusted roots;
- strict atomic worker envelope and return-code agreement;
- one adapter object and profile across preflight/extraction;
- canonical profile validation plus provenance membership;
- source, wheel, and sdist startup proof;
- explicit authenticity, dependency-closure, ambient-effect, and concurrent-file
  limitations;
- preservation and disclosure of review-only zero-extraction replay.

The Red Cell's expansion candidates—signing, containers, automatic import-closure
hashing, environment installation, and snapshot locking—are rejected as outside
PageLedger's package role and the 0.4.1 patch boundary.
