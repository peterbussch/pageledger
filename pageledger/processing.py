"""Document-level processing policy over durable, generation-zero extraction runs."""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import math
import shlex
import sys
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from . import adjudication, corrections, processing_policy, runner
from .adapters import PageLedgerDiagnostic
from .checkpoint import (
    Checkpoint,
    atomic_bytes,
    digest,
    file_digest,
    read_record,
    write_record,
    writer_lock,
)
from .classifier import classify_signals, merge_classify_thresholds, structural_signals
from .comparison import compare_texts
from .config import load_config
from .contest import contest as contest_spans
from .image_evidence import validate_tile_text
from .image_source import (
    KIND,
    manifest_changes,
    read_image_source,
    snapshot_growth,
    snapshot_warnings,
)
from .layout import layout_sha256
from .lexicon import load_lexicon, roughness
from .processing_config import LAYOUT_STAGES, ladder, processing_config
from .processing_policy import (
    HOLD_POLICIES,
    HOLD_POLICY,
    warning_holds,
)
from .processing_source import TEXT, inspect_source, source_kind
from .replay import _package_code_sha256
from .review_sheet import _corrected as review_sheet_corrected
from .review_sheet import read_review_decisions, read_review_sheet, write_review_sheet
from .source_index import SourceUnavailable, resolve_source
from .tiling import centres_inside, check_edges, plan_sha256, plan_tiles, project_witness
from .verify import verify_run


def _page_id(number: int) -> str:
    return f"doc_0001_page_{number:04d}"


def _save(job: dict, root: Path) -> None:
    value = copy.deepcopy(job)
    for page in value["pages"]:
        for attempt in page["attempts"]:
            attempt.pop("text", None)
    write_record(root / "job.json", value)


def _publish(job: dict, root: Path, *, check_source: bool = True) -> dict:
    from .document_report import write_document_report

    if check_source:
        _check_source(job)
    _save(job, root)
    write_document_report(job, root)
    return {
        "job_id": job["job_id"],
        "out_dir": str(root),
        "status": job["status"],
        "report": str(root / "report.md"),
        "next_action": job["next_action"],
    }


def _check_source(job: dict) -> Path:
    """Return where the recorded source bytes are now; job.json keeps the recorded path."""
    try:
        return resolve_source(
            job["source"]["path"], job["source"]["sha256"], job["source"].get("kind")
        )
    except SourceUnavailable as exc:
        raise ValueError("Document job source changed or is missing" + _changes(job)) from exc


def _changes(job: dict) -> str:
    """Which page images differ from the recorded ones, when the folder is still there."""
    source = job["source"]
    if source.get("kind") != KIND or not Path(source["path"]).exists():
        return ""
    try:
        current = read_image_source(Path(source["path"])).manifest()
    except (OSError, ValueError, RuntimeError) as exc:
        return f": {exc}"
    return ": " + "; ".join(manifest_changes(source["images"], current))


def _pdf_like(source: dict) -> bool:
    """Page images are read through a PDF carrier, so they take the PDF stage defaults."""
    return source.get("kind") == KIND or Path(source["path"]).suffix.lower() == ".pdf"


def _build_comparisons(attempts: list[dict]) -> list[dict]:
    readable = [
        item for item in attempts if item["outcome"] == "completed" and item["text"].strip()
    ]
    return [
        {
            "left_attempt": left["attempt_id"],
            "right_attempt": right["attempt_id"],
            **compare_texts(left["text"], right["text"]),
        }
        for index, left in enumerate(readable)
        for right in readable[index + 1 :]
    ]


