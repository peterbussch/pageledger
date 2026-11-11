"""Page images as a source: their identity, their native size, and the PDF the renderers read.

A source is one image file (a one-page document) or a folder of page images
(one document, pages in natural order of file name). Its identity is the
SHA-256 of a canonical manifest that lists each page's file name, SHA-256,
size in bytes and pixel width and height, so a changed, missing, added or
reordered image changes it.

Every built-in engine renders from a PDF with Poppler. For them PageLedger
writes a carrier: a PDF that holds each image's own bytes, JPEG as it is and
PNG with its compressed data unchanged where PDF can carry it. Pages a run does
not read are left empty in the carrier, so it stays small. The carrier is
derived and reproducible: the same images and pages give the same bytes. It is
never the record of custody; the images are.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import tempfile
import zlib
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

KIND = "page_images"
# Page size for an image that declares no usable density.
DEFAULT_PPI = 300
# A declared density outside this range is treated as no declaration.
_PPI_RANGE = (20.0, 4800.0)

_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_CONVERT = {
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".jp2": "JPEG 2000",
    ".j2k": "JPEG 2000",
    ".jpx": "JPEG 2000",
    ".jpf": "JPEG 2000",
    ".gif": "GIF",
    ".bmp": "BMP",
    ".webp": "WebP",
    ".heic": "HEIC",
    ".heif": "HEIF",
}
_CONVERSIONS = {
    "TIFF": "magick page.tif page.png (ImageMagick) or vips copy page.tif page.png",
    "JPEG 2000": "opj_decompress -i page.jp2 -o page.png (OpenJPEG)",
}
IMAGE_SUFFIXES = frozenset({*_MEDIA_TYPES, *_CONVERT})
_ROTATIONS = {1: 0, 3: 180, 6: 90, 8: 270}


def _diagnostic(code: str, message: str) -> Exception:
    from .adapters import PageLedgerDiagnostic

    return PageLedgerDiagnostic(code, message)


@dataclass(frozen=True)
class ImagePage:
    name: str
    path: Path
    sha256: str
    bytes: int
    width: int
    height: int
    media_type: str
    ppi: tuple[float, float]
    ppi_basis: str
    rotation: int

    def entry(self) -> dict[str, Any]:
        """The page's line in the canonical manifest."""
        return {
            "name": self.name,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
        }

    def record(self) -> dict[str, Any]:
        """What provenance records about the page's image."""
        return {
            **self.entry(),
            "media_type": self.media_type,
            "ppi": list(self.ppi),
            "ppi_basis": self.ppi_basis,
            "rotation": self.rotation,
        }

    @property
    def points(self) -> tuple[float, float]:
        return self.width * 72 / self.ppi[0], self.height * 72 / self.ppi[1]


@dataclass(frozen=True)
class ImageSource:
    path: Path
    pages: tuple[ImagePage, ...]
    skipped: tuple[str, ...] = ()
    # The IIIF fetch record of a folder fetch-iiif wrote: context, not identity.
    fetch: dict[str, Any] | None = None

    @property
    def sha256(self) -> str:
        return manifest_sha256(self.manifest())

    def manifest(self) -> list[dict[str, Any]]:
        return [page.entry() for page in self.pages]

    def page(self, number: int) -> ImagePage:
        if not 1 <= number <= len(self.pages):
            raise ValueError(f"page_number {number} out of range for {self.path}")
        return self.pages[number - 1]


