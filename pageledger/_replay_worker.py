"""Private replay worker request/response entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

from .replay import (
    _WORKER_GENERIC_CODE,
    _WORKER_GENERIC_MESSAGE,
    _WORKER_PROTOCOL_VERSION,
    ReplayError,
    _atomic_write_json,
    _replay_bundle_in_process,
)


def main(argv: list[str] | None = None) -> int:
    """Run one replay transaction and write its result envelope."""
    values = sys.argv[1:] if argv is None else argv
    request_id, result_path, bundle_dir, out_dir, adapter_path = values
    try:
        result = _replay_bundle_in_process(
            Path(bundle_dir),
            Path(out_dir),
            adapter_path=Path(adapter_path) if adapter_path else None,
        )
    except ReplayError as exc:
        envelope = {
            "protocol_version": _WORKER_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": exc.code, "message": str(exc)},
        }
        _atomic_write_json(Path(result_path), envelope)
        return 1
    except Exception:
        envelope = {
            "protocol_version": _WORKER_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {
                "code": _WORKER_GENERIC_CODE,
                "message": _WORKER_GENERIC_MESSAGE,
            },
        }
        _atomic_write_json(Path(result_path), envelope)
        return 1

    envelope = {
        "protocol_version": _WORKER_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }
    _atomic_write_json(Path(result_path), envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
