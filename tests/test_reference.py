"""Tests for the caller-supplied image fetch.

Every other test monkeypatches reference.fetch, so the module's own guarantees -
https only, no redirects, a streamed byte cap and format confirmed by decoding -
were never actually executed. These run it against a real local HTTP server so
the request path, not a stand-in, is what gets checked.
"""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

import conf
from lib import reference, schemas
from lib.errors import AssetInvalid, ReferenceFetchFailed


def _png(size=(8, 8)):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        status, headers, body = self.routes[self.path]
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    """Serve fixtures over http and treat that origin as authorized.

    The origin allowlist is bypassed deliberately: these tests are about what
    fetch does once a URL is allowed. The allowlist itself is covered in
    test_schemas.py.
    """
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(schemas, "check_authorized_origin", lambda url: None)
    monkeypatch.setattr(reference.schemas, "check_authorized_origin", lambda url: None)
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_a_valid_image_is_fetched_and_measured(server):
    _Handler.routes["/ok.png"] = (200, {"Content-Type": "image/png"}, _png((40, 25)))
    result = reference.fetch(f"{server}/ok.png")
    assert result["format"] == "PNG"
    assert result["mimeType"] == "image/png"
    assert (result["width"], result["height"]) == (40, 25)


def test_a_redirect_is_refused_rather_than_followed(server):
    """Following it would escape the origin allowlist entirely."""
    _Handler.routes["/go"] = (302, {"Location": "https://evil.example/x.png"}, b"")
    with pytest.raises(AssetInvalid, match="redirects"):
        reference.fetch(f"{server}/go")


def test_an_oversized_image_is_refused(server):
    _Handler.routes["/big.png"] = (200, {}, _png((400, 400)))
    with pytest.raises(AssetInvalid, match="byte limit"):
        reference.fetch(f"{server}/big.png", max_bytes=64)


def test_a_declared_image_that_is_not_one_is_refused(server):
    """The Content-Type header is the server's claim, not evidence."""
    _Handler.routes["/fake.png"] = (
        200,
        {"Content-Type": "image/png"},
        b"<html>not an image</html>",
    )
    with pytest.raises(AssetInvalid, match="not a decodable image"):
        reference.fetch(f"{server}/fake.png")


def test_an_unsupported_format_is_refused(server):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="BMP")
    _Handler.routes["/x.bmp"] = (200, {}, buf.getvalue())
    with pytest.raises(AssetInvalid, match="not supported"):
        reference.fetch(f"{server}/x.bmp")


def test_an_error_response_is_reported_as_a_fetch_failure(server):
    _Handler.routes["/missing"] = (404, {}, b"")
    with pytest.raises(ReferenceFetchFailed, match="HTTP 404"):
        reference.fetch(f"{server}/missing")


def test_an_empty_body_is_reported_as_a_fetch_failure(server):
    _Handler.routes["/empty"] = (200, {"Content-Type": "image/png"}, b"")
    with pytest.raises(ReferenceFetchFailed, match="empty"):
        reference.fetch(f"{server}/empty")


def test_an_unreachable_host_is_reported_as_a_fetch_failure(monkeypatch):
    monkeypatch.setattr(schemas, "check_authorized_origin", lambda url: None)
    with pytest.raises(ReferenceFetchFailed, match="Could not fetch"):
        reference.fetch("http://127.0.0.1:1/nothing.png")


def test_the_origin_allowlist_is_enforced_before_any_request_is_made():
    """No allowlist bypass here: an unauthorized origin must never be contacted."""
    with pytest.raises(AssetInvalid):
        reference.fetch("https://evil.example/inspiration.png")


def test_the_cap_defaults_to_configuration_when_no_limit_is_given(server, monkeypatch):
    _Handler.routes["/cap.png"] = (200, {}, _png((40, 40)))
    monkeypatch.setattr(conf, "MAX_REFERENCE_BYTES", 10)
    with pytest.raises(AssetInvalid, match="byte limit"):
        reference.fetch(f"{server}/cap.png")
