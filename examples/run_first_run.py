#!/usr/bin/env python3
"""Execute the maintained shell journey embedded in docs/first-run.md.

This helper is intentionally standard-library-only. It can run the journey
against either a source checkout or an isolated installed wheel.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

FENCE = "```bash pageledger-tutorial"


def _tutorial_script(document: Path) -> str:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in document.read_text(encoding="utf-8").splitlines():
        if line == FENCE:
            if current is not None:
                raise ValueError("nested executable tutorial fence")
            current = []
        elif line == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        raise ValueError("unterminated executable tutorial fence")
    if not blocks:
        raise ValueError(f"no {FENCE!r} blocks found")
    return "\n\n".join(blocks) + "\n"


def _import_receipt(python: Path, *, cwd: Path, env: dict[str, str]) -> dict[str, str]:
    command = (
        "import json, pageledger; "
        "print(json.dumps({'version': pageledger.__version__, "
        "'path': str(pageledger.__file__)}))"
    )
    result = subprocess.run(
        [str(python), "-c", command],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"PageLedger import failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument("--forbid-import-root", type=Path)
    args = parser.parse_args(argv)

    if args.source_root is not None and args.forbid_import_root is not None:
        parser.error("--source-root and --forbid-import-root are mutually exclusive")

    document = args.document.resolve(strict=True)
    python = args.python.expanduser().absolute()
    if not python.is_file():
        raise FileNotFoundError(python)
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=False)

    env = os.environ.copy()
    if args.source_root is not None:
        env["PYTHONPATH"] = str(args.source_root.resolve(strict=True))
    else:
        env.pop("PYTHONPATH", None)
    env["PYTHON"] = str(python)

    receipt = _import_receipt(python, cwd=work_dir, env=env)
    if args.expected_version is not None and receipt["version"] != args.expected_version:
        raise RuntimeError(
            f"expected PageLedger {args.expected_version}, imported {receipt['version']}"
        )
    if args.forbid_import_root is not None:
        forbidden = args.forbid_import_root.resolve(strict=True)
        imported = Path(receipt["path"]).resolve(strict=True)
        if imported == forbidden or forbidden in imported.parents:
            raise RuntimeError(f"PageLedger imported from forbidden checkout: {imported}")

    shim_dir = work_dir / ".tutorial-bin"
    shim_dir.mkdir()
    shim = shim_dir / "pageledger"
    shim.write_text(
        "#!/bin/sh\nexec " + shlex.quote(str(python)) + " -m pageledger \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")

    print(f"PageLedger {receipt['version']} imported from {receipt['path']}")
    result = subprocess.run(
        ["/bin/sh"],
        input="set -eu\n" + _tutorial_script(document),
        cwd=work_dir,
        env=env,
        text=True,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
