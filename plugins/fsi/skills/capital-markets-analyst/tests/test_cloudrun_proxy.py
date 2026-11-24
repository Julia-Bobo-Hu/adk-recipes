"""Tests for the local Cloud Run proxy.

A small HTTP server on the loopback interface stands in for Cloud Run, and a counter stands in
for gcloud, so nothing here needs a network, a Google account or the gcloud CLI. Whether
Cloud Run accepts a real gcloud identity token is outside what these tests can show.
"""

from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import Any
import urllib.error
import urllib.request

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import cloudrun_proxy  # noqa: E402


class _FakeCloudRun:
    """Records every request and answers the way the test asks it to."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.accepted_tokens: set[str] | None = None  # None: accept any token
        self.app_status = 200
        self.app_body = b'{"ok": true}'
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                fake.requests.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body, "token": token}
                )
                if fake.accepted_tokens is not None and token not in fake.accepted_tokens:
                    status, payload, content_type = 403, b"<html>Your client does not have permission</html>", "text/html"
                else:
                    status, payload, content_type = fake.app_status, fake.app_body, "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("X-From-App", "yes")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = _answer  # noqa: N815

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _CountingTokens:
    """Stands in for gcloud: returns token-1, token-2, ... and counts the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"token-{self.calls}"


@pytest.fixture
def upstream() -> Iterator[_FakeCloudRun]:
    fake = _FakeCloudRun()
    yield fake
    fake.close()


