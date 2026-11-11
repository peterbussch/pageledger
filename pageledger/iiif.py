"""Fetch the page images of a IIIF resource once, into a folder with a fetch record.

The snapshot holds one JPEG per canvas at the top level (``0001.jpg``), the
manifest and each image's ``info.json`` under ``iiif/``, and ``iiif-fetch.json``,
which binds every file to its hash and the URLs it came from.
"""

from __future__ import annotations

import email.utils
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ._version import __version__
from .checkpoint import atomic_bytes

RECORD_NAME = "iiif-fetch.json"
RECORD_DIR = "iiif"
USER_AGENT = f"PageLedger/{__version__} (IIIF fetch)"
DEFAULT_DELAY = 1.0
DEFAULT_MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_INFO_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
MAX_RETRIES = 5
RETRY_AFTER_CAP = 60.0
RETRY_STATUSES = frozenset({429, 502, 503, 504})
REQUEST_TIMEOUT = 60.0
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK = 1024 * 1024
_JSON_ACCEPT = "application/ld+json, application/json;q=0.9, */*;q=0.1"
_IMAGE_FILE = re.compile(r"\d{4,}\.jpg\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT = re.compile(r"iiif/(?:manifest|\d{4,}\.info)\.json\Z")


class Response(Protocol):
    status: int
    headers: Any

    def read(self, amount: int, /) -> bytes: ...

    def close(self) -> None: ...


Opener = Callable[[urllib.request.Request, float], Response]


class IIIFError(ValueError):
    code = "iiif_error"


class FetchError(IIIFError):
    """One resource could not be fetched or was not acceptable; others may still be."""

    code = "iiif_fetch_failed"


class ServerBusy(IIIFError):
    """The server kept refusing or the network kept failing; stop asking it."""

    code = "iiif_server_unavailable"


@dataclass(frozen=True)
class Fetched:
    url: str
    final_url: str
    content_type: str
    body: bytes
    attempts: int


@dataclass(frozen=True)
class Canvas:
    index: int
    canvas_id: str | None
    label: Any
    service_id: str | None
    problem: str | None = None


@dataclass(frozen=True)
class ImageInfo:
    api: int
    width: int
    height: int
    level: int | None
    sizes: tuple[tuple[int, int], ...]
    max_width: int | None
    max_height: int | None
    max_area: int | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def default_opener() -> Opener:
    """One request, one response: redirects and errors come back as responses."""
    opener = urllib.request.build_opener(_NoRedirect())

    def open_url(request: urllib.request.Request, timeout: float) -> Response:
        try:
            response: Response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error  # type: ignore[return-value]
        return response

    return open_url


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_url(url: str) -> None:
    if not url.isascii() or not url.isprintable() or any(c.isspace() for c in url):
        raise FetchError(f"URL holds a space, control or non-ASCII character: {url!r}")
    try:
        parts = urllib.parse.urlsplit(url)
        host, _port = parts.hostname, parts.port
    except ValueError as error:
        raise FetchError(f"not a usable URL ({error}): {url}") from error
    if parts.scheme not in {"http", "https"} or not host:
        raise FetchError(f"only http and https URLs can be fetched: {url}")
    if "@" in parts.netloc:
        raise FetchError(f"URL carries a user name or password, which would be recorded: {url}")


def _addresses(host: str) -> list[str]:
    try:
        return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
    except (OSError, UnicodeError):
        return []


def _public(host: str) -> bool:
    """Whether every address ``host`` names is on the public internet.

    A name that does not resolve counts as public: the request then fails on its own.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        addresses = [ipaddress.ip_address(host.split("%")[0])]
    except ValueError:
        addresses = [ipaddress.ip_address(item.split("%")[0]) for item in _addresses(host)]
    return all(address.is_global for address in addresses)


def _retry_wait(retry_after: str | None, attempt: int) -> float:
    if retry_after:
        value = retry_after.strip()
        try:
            seconds = float(value)
        except ValueError:
            try:
                date = email.utils.parsedate_to_datetime(value)
                # An HTTP date is GMT; "-0000" parses without a zone.
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = float(2**attempt)
        if math.isfinite(seconds):
            return min(max(seconds, 0.0), RETRY_AFTER_CAP)
    return min(float(2**attempt), RETRY_AFTER_CAP)


def _read_capped(response: Response, limit: int, url: str, what: str) -> bytes:
    declared = response.headers.get("Content-Length")
    declared = declared.strip() if declared is not None else ""
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise FetchError(
            f"{what}: {url} declares {int(declared)} bytes, above the {limit}-byte limit"
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(_CHUNK, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise FetchError(f"{what}: {url} is larger than the {limit}-byte limit")


class Client:
    """Polite sequential HTTP: a delay between requests, bounded redirects and retries.

    ``home`` is the URL the user named. Its host is trusted; any other host a
    manifest, service or redirect leads to must be on the public internet, and
    under an https ``home`` nothing is fetched over plain http.
    """

    def __init__(
        self,
        opener: Opener | None = None,
        *,
        delay: float = DEFAULT_DELAY,
        sleep: Callable[[float], None] | None = None,
        home: str | None = None,
    ) -> None:
        self.opener = opener or default_opener()
        self.delay = delay
        self.sleep = sleep or time.sleep
        self.requests = 0
        self.home = urllib.parse.urlsplit(home) if home is not None else None

    def _check_destination(self, url: str) -> None:
        check_url(url)
        if self.home is None:
            return
        parts = urllib.parse.urlsplit(url)
        if self.home.scheme == "https" and parts.scheme == "http":
            raise FetchError(f"refused a plain http URL under the https URL you named: {url}")
        host = parts.hostname or ""
        if host != self.home.hostname and not _public(host):
            raise FetchError(
                f"refused {url}: {host} is a loopback, private or link-local address, and "
                f"only the host of the URL you named ({self.home.hostname}) may be one"
            )

    def _open(self, url: str, accept: str) -> tuple[str, Response]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._check_destination(current)
            if self.requests:
                self.sleep(self.delay)
            self.requests += 1
            request = urllib.request.Request(
                current, headers={"User-Agent": USER_AGENT, "Accept": accept}
            )
            response = self.opener(request, REQUEST_TIMEOUT)
            if response.status not in _REDIRECT_STATUSES:
                return current, response
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise FetchError(f"redirect without a Location from {current}")
            target = urllib.parse.urljoin(current, location.strip())
            if (
                urllib.parse.urlsplit(current).scheme == "https"
                and urllib.parse.urlsplit(target).scheme == "http"
            ):
                raise FetchError(f"refused a redirect from https to http: {current} -> {target}")
            current = target
        raise FetchError(f"more than {MAX_REDIRECTS} redirects from {url}")

    def get(self, url: str, *, accept: str, limit: int, what: str) -> Fetched:
        problem = ""
        attempt = 0
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                final_url, response = self._open(url, accept)
                try:
                    if response.status == 200:
                        body = _read_capped(response, limit, final_url, what)
                        content_type = response.headers.get("Content-Type") or ""
                        return Fetched(url, final_url, content_type, body, attempt)
                    if response.status not in RETRY_STATUSES:
                        raise FetchError(f"{what}: HTTP {response.status} from {final_url}")
                    problem = f"HTTP {response.status}"
                    wait = _retry_wait(response.headers.get("Retry-After"), attempt)
                finally:
                    response.close()
            except http.client.InvalidURL as error:
                raise FetchError(f"{what}: not a usable URL ({error}): {url}") from error
            except (OSError, http.client.HTTPException) as error:
                problem = f"network error ({error})"
                wait = _retry_wait(None, attempt)
            if attempt <= MAX_RETRIES:
                self.sleep(wait)
        raise ServerBusy(
            f"{what}: {problem} from {url} after {attempt} attempts; "
            "the server may be busy, so try again later with --resume"
        )


def jpeg_frame(data: bytes) -> tuple[int, int, int]:
    """Width, height and component count from a JPEG header, at any size.

    The header is read by the same reader as a folder of page images, so a
    fetched image is one ``process`` can read. Unlike
    ``image_evidence.jpeg_dimensions`` this accepts grayscale and any size up to
    the page-image pixel limit; it reads only the header and leaves decoding to
    the renderer.
    """
    from .adapters import PageLedgerDiagnostic
    from .image_source import jpeg_header

    try:
        frame = jpeg_header(data, "the image")
    except PageLedgerDiagnostic as error:
        # The reason only: advice on converting local files does not fit a server's response.
        raise ValueError(error.message.split(". ")[0]) from error
    if frame["components"] not in {1, 3}:
        raise ValueError("JPEG must have one or three 8-bit components")
    if not frame["scan"]:
        raise ValueError("JPEG header ends before the image data")
    return frame["width"], frame["height"], frame["components"]


def _id(value: Mapping[str, Any]) -> str | None:
    for key in ("id", "@id"):
        if isinstance(value.get(key), str) and value[key]:
            return str(value[key])
    return None


def _type(value: Mapping[str, Any]) -> str:
    kind = value.get("type", value.get("@type"))
    return kind if isinstance(kind, str) else ""


def _strings(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, str)]


def _image_api(value: Mapping[str, Any]) -> int | None:
    kind = _type(value)
    if kind in {"ImageService1", "ImageService2", "ImageService3"}:
        return int(kind[-1])
    marks = _strings(value.get("@context")) + _strings(value.get("profile"))
    marks += [value["protocol"]] if isinstance(value.get("protocol"), str) else []
    for version in (3, 2, 1):
        if any(f"iiif.io/api/image/{version}" in mark for mark in marks):
            return version
    if any(mark == "http://iiif.io/api/image" for mark in marks):
        return 2 if "@id" in value else 3
    return None


def _service_id(services: Any) -> tuple[str | None, str | None]:
    """The first IIIF Image API 2 or 3 service, or why there is none."""
    entries = services if isinstance(services, list) else [services]
    reason = "no image service"
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        version = _image_api(entry)
        if version == 1:
            reason = "only an Image API 1 service, which is not supported"
        elif version in {2, 3} and _id(entry):
            return str(_id(entry)).rstrip("/"), None
    return None, reason


def _document_kind(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    kind = _type(document)
    if kind in {"sc:Manifest", "Manifest"}:
        return "manifest"
    if kind in {"sc:Collection", "Collection"}:
        return "collection"
    if _image_api(document) is not None and "width" in document and "height" in document:
        return "image_service"
    return None


def manifest_canvases(manifest: Mapping[str, Any]) -> tuple[int, list[Canvas]]:
    """Presentation API version and canvases in order, each with its image service or a problem."""
    if _type(manifest) == "sc:Manifest":
        version = 2
        sequences = manifest.get("sequences")
        first = sequences[0] if isinstance(sequences, list) and sequences else {}
        raw = first.get("canvases") if isinstance(first, dict) else None
    else:
        version = 3
        raw = manifest.get("items")
    if not isinstance(raw, list) or not raw:
        raise IIIFError("the manifest lists no canvases")
    canvases = []
    for index, canvas in enumerate(raw, start=1):
        if not isinstance(canvas, dict):
            canvases.append(Canvas(index, None, None, None, "not a canvas object"))
            continue
        body = _first_body(canvas, version)
        service: str | None
        problem: str | None
        if body is None:
            service, problem = None, "no image annotation"
        elif _type(body) in {"Choice", "oa:Choice"}:
            service, problem = None, "a choice of images, which is not supported"
        else:
            service, problem = _service_id(body.get("service"))
        canvases.append(Canvas(index, _id(canvas), canvas.get("label"), service, problem))
    return version, canvases


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _first_body(canvas: Mapping[str, Any], version: int) -> dict[str, Any] | None:
    if version == 2:
        annotation = _first(canvas.get("images"))
        body = annotation.get("resource") if isinstance(annotation, dict) else None
    else:
        page = _first(canvas.get("items"))
        annotation = _first(page.get("items")) if isinstance(page, dict) else None
        body = _first(annotation.get("body")) if isinstance(annotation, dict) else None
    return body if isinstance(body, dict) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def parse_info(document: Any) -> ImageInfo:
    """Read an Image API 2 or 3 ``info.json`` as servers actually write it."""
    if not isinstance(document, dict):
        raise FetchError("image information is not a JSON object")
    api = _image_api(document)
    if api == 1:
        raise FetchError("Image API 1 services are not supported")
    if api not in {2, 3}:
        raise FetchError("not an Image API 2 or 3 information document")
    width = _positive_int(document.get("width"))
    height = _positive_int(document.get("height"))
    if width is None or height is None:
        raise FetchError("image information has no positive width and height")
    profile = document.get("profile")
    entries = profile if isinstance(profile, list) else [profile]
    level = None
    for mark in _strings(profile):
        found = re.search(r"level([012])", mark)
        if found:
            level = int(found.group(1))
            break
    limits: dict[str, int | None] = {"maxWidth": None, "maxHeight": None, "maxArea": None}
    for source in [document, *[entry for entry in entries if isinstance(entry, dict)]]:
        for key in limits:
            value = _positive_int(source.get(key))
            current = limits[key]
            if value is not None:
                limits[key] = value if current is None else min(current, value)
    if limits["maxHeight"] is None:
        limits["maxHeight"] = limits["maxWidth"]
    sizes = []
    listed = document.get("sizes")
    for size in listed if isinstance(listed, list) else []:
        if isinstance(size, dict):
            size_width, size_height = (
                _positive_int(size.get("width")),
                _positive_int(size.get("height")),
            )
            if size_width and size_height:
                sizes.append((size_width, size_height))
    return ImageInfo(
        api=api,
        width=width,
        height=height,
        level=level,
        sizes=tuple(sizes),
        max_width=limits["maxWidth"],
        max_height=limits["maxHeight"],
        max_area=limits["maxArea"],
    )


def _within(info: ImageInfo, width: int, height: int) -> bool:
    return (
        width <= info.width
        and height <= info.height
        and (info.max_width is None or width <= info.max_width)
        and (info.max_height is None or height <= info.max_height)
        and (info.max_area is None or width * height <= info.max_area)
    )


def image_request(service_id: str, info: ImageInfo) -> str:
    """The URL of the whole image at the largest size the server allows, never above native.

    Without size limits that is ``full/max`` (Image API 3) or ``full/full`` (2).
    Image API 3 ``max`` already honours the limits. Image API 2 ``full`` does
    not, so a limited v2 service is asked for an explicit width that fits, or
    at level 0 for the largest listed size that fits.
    """
    base = service_id.rstrip("/")
    if _within(info, info.width, info.height) or info.api == 3:
        size = "max" if info.api == 3 else "full"
        return f"{base}/full/{size}/0/default.jpg"
    if info.level == 0:
        fitting = [size for size in info.sizes if _within(info, *size)]
        if not fitting:
            raise FetchError(
                "the level 0 service limits the image size and lists no size within the limits"
            )
        return f"{base}/full/{max(fitting)[0]},/0/default.jpg"
    scale = min(
        (info.max_width or info.width) / info.width,
        (info.max_height or info.height) / info.height,
        math.sqrt((info.max_area or info.width * info.height) / (info.width * info.height)),
    )
    width = max(1, math.floor(info.width * scale))
    while width > 1 and not _within(info, width, max(1, round(info.height * width / info.width))):
        width -= 1
    return f"{base}/full/{width},/0/default.jpg"


def _parse_json(body: bytes) -> Any:
    """The JSON document, or None. A value the record could not store again (NaN, a
    lone surrogate, nesting too deep) makes it None too."""
    try:
        document = json.loads(body.decode("utf-8-sig"))
        json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        return None
    return document


def _dump(value: Mapping[str, Any]) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return (text + "\n").encode("utf-8")


def _file_name(index: int, count: int) -> str:
    return f"{index:0{max(4, len(str(count)))}d}.jpg"


def _resolve(client: Client, url: str) -> tuple[Fetched, str, Any]:
    """Fetch what the user named: a manifest, an info.json, or an image service base URL."""
    first_error: IIIFError | None = None
    try:
        fetched = client.get(
            url, accept=_JSON_ACCEPT, limit=MAX_MANIFEST_BYTES, what="IIIF resource"
        )
    except FetchError as error:
        first_error = error
    else:
        document = _parse_json(fetched.body)
        kind = _document_kind(document)
        if kind == "collection":
            raise IIIFError("the URL names a IIIF collection; fetch one of its manifests instead")
        if kind is not None:
            return fetched, kind, document
    if url.rstrip("/").endswith("/info.json"):
        raise first_error or IIIFError(f"{url} is not a IIIF manifest or image information")
    info_url = url.rstrip("/") + "/info.json"
    try:
        fetched = client.get(
            info_url, accept=_JSON_ACCEPT, limit=MAX_INFO_BYTES, what="IIIF resource"
        )
    except FetchError as error:
        first = first_error or f"{url} is not a IIIF manifest or image information"
        raise IIIFError(f"{first}; trying {info_url} instead also failed: {error}") from error
    document = _parse_json(fetched.body)
    if _document_kind(document) != "image_service":
        raise IIIFError(f"neither {url} nor {info_url} is a IIIF manifest or image information")
    return fetched, "image_service", document


def _service_base(fetched: Fetched, document: Mapping[str, Any]) -> str:
    for candidate in (fetched.final_url, fetched.url):
        if candidate.endswith("/info.json"):
            return candidate[: -len("/info.json")]
    declared = _id(document)
    if declared is None:
        raise IIIFError(f"cannot tell the image service base URL from {fetched.final_url}")
    return declared.rstrip("/")


def _select(canvases: list[Canvas], pages: str | None) -> list[Canvas]:
    if pages is None:
        return canvases
    from .runner import _parse_pages_expression

    wanted = _parse_pages_expression(pages)
    beyond = [number for number in wanted if number > len(canvases)]
    if beyond:
        raise IIIFError(
            f"--pages asks for canvas {beyond[0]}, but the resource has {len(canvases)} canvases"
        )
    return [canvases[number - 1] for number in wanted]


def _missing_entry(canvas: Canvas, count: int, error: str | None) -> dict[str, Any]:
    return {
        "canvas_index": canvas.index,
        "canvas_id": canvas.canvas_id,
        "label": canvas.label,
        "file": _file_name(canvas.index, count),
        "error": error,
    }


def _fetch_canvas(
    client: Client, out: Path, canvas: Canvas, count: int, max_image_bytes: int
) -> dict[str, Any]:
    assert canvas.service_id is not None
    what = f"canvas {canvas.index}"
    info_fetched = client.get(
        f"{canvas.service_id}/info.json", accept=_JSON_ACCEPT, limit=MAX_INFO_BYTES, what=what
    )
    try:
        info = parse_info(_parse_json(info_fetched.body))
    except FetchError as error:
        raise FetchError(f"{what}: {info_fetched.final_url}: {error}") from error
    request_url = image_request(canvas.service_id, info)
    image = client.get(request_url, accept="image/jpeg", limit=max_image_bytes, what=what)
    media_type = image.content_type.split(";")[0].strip().lower()
    if media_type != "image/jpeg":
        raise FetchError(
            f"{what}: {image.final_url} answered {media_type or 'no content type'}, not image/jpeg"
        )
    try:
        width, height, _components = jpeg_frame(image.body)
    except ValueError as error:
        raise FetchError(f"{what}: {image.final_url} is not a usable JPEG: {error}") from error
    if not image.body.endswith(b"\xff\xd9"):
        raise FetchError(f"{what}: {image.final_url} is truncated (no end-of-image marker)")
    if width > info.width or height > info.height:
        raise FetchError(
            f"{what}: the server sent {width}x{height}, larger than the native "
            f"{info.width}x{info.height} its info.json declares"
        )
    name = _file_name(canvas.index, count)
    info_artifact = f"{RECORD_DIR}/{name.removesuffix('.jpg')}.info.json"
    atomic_bytes(out / info_artifact, info_fetched.body)
    atomic_bytes(out / name, image.body)
    return {
        "file": name,
        "sha256": _sha256(image.body),
        "bytes": len(image.body),
        "canvas_index": canvas.index,
        "canvas_id": canvas.canvas_id,
        "label": canvas.label,
        "service_id": canvas.service_id,
        "image_api": info.api,
        "info": {
            "url": info_fetched.url,
            "final_url": info_fetched.final_url,
            "artifact": info_artifact,
            "sha256": _sha256(info_fetched.body),
        },
        "native_width": info.width,
        "native_height": info.height,
        "request_url": request_url,
        "final_url": image.final_url,
        "width": width,
        "height": height,
        "below_native": width < info.width or height < info.height,
        "fetched_at": _now(),
        "attempts": image.attempts,
    }


def _top_level_images(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.iterdir()
        if not path.name.startswith(".") and path.suffix.lower() in IMAGE_SUFFIXES
    )


def _load_record(out: Path) -> tuple[dict[str, Any], bytes]:
    path = out / RECORD_NAME
    if path.is_symlink() or not path.is_file():
        raise IIIFError(f"{out} has no {RECORD_NAME} to resume or check")
    data = path.read_bytes()
    record = _parse_json(data)
    if (
        not isinstance(record, dict)
        or record.get("kind") != "iiif_fetch"
        or record.get("schema_version") != "0.1"
        or record.get("status") not in {"complete", "incomplete"}
        or not isinstance(record.get("requested_url"), str)
        or _positive_int(record.get("canvas_count")) is None
        or not isinstance(record.get("images"), list)
        or not isinstance(record.get("missing"), list)
    ):
        raise IIIFError(f"{path} is not a PageLedger IIIF fetch record")
    names = []
    for entry in record["images"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("file"), str)
            or not _IMAGE_FILE.match(entry["file"])
            or not isinstance(entry.get("sha256"), str)
            or not _SHA256.match(entry["sha256"])
            or any(
                _positive_int(entry.get(key)) is None
                for key in ("canvas_index", "width", "height", "native_width", "native_height")
            )
        ):
            raise IIIFError(f"{path} has a malformed image entry")
        names.append(entry["file"])
    for entry in record["missing"]:
        if (
            not isinstance(entry, dict)
            or _positive_int(entry.get("canvas_index")) is None
            or not isinstance(entry.get("error", 0), (str, type(None)))
        ):
            raise IIIFError(f"{path} has a malformed missing entry")
    if len(set(names)) != len(names):
        raise IIIFError(f"{path} lists an image twice")
    return record, data


def _check_artifact(out: Path, artifact: str, sha256: str) -> None:
    if not _ARTIFACT.match(artifact):
        raise IIIFError(f"{out}: the fetch record names an unexpected file {artifact!r}")
    path = out / artifact
    if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != sha256:
        raise IIIFError(f"{path} no longer matches the fetch record")


def _check_snapshot(out: Path, record: Mapping[str, Any]) -> None:
    recorded = {entry["file"] for entry in record["images"]}
    for entry in record["images"]:
        path = out / entry["file"]
        if path.is_symlink() or not path.is_file():
            raise IIIFError(f"{path} is listed in the fetch record but missing")
        data = path.read_bytes()
        if _sha256(data) != entry["sha256"] or len(data) != entry.get("bytes"):
            raise IIIFError(
                f"{path} no longer matches the fetch record; restore it, or start a new fetch"
            )
        info = entry.get("info")
        if isinstance(info, dict):
            _check_artifact(out, str(info.get("artifact")), str(info.get("sha256")))
    for path in _top_level_images(out):
        if path.name not in recorded:
            raise IIIFError(
                f"{path} is not in the fetch record (perhaps a fetch was interrupted while "
                "writing it); delete it and resume to fetch it again"
            )


def fetch_iiif(
    url: str,
    out: Path,
    *,
    pages: str | None = None,
    delay: float = DEFAULT_DELAY,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    resume: bool = False,
    opener: Opener | None = None,
    sleep: Callable[[float], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch the page images of ``url`` into ``out`` and return a summary of the snapshot."""
    if delay < 0 or not math.isfinite(delay):
        raise IIIFError("--delay must be a finite number of seconds, zero or more")
    if max_image_bytes < 1:
        raise IIIFError("--max-image-bytes must be positive")
    check_url(url)
    client = Client(opener, delay=delay, sleep=sleep, home=url)
    say = progress or (lambda _message: None)

    if resume:
        record, _ = _load_record(out)
        if record.get("requested_url") != url:
            raise IIIFError(f"{out} was fetched from {record.get('requested_url')}, not {url}")
        if pages is not None and pages != record.get("selection"):
            raise IIIFError(
                f"{out} was fetched with --pages {record.get('selection')!r}; "
                "resume with the same selection or start a new fetch"
            )
        _check_snapshot(out, record)
        record.pop("stopped_by", None)
        if record["status"] == "complete":
            return _summary(out, record, fetched_now=0)
        manifest = record.get("manifest")
        if isinstance(manifest, dict):
            _check_artifact(out, str(manifest.get("artifact")), str(manifest.get("sha256")))
            document = _parse_json((out / str(manifest["artifact"])).read_bytes())
            _version, canvases = manifest_canvases(document)
        else:
            fetched, _kind, document = _resolve(client, url)
            canvases = [Canvas(1, None, None, _service_base(fetched, document))]
        count = len(canvases)
        if record.get("canvas_count") != count:
            raise IIIFError(f"{out}: the stored manifest no longer matches the fetch record")
        missing_indexes = {entry.get("canvas_index") for entry in record["missing"]}
        pending = [canvas for canvas in canvases if canvas.index in missing_indexes]
    else:
        if out.exists() or out.is_symlink():
            raise IIIFError(f"{out} already exists; use --resume to continue a fetch into it")
        fetched, kind, document = _resolve(client, url)
        if kind == "manifest":
            version, canvases = manifest_canvases(document)
        else:
            canvases = [Canvas(1, None, None, _service_base(fetched, document))]
        pending = _select(canvases, pages)
        count = len(canvases)
        unusable = [canvas for canvas in pending if canvas.service_id is None]
        if unusable:
            canvas = unusable[0]
            named = f" ({canvas.canvas_id})" if canvas.canvas_id else ""
            raise IIIFError(
                f"canvas {canvas.index}{named} has {canvas.problem}; PageLedger fetches page "
                f"images only through a IIIF Image API 2 or 3 service"
                + (
                    f" ({len(unusable)} selected canvases have this problem)"
                    if len(unusable) > 1
                    else ""
                )
            )
        manifest = None
        if kind == "manifest":
            manifest = {
                "url": fetched.url,
                "final_url": fetched.final_url,
                "sha256": _sha256(fetched.body),
                "artifact": f"{RECORD_DIR}/manifest.json",
                "api_version": version,
            }
        record = {
            "schema_version": "0.1",
            "kind": "iiif_fetch",
            "tool": {"name": "pageledger", "version": __version__, "user_agent": USER_AGENT},
            "requested_url": url,
            "source_kind": kind,
            "manifest": manifest,
            "canvas_count": count,
            "selection": pages,
            "status": "incomplete",
            "started_at": _now(),
            "finished_at": None,
            "images": [],
            "missing": [_missing_entry(canvas, count, None) for canvas in pending],
        }
        first = _dump(record)
        out.mkdir(parents=True)
        (out / RECORD_DIR).mkdir()
        if manifest is not None:
            atomic_bytes(out / manifest["artifact"], fetched.body)
        atomic_bytes(out / RECORD_NAME, first)

    images = {entry["canvas_index"]: entry for entry in record["images"]}
    missing = {entry["canvas_index"]: entry for entry in record["missing"]}
    fetched_now = 0

    def save(finished: bool) -> None:
        record["images"] = [images[index] for index in sorted(images)]
        record["missing"] = [missing[index] for index in sorted(missing)]
        record["status"] = "incomplete" if missing else "complete"
        record["finished_at"] = _now() if finished else None
        atomic_bytes(out / RECORD_NAME, _dump(record))

    try:
        for canvas in pending:
            if canvas.service_id is None:
                missing[canvas.index] = _missing_entry(canvas, count, f"has {canvas.problem}")
                continue
            say(f"canvas {canvas.index} of {count}")
            try:
                entry = _fetch_canvas(client, out, canvas, count, max_image_bytes)
            except ServerBusy as error:
                missing[canvas.index] = _missing_entry(canvas, count, str(error))
                record["stopped_by"] = str(error)
                break
            except FetchError as error:
                missing[canvas.index] = _missing_entry(canvas, count, str(error))
                say(f"canvas {canvas.index}: {error}")
            else:
                images[canvas.index] = entry
                missing.pop(canvas.index, None)
                fetched_now += 1
                if entry["below_native"]:
                    say(_below_native_message(entry))
            save(finished=False)
    finally:
        save(finished=True)
    return _summary(out, record, fetched_now=fetched_now)


def _below_native_message(entry: Mapping[str, Any]) -> str:
    return (
        f"canvas {entry['canvas_index']}: the server allows only {entry['width']}x"
        f"{entry['height']}, below the native {entry['native_width']}x{entry['native_height']}"
    )


def _summary(out: Path, record: Mapping[str, Any], *, fetched_now: int) -> dict[str, Any]:
    stopped = record.get("stopped_by")
    return {
        "status": record["status"],
        "out": str(out),
        "record": str(out / RECORD_NAME),
        "requested_url": record["requested_url"],
        "canvas_count": record["canvas_count"],
        "fetched": len(record["images"]),
        "fetched_now": fetched_now,
        "missing": [
            {"canvas_index": entry["canvas_index"], "error": entry["error"]}
            for entry in record["missing"]
        ],
        "warnings": [
            {
                "code": "iiif_below_native",
                "canvas_index": entry["canvas_index"],
                "message": _below_native_message(entry),
            }
            for entry in record["images"]
            if entry.get("below_native")
        ],
        "stopped_by": stopped if isinstance(stopped, str) else None,
    }


def read_fetch_record(
    folder: Path, image_hashes: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Check a folder's IIIF fetch record against its images, for annotating a job's source.

    ``image_hashes`` maps each top-level image file name to its SHA-256, as a
    folder reader has already computed them; without it the folder's
    ``.jpg``, ``.jpeg``, ``.png``, ``.tif`` and ``.tiff`` files (not dotfiles)
    are hashed here. Returns None when the folder has no record, raises
    ValueError when the images and the record disagree in any way, and
    otherwise returns the record's own hash, requested URL and status, and
    ``canvases``: each image's canvas position and id, by file name.
    """
    if not (folder / RECORD_NAME).exists() and not (folder / RECORD_NAME).is_symlink():
        return None
    record, data = _load_record(folder)
    if image_hashes is None:
        image_hashes = {path.name: _sha256(path.read_bytes()) for path in _top_level_images(folder)}
    listed = {entry["file"]: entry["sha256"] for entry in record["images"]}
    extra = sorted(set(image_hashes) - set(listed))
    absent = sorted(set(listed) - set(image_hashes))
    if extra or absent:
        details = []
        if extra:
            details.append(f"not in the record: {', '.join(extra)}")
        if absent:
            details.append(f"recorded but missing: {', '.join(absent)}")
        raise IIIFError(f"{folder}: images and {RECORD_NAME} disagree ({'; '.join(details)})")
    changed = sorted(name for name, sha256 in listed.items() if image_hashes[name] != sha256)
    if changed:
        raise IIIFError(f"{folder}: images changed since they were fetched: {', '.join(changed)}")
    return {
        "sha256": _sha256(data),
        "requested_url": str(record["requested_url"]),
        "status": str(record["status"]),
        "canvases": {
            entry["file"]: {
                "canvas_index": entry["canvas_index"],
                "canvas_id": entry["canvas_id"]
                if isinstance(entry.get("canvas_id"), str)
                else None,
            }
            for entry in record["images"]
        },
    }
