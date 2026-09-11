"""Durable, single-writer recovery records, separate from portable run artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .adapters import ADAPTER_FAILURE_CODES, AdapterFailure, ExtractionResult
from .image_evidence import validate_input_evidence
from .replay import _adapter_code_sha256, _package_code_sha256

PAGE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
STATES = {"pending", "started", "outcome_unknown", "response", "completed", "failed"}
SAFE_ERROR_CODES = ADAPTER_FAILURE_CODES
RESULT_FIELDS = {"content", "format", "confidence", "model", "warnings", "usage", "confidence_detail", "input_evidence"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_bytes(path: Path, content: bytes) -> None:
    """Publish complete bytes, including file and directory durability barriers."""
    if path.is_symlink() or not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("Unsafe recovery artifact path")
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_record(path: Path, payload: dict[str, Any]) -> None:
    envelope = {"schema_version": "0.1", "sha256": digest(payload), "payload": payload}
    atomic_bytes(path, (json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                                  allow_nan=False, indent=2) + "\n").encode())


def read_record(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing or unsafe record")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(envelope, dict)
                or set(envelope) != {"schema_version", "sha256", "payload"}
                or envelope["schema_version"] != "0.1"
                or not isinstance(envelope["payload"], dict)
                or envelope["sha256"] != digest(envelope["payload"])):
            raise ValueError("invalid envelope or checksum")
        return envelope["payload"]
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ValueError(f"Recovery record invalid: {path.name}") from exc


@contextmanager
def writer_lock(root: Path):
    """OS advisory lock: termination releases it, the retained file is harmless."""
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError("Resumable runs require POSIX advisory locks") from exc
    path = root / ".resume.lock"
    if path.is_symlink():
        raise ValueError("Unsafe recovery lock")
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Run already has an active writer") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def adapter_identity(adapter: Any, profile: Any) -> dict[str, Any]:
    return {
        "name": adapter.name if adapter is not None else None,
        "version": adapter.version if adapter is not None else None,
        "code_sha256": _adapter_code_sha256(adapter) if adapter is not None else None,
        "package_sha256": _package_code_sha256(),
        "profile": profile,
        "metadata": {
            key: list(getattr(adapter, key, ()))
            for key in ("input_types", "output_types", "capabilities")
        } | {"deterministic": getattr(adapter, "deterministic", None)},
    }


class Checkpoint:
    def __init__(self, root: Path, *, existing: bool = False):
        self.root = root
        self.job: dict[str, Any] | None = None
        self.records: dict[str, dict[str, Any]] = {}
        if existing:
            self.job = read_record(root / "checkpoint.json")
            try:
                self.validate()
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("Malformed recovery evidence") from exc

    def page_path(self, page_id: str) -> Path:
        if not isinstance(page_id, str) or not PAGE_ID.fullmatch(page_id):
            raise ValueError("Invalid recovery page identity")
        return self.root / ".checkpoint" / "pages" / f"{page_id}.json"

    def initialize(self, job: dict[str, Any]) -> None:
        self.job = job
        pages_dir = self.root / ".checkpoint" / "pages"
        pages_dir.mkdir(parents=True)
        _sync_directory(pages_dir.parent)
        _sync_directory(self.root)
        for document in job["documents"]:
            for page in document["pages"]:
                self.save(page["page_id"], {"state": "pending"})
        write_record(self.root / "checkpoint.json", job)

    def save(self, page_id: str, record: dict[str, Any]) -> None:
        assert self.job is not None
        value = {"run_id": self.job["run_id"], "page_id": page_id, **record}
        write_record(self.page_path(page_id), value)
        self.records[page_id] = value

    def check_sources(self) -> None:
        assert self.job is not None
        inputs = self.job.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("Invalid recovery source identity inventory")
        for source in inputs:
            if (not isinstance(source, dict) or not isinstance(source.get("path"), str)
                    or not isinstance(source.get("sha256"), str)):
                raise ValueError("Invalid recovery source identity")
            try:
                if file_digest(Path(source["path"])) != source["sha256"]:
                    raise ValueError("source changed")
            except (OSError, ValueError) as exc:
                raise ValueError("Recovery source identity changed or source is missing") from exc

    def validate(self, *, sources: bool = True) -> None:
        """Validate all pages before any adapter request or artifact reconstruction."""
        job = self.job
        required = {"run_id", "started_at", "root", "config_sha256", "config_source_path",
                    "identity", "inputs", "documents", "imported_routes", "route_warnings",
                    "routing", "log_level"}
        if (not isinstance(job, dict) or set(job) != required
                or not all(isinstance(job[key], str) and job[key]
                           for key in ("run_id", "started_at", "root", "config_sha256",
                                       "config_source_path", "log_level"))
                or job["root"] != str(self.root)
                or not isinstance(job["identity"], dict)
                or set(job["identity"]) != {"name", "version", "code_sha256", "package_sha256", "profile", "metadata"}
                or not isinstance(job["route_warnings"], list)
                or not all(isinstance(warning, str) for warning in job["route_warnings"])
                or (job["imported_routes"] is not None and not isinstance(job["imported_routes"], dict))
                or (job["routing"] is not None and not isinstance(job["routing"], dict))
                or not isinstance(job["documents"], list)
                or not isinstance(job["inputs"], list)
                or not job["inputs"]
                or len(job["inputs"]) != len(job["documents"])):
            raise ValueError("Invalid recovery job identity or plan")
        snapshot = self.root / "config-snapshot.yml"
        try:
            if snapshot.is_symlink() or file_digest(snapshot) != job["config_sha256"]:
                raise ValueError("snapshot changed")
        except (OSError, ValueError) as exc:
            raise ValueError("Recovery config identity changed or snapshot is missing") from exc
        from .aligner import load_schema_spec
        from .config import load_config
        config = load_config(snapshot, validate_adapter=False)
        schema_spec = load_schema_spec(config.data)
        expected: dict[str, dict[str, Any]] = {}
        page_sources: dict[str, str] = {}
        source_paths: set[str] = set()
        for source, document in zip(job["inputs"], job["documents"], strict=True):
            if (not isinstance(source, dict) or not isinstance(document, dict)
                    or type(source.get("page_count")) is not int or source["page_count"] < 1
                    or not isinstance(source.get("path"), str)
                    or not Path(source["path"]).is_absolute()
                    or document.get("source") != source["path"]
                    or document.get("source_sha256") != source.get("sha256")
                    or document.get("page_count") != source["page_count"]
                    or not isinstance(document.get("pages"), list)):
                raise ValueError("Invalid recovery source inventory")
            if source["path"] in source_paths:
                raise ValueError("Duplicate recovery source identity")
            source_paths.add(source["path"])
            numbers: set[int] = set()
            for page in document["pages"]:
                if (not isinstance(page, dict) or not isinstance(page.get("page_id"), str)
                        or type(page.get("page_number")) is not int
                        or not 1 <= page["page_number"] <= source["page_count"]
                        or page["page_number"] in numbers or page["page_id"] in expected
                        or not isinstance(page.get("action"), str)):
                    raise ValueError("Invalid recovery page inventory")
                self.page_path(page["page_id"])
                numbers.add(page["page_number"])
                expected[page["page_id"]] = page
                page_sources[page["page_id"]] = source["sha256"]
            from .runner import _parse_pages_expression
            planned_numbers = (set(_parse_pages_expression(source["pages"])) if "pages" in source
                               else set(range(1, source["page_count"] + 1)))
            if numbers != planned_numbers:
                raise ValueError("Recovery plan has missing or unexpected source pages")
        pages_dir = self.root / ".checkpoint" / "pages"
        if pages_dir.is_symlink() or pages_dir.parent.is_symlink() or not pages_dir.is_dir():
            raise ValueError("Missing or unsafe recovery inventory")
        actual = {p.name for p in pages_dir.iterdir() if not p.name.startswith(".pending-")}
        if actual != {f"{page_id}.json" for page_id in expected}:
            raise ValueError("Recovery page inventory mismatch: missing or unexpected receipt")
        raw_expected: set[str] = set()
        normalized_expected: set[str] = set()
        for page_id in expected:
            record = read_record(self.page_path(page_id))
            state = record.get("state")
            if not isinstance(state, str):
                raise ValueError("Invalid recovery page state type")
            required_fields = {"run_id", "page_id", "state"}
            if state in {"started", "outcome_unknown", "response", "completed", "failed"}:
                required_fields.add("started_at")
            if state in {"response", "completed"}:
                required_fields.update({"result", "extraction_seconds"})
            if state == "completed":
                required_fields.add("completion")
            if state == "failed":
                required_fields.add("error")
                if "partial_result" in record:
                    required_fields.update({"partial_result", "extraction_seconds"})
            if (set(record) != required_fields or state not in STATES
                    or record["run_id"] != job["run_id"] or record["page_id"] != page_id):
                raise ValueError("Invalid recovery page state")
            if state == "failed":
                error = record["error"]
                if (not isinstance(error, dict)
                        or set(error) != {"type", "code", "http_status", "cost_usd"}
                        or not isinstance(error["type"], str)
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", error["type"])
                        or not isinstance(error["code"], str)
                        or error["code"] not in SAFE_ERROR_CODES | {"adapter_failure", "timeout"}
                        or (error["cost_usd"] is not None and (
                            type(error["cost_usd"]) not in {int, float}
                            or not math.isfinite(error["cost_usd"])
                            or error["cost_usd"] < 0))
                        or (error["http_status"] is not None
                            and (type(error["http_status"]) is not int
                                 or not 100 <= error["http_status"] <= 599))):
                    raise ValueError("Invalid typed recovery failure")
                if "partial_result" in record:
                    self._validate_saved_result(record["partial_result"],
                                                page_sources[page_id], expected[page_id])
                    seconds = record["extraction_seconds"]
                    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0:
                        raise ValueError("Invalid partial extraction duration")
                    if error["cost_usd"] != record["partial_result"]["usage"].get("cost_usd"):
                        raise ValueError("Partial response cost disagrees with failure")
                elif error["cost_usd"] is not None:
                    raise ValueError("Failure cost requires validated partial response")
            if "started_at" in record and not isinstance(record["started_at"], str):
                raise ValueError("Invalid recovery attempt timestamp")
            if state in {"response", "completed"}:
                result_data = record["result"]
                result = self._validate_saved_result(result_data, page_sources[page_id], expected[page_id])
                seconds = record["extraction_seconds"]
                if type(seconds) not in {int, float} or seconds < 0:
                    raise ValueError("Invalid saved extraction duration")
                extension = "txt" if result.format == "text" else result.format
                relative = f"raw/{page_id}.{extension}"
                raw = self.root / relative
                raw_expected.add(raw.name)
                text = result.content if isinstance(result.content, str) else json.dumps(
                    result.content, ensure_ascii=False, allow_nan=False)
                expected_sha = hashlib.sha256(text.encode()).hexdigest()
                if state == "completed" or raw.exists() or raw.is_symlink():
                    self._check_file(raw, expected_sha)
                if state == "response":
                    normalized = self.root / "normalized" / f"{page_id}.json"
                    if normalized.exists() or normalized.is_symlink():
                        from .aligner import ALIGNABLE_FORMATS, align_page
                        alignment = (align_page(result.content, result.format, schema_spec,
                                     page=expected[page_id], run_id=job["run_id"],
                                     schema_version=config.schema_version, raw_artifact=relative)
                                     if schema_spec is not None and result.format in ALIGNABLE_FORMATS
                                     else None)
                        if (normalized.is_symlink() or not normalized.is_file()
                                or alignment is None
                                or json.loads(normalized.read_text()) != alignment):
                            raise ValueError("Normalized response evidence changed")
                        normalized_expected.add(normalized.name)
                if state == "completed":
                    completion = record["completion"]
                    if (not isinstance(completion, dict)
                            or set(completion) != {"provenance", "quality", "alignment"}
                            or not isinstance(completion["provenance"], dict)
                            or not isinstance(completion["quality"], dict)
                            or completion["provenance"].get("run_id") != job["run_id"]
                            or completion["provenance"].get("page_id") != page_id
                            or completion["quality"].get("page_id") != page_id
                            or completion["provenance"].get("result", {}).get("raw_sha256") != expected_sha
                            or completion["provenance"].get("result", {}).get("raw_artifact") != relative
                            or completion["provenance"].get("input_evidence") != result.input_evidence):
                        raise ValueError("Invalid completion evidence")
                    alignment = completion["alignment"]
                    if alignment is not None:
                        normalized = self.root / "normalized" / f"{page_id}.json"
                        if normalized.is_symlink() or not normalized.is_file():
                            raise ValueError("Missing or unsafe normalized completion")
                        if json.loads(normalized.read_text()) != alignment:
                            raise ValueError("Normalized completion changed")
                        normalized_expected.add(normalized.name)
            self.records[page_id] = record
        unfinished = False
        for page_id, page in expected.items():
            state = self.records[page_id]["state"]
            if page["action"] in {"review", "skip"}:
                if state != "pending":
                    raise ValueError("Non-extracting page has an invalid attempt")
                continue
            if unfinished and state != "pending":
                raise ValueError("Recovery attempt order contains a gap")
            if state != "completed":
                unfinished = True
        for directory, allowed in (("raw", raw_expected), ("normalized", normalized_expected)):
            path = self.root / directory
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Missing or unsafe recovery output directory")
            if any(p.name not in allowed and not p.name.startswith(".pending-") for p in path.iterdir()):
                raise ValueError("Unexpected recovery output artifact")
        if sources:
            self.check_sources()

    def _validate_saved_result(self, data: Any, source_sha256: str,
                               page: dict[str, Any]) -> ExtractionResult:
        from .runner import _validate_extraction_result
        if (not isinstance(data, dict) or set(data) not in
                (RESULT_FIELDS, RESULT_FIELDS - {"input_evidence"})):
            raise ValueError("Invalid saved response")
        result = ExtractionResult(**data)
        _validate_extraction_result("checkpoint", result)
        if result.input_evidence is not None:
            validate_input_evidence(result.input_evidence, root=self.root,
                                    source_sha256=source_sha256, page_number=page["page_number"],
                                    prompt_sha256=hashlib.sha256((page.get("prompt") or "").encode()).hexdigest())
        return result

    @staticmethod
    def _check_file(path: Path, expected: str) -> None:
        try:
            if path.is_symlink() or file_digest(path) != expected:
                raise ValueError("output changed")
        except (OSError, ValueError) as exc:
            raise ValueError("Recovery output changed or completed output is missing") from exc

    def require_resumable(self) -> None:
        for page_id, record in self.records.items():
            if record["state"] == "started":
                self.save(page_id, {**record, "state": "outcome_unknown"})
        if any(r["state"] == "outcome_unknown" for r in self.records.values()):
            raise ValueError("outcome_unknown: request started without a durable outcome; refusing further calls")
        if any(r["state"] == "failed" for r in self.records.values()):
            raise ValueError("Known adapter failure retained; refusing further calls")

    def complete(self, page_id: str, *, provenance: Any, quality: Any, alignment: Any) -> None:
        record = self.records[page_id]
        completion = {"provenance": provenance, "quality": quality, "alignment": alignment}
        if record["state"] == "completed":
            if record["completion"] != completion:
                raise ValueError("Reconstructed page evidence disagrees with durable completion")
            return
        self.save(page_id, {**record, "state": "completed", "completion": completion})

    def extract(self, *, adapter: Any, source: Path, page: dict[str, Any], prompt: str | None):
        import time

        from .runner import _utc_now, _validate_extraction_result
        page_id = page["page_id"]
        record = self.records[page_id]
        if record["state"] in {"response", "completed"}:
            return (ExtractionResult(**record["result"]), record["extraction_seconds"],
                    record["started_at"], 1, None)
        if record["state"] != "pending":
            raise ValueError("Recovery page does not permit another adapter call")
        started_at = _utc_now()
        self.save(page_id, {"state": "started", "started_at": started_at})
        started = time.perf_counter()
        try:
            result = adapter.extract(source, page_id=page_id, page_number=page["page_number"],
                                     action=page["action"], prompt=prompt)
            elapsed = round(time.perf_counter() - started, 3)
            _validate_extraction_result(adapter.name, result)
            if getattr(result, "input_evidence", None) is not None:
                validate_input_evidence(result.input_evidence, root=self.root,
                                        source_sha256=file_digest(source), page_number=page["page_number"],
                                        prompt_sha256=hashlib.sha256((prompt or "").encode()).hexdigest())
            result_data = {key: getattr(result, key, None) for key in RESULT_FIELDS}
        except Exception as exc:
            error_type = type(exc).__name__
            error: dict[str, Any] = {"type": error_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", error_type) else "AdapterError",
                     "code": "adapter_failure", "http_status": None, "cost_usd": None}
            if isinstance(exc, TimeoutError):
                error["code"] = "timeout"
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code in SAFE_ERROR_CODES:
                error["code"] = code
            for value in (getattr(exc, "http_status", None), getattr(exc, "status_code", None), getattr(exc, "status", None),
                          getattr(exc, "code", None), getattr(getattr(exc, "response", None), "status_code", None)):
                if type(value) is int and 100 <= value <= 599:
                    error["http_status"] = value
                    break
            failed: dict[str, Any] = {"state": "failed", "started_at": started_at, "error": error}
            if isinstance(exc, AdapterFailure) and exc.partial_result is not None:
                partial: dict[str, Any] = {key: getattr(exc.partial_result, key, None) for key in RESULT_FIELDS}
                try:
                    self._validate_saved_result(partial, file_digest(source), {**page, "prompt": prompt})
                    cost = partial["usage"].get("cost_usd")
                    if cost is not None and cost < 0:
                        raise ValueError("Negative partial cost")
                except (OSError, ValueError, TypeError, AttributeError):
                    error["code"] = "IMAGE_EVIDENCE_INVALID"
                else:
                    failed["partial_result"] = partial
                    failed["extraction_seconds"] = round(time.perf_counter() - started, 3)
                    error["cost_usd"] = cost
            self.save(page_id, failed)
            raise RuntimeError(f"Adapter {error['type']} (HTTP {error['http_status']}); outcome retained, queued work stopped") from None
        # Persistence failures remain started/outcome_unknown; never label them provider failures.
        self.save(page_id, {"state": "response", "started_at": started_at,
                            "extraction_seconds": elapsed, "result": result_data})
        return result, elapsed, started_at, 1, None
