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


_IMAGE_FIELDS = {
    "artifact",
    "sha256",
    "media_type",
    "bytes",
    "width",
    "height",
    "color_space",
    "crop",
    "renderer",
}
_TILES_FIELDS = {
    "schema_version",
    "kind",
    "source_sha256",
    "page_number",
    "requested_model",
    "model",
    "provider",
    "prompt_sha256",
    "plan_sha256",
    "page_box",
    "separator",
    "tiles",
}
_TILE_FIELDS = _IMAGE_FIELDS | {
    "tile_id",
    "outcome",
    "failure_code",
    "model",
    "provider",
    "text_offset",
    "text_length",
    "text_sha256",
}
_TILE_ID = re.compile(r"t[0-9]{2}\Z")
TILE_SEPARATOR = "\n"


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _check_image(image: dict[str, Any], label: str = "") -> None:
    """One exact JPEG: its format, file, hash, size, crop and how it was rendered."""
    if image["media_type"] != "image/jpeg" or image["color_space"] != "RGB":
        raise _invalid(f"{label}unsupported descriptor format")
    if not _is_sha256(image["sha256"]):
        raise _invalid(f"{label}sha256 must be a SHA-256")
    for field, maximum in (
        ("bytes", MAX_IMAGE_BYTES),
        ("width", MAX_IMAGE_DIMENSION),
        ("height", MAX_IMAGE_DIMENSION),
    ):
        value = image[field]
        if type(value) is not int or value < 1 or value > maximum:
            raise _invalid(f"{label}{field} outside supported bounds")
    _artifact_parts(image["artifact"])
    crop = image["crop"]
    if crop != {"kind": "full_page"}:
        if (
            not isinstance(crop, dict)
            or set(crop) != {"kind", "units", "x", "y", "width", "height"}
            or crop["kind"] != "box"
            or crop["units"] != "pdf_points"
        ):
            raise _invalid(f"{label}crop must describe a full page or exact PDF point box")
        for field in ("x", "y", "width", "height"):
            value = crop[field]
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or value < 0
                or (field in {"width", "height"} and value == 0)
            ):
                raise _invalid(f"{label}crop coordinates")
    renderer = image["renderer"]
    if (
        not isinstance(renderer, dict)
        or set(renderer) != {"command", "version", "parameters"}
        or not _nonempty(renderer["command"])
        or not _nonempty(renderer["version"])
        or not isinstance(renderer["parameters"], dict)
    ):
        raise _invalid(f"{label}renderer must record command, version, and parameters")


def _check_tiles(evidence: dict[str, Any]) -> None:
    from .adapters import ADAPTER_FAILURE_CODES

    if not _is_sha256(evidence["plan_sha256"]):
        raise _invalid("plan_sha256 must be a SHA-256")
    box = evidence["page_box"]
    if (
        not isinstance(box, dict)
        or set(box) != {"units", "width", "height"}
        or box["units"] != "pdf_points"
        or not all(
            type(box[key]) in {int, float} and math.isfinite(box[key]) and box[key] > 0
            for key in ("width", "height")
        )
    ):
        raise _invalid("page_box must be a positive box in pdf_points")
    if evidence["separator"] != TILE_SEPARATOR:
        raise _invalid("separator must be a line break")
    tiles = evidence["tiles"]
    if not isinstance(tiles, list) or not tiles:
        raise _invalid("tiles must list the tiles sent")
    last = ""
    artifacts: set[str] = set()
    for tile in tiles:
        if not isinstance(tile, dict) or set(tile) != _TILE_FIELDS:
            raise _invalid("tile fields")
        tile_id = tile["tile_id"]
        if not isinstance(tile_id, str) or not _TILE_ID.fullmatch(tile_id) or tile_id <= last:
            raise _invalid("tiles must be listed once each, in plan order (t01, t02, ...)")
        last = tile_id
        label = f"tile {tile_id}: "
        if tile["artifact"] in artifacts:
            raise _invalid(f"{label}artifact repeats another tile's")
        artifacts.add(tile["artifact"])
        for field in ("model", "provider"):
            if tile[field] is not None and not _nonempty(tile[field]):
                raise _invalid(f"{label}{field} must be non-empty or null")
        # The page names a model only when every tile that reports one names it; tiles
        # served by different models leave it null and fail the page.
        if None not in (tile["model"], evidence["model"]) and tile["model"] != evidence["model"]:
            raise _invalid(f"{label}model differs from the page's model")
        offset, length, digest = tile["text_offset"], tile["text_length"], tile["text_sha256"]
        if tile["outcome"] == "completed":
            if (
                tile["failure_code"] is not None
                or type(offset) is not int
                or type(length) is not int
                or offset < 0
                or length < 0
                or not _is_sha256(digest)
            ):
                raise _invalid(f"{label}a completed tile records its text slice")
        elif tile["outcome"] == "failed":
            if tile["failure_code"] not in ADAPTER_FAILURE_CODES or (offset, length, digest) != (
                None,
                None,
                None,
            ):
                raise _invalid(f"{label}a failed tile records a failure code and no text")
        else:
            raise _invalid(f"{label}outcome must be completed or failed")
        _check_image(tile, label)


