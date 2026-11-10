"""IIIF fetch: parsing, polite HTTP, safety checks, the record and resumption, all offline."""

from __future__ import annotations

import hashlib
import json
from email.message import Message
from pathlib import Path

import pytest

from pageledger import iiif
from pageledger.cli import main
from pageledger.image_evidence import jpeg_dimensions

SCHEMA = json.loads(
    (Path(__file__).parents[2] / "schemas" / "iiif-fetch.schema.json").read_text(encoding="utf-8")
)
MANIFEST_URL = "https://iiif.example.org/item/manifest.json"
SERVICE = "https://images.example.org/iiif/page"


def jpeg(width: int, height: int, components: int = 3, size: int = 64) -> bytes:
    frame = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes([components])
    frame += b"".join(bytes([n + 1, 0x11, 0]) for n in range(components))
    sof = b"\xff\xc0" + (len(frame) + 2).to_bytes(2, "big") + frame
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + sof + sos + bytes(size) + b"\xff\xd9"


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._body = body
        self.read_bytes = 0
        self.closed = False

    def read(self, amount: int) -> bytes:
        chunk = self._body[self.read_bytes : self.read_bytes + amount]
        self.read_bytes += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeServer:
    """Answers by URL; a list of answers is used in order and its last one repeats."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.responses: list[FakeResponse] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        answers = self.routes.get(url)
        if answers is None:
            response = FakeResponse(404)
        else:
            if isinstance(answers, list):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
            else:
                answer = answers
            if isinstance(answer, BaseException):
                raise answer
            response = answer if isinstance(answer, FakeResponse) else ok(answer)
        self.responses.append(response)
        return response

    def urls(self) -> list[str]:
        return [url for url, _ in self.requests]


def ok(body: object, content_type: str | None = None) -> FakeResponse:
    if isinstance(body, (dict, list)):
        return FakeResponse(200, json.dumps(body).encode(), {"Content-Type": "application/ld+json"})
    assert isinstance(body, bytes)
    return FakeResponse(200, body, {"Content-Type": content_type or "image/jpeg"})


def info_v3(service: str, width: int, height: int, **extra: object) -> dict:
    return {
        "@context": "http://iiif.io/api/image/3/context.json",
        "id": service,
        "type": "ImageService3",
        "protocol": "http://iiif.io/api/image",
        "profile": "level2",
        "width": width,
        "height": height,
        **extra,
    }


def info_v2(service: str, width: int, height: int, profile: object = None, **extra: object) -> dict:
    return {
        "@context": "http://iiif.io/api/image/2/context.json",
        "@id": service,
        "protocol": "http://iiif.io/api/image",
        "profile": profile or ["http://iiif.io/api/image/2/level2.json"],
        "width": width,
        "height": height,
        **extra,
    }


def manifest_v3(services: list[str | None], labels: list[object] | None = None) -> dict:
    items = []
    for index, service in enumerate(services, start=1):
        body: dict = {"id": f"{service}/full/max/0/default.jpg", "type": "Image"}
        if service is not None:
            body["service"] = [{"id": service, "type": "ImageService3", "profile": "level2"}]
        items.append(
            {
                "id": f"https://iiif.example.org/item/canvas/{index}",
                "type": "Canvas",
                "label": (labels or {})[index - 1] if labels else {"en": [f"p. {index}"]},
                "items": [{"type": "AnnotationPage", "items": [{"body": body}]}],
            }
        )
    return {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": MANIFEST_URL,
        "type": "Manifest",
        "items": items,
    }


def manifest_v2(services: list[dict | list]) -> dict:
    canvases = [
        {
            "@id": f"https://iiif.example.org/item/canvas/{index}",
            "@type": "sc:Canvas",
            "label": f"Page {index}",
            "images": [{"resource": {"@id": "x.jpg", "service": service}}],
        }
        for index, service in enumerate(services, start=1)
    ]
    return {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "@id": MANIFEST_URL,
        "@type": "sc:Manifest",
        "sequences": [{"canvases": canvases}],
    }


def two_page_v3() -> FakeServer:
    return FakeServer(
        {
            MANIFEST_URL: manifest_v3([f"{SERVICE}1", f"{SERVICE}2"]),
            f"{SERVICE}1/info.json": info_v3(f"{SERVICE}1", 300, 400),
            f"{SERVICE}2/info.json": info_v3(f"{SERVICE}2", 310, 410),
            f"{SERVICE}1/full/max/0/default.jpg": jpeg(300, 400),
            f"{SERVICE}2/full/max/0/default.jpg": jpeg(310, 410, components=1),
        }
    )


def validate(record: dict) -> None:
    from jsonschema import validate as check

    check(instance=record, schema=SCHEMA)


def load(out: Path) -> dict:
    return json.loads((out / iiif.RECORD_NAME).read_text(encoding="utf-8"))


def test_v3_manifest_snapshot_binds_bytes_to_urls(tmp_path: Path) -> None:
    server, sleeps = two_page_v3(), []
    out = tmp_path / "snap"
    summary = iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=sleeps.append)

    assert summary["status"] == "complete" and summary["fetched_now"] == 2
    assert sorted(path.name for path in out.iterdir()) == [
        "0001.jpg",
        "0002.jpg",
        "iiif",
        "iiif-fetch.json",
    ]
    assert sorted(path.name for path in (out / "iiif").iterdir()) == [
        "0001.info.json",
        "0002.info.json",
        "manifest.json",
    ]
    record = load(out)
    validate(record)
    assert record["manifest"]["api_version"] == 3
    assert (
        record["manifest"]["sha256"]
        == hashlib.sha256((out / "iiif/manifest.json").read_bytes()).hexdigest()
    )
    first, second = record["images"]
    assert first["request_url"] == f"{SERVICE}1/full/max/0/default.jpg"
    assert first["sha256"] == hashlib.sha256((out / "0001.jpg").read_bytes()).hexdigest()
    assert (first["width"], first["height"], first["below_native"]) == (300, 400, False)
    assert second["label"] == {"en": ["p. 2"]}
    assert record["finished_at"] is not None and record["missing"] == []
    assert all(headers["User-agent"] == iiif.USER_AGENT for _, headers in server.requests)
    assert iiif.USER_AGENT.startswith("PageLedger/") and "github" not in iiif.USER_AGENT.lower()
    # One request at a time, with the delay before every request after the first.
    assert sleeps == [iiif.DEFAULT_DELAY] * (len(server.requests) - 1)


def test_v2_manifest_service_list_and_profile_limits_fetch_below_native(tmp_path: Path) -> None:
    profile = ["http://iiif.io/api/image/2/level2.json", {"maxWidth": 4000, "formats": ["jpg"]}]
    service = {
        "@context": "http://iiif.io/api/image/2/context.json",
        "@id": SERVICE,
        "profile": profile[0],
    }
    server = FakeServer(
        {
            MANIFEST_URL: manifest_v2([[{"@id": "https://x.org/other"}, service]]),
            f"{SERVICE}/info.json": info_v2(SERVICE, 9500, 7000, profile),
            f"{SERVICE}/full/4000,/0/default.jpg": jpeg(4000, 2947, components=1),
        }
    )
    out = tmp_path / "snap"
    summary = iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)

    record = load(out)
    validate(record)
    image = record["images"][0]
    assert image["below_native"] is True and image["image_api"] == 2
    assert (image["native_width"], image["width"]) == (9500, 4000)
    assert summary["warnings"][0]["code"] == "iiif_below_native"
    assert all("pct:" not in url and "^" not in url for url in server.urls())


def test_level0_v2_with_limits_asks_for_the_largest_listed_size(tmp_path: Path) -> None:
    info = iiif.parse_info(
        info_v2(
            SERVICE,
            6000,
            4000,
            ["http://iiif.io/api/image/2/level0.json", {"maxWidth": 3000}],
            sizes=[
                {"width": 750, "height": 500},
                {"width": 3000, "height": 2000},
                {"width": 6000, "height": 4000},
            ],
        )
    )
    assert info.level == 0 and info.max_height == 3000
    assert iiif.image_request(SERVICE, info) == f"{SERVICE}/full/3000,/0/default.jpg"


def test_level0_v2_with_limits_and_no_fitting_size_fails() -> None:
    info = iiif.parse_info(
        info_v2(SERVICE, 6000, 4000, ["http://iiif.io/api/image/2/level0.json", {"maxWidth": 100}])
    )
    with pytest.raises(iiif.FetchError, match="level 0"):
        iiif.image_request(SERVICE, info)


@pytest.mark.parametrize(
    "document,expected",
    [
        (info_v2(SERVICE, 5000, 3000), "full/full/0/default.jpg"),
        (
            info_v2(SERVICE, 5000, 3000, "http://iiif.io/api/image/2/level1.json"),
            "full/full/0/default.jpg",
        ),
        (info_v3(SERVICE, 5000, 3000), "full/max/0/default.jpg"),
        (info_v3(SERVICE, 5000, 3000, maxWidth=2000), "full/max/0/default.jpg"),
        (
            info_v3(SERVICE, 5000, 3000, profile="level0", sizes=[{"width": 500, "height": 300}]),
            "full/max/0/default.jpg",
        ),
        (info_v2(SERVICE, 5000, 3000, maxWidth=2000), "full/2000,/0/default.jpg"),
        (info_v2(SERVICE, 5000, 3000, maxWidth=6000, maxHeight=1500), "full/2500,/0/default.jpg"),
        (info_v2(SERVICE, 5000, 3000, maxArea=3_750_000), "full/2500,/0/default.jpg"),
        (info_v2(SERVICE, 5000, 3000, maxWidth=9000), "full/full/0/default.jpg"),
    ],
)
def test_image_request_never_above_native(document: dict, expected: str) -> None:
    info = iiif.parse_info(document)
    url = iiif.image_request(SERVICE + "/", info)
    assert url == f"{SERVICE}/{expected}"
    size = url.split("/")[-3]
    if size[:-1].isdigit():
        width = int(size[:-1])
        assert width <= info.width
        assert iiif._within(info, width, round(info.height * width / info.width))


def test_parse_info_v3_infers_max_height_from_max_width() -> None:
    info = iiif.parse_info(info_v3(SERVICE, 5000, 8000, maxWidth=2000))
    assert (info.api, info.max_width, info.max_height, info.level) == (3, 2000, 2000, 2)


@pytest.mark.parametrize(
    "document,message",
    [
        (
            {
                "@context": "http://iiif.io/api/image/1/context.json",
                "@id": SERVICE,
                "width": 1,
                "height": 1,
            },
            "Image API 1",
        ),
        ({"width": 1, "height": 1}, "not an Image API"),
        (info_v3(SERVICE, 0, 10), "positive width"),
        ([], "JSON object"),
    ],
)
def test_parse_info_refuses_what_it_cannot_use(document: object, message: str) -> None:
    with pytest.raises(iiif.FetchError, match=message):
        iiif.parse_info(document)


def test_info_json_url_is_a_one_image_source(tmp_path: Path) -> None:
    server = FakeServer(
        {
            f"{SERVICE}/info.json": info_v2(SERVICE, 200, 100),
            f"{SERVICE}/full/full/0/default.jpg": jpeg(200, 100),
        }
    )
    out = tmp_path / "snap"
    iiif.fetch_iiif(f"{SERVICE}/info.json", out, opener=server, sleep=lambda _s: None)
    record = load(out)
    validate(record)
    assert record["source_kind"] == "image_service" and record["manifest"] is None
    assert record["images"][0]["canvas_id"] is None and record["canvas_count"] == 1


def test_base_url_redirect_to_info_json_is_followed_and_recorded(tmp_path: Path) -> None:
    server = FakeServer(
        {
            SERVICE: FakeResponse(303, headers={"Location": "page/info.json"}),
            f"{SERVICE}/info.json": info_v3(SERVICE, 200, 100),
            f"{SERVICE}/full/max/0/default.jpg": jpeg(200, 100),
        }
    )
    out = tmp_path / "snap"
    iiif.fetch_iiif(SERVICE, out, opener=server, sleep=lambda _s: None)
    record = load(out)
    assert record["images"][0]["service_id"] == SERVICE
    assert record["images"][0]["info"]["final_url"] == f"{SERVICE}/info.json"


def test_base_url_without_redirect_falls_back_to_info_json(tmp_path: Path) -> None:
    server = FakeServer(
        {
            f"{SERVICE}/info.json": info_v3(SERVICE, 200, 100),
            f"{SERVICE}/full/max/0/default.jpg": jpeg(200, 100),
        }
    )
    iiif.fetch_iiif(SERVICE, tmp_path / "snap", opener=server, sleep=lambda _s: None)
    assert server.urls()[:2] == [SERVICE, f"{SERVICE}/info.json"]


def test_retry_after_is_honoured_and_capped(tmp_path: Path) -> None:
    sleeps: list[float] = []
    server = two_page_v3()
    server.routes[f"{SERVICE}1/full/max/0/default.jpg"] = [
        FakeResponse(503, headers={"Retry-After": "600"}),
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(502),
        ok(jpeg(300, 400)),
    ]
    summary = iiif.fetch_iiif(
        MANIFEST_URL, tmp_path / "snap", opener=server, sleep=sleeps.append, delay=0.5
    )
    assert summary["status"] == "complete"
    waits = [value for value in sleeps if value != 0.5]
    assert waits == [iiif.RETRY_AFTER_CAP, 7.0, 8.0]
    assert load(tmp_path / "snap")["images"][0]["attempts"] == 4


def test_server_that_stays_busy_stops_the_fetch_and_resume_finishes_it(
    tmp_path: Path, capsys
) -> None:
    sleeps: list[float] = []
    server = two_page_v3()
    good = server.routes[f"{SERVICE}2/full/max/0/default.jpg"]
    server.routes[f"{SERVICE}2/full/max/0/default.jpg"] = FakeResponse(503)
    out = tmp_path / "snap"
    summary = iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=sleeps.append)

    assert summary["status"] == "incomplete"
    assert [item["canvas_index"] for item in summary["missing"]] == [2]
    assert "after 6 attempts" in summary["stopped_by"]
    assert [value for value in sleeps if value != 1.0] == [2.0, 4.0, 8.0, 16.0, 32.0]
    record = load(out)
    validate(record)
    assert record["status"] == "incomplete" and record["missing"][0]["file"] == "0002.jpg"
    assert "--resume" in record["missing"][0]["error"]

    server.routes[f"{SERVICE}2/full/max/0/default.jpg"] = good
    before = len(server.requests)
    summary = iiif.fetch_iiif(MANIFEST_URL, out, resume=True, opener=server, sleep=lambda _s: None)
    assert summary["status"] == "complete" and summary["fetched_now"] == 1
    # The stored manifest is reused; only canvas 2 is asked for again.
    assert server.urls()[before:] == [f"{SERVICE}2/info.json", f"{SERVICE}2/full/max/0/default.jpg"]
    record = load(out)
    validate(record)
    assert "stopped_by" not in record and [image["file"] for image in record["images"]] == [
        "0001.jpg",
        "0002.jpg",
    ]

    complete = (out / iiif.RECORD_NAME).read_bytes()
    again = iiif.fetch_iiif(
        MANIFEST_URL, out, resume=True, opener=FakeServer({}), sleep=lambda _s: None
    )
    assert again["status"] == "complete" and again["fetched_now"] == 0
    assert (out / iiif.RECORD_NAME).read_bytes() == complete


def test_network_errors_are_retried(tmp_path: Path) -> None:
    server = two_page_v3()
    server.routes[MANIFEST_URL] = [ConnectionResetError("reset"), ok(manifest_v3([f"{SERVICE}1"]))]
    summary = iiif.fetch_iiif(MANIFEST_URL, tmp_path / "snap", opener=server, sleep=lambda _s: None)
    assert summary["status"] == "complete"


def test_a_canvas_specific_failure_is_recorded_and_the_fetch_continues(tmp_path: Path) -> None:
    server = two_page_v3()
    server.routes[f"{SERVICE}1/full/max/0/default.jpg"] = ok(
        b"\x89PNG\r\n\x1a\n" + bytes(20), "image/png"
    )
    out = tmp_path / "snap"
    summary = iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)
    assert summary["status"] == "incomplete" and summary["fetched"] == 1
    assert "not image/jpeg" in summary["missing"][0]["error"]
    assert not (out / "0001.jpg").exists() and (out / "0002.jpg").exists()


@pytest.mark.parametrize(
    "body,message",
    [
        (b"\x89PNG\r\n\x1a\n" + bytes(20), "not a usable JPEG"),
        (jpeg(300, 400)[:-2], "truncated"),
        (jpeg(301, 400), "larger than the native"),
    ],
)
def test_image_bytes_are_checked_not_only_the_header(
    tmp_path: Path, body: bytes, message: str
) -> None:
    server = two_page_v3()
    server.routes[f"{SERVICE}1/full/max/0/default.jpg"] = ok(body, "image/jpeg")
    summary = iiif.fetch_iiif(
        MANIFEST_URL, tmp_path / "snap", opener=server, sleep=lambda _s: None, pages="1"
    )
    assert message in summary["missing"][0]["error"]


def test_image_size_cap_applies_while_streaming(tmp_path: Path) -> None:
    server = two_page_v3()
    big = FakeResponse(200, jpeg(300, 400, size=5000), {"Content-Type": "image/jpeg"})
    server.routes[f"{SERVICE}1/full/max/0/default.jpg"] = big
    summary = iiif.fetch_iiif(
        MANIFEST_URL,
        tmp_path / "snap",
        opener=server,
        sleep=lambda _s: None,
        pages="1",
        max_image_bytes=1000,
    )
    assert "larger than the 1000-byte limit" in summary["missing"][0]["error"]
    assert big.read_bytes <= 1001 and big.closed


def test_declared_content_length_above_cap_is_refused_unread(tmp_path: Path) -> None:
    server = two_page_v3()
    big = FakeResponse(
        200, jpeg(300, 400), {"Content-Type": "image/jpeg", "Content-Length": "999999"}
    )
    server.routes[f"{SERVICE}1/full/max/0/default.jpg"] = big
    summary = iiif.fetch_iiif(
        MANIFEST_URL,
        tmp_path / "snap",
        opener=server,
        sleep=lambda _s: None,
        pages="1",
        max_image_bytes=1000,
    )
    assert "declares 999999 bytes" in summary["missing"][0]["error"] and big.read_bytes == 0


def test_manifest_size_is_capped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(iiif, "MAX_MANIFEST_BYTES", 100)
    server = FakeServer({MANIFEST_URL: manifest_v3([f"{SERVICE}1"] * 10)})
    with pytest.raises(iiif.IIIFError, match="limit"):
        iiif.fetch_iiif(MANIFEST_URL, tmp_path / "snap", opener=server, sleep=lambda _s: None)
    assert not (tmp_path / "snap").exists()


def test_redirect_from_https_to_http_is_refused() -> None:
    server = FakeServer(
        {MANIFEST_URL: FakeResponse(302, headers={"Location": "http://iiif.example.org/m"})}
    )
    client = iiif.Client(server, sleep=lambda _s: None)
    with pytest.raises(iiif.FetchError, match="https to http"):
        client.get(MANIFEST_URL, accept="*/*", limit=100, what="manifest")


def test_redirects_are_bounded_and_the_final_url_recorded() -> None:
    hops = {
        f"https://a.org/{n}": FakeResponse(301, headers={"Location": f"/{n + 1}"}) for n in range(4)
    }
    client = iiif.Client(
        FakeServer({**hops, "https://a.org/3": ok({"x": 1})}), sleep=lambda _s: None
    )
    fetched = client.get("https://a.org/0", accept="*/*", limit=100, what="x")
    assert fetched.final_url == "https://a.org/3" and fetched.url == "https://a.org/0"
    client = iiif.Client(FakeServer(hops), sleep=lambda _s: None)
    with pytest.raises(iiif.FetchError, match="more than 3 redirects"):
        client.get("https://a.org/0", accept="*/*", limit=100, what="x")


def test_redirect_from_http_to_https_is_allowed() -> None:
    server = FakeServer(
        {
            "http://a.org/m": FakeResponse(301, headers={"Location": "https://a.org/m"}),
            "https://a.org/m": ok({}),
        }
    )
    assert (
        iiif.Client(server, sleep=lambda _s: None)
        .get("http://a.org/m", accept="*/*", limit=10, what="x")
        .final_url
        == "https://a.org/m"
    )


@pytest.mark.parametrize(
    "url,message",
    [
        ("file:///etc/passwd", "only http and https"),
        ("ftp://a.org/x", "only http and https"),
        ("https://user:secret@a.org/manifest", "user name or password"),
    ],
)
def test_only_plain_http_urls(tmp_path: Path, url: str, message: str) -> None:
    with pytest.raises(iiif.FetchError, match=message):
        iiif.fetch_iiif(url, tmp_path / "snap", opener=FakeServer({}), sleep=lambda _s: None)


def test_redirect_to_another_scheme_is_refused() -> None:
    server = FakeServer(
        {MANIFEST_URL: FakeResponse(302, headers={"Location": "file:///etc/passwd"})}
    )
    with pytest.raises(iiif.FetchError, match="only http and https"):
        iiif.Client(server, sleep=lambda _s: None).get(
            MANIFEST_URL, accept="*/*", limit=10, what="x"
        )


def test_canvas_without_image_service_fails_before_anything_is_written(tmp_path: Path) -> None:
    server = FakeServer({MANIFEST_URL: manifest_v3([f"{SERVICE}1", None])})
    out = tmp_path / "snap"
    with pytest.raises(
        iiif.IIIFError,
        match=r"canvas 2 \(https://iiif.example.org/item/canvas/2\) has no image service",
    ):
        iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)
    assert not out.exists() and server.urls() == [MANIFEST_URL]
    # Selecting around it is fine.
    server.routes.update(two_page_v3().routes)
    server.routes[MANIFEST_URL] = manifest_v3([f"{SERVICE}1", None])
    assert (
        iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None, pages="1")[
            "status"
        ]
        == "complete"
    )


def test_collection_and_image_api_1_services_are_refused(tmp_path: Path) -> None:
    collection = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": MANIFEST_URL,
        "type": "Collection",
    }
    with pytest.raises(iiif.IIIFError, match="collection"):
        iiif.fetch_iiif(
            MANIFEST_URL,
            tmp_path / "a",
            opener=FakeServer({MANIFEST_URL: collection}),
            sleep=lambda _s: None,
        )
    old = {"@context": "http://iiif.io/api/image/1/context.json", "@id": SERVICE}
    with pytest.raises(iiif.IIIFError, match="Image API 1"):
        iiif.fetch_iiif(
            MANIFEST_URL,
            tmp_path / "b",
            opener=FakeServer({MANIFEST_URL: manifest_v2([old])}),
            sleep=lambda _s: None,
        )


def test_labels_and_ids_never_name_files(tmp_path: Path) -> None:
    manifest = manifest_v3([f"{SERVICE}1"], labels=["../../escape"])
    manifest["items"][0]["id"] = "../../../etc/passwd"
    server = two_page_v3()
    server.routes[MANIFEST_URL] = manifest
    out = tmp_path / "snap"
    iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)
    record = load(out)
    assert record["images"][0]["file"] == "0001.jpg"
    assert record["images"][0]["label"] == "../../escape"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["snap"]


def test_pages_select_canvases_by_position(tmp_path: Path) -> None:
    out = tmp_path / "snap"
    summary = iiif.fetch_iiif(
        MANIFEST_URL, out, opener=two_page_v3(), sleep=lambda _s: None, pages="2"
    )
    assert (
        summary["fetched"] == 1 and (out / "0002.jpg").exists() and not (out / "0001.jpg").exists()
    )
    record = load(out)
    assert record["selection"] == "2" and record["canvas_count"] == 2
    with pytest.raises(iiif.IIIFError, match="canvas 5, but the resource has 2"):
        iiif.fetch_iiif(
            MANIFEST_URL,
            tmp_path / "other",
            opener=two_page_v3(),
            sleep=lambda _s: None,
            pages="1,5",
        )
    with pytest.raises(ValueError, match="--pages"):
        iiif.fetch_iiif(
            MANIFEST_URL, tmp_path / "third", opener=two_page_v3(), sleep=lambda _s: None, pages="x"
        )


def test_file_names_widen_for_long_manifests() -> None:
    assert iiif._file_name(7, 12_000) == "00007.jpg" and iiif._file_name(7, 300) == "0007.jpg"


def test_existing_output_needs_resume(tmp_path: Path) -> None:
    out = tmp_path / "snap"
    out.mkdir()
    with pytest.raises(iiif.IIIFError, match="--resume"):
        iiif.fetch_iiif(MANIFEST_URL, out, opener=two_page_v3(), sleep=lambda _s: None)


def _complete(tmp_path: Path) -> Path:
    out = tmp_path / "snap"
    iiif.fetch_iiif(MANIFEST_URL, out, opener=two_page_v3(), sleep=lambda _s: None)
    return out


def test_resume_refuses_a_changed_file(tmp_path: Path) -> None:
    out = _complete(tmp_path)
    (out / "0001.jpg").write_bytes(jpeg(300, 400, size=65))
    with pytest.raises(iiif.IIIFError, match="0001.jpg no longer matches"):
        iiif.fetch_iiif(MANIFEST_URL, out, resume=True, opener=two_page_v3(), sleep=lambda _s: None)


def test_resume_refuses_a_deleted_or_unrecorded_file(tmp_path: Path) -> None:
    out = _complete(tmp_path)
    (out / "0002.jpg").unlink()
    with pytest.raises(iiif.IIIFError, match="listed in the fetch record but missing"):
        iiif.fetch_iiif(MANIFEST_URL, out, resume=True, opener=two_page_v3(), sleep=lambda _s: None)


def test_an_image_written_but_not_recorded_must_be_deleted_before_resuming(tmp_path: Path) -> None:
    server = two_page_v3()
    server.routes[f"{SERVICE}2/full/max/0/default.jpg"] = FakeResponse(503)
    out = tmp_path / "snap"
    iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)
    (out / "0002.jpg").write_bytes(jpeg(310, 410))  # as if killed between the image and the record
    with pytest.raises(iiif.IIIFError, match="0002.jpg is not in the fetch record"):
        iiif.fetch_iiif(MANIFEST_URL, out, resume=True, opener=two_page_v3(), sleep=lambda _s: None)
    (out / "0002.jpg").unlink()
    assert (
        iiif.fetch_iiif(
            MANIFEST_URL, out, resume=True, opener=two_page_v3(), sleep=lambda _s: None
        )["status"]
        == "complete"
    )


def test_resume_refuses_another_url_or_selection_and_a_changed_manifest(tmp_path: Path) -> None:
    server = two_page_v3()
    server.routes[f"{SERVICE}2/full/max/0/default.jpg"] = FakeResponse(404)
    out = tmp_path / "snap"
    iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)
    with pytest.raises(iiif.IIIFError, match="was fetched from"):
        iiif.fetch_iiif(
            MANIFEST_URL + "?x", out, resume=True, opener=two_page_v3(), sleep=lambda _s: None
        )
    with pytest.raises(iiif.IIIFError, match="same selection"):
        iiif.fetch_iiif(
            MANIFEST_URL, out, resume=True, pages="2", opener=two_page_v3(), sleep=lambda _s: None
        )
    (out / "iiif" / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(iiif.IIIFError, match="manifest.json no longer matches"):
        iiif.fetch_iiif(MANIFEST_URL, out, resume=True, opener=two_page_v3(), sleep=lambda _s: None)
    with pytest.raises(iiif.IIIFError, match="no iiif-fetch.json"):
        iiif.fetch_iiif(
            MANIFEST_URL,
            tmp_path / "nothing",
            resume=True,
            opener=two_page_v3(),
            sleep=lambda _s: None,
        )


def test_an_interrupted_fetch_leaves_an_incomplete_record(tmp_path: Path) -> None:
    server = two_page_v3()
    server.routes[f"{SERVICE}2/info.json"] = KeyboardInterrupt()
    out = tmp_path / "snap"
    with pytest.raises(KeyboardInterrupt):
        iiif.fetch_iiif(MANIFEST_URL, out, opener=server, sleep=lambda _s: None)
    record = load(out)
    validate(record)
    assert record["status"] == "incomplete" and record["finished_at"] is not None
    assert [entry["canvas_index"] for entry in record["missing"]] == [2]
    assert record["missing"][0]["error"] is None
    assert iiif.read_fetch_record(out) == {
        "sha256": hashlib.sha256((out / iiif.RECORD_NAME).read_bytes()).hexdigest(),
        "requested_url": MANIFEST_URL,
        "status": "incomplete",
    }


def test_read_fetch_record_checks_the_folder(tmp_path: Path) -> None:
    assert iiif.read_fetch_record(tmp_path) is None
    out = _complete(tmp_path)
    summary = iiif.read_fetch_record(out)
    assert summary is not None and summary["status"] == "complete"
    (out / ".DS_Store").write_bytes(b"finder")
    (out / "notes.txt").write_text("mine", encoding="utf-8")
    assert iiif.read_fetch_record(out) == summary

    hashes = {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in ("0001.jpg", "0002.jpg")
    }
    assert iiif.read_fetch_record(out, hashes) == summary
    with pytest.raises(ValueError, match="images changed since they were fetched: 0002.jpg"):
        iiif.read_fetch_record(out, {**hashes, "0002.jpg": "0" * 64})
    with pytest.raises(ValueError, match="not in the record: extra.png"):
        iiif.read_fetch_record(out, {**hashes, "extra.png": "0" * 64})

    (out / "0003.jpeg").write_bytes(jpeg(10, 10))
    with pytest.raises(ValueError, match="not in the record: 0003.jpeg"):
        iiif.read_fetch_record(out)
    (out / "0003.jpeg").unlink()
    (out / "0001.jpg").unlink()
    with pytest.raises(ValueError, match="recorded but missing: 0001.jpg"):
        iiif.read_fetch_record(out)


def test_read_fetch_record_refuses_a_malformed_record(tmp_path: Path) -> None:
    out = _complete(tmp_path)
    record = load(out)
    record["images"][0]["file"] = "../0001.jpg"
    (out / iiif.RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed image entry"):
        iiif.read_fetch_record(out)


def test_jpeg_frame_reads_large_grayscale_masters_without_changing_jpeg_dimensions() -> None:
    master = jpeg(9500, 7200, components=1)
    assert iiif.jpeg_frame(master) == (9500, 7200, 1)
    assert iiif.jpeg_frame(jpeg(5000, 6000)) == (5000, 6000, 3)
    with pytest.raises(ValueError):
        jpeg_dimensions(master)
    assert jpeg_dimensions(jpeg(4096, 300)) == (4096, 300)
    with pytest.raises(ValueError, match="start-of-image"):
        iiif.jpeg_frame(b"GIF89a")
    with pytest.raises(ValueError, match="one or three"):
        iiif.jpeg_frame(jpeg(10, 10, components=4))
    with pytest.raises(ValueError, match="ends before"):
        iiif.jpeg_frame(jpeg(10, 10)[:4])


def test_retry_wait_reads_seconds_and_dates() -> None:
    assert iiif._retry_wait("3", 1) == 3.0
    assert iiif._retry_wait("Wed, 21 Oct 2015 07:28:00 GMT", 1) == 0.0
    assert iiif._retry_wait("Wed, 21 Oct 2099 07:28:00 GMT", 1) == iiif.RETRY_AFTER_CAP
    assert iiif._retry_wait("soon", 3) == 8.0
    assert iiif._retry_wait(None, 6) == iiif.RETRY_AFTER_CAP


def test_cli_fetch_iiif_json_and_exit_codes(tmp_path: Path, monkeypatch, capsys) -> None:
    server = two_page_v3()
    monkeypatch.setattr(iiif, "default_opener", lambda: server)
    monkeypatch.setattr(iiif.time, "sleep", lambda _s: None)
    out = tmp_path / "snap"
    assert main(["fetch-iiif", MANIFEST_URL, "--out", str(out), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "complete" and summary["fetched"] == 2

    server.routes[f"{SERVICE}1/full/max/0/default.jpg"] = FakeResponse(404)
    assert main(["fetch-iiif", MANIFEST_URL, "--out", str(tmp_path / "b"), "--delay", "0"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("Incomplete: 1 of 2")
    assert "missing: canvas 1: canvas 1: HTTP 404" in captured.out
    assert "canvas 2 of 2" in captured.err

    assert main(["fetch-iiif", MANIFEST_URL, "--out", str(out), "--json"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error" and error["code"] == "iiif_error"
    with pytest.raises(SystemExit) as usage:
        main(["fetch-iiif", MANIFEST_URL])
    assert usage.value.code == 2


def test_cli_reports_below_native_warning(tmp_path: Path, monkeypatch, capsys) -> None:
    server = FakeServer(
        {
            f"{SERVICE}/info.json": info_v3(SERVICE, 5000, 3000, maxWidth=2000),
            f"{SERVICE}/full/max/0/default.jpg": jpeg(2000, 1200),
        }
    )
    monkeypatch.setattr(iiif, "default_opener", lambda: server)
    monkeypatch.setattr(iiif.time, "sleep", lambda _s: None)
    assert main(["fetch-iiif", f"{SERVICE}/info.json", "--out", str(tmp_path / "s")]) == 0
    out = capsys.readouterr()
    assert "warning: iiif_below_native: canvas 1: the server allows only 2000x1200" in out.out