def _safe(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Unsafe document job artifact path")
    full = root / path
    if any(item.is_symlink() for item in [full, *full.parents] if item != root.parent):
        raise ValueError("Unsafe document job symlink")
    if not full.resolve().is_relative_to(root):
        raise ValueError("Document job artifact escapes its root")
    return full


# Commands that may change a job outside the directory it was written in, as recorded,
# and as a person types them now.
RELOCATED_WRITERS = ("review-job", "adjudicate")
_WRITER_COMMANDS = {"review-job": "review --apply", "adjudicate": "adjudicate"}


def _load(root: Path) -> dict:
    """Read and check a job wherever it is now; ``root`` in job.json is where it was written."""
    job = read_record(root / "job.json")
    if job.get("schema_version") != "0.1" or not isinstance(job.get("root"), str):
        raise ValueError("Document job identity changed")
    relocations = job.get("relocations", [])
    if not isinstance(relocations, list) or any(
        not isinstance(item, dict)
        or set(item) != {"root", "command", "at"}
        or not all(isinstance(value, str) for value in item.values())
        or item["command"] not in RELOCATED_WRITERS
        for item in relocations
    ):
        raise ValueError("Document job relocations are invalid")
    if job.get("config_sha256") != digest(job.get("config")):
        raise ValueError("Document job configuration changed")
    config = job["config"]
    expected_policy = processing_config(config, pdf=_pdf_like(job["source"]))
    if job.get("policy") != expected_policy or job.get("limits") != _effective_limits(
        expected_policy["limits"], job.get("limits_history", [])
    ):
        raise ValueError("Document processing policy changed")
    selected = job.get("selected_pages")
    count = job["source"]["page_count"]
    images = job["source"].get("images")
    if (
        not isinstance(selected, list)
        or selected != sorted(set(selected))
        or any(type(n) is not int or count is None or not 1 <= n <= count for n in selected)
        or [p["page_number"] for p in job["pages"]] != selected
        or any(
            p["page_id"] != _page_id(p["page_number"])
            or p["source_sha256"] != job["source"]["sha256"]
            or p.get("source_image_sha256")
            != (images[p["page_number"] - 1]["sha256"] if images else None)
            for p in job["pages"]
        )
    ):
        raise ValueError("Document job page inventory changed")
    identifiers = set()
    from .processing_policy import validate_review

    for page in job["pages"]:
        history = page.get("review_history", [])
        if not isinstance(history, list) or (history and page["review"] != history[-1]):
            raise ValueError("Active review differs from retained review history")
        for receipt in history:
            validate_review(receipt, page)
        _check_corrections(root, page)
        if not isinstance(page.get("adjudications", []), list):
            raise ValueError("Document job adjudications are invalid")
        for receipt in page.get("adjudications", []):
            adjudication.validate_receipt(receipt, page)
    for stage in job["stages"]:
        if (
            stage["stage"] not in ladder(job["policy"])
            or stage["stage_id"] in identifiers
            or stage["pages"] != sorted(set(stage["pages"]))
            or not stage["pages"]
            or not set(stage["pages"]).issubset(selected)
            or stage["run_path"] != f"attempts/{stage['stage_id']}"
        ):
            raise ValueError("Document job attempt plan changed")
        identifiers.add(stage["stage_id"])
        path = _safe(root, stage["config_path"])
        if file_digest(path) != stage["config_sha256"]:
            raise ValueError("Document job attempt configuration changed")
        _safe(root, stage["run_path"])
    return job


def _check_corrections(root: Path, page: dict, pending: frozenset[str] = frozenset()) -> None:
    """Refuse a corrected text that changed, is missing, or starts from nothing recorded.

    ``pending`` are the hashes of corrections checked before they are written.
    """
    for _, correction in corrections.chain(page.get("review_history", []), page["page_id"]):
        path = _safe(root, correction["artifact"])
        if correction["sha256"] in pending and not path.exists():
            continue
        if not path.is_file() or file_digest(path) != correction["sha256"]:
            raise ValueError(f"Corrected text changed or is missing: {correction['artifact']}")


def _relocated(job: dict, root: Path) -> bool:
    return job["root"] != str(root)


def _note_relocation(job: dict, root: Path, command: str) -> None:
    """Record a change made to a job somewhere other than where it was written."""
    if _relocated(job, root):
        job.setdefault("relocations", []).append(
            {"root": str(root), "command": command, "at": runner._utc_now()}
        )


def _refuse_relocated_resume(job: dict, root: Path) -> None:
    # Each stage run is bound to the directory the job was written in, and a planned image
    # stage writes its evidence there; a second copy resuming would also pay for its pages.
    if _relocated(job, root):
        raise ValueError(
            f"This job was written in {job['root']} and is now in {root}. It resumes only "
            f"where it was written: move it back to {job['root']} to resume it. verify, "
            "review, adjudicate and export work where it is now."
        )


def _location_warnings(job: dict, root: Path) -> list[dict]:
    """What verification reports about where the job and its source are now."""
    warnings = []
    if _relocated(job, root):
        warnings.append(
            {
                "code": "job_relocated",
                "message": f"Job was written in {job['root']} and is now in {root}",
                "recorded_root": job["root"],
                "root": str(root),
            }
        )
    for item in job.get("relocations", []):
        warnings.append(
            {
                "code": "job_changed_elsewhere",
                "message": (
                    f"{_WRITER_COMMANDS[item['command']]} changed this job in {item['root']} "
                    f"on {item['at']}, not where it was written"
                ),
                **item,
            }
        )
    recorded = job["source"]["path"]
    located: Path | None = None
    try:
        located = _check_source(job)
    except ValueError:
        grown = snapshot_growth(job["source"])
        if grown is not None:
            return warnings + grown
        if Path(recorded).expanduser().exists():
            raise
        warnings.append(
            {
                "code": "source_missing",
                "message": f"Source file is missing: {recorded}",
                "source": recorded,
            }
        )
    else:
        if located != Path(recorded).expanduser():
            warnings.append(
                {
                    "code": "source_relocated",
                    "message": f"Source found through the source index: {recorded} -> {located}",
                    "source": recorded,
                    "located": str(located),
                }
            )
    return warnings + snapshot_warnings(job["source"], located)


def _source_warnings(job: dict, root: Path) -> list[dict]:
    """For a command that records decisions, which reads none of the source's bytes: a
    missing source is a warning, as in verify, and only other bytes at its path an error."""
    return [
        item
        for item in _location_warnings(job, root)
        if item["code"] not in {"job_relocated", "job_changed_elsewhere"}
    ]


def _stage_checkpoint(job: dict, root: Path, stage: dict) -> Checkpoint:
    """A stage run's checkpoint, bound to where the job recorded it, wherever the job is now."""
    return Checkpoint(
        _safe(root, stage["run_path"]),
        existing=True,
        verify_sources=False,
        recorded_root=Path(job["root"]) / stage["run_path"],
    )


def _review_decisions(job: dict, review: dict) -> None:
    from .processing_policy import validate_review

    if (
        not isinstance(review, dict)
        or set(review) != {"schema_version", "source_sha256", "decisions"}
        or review["schema_version"] != "0.1"
        or review["source_sha256"] != job["source"]["sha256"]
        or not isinstance(review["decisions"], list)
    ):
        raise ValueError("Invalid document review identity or envelope")
    by_id = {p["page_id"]: p for p in job["pages"]}
    seen = set()
    for decision in review["decisions"]:
        if (
            not isinstance(decision, dict)
            or not isinstance(decision.get("page_id"), str)
            or decision["page_id"] not in by_id
            or decision["page_id"] in seen
        ):
            raise ValueError("Review has duplicate or unselected pages")
        seen.add(decision["page_id"])
        bound = {**review, "decisions": [decision]}
        page = by_id[decision["page_id"]]
        validate_review(bound, page)
    for decision in review["decisions"]:
        page = by_id[decision["page_id"]]
        bound = {**review, "decisions": [decision]}
        history = page.setdefault("review_history", [])
        if not history or history[-1] != bound:
            history.append(bound)
        page["review"] = bound


_PROCESS_RUN_GUIDANCE = {
    "budget": (
        "set processing.limits (max_attempt_pages, max_image_pages, max_tokens, max_cost_usd)"
    ),
    "pricing": "document jobs record adapter-reported cost; configured rates are not applied",
    "grading": "document jobs hold pages by review reasons, not grades",
    "rerun_if": "document jobs escalate through processing stages",
    "quarantine_if": "document jobs record source defects through review receipts",
    "adapter_options": "set options on a stage, e.g. processing.local_ocr.adapter_options",
    "max_rerun_depth": "document jobs do not rerun",
    "max_consecutive_failures": "document jobs stop at the first provider failure",
}


def _process_run_settings(data: dict) -> list[str]:
    """Reject run settings a document job would ignore; warn about harmless ones."""
    run_section = data.get("run") or {}
    for key, guidance in _PROCESS_RUN_GUIDANCE.items():
        if key in run_section:
            raise ValueError(f"run.{key} is ignored by process; {guidance}")
    warnings = []
    if "adapter" in run_section:
        warnings.append(
            "run.adapter is ignored by process; stages choose adapters "
            "(processing.local_text.adapter)"
        )
    if "taxonomy" in data:
        warnings.append("taxonomy is ignored by process; document jobs route pages by stage")
    return warnings


def process(
    *,
    source: Path,
    config_path: Path,
    out_dir: Path,
    pages: str | None = None,
    adapter_path: Path | None = None,
    review_path: Path | None = None,
) -> dict:
    warnings = _process_run_settings(load_config(config_path, validate_adapter=False).data)
    result = _process(
        source=source,
        config_path=config_path,
        out_dir=out_dir,
        pages=pages,
        adapter_path=adapter_path,
        review_path=review_path,
    )
    if warnings:
        result["config_warnings"] = warnings
    return result


def _process(
    *,
    source: Path,
    config_path: Path,
    out_dir: Path,
    pages: str | None = None,
    adapter_path: Path | None = None,
    review_path: Path | None = None,
) -> dict:
    source = source.expanduser().resolve()
    if out_dir.expanduser().exists():
        raise ValueError(
            f"A job or another file is already at {out_dir.expanduser()}. "
            f"Use `pageledger inspect {out_dir}`, `pageledger resume {out_dir}`, "
            "or choose another --out directory."
        )
    config = load_config(config_path, validate_adapter=False)
    if config.adapter_order is not None:
        raise ValueError(
            "process uses processing stages; run.adapter_order remains for rerun generations"
        )
    if config.max_retries or config.on_page_error != "stop":
        raise ValueError("process requires zero retries and on_page_error: stop")
    kind = source_kind(source)
    images = read_image_source(source) if kind == KIND else None
    policy = processing_config(config.data, pdf=kind != TEXT)
    if images is not None and any(
        policy[name] is not None and policy[name]["adapter"] == "text" for name in ladder(policy)
    ):
        raise ValueError(
            "The text adapter cannot read page images; use pdf_text for processing.local_text "
            "and an OCR adapter for processing.local_ocr."
        )
    # Build every stage's adapter now, so a bad option fails before the job exists
    # rather than when the first page reaches that stage. Constructors make no requests.
    runner._apply_adapter_path(adapter_path)
    adapters = {}
    for name in ladder(policy):
        if policy[name] is not None:
            try:
                adapter = adapters[name] = runner.load_adapter(
                    policy[name]["adapter"], policy[name]["adapter_options"]
                )
                # alto's {stem} names files after page images, which only they have,
                # and its {page} must not pair page images with another page's file.
                if getattr(adapter, "uses_stem", None) == (images is None):
                    adapter.preflight([source])
            except ValueError as exc:
                raise ValueError(f"processing.{name}: {exc}") from exc
    if policy.get("tiling"):
        _check_tiling_adapters(policy["tiling"], adapters)
    # A missing or unreadable word list fails here, before the job exists.
    lexicon = load_lexicon(policy["lexicon"]).identity if policy.get("lexicon") else None
    source_sha = images.sha256 if images else file_digest(source)
    container_error = None
    container_action = "Obtain a valid source container and start a new job."
    try:
        count, annotations = (
            (len(images.pages), {"status": "none", "count": 0})
            if images
            else inspect_source(source)
        )
    except PageLedgerDiagnostic as exc:
        count, annotations = None, {"status": "unknown", "count": None}
        container_error = f"source_container_invalid:{exc.code}"
        container_action = f"{exc.message} Then start a new job."
    except Exception as exc:
        count, annotations = None, {"status": "unknown", "count": None}
        container_error = f"source_container_invalid:{type(exc).__name__}"
    selected = (
        runner._parse_pages_expression(pages)
        if pages is not None
        else list(range(1, count + 1))
        if count is not None
        else []
    )
    if count is not None and (not selected or max(selected) > count):
        if selected and max(selected) > count:
            first = min(page for page in selected if page > count)
            raise ValueError(f"Page {first} is past the end: the source has {count} pages.")
        raise ValueError("No pages were selected.")
    if container_error:
        selected = []
    attempt_limit = policy["limits"].get("max_attempt_pages")
    configured_stages = [name for name in ladder(policy) if policy.get(name) is not None]
    projected_attempts = len(selected) * len(configured_stages)
    if selected and attempt_limit is not None and projected_attempts > attempt_limit:
        print(
            f"Notice: up to {projected_attempts} attempts are possible for {len(selected)} selected "
            f"pages across {len(configured_stages)} configured stages; max_attempt_pages is "
            f"{attempt_limit}.",
            file=sys.stderr,
        )
    root = out_dir.expanduser().resolve()
    from .checkpoint import _sync_directory

    ancestors = []
    ancestor = root
    while not ancestor.exists():
        ancestors.append(ancestor)
        ancestor = ancestor.parent
    root.mkdir(parents=True, exist_ok=False)
    for directory in reversed(ancestors):
        _sync_directory(directory.parent)
    (root / ".job").mkdir()
    (root / "attempts").mkdir()
    job = {
        "schema_version": "0.1",
        "job_id": f"job_{uuid4().hex}",
        "created_at": runner._utc_now(),
        "root": str(root),
        "source": {
            "path": str(source),
            "sha256": source_sha,
            "page_count": count,
            "annotations": annotations,
            **({"kind": KIND, "images": images.manifest()} if images else {}),
            **({"iiif": images.fetch} if images and images.fetch else {}),
        },
        "config": config.data,
        "config_sha256": digest(config.data),
        "policy": policy,
        "hold_policy": HOLD_POLICY,
        **({"lexicon": lexicon} if lexicon else {}),
        "package_sha256": _package_code_sha256(),
        "selected_pages": selected,
        "pages": [],
        "stages": [],
        "status": "processing",
        "halt_reason": None,
        "limits": policy["limits"],
        "links": policy["links"],
        "usage": {},
        "next_action": "Process selected source pages.",
        "source_retention": {
            "capture": "present",
            "preservation": "unverified",
            "removal_eligibility": "not_assessed",
            "removed": False,
        },
    }
    job["pages"] = [
        dict(
            page_id=_page_id(n),
            page_number=n,
            source_sha256=source_sha,
            **({"source_image_sha256": images.page(n).sha256} if images else {}),
            attempts=[],
            selected_attempt=None,
            disposition="pending",
            review_reasons=[],
            review=None,
            review_history=[],
            next_action="local_text",
        )
        for n in selected
    ]
    if review_path is not None:
        _review_decisions(job, json.loads(review_path.read_text(encoding="utf-8")))
    with writer_lock(root):
        if container_error:
            job["status"] = "halted"
            job["halt_reason"] = container_error
            job["next_action"] = container_action
            _refresh(job, root)
            return _publish(job, root)
        _refresh(job, root)
        _save(job, root)
        return _continue(job, root, adapter_path)


def _escalation(job: dict) -> tuple[tuple[str, ...], Any, float | None]:
    """The job's triggers, and its lexicon's judgement when the rough trigger is on."""
    policy = job["policy"]
    lexicon_config = policy.get("lexicon")
    if not lexicon_config:
        return tuple(policy.get("escalate_on", ("hold",))), None, None
    lexicon = _job_lexicon(job)
    if lexicon.identity != job.get("lexicon"):
        raise ValueError(
            f"This job judged words with {job.get('lexicon')}; the installed lexicon is "
            f"{lexicon.identity}. Install the recorded version, or restore the recorded "
            "word list, to verify or resume it."
        )
    return (
        tuple(policy.get("escalate_on", ("hold",))),
        # Selection, triggers and their history judge the same readings repeatedly.
        functools.lru_cache(maxsize=None)(lambda text: roughness(text, lexicon)),
        lexicon_config.get("rough_below"),
    )


def _job_lexicon(job: dict) -> Any:
    """The lexicon a job judged words with; a word list is read from where the job found it."""
    config = job["policy"]["lexicon"]
    if config["provider"] == "wordlist" and job.get("lexicon"):
        config = {**config, "path": job["lexicon"]["path"]}
    return load_lexicon(config)


def _contester(job: dict, root: Path) -> Any:
    """Compare a page's reader with its literal witness, when the job contests readings.

    A reading in tiles follows its tiles' order, so the witness is its own layout
    regrouped tile by tile (``tiling.project_witness``), kept as an artifact.
    """
    settings = job["policy"].get("contest")
    if not settings:
        return None
    lexicon = _job_lexicon(job) if job["policy"].get("lexicon") else None
    receipts = {page["page_id"]: page.get("adjudications", []) for page in job["pages"]}
    stages = {stage["stage_id"]: stage for stage in job["stages"]}

    def witness_text(base: dict, witness: dict) -> tuple[str, dict | None]:
        evidence = base.get("input_evidence") or {}
        stage = stages[base["attempt_id"].split(":")[0]]
        if evidence.get("kind") != "page_tiles" or "tiles" not in stage:
            return witness["text"], None
        if not witness.get("layout_sha256"):
            return witness["text"], None
        text, outside = project_witness(_attempt_layout(root, witness), _stage_plan(root, stage))
        return text, {
            "plan_sha256": evidence["plan_sha256"],
            "artifact": f"witnesses/{base['page_id']}.txt",
            "outside_items": outside,
        }

    @functools.cache
    def spans(base: str, witness: str, engine: str) -> list[dict]:
        return contest_spans(
            base,
            witness,
            rules=settings["rules"],
            engine=engine,
            known=lexicon.known if lexicon else None,
        )

    def contest(base: dict, witness: dict) -> dict:
        engine = job["policy"][witness["stage"]]["adapter"]
        text, projection = witness_text(base, witness)
        record = {
            "schema_version": "0.1",
            "page_id": base["page_id"],
            "rules": settings["rules"],
            "base": {"attempt": base["attempt_id"], "text_sha256": _text_sha256(base["text"])},
            "witness": {
                "attempt": witness["attempt_id"],
                "text_sha256": _text_sha256(text),
                "adapter": engine,
                **({"projection": projection} if projection else {}),
            },
            "spans": copy.deepcopy(spans(base["text"], text, engine)),
        }
        sha256 = hashlib.sha256(adjudication.encode(record)).hexdigest()
        still_open, decided = adjudication.settle(record, sha256, receipts[base["page_id"]])
        return {
            "record": record,
            "sha256": sha256,
            "open": len(still_open),
            "decided": decided,
            "edition": adjudication.edition(base["text"], record, decided)
            if not still_open
            else None,
            "witness_text": text if projection else None,
        }

    return contest


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _retain(root: Path, relative: str, encoded: bytes, materialize: bool, what: str) -> str:
    """Write a derived artifact, or when verifying, refuse one that differs; return its hash."""
    target = _safe(root, relative)
    if not target.exists() or target.read_bytes() != encoded:
        if not materialize:
            raise ValueError(f"{what} differ from the retained readings and receipts")
        target.parent.mkdir(exist_ok=True)
        atomic_bytes(target, encoded)
    return file_digest(target)


def _retain_contested(
    root: Path, page: dict, result: dict, materialize: bool, before: dict | None
) -> None:
    """Keep a page's spans and any edition as artifacts; the page keeps hashes and counts.

    Spans change when a new reading becomes the base or the witness. Different spans
    from the same two readings mean the contest procedure changed, which would orphan
    every receipt that answers them, so that is refused.
    """
    record = result["record"]
    if (
        before is not None
        and before["base_attempt"] == record["base"]["attempt"]
        and before["witness_attempt"] == record["witness"]["attempt"]
        and before["sha256"] != result["sha256"]
    ):
        raise ValueError(
            f"Contest of {page['page_id']} gives different spans from the same readings: "
            f"this job was contested under {record['rules']} by another PageLedger version. "
            "Install that version to continue or verify it."
        )
    relative = f"contested/{page['page_id']}.json"
    _retain(root, relative, adjudication.encode(record), materialize, "Contested spans")
    spans = record["spans"]
    page["contested"] = {
        "artifact": relative,
        "sha256": result["sha256"],
        "base_attempt": record["base"]["attempt"],
        "witness_attempt": record["witness"]["attempt"],
        "kinds": dict(sorted(Counter(span["kind"] for span in spans).items())),
        "open": result["open"],
        "by_rule": sum(span["status"] == "settled" for span in spans),
        "by_review": len(result["decided"]),
        "superseded": [
            {"reason": reason, "attempt": attempt} for reason, attempt in result["superseded"]
        ],
    }
    projection = record["witness"].get("projection")
    if projection is not None:
        sha256 = _retain(
            root,
            projection["artifact"],
            result["witness_text"].encode("utf-8"),
            materialize,
            "Witness projections",
        )
        page["contested"]["witness_projection"] = {**projection, "sha256": sha256}
    if result["edition"] is not None:
        relative = f"editions/{page['page_id']}.txt"
        sha256 = _retain(root, relative, result["edition"].encode(), materialize, "Edition texts")
        page["edition"] = {
            "artifact": relative,
            "sha256": sha256,
            "base_attempt": record["base"]["attempt"],
        }


def _refresh(job: dict, root: Path, *, materialize: bool = True, check_source: bool = True) -> None:
    """Rebuild the job index from validated durable child evidence before scheduling."""
    from .processing_policy import assess_page

    if materialize and check_source:
        _check_source(job)
    pages = {p["page_id"]: p for p in job["pages"]}
    contested_before = {}
    for page in pages.values():
        page["attempts"] = []
        page["review_reasons"] = []
        contested_before[page["page_id"]] = page.pop("contested", None)
        page.pop("edition", None)
    for stage in job["stages"]:
        child = _safe(root, stage["run_path"])
        if not child.exists():
            if stage["status"] not in {"planned", "running"}:
                raise ValueError("Completed child run is missing")
            continue
        if not (child / "checkpoint.json").exists():
            if stage["status"] == "completed":
                raise ValueError("Completed child recovery plan is missing")
            stage["status"] = "halted"
            # A child that raised before its plan was durable keeps the reason recorded
            # then, so verification rebuilds the report the job published.
            if not str(job.get("halt_reason")).startswith(f"attempt_stopped:{stage['stage_id']}:"):
                job.update(
                    status="halted",
                    halt_reason="initialization_incomplete",
                    next_action="Inspect the retained incomplete initialization before starting a new job; no calls were scheduled without a durable plan.",
                )
            continue
        # The shared source is checked once (above, or by verify_job when only reading);
        # bind each child's claims to it without rereading the input for every attempt.
        checkpoint = _stage_checkpoint(job, root, stage)
        identity = checkpoint.job
        assert identity is not None
        if identity["config_sha256"] != stage["config_sha256"]:
            raise ValueError("Child run configuration disagrees with document job")
        sources = identity["inputs"]
        # Content is the identity: a child started after the original moved
        # records the path it actually read, found through the source index.
        if (
            len(sources) != 1
            or sources[0]["sha256"] != job["source"]["sha256"]
            or sources[0]["page_count"] != job["source"]["page_count"]
            or set(checkpoint.records) != {_page_id(n) for n in stage["pages"]}
        ):
            raise ValueError("Child run source/page identity disagrees with document job")
        if (child / "manifest.json").exists():
            verification = verify_run(child, check_external_sources=False)
            if verification["status"] != "pass":
                raise ValueError("Child run verification failed")
        for page_id, record in checkpoint.records.items():
            state = record["state"]
            if state == "pending":
                continue
            outcome = "outcome_unknown" if state == "started" else state
            result = record.get("result", record.get("partial_result", {}))
            content = result.get("content", "")
            text = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False, allow_nan=False)
            )
            completion = record.get("completion", {})
            provenance = completion.get("provenance", {})
            raw = provenance.get("result", {})
            artifact = f"{stage['run_path']}/{raw['raw_artifact']}" if raw else None
            raw_sha = raw.get("raw_sha256")
            if state == "failed" and result:
                partial = f"partials/{stage['stage_id']}-{page_id}.txt"
                target = _safe(root, partial)
                encoded = text.encode("utf-8")
                if not target.exists():
                    if not materialize:
                        raise ValueError("Retained partial text is missing")
                    target.parent.mkdir(exist_ok=True)
                    atomic_bytes(target, encoded)
                elif target.read_bytes() != encoded:
                    raise ValueError("Retained partial text changed")
                artifact, raw_sha = partial, file_digest(target)
            signals = structural_signals(
                text,
                result_format=result.get("format"),
                confidence_detail=result.get("confidence_detail"),
            )
            classification = asdict(
                classify_signals(
                    signals,
                    merge_classify_thresholds(None),
                    pdf_embedded_text_probe=stage["stage"] == "local_text"
                    and _pdf_like(job["source"]),
                )
            )
            attempt = {
                "attempt_id": f"{stage['stage_id']}:{page_id}",
                "stage": stage["stage"],
                "run_path": stage["run_path"],
                "run_id": identity["run_id"],
                "page_id": page_id,
                "page_number": pages[page_id]["page_number"],
                "source_sha256": job["source"]["sha256"],
                "outcome": outcome,
                "raw_artifact": artifact,
                "raw_sha256": raw_sha,
                "format": result.get("format"),
                "text": text,
                "warnings": completion.get("quality", {}).get(
                    "warnings", result.get("warnings", [])
                ),
                "classification": classification,
                "alignment": completion.get("alignment"),
                "usage": result.get("usage", {}),
                "failure": record.get("error"),
                "input_evidence": result.get("input_evidence"),
            }
            if job.get("hold_policy") in HOLD_POLICIES:
                attempt["adapter_capabilities"] = provenance.get("extractor", {}).get(
                    "capabilities", []
                )
            # A layout counts once its run has bound it: a completed page's provenance hashes it.
            if state == "completed" and result.get("layout") is not None:
                attempt["layout_sha256"] = provenance["layout_sha256"]
            if (
                stage["stage"] in IMAGE_STAGES
                and state == "completed"
                and result.get("input_evidence") is None
            ):
                attempt["outcome"] = "failed"
                attempt["failure"] = {
                    "type": "EvidenceError",
                    "code": "IMAGE_EVIDENCE_REQUIRED",
                    "http_status": None,
                    "cost_usd": result.get("usage", {}).get("cost_usd"),
                }
                job.update(
                    status="halted",
                    halt_reason="image_evidence_missing",
                    next_action="Inspect the image adapter: completed image calls require bound input evidence.",
                )
            pages[page_id]["attempts"].append(attempt)
    if job["policy"].get("tiling"):
        stages = {stage["stage_id"]: stage for stage in job["stages"]}
        for page in pages.values():
            for attempt in page["attempts"]:
                stage = stages[attempt["attempt_id"].split(":")[0]]
                if "tiles" in stage:
                    _check_tiled_attempt(job, root, page, attempt, stage)
    holds = warning_holds(job)
    escalate_on, roughness_of, rough_below = _escalation(job)
    contest = _contester(job, root)
    ladder_policy = job.get("hold_policy") in processing_policy.LADDER_POLICIES
    for page in pages.values():
        if job.get("hold_policy") in HOLD_POLICIES:
            page["comparisons"] = _build_comparisons(page["attempts"])
        else:
            page.pop("comparisons", None)
        page.update(
            assess_page(
                page,
                page["review"],
                holds_for=holds,
                # Only jobs written under the current policy clear a blank hold this way.
                text_refutes_blank=job.get("hold_policy")
                in processing_policy.BLANK_REFUTING_POLICIES,
                layer_outvoted=ladder_policy,
                reader_coverage=ladder_policy,
                ocr_layer_climbs=ladder_policy,
                corroborate=ladder_policy,
                witnesses=job.get("hold_policy") in processing_policy.WITNESS_POLICIES,
                configured=tuple(n for n in ladder(job["policy"]) if job["policy"][n] is not None),
                escalate_on=escalate_on,
                roughness=roughness_of,
                rough_below=rough_below,
                contest=contest,
                stages=ladder(job["policy"]),
            )
        )
        result = page.pop("contested", None)
        if result is not None:
            _retain_contested(root, page, result, materialize, contested_before[page["page_id"]])
    attempts = [attempt for page in pages.values() for attempt in page["attempts"]]
    paid = [a for a in attempts if a["stage"] in {"image", "second_opinion"}]
    token_values = [a["usage"].get("tokens") for a in attempts]
    cost_values = [a["usage"].get("cost_usd") for a in attempts]
    paid_costs = [a["usage"].get("cost_usd") for a in paid]
    paid_tokens = [a["usage"].get("tokens") for a in paid]
    known_cost = round(sum(c for c in cost_values if c is not None), 12)
    job["usage"] = {
        "attempt_pages": len(attempts),
        "image_calls": len(paid),
        # Present only in jobs with a local reader, which keeps its own budget.
        **(
            {"local_image_calls": sum(a["stage"] == "local_image" for a in attempts)}
            if "local_image" in job["policy"]
            else {}
        ),
        # Present only in jobs that read pages in tiles: one request per tile sent.
        **({"image_requests": sum(map(_requests, paid))} if job["policy"].get("tiling") else {}),
        "tokens": sum(t for t in token_values if t is not None),
        "tokens_known": all(t is not None for t in token_values),
        "cost_usd": known_cost if all(c is not None for c in cost_values) else None,
        "known_cost_usd": known_cost,
        "cost_known": all(c is not None for c in cost_values),
        "paid_cost_known": all(c is not None for c in paid_costs),
        "paid_tokens_known": all(t is not None for t in paid_tokens),
    }


