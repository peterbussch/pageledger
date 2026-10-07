"""Find a moved source original by content hash without rewriting sealed records.

Runs and document jobs record each source by absolute path and SHA-256. The
hash is the identity; the path is where the bytes were when the run started.
When originals move, a source index maps hashes to their current locations:
one or more TSV files of ``sha256<TAB>absolute_path`` lines. Records keep the
original path; only reads go to the relocated file, and only after its digest
matches.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .checkpoint import file_digest

ENV_VAR = "PAGELEDGER_SOURCE_INDEX"
DEFAULT_INDEX = "~/.config/pageledger/source-index.tsv"

logger = logging.getLogger(__name__)


class SourceUnavailable(ValueError):
    """Neither the recorded path nor any indexed path holds the recorded bytes."""


def index_files() -> list[Path]:
    """Index files from PAGELEDGER_SOURCE_INDEX, else the default if it exists.

    An empty PAGELEDGER_SOURCE_INDEX disables the index, including the default.
    """
    value = os.environ.get(ENV_VAR)
    if value is not None:
        return [Path(item).expanduser() for item in value.split(os.pathsep) if item.strip()]
    default = Path(DEFAULT_INDEX).expanduser()
    return [default] if default.is_file() else []


def indexed_paths(sha256: str) -> list[Path]:
    """Every indexed location for ``sha256``, in index order; unverified."""
    wanted = sha256.lower()
    found: list[Path] = []
    for index in index_files():
        try:
            with index.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.rstrip("\r\n")
                    if not line or line.startswith("#"):
                        continue
                    fields = line.split("\t")
                    if len(fields) < 2 or fields[0].strip().lower() != wanted:
                        continue
                    path = Path(fields[1].strip()).expanduser()
                    if path.is_absolute():
                        found.append(path)
                    else:
                        logger.debug("Ignoring relative source index path in %s", index)
        except (OSError, UnicodeError) as exc:
            logger.debug("Source index %s is unreadable: %s", index, exc)
    return found


def _matches(path: Path, sha256: str) -> bool:
    try:
        return path.is_file() and file_digest(path) == sha256
    except OSError:
        return False


def find_indexed(sha256: str) -> Path | None:
    """The first indexed location whose bytes match ``sha256``."""
    return next((path for path in indexed_paths(sha256) if _matches(path, sha256)), None)


def resolve_source(recorded_path: str, sha256: str) -> Path:
    """Return a readable file holding the recorded bytes, preferring the recorded path.

    Raises SourceUnavailable when no candidate's digest matches; a file with
    different bytes is never returned.
    """
    recorded = Path(recorded_path).expanduser()
    if _matches(recorded, sha256):
        return recorded
    located = find_indexed(sha256)
    if located is None:
        raise SourceUnavailable(f"Source changed or is missing: {recorded_path}")
    return located
