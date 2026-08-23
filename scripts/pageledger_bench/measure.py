"""One-process PageLedger benchmark measurement and evidence receipts."""

from __future__ import annotations

import cProfile
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import plistlib
import pstats
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pageledger.runner import run as runner_run

from .oracle import ValidationReceipt, validate_run
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


class BenchmarkError(RuntimeError):
    """Fail-closed benchmark refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def measure_run(
    workload_name: str,
    out_dir: Path,
    *,
    process_state: str,
    command: list[str] | None = None,
    _prepared_workload: WorkloadSpec | None = None,
    _oracle_validator: Callable[[Path, WorkloadSpec], ValidationReceipt] = DEFAULT_ORACLE_VALIDATOR,
) -> dict[str, Any]:
    """Generate, preload, and measure exactly one ``runner.run`` call."""
    if process_state not in {"fresh-process", "cold", "warm"}:
        raise BenchmarkError("process_state_invalid", f"Unknown process state: {process_state}")
    requested_out = Path(out_dir).expanduser().absolute()
    _refuse_output_path(requested_out)

    frozen_manifest = load_frozen_manifest()
    cap_bytes = int(frozen_manifest["temp_output_cap_bytes"])
    protected = _protected_hashes(frozen_manifest["protected_paths"])
    page_count = (
        len(_prepared_workload.page_specs)
        if _prepared_workload is not None
        else int(frozen_manifest["workloads"][workload_name]["page_count"])
    )
    projected_bytes = page_count * _PROJECTED_BYTES_PER_PAGE
    free_bytes = shutil.disk_usage(_nearest_existing_parent(requested_out)).free
    _check_capacity(
        requested_out,
        projected_bytes=projected_bytes,
        cap_bytes=cap_bytes,
        free_bytes=free_bytes,
    )
    host = _host_fingerprint(requested_out, process_state, free_bytes)
    git = _git_fingerprint()

    requested_out.mkdir(parents=True)
    workload = _prepared_workload or DEFAULT_WORKLOAD_FACTORY(
        workload_name, requested_out / "workload"
    )
    adapter = ZeroWorkAdapter(workload.page_specs)
    run_dir = requested_out / "run"
    profile_path = requested_out / "profile.pstats"
    profile_text_path = requested_out / "profile.txt"
    receipt_path = requested_out / "measurement.json"
    receipt_hash_path = requested_out / "measurement.json.sha256"
    events: list[tuple[str, int]] = []

    profiler = cProfile.Profile()
    wall_started = time.perf_counter_ns()
    profiler.runcall(
        runner_run,
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

    _write_profiles(profiler, profile_path, profile_text_path)
    inventory = _regular_file_inventory(run_dir, cap_bytes=cap_bytes)
    manifest_last = _manifest_is_last(run_dir, inventory["paths"])
    if not manifest_last:
        raise BenchmarkError(
            "manifest_not_last",
            "manifest.json was not the final run artifact write",
        )
    validation = _oracle_validator(run_dir, workload)
    timing = _summarize_timing(events, total_wall_ns, len(workload.page_specs))
    _require_timing_contract(timing, len(workload.page_specs))
    workspace_inventory = _regular_file_inventory(requested_out, cap_bytes=cap_bytes)
    frozen_hashes = {
        "benchmark_manifest_sha256": sha256_path(_MANIFEST_PATH),
        "schema_sha256": dict(frozen_manifest["schema_sha256"]),
        "recipe_sha256": dict(frozen_manifest["recipe_sha256"]),
        "workloads_sha256": {
            name: dict(spec["hashes"])
            for name, spec in sorted(frozen_manifest["workloads"].items())
        },
        "protected_paths_sha256": protected,
    }
    schema_matched = all(
        sha256_path(_REPOSITORY_ROOT / "schemas" / filename) == expected
        for filename, expected in frozen_manifest["schema_sha256"].items()
    )
    receipt: dict[str, Any] = {
        "receipt_version": 1,
        "status": "pass" if validation.valid and schema_matched else "fail",
        "command": list(command or sys.argv),
        "workload": {
            "name": workload.name,
            "generator_version": (
                GENERATOR_VERSION if workload.name in frozen_manifest["workloads"] else "test-seam"
            ),
            "page_count": len(workload.page_specs),
            "category_counts": dict(
                sorted(Counter(page.category for page in workload.page_specs).items())
            ),
            "generated_sha256": {
                name: sha256_path(path) for name, path in sorted(workload.generated_paths.items())
            },
            "frozen_contract": frozen_manifest["workloads"].get(workload.name),
        },
        "frozen_hashes": frozen_hashes,
        "git": git,
        "host": host,
        "timing": timing,
        "resources": {
            "peak_rss_bytes": peak_rss_bytes,
            "ru_maxrss_source_unit": source_unit,
        },
        "output": {
            "run_dir": str(run_dir),
            "regular_file_count": inventory["regular_file_count"],
            "logical_bytes": inventory["logical_bytes"],
            "cap_bytes": cap_bytes,
            "projected_bytes": projected_bytes,
            "manifest_last_artifact_write": manifest_last,
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
            "oracle": {
                "valid": validation.valid,
                "errors": [_oracle_error(error) for error in validation.errors],
                "canonical_sha256": (
                    canonical_sha256(validation.canonical)
                    if validation.canonical is not None
                    else None
                ),
            },
            "schema_state": {
                "status": "matched" if schema_matched else "drifted",
                "sha256": dict(frozen_manifest["schema_sha256"]),
            },
        },
        "receipt_sha256_sidecar": str(receipt_hash_path),
    }
    receipt["receipt_payload_sha256"] = canonical_sha256(receipt)
    _atomic_write_json(receipt_path, receipt)
    _atomic_write_bytes(
        receipt_hash_path,
        f"{sha256_path(receipt_path)}\n".encode("ascii"),
    )
    _regular_file_inventory(requested_out, cap_bytes=cap_bytes)
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


def _require_timing_contract(timing: dict[str, Any], pages: int) -> None:
    phases = set(timing["ledger"]["phases"])
    if phases != _DIRECT_LEDGER_PHASES:
        missing = sorted(_DIRECT_LEDGER_PHASES - phases)
        extra = sorted(phases - _DIRECT_LEDGER_PHASES)
        raise BenchmarkError(
            "timing_phase_set_mismatch",
            f"direct phase set mismatch (missing={missing}, extra={extra})",
        )
    adapter_count = timing["adapter"]["count"]
    if adapter_count != pages:
        raise BenchmarkError(
            "timing_adapter_count_mismatch",
            f"direct adapter span count {adapter_count} does not match page count {pages}",
        )


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


def _nearest_existing_parent(path: Path) -> Path:
    current = path.parent
    while not current.exists():
        current = current.parent
    return current


def _check_capacity(path: Path, *, projected_bytes: int, cap_bytes: int, free_bytes: int) -> None:
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


def _manifest_is_last(run_dir: Path, paths: list[Path]) -> bool:
    manifest = run_dir / "manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    manifest_mtime = manifest.stat().st_mtime_ns
    return all(path == manifest or path.stat().st_mtime_ns <= manifest_mtime for path in paths)


def _protected_hashes(relative_paths: list[str]) -> dict[str, str]:
    hashes = _current_protected_hashes(relative_paths)
    for relative in relative_paths:
        current = (_REPOSITORY_ROOT / relative).read_bytes()
        if current != _git_blob_bytes(relative):
            raise BenchmarkError(
                "protected_hash_drift",
                f"Refusing protected benchmark path drift: {relative}",
            )
    return hashes


def _current_protected_hashes(relative_paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = _REPOSITORY_ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise BenchmarkError(
                "protected_path_invalid",
                f"Protected benchmark path must be a regular file: {relative}",
            )
        hashes[relative] = sha256_path(path)
    return hashes


def _git_blob_bytes(relative: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise BenchmarkError(
            "protected_path_untracked",
            f"Protected benchmark path is absent from HEAD: {relative}",
        )
    return completed.stdout


def _git_fingerprint() -> dict[str, Any]:
    sha = _git_text("rev-parse", "HEAD")
    branch = _git_text("branch", "--show-current")
    status = _git_text("status", "--porcelain", "--untracked-files=no")
    return {"sha": sha, "branch": branch, "tracked_dirty": bool(status)}


def _git_text(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BenchmarkError("git_unavailable", f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _host_fingerprint(path: Path, process_state: str, free_bytes: int) -> dict[str, Any]:
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
        "filesystem": _volume_fingerprint(output_parent, free_bytes),
        "temp_volume": _volume_fingerprint(temp_root, shutil.disk_usage(temp_root).free),
        "power_mode": _power_mode(),
        "process_state": process_state,
        "os_page_cache": "uncontrolled",
    }


def _dependency_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _cpu_description() -> str:
    if platform.system() == "Darwin":
        completed = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
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
        volume = subprocess.run(
            ["df", "-P", str(path)], check=False, capture_output=True, text=True
        )
        lines = volume.stdout.splitlines()
        if volume.returncode == 0 and len(lines) >= 2:
            device = lines[-1].split()[0]
            info = subprocess.run(
                ["diskutil", "info", "-plist", device],
                check=False,
                capture_output=True,
            )
            if info.returncode == 0:
                try:
                    filesystem = plistlib.loads(info.stdout).get("FilesystemType")
                except plistlib.InvalidFileException:
                    filesystem = None
                if isinstance(filesystem, str) and filesystem:
                    return filesystem
        return "unavailable"
    completed = subprocess.run(
        ["stat", "-f", "-c", "%T", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return "unavailable"


def _power_mode() -> dict[str, str]:
    if platform.system() == "Darwin":
        completed = subprocess.run(
            ["pmset", "-g"], check=False, capture_output=True, text=True
        )
        if completed.returncode == 0:
            for line in completed.stdout.splitlines():
                setting = line.strip().split()
                if setting and setting[0] in {"powermode", "lowpowermode"}:
                    value = line.split()[-1]
                    return {"mode": f"{setting[0]}={value}"}
        return {"unavailable_reason": "pmset did not report a power mode"}
    profile = Path("/sys/firmware/acpi/platform_profile")
    try:
        mode = profile.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return {"unavailable_reason": f"platform power profile unavailable: {exc.strerror}"}
    return {"mode": mode or "unavailable"}


def _normalize_peak_rss(value: int | float, system: str) -> tuple[int, str]:
    if system == "Darwin":
        return int(value), "bytes"
    if system == "Linux":
        return int(value) * 1024, "KiB"
    return int(value), "platform-defined units"


def _write_profiles(profiler: cProfile.Profile, binary: Path, text: Path) -> None:
    temporary = binary.with_name(f".{binary.name}.tmp-{uuid.uuid4().hex}")
    profiler.dump_stats(str(temporary))
    os.replace(temporary, binary)
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


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_bytes(
        path,
        (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode("utf-8"),
    )


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