def _budget_reason(job: dict, stage: str, count: int, requests: int | None = None) -> str | None:
    """Why ``count`` more pages of ``stage`` (``requests`` image requests, when they are
    planned tiles) would pass a limit, if they would."""
    limits, usage = job["limits"], job["usage"]
    if (
        limits["max_attempt_pages"] is not None
        and usage["attempt_pages"] + count > limits["max_attempt_pages"]
    ):
        return "max_attempt_pages"
    if limits["max_tokens"] is not None and usage["tokens"] >= limits["max_tokens"]:
        return "max_tokens"
    if limits["max_cost_usd"] is not None and usage["known_cost_usd"] >= limits["max_cost_usd"]:
        return "max_cost_usd"
    if (
        stage == "local_image"
        and usage["local_image_calls"] + count > limits["max_local_image_pages"]
    ):
        return "max_local_image_pages"
    if stage in {"image", "second_opinion"}:
        if usage["image_calls"] + count > limits["max_image_pages"]:
            return "max_image_pages"
        if (
            limits.get("max_image_requests") is not None
            and usage["image_requests"] + (count if requests is None else requests)
            > limits["max_image_requests"]
        ):
            return "max_image_requests"
        if limits["max_tokens"] is not None and not usage["paid_tokens_known"]:
            return "unknown_token_usage"
        if limits["max_cost_usd"] is not None and not usage["paid_cost_known"]:
            return "unknown_paid_cost"
    return None


