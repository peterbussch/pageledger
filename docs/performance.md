# Performance evidence and successor protocol

PageLedger's retained optimization makes some ledger serialization faster. It
does not make OCR or provider extraction faster, and it did not meet the
original 2× ledger-throughput goal. This page separates the evidence already
collected from a proposed protocol for any later campaign.

## What the existing evidence shows

The final production comparison used seven matched, fresh-process pairs with
profiling disabled. On its 5,000-page synthetic fixture, the accepted candidate
reduced paired median ledger time by **29.993%**, a **1.428×** speedup. The
fixture contained 316,820 payload characters, about 63 per page. Comparing the
reported median ledger spans gives roughly 1.53 seconds before and 1.08 seconds
after: about **0.45 seconds saved** across all 5,000 tiny pages.

Those numbers are fixture-specific. They do not establish an end-to-end OCR
speedup, saved review time, or behavior on full-page prose, tables, or large
metadata. A separate 1,000-page synthetic fixture supplied historical
generalization evidence. The 50,000-page RSS target was not measured.

The retained change selects PyYAML's C safe dumper only when it can preserve the
existing artifact bytes. Its conservative compatibility check falls back when
any value contains whitespace or unsupported characters. Because source paths
are embedded in route and rerun artifacts, a filename containing a space makes
those complete artifacts use the legacy dumper. Identical small inputs named
`sample.txt` and `Source collection.txt` confirmed selection behavior, not a
new timing result. See the [serializer guard](../pageledger/artifacts.py), the
[fixture definitions](../scripts/pageledger_bench/workloads.py), the
[measurement boundary](../scripts/pageledger_bench/measure.py), and the
[independent oracle](../scripts/pageledger_bench/oracle.py).

The original production gate required at least 2×, or at least a 50% reduction.
The observed paired ratio was about 0.7001 and the 95% effect interval was
29.53–30.66%, so both the point gate and confidence gate failed. The useful
improvement remains; the original objective remains unmet.

## Proposed successor protocol

This section is a proposal, not an approved campaign and not authorization to
run benchmarks. Any successor begins with a concise reviewed protocol plus a
fresh, explicit time and resource budget.

### Measurement modes

- Acceptance runs record unprofiled total wall time and non-overlapping,
  exclusive phase times. Timing starts and stops in a fresh process around the
  declared production operation; fixture generation, relocation, oracle work,
  and report rendering stay outside it.
- Diagnostic profiling runs in a separate process and cannot supply acceptance
  timing. Profiler output explains a result; it does not adjudicate one.
- Run at least seven matched baseline/candidate pairs after untimed warmups.
  Predeclare the seed and complete order, alternate `A-B` and `B-A` as evenly as
  possible, and retain every repetition and raw value.
- Record commit identities, interpreter and dependency versions, platform,
  filesystem, free space, fixture hashes, command, order, and timing-mode
  identity in each receipt.

Use standard-library and existing repository tooling first. `pyperf` may later
help with development-only process orchestration if it is available offline and
the approved protocol permits it. It would not become a core dependency or
replace PageLedger's oracle, verifier, paired analysis, or receipts.

### Workload matrix

Measure matching baseline and candidate runs for:

- ordinary, spaced, and Unicode source paths, with the same content repeated
  across filename variants so path selection is isolated;
- tiny contract-stress pages and realistic full prose, table-heavy, noisy, and
  metadata-heavy pages, with byte and structure distributions recorded;
- the original 5,000-page fixture for continuity and separate representative
  generalization fixtures.

Do not compare different workload sizes as a regression gate. Each candidate
ratio uses its matching, contemporaneous baseline for the same fixture and
environment.

### Correctness and acceptance

Let `A_i` be original-baseline ledger time and `B_i` the candidate time for
matched pair `i`. Report every `A_i`, `B_i`, and `B_i/A_i`, the paired summary,
confidence interval, absolute time saved, and end-to-end share.

- The original cumulative goal passes only when paired original-baseline ledger
  `B/A <= 0.50` and the final production adjudication's 95% effect lower bound
  is at least 50%.
- A candidate's generalization gate is `candidate generalization total wall /
  matching baseline generalization total wall <= 1.05`.
- Its verifier gate is `candidate verifier wall / matching baseline verifier
  wall <= 1.10`.
- The historical paired log-ratio dispersion ceiling remains 6%. The historical
  minimum effect for promoting one candidate remains 10%. A candidate may clear
  that promotion rule without satisfying the cumulative 2× goal; the two
  decisions must be reported separately.
- Unaccounted measured wall remains at most 5%. Retained temporary evidence
  remains at most 2 GiB, and collection requires at least 75,000,000,000 free
  bytes.

All original correctness gates remain binding: preserve the frozen baseline and
protected inputs; validate schemas, canonical artifact bytes, hashes,
provenance, ordering, failure behavior, and sole-final-manifest semantics; and
require both the independent oracle and production `verify-run` checks to pass.
No speed result can compensate for a correctness disagreement.

The historical thresholds above describe the first campaign. Any change needs
prospective approval and cannot retroactively turn its failed hard result into a
pass.

### Scale and resources

A future scale gate must project allocated disk from measured pilot runs, state
what is retained, and keep retention bounded. An arbitrary `pages × estimated
bytes` formula may justify skipping a run under a declared contract, but it does
not prove the physical workload is infeasible or establish RSS.

On 2026-09-06 the development volume had about 49 GiB free, below the historical
75-billion-byte floor. A new trial is therefore inadmissible under that resource
contract. No more optimization or benchmark trials should run until a reviewer
approves a fresh time/resource budget and preflight confirms its limits.

## Reader impact is still pending

Automated correctness and timing do not replace observation with actual readers.
A later reader check should ask whether a researcher can answer: What ran? Where
is the text? Which page needs attention and why? What does verification prove?
What should happen next? No human study or acceptance result is claimed here.