@pytest.fixture
def proxy(upstream: _FakeCloudRun) -> Iterator[tuple[str, _CountingTokens]]:
    fetch = _CountingTokens()
    server = cloudrun_proxy.build_server(upstream.url, cloudrun_proxy.TokenSource(fetch), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", fetch
    server.shutdown()
    server.server_close()


def _call(url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {})  # noqa: S310 - loopback test server
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, dict(response.getheaders()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def test_proxy_adds_the_identity_token_and_forwards_the_request(upstream: _FakeCloudRun, proxy: tuple[str, _CountingTokens]) -> None:
    """The service sees the caller's token and the browser's request; the browser sees the service's answer."""
    base, fetch = proxy
    payload = json.dumps({"book_id": "next27_flagship", "rate_bps": "50"}).encode()

    status, headers, body = _call(
        f"{base}/api/analyze?x=1",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer from-the-browser", "Connection": "keep-alive"},
    )

    assert (status, body) == (200, b'{"ok": true}')
    assert headers["X-From-App"] == "yes" and headers["Content-Type"] == "application/json"
    seen = upstream.requests[-1]
    assert (seen["method"], seen["path"], seen["body"]) == ("POST", "/api/analyze?x=1", payload)
    assert seen["token"] == "token-1"  # the browser's own Authorization header is replaced
    assert seen["headers"]["Host"] == upstream.url.removeprefix("http://")
    assert seen["headers"]["Content-Type"] == "application/json"

    # A second request reuses the token: gcloud is asked once.
    assert _call(f"{base}/")[0] == 200
    assert upstream.requests[-1]["token"] == "token-1" and fetch.calls == 1


def test_proxy_fetches_a_new_token_once_when_cloud_run_refuses(upstream: _FakeCloudRun, proxy: tuple[str, _CountingTokens]) -> None:
    """An expired token is replaced without the user noticing."""
    base, fetch = proxy
    upstream.accepted_tokens = {"token-2"}

    status, _, body = _call(f"{base}/api/bootstrap")

    assert (status, body) == (200, b'{"ok": true}')
    assert [request["token"] for request in upstream.requests] == ["token-1", "token-2"]
    assert fetch.calls == 2


def test_proxy_explains_a_refusal_that_persists(upstream: _FakeCloudRun, proxy: tuple[str, _CountingTokens]) -> None:
    """An account without permission gets one retry and then a plain-language reason."""
    base, fetch = proxy
    upstream.accepted_tokens = set()

    status, headers, body = _call(f"{base}/")

    assert status == 403
    assert headers["Content-Type"].startswith("text/plain")
    assert "run.routes.invoke" in body.decode() and "<html>" not in body.decode()
    assert len(upstream.requests) == 2 and fetch.calls == 2
    assert all("token-" not in line for line in body.decode().splitlines())  # the token is never shown


def test_proxy_passes_application_errors_through_without_a_retry(upstream: _FakeCloudRun, proxy: tuple[str, _CountingTokens]) -> None:
    """A 400 from the workstation is the workstation's answer: sent once, returned unchanged."""
    base, _ = proxy
    upstream.app_status, upstream.app_body = 400, b'{"error": "ValueError: bad shock"}'

    status, _, body = _call(f"{base}/api/analyze", data=b"{}", headers={"Content-Type": "application/json"})

    assert (status, body) == (400, b'{"error": "ValueError: bad shock"}')
    assert len(upstream.requests) == 1


def test_proxy_sends_each_header_once_and_keeps_the_declared_length_on_head(
    upstream: _FakeCloudRun, proxy: tuple[str, _CountingTokens]
) -> None:
    """The service's Server and Date headers are passed on, not doubled; HEAD keeps the GET length."""
    base, _ = proxy

    with urllib.request.urlopen(f"{base}/", timeout=10) as response:  # noqa: S310 - loopback test server
        names = [name.lower() for name, _ in response.getheaders()]
        assert response.read() == upstream.app_body
    assert names.count("server") == 1 and names.count("date") == 1 and names.count("content-length") == 1

    head = urllib.request.Request(f"{base}/", method="HEAD")  # noqa: S310
    with urllib.request.urlopen(head, timeout=10) as response:  # noqa: S310
        assert response.read() == b""
        assert response.headers["Content-Length"] == str(len(upstream.app_body))
    assert upstream.requests[-1]["method"] == "HEAD"


def test_proxy_reports_an_unreachable_service() -> None:
    """A service that cannot be reached becomes a 502 with the reason, not a dropped connection."""
    server = cloudrun_proxy.build_server("http://127.0.0.1:1", cloudrun_proxy.TokenSource(lambda: "token"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _, body = _call(f"http://127.0.0.1:{server.server_address[1]}/")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 502 and "could not reach" in body.decode()


def test_proxy_listens_on_the_loopback_interface_only(upstream: _FakeCloudRun) -> None:
    server = cloudrun_proxy.build_server(upstream.url, cloudrun_proxy.TokenSource(lambda: "token"), port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_token_source_reuses_a_token_until_it_is_old() -> None:
    now = [0.0]
    fetch = _CountingTokens()
    tokens = cloudrun_proxy.TokenSource(fetch, max_age_seconds=100, clock=lambda: now[0])

    assert [tokens.token(), tokens.token()] == ["token-1", "token-1"]
    now[0] = 99.0
    assert tokens.token() == "token-1"
    now[0] = 100.0
    assert tokens.token() == "token-2"
    assert tokens.token(force_refresh=True) == "token-3"
    assert fetch.calls == 3


def test_header_rules_drop_connection_headers_and_keep_the_rest() -> None:
    sent = cloudrun_proxy.upstream_request_headers(
        [("Host", "127.0.0.1:8097"), ("Cookie", "a=b"), ("Connection", "keep-alive"), ("Authorization", "Bearer x"), ("Content-Length", "2")],
        "service.example",
        "the-token",
    )
    assert sent == {"Cookie": "a=b", "Host": "service.example", "Authorization": "Bearer the-token"}

    returned = cloudrun_proxy.browser_response_headers(
        [("Content-Type", "text/html"), ("Transfer-Encoding", "chunked"), ("Content-Length", "10"), ("Set-Cookie", "a=b")]
    )
    assert returned == [("Content-Type", "text/html"), ("Set-Cookie", "a=b")]


def test_the_account_option_is_passed_to_gcloud(tmp_path: Path) -> None:
    """With several signed-in accounts, the proxy can name the one to use instead of the active one."""
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text('#!/bin/sh\necho "$@"\n', encoding="utf-8")
    fake_gcloud.chmod(0o755)

    assert cloudrun_proxy.gcloud_identity_token(str(fake_gcloud)) == "auth print-identity-token"
    assert (
        cloudrun_proxy.gcloud_identity_token(str(fake_gcloud), "analyst@example.com")
        == "auth print-identity-token --account analyst@example.com"
    )


def test_a_missing_gcloud_is_reported_in_plain_words() -> None:
    with pytest.raises(cloudrun_proxy.ProxyError, match="was not found on PATH"):
        cloudrun_proxy.run_gcloud(["auth", "print-identity-token"], gcloud="/nonexistent/bin/gcloud")