def validate_input_evidence(
    evidence: Any,
    *,
    root: Path | None = None,
    source_sha256: str | None = None,
    page_number: int | None = None,
    prompt_sha256: str | None = None,
) -> None:
    """Check descriptor shape, expected page bindings, and optionally retained bytes.

    ``page_image`` describes the one JPEG sent for a page; ``page_tiles`` every tile
    image sent for a page read in tiles, whose text ``validate_tile_text`` checks.
    """
    kind = evidence.get("kind") if isinstance(evidence, dict) else None
    fields = _TILES_FIELDS if kind == "page_tiles" else _FIELDS
    if (
        not isinstance(evidence, dict)
        or not fields <= set(evidence)
        or set(evidence) - fields - {"system_prompt_sha256"}
    ):
        raise _invalid("descriptor fields")
    if not _json_value(evidence):
        raise _invalid("descriptor must contain finite JSON values")
    if evidence["schema_version"] != "0.1" or kind not in {"page_image", "page_tiles"}:
        raise _invalid("unsupported descriptor format")
    for field in ("source_sha256", "prompt_sha256", "system_prompt_sha256"):
        if field in evidence and not _is_sha256(evidence[field]):
            raise _invalid(f"{field} must be a SHA-256")
    if type(evidence["page_number"]) is not int or evidence["page_number"] < 1:
        raise _invalid("page_number outside supported bounds")
    for field in ("requested_model", "model", "provider"):
        if evidence[field] is None and field != "requested_model":
            continue
        if not _nonempty(evidence[field]):
            raise _invalid(f"{field} must be non-empty or an allowed null")
    if kind == "page_tiles":
        _check_tiles(evidence)
    else:
        _check_image(evidence)
    for field, expected in (
        ("source_sha256", source_sha256),
        ("page_number", page_number),
        ("prompt_sha256", prompt_sha256),
    ):
        if expected is not None and evidence[field] != expected:
            raise _invalid(f"{field} does not match the extraction request")
    if root is not None:
        for image in evidence["tiles"] if kind == "page_tiles" else [evidence]:
            label = f"tile {image['tile_id']}: " if kind == "page_tiles" else ""
            _check_bytes(image, Path(root), label)


def _check_bytes(image: dict[str, Any], root: Path, label: str) -> None:
    data = read_image_artifact(root, image["artifact"])
    if len(data) != image["bytes"] or hashlib.sha256(data).hexdigest() != image["sha256"]:
        raise _invalid(f"{label}artifact bytes or SHA-256 changed")
    if jpeg_dimensions(data) != (image["width"], image["height"]):
        raise _invalid(f"{label}JPEG dimensions differ from descriptor")


def validate_tile_text(evidence: Any, text: str) -> None:
    """Check that a tiled reading is exactly its completed tiles' texts, in order,
    joined by the separator, each slice matching its recorded hash."""
    if not isinstance(evidence, dict) or evidence.get("kind") != "page_tiles":
        return
    separator = evidence["separator"]
    cursor = 0
    completed = [tile for tile in evidence["tiles"] if tile["outcome"] == "completed"]
    for number, tile in enumerate(completed):
        if number:
            if text[cursor : cursor + len(separator)] != separator:
                raise _invalid(f"tile {tile['tile_id']}: texts are not joined by the separator")
            cursor += len(separator)
        piece = text[cursor : cursor + tile["text_length"]]
        if (
            tile["text_offset"] != cursor
            or len(piece) != tile["text_length"]
            or hashlib.sha256(piece.encode("utf-8")).hexdigest() != tile["text_sha256"]
        ):
            raise _invalid(f"tile {tile['tile_id']}: text differs from its recorded slice")
        cursor += tile["text_length"]
    if cursor != len(text):
        raise _invalid("the reading has text that no tile accounts for")