# Limits a paused job can raise on resume. Unknown paid usage still halts:
# no higher limit makes an unmeasured spend measurable.
_RAISABLE_LIMITS = (
    "max_attempt_pages",
    "max_image_pages",
    "max_local_image_pages",
    "max_tokens",
    "max_cost_usd",
    "max_image_requests",
)
# Stages that send a page image to a model and keep the exact image as evidence.
IMAGE_STAGES = ("local_image", "image", "second_opinion")
# Remote readers wait on a provider, not the machine: processing.batch runs their pages at once.
CONCURRENT_STAGES = ("image", "second_opinion")
# A remote reader's failure that a second request may not repeat. Each page gets one retry.
TRANSIENT_FAILURES = frozenset(
    {"MODEL_TIMEOUT", "MODEL_HTTP_ERROR", "MODEL_NETWORK_ERROR", "MODEL_UNAVAILABLE"}
)


def _attempt_allowance(job: dict) -> int:
    limit = job["limits"]["max_attempt_pages"]
    return len(job["pages"]) if limit is None else limit - job["usage"]["attempt_pages"]


def _stop_for_budget(job: dict, reason: str) -> None:
    if reason in _RAISABLE_LIMITS:
        job.update(
            status="paused_budget",
            halt_reason=f"budget:{reason}",
            next_action=(
                f"Reached {reason} ({job['limits'][reason]}). Review the retained results, "
                f"or continue with: pageledger resume JOB_DIR --raise-limit {reason}=N"
            ),
        )
    else:
        job.update(
            status="halted",
            halt_reason=f"budget:{reason}",
            next_action=f"Review retained results and the {reason} budget before starting new work.",
        )


