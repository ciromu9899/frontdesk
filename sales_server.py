"""Local-only Shellie sales webhook, claim, receipt and download service."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sales


MAX_BODY = 512_000


class SalesHandler(BaseHTTPRequestHandler):
    server_version = "ShellieSales/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid body size"})
            return None
        return self.rfile.read(length)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        body = self._body()
        if body is None:
            return
        if path == "/paypal-sales":
            self._paypal(body)
        elif path == "/claim":
            self._claim(body)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _paypal(self, body: bytes) -> None:
        try:
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid event"})
            return
        try:
            if not sales.verify_webhook(dict(self.headers), event):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "signature verification failed"})
                return
            result = sales.process_webhook(event)
        except sales.SalesError as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, result)

    def _claim(self, body: bytes) -> None:
        try:
            request = json.loads(body)
            result = sales.claim(str(request.get("order_id", "")),
                                 str(request.get("email", "")))
        except (json.JSONDecodeError, AttributeError, sales.SalesError):
            self._json(HTTPStatus.FORBIDDEN, {"error": "purchase could not be verified"})
            return
        result["download_url"] = "/download?token=" + urllib.parse.quote(result["download_token"])
        result["receipt_url"] = "/receipt?token=" + urllib.parse.quote(result["download_token"])
        self._json(HTTPStatus.OK, result)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path not in {"/download", "/receipt"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        try:
            entitlement = sales.verify_download_token(token)
            if parsed.path == "/receipt":
                self._json(HTTPStatus.OK, sales.receipt(entitlement["order_id"]))
                return
            release = Path(os.environ.get("SHELLIE_RELEASE_FILE", "")).expanduser().resolve()
            if not release.is_file() or release.suffix.lower() != ".zip":
                raise sales.SalesConfigurationError("Release package is not configured.")
            content = release.read_bytes()
        except (sales.SalesError, OSError):
            self._json(HTTPStatus.FORBIDDEN, {"error": "download is unavailable"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{release.name}"')
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Shellie FrontDesk sales service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Refusing a public bind. Put this service behind an authenticated TLS proxy.")
        return 2
    server = ThreadingHTTPServer((args.host, args.port), SalesHandler)
    print(f"Shellie sales service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
