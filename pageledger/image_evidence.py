"""Dependency-free validation of the exact image bytes supplied to an adapter."""

from __future__ import annotations

import hashlib
import math
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {
    "schema_version",
    "kind",
    "source_sha256",
    "page_number",
    "artifact",
    "sha256",
    "media_type",
    "bytes",
    "width",
    "height",
    "color_space",
    "crop",
    "renderer",
    "requested_model",
    "model",
    "provider",
    "prompt_sha256",
}


def _invalid(message: str) -> ValueError:
    return ValueError(f"Image evidence invalid: {message}")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json_value(value: Any, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item, depth + 1) for key, item in value.items())
    return False


def _artifact_parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise _invalid("artifact must be a relative evidence JPEG path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) < 2
        or path.parts[0] != "evidence"
        or any(part in {".", ".."} for part in path.parts)
        or path.suffix.lower() not in {".jpg", ".jpeg"}
    ):
        raise _invalid("artifact must be a contained relative evidence JPEG path")
    return path.parts


def read_image_artifact(root: Path, artifact: str) -> bytes:
    """Read at most the JPEG byte ceiling, rejecting symlinks in every component."""
    parts = _artifact_parts(artifact)
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, directory_flags)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as handle:
            import stat

            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _invalid("artifact is not a regular file")
            data = handle.read(MAX_IMAGE_BYTES + 1)
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise _invalid("JPEG exceeds the byte limit or is empty")
        return data
    except OSError as exc:
        raise _invalid("artifact missing, unsafe, or unreadable") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Bounded JPEG header inspection; decoding remains the renderer's job."""
    if (
        len(data) > MAX_IMAGE_BYTES
        or not data.startswith(b"\xff\xd8")
        or not data.endswith(b"\xff\xd9")
    ):
        raise _invalid("JPEG framing or byte limit")
    offset = 2
    dimensions = None
    while offset < len(data) - 2:
        if data[offset] != 255:
            raise _invalid("JPEG marker")
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise _invalid("JPEG segment length")
        if marker in {0xC0, 0xC1, 0xC2}:
            if length != 17 or data[offset + 2] != 8 or data[offset + 7] != 3:
                raise _invalid("JPEG must have three 8-bit color components")
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if not 1 <= width <= MAX_IMAGE_DIMENSION or not 1 <= height <= MAX_IMAGE_DIMENSION:
                raise _invalid("JPEG dimensions exceed limit")
            if dimensions is not None:
                raise _invalid("JPEG has multiple frame headers")
            dimensions = (width, height)
        if marker == 0xDA:
            if dimensions is None:
                raise _invalid("JPEG missing supported frame header")
            return dimensions
        offset += length
    raise _invalid("JPEG missing scan header")


def validate_input_evidence(
    evidence: Any,
    *,
    root: Path | None = None,
    source_sha256: str | None = None,
    page_number: int | None = None,
    prompt_sha256: str | None = None,
) -> None:
    """Check descriptor shape, expected page bindings, and optionally retained bytes."""
    if (
        not isinstance(evidence, dict)
        or not _FIELDS <= set(evidence)
        or set(evidence) - _FIELDS - {"system_prompt_sha256"}
    ):
        raise _invalid("descriptor fields")
    if not _json_value(evidence):
        raise _invalid("descriptor must contain finite JSON values")
    if (
        evidence["schema_version"] != "0.1"
        or evidence["kind"] != "page_image"
        or evidence["media_type"] != "image/jpeg"
        or evidence["color_space"] != "RGB"
    ):
        raise _invalid("unsupported descriptor format")
    for field in ("source_sha256", "sha256", "prompt_sha256", "system_prompt_sha256"):
        if field in evidence and (
            not isinstance(evidence[field], str) or not _SHA256.fullmatch(evidence[field])
        ):
            raise _invalid(f"{field} must be a SHA-256")
    for field, maximum in (
        ("page_number", None),
        ("bytes", MAX_IMAGE_BYTES),
        ("width", MAX_IMAGE_DIMENSION),
        ("height", MAX_IMAGE_DIMENSION),
    ):
        value = evidence[field]
        if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
            raise _invalid(f"{field} outside supported bounds")
    _artifact_parts(evidence["artifact"])
    for field in ("requested_model", "model", "provider"):
        if evidence[field] is None and field != "requested_model":
            continue
        if not _nonempty(evidence[field]):
            raise _invalid(f"{field} must be non-empty or an allowed null")
    crop = evidence["crop"]
    if crop != {"kind": "full_page"}:
        if (
            not isinstance(crop, dict)
            or set(crop) != {"kind", "units", "x", "y", "width", "height"}
            or crop["kind"] != "box"
            or crop["units"] != "pdf_points"
        ):
            raise _invalid("crop must describe a full page or exact PDF point box")
        for field in ("x", "y", "width", "height"):
            value = crop[field]
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or value < 0
                or (field in {"width", "height"} and value == 0)
            ):
                raise _invalid("crop coordinates")
    renderer = evidence["renderer"]
    if (
        not isinstance(renderer, dict)
        or set(renderer) != {"command", "version", "parameters"}
        or not _nonempty(renderer["command"])
        or not _nonempty(renderer["version"])
        or not isinstance(renderer["parameters"], dict)
    ):
        raise _invalid("renderer must record command, version, and parameters")
    for field, expected in (
        ("source_sha256", source_sha256),
        ("page_number", page_number),
        ("prompt_sha256", prompt_sha256),
    ):
        if expected is not None and evidence[field] != expected:
            raise _invalid(f"{field} does not match the extraction request")
    if root is not None:
        data = read_image_artifact(Path(root), evidence["artifact"])
        if len(data) != evidence["bytes"] or hashlib.sha256(data).hexdigest() != evidence["sha256"]:
            raise _invalid("artifact bytes or SHA-256 changed")
        if jpeg_dimensions(data) != (evidence["width"], evidence["height"]):
            raise _invalid("JPEG dimensions differ from descriptor")
