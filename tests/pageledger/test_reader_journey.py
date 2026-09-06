"""Execute the maintained first-run tutorial as one dependent shell journey."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_first_run_tutorial_executes_the_documented_sequence(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(ROOT / "docs" / "first-run.md"),
            "--work-dir",
            str(tmp_path / "tutorial"),
            "--python",
            sys.executable,
            "--source-root",
            str(ROOT),
            "--expected-version",
            "0.4.1",
        ],
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
    for receipt in (
        "TUTORIAL_WARNING_OK",
        "TUTORIAL_RERUN_SELECTION_OK",
        "TUTORIAL_EXTERNAL_REVIEW_INTEGRITY_OK",
        "TUTORIAL_REPLAY_RELOCATION_OK",
    ):
        assert receipt in result.stdout