def _is_raise(value: object, current: object) -> bool:
    return (
        isinstance(current, (int, float))
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > current
        and (isinstance(value, int) or not isinstance(current, int))
    )


def _effective_limits(limits: dict, history: list) -> dict:
    """The configured limits with every recorded raise applied, in order."""
    effective = dict(limits)
    if not isinstance(history, list):
        raise ValueError("Document job limit history is invalid")
    for entry in history:
        key = entry.get("limit") if isinstance(entry, dict) else None
        if (
            key not in _RAISABLE_LIMITS
            or entry.get("previous") != effective[key]
            or not _is_raise(entry.get("value"), effective[key])
            or not isinstance(entry.get("at"), str)
        ):
            raise ValueError("Document job limit history is invalid")
        effective[key] = entry["value"]
    return effective


def _raise_limits(job: dict, raised: dict[str, int | float]) -> None:
    if job["status"] != "paused_budget":
        raise ValueError("Limits can be raised only on a job paused by a budget limit")
    for key, value in raised.items():
        if key not in _RAISABLE_LIMITS:
            raise ValueError(
                f"{key} is not a processing limit; use one of {', '.join(_RAISABLE_LIMITS)}"
            )
        current = job["limits"].get(key)
        if current is None:
            raise ValueError(f"{key} is not limited in this job")
        if not _is_raise(value, current):
            raise ValueError(f"{key} must be raised above {current}")
    history = list(job.get("limits_history", []))
    for key, value in raised.items():
        history.append(
            {"at": runner._utc_now(), "limit": key, "previous": job["limits"][key], "value": value}
        )
        job["limits"][key] = value
    job["limits_history"] = history
    job.update(status="processing", halt_reason=None, next_action="Process selected source pages.")


def _plan_stage(
    job: dict,
    root: Path,
    stage: str,
    numbers: list[int],
    *,
    retry: bool = False,
    tiles: dict | None = None,
) -> dict:
    # A job's first text batch keeps the plain name; a batch resumed after a
    # budget pause is named by its first page, like the per-page stages.
    first_text_batch = stage == "local_text" and all(
        item["stage"] != "local_text" for item in job["stages"]
    )
    stage_id = stage if first_text_batch else f"{stage}-{numbers[0]:04d}"
    if retry:
        stage_id += "-retry-1"
    existing = {item["stage_id"] for item in job["stages"]}
    if stage_id in existing:
        original = stage_id
        suffix = 1
        while stage_id in existing:
            stage_id = f"{original}-retry-{suffix}"
            suffix += 1
    profile = job["policy"][stage]
    options = dict(profile["adapter_options"])
    path = f"attempts/{stage_id}"
    if stage in IMAGE_STAGES:
        options["evidence_dir"] = str(root / path / "evidence")
    if tiles is not None:
        if len(numbers) != 1:
            raise ValueError("A tiled stage reads one page")
        options["tiles"] = tiles
    config = copy.deepcopy(job["config"])
    config.pop("processing", None)
    config["taxonomy"] = {
        "page_types": {"prose": {"default_action": "transcribe_text", "prompt": profile["prompt"]}}
    }
    config["run"] = {
        "adapter": profile["adapter"],
        "adapter_options": options,
        "on_page_error": "stop",
        "retry": {"max_retries": 0},
    }
    budget = {"max_pages": len(numbers)}
    for key, unit in (("max_tokens", "tokens"), ("max_cost_usd", "known_cost_usd")):
        if job["limits"][key] is not None:
            child_key = "max_usd" if key == "max_cost_usd" else key
            budget[child_key] = max(0, job["limits"][key] - job["usage"][unit])
    config["run"]["budget"] = budget
    config_path = f".job/{stage_id}.yml"
    encoded = yaml.safe_dump(config, sort_keys=True, allow_unicode=True).encode("utf-8")
    atomic_bytes(root / config_path, encoded)
    item: dict[str, Any] = {
        "stage_id": stage_id,
        "stage": stage,
        "pages": numbers,
        "run_path": path,
        "config_path": config_path,
        "config_sha256": file_digest(root / config_path),
        "status": "planned",
    }
    if tiles is not None:
        item["tiles"] = {
            "plan_sha256": plan_sha256(tiles),
            "count": len(tiles["tiles"]),
            "layout_attempt": (tiles["layout"] or {}).get("attempt"),
        }
    job["stages"].append(item)
    _save(job, root)
    return item


# Reading a dense page in tiles (processing.tiling). The plan for a page is made from
# the layout of a literal reading, kept in the stage's hashed configuration, and made
# again from the retained layout whenever the job is rebuilt.


def _check_tiling_adapters(tiling: dict, adapters: dict) -> None:
    import shutil

    if shutil.which("pdfinfo") is None:
        raise ValueError("processing.tiling needs pdfinfo for each page's size; install Poppler")
    layout = adapters[tiling["layout"]]
    if "layout" not in getattr(layout, "capabilities", ()):
        raise ValueError(
            f"processing.tiling.layout: the {tiling['layout']} adapter reports no layout "
            "(capability layout); use rapidocr or alto"
        )
    for name in tiling["stages"]:
        adapter = adapters[name]
        if "page_tiles" not in getattr(adapter, "capabilities", ()):
            raise ValueError(
                f"processing.tiling.stages: the {name} adapter cannot read tiles "
                "(capability page_tiles); use vision"
            )
        if type(getattr(adapter, "max_image_side", None)) is not int:
            raise ValueError(f"processing.tiling.stages: the {name} adapter has no max_image_side")


def _tiler(job: dict, root: Path, stage: str, adapter_path: Path | None) -> Any:
    """A function planning one page's tiles for ``stage``, or None when it is not tiled."""
    tiling = job["policy"].get("tiling")
    if not tiling or stage not in tiling["stages"]:
        return None
    from .adapters import native_page_pixels
    from .alto_adapter import rendered_page_size

    runner._apply_adapter_path(adapter_path)
    profile = job["policy"][stage]
    side = runner.load_adapter(profile["adapter"], profile["adapter_options"]).max_image_side
    source = _check_source(job)
    pages = {page["page_number"]: page for page in job["pages"]}

    def plan(number: int) -> dict:
        size = rendered_page_size(source, number)
        if size is None:
            raise ValueError(f"pdfinfo cannot say the size of page {number}; tiles need it")
        native = native_page_pixels(source, number)
        attempt, _ = _layout_attempt(job, root, pages[number])
        return plan_tiles(
            None if attempt is None else _attempt_layout(root, attempt),
            page_width=size[0],
            page_height=size[1],
            min_line_pixels=tiling["min_line_pixels"],
            max_tiles=tiling["max_tiles"],
            max_image_side=side,
            max_dpi=None if native is None else max(native[0], native[1]) / native[2],
            layout_attempt=None if attempt is None else attempt["attempt_id"],
        )

    return plan


def _layout_attempt(job: dict, root: Path, page: dict) -> tuple[dict | None, dict | None]:
    """The literal reading whose layout a page is cut along, and the one set aside.

    The configured stage's latest completed reading with a layout. When that is not
    the job's own OCR (an ALTO file, say) and fewer than nine in ten of its item
    centres fall inside the OCR's boxes, its boxes point elsewhere on the page: the
    OCR layout is used instead, and the other is returned as rejected.
    """

    def latest(stage: str) -> dict | None:
        found = [
            attempt
            for attempt in page["attempts"]
            if attempt["stage"] == stage
            and attempt["outcome"] == "completed"
            and attempt.get("layout_sha256")
        ]
        return found[-1] if found else None

    stage = job["policy"]["tiling"]["layout"]
    chosen, ocr = latest(stage), latest("local_ocr")
    if chosen is None or ocr is None or stage == "local_ocr":
        return chosen, None
    inside, total = centres_inside(_attempt_layout(root, chosen), _attempt_layout(root, ocr))
    if total and 10 * inside < 9 * total:
        return ocr, chosen
    return chosen, None