def manifest_sha256(entries: list[dict[str, Any]]) -> str:
    """SHA-256 of the canonical JSON of the page list: sorted keys, no spaces, UTF-8."""
    encoded = json.dumps(
        {"kind": KIND, "pages": entries},
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_image_source(path: Path) -> bool:
    """An image file, or a folder holding at least one (hidden files aside)."""
    if path.is_dir():
        return any(
            child.suffix.lower() in IMAGE_SUFFIXES and not child.name.startswith(".")
            for child in path.iterdir()
            if child.is_file()
        )
    return path.suffix.lower() in IMAGE_SUFFIXES


def _natural_key(name: str) -> tuple[list[Any], str]:
    parts = re.split(r"(\d+)", name.lower())
    return [int(part) if index % 2 else part for index, part in enumerate(parts)], name


def read_image_source(path: Path) -> ImageSource:
    """Hash and measure every page image; refuse anything that is not one.

    A folder ``fetch-iiif`` wrote also holds ``iiif-fetch.json`` and ``iiif/``.
    They are read only when the record lists exactly the folder's images with
    their hashes, and the record is kept as context beside the images.
    """
    from .iiif import RECORD_DIR, RECORD_NAME

    if path.is_dir():
        files: list[Path] = []
        skipped: list[str] = []
        record = folder = False
        for child in path.iterdir():
            if child.name.startswith("."):
                skipped.append(child.name)
            elif child.name == RECORD_NAME and child.is_file() and not child.is_symlink():
                record = True
            elif child.name == RECORD_DIR and child.is_dir() and not child.is_symlink():
                folder = True
            elif not child.is_file():
                raise ValueError(
                    f"{path} is a folder of page images, so PageLedger reads only the images "
                    f"directly inside it; '{child.name}' is not one. Move it out of the folder."
                )
            else:
                files.append(child)
        if folder and not record:
            raise ValueError(
                f"{path} is a folder of page images, so PageLedger reads only the images "
                f"directly inside it; '{RECORD_DIR}' is not one, and without {RECORD_NAME} "
                "the folder is not a IIIF snapshot. Move it out of the folder."
            )
        if not files:
            raise ValueError(f"No page images in {path}")
        files.sort(key=lambda item: _natural_key(item.name))
        pages = tuple(_read_page(item) for item in files)
        fetch = _fetch_annotation(path, pages) if record else None
        return ImageSource(path, pages, tuple(sorted(skipped)), fetch)
    return ImageSource(path, (_read_page(path),))


def _fetch_annotation(path: Path, pages: tuple[ImagePage, ...]) -> dict[str, Any]:
    from .iiif import RECORD_NAME, read_fetch_record

    try:
        fetched = read_fetch_record(path, {page.name: page.sha256 for page in pages})
    except ValueError as exc:
        raise ValueError(
            f"{exc}. A folder holding {RECORD_NAME} is read as the IIIF snapshot it records, "
            "so its images must be exactly the ones fetch-iiif fetched."
        ) from exc
    assert fetched is not None
    canvases = fetched["canvases"]
    return {
        "sha256": fetched["sha256"],
        "requested_url": fetched["requested_url"],
        "status": fetched["status"],
        "canvases": [{"name": page.name, **canvases[page.name]} for page in pages],
    }


def _read_page(path: Path) -> ImagePage:
    suffix = path.suffix.lower()
    if suffix not in _MEDIA_TYPES:
        kind = _CONVERT.get(suffix)
        if kind is None:
            raise ValueError(
                f"'{path.name}' is not a page image, but its folder holds page images. A folder "
                "is read either as one document of page images (JPEG or PNG only) or as separate "
                "files, never both: move the images or the other files out."
            )
        how = _CONVERSIONS.get(kind)
        raise _diagnostic(
            "unsupported_image",
            f"'{path.name}' is {kind}; PageLedger reads JPEG and PNG page images. Convert it "
            "losslessly to PNG first"
            + (f", for example: {how}." if how else ".")
            + " The converted files are then the source PageLedger records.",
        )
    data = path.read_bytes()
    width, height, ppi, basis, rotation = (_jpeg if suffix != ".png" else _png)(data, path.name)
    return ImagePage(
        name=path.name,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        width=width,
        height=height,
        media_type=_MEDIA_TYPES[suffix],
        ppi=ppi or (float(DEFAULT_PPI), float(DEFAULT_PPI)),
        ppi_basis=basis if ppi else "default",
        rotation=rotation,
    )


def source_identity(path: Path, kind: str | None) -> str:
    """The recorded identity of a source: its manifest hash for page images, else its bytes."""
    if kind == KIND:
        return read_image_source(path).sha256
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_changes(recorded: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[str]:
    """Plain descriptions of how a folder differs from its recorded manifest."""
    before = {entry["name"]: entry for entry in recorded}
    after = {entry["name"]: entry for entry in current}
    changes = [f"missing: {name}" for name in before if name not in after]
    changes += [f"added: {name}" for name in after if name not in before]
    changes += [
        f"changed: {name}" for name in before if name in after and before[name] != after[name]
    ]
    if not changes and [e["name"] for e in recorded] != [e["name"] for e in current]:
        changes.append("reordered")
    return changes


def snapshot_warnings(source: dict[str, Any], located: Path) -> list[dict[str, Any]]:
    """What verification says about the IIIF snapshot a source was read from.

    ``located`` holds the recorded images, so the fetch record there has already
    been checked against them; only its own bytes can differ.
    """
    recorded = source.get("iiif")
    if not isinstance(recorded, dict):
        return []
    from .iiif import RECORD_NAME

    warnings = []
    if recorded.get("status") == "incomplete":
        warnings.append(_incomplete(source, recorded))
    record = located / RECORD_NAME
    current = hashlib.sha256(record.read_bytes()).hexdigest() if record.is_file() else None
    if current != recorded.get("sha256"):
        warnings.append(
            {
                "code": "iiif_record_changed",
                "message": (
                    f"The IIIF fetch record in {located} "
                    + ("is gone" if current is None else "changed")
                    + " since this was written; the page images it read are unchanged"
                ),
                "source": source["path"],
            }
        )
    return warnings


def snapshot_growth(source: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Warnings for a IIIF snapshot that ``fetch-iiif --resume`` added images to, or None.

    The snapshot has grown when its folder is still a valid snapshot of the same
    URL and every recorded image is there unchanged. The recorded pages keep
    their numbers; the folder read anew would number them differently.
    """
    recorded = source.get("iiif")
    images = source.get("images")
    if source.get("kind") != KIND or not isinstance(recorded, dict) or not images:
        return None
    try:
        current = read_image_source(Path(source["path"]).expanduser())
    except (OSError, ValueError, RuntimeError):
        return None
    if current.fetch is None or current.fetch["requested_url"] != recorded.get("requested_url"):
        return None
    now = {entry["name"]: entry for entry in current.manifest()}
    if any(now.get(entry.get("name")) != entry for entry in images):
        return None
    names = {entry["name"] for entry in images}
    added = [name for name in now if name not in names]
    if not added:
        return None
    warnings = [_incomplete(source, recorded)] if recorded.get("status") == "incomplete" else []
    warnings.append(
        {
            "code": "iiif_snapshot_grown",
            "message": (
                f"fetch-iiif --resume added {len(added)} image(s) to {source['path']} after this "
                f"was written ({', '.join(added)}); every image it read is unchanged and keeps "
                "its page number here. A new job on the folder would number pages differently."
            ),
            "source": source["path"],
            "added": added,
        }
    )
    return warnings


def _incomplete(source: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": "iiif_snapshot_incomplete",
        "message": (
            f"{source['path']} was an incomplete IIIF snapshot of {recorded.get('requested_url')} "
            "when this was written: canvases that were never fetched are not in it"
        ),
        "source": source["path"],
    }


def _malformed(name: str, what: str) -> Exception:
    return _diagnostic(
        "malformed_image",
        f"'{name}' could not be read as an image ({what}); the file may be damaged or "
        "truncated. PageLedger does not repair sources.",
    )


def _density(x: float, y: float, per_cm: bool) -> tuple[float, float] | None:
    if per_cm:
        x, y = x * 2.54, y * 2.54
    if not all(_PPI_RANGE[0] <= value <= _PPI_RANGE[1] for value in (x, y)):
        return None
    return round(x, 4), round(y, 4)


def _exif(tiff: bytes) -> tuple[tuple[float, float] | None, int]:
    """Density and orientation from an EXIF (TIFF) block; anything unreadable is absent."""
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    try:
        if order is None or struct.unpack(order + "H", tiff[2:4])[0] != 42:
            return None, 1
        offset = struct.unpack(order + "I", tiff[4:8])[0]
        count = struct.unpack(order + "H", tiff[offset : offset + 2])[0]
        orientation, unit, resolution = 1, 2, {}
        for index in range(count):
            entry = tiff[offset + 2 + 12 * index : offset + 14 + 12 * index]
            tag, kind = struct.unpack(order + "HH", entry[:4])
            if tag == 0x0112 and kind == 3:
                orientation = struct.unpack(order + "H", entry[8:10])[0]
            elif tag == 0x0128 and kind == 3:
                unit = struct.unpack(order + "H", entry[8:10])[0]
            elif tag in (0x011A, 0x011B) and kind == 5:
                at = struct.unpack(order + "I", entry[8:12])[0]
                numerator, denominator = struct.unpack(order + "II", tiff[at : at + 8])
                if denominator:
                    resolution[tag] = numerator / denominator
    except (struct.error, IndexError):
        return None, 1
    density = None
    if unit in (2, 3) and len(resolution) == 2:
        density = _density(resolution[0x011A], resolution[0x011B], unit == 3)
    return density, orientation


def _rotation(orientation: int, name: str) -> int:
    if orientation in (2, 4, 5, 7):
        raise _diagnostic(
            "unsupported_image",
            f"'{name}' is stored mirrored (EXIF orientation {orientation}). Save it with the "
            "orientation applied, for example: magick page.jpg -auto-orient page.png",
        )
    return _ROTATIONS.get(orientation, 0)


_SOF = {0xC0, 0xC1, 0xC2}
_SOF_OTHER = {0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_header(data: bytes, name: str) -> dict[str, Any]:
    """Frame, density and orientation from a JPEG's header, up to its first scan.

    Everything PageLedger cannot carry to a PDF renderer is refused here, so
    ``fetch-iiif`` keeps only JPEGs a folder of page images can read. ``scan``
    says whether the header reached the image data.
    """
    if data[:2] != b"\xff\xd8":
        raise _malformed(name, "no JPEG start marker")
    position, frame = 2, None
    jfif = exif = None
    adobe = scan = False
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise _malformed(name, "broken marker sequence")
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker in (0xD9, 0xDA):
            scan = marker == 0xDA
            break
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        body = data[position + 4 : position + 2 + length]
        if length < 2 or len(body) != length - 2:
            raise _malformed(name, "truncated segment")
        if marker == 0xE0 and body[:5] == b"JFIF\x00" and len(body) >= 12 and jfif is None:
            units = body[7]
            x, y = int.from_bytes(body[8:10], "big"), int.from_bytes(body[10:12], "big")
            jfif = _density(x, y, units == 2) if units in (1, 2) else None
        elif marker == 0xE1 and body[:6] == b"Exif\x00\x00" and exif is None:
            exif = _exif(body[6:])
        elif marker == 0xEE and body[:5] == b"Adobe":
            adobe = True
        elif marker in _SOF_OTHER:
            raise _diagnostic(
                "unsupported_image",
                f"'{name}' is a lossless, hierarchical or arithmetic-coded JPEG, which PDF "
                "renderers do not read. Convert it losslessly to PNG first.",
            )
        elif marker in _SOF:
            if frame is not None:
                raise _malformed(name, "more than one frame header")
            if len(body) < 6 or len(body) != 6 + 3 * body[5]:
                raise _malformed(name, "short frame header")
            frame = {
                "precision": body[0],
                "height": int.from_bytes(body[1:3], "big"),
                "width": int.from_bytes(body[3:5], "big"),
                "components": body[5],
            }
        position += 2 + length
    if frame is None:
        raise _malformed(name, "no frame header")
    if frame["precision"] != 8 or frame["components"] not in (1, 3, 4):
        raise _diagnostic(
            "unsupported_image",
            f"'{name}' is a {frame['precision']}-bit JPEG with {frame['components']} "
            "components; PDF carries 8-bit grey, RGB and CMYK JPEG. Convert it losslessly "
            "to PNG first.",
        )
    if not frame["width"] or not frame["height"]:
        raise _malformed(name, "no image size in the frame header")
    density, basis, orientation = jfif, "jfif", 1
    if exif is not None:
        if density is None:
            density, basis = exif[0], "exif"
        orientation = exif[1]
    return {
        **frame,
        "density": density,
        "basis": basis,
        "rotation": _rotation(orientation, name),
        "adobe": adobe,
        "scan": scan,
    }


def _jpeg(data: bytes, name: str) -> tuple[int, int, tuple[float, float] | None, str, int]:
    frame = jpeg_header(data, name)
    return frame["width"], frame["height"], frame["density"], frame["basis"], frame["rotation"]


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _png_chunks(data: bytes, name: str) -> dict[str, Any]:
    if data[:8] != _PNG_SIGNATURE:
        raise _malformed(name, "no PNG signature")
    position, header, palette, transparency = 8, None, None, None
    density, orientation, idat = None, 1, []
    while True:
        if position + 12 > len(data):
            raise _malformed(name, "truncated chunk")
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        crc = data[position + 8 + length : position + 12 + length]
        if len(body) != length or len(crc) != 4:
            raise _malformed(name, "truncated chunk")
        if zlib.crc32(kind + body) != int.from_bytes(crc, "big"):
            raise _malformed(name, f"bad checksum in {kind.decode('latin-1')} chunk")
        position += 12 + length
        if header is None and kind != b"IHDR":
            raise _malformed(name, "IHDR is not the first chunk")
        if kind == b"IHDR" and length == 13:
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            header = (width, height, depth, color, compression, filtering, interlace)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"pHYs" and length == 9:
            x, y, unit = struct.unpack(">IIB", body)
            if unit == 1:
                density = _density(x * 0.0254, y * 0.0254, False)
        elif kind == b"eXIf":
            orientation = _exif(body)[1]
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None or not idat:
        raise _malformed(name, "no image header or data")
    width, height, depth, color, compression, filtering, interlace = header
    if (
        not width
        or not height
        or depth not in _PNG_DEPTHS.get(color, ())
        or compression
        or filtering
        or interlace not in (0, 1)
        or (color == 3 and not palette)
    ):
        raise _malformed(name, "invalid image header")
    if interlace:
        raise _diagnostic(
            "unsupported_image",
            f"'{name}' is an interlaced PNG. Save it without interlacing first, for example: "
            "magick page.png -interlace none page-flat.png",
        )
    return {
        "width": width,
        "height": height,
        "depth": depth,
        "color": color,
        "palette": palette,
        "transparency": transparency,
        "density": density,
        "orientation": orientation,
        "data": b"".join(idat),
    }


def _png(data: bytes, name: str) -> tuple[int, int, tuple[float, float] | None, str, int]:
    chunks = _png_chunks(data, name)
    _png_mask(chunks, name)
    return (
        chunks["width"],
        chunks["height"],
        chunks["density"],
        "png_phys",
        _rotation(chunks["orientation"], name),
    )


def _png_mask(chunks: dict[str, Any], name: str) -> list[int] | None:
    """A PNG's single transparent colour as a PDF colour-key mask."""
    transparency, color = chunks["transparency"], chunks["color"]
    if transparency is None or color in (4, 6):
        return None
    if color == 0 and len(transparency) >= 2:
        value = int.from_bytes(transparency[:2], "big")
        return [value, value]
    if color == 2 and len(transparency) >= 6:
        values = struct.unpack(">HHH", transparency[:6])
        return [value for value in values for _ in range(2)]
    if color == 3 and set(transparency) <= {0, 255}:
        clear = [index for index, alpha in enumerate(transparency) if alpha == 0]
        return [value for index in clear for value in (index, index)] or None
    raise _diagnostic(
        "unsupported_image",
        f"'{name}' has a palette with partial transparency. Save it as an RGB PNG first, "
        "for example: magick page.png -type TrueColorAlpha page-rgb.png",
    )


def _unfilter(raw: bytes, height: int, row: int, step: int, name: str) -> bytes:
    """Undo PNG's per-row filters; ``step`` is bytes per pixel, at least one."""
    if len(raw) < height * (row + 1):
        raise _malformed(name, "image data is shorter than its header says")
    out = bytearray()
    previous = bytearray(row)
    for index in range(height):
        start = index * (row + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + row])
        if kind == 1:
            for i in range(step, row):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif kind == 2:
            for i in range(row):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(row):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(row):
                a = line[i - step] if i >= step else 0
                b = previous[i]
                c = previous[i - step] if i >= step else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        elif kind:
            raise _malformed(name, f"unknown row filter {kind}")
        out += line
        previous = line
    return bytes(out)


def _number(value: float) -> bytes:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return (text if text not in ("", "-0") else "0").encode()


def _image_objects(page: ImagePage) -> list[tuple[dict[bytes, bytes], bytes]]:
    """The page image as PDF image dictionaries and streams; a soft mask comes second."""
    data = page.path.read_bytes()
    if hashlib.sha256(data).hexdigest() != page.sha256:
        raise ValueError(f"Page image changed since it was hashed: {page.path}")
    size = {b"/Width": str(page.width).encode(), b"/Height": str(page.height).encode()}
    if page.media_type == "image/jpeg":
        frame = jpeg_header(data, page.name)
        spaces = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}
        entries = {
            **size,
            b"/ColorSpace": spaces[frame["components"]],
            b"/BitsPerComponent": b"8",
            b"/Filter": b"/DCTDecode",
        }
        if frame["components"] == 4 and frame["adobe"]:
            # Adobe writes CMYK JPEG inverted.
            entries[b"/Decode"] = b"[1 0 1 0 1 0 1 0]"
        return [(entries, data)]
    chunks = _png_chunks(data, page.name)
    color, depth = chunks["color"], chunks["depth"]
    if color == 3:
        palette = chunks["palette"]
        space = b"[/Indexed /DeviceRGB %d <%s>]" % (len(palette) // 3 - 1, palette.hex().encode())
    else:
        space = b"/DeviceGray" if color in (0, 4) else b"/DeviceRGB"
    entries = {**size, b"/ColorSpace": space, b"/BitsPerComponent": str(depth).encode()}
    if color not in (4, 6):
        entries[b"/Filter"] = b"/FlateDecode"
        entries[b"/DecodeParms"] = (
            b"<< /Predictor 15 /Colors %d /BitsPerComponent %d /Columns %d >>"
            % (
                _PNG_CHANNELS[color],
                depth,
                page.width,
            )
        )
        mask = _png_mask(chunks, page.name)
        if mask:
            entries[b"/Mask"] = b"[" + b" ".join(str(v).encode() for v in mask) + b"]"
        return [(entries, chunks["data"])]
    # PDF has no image with an alpha channel: split it into a soft mask. Both are
    # stored unfiltered, so the carrier does not depend on a zlib build.
    sample = depth // 8
    channels = _PNG_CHANNELS[color]
    pixel = channels * sample
    try:
        raw = zlib.decompress(chunks["data"])
    except zlib.error as exc:
        raise _malformed(page.name, "compressed data is damaged") from exc
    pixels = _unfilter(raw, page.height, page.width * pixel, pixel, page.name)
    colour_bytes = (channels - 1) * sample
    colour = bytearray(len(pixels) // pixel * colour_bytes)
    alpha = bytearray(len(pixels) // pixel * sample)
    for offset in range(colour_bytes):
        colour[offset::colour_bytes] = pixels[offset::pixel]
    for offset in range(sample):
        alpha[offset::sample] = pixels[colour_bytes + offset :: pixel]
    soft = {**size, b"/ColorSpace": b"/DeviceGray", b"/BitsPerComponent": str(depth).encode()}
    return [(entries, bytes(colour)), (soft, bytes(alpha))]


def _dictionary(entries: dict[bytes, bytes]) -> bytes:
    return b"<< " + b" ".join(key + b" " + value for key, value in entries.items()) + b" >>"


class _Sink(Protocol):
    def write(self, data: bytes, /) -> Any: ...


class _Writer:
    def __init__(self, sink: _Sink) -> None:
        self.sink = sink
        self.position = 0
        self.offsets: dict[int, int] = {}

    def write(self, data: bytes) -> None:
        self.sink.write(data)
        self.position += len(data)

    def object(self, number: int, body: bytes, stream: bytes | None = None) -> None:
        self.offsets[number] = self.position
        self.write(b"%d 0 obj\n" % number + body)
        if stream is not None:
            self.write(b"\nstream\n" + stream + b"\nendstream")
        self.write(b"\nendobj\n")


def write_carrier(source: ImageSource, sink: _Sink, pages: Collection[int] | None = None) -> None:
    """Write the carrier PDF; pages outside ``pages`` keep their size but carry no image."""
    wanted = set(range(1, len(source.pages) + 1)) if pages is None else set(pages)
    writer = _Writer(sink)
    writer.write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    writer.object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    number, kids = 3, []
    for index, page in enumerate(source.pages, start=1):
        width, height = page.points
        entries = {
            b"/Type": b"/Page",
            b"/Parent": b"2 0 R",
            b"/MediaBox": b"[0 0 " + _number(width) + b" " + _number(height) + b"]",
        }
        if page.rotation:
            entries[b"/Rotate"] = str(page.rotation).encode()
        kids.append(number)
        page_number, number = number, number + 1
        if index in wanted:
            objects = _image_objects(page)
            contents, image = number, number + 1
            number += 2
            entries[b"/Resources"] = b"<< /XObject << /Im0 %d 0 R >> >>" % image
            entries[b"/Contents"] = b"%d 0 R" % contents
            writer.object(page_number, _dictionary(entries))
            drawing = b"q " + _number(width) + b" 0 0 " + _number(height) + b" 0 0 cm /Im0 Do Q"
            writer.object(contents, b"<< /Length %d >>" % len(drawing), drawing)
            image_entries, image_data = objects[0]
            image_entries = {b"/Type": b"/XObject", b"/Subtype": b"/Image", **image_entries}
            if len(objects) > 1:
                image_entries[b"/SMask"] = b"%d 0 R" % number
            image_entries[b"/Length"] = str(len(image_data)).encode()
            writer.object(image, _dictionary(image_entries), image_data)
            if len(objects) > 1:
                mask_entries, mask_data = objects[1]
                mask_entries = {b"/Type": b"/XObject", b"/Subtype": b"/Image", **mask_entries}
                mask_entries[b"/Length"] = str(len(mask_data)).encode()
                writer.object(number, _dictionary(mask_entries), mask_data)
                number += 1
        else:
            writer.object(page_number, _dictionary(entries))
    references = b" ".join(b"%d 0 R" % kid for kid in kids)
    writer.object(2, b"<< /Type /Pages /Kids [" + references + b"] /Count %d >>" % len(kids))
    start = writer.position
    writer.write(b"xref\n0 %d\n0000000000 65535 f \n" % number)
    for item in range(1, number):
        writer.write(b"%010d 00000 n \n" % writer.offsets[item])
    writer.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (number, start))


class _Tee:
    def __init__(self, sink: _Sink | None) -> None:
        self.sink = sink
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> None:
        self.digest.update(data)
        if self.sink is not None:
            self.sink.write(data)


def carrier_sha256(source: ImageSource, pages: Collection[int] | None = None) -> str:
    """The carrier's SHA-256, computed without writing it."""
    tee = _Tee(None)
    write_carrier(source, tee, pages)
    return tee.digest.hexdigest()


_CARRIERS: dict[Path, ImageSource] = {}


@contextmanager
def carrier(source: ImageSource, pages: Collection[int]) -> Iterator[tuple[Path, str]]:
    """Write a temporary carrier for ``pages`` and register it while it exists."""
    with tempfile.TemporaryDirectory(prefix="pageledger-carrier-") as directory:
        path = (Path(directory) / "carrier.pdf").resolve()
        with path.open("wb") as handle:
            tee = _Tee(handle)
            write_carrier(source, tee, pages)
        _CARRIERS[path] = source
        try:
            yield path, tee.digest.hexdigest()
        finally:
            _CARRIERS.pop(path, None)


def carrier_page(path: Path, page_number: int) -> ImagePage | None:
    """The page image behind page ``page_number`` of a registered carrier, if ``path`` is one."""
    source = _CARRIERS.get(path.absolute()) or _CARRIERS.get(path.resolve())
    if source is None or not 1 <= page_number <= len(source.pages):
        return None
    return source.pages[page_number - 1]


def read_sha256(entry: dict[str, Any], page_number: int) -> str:
    """The hash of the file an engine read for a page: the carrier, the page image, or the source."""
    if isinstance(entry.get("carrier"), dict):
        return entry["carrier"]["sha256"]
    if entry.get("kind") == KIND:
        return entry["images"][page_number - 1]["sha256"]
    return entry["sha256"]


def page_target(source: dict[str, Any], page_number: int) -> tuple[str, str]:
    """How a source link reaches one page: a path to add (the page's image) and a fragment."""
    images = source.get("images") if source.get("kind") == KIND else None
    if not images:
        return "", f"#page={page_number}"
    if Path(source["path"]).suffix.lower() in IMAGE_SUFFIXES:
        return "", ""
    return "/" + images[page_number - 1]["name"], ""
