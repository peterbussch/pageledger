"""Canonical reproducibility profiles used by verified replay."""

from __future__ import annotations

import hashlib
import inspect
import json
import locale
import platform
import re
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from ._version import __version__

PROFILE_VERSION = "0.1"
_MATERIAL_KINDS = {"binary", "package", "model", "asset"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def profile_sha256(profile: Mapping[str, object]) -> str:
    """Hash a profile after removing its self-referential digest."""
    payload = dict(profile)
    payload.pop("profile_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_reproducibility_profile(adapter: Any) -> dict[str, Any] | None:
    """Build PageLedger's strict, path-free profile envelope for *adapter*."""
    hook = getattr(adapter, "reproducibility_profile", None)
    if hook is None:
        return None
    if not callable(hook):
        raise ValueError("adapter reproducibility_profile must be callable")
    try:
        supplied = hook()
        materials = _validate_materials(supplied)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"adapter reproducibility_profile is invalid: {exc}") from exc

    profile: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "pageledger": {
            "version": __version__,
            "code_sha256": _package_code_sha256(),
        },
        "adapter": {
            "module": type(adapter).__module__,
            "name": adapter.name,
            "version": adapter.version,
            "code_sha256": _adapter_code_sha256(adapter),
        },
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "preferred_encoding": locale.getpreferredencoding(False),
            "filesystem_encoding": sys.getfilesystemencoding(),
        },
        "materials": sorted(materials, key=lambda item: (item["kind"], item["name"])),
    }
    profile["profile_sha256"] = profile_sha256(profile)
    return profile


def _validate_materials(supplied: object) -> list[dict[str, str]]:
    if not isinstance(supplied, Mapping):
        raise ValueError("adapter reproducibility_profile must be a mapping")
    if set(supplied) != {"materials"}:
        raise ValueError(
            "adapter reproducibility_profile must contain exactly the 'materials' key"
        )
    materials = supplied["materials"]
    if not isinstance(materials, list):
        raise ValueError("adapter reproducibility_profile materials must be a list")

    validated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, material in enumerate(materials):
        if not isinstance(material, Mapping):
            raise ValueError(
                f"adapter reproducibility_profile material {index} must be a mapping"
            )
        if set(material) != {"kind", "name", "version", "sha256"}:
            raise ValueError(
                "adapter reproducibility_profile materials must contain exactly "
                "kind, name, version, and sha256"
            )
        kind = material["kind"]
        name = material["name"]
        version = material["version"]
        digest = material["sha256"]
        if not all(isinstance(value, str) for value in (kind, name, version, digest)):
            raise ValueError(
                "adapter reproducibility_profile material fields must be strings"
            )
        if kind not in _MATERIAL_KINDS:
            raise ValueError(
                f"adapter reproducibility_profile material kind is invalid: {kind!r}"
            )
        if not name or not version:
            raise ValueError(
                "adapter reproducibility_profile material name and version must be non-empty"
            )
        if _looks_like_path(name) or _looks_like_path(version):
            raise ValueError(
                "adapter reproducibility_profile material names and versions must not contain paths"
            )
        if _SHA256.fullmatch(digest) is None:
            raise ValueError(
                "adapter reproducibility_profile material sha256 must be lowercase SHA-256"
            )
        identity = (kind, name)
        if identity in seen:
            raise ValueError(
                "adapter reproducibility_profile materials must have unique kind/name pairs"
            )
        seen.add(identity)
        validated.append(
            {"kind": kind, "name": name, "version": version, "sha256": digest}
        )

    try:
        json.dumps(supplied, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"adapter reproducibility_profile must contain finite JSON data: {exc}"
        ) from exc
    return validated


def _looks_like_path(value: str) -> bool:
    return (
        value.startswith(("/", "\\", "~"))
        or "/" in value
        or "\\" in value
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _hash_named_files(files: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative, path in sorted(files, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            if not path.is_file():
                continue
            digest.update(path.read_bytes())
        except OSError as exc:
            raise ValueError(f"Cannot hash reproducibility material '{relative}': {exc}") from exc
    return digest.hexdigest()


def _package_code_sha256() -> str:
    package_dir = Path(__file__).resolve().parent
    files = [
        (path.relative_to(package_dir).as_posix(), path)
        for path in package_dir.glob("*.py")
        if path.is_file()
    ]
    return _hash_named_files(files)


def _adapter_code_sha256(adapter: Any) -> str:
    source = inspect.getsourcefile(type(adapter))
    if not source:
        raise ValueError("Cannot locate regular source/module file for adapter code")
    path = Path(source)
    if not path.is_file():
        raise ValueError("Cannot locate regular source/module file for adapter code")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot read adapter source/module file: {exc}") from exc


def package_material(name: str) -> dict[str, str]:
    """Return a deterministic material descriptor for an installed distribution."""
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise ValueError(f"Cannot locate package distribution '{name}'") from exc
    files = distribution.files
    if files is None:
        raise ValueError(f"Cannot enumerate files for package distribution '{name}'")
    named_files: list[tuple[str, Path]] = []
    for relative in files:
        path = Path(str(distribution.locate_file(relative)))
        if path.is_file():
            named_files.append((Path(relative).as_posix(), path))
    if not named_files:
        raise ValueError(f"Cannot hash files for package distribution '{name}'")
    return {
        "kind": "package",
        "name": name,
        "version": distribution.version,
        "sha256": _hash_named_files(named_files),
    }


def binary_material(name: str, path: str, version: str) -> dict[str, str]:
    """Return a material descriptor for an executable without storing its path."""
    executable = Path(path)
    if not executable.is_file():
        raise ValueError(f"Cannot locate regular executable for '{name}'")
    try:
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot hash executable for '{name}': {exc}") from exc
    return {"kind": "binary", "name": name, "version": version, "sha256": digest}


def model_material(name: str, path: Path, version: str = "unknown") -> dict[str, str]:
    """Return a material descriptor for a trained model file."""
    if not path.is_file():
        raise ValueError(f"Cannot locate trained-data model '{name}'")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot hash trained-data model '{name}': {exc}") from exc
    return {"kind": "model", "name": name, "version": version, "sha256": digest}