def _attempt_layout(root: Path, attempt: dict) -> dict:
    """An attempt's layout from its stage's recovery record, checked against its hash."""
    path = _safe(root, attempt["run_path"]) / ".checkpoint" / "pages" / f"{attempt['page_id']}.json"
    layout = read_record(path).get("result", {}).get("layout")
    if layout is None or layout_sha256(layout) != attempt.get("layout_sha256"):
        raise ValueError(f"The layout of {attempt['attempt_id']} changed or is missing")
    return layout


def _stage_plan(root: Path, stage: dict) -> dict:
    """A tiled stage's plan, from its hashed configuration."""
    config = yaml.safe_load(_safe(root, stage["config_path"]).read_text(encoding="utf-8"))
    plan = config["run"]["adapter_options"].get("tiles")
    if plan is None or plan_sha256(plan) != stage["tiles"]["plan_sha256"]:
        raise ValueError(f"The tile plan of {stage['stage_id']} changed")
    return plan


def _requests(attempt: dict) -> int:
    """Image requests a paid attempt made: one per tile sent, or one for the page."""
    evidence = attempt.get("input_evidence") or {}
    return len(evidence["tiles"]) if evidence.get("kind") == "page_tiles" else 1


def _check_tiled_attempt(job: dict, root: Path, page: dict, attempt: dict, stage: dict) -> None:
    """Bind a tiled reading to its plan, the plan to its layout, and record what the tiles
    found: failed tiles, cuts through text, text too small, a missing or rejected layout."""
    plan = _stage_plan(root, stage)
    tiling = job["policy"]["tiling"]
    if (
        plan["settings"]["min_line_pixels"] != tiling["min_line_pixels"]
        or plan["settings"]["max_tiles"] != tiling["max_tiles"]
    ):
        raise ValueError(f"The tile plan of {stage['stage_id']} differs from the job's tiling")
    by_id = {item["attempt_id"]: item for item in page["attempts"]}
    source = plan["layout"]["attempt"] if plan["layout"] else None
    if plan["layout"] is not None and (
        source not in by_id or by_id[source].get("layout_sha256") != plan["layout"]["sha256"]
    ):
        raise ValueError(f"The layout the tiles of {stage['stage_id']} were cut along is missing")
    again = plan_tiles(
        None if source is None else _attempt_layout(root, by_id[source]),
        page_width=plan["page_box"]["width"],
        page_height=plan["page_box"]["height"],
        max_dpi=plan["tiles"][0]["max_dpi"],
        layout_attempt=source,
        **plan["settings"],
    )
    if plan_sha256(again) != stage["tiles"]["plan_sha256"]:
        raise ValueError(
            f"The tiles of {stage['stage_id']} cannot be planned again from the retained "
            "layout: this job was planned by another PageLedger version"
        )
    evidence = attempt.get("input_evidence")
    if evidence is None:
        return
    if evidence.get("kind") != "page_tiles" or evidence["plan_sha256"] != plan_sha256(plan):
        raise ValueError(f"{attempt['attempt_id']} was not read from its tile plan")
    planned = {tile["tile_id"]: tile for tile in plan["tiles"]}
    for tile in evidence["tiles"]:
        box, crop = planned.get(tile["tile_id"]), tile["crop"]
        if box is None or crop.get("kind") != "box":
            raise ValueError(f"{attempt['attempt_id']} sent a tile its plan does not have")
        pixel = crop["width"] / tile["width"] + 0.001
        if any(
            abs(crop[start] - box[start]) > pixel
            or abs(crop[start] + crop[size] - box[start] - box[size]) > pixel
            for start, size in (("x", "width"), ("y", "height"))
        ):
            raise ValueError(
                f"{attempt['attempt_id']} tile {tile['tile_id']} is not its planned box"
            )
    validate_tile_text(evidence, attempt["text"])
    chosen = by_id.get(source) if source else None
    rejected = chosen is not None and chosen["stage"] != tiling["layout"]
    # Boxes set aside as pointing elsewhere on the page say nothing about the cuts.
    literal = [
        _attempt_layout(root, item)
        for item in page["attempts"]
        if item["stage"] in LAYOUT_STAGES
        and not (rejected and item["stage"] == tiling["layout"])
        and item["outcome"] == "completed"
        and item.get("layout_sha256")
    ]
    failed = [tile["tile_id"] for tile in evidence["tiles"] if tile["outcome"] == "failed"]
    edge_cut = check_edges(plan, literal)
    codes = {
        "tile_failed": bool(failed),
        "tile_edge_cut": bool(edge_cut),
        "tile_text_small": any(tile["small_text"] for tile in plan["tiles"]),
        "tile_layout_missing": plan["layout"] is None,
        "alto_layout_rejected": rejected,
    }
    attempt["warnings"] = [
        *attempt["warnings"],
        *(code for code, found in codes.items() if found and code not in attempt["warnings"]),
    ]
    attempt["tiling"] = {
        "plan_sha256": stage["tiles"]["plan_sha256"],
        "layout_attempt": source,
        "tiles": len(plan["tiles"]),
        "sent": len(evidence["tiles"]),
        "failed": failed,
        "edge_cut": edge_cut,
    }


def _retry_local(job: dict, root: Path, stage: dict, adapter_path: Path | None) -> bool:
    """Retain an uncertain local attempt and retry only its unfinished pages once."""
    if (
        stage["stage"] not in {"local_text", "local_ocr", "second_ocr"}
        or job["policy"][stage["stage"]]["adapter"]
        not in {"text", "pdf_text", "pdf_ocr", "rapidocr", "indra", "alto"}
        or "-retry-" in stage["stage_id"]
        or job["package_sha256"] != _package_code_sha256()
    ):
        return False
    child = _safe(root, stage["run_path"])
    if not (child / "checkpoint.json").exists():
        return False
    checkpoint = _stage_checkpoint(job, root, stage)
    states = {record["state"] for record in checkpoint.records.values()}
    if not states.intersection({"started", "outcome_unknown"}) or "failed" in states:
        return False
    numbers = [
        number
        for number in stage["pages"]
        if checkpoint.records[_page_id(number)]["state"]
        in {"started", "outcome_unknown", "pending"}
    ]
    reason = _budget_reason(job, stage["stage"], len(numbers))
    if reason:
        _stop_for_budget(job, reason)
        return False
    stage["status"] = "halted"
    job.update(status="processing", halt_reason=None)
    retry = _plan_stage(job, root, stage["stage"], numbers, retry=True)
    return _execute(job, root, retry, adapter_path)


def _launch(root: Path, source: Path, stage: dict, adapter_path: Path | None) -> Exception | None:
    """Run one planned child. Called from worker threads, so it reads the stage and never the job."""
    child = _safe(root, stage["run_path"])
    try:
        if child.exists():
            runner.resume(child, adapter_path=adapter_path)
        else:
            runner.run(
                inputs=[source],
                config_path=root / stage["config_path"],
                out_dir=child,
                dry_run=False,
                pages=",".join(map(str, stage["pages"])),
                adapter_path=adapter_path,
                resumable=True,
            )
    except (RuntimeError, ValueError, OSError) as exc:
        return exc
    return None


def _failure_action(job: dict, root: Path, stage: dict) -> str:
    """After a refresh: whether a stopped child halts the job, continues past it, or retries it."""
    # Initialization may fail before a durable page plan exists; it made no
    # verified request, and the directory remains evidence rather than reusable output.
    if not (_safe(root, stage["run_path"]) / "checkpoint.json").exists():
        return "halt"
    failures = [
        attempt
        for page in job["pages"]
        for attempt in page["attempts"]
        if attempt["run_path"] == stage["run_path"] and attempt["outcome"] != "completed"
    ]
    if failures and all(attempt["outcome"] == "failed" for attempt in failures):
        transient = all(
            (attempt.get("failure") or {}).get("code") in TRANSIENT_FAILURES for attempt in failures
        )
        unread = all(map(_nothing_read, failures))
        if (
            stage["stage"] in CONCURRENT_STAGES
            and transient
            and unread
            and "-retry-" not in stage["stage_id"]
        ):
            return "retry"
        # A rejected optional witness remains a provider_failure hold on its page,
        # and a local reader that failed costs nothing: its page climbs past it,
        # even when it kept a partial reading (a loop cut off at max_tokens).
        # An image page that fails transiently twice is held the same way, and so
        # is a page read in tiles that stopped after some tiles were paid for:
        # reading it again would buy those tiles again.
        if stage["stage"] in {"second_opinion", "local_image"} or (
            stage["stage"] == "image" and transient and (unread or "tiles" in stage)
        ):
            return "continue"
    return "retry_local"


def _nothing_read(attempt: dict) -> bool:
    """A failed request kept no reading: no partial output, or only the image it sent.

    The built-in vision adapter keeps an empty partial reading with its evidence on
    every failed request; a page read in tiles has read nothing until a tile completes.
    """
    if not attempt.get("raw_artifact"):
        return True
    evidence = attempt.get("input_evidence") or {}
    if evidence.get("kind") == "page_tiles":
        return not any(tile["outcome"] == "completed" for tile in evidence["tiles"])
    return not attempt["text"].strip()


