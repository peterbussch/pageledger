#!/usr/bin/env python3
"""Demonstrate saved-response recovery with the built-in text adapter.

The interruption hook is intentionally kept in this example helper rather
than PageLedger's production code. It raises after a response checkpoint is
saved, then resumes the same job and records the adapter calls made by both
halves of the demonstration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pageledger.adapters import TextAdapter
from pageledger.checkpoint import Checkpoint
from pageledger.processing import process, resume_job


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calls", type=Path, required=True)
    args = parser.parse_args()

    original_extract = TextAdapter.extract
    original_save = Checkpoint.save
    interrupted = False

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        with args.calls.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"page_id": page_id, "page_number": page_number}) + "\n")
        return original_extract(
            self,
            source,
            page_id=page_id,
            page_number=page_number,
            action=action,
            prompt=prompt,
        )

    def save(self, page_id, record):
        nonlocal interrupted
        original_save(self, page_id, record)
        if record.get("state") == "response" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    TextAdapter.extract = extract
    Checkpoint.save = save
    try:
        try:
            process(source=args.source, config_path=args.config, out_dir=args.out)
        except KeyboardInterrupt:
            print("DOCUMENT_JOURNEY_INTERRUPTED_AFTER_SAVE")
        else:
            raise RuntimeError("expected the controlled interruption")
        result = resume_job(args.out)
    finally:
        TextAdapter.extract = original_extract
        Checkpoint.save = original_save

    if result["status"] != "completed":
        raise RuntimeError(f"recovery did not complete: {result['status']}")
    print("DOCUMENT_JOURNEY_RECOVERY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
