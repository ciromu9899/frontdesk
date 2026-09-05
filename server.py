"""Run FrontDesk as an OS-neutral, headless web service.

The customer chat and the shared inbox are served from one process.  This is
the entry point used by the container edition; it also runs directly anywhere
Python 3.11 or newer is available.
"""

from __future__ import annotations

import argparse
import ipaddress
import signal
import threading
from http.server import ThreadingHTTPServer

import admin
import auth
import config
import webchat


class FrontDeskHTTPServer(ThreadingHTTPServer):
    """Threaded server whose request threads cannot block clean shutdown."""

    daemon_threads = True
    allow_reuse_address = True


def bind_host(value: str) -> str:
    """Accept localhost or an IPv4 address without resolving arbitrary names."""
    if value == "localhost":
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "host must be localhost or an IPv4 address such as 127.0.0.1 or 0.0.0.0"
        ) from None
    if address.version != 4:
        raise argparse.ArgumentTypeError("the OS-neutral server currently requires IPv4")
    return value


def create_servers(
    host: str, web_port: int, admin_port: int, secret: str
) -> tuple[FrontDeskHTTPServer, FrontDeskHTTPServer]:
    """Create both listeners, closing the first if the second cannot bind."""
    if web_port == admin_port and web_port != 0:
        raise ValueError("web and admin ports must be different")
    web_server = FrontDeskHTTPServer((host, web_port), webchat.WebChatHandler)
    try:
        admin_server = FrontDeskHTTPServer((host, admin_port), admin.AdminHandler)
    except Exception:
        web_server.server_close()
        raise
    admin_server.auth_secret = secret  # type: ignore[attr-defined]
    return web_server, admin_server


def run(host: str, web_port: int, admin_port: int) -> int:
    config.load_dotenv()
    try:
        secret = auth.signing_secret()
    except auth.AuthError as exc:
        print(f"FrontDesk cannot start: {exc}")
        return 2

    try:
        servers = create_servers(host, web_port, admin_port, secret)
    except (OSError, ValueError) as exc:
        print(f"FrontDesk cannot bind its web ports: {exc}")
        return 2

    stopping = threading.Event()
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in servers
    ]

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        current = getattr(signal, signal_name, None)
        if current is not None:
            signal.signal(current, request_stop)

    for thread in threads:
        thread.start()

    display_host = "localhost" if host == "0.0.0.0" else host
    print("FrontDesk OS-neutral server is ready.")
    print(f"Customer chat: http://{display_host}:{web_port}/")
    print(f"Shared inbox:  http://{display_host}:{admin_port}/login")
    if host == "0.0.0.0":
        print("Network listening is enabled; use a trusted HTTPS reverse proxy in production.")

    result = 0
    try:
        while not stopping.wait(0.5):
            if not all(thread.is_alive() for thread in threads):
                result = 1
                break
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run FrontDesk customer chat and shared inbox on any supported OS"
    )
    parser.add_argument("--host", type=bind_host, default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8766)
    parser.add_argument("--admin-port", type=int, default=8765)
    args = parser.parse_args()
    for name, port in (("web", args.web_port), ("admin", args.admin_port)):
        if not 1 <= port <= 65535:
            parser.error(f"{name} port must be between 1 and 65535")
    return run(args.host, args.web_port, args.admin_port)


if __name__ == "__main__":
    raise SystemExit(main())