def _run_concurrently(
    job: dict,
    root: Path,
    source: Path,
    stages: Iterator[dict],
    adapter_path: Path | None,
    concurrent: int,
    done: list,
) -> None:
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        running: dict = {}

        def start() -> bool:
            stage = next(stages, None)
            if stage is None:
                return False
            stage["status"] = "running"
            _save(job, root)
            running[pool.submit(_launch, root, source, stage, adapter_path)] = stage
            return True

        while len(running) < concurrent and start():
            pass
        while running:
            finished, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
            for future in finished:
                stage = running.pop(future)
                error = future.result()
                stage["status"] = "halted" if error else "completed"
                done.append((stage, error))
            while not any(error for _, error in done) and len(running) < concurrent and start():
                pass


def _execute(job: dict, root: Path, stage: dict, adapter_path: Path | None) -> bool:
    return _execute_stages(job, root, iter([stage]), adapter_path)


def _execute_stages(
    job: dict, root: Path, stages: Iterator[dict], adapter_path: Path | None, concurrent: int = 1
) -> bool:
    """Run planned stages, up to `concurrent` at a time, then refresh and publish once.

    `stages` plans lazily on this thread, so each child is durable in job.json before it starts;
    after the first failure nothing new is started and the running children finish.
    """
    source = _check_source(job)
    done: list[tuple[dict, Exception | None]] = []
    if concurrent == 1:
        for stage in stages:
            stage["status"] = "running"
            _save(job, root)
            error = _launch(root, source, stage, adapter_path)
            stage["status"] = "halted" if error else "completed"
            done.append((stage, error))
            if error:
                break
    else:
        _run_concurrently(job, root, source, stages, adapter_path, concurrent, done)
        done.sort(key=lambda item: job["stages"].index(item[0]))
    failed = [(stage, error) for stage, error in done if error]
    if not failed:
        _refresh(job, root)
        _publish(job, root)
        return job["status"] != "halted"

    def halt(stage: dict, error: Exception) -> None:
        job["status"] = "halted"
        job["halt_reason"] = f"attempt_stopped:{stage['stage_id']}:{type(error).__name__}"
        job["next_action"] = (
            "Inspect the retained failure or uncertain request; reconcile it before starting new work."
        )

    halt(*failed[0])
    if not (_safe(root, failed[0][0]["run_path"]) / "checkpoint.json").exists():
        return False
    _refresh(job, root)
    actions = [(stage, error, _failure_action(job, root, stage)) for stage, error in failed]
    stuck = [item for item in actions if item[2] in {"halt", "retry_local"}]
    if stuck:
        stage, error, action = stuck[0]
        halt(stage, error)
        if action == "retry_local" and len(failed) == 1:
            return _retry_local(job, root, stage, adapter_path)
        return False
    retries = [stage for stage, _, action in actions if action == "retry"]
    if retries:
        return _retry(job, root, retries, adapter_path, concurrent)
    job.update(status="processing", halt_reason=None)
    _publish(job, root)
    return True


def _retry(
    job: dict, root: Path, stages: list[dict], adapter_path: Path | None, concurrent: int
) -> bool:
    """Request the pages of stages that failed transiently once more, within the budget."""
    # A tiled page is read again whole: every tile of its plan is requested again.
    requests = sum(stage.get("tiles", {}).get("count", 1) for stage in stages)
    reason = _budget_reason(job, stages[0]["stage"], len(stages), requests)
    if reason:
        # Nothing is planned: resume finds the retry still owed (_owed_retries).
        _stop_for_budget(job, reason)
        return False
    job.update(status="processing", halt_reason=None)
    planned = (
        _plan_stage(
            job,
            root,
            stage["stage"],
            stage["pages"],
            retry=True,
            tiles=_stage_plan(root, stage) if "tiles" in stage else None,
        )
        for stage in stages
    )
    return _execute_stages(job, root, planned, adapter_path, concurrent)


def _owed_retries(job: dict, root: Path) -> list[dict]:
    """Stages that failed transiently and whose one retry a budget pause stopped before
    it was planned. A page a person has reviewed since is settled, not owed a reading."""
    retried = {
        (stage["stage"], tuple(stage["pages"]))
        for stage in job["stages"]
        if "-retry-" in stage["stage_id"]
    }
    reviewed = {page["page_number"] for page in job["pages"] if page.get("review")}
    return [
        stage
        for stage in job["stages"]
        if stage["status"] == "halted"
        and stage["stage"] in CONCURRENT_STAGES
        and "-retry-" not in stage["stage_id"]
        and (stage["stage"], tuple(stage["pages"])) not in retried
        and not reviewed.intersection(stage["pages"])
        and _failure_action(job, root, stage) == "retry"
    ]


def _continue(job: dict, root: Path, adapter_path: Path | None) -> dict:
    if job["package_sha256"] != _package_code_sha256():
        raise ValueError("Document processing implementation changed; refusing pending work")
    for stage in job["stages"]:
        if stage["status"] in {"planned", "running"}:
            if not _execute(job, root, stage, adapter_path):
                return _publish(job, root)
    _refresh(job, root)
    owed = _owed_retries(job, root)
    if owed:
        concurrent = _batching(job, owed[0]["stage"])[1]
        if not _retry(job, root, owed, adapter_path, concurrent):
            return _publish(job, root)
    for name in ladder(job["policy"]):
        if job["policy"][name] is None:
            continue
        eligible = [p["page_number"] for p in job["pages"] if p["next_action"] == name]
        benchmark = job["policy"].get("benchmark")
        if benchmark and benchmark["stage"] == name:
            eligible = sorted(
                set(eligible)
                | {
                    p["page_number"]
                    for p in job["pages"]
                    if p["page_number"] % benchmark["every_nth_page"] == 0
                    and not any(a["stage"] == name for a in p["attempts"])
                }
            )
        groups = [eligible] if name == "local_text" and eligible else [[n] for n in eligible]
        size, concurrent = _batching(job, name)
        tiler = _tiler(job, root, name, adapter_path)
        plans: dict[int, dict] = {}
        while groups:
            batch: list[list[int]] = []
            stop: str | None = None
            for numbers in groups[:size]:
                count = sum(len(item) for item in batch)
                requests = None
                if tiler is not None:
                    plans[numbers[0]] = plans.get(numbers[0]) or tiler(numbers[0])
                    requests = sum(len(plans[item[0]]["tiles"]) for item in [*batch, numbers])
                reason = _budget_reason(job, name, count + len(numbers), requests)
                allowance = _attempt_allowance(job) - count
                if reason == "max_attempt_pages" and 0 < allowance < len(numbers):
                    # Process what the limit allows, then pause for the rest.
                    numbers = numbers[:allowance]
                    reason = _budget_reason(job, name, count + len(numbers))
                    if not reason:
                        batch.append(numbers)
                        reason = "max_attempt_pages"
                if reason:
                    stop = reason
                    break
                batch.append(numbers)
            groups = groups[len(batch) :]
            if batch:
                planned = (
                    _plan_stage(job, root, name, numbers, tiles=plans.get(numbers[0]))
                    for numbers in batch
                )
                if not _execute_stages(job, root, planned, adapter_path, concurrent):
                    return _publish(job, root)
            if stop:
                _stop_for_budget(job, stop)
                return _publish(job, root)
    job["status"] = "completed"
    job["next_action"] = _review_next_action(job)
    return _publish(job, root)


def _batching(job: dict, stage: str) -> tuple[int, int]:
    """Pages a stage runs between refreshes, and how many of them at once.

    A token or cost limit is checked against usage measured after each page, so it keeps
    pages one at a time.
    """
    batch = job["policy"].get("batch")
    if (
        batch is None
        or job["limits"]["max_tokens"] is not None
        or job["limits"]["max_cost_usd"] is not None
    ):
        return 1, 1
    return batch["pages"], batch["concurrent"] if stage in CONCURRENT_STAGES else 1


def _review_next_action(job: dict) -> str:
    if any(p["disposition"] == "source_defect" for p in job["pages"]):
        return (
            "Obtain an alternate source for the damaged pages; do not reconstruct invisible text."
        )
    unresolved = [
        p for p in job["pages"] if p["disposition"] not in {"reviewed_text", "reviewed_blank"}
    ]
    return (
        "Check the pages against the source, those that need a person first, and record "
        "decisions with pageledger review."
        if unresolved
        else "Every page has a person's decision. PageLedger does not preserve or publish "
        "the source; keep it with this job."
    )


def resume_job(
    job_dir: Path,
    *,
    adapter_path: Path | None = None,
    raise_limits: dict[str, int | float] | None = None,
) -> dict:
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        _refuse_relocated_resume(job, root)
        # Refuse changes in any retained attempt before starting a pending one.
        _refresh(job, root)
        if raise_limits:
            _raise_limits(job, raise_limits)
        if job["status"] == "halted" and str(job.get("halt_reason")).startswith("attempt_stopped:"):
            stopped = [stage for stage in job["stages"] if stage["status"] == "halted"]
            if stopped and _retry_local(job, root, stopped[-1], adapter_path):
                return _continue(job, root, adapter_path)
        if job["status"] != "processing":
            return _publish(job, root)
        return _continue(job, root, adapter_path)


