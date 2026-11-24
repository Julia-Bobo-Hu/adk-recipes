"""Open the private Cloud Run workstation in a browser, without the gcloud proxy component.

    python3 scripts/cloudrun_proxy.py --project <project>
    python3 scripts/cloudrun_proxy.py --project <project> --account you@example.com
    python3 scripts/cloudrun_proxy.py --service-url https://<service>.run.app --port 8097

Then open http://127.0.0.1:8097.

Cloud Run answers only requests that carry an identity token, and a browser cannot send one.
`gcloud run services proxy` solves that, but it needs the `cloud-run-proxy` component, which a
package-managed gcloud cannot install and a locked-down laptop may refuse to run. This script
does the same job with the standard library and the gcloud that is already signed in:

- it listens on the loopback interface only, so nothing is exposed to the network;
- it asks gcloud for the caller's identity token (`gcloud auth print-identity-token`) and keeps
  it in memory, never on disk and never in the log;
- it forwards each request to the service with that token and returns the answer unchanged.

The caller needs `run.routes.invoke` on the service (project owners and editors have it;
anyone else needs `roles/run.invoker`).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVICE = "fsi-capital-markets-analyst"
DEFAULT_REGION = "us-central1"
DEFAULT_PORT = 8097
LOOPBACK = "127.0.0.1"

# One live evaluation is a single request of about five and a half minutes.
UPSTREAM_TIMEOUT_SECONDS = 960
# Google identity tokens last one hour; fetch a new one well before that.
TOKEN_MAX_AGE_SECONDS = 45 * 60
GCLOUD_TIMEOUT_SECONDS = 60

# Headers that describe one connection and must not be copied to the next one (RFC 9110, 7.6.1).
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Set by this proxy for the upstream request, so the browser's values are dropped.
REPLACED_REQUEST_HEADERS = frozenset({"host", "authorization", "content-length"})
# Recomputed for the browser because the whole body is read before it is sent on.
REPLACED_RESPONSE_HEADERS = frozenset({"content-length"})
# The workstation answers 200, 400, 404 or 500. These two come only from Cloud Run's front end.
REFUSED_BY_CLOUD_RUN = frozenset({int(HTTPStatus.UNAUTHORIZED), int(HTTPStatus.FORBIDDEN)})


class ProxyError(RuntimeError):
    """Raised when the proxy cannot get what it needs from gcloud."""


def run_gcloud(arguments: list[str], gcloud: str = "gcloud", account: str | None = None) -> str:
    """Run one gcloud command and return its trimmed standard output.

    `account` picks one of the accounts gcloud is signed in with, for this command only.
    Without it gcloud uses its active account, which changes whenever someone runs
    `gcloud auth login` or `gcloud config set account`.

    Raises:
        ProxyError: gcloud is missing, timed out, or returned an error.
    """
    command = [gcloud, *arguments]
    if account:
        command += ["--account", account]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            command,
            capture_output=True,
            text=True,
            timeout=GCLOUD_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
        )
    except FileNotFoundError as exc:
        raise ProxyError(f"`{gcloud}` was not found on PATH. Install the Google Cloud CLI first.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProxyError(f"`{gcloud} {arguments[0]} ...` did not answer within {GCLOUD_TIMEOUT_SECONDS} s.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {completed.returncode}"
        raise ProxyError(f"`{gcloud} {' '.join(arguments[:3])} ...` failed: {reason}")
    return completed.stdout.strip()


def gcloud_identity_token(gcloud: str = "gcloud", account: str | None = None) -> str:
    """Return an identity token for `account`, or for gcloud's active account."""
    token = run_gcloud(["auth", "print-identity-token"], gcloud, account)
    if not token:
        raise ProxyError("gcloud returned no identity token. Run `gcloud auth login` and try again.")
    return token


def resolve_service_url(
    service: str, project: str, region: str, gcloud: str = "gcloud", account: str | None = None
) -> str:
    """Look up the HTTPS address of a Cloud Run service."""
    url = run_gcloud(
        ["run", "services", "describe", service, "--project", project, "--region", region, "--format", "value(status.url)"],
        gcloud,
        account,
    )
    if not url.startswith("https://"):
        raise ProxyError(f"Cloud Run returned no address for service {service!r} in {project}/{region}.")
    return url


