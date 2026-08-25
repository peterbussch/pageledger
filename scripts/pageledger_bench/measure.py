"""One-process PageLedger benchmark measurement and evidence receipts."""

from __future__ import annotations

import cProfile
import fcntl
import hashlib
import importlib.metadata
import io
import json
import operator
import os
import platform
import plistlib
import pstats
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pageledger.runner as runner_module
from pageledger.runner import run as runner_run

from .oracle import (
    EquivalenceReceipt,
    ValidationReceipt,
    compare_runs,
    validate_run,
)
from .workloads import (
    GENERATOR_VERSION,
    WorkloadSpec,
    ZeroWorkAdapter,
    generate_workload,
    load_frozen_manifest,
    sha256_path,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = Path(__file__).with_name("benchmark_manifest.json")
_PROJECTED_BYTES_PER_PAGE = 64 * 1024
_DEPENDENCIES = ("pageledger", "PyYAML", "jsonschema")
_SUBPROCESS_TIMEOUT_SECONDS = 5.0
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_TRACE_INTERPOSITION_POLICY_VERSION = "2.3"
_TRACE_READ_ONLY_FILESYSTEM_EVENTS = frozenset(
    {
        "glob.glob",
        "glob.glob/2",
        "os.fwalk",
        "os.getxattr",
        "os.listdir",
        "os.listxattr",
        "os.scandir",
        "os.walk",
        "pathlib.Path.glob",
        "pathlib.Path.rglob",
    }
)
_TRACE_NON_FILESYSTEM_OS_EVENTS = frozenset({"os.kill", "os.putenv", "os.unsetenv"})
_TRACE_PROCESS_CREATION_EVENTS = frozenset(
    {
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.system",
        "subprocess.Popen",
    }
)
_TRACE_MUTATION_PATH_SPECS: dict[str, tuple[tuple[int, int | None, bool], ...]] = {
    "os.mkdir": ((0, 2, False),),
    "os.remove": ((0, 1, False),),
    "os.rmdir": ((0, 1, False),),
    "os.rename": ((0, 2, False), (1, 3, False)),
    "os.link": ((0, 2, False), (1, 3, False)),
    "os.symlink": ((1, 2, False),),
    "os.truncate": ((0, None, True),),
    "os.chmod": ((0, 2, True),),
    "os.chown": ((0, 3, True),),
    "os.utime": ((0, 3, True),),
    "os.setxattr": ((0, None, True),),
    "os.removexattr": ((0, None, True),),
    "os.chflags": ((0, None, False),),
    "shutil.copyfile": ((0, None, False), (1, None, False)),
    "shutil.copymode": ((0, None, False), (1, None, False)),
    "shutil.copystat": ((0, None, False), (1, None, False)),
    "shutil.copytree": ((0, None, False), (1, None, False)),
    "shutil.move": ((0, None, False), (1, None, False)),
    "shutil.rmtree": ((0, 1, False),),
    "shutil.chown": ((0, None, False),),
}
REQUIRED_FREEZE_PATHS = frozenset(
    {
        "schemas/audit.schema.json",
        "schemas/cost.schema.json",
        "schemas/manifest.schema.json",
        "schemas/normalized-page.schema.json",
        "schemas/provenance-line.schema.json",
        "schemas/quality-line.schema.json",
        "schemas/run-log-line.schema.json",
        "scripts/pageledger_bench/__init__.py",
        "scripts/pageledger_bench/__main__.py",
        "scripts/pageledger_bench/benchmark_manifest.json",
        "scripts/pageledger_bench/measure.py",
        "scripts/pageledger_bench/oracle.py",
        "scripts/pageledger_bench/workloads.py",
        "tests/pageledger/test_benchmark_measure.py",
        "tests/pageledger/test_benchmark_oracle.py",
        "tests/pageledger/test_benchmark_timing.py",
        "tests/pageledger/test_benchmark_workloads.py",
    }
)
_DIRECT_LEDGER_PHASES = frozenset(
    {
        "plan_setup",
        "page_control",
        "result_validation",
        "raw_artifact",
        "usage_budget_provenance",
        "quality",
        "alignment",
        "page_log_control",
        "halt_accounting_route",
        "grading",
        "policy_queues",
        "models",
        "audit_write",
        "ledger_jsonl_write",
        "cost_build_write",
        "rerun_build_write",
        "runlog_build_write",
        "manifest_commit",
        "result_return",
    }
)
DEFAULT_WORKLOAD_FACTORY = generate_workload
DEFAULT_ORACLE_VALIDATOR = validate_run
DEFAULT_EQUIVALENCE_VALIDATOR = compare_runs
DEFAULT_RUNNER = runner_run


class BenchmarkError(RuntimeError):
    """Fail-closed benchmark refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ApprovedFreeze:
    """Externally approved immutable harness identity and resource policy."""

    harness_sha: str
    benchmark_manifest_sha256: str
    protected_paths_sha256: dict[str, str]
    free_space_floor_bytes: int
    lock_path: Path
    receipt_path: Path
    receipt_sha256: str


def canonical_sha256(value: Any) -> str:
    """Hash a JSON-compatible value using one stable canonical encoding."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_approved_freeze(path: Path) -> ApprovedFreeze:
    """Load one strict external freeze receipt without trusting candidate files."""
    receipt_path = Path(path).expanduser().absolute()
    _require_external_path(receipt_path)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise BenchmarkError(
            "freeze_receipt_invalid", "Approved freeze receipt must be one regular file"
        )
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("freeze_receipt_invalid", "Approved freeze receipt is unreadable") from exc
    required = {
        "harness_sha",
        "benchmark_manifest_sha256",
        "protected_paths_sha256",
        "free_space_floor_bytes",
        "lock_path",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise BenchmarkError(
            "freeze_receipt_invalid",
            f"Approved freeze receipt must contain exactly {sorted(required)}",
        )
    harness_sha = payload["harness_sha"]
    manifest_sha = payload["benchmark_manifest_sha256"]
    protected = payload["protected_paths_sha256"]
    floor = payload["free_space_floor_bytes"]
    lock_value = payload["lock_path"]
    if not isinstance(harness_sha, str) or not _GIT_SHA.fullmatch(harness_sha):
        raise BenchmarkError("freeze_receipt_invalid", "harness_sha must be 40 lowercase hex")
    if not isinstance(manifest_sha, str) or not _SHA256.fullmatch(manifest_sha):
        raise BenchmarkError(
            "freeze_receipt_invalid", "benchmark_manifest_sha256 must be 64 lowercase hex"
        )
    if not isinstance(protected, dict) or set(protected) != REQUIRED_FREEZE_PATHS:
        raise BenchmarkError(
            "freeze_receipt_invalid",
            "protected_paths_sha256 must contain exactly the required frozen harness paths",
        )
    if not all(isinstance(value, str) and _SHA256.fullmatch(value) for value in protected.values()):
        raise BenchmarkError(
            "freeze_receipt_invalid", "Every protected path hash must be 64 lowercase hex"
        )
    if type(floor) is not int or floor <= 0:
        raise BenchmarkError(
            "freeze_receipt_invalid", "free_space_floor_bytes must be a positive integer"
        )
    if not isinstance(lock_value, str) or not lock_value:
        raise BenchmarkError("freeze_receipt_invalid", "lock_path must be a non-empty string")
    lock_path = Path(lock_value).expanduser().absolute()
    _require_external_path(lock_path)
    return ApprovedFreeze(
        harness_sha=harness_sha,
        benchmark_manifest_sha256=manifest_sha,
        protected_paths_sha256=dict(sorted(protected.items())),
        free_space_floor_bytes=floor,
        lock_path=lock_path,
        receipt_path=receipt_path,
        receipt_sha256=sha256_path(receipt_path),
    )


def _validate_approved_freeze(
    approval: ApprovedFreeze, *, candidate_head: str | None = None
) -> ApprovedFreeze:
    """Bind current harness bytes to an existing externally approved commit."""
    if not _git_commit_exists(approval.harness_sha):
        raise BenchmarkError(
            "approved_harness_missing",
            f"Approved harness commit does not exist: {approval.harness_sha}",
        )
    head = candidate_head or _git_text("rev-parse", "HEAD")
    if not _git_is_ancestor(approval.harness_sha, head):
        raise BenchmarkError(
            "approved_harness_not_ancestor",
            f"Approved harness {approval.harness_sha} is not an ancestor of candidate {head}",
        )
    dirty = _git_status_porcelain()
    if dirty:
        raise BenchmarkError(
            "tracked_dirty",
            f"Refusing tracked-dirty benchmark execution: {dirty.splitlines()[0]}",
        )
    if set(approval.protected_paths_sha256) != REQUIRED_FREEZE_PATHS:
        raise BenchmarkError(
            "approved_freeze_mismatch", "Approved protected path set is not exact"
        )
    current_manifest_sha = sha256_path(_MANIFEST_PATH)
    if current_manifest_sha != approval.benchmark_manifest_sha256:
        raise BenchmarkError(
            "approved_manifest_drift",
            "Current benchmark manifest does not match approved benchmark manifest SHA-256",
        )
    approved_manifest_sha = hashlib.sha256(
        _git_blob_bytes_at(
            approval.harness_sha,
            _MANIFEST_PATH.relative_to(_REPOSITORY_ROOT).as_posix(),
        )
    ).hexdigest()
    if approved_manifest_sha != approval.benchmark_manifest_sha256:
        raise BenchmarkError(
            "approved_manifest_drift",
            "Approved harness commit does not carry the approved benchmark manifest",
        )
    for relative in sorted(REQUIRED_FREEZE_PATHS):
        expected = approval.protected_paths_sha256[relative]
        current = _REPOSITORY_ROOT / relative
        if current.is_symlink() or not current.is_file() or sha256_path(current) != expected:
            raise BenchmarkError(
                "approved_harness_drift",
                f"Current protected harness bytes differ from approval: {relative}",
            )
        approved = hashlib.sha256(
            _git_blob_bytes_at(approval.harness_sha, relative)
        ).hexdigest()
        if approved != expected:
            raise BenchmarkError(
                "approved_harness_drift",
                f"Protected path differs from approved harness commit bytes: {relative}",
            )
    return approval


DEFAULT_FREEZE_VALIDATOR = _validate_approved_freeze


def _capture_candidate_boundary(approval: ApprovedFreeze) -> dict[str, Any]:
    """Capture one clean approved candidate identity without a mutable gap."""
    before = _git_fingerprint()
    _require_clean_boundary(before)
    if not _git_is_ancestor(approval.harness_sha, str(before["sha"])):
        raise BenchmarkError(
            "approved_harness_not_ancestor",
            f"Approved harness {approval.harness_sha} is not an ancestor of "
            f"candidate {before['sha']}",
        )
    _validate_approved_freeze(approval, candidate_head=str(before["sha"]))
    after = _git_fingerprint()
    _require_clean_boundary(after)
    _require_same_candidate_boundary(before, after, context="freeze validation")
    return before


def _require_clean_boundary(state: dict[str, Any]) -> None:
    if state.get("tracked_dirty") is not False:
        raise BenchmarkError(
            "tracked_dirty", "Refusing tracked-dirty candidate at measurement boundary"
        )
    sha = state.get("sha")
    if not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha):
        raise BenchmarkError("candidate_identity_invalid", "Candidate HEAD is not a full Git SHA")


def _require_same_candidate_boundary(
    before: dict[str, Any], after: dict[str, Any], *, context: str = "measured run"
) -> None:
    if before.get("sha") != after.get("sha") or before.get("branch") != after.get("branch"):
        raise BenchmarkError(
            "candidate_identity_changed",
            f"Candidate HEAD/state changed across {context}",
        )


DEFAULT_BOUNDARY_VALIDATOR = _capture_candidate_boundary


def measure_run(
    workload_name: str,
    out_dir: Path,
    *,
    approved_freeze: ApprovedFreeze,
    warmup_state: str,
    command: list[str] | None = None,
    _prepared_workload: WorkloadSpec | None = None,
    _oracle_validator: Callable[[Path, WorkloadSpec], ValidationReceipt] = DEFAULT_ORACLE_VALIDATOR,
    _equivalence_validator: Callable[
        [Path, Path, WorkloadSpec], EquivalenceReceipt
    ] = DEFAULT_EQUIVALENCE_VALIDATOR,
    _freeze_validator: Callable[[ApprovedFreeze], ApprovedFreeze] | None = None,
    _boundary_validator: Callable[[ApprovedFreeze], dict[str, Any]] | None = None,
    _runner: Callable[..., dict[str, Any]] = DEFAULT_RUNNER,
) -> dict[str, Any]:
    """Measure one untraced run and validate it with one untimed trace replay."""
    if warmup_state not in {"none", "post-untimed-warmup"}:
        raise BenchmarkError("warmup_state_invalid", f"Unknown warmup state: {warmup_state}")
    requested_out = Path(out_dir).expanduser().absolute()
    _refuse_output_path(requested_out)
    _require_external_path(requested_out)
    validator = _freeze_validator or DEFAULT_FREEZE_VALIDATOR
    boundary_validator = _boundary_validator or DEFAULT_BOUNDARY_VALIDATOR

    preflight_manifest = load_frozen_manifest()
    preflight_cap_bytes = int(preflight_manifest["temp_output_cap_bytes"])
    preflight_page_count = (
        len(_prepared_workload.page_specs)
        if _prepared_workload is not None
        else int(preflight_manifest["workloads"][workload_name]["page_count"])
    )
    preflight_projected_bytes = preflight_page_count * _PROJECTED_BYTES_PER_PAGE * 2
    preflight_free_bytes = shutil.disk_usage(_nearest_existing_parent(requested_out)).free
    _check_capacity(
        requested_out,
        projected_bytes=preflight_projected_bytes,
        cap_bytes=preflight_cap_bytes,
        free_bytes=preflight_free_bytes,
        free_space_floor_bytes=approved_freeze.free_space_floor_bytes,
    )
    with _benchmark_lock(approved_freeze.lock_path) as lock_identity:
        approved_freeze = validator(approved_freeze)
        initial_boundary = boundary_validator(approved_freeze)
        _require_clean_boundary(initial_boundary)
        frozen_manifest = load_frozen_manifest()
        cap_bytes = int(frozen_manifest["temp_output_cap_bytes"])
        page_count = (
            len(_prepared_workload.page_specs)
            if _prepared_workload is not None
            else int(frozen_manifest["workloads"][workload_name]["page_count"])
        )
        projected_bytes = page_count * _PROJECTED_BYTES_PER_PAGE * 2
        free_bytes = shutil.disk_usage(_nearest_existing_parent(requested_out)).free
        _check_capacity(
            requested_out,
            projected_bytes=projected_bytes,
            cap_bytes=cap_bytes,
            free_bytes=free_bytes,
            free_space_floor_bytes=approved_freeze.free_space_floor_bytes,
        )
        host = _host_fingerprint(requested_out, warmup_state, free_bytes)
        requested_out.mkdir(parents=True)
        workload = _prepared_workload or DEFAULT_WORKLOAD_FACTORY(
            workload_name, requested_out / "workload"
        )
        adapter = ZeroWorkAdapter(workload.page_specs)
        run_dir = requested_out / "run"
        control_run_dir = requested_out / "control-run"
        profile_path = requested_out / "profile.pstats"
        profile_text_path = requested_out / "profile.txt"
        receipt_path = requested_out / "measurement.json"
        receipt_hash_path = requested_out / "measurement.json.sha256"
        events: list[tuple[str, int]] = []

        before_boundary = boundary_validator(approved_freeze)
        _require_clean_boundary(before_boundary)
        _require_same_candidate_boundary(
            initial_boundary, before_boundary, context="pre-measure preparation"
        )
        profiler = cProfile.Profile()
        wall_started = time.perf_counter_ns()
        profiler.runcall(
            _runner,
            inputs=[workload.source_path],
            config_path=workload.config_path,
            out_dir=run_dir,
            dry_run=False,
            _loaded_adapter=adapter,
            _reproducibility_profile=None,
            _phase_observer=lambda name, duration: events.append((name, duration)),
        )
        total_wall_ns = time.perf_counter_ns() - wall_started
        peak_rss_bytes, source_unit = _normalize_peak_rss(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            platform.system(),
        )
        after_boundary = boundary_validator(approved_freeze)
        _require_clean_boundary(after_boundary)
        _require_same_candidate_boundary(before_boundary, after_boundary)

        _write_profiles(profiler, profile_path, profile_text_path)
        control_relocation = _relocate_control_run(run_dir, control_run_dir)
        trace_adapter = ZeroWorkAdapter(workload.page_specs)
        trace_events: list[tuple[str, int]] = []
        with _CompletedMutationTrace(run_dir) as mutation_trace:
            _runner(
                inputs=[workload.source_path],
                config_path=workload.config_path,
                out_dir=run_dir,
                dry_run=False,
                _loaded_adapter=trace_adapter,
                _reproducibility_profile=None,
                _phase_observer=lambda name, duration: trace_events.append(
                    (name, duration)
                ),
            )
        _require_manifest_last(mutation_trace.events)
        trace_phase_sequence = _require_phase_sequence(
            trace_events, len(workload.page_specs)
        )
        final_boundary = boundary_validator(approved_freeze)
        _require_clean_boundary(final_boundary)
        _require_same_candidate_boundary(
            before_boundary, final_boundary, context="trace validation replay"
        )
        inventory = _regular_file_inventory(control_run_dir, cap_bytes=cap_bytes)
        trace_inventory = _regular_file_inventory(run_dir, cap_bytes=cap_bytes)
        validation = _oracle_validator(control_run_dir, workload)
        trace_validation = _oracle_validator(run_dir, workload)
        equivalence = _equivalence_validator(control_run_dir, run_dir, workload)
        timing = _summarize_timing(events, total_wall_ns, len(workload.page_specs))
        _require_timing_contract(
            events,
            timing,
            len(workload.page_specs),
            float(frozen_manifest["max_unaccounted_timing_ratio"]),
        )
        workspace_inventory = _regular_file_inventory(requested_out, cap_bytes=cap_bytes)
        frozen_hashes = {
            "benchmark_manifest_sha256": sha256_path(_MANIFEST_PATH),
            "schema_sha256": dict(frozen_manifest["schema_sha256"]),
            "recipe_sha256": dict(frozen_manifest["recipe_sha256"]),
            "workloads_sha256": {
                name: dict(spec["hashes"])
                for name, spec in sorted(frozen_manifest["workloads"].items())
            },
            "protected_paths_sha256": dict(approved_freeze.protected_paths_sha256),
        }
        schema_matched = all(
            sha256_path(_REPOSITORY_ROOT / "schemas" / filename) == expected
            for filename, expected in frozen_manifest["schema_sha256"].items()
        )
        receipt: dict[str, Any] = {
            "receipt_version": 1,
            "status": "pass" if validation.valid and schema_matched else "fail",
            "command": list(command or sys.argv),
            "approved_freeze": _freeze_evidence(approved_freeze),
            "benchmark_lock": lock_identity,
            "candidate_boundary": {
                "initial_under_lock": initial_boundary,
                "immediately_before_measured_run": before_boundary,
                "immediately_after_measured_run": after_boundary,
                "after_trace_validation": final_boundary,
                "unchanged": True,
            },
            "workload": {
                "name": workload.name,
                "generator_version": (
                    GENERATOR_VERSION
                    if workload.name in frozen_manifest["workloads"]
                    else "test-seam"
                ),
                "page_count": len(workload.page_specs),
                "category_counts": dict(
                    sorted(Counter(page.category for page in workload.page_specs).items())
                ),
                "generated_sha256": {
                    name: sha256_path(path)
                    for name, path in sorted(workload.generated_paths.items())
                },
                "frozen_contract": frozen_manifest["workloads"].get(workload.name),
            },
            "frozen_hashes": frozen_hashes,
            "git": before_boundary,
            "host": host,
            "timing": timing,
            "run_mutations": {
                "source": "untimed-trace-replay",
                "run_dir": str(run_dir),
                "events": mutation_trace.events,
                "trace_sha256": canonical_sha256(mutation_trace.events),
                "manifest_last": True,
            },
            "trace_validation": {
                "mode": "untimed-completed-mutation-replay",
                "timed": False,
                "profiled": False,
                "runner_calls": 1,
                "runner_out_dir": str(run_dir),
                "same_runner_out_dir_as_measured_call": True,
                "phase_sequence": trace_phase_sequence,
                "interposition_policy_version": _TRACE_INTERPOSITION_POLICY_VERSION,
                "inventory": {
                    "regular_file_count": trace_inventory["regular_file_count"],
                    "logical_bytes": trace_inventory["logical_bytes"],
                },
                "validation": _validation_evidence(trace_validation),
                "equivalence": _equivalence_evidence(equivalence),
            },
            "resources": {
                "peak_rss_bytes": peak_rss_bytes,
                "ru_maxrss_source_unit": source_unit,
            },
            "output": {
                "runner_out_dir": str(run_dir),
                "measured_control_run_dir": str(control_run_dir),
                "control_relocation": control_relocation,
                "run_dir": str(control_run_dir),
                "regular_file_count": inventory["regular_file_count"],
                "logical_bytes": inventory["logical_bytes"],
                "trace_run_dir": str(run_dir),
                "trace_regular_file_count": trace_inventory["regular_file_count"],
                "trace_logical_bytes": trace_inventory["logical_bytes"],
                "cap_bytes": cap_bytes,
                "projected_peak_bytes": projected_bytes,
                "free_space_floor_bytes": approved_freeze.free_space_floor_bytes,
                "manifest_last_artifact_write": True,
                "workspace_pre_receipt": {
                    "regular_file_count": workspace_inventory["regular_file_count"],
                    "logical_bytes": workspace_inventory["logical_bytes"],
                },
            },
            "profile": {
                "pstats": _file_evidence(profile_path),
                "text": _file_evidence(profile_text_path),
            },
            "validation": {
                "verify_report": validation.verifier_report,
                "oracle": _validation_evidence(validation),
                "schema_state": {
                    "status": "matched" if schema_matched else "drifted",
                    "sha256": dict(frozen_manifest["schema_sha256"]),
                },
            },
            "receipt_sha256_sidecar": str(receipt_hash_path),
        }
        receipt["status"] = (
            "pass"
            if validation.valid
            and trace_validation.valid
            and equivalence.equivalent
            and schema_matched
            else "fail"
        )
        receipt["receipt_payload_sha256"] = canonical_sha256(receipt)
        receipt_bytes = _json_bytes(receipt)
        receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
        sidecar_bytes = f"{receipt_sha}\n".encode("ascii")
        _publish_receipt(
            receipt_path,
            receipt_bytes,
            receipt_hash_path,
            sidecar_bytes,
            existing_workspace_bytes=workspace_inventory["logical_bytes"],
            cap_bytes=cap_bytes,
        )
        return receipt


def _summarize_timing(
    events: list[tuple[str, int]], total_wall_ns: int, pages: int
) -> dict[str, Any]:
    """Aggregate only direct emitted spans; never derive ledger time by subtraction."""
    if total_wall_ns <= 0 or pages <= 0:
        raise BenchmarkError("timing_invalid", "Wall time and page count must be positive")
    spans: dict[str, list[int]] = defaultdict(list)
    for name, duration_ns in events:
        if duration_ns < 0:
            raise BenchmarkError("timing_invalid", f"Negative phase span for {name}")
        spans[name].append(duration_ns)
    adapter_values = spans.pop("adapter_call", [])
    phase_summary = {
        name: {"count": len(values), "total_ns": sum(values)}
        for name, values in sorted(spans.items())
    }
    adapter_ns = sum(adapter_values)
    ledger_ns = sum(span["total_ns"] for span in phase_summary.values())
    observer_total_ns = ledger_ns + adapter_ns
    unaccounted_ns = max(0, total_wall_ns - observer_total_ns)
    granularity_ns = max(1, int(time.get_clock_info("perf_counter").resolution * 1_000_000_000))
    if observer_total_ns > total_wall_ns + granularity_ns:
        raise BenchmarkError(
            "timing_inconsistent",
            "Direct phase spans exceed total wall time beyond clock granularity",
        )
    return {
        "clock": "time.perf_counter_ns",
        "clock_granularity_ns": granularity_ns,
        "total_wall_ns": total_wall_ns,
        "adapter": {
            "phase": "adapter_call",
            "count": len(adapter_values),
            "total_ns": adapter_ns,
        },
        "ledger": {
            "phases": phase_summary,
            "total_ns": ledger_ns,
            "ns_per_page": ledger_ns / pages,
        },
        "observer_total_ns": observer_total_ns,
        "unaccounted_ns": unaccounted_ns,
        "unaccounted_ratio": unaccounted_ns / total_wall_ns,
        "pages_per_second": pages * 1_000_000_000 / total_wall_ns,
    }


def _expected_phase_names(pages: int) -> list[str]:
    per_page = [
        "page_control",
        "adapter_call",
        "result_validation",
        "page_control",
        "raw_artifact",
        "usage_budget_provenance",
        "quality",
        "alignment",
        "page_log_control",
    ]
    final = [
        "halt_accounting_route",
        "grading",
        "policy_queues",
        "models",
        "audit_write",
        "ledger_jsonl_write",
        "cost_build_write",
        "rerun_build_write",
        "runlog_build_write",
        "manifest_commit",
        "result_return",
    ]
    return ["plan_setup", *(per_page * pages), *final]


def _require_timing_contract(
    events: list[tuple[str, int]],
    timing: dict[str, Any],
    pages: int,
    max_unaccounted_ratio: float,
) -> None:
    _require_phase_sequence(events, pages)
    phases = set(timing["ledger"]["phases"])
    if phases != _DIRECT_LEDGER_PHASES:
        raise BenchmarkError("timing_phase_set_mismatch", "direct ledger phase set is incomplete")
    if timing["unaccounted_ratio"] > max_unaccounted_ratio:
        raise BenchmarkError(
            "timing_unaccounted_exceeded",
            f"unaccounted timing ratio {timing['unaccounted_ratio']:.9f} exceeds "
            f"approved ceiling {max_unaccounted_ratio:.9f}",
        )


def _require_phase_sequence(
    events: list[tuple[str, int]], pages: int
) -> dict[str, Any]:
    """Validate observer control flow without treating replay spans as performance data."""
    names = [name for name, _duration in events]
    expected = _expected_phase_names(pages)
    if names != expected:
        mismatch = next(
            (
                index
                for index, pair in enumerate(zip(names, expected, strict=False))
                if pair[0] != pair[1]
            ),
            min(len(names), len(expected)),
        )
        raise BenchmarkError(
            "timing_phase_sequence_mismatch",
            "direct phase event sequence does not match frozen successful-run shape "
            f"at index {mismatch} (observed={len(names)}, expected={len(expected)})",
        )
    adapter_count = names.count("adapter_call")
    if adapter_count != pages:
        raise BenchmarkError(
            "timing_adapter_count_mismatch", "direct adapter span count does not match page count"
        )
    return {
        "valid": True,
        "event_count": len(names),
        "adapter_count": adapter_count,
        "phase_counts": dict(sorted(Counter(names).items())),
        "sequence_sha256": canonical_sha256(names),
        "performance_evidence": False,
    }


def _relocate_control_run(run_dir: Path, control_run_dir: Path) -> dict[str, Any]:
    """Atomically free the exact runner path while preserving measured output."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise BenchmarkError(
            "control_relocation_failed", "Measured run directory is not a regular directory"
        )
    if os.path.lexists(control_run_dir):
        raise BenchmarkError(
            "control_relocation_failed", "Measured control destination already exists"
        )
    try:
        os.replace(run_dir, control_run_dir)
        _fsync_directory(run_dir.parent)
    except OSError as exc:
        raise BenchmarkError(
            "control_relocation_failed", "Could not atomically relocate measured control run"
        ) from exc
    return {
        "from": str(run_dir),
        "to": str(control_run_dir),
        "atomic": True,
        "timed": False,
        "profiled": False,
    }


def _refuse_output_path(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise BenchmarkError("output_symlink", f"Benchmark output path contains symlink: {current}")
        if current.parent == current:
            break
        current = current.parent
    if os.path.lexists(path):
        raise BenchmarkError(
            "output_exists",
            f"Benchmark output must point to a path that does not already exist: {path}",
        )


def _require_external_path(path: Path) -> None:
    candidate = path.expanduser().absolute()
    repository = _REPOSITORY_ROOT.resolve()
    for form in (candidate, candidate.resolve(strict=False)):
        try:
            form.relative_to(repository)
        except ValueError:
            continue
        raise BenchmarkError(
            "evidence_inside_candidate",
            f"Benchmark evidence must remain outside the candidate Git worktree: {candidate}",
        )


def _nearest_existing_parent(path: Path) -> Path:
    current = path.parent
    while not current.exists():
        current = current.parent
    return current


def _check_capacity(
    path: Path,
    *,
    projected_bytes: int,
    cap_bytes: int,
    free_bytes: int,
    free_space_floor_bytes: int = 0,
) -> None:
    if projected_bytes > cap_bytes:
        raise BenchmarkError(
            "projected_output_too_large",
            f"Benchmark projected output {projected_bytes} exceeds cap {cap_bytes} at {path}",
        )
    if free_bytes < projected_bytes:
        raise BenchmarkError(
            "insufficient_free_disk",
            f"Benchmark free disk {free_bytes} is below projected output {projected_bytes} at {path}",
        )
    if free_bytes - projected_bytes < free_space_floor_bytes:
        raise BenchmarkError(
            "free_space_floor_violated",
            f"Benchmark would leave less than approved free-space floor "
            f"{free_space_floor_bytes} at {path}",
        )


def _regular_file_inventory(root: Path, *, cap_bytes: int) -> dict[str, Any]:
    count = 0
    logical_bytes = 0
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise BenchmarkError("output_symlink", f"Benchmark output contains symlink: {path}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise BenchmarkError("output_non_regular", f"Benchmark output is not regular: {path}")
        count += 1
        logical_bytes += path.stat().st_size
        paths.append(path)
        if logical_bytes > cap_bytes:
            raise BenchmarkError(
                "actual_output_too_large",
                f"Benchmark actual output {logical_bytes} exceeds cap {cap_bytes}",
            )
    return {"regular_file_count": count, "logical_bytes": logical_bytes, "paths": paths}


class _TrackedWriteHandle:
    """Delegate one write handle and emit its mutation only after close succeeds."""

    def __init__(self, handle: Any, trace: _CompletedMutationTrace, path: Path) -> None:
        self._handle = handle
        self._trace = trace
        self._path = path
        self._completed = False
        try:
            trace._register_handle_identity(handle, path)
        except BaseException:
            handle.close()
            raise

    def __enter__(self) -> _TrackedWriteHandle:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[Any]:
        return iter(self._handle)

    def __next__(self) -> Any:
        return next(self._handle)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def close(self) -> None:
        if self._completed:
            return
        self._handle.close()
        self._completed = True
        self._trace._complete_handle(self, self._path)


class _CompletedMutationTrace:
    """Untimed completed-mutation trace for the current runner write mechanisms."""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.events: list[dict[str, Any]] = []
        self._active = False
        self._authorizations: list[dict[str, Any]] = []
        self._unhandled: list[str] = []
        self._open_handles: dict[int, str] = {}
        self._tracked_identities: dict[tuple[int, int], Path] = {}
        self._trace_pid: int | None = None
        self._original_path_open = Path.open
        self._original_path_mkdir = Path.mkdir
        self._original_path_unlink = Path.unlink
        self._original_path_rmdir = Path.rmdir
        self._original_path_rename = Path.rename
        self._original_path_replace = Path.replace
        self._original_copyfile = runner_module.copyfile

    def __enter__(self) -> _CompletedMutationTrace:
        self._trace_pid = os.getpid()
        self._require_sequential_process()
        trace = self

        def tracked_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            mode = str(args[0] if args else kwargs.get("mode", "r"))
            if not trace._inside(path) or not any(character in mode for character in "wax+"):
                return trace._original_path_open(path, *args, **kwargs)
            with trace._authorize(path, {"open"}) as authorization:
                handle = trace._original_path_open(path, *args, **kwargs)
            trace._require_observed(authorization, {"open"})
            wrapped = _TrackedWriteHandle(handle, trace, path.absolute())
            trace._open_handles[id(wrapped)] = trace._relative(path)
            return wrapped

        def tracked_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            existed = path.exists()
            with trace._authorize(path, {"os.mkdir"}) as authorization:
                result = trace._original_path_mkdir(path, *args, **kwargs)
            if not existed and path.exists() and trace._inside(path):
                trace._require_observed(authorization, {"os.mkdir"})
                trace._completed("mkdir_complete", path)
            return result

        def tracked_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            with trace._authorize(path, {"os.remove"}) as authorization:
                result = trace._original_path_unlink(path, *args, **kwargs)
            if trace._inside(path):
                trace._require_observed(authorization, {"os.remove"})
                trace._completed("remove_complete", path)
            return result

        def tracked_rmdir(path: Path) -> None:
            with trace._authorize(path, {"os.rmdir"}) as authorization:
                result = trace._original_path_rmdir(path)
            if trace._inside(path):
                trace._require_observed(authorization, {"os.rmdir"})
                trace._completed("rmdir_complete", path)
            return result

        def tracked_rename(path: Path, target: Any) -> Path:
            source = path.absolute()
            destination = Path(target).absolute()
            with trace._authorize(
                destination, {"os.rename"}, related_paths={source, destination}
            ) as authorization:
                result = trace._original_path_rename(path, target)
            if trace._inside(path) or trace._inside(destination):
                trace._require_observed(authorization, {"os.rename"})
                trace._completed("rename_complete", destination)
            return result

        def tracked_replace(path: Path, target: Any) -> Path:
            source = path.absolute()
            destination = Path(target).absolute()
            with trace._authorize(
                destination, {"os.rename"}, related_paths={source, destination}
            ) as authorization:
                result = trace._original_path_replace(path, target)
            if trace._inside(path) or trace._inside(destination):
                trace._require_observed(authorization, {"os.rename"})
                trace._completed("replace_complete", destination)
            return result

        def tracked_copyfile(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
            target = Path(destination).absolute()
            with trace._authorize(
                target, {"shutil.copyfile", "open"}
            ) as authorization:
                result = trace._original_copyfile(source, destination, *args, **kwargs)
            if trace._inside(target):
                trace._require_observed(authorization, {"shutil.copyfile", "open"})
                trace._completed("copyfile_complete", target)
            return result

        Path.open = tracked_open
        Path.mkdir = tracked_mkdir
        Path.unlink = tracked_unlink
        Path.rmdir = tracked_rmdir
        Path.rename = tracked_rename
        Path.replace = tracked_replace
        runner_module.copyfile = tracked_copyfile
        self._active = True
        sys.addaudithook(self._audit)
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        self._active = False
        Path.open = self._original_path_open
        Path.mkdir = self._original_path_mkdir
        Path.unlink = self._original_path_unlink
        Path.rmdir = self._original_path_rmdir
        Path.rename = self._original_path_rename
        Path.replace = self._original_path_replace
        runner_module.copyfile = self._original_copyfile
        if exc_type is not None:
            return
        self._require_sequential_process()
        if self._unhandled:
            raise BenchmarkError(
                "trace_unhandled_mutation",
                f"Trace observed unhandled mutation mechanism: {self._unhandled[0]}",
            )
        if self._open_handles:
            raise BenchmarkError(
                "trace_unmatched_open",
                "Trace ended with unmatched write opens: "
                + ", ".join(sorted(self._open_handles.values())),
            )

    @contextmanager
    def _authorize(
        self,
        path: Path,
        expected_events: set[str],
        *,
        related_paths: set[Path] | None = None,
    ) -> Iterator[dict[str, Any]]:
        authorization = {
            "path": path.absolute(),
            "paths": {
                candidate.absolute()
                for candidate in (related_paths if related_paths is not None else {path})
            },
            "expected": expected_events,
            "observed": set(),
        }
        self._authorizations.append(authorization)
        try:
            yield authorization
        finally:
            popped = self._authorizations.pop()
            assert popped is authorization

    def _audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not self._active:
            return
        if event in _TRACE_PROCESS_CREATION_EVENTS:
            raise BenchmarkError(
                "trace_process_creation",
                f"Trace refuses process creation during sequential replay: {event}",
            )
        if event == "open":
            mutation = self._audit_open(args)
        elif event in _TRACE_MUTATION_PATH_SPECS:
            mutation = self._audit_known_mutation(event, args)
        elif (
            event in _TRACE_READ_ONLY_FILESYSTEM_EVENTS
            or event in _TRACE_NON_FILESYSTEM_OS_EVENTS
        ):
            return
        else:
            inside = [path for path in self._audit_argument_paths(args) if self._inside(path)]
            if inside:
                self._unhandled.append(
                    f"unknown filesystem audit event {event}:{self._relative(inside[0])}"
                )
            return
        if mutation is None:
            return
        operation, paths = mutation
        for authorization in reversed(self._authorizations):
            if operation in authorization["expected"] and any(
                path in authorization["paths"] for path in paths
            ):
                authorization["observed"].add(operation)
                return
        self._unhandled.append(f"{operation}:{self._relative(paths[0])}")

    def _audit_open(self, args: tuple[Any, ...]) -> tuple[str, list[Path]] | None:
        if len(args) < 3:
            return None
        mode = args[1] if isinstance(args[1], str) else ""
        flags = args[2] if isinstance(args[2], int) else 0
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if not any(character in mode for character in "wax+") and not flags & write_flags:
            return None
        path, unresolved = self._resolve_audit_path(
            args[0], dir_fd=None, supports_fd=True, reject_relative=True
        )
        if unresolved:
            self._unhandled.append("open:<unresolved-directory-relative-path>")
            return None
        return ("open", [path]) if path is not None and self._inside(path) else None

    def _audit_known_mutation(
        self, event: str, args: tuple[Any, ...]
    ) -> tuple[str, list[Path]] | None:
        paths: list[Path] = []
        unresolved = False
        for path_index, dir_fd_index, supports_fd in _TRACE_MUTATION_PATH_SPECS[event]:
            if len(args) <= path_index:
                unresolved = True
                continue
            dir_fd = (
                args[dir_fd_index]
                if dir_fd_index is not None and len(args) > dir_fd_index
                else None
            )
            path, failed = self._resolve_audit_path(
                args[path_index], dir_fd=dir_fd, supports_fd=supports_fd
            )
            unresolved = unresolved or failed
            if path is not None:
                paths.append(path)
        inside = [path for path in paths if self._inside(path)]
        if unresolved:
            self._unhandled.append(f"{event}:<unresolved-directory-relative-path>")
        return (event, inside) if inside else None

    def _audit_argument_paths(self, args: tuple[Any, ...]) -> list[Path]:
        paths: list[Path] = []
        for value in args:
            if isinstance(value, int) and not isinstance(value, bool):
                path = self._path_from_fd(value)
                if path is not None:
                    paths.append(path)
                continue
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            path, _unresolved = self._resolve_audit_path(
                value, dir_fd=None, supports_fd=False
            )
            if path is not None:
                paths.append(path)
        return paths

    def _resolve_audit_path(
        self,
        value: object,
        *,
        dir_fd: object,
        supports_fd: bool,
        reject_relative: bool = False,
    ) -> tuple[Path | None, bool]:
        if supports_fd and isinstance(value, int):
            path = self._path_from_fd(value)
            return path, path is None
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None, True
        try:
            path = Path(os.fsdecode(value))
        except (OSError, TypeError, ValueError):
            return None, True
        if path.is_absolute():
            return path.absolute(), False
        if isinstance(dir_fd, int) and dir_fd >= 0:
            directory = self._path_from_fd(dir_fd)
            if directory is None:
                return None, True
            return (directory / path).absolute(), False
        if reject_relative:
            return None, True
        return path.absolute(), False

    def _path_from_fd(self, descriptor: int) -> Path | None:
        if isinstance(descriptor, bool):
            return None
        try:
            descriptor = operator.index(descriptor)
        except TypeError:
            return None
        if descriptor < 0:
            return None
        try:
            metadata = os.fstat(descriptor)
        except (OSError, OverflowError, TypeError, ValueError):
            return None
        tracked = self._tracked_identities.get((metadata.st_dev, metadata.st_ino))
        if tracked is not None:
            return tracked
        try:
            return Path(os.readlink(f"/dev/fd/{descriptor}")).absolute()
        except (OSError, TypeError, ValueError):
            return None

    def _inside(self, path: Path) -> bool:
        try:
            path.absolute().relative_to(self.root)
        except ValueError:
            return False
        return True

    def _relative(self, path: Path) -> str:
        try:
            return path.absolute().relative_to(self.root).as_posix()
        except ValueError:
            return str(path.absolute())

    @staticmethod
    def _require_observed(
        authorization: dict[str, Any], expected: set[str]
    ) -> None:
        missing = expected - authorization["observed"]
        if missing:
            raise BenchmarkError(
                "trace_interposition_incomplete",
                "Trace interposition missed expected events: " + ", ".join(sorted(missing)),
            )

    def _register_handle_identity(self, handle: Any, path: Path) -> None:
        try:
            descriptor = operator.index(handle.fileno())
            if isinstance(descriptor, bool) or descriptor < 0:
                raise ValueError("invalid descriptor")
            metadata = os.fstat(descriptor)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
            raise BenchmarkError(
                "trace_file_identity_unavailable",
                "Trace could not establish stable file identity for tracked write "
                f"handle: {self._relative(path)}",
            ) from exc
        identity = (metadata.st_dev, metadata.st_ino)
        absolute = path.absolute()
        existing = self._tracked_identities.get(identity)
        if existing is not None and existing != absolute:
            raise BenchmarkError(
                "trace_file_identity_collision",
                "Trace observed one tracked file identity under multiple paths: "
                f"{self._relative(existing)} and {self._relative(absolute)}",
            )
        self._tracked_identities[identity] = absolute

    def _complete_handle(self, handle: _TrackedWriteHandle, path: Path) -> None:
        self._open_handles.pop(id(handle), None)
        if self._active:
            self._completed("write_close", path)

    def _completed(self, operation: str, path: Path) -> None:
        relative = self._relative(path)
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "operation": operation,
                "path": relative,
            }
        )
        if relative == "manifest.json":
            self._require_no_live_tracked_descriptors()

    def _require_no_live_tracked_descriptors(self) -> None:
        self._require_sequential_process()
        aliases: list[tuple[int, Path]] = []
        try:
            with open(os.devnull, "rb") as sentinel:
                sentinel_descriptor = sentinel.fileno()
                observed: set[int] = set()
                with os.scandir("/dev/fd") as entries:
                    for entry in entries:
                        if not entry.name.isdecimal():
                            raise BenchmarkError(
                                "trace_descriptor_scan_failed",
                                "Trace descriptor enumeration returned a non-descriptor entry",
                            )
                        descriptor = int(entry.name)
                        observed.add(descriptor)
                        try:
                            metadata = os.fstat(descriptor)
                        except (OSError, OverflowError, TypeError, ValueError) as exc:
                            raise BenchmarkError(
                                "trace_descriptor_scan_failed",
                                "Trace descriptor enumeration changed during its live snapshot",
                            ) from exc
                        path = self._tracked_identities.get(
                            (metadata.st_dev, metadata.st_ino)
                        )
                        if path is not None:
                            aliases.append((descriptor, path))
                if sentinel_descriptor not in observed:
                    raise BenchmarkError(
                        "trace_descriptor_scan_failed",
                        "Trace descriptor enumeration did not report its sentinel descriptor",
                    )
        except BenchmarkError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise BenchmarkError(
                "trace_descriptor_scan_failed",
                "Trace could not enumerate live descriptors at manifest completion",
            ) from exc
        if aliases:
            descriptor, path = min(aliases, key=lambda item: item[0])
            raise BenchmarkError(
                "trace_descriptor_alias",
                "Refusing manifest completion with live descriptor alias "
                f"{descriptor} for tracked artifact {self._relative(path)}",
            )

    def _require_sequential_process(self) -> None:
        if self._trace_pid != os.getpid():
            raise BenchmarkError(
                "trace_process_changed",
                "Trace replay cannot continue in a different process",
            )
        if threading.active_count() != 1:
            raise BenchmarkError(
                "trace_concurrency_unsupported",
                "Trace replay must remain single-threaded for descriptor completeness",
            )


def _require_manifest_last(events: list[dict[str, Any]]) -> None:
    manifest_events = [event for event in events if event["path"] == "manifest.json"]
    if len(manifest_events) != 1:
        raise BenchmarkError(
            "manifest_commit_trace_invalid",
            f"Expected exactly one manifest.json mutation, observed {len(manifest_events)}",
        )
    if not events or events[-1]["path"] != "manifest.json":
        final = events[-1]["path"] if events else "<none>"
        raise BenchmarkError(
            "manifest_not_last",
            f"manifest.json is not the final trace mutation (final={final})",
        )


@contextmanager
def _benchmark_lock(path: Path) -> Iterator[dict[str, Any]]:
    lock_path = path.expanduser().absolute()
    _require_external_path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise BenchmarkError("benchmark_lock_failed", f"Cannot open benchmark lock: {lock_path}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BenchmarkError(
                "benchmark_lock_held", f"Machine-wide benchmark lock is already held: {lock_path}"
            ) from exc
        metadata = os.fstat(descriptor)
        yield {
            "acquired": True,
            "path": str(lock_path),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "pid": os.getpid(),
        }
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _freeze_evidence(approval: ApprovedFreeze) -> dict[str, Any]:
    return {
        "harness_sha": approval.harness_sha,
        "benchmark_manifest_sha256": approval.benchmark_manifest_sha256,
        "protected_paths_sha256": dict(approval.protected_paths_sha256),
        "free_space_floor_bytes": approval.free_space_floor_bytes,
        "lock_path": str(approval.lock_path),
        "receipt_path": str(approval.receipt_path),
        "receipt_sha256": approval.receipt_sha256,
    }


def _git_fingerprint() -> dict[str, Any]:
    sha = _git_text("rev-parse", "HEAD")
    branch = _git_text("branch", "--show-current")
    status = _git_text("status", "--porcelain", "--untracked-files=no")
    return {"sha": sha, "branch": branch, "tracked_dirty": bool(status)}


def _git_text(*args: str) -> str:
    completed = _git_command(["git", *args], text=True)
    if completed.returncode != 0:
        raise BenchmarkError("git_unavailable", f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _git_command(command: list[str], *, text: bool) -> subprocess.CompletedProcess[Any]:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            command,
            cwd=_REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=text,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(
            "git_timeout", f"git command timed out after {_SUBPROCESS_TIMEOUT_SECONDS} seconds"
        ) from exc
    except OSError as exc:
        raise BenchmarkError("git_unavailable", f"git command unavailable: {command[1]}") from exc


def _git_status_porcelain() -> str:
    return _git_text("status", "--porcelain", "--untracked-files=no")


def _git_commit_exists(sha: str) -> bool:
    completed = _git_command(["git", "cat-file", "-e", f"{sha}^{{commit}}"], text=False)
    return completed.returncode == 0


def _git_is_ancestor(ancestor: str, head: str) -> bool:
    completed = _git_command(
        ["git", "merge-base", "--is-ancestor", ancestor, head], text=False
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise BenchmarkError(
        "git_unavailable", "git merge-base --is-ancestor failed"
    )


def _git_blob_bytes_at(sha: str, relative: str) -> bytes:
    completed = _git_command(["git", "show", f"{sha}:{relative}"], text=False)
    if completed.returncode != 0:
        raise BenchmarkError(
            "approved_harness_path_missing",
            f"Approved harness commit lacks protected path: {relative}",
        )
    return completed.stdout


def _host_fingerprint(path: Path, warmup_state: str, free_bytes: int) -> dict[str, Any]:
    output_parent = _nearest_existing_parent(path)
    temp_root = Path(tempfile.gettempdir()).resolve()
    return {
        "os": platform.system(),
        "kernel": platform.release(),
        "cpu": _cpu_description(),
        "machine": platform.machine(),
        "ram_bytes": _ram_bytes(),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "dependencies": {name: _dependency_version(name) for name in _DEPENDENCIES},
        "runtime_content_sha256": {
            "python_executable": sha256_path(Path(sys.executable).resolve()),
            "pageledger_package": _tree_sha256(_REPOSITORY_ROOT / "pageledger"),
            "PyYAML": _distribution_content_sha256("PyYAML"),
            "jsonschema": _distribution_content_sha256("jsonschema"),
        },
        "hardware": _hardware_identity(),
        "filesystem": _volume_fingerprint(output_parent, free_bytes),
        "temp_volume": _volume_fingerprint(temp_root, shutil.disk_usage(temp_root).free),
        "background_load": _background_load(),
        "power_mode": _power_mode(),
        "thermal_state": _thermal_state(),
        "process_state": "fresh-process",
        "warmup_state": warmup_state,
        "os_page_cache": "uncontrolled",
    }


def _dependency_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _distribution_content_sha256(name: str) -> str | None:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    digest = hashlib.sha256()
    found = False
    for relative in sorted(distribution.files or [], key=str):
        path = Path(distribution.locate_file(relative))
        if not path.is_file() or path.is_symlink():
            continue
        encoded = str(relative).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        found = True
    return digest.hexdigest() if found else None


def _cpu_description() -> str:
    if platform.system() == "Darwin":
        result = _optional_command(["sysctl", "-n", "machdep.cpu.brand_string"])
        if result["available"] and result["stdout"].strip():
            return result["stdout"].strip()
    description = platform.processor().strip()
    if description:
        return description
    if platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.partition(":")[2].strip()
        except OSError:
            pass
    return "unavailable"


def _ram_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, TypeError, ValueError):
        return None


def _volume_fingerprint(path: Path, free_bytes: int) -> dict[str, Any]:
    filesystem_type = _filesystem_type(path)
    return {
        "path": str(path),
        "device": path.stat().st_dev,
        "filesystem_type": filesystem_type,
        "free_bytes": free_bytes,
    }


def _filesystem_type(path: Path) -> str:
    if platform.system() == "Darwin":
        volume = _optional_command(["df", "-P", str(path)])
        lines = volume.get("stdout", "").splitlines()
        if volume["available"] and len(lines) >= 2:
            device = lines[-1].split()[0]
            info = _optional_command(["diskutil", "info", "-plist", device], text=False)
            if info["available"]:
                try:
                    filesystem = plistlib.loads(info["stdout"]).get("FilesystemType")
                except (AttributeError, plistlib.InvalidFileException):
                    filesystem = None
                if isinstance(filesystem, str) and filesystem:
                    return filesystem
        return "unavailable"
    completed = _optional_command(["stat", "-f", "-c", "%T", str(path)])
    if completed["available"] and completed["stdout"].strip():
        return completed["stdout"].strip()
    return "unavailable"


def _power_mode() -> dict[str, str]:
    if platform.system() == "Darwin":
        completed = _optional_command(["pmset", "-g"])
        if completed["available"]:
            for line in completed["stdout"].splitlines():
                setting = line.strip().split()
                if setting and setting[0] in {"powermode", "lowpowermode"}:
                    value = line.split()[-1]
                    return {"mode": f"{setting[0]}={value}"}
        return {
            "unavailable_reason": str(
                completed.get("unavailable_reason", "pmset did not report a power mode")
            )
        }
    profile = Path("/sys/firmware/acpi/platform_profile")
    try:
        mode = profile.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return {"unavailable_reason": f"platform power profile unavailable: {exc.strerror}"}
    return {"mode": mode or "unavailable"}


def _hardware_identity() -> dict[str, Any]:
    if platform.system() == "Darwin":
        model = _optional_command(["sysctl", "-n", "hw.model"])
        model_value = model["stdout"].strip() if model["available"] else None
        reason = None if model["available"] else model["unavailable_reason"]
    else:
        model_path = Path("/sys/devices/virtual/dmi/id/product_name")
        try:
            model_value = model_path.read_text(encoding="utf-8").strip()
            reason = None
        except OSError as exc:
            model_value = None
            reason = f"hardware model unavailable: {exc.strerror}"
    return {
        "node": platform.node() or "unavailable",
        "model": model_value,
        "model_unavailable_reason": reason,
        "machine": platform.machine(),
        "cpu": _cpu_description(),
        "ram_bytes": _ram_bytes(),
    }


def _background_load() -> dict[str, Any]:
    try:
        load_average: list[float] | None = list(os.getloadavg())
    except OSError:
        load_average = None
    process = _optional_command(
        ["ps", "-axo", "pid=,ppid=,state=,%cpu=,%mem=,comm="]
    )
    if not process["available"]:
        return {
            "load_average": load_average,
            "logical_cpu_count": os.cpu_count(),
            "process_snapshot_available": False,
            "unavailable_reason": process["unavailable_reason"],
        }
    rows = [line.strip() for line in process["stdout"].splitlines() if line.strip()]
    parsed: list[dict[str, Any]] = []
    runnable = 0
    for row in rows:
        fields = row.split(None, 5)
        if len(fields) != 6:
            continue
        pid, ppid, state_value, cpu, memory, command = fields
        runnable += int(state_value.startswith("R"))
        try:
            parsed.append(
                {
                    "pid": int(pid),
                    "ppid": int(ppid),
                    "state": state_value,
                    "cpu_percent": float(cpu),
                    "memory_percent": float(memory),
                    "command": command,
                }
            )
        except ValueError:
            continue
    top = sorted(parsed, key=lambda item: (-item["cpu_percent"], item["pid"]))[:10]
    return {
        "load_average": load_average,
        "logical_cpu_count": os.cpu_count(),
        "process_snapshot_available": True,
        "process_count": len(parsed),
        "runnable_process_count": runnable,
        "top_cpu_processes": top,
    }


def _thermal_state() -> dict[str, Any]:
    if platform.system() == "Darwin":
        result = _optional_command(["pmset", "-g", "therm"])
        if not result["available"]:
            return {"available": False, "unavailable_reason": result["unavailable_reason"]}
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        if not lines:
            return {
                "available": False,
                "unavailable_reason": "pmset reported no thermal state",
            }
        return {"available": True, "pmset_therm": lines}
    zones: dict[str, int] = {}
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            zones[path.parent.name] = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    if not zones:
        return {"available": False, "unavailable_reason": "no readable thermal zones"}
    return {"available": True, "millidegrees_celsius": zones}


def _optional_command(command: list[str], *, text: bool = True) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=text,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "unavailable_reason": (
                f"command timed out after {_SUBPROCESS_TIMEOUT_SECONDS} seconds: {command[0]}"
            ),
        }
    except OSError as exc:
        return {
            "available": False,
            "unavailable_reason": f"command unavailable ({command[0]}): {exc.strerror}",
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "unavailable_reason": f"command exited {completed.returncode}: {command[0]}",
        }
    return {"available": True, "stdout": completed.stdout}


def _normalize_peak_rss(value: int | float, system: str) -> tuple[int, str]:
    if system == "Darwin":
        return int(value), "bytes"
    if system == "Linux":
        return int(value) * 1024, "KiB"
    return int(value), "platform-defined units"


def _write_profiles(profiler: cProfile.Profile, binary: Path, text: Path) -> None:
    temporary = binary.with_name(f".{binary.name}.tmp-{uuid.uuid4().hex}")
    try:
        profiler.dump_stats(str(temporary))
        _fsync_file(temporary)
        os.replace(temporary, binary)
        _fsync_file(binary)
        _fsync_directory(binary.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats()
    _atomic_write_bytes(text, stream.getvalue().encode("utf-8"))


def _file_evidence(path: Path) -> dict[str, Any]:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_path(path)}


def _oracle_error(error: Any) -> dict[str, Any]:
    try:
        return asdict(error)
    except TypeError:
        return {
            "code": str(getattr(error, "code", "unknown")),
            "message": str(getattr(error, "message", error)),
            "artifact": getattr(error, "artifact", None),
        }


def _validation_evidence(validation: ValidationReceipt) -> dict[str, Any]:
    return {
        "valid": validation.valid,
        "errors": [_oracle_error(error) for error in validation.errors],
        "verifier_report": validation.verifier_report,
        "canonical_sha256": (
            canonical_sha256(validation.canonical)
            if validation.canonical is not None
            else None
        ),
    }


def _equivalence_evidence(equivalence: EquivalenceReceipt) -> dict[str, Any]:
    return {
        "equivalent": equivalence.equivalent,
        "errors": [_oracle_error(error) for error in equivalence.errors],
        "control_valid": equivalence.control.valid,
        "trace_valid": equivalence.candidate.valid,
        "control_canonical_sha256": (
            canonical_sha256(equivalence.control.canonical)
            if equivalence.control.canonical is not None
            else None
        ),
        "trace_canonical_sha256": (
            canonical_sha256(equivalence.candidate.canonical)
            if equivalence.candidate.canonical is not None
            else None
        ),
    }


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _publish_receipt(
    receipt_path: Path,
    receipt_bytes: bytes,
    sidecar_path: Path,
    sidecar_bytes: bytes,
    *,
    existing_workspace_bytes: int,
    cap_bytes: int,
) -> None:
    final_bytes = existing_workspace_bytes + len(receipt_bytes) + len(sidecar_bytes)
    if final_bytes > cap_bytes:
        raise BenchmarkError(
            "final_evidence_too_large",
            f"Benchmark final evidence {final_bytes} exceeds cap {cap_bytes}",
        )
    _atomic_write_bytes(sidecar_path, sidecar_bytes)
    try:
        _atomic_write_bytes(receipt_path, receipt_bytes)
    except BaseException as publication_error:
        cleanup_errors: list[BaseException] = []
        try:
            if os.path.lexists(receipt_path):
                receipt_path.unlink()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        try:
            _fsync_directory(receipt_path.parent)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BenchmarkError(
                "receipt_cleanup_failed",
                "Receipt publication failed and cleanup could not be made durable; "
                "measurement is invalid",
            ) from publication_error
        raise


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_file(path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