def review_job(
    job_dir: Path, review_path: Path, *, reviewer: str | None = None, dry_run: bool = False
) -> dict:
    """Record human decisions from a filled-in review sheet (.csv), a decisions file
    (JSON with the sheet's bindings, which the review view downloads) or a JSON receipt.

    With dry_run, every decision is checked and counted but nothing is recorded.
    """
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        located = _source_warnings(job, root)
        _refresh(job, root, check_source=False)
        corrected: list[dict] = []
        if review_path.suffix.lower() == ".csv":
            review, corrected = read_review_sheet(job, review_path, reviewer, root)
        else:
            review = json.loads(review_path.read_text(encoding="utf-8"))
            if isinstance(review, dict) and "bindings" in review:
                review, corrected = read_review_decisions(job, review_path, review, reviewer, root)
            elif reviewer:
                raise ValueError(
                    "--reviewer is for review sheets and decisions files; "
                    "a JSON receipt names its reviewers"
                )
            else:
                _keep_corrections(job, review)
        pending = frozenset(item["sha256"] for item in corrected)

        def record(target: dict) -> None:
            _review_decisions(target, review)
            for page in target["pages"]:
                _check_corrections(root, page, pending)

        record(copy.deepcopy(job))
        counts: dict[str, Any] = {"decisions": _decision_counts(review)}
        if corrected:
            counts["corrections"] = [
                {key: item[key] for key in ("page_number", "from", "words", "large")}
                for item in corrected
            ]
        if dry_run:
            return {
                "job_id": job["job_id"],
                "out_dir": str(root),
                "status": "checked",
                **counts,
                "warnings": located + _blank_over_text(job, review),
            }
        for item in corrected:
            target = _safe(root, corrections.artifact_path(item["page_id"], item["sha256"]))
            target.parent.mkdir(exist_ok=True)
            encoded = item["text"].encode("utf-8")
            if not target.exists():
                atomic_bytes(target, encoded)
            elif target.read_bytes() != encoded:
                raise ValueError(f"A different corrected text is already kept at {target}")
        record(job)
        warnings = located + _blank_over_text(job, review)
        _note_relocation(job, root, "review-job")
        _refresh(job, root, check_source=False)
        if job["status"] == "processing":
            job.update(
                status="halted",
                halt_reason="review_changed_plan",
                next_action="Review receipts changed an interrupted plan. Inspect the retained attempts before explicitly starting new work.",
            )
        elif job["status"] == "completed":
            job["next_action"] = _review_next_action(job)
        return {**_publish(job, root, check_source=False), **counts, "warnings": warnings}


def _keep_corrections(job: dict, review: Any) -> None:
    """A plain receipt carries no binding, so it may not replace a person's corrected text."""
    pages = {page["page_id"]: page for page in job["pages"]}
    decisions = review.get("decisions") if isinstance(review, dict) else None
    for decision in decisions if isinstance(decisions, list) else []:
        page_id = decision.get("page_id") if isinstance(decision, dict) else None
        page = pages.get(page_id) if isinstance(page_id, str) else None
        if page is None:
            # The receipt's own checks name what is wrong with this decision.
            continue
        active = review_sheet_corrected(page)
        if active is not None and decision.get("correction") != active["correction"]:
            raise ValueError(
                f"Page {page['page_number']} carries text corrected by {active['reviewer']}; "
                "a JSON receipt cannot replace it. Decide the page in a review sheet or "
                "the review view, which show the correction."
            )


# A page a person calls blank whose selected reading has this many words may be
# misfiled: the 2026-11 dogfood recorded blank on a page with 1,295 OCR words.
BLANK_WORDS_WARNING = 50


def _blank_over_text(job: dict, review: Any) -> list[dict]:
    """Warn on each page recorded as blank whose selected reading has many words."""
    pages = {page["page_id"]: page for page in job["pages"]}
    decisions = review.get("decisions") if isinstance(review, dict) else None
    warnings = []
    for decision in decisions if isinstance(decisions, list) else []:
        page = pages.get(decision.get("page_id")) if isinstance(decision, dict) else None
        if page is None or decision.get("disposition") != "reviewed_blank":
            continue
        chosen = decision.get("selected_attempt") or page.get("selected_attempt")
        attempt: dict = next((a for a in page["attempts"] if a["attempt_id"] == chosen), {})
        words = len((attempt.get("text") or "").split())
        if words >= BLANK_WORDS_WARNING:
            warnings.append(
                {
                    "code": "blank_with_text",
                    "message": f"Page {page['page_number']} is recorded as blank, but its "
                    f"selected reading has {words} words. Check the page before applying.",
                }
            )
    return warnings


def adjudication_packets(job_dir: Path, out: Path, terms: list[str] | None = None) -> dict:
    """Write a packet for each page with open contested spans, for an adjudicator to answer."""
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        if not job["policy"].get("contest"):
            raise ValueError("This job does not contest readings; it has no spans to settle")
        _refresh(job, root, materialize=False)
        return {"status": "written", **adjudication.write_packets(job, root, out, terms)}


def adjudicate_job(job_dir: Path, decisions: Path, *, reviewer: str, dry_run: bool = False) -> dict:
    """Record answers from <page_id>.decisions.json files as receipts, then rebuild the job."""
    if not reviewer.strip():
        raise ValueError(
            "Name who settled the spans with --reviewer, e.g. agent:sol or person:NAME"
        )
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        located = _source_warnings(job, root)
        _refresh(job, root, check_source=False)
        now = runner._utc_now()
        recorded = []
        for page in job["pages"]:
            path = decisions / f"{page['page_id']}.decisions.json"
            if not path.exists():
                continue
            contested = page.get("contested")
            if not contested:
                raise ValueError(f"{page['page_id']} has no contested spans to settle")
            if page.get("review") is not None:
                raise ValueError(f"{page['page_id']} was reviewed by a person; its text is settled")
            record = json.loads((root / contested["artifact"]).read_bytes())
            answer = json.loads(path.read_text(encoding="utf-8"))
            receipt = adjudication.receipt_for(
                page,
                answer,
                record=record,
                sha256=contested["sha256"],
                reviewer=reviewer,
                reviewed_at=now,
            )
            recorded.append((page, receipt))
        if not recorded:
            raise ValueError(f"No <page_id>.decisions.json files in {decisions}")
        settled = sum(
            d["confidence"] == "high" for _, receipt in recorded for d in receipt["decisions"]
        )
        counts = {"pages": len(recorded), "decisions_high": settled, "warnings": located}
        if dry_run:
            return {"job_id": job["job_id"], "out_dir": str(root), "status": "checked", **counts}
        for page, receipt in recorded:
            page.setdefault("adjudications", []).append(receipt)
        _note_relocation(job, root, "adjudicate")
        _refresh(job, root, check_source=False)
        if job["status"] == "completed":
            job["next_action"] = _review_next_action(job)
        return {**_publish(job, root, check_source=False), **counts}


def _decision_counts(review: dict) -> dict[str, int]:
    return dict(Counter(decision["disposition"] for decision in review["decisions"]))


def create_review_sheet(job_dir: Path, sheet: Path) -> dict:
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        _refresh(job, root, materialize=False)
        return {"status": "written", **write_review_sheet(job, sheet, root)}


def _derived(page: dict) -> tuple:
    """What a page's record states that verification must rebuild identically."""
    return (
        page["selected_attempt"],
        page["disposition"],
        page["review_reasons"],
        page.get("triggers"),
        page.get("contested"),
        page.get("edition"),
    )


def verify_job(job_dir: Path) -> dict:
    root = job_dir.expanduser().resolve()
    try:
        job = _load(root)
        if job["status"] == "processing":
            # Its record describes work in progress, which a finished job's checks would
            # read as evidence that disagrees with itself.
            return {
                "status": "fail",
                "reason": "unfinished",
                "job_id": job["job_id"],
                "out_dir": str(root),
                "error": "This job did not finish: it was interrupted, or it is still running. "
                f"Run `pageledger resume {shlex.quote(str(root))}` to finish it, then verify again.",
            }
        warnings = _location_warnings(job, root)
        before = [_derived(p) for p in job["pages"]]
        _refresh(job, root, materialize=False)
        if before != [_derived(p) for p in job["pages"]]:
            raise ValueError("Job selections disagree with retained evidence")
        from .document_report import build_document_report, verify_document_report

        report = verify_document_report(root)
        # Reports written before 0.5.1 have no format marker and must be
        # rebuilt with their original renderer for byte/evidence comparison.
        report_format = report.get("report_format")
        if report != build_document_report(
            job,
            root,
            report_format=report_format,
            include_stage_accounting="stage_accounting" in report,
        ):
            raise ValueError("Document report differs from verified job evidence")
        return {
            "status": "pass",
            "job_id": job["job_id"],
            "out_dir": str(root),
            "warnings": warnings,
        }
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {"status": "fail", "out_dir": str(root), "error": str(exc)}