class TokenSource:
    """Caches an identity token and fetches a new one when it is old or was refused."""

    def __init__(
        self,
        fetch: Callable[[], str],
        max_age_seconds: float = TOKEN_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._fetched_at = 0.0

    def token(self, force_refresh: bool = False) -> str:
        """Return the cached token, fetching a new one if needed."""
        with self._lock:
            expired = self._clock() - self._fetched_at >= self._max_age_seconds
            if force_refresh or not self._token or expired:
                self._token = self._fetch()
                self._fetched_at = self._clock()
            return self._token


def upstream_request_headers(headers: list[tuple[str, str]], upstream_host: str, token: str) -> dict[str, str]:
    """Build the headers sent to Cloud Run: the browser's own, plus the identity token."""
    forwarded = {
        name: value
        for name, value in headers
        if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() not in REPLACED_REQUEST_HEADERS
    }
    forwarded["Host"] = upstream_host
    forwarded["Authorization"] = f"Bearer {token}"
    return forwarded


def browser_response_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop the headers of the upstream connection; keep everything else as it came."""
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() not in REPLACED_RESPONSE_HEADERS
    ]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand redirects back to the browser instead of following them with the token attached."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:  # noqa: D102 - see class docstring
        return None


def make_handler(service_url: str, tokens: TokenSource, verbose: bool = False) -> type[BaseHTTPRequestHandler]:
    """Create the request handler bound to one Cloud Run service."""
    base = service_url.rstrip("/")
    upstream_host = urllib.parse.urlsplit(base).netloc
    opener = urllib.request.build_opener(_NoRedirect)

    class CloudRunProxyHandler(BaseHTTPRequestHandler):
        """Forwards every request to the service with the caller's identity token."""

        protocol_version = "HTTP/1.1"

        def _send(
            self, status: int, headers: list[tuple[str, str]], body: bytes, declared_length: str | None = None
        ) -> None:
            # send_response_only: the service's own Server and Date headers are passed on, so the
            # base class must not add a second pair.
            self.send_response_only(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", declared_length if declared_length is not None else str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_text(self, status: int, message: str) -> None:
            self._send(status, [("Content-Type", "text/plain; charset=utf-8")], message.encode("utf-8"))

        def _call_upstream(self, body: bytes | None, token: str) -> tuple[int, list[tuple[str, str]], bytes]:
            request = urllib.request.Request(  # noqa: S310 - the address is the configured HTTPS service
                base + self.path,
                data=body,
                method=self.command,
                headers=upstream_request_headers(self.headers.items(), upstream_host, token),
            )
            try:
                with opener.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                    return response.status, response.getheaders(), response.read()
            except urllib.error.HTTPError as error:
                # 3xx, 4xx and 5xx are answers too; pass them on as they are.
                return error.code, list(error.headers.items()), error.read()

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            try:
                status, headers, payload = self._call_upstream(body, tokens.token())
                if status in REFUSED_BY_CLOUD_RUN:
                    # The workstation itself never answers 401 or 403, so either status means
                    # Cloud Run rejected the token before the request reached the app. Nothing
                    # ran, so it is safe to fetch a new token and send the request once more.
                    status, headers, payload = self._call_upstream(body, tokens.token(force_refresh=True))
            except ProxyError as error:
                self._send_text(HTTPStatus.BAD_GATEWAY, f"The proxy could not get an identity token: {error}\n")
                return
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                self._send_text(HTTPStatus.BAD_GATEWAY, f"The proxy could not reach {base}: {error}\n")
                return
            if status in REFUSED_BY_CLOUD_RUN:
                hint = (
                    f"Cloud Run refused this account (HTTP {int(status)}). The signed-in gcloud account needs "
                    "run.routes.invoke on the service: project owner or editor, or roles/run.invoker.\n"
                )
                self._send_text(status, hint)
                return
            if self.command == "HEAD":
                # A HEAD answer has no body; keep the length the service declared for the GET.
                declared = next((value for name, value in headers if name.lower() == "content-length"), None)
                self._send(status, browser_response_headers(headers), b"", declared_length=declared)
            else:
                self._send(status, browser_response_headers(headers), payload)

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _forward  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature of the base class
            if verbose:
                super().log_message(format, *args)

    return CloudRunProxyHandler


def build_server(service_url: str, tokens: TokenSource, port: int = DEFAULT_PORT, verbose: bool = False) -> ThreadingHTTPServer:
    """Create the proxy server, bound to the loopback interface only."""
    server = ThreadingHTTPServer((LOOPBACK, port), make_handler(service_url, tokens, verbose))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local authenticated proxy for the private Cloud Run workstation")
    parser.add_argument("--service-url", help="HTTPS address of the service; looked up with gcloud when omitted")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help=f"Cloud Run service name (default {DEFAULT_SERVICE})")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), help="Google Cloud project of the service")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"Cloud Run region (default {DEFAULT_REGION})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Local port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--gcloud", default="gcloud", help="Path of the gcloud executable")
    parser.add_argument("--account", help="Signed-in gcloud account to use (default: gcloud's active account)")
    parser.add_argument("--verbose", action="store_true", help="Log each forwarded request")
    args = parser.parse_args(argv)

    try:
        if args.service_url:
            service_url = args.service_url
        else:
            if not args.project:
                parser.error("pass --project (or set GOOGLE_CLOUD_PROJECT), or pass --service-url")
            service_url = resolve_service_url(args.service, args.project, args.region, args.gcloud, args.account)
        if not service_url.startswith("https://"):
            parser.error("--service-url must start with https://")
        tokens = TokenSource(lambda: gcloud_identity_token(args.gcloud, args.account))
        tokens.token()  # fail now, with a clear message, if gcloud is not signed in
        server = build_server(service_url, tokens, args.port, args.verbose)
    except ProxyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: cannot listen on {LOOPBACK}:{args.port}: {error}. Pass another --port.", file=sys.stderr)
        return 2

    print(f"Forwarding http://{LOOPBACK}:{args.port} -> {service_url}", flush=True)
    print("Open that local address in a browser. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
