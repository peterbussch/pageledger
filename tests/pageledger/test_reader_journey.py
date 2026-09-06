"""Execute the maintained first-run tutorial as one dependent shell journey."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_exact_wheel_mode_rejects_distribution_module_version_mismatch(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "mismatched-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin" / "python"
    purelib = Path(
        subprocess.run(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    package = purelib / "pageledger"
    package.mkdir()
    (package / "__init__.py").write_text(
        '__version__ = "0.4.1"\n', encoding="utf-8"
    )
    distribution = purelib / "pageledger-9.9.9.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pageledger\nVersion: 9.9.9\n",
        encoding="utf-8",
    )
    document = tmp_path / "journey.md"
    document.write_text(
        "```bash pageledger-tutorial\necho TUTORIAL_RAN\n```\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(python),
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(document),
            "--work-dir",
            str(tmp_path / "tutorial"),
            "--python",
            str(python),
            "--expected-version",
            "0.4.1",
            "--forbid-import-root",
            str(ROOT),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert result.returncode != 0
    assert "distribution metadata version 9.9.9" in result.stderr
    assert "TUTORIAL_RAN" not in result.stdout


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
