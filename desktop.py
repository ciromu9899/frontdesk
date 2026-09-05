"""Single-process desktop entrypoint used by the self-contained Windows build."""

from __future__ import annotations

import argparse
import os
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

import admin
import auth
import local_ai
import quickstart
import webchat


def _download_progress(received: int, total: int) -> None:
    if total:
        percent = min(100, received * 100 // total)
        print(f"\rDownloading local AI model: {percent:3d}%", end="", flush=True)
    else:
        print(f"\rDownloading local AI model: {received // (1024 * 1024)} MB", end="", flush=True)


def ensure_local_model(non_interactive: bool = False):
    model = local_ai.default_model_path()
    if local_ai.model_ready(model):
        return model
    if non_interactive:
        raise local_ai.LocalAIError("The verified local AI model has not been downloaded.")
    print(f"\nFrontDesk needs the {local_ai.MODEL_DISPLAY_SIZE} local AI model once.")
    print("Source: Qwen/Qwen3-8B-GGUF (Apache-2.0). SHA-256 will be verified.")
    if input("Download now? [Y/n]: ").strip().lower() not in {"", "y", "yes"}:
        raise local_ai.LocalAIError("Model download was cancelled.")
    result = local_ai.download_model(model, progress=_download_progress)
    print("\nModel download and SHA-256 verification completed.")
    return result


def run_servers(environment: dict[str, str], token: str, language: str,
                web_port: int, admin_port: int, *, open_browser: bool = True) -> int:
    os.environ.update(environment)
    web_server = ThreadingHTTPServer(("127.0.0.1", web_port), webchat.WebChatHandler)
    admin_server = ThreadingHTTPServer(("127.0.0.1", admin_port), admin.AdminHandler)
    admin_server.auth_secret = environment["FRONTDESK_AUTH_SECRET"]  # type: ignore[attr-defined]
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in (web_server, admin_server)]
    for thread in threads:
        thread.start()
    web_url = f"http://127.0.0.1:{web_port}/?lang={language}"
    admin_url = f"http://127.0.0.1:{admin_port}/login"
    try:
        copied = quickstart.copy_to_clipboard(token)
        print("\nFrontDesk is ready.")
        print(f"Customer chat: {web_url}")
        print(f"Shared inbox:  {admin_url}")
        if copied:
            print("The administrator token is copied. Press Ctrl+V on the sign-in page.")
        else:
            print("Administrator token (keep private):")
            print(token)
        print("Press Ctrl+C here to stop FrontDesk.")
        if open_browser:
            webbrowser.open(web_url)
            webbrowser.open(admin_url)
        while all(thread.is_alive() for thread in threads):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        print("\nStopping FrontDesk...")
        return 0
    finally:
        for server in (web_server, admin_server):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the self-contained FrontDesk desktop app")
    parser.add_argument("--guided", action="store_true")
    parser.add_argument("--provider", choices=("llamacpp", "echo"), default="llamacpp")
    parser.add_argument("--region", choices=("us", "uk"), default="us")
    parser.add_argument("--lang", choices=("en", "es"), default="en")
    parser.add_argument("--tenant", default="salon:default")
    parser.add_argument("--web-port", type=int, default=8766)
    parser.add_argument("--admin-port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    region, language, provider = args.region, args.lang, args.provider
    if args.guided:
        region = quickstart._choose("Where is this FrontDesk installation used?", (
            ("United States", "us"), ("United Kingdom", "uk")))
        languages = (("English", "en"), ("US Spanish", "es")) if region == "us" else (("English", "en"),)
        language = quickstart._choose("What language should customer chat open in?", languages)
        provider = quickstart._choose("Which mode should FrontDesk use?", (
            ("Local AI - recommended", "llamacpp"), ("Safe demonstration", "echo")))

    if region == "uk" and language == "es":
        parser.error("US Spanish is available only with --region us")
    ports = [args.web_port, args.admin_port]
    if provider == "llamacpp":
        ports.append(local_ai.DEFAULT_PORT)
    occupied = [port for port in ports
                if not quickstart.port_available("127.0.0.1", port)]
    if occupied:
        print(f"Cannot start: port already in use: {', '.join(map(str, occupied))}")
        return 2

    local_process = None
    try:
        if provider == "llamacpp":
            model = ensure_local_model(args.non_interactive)
            local_process = local_ai.start_server(local_ai.server_executable(), model)
            print("Starting local AI...")
            if not local_ai.wait_until_ready(local_process):
                raise local_ai.LocalAIError("The local AI runtime did not become ready.")
        environment, token = quickstart.build_runtime_environment(
            os.environ, provider=provider, region=region, tenant=args.tenant)
        environment["LLAMACPP_BASE_URL"] = f"http://127.0.0.1:{local_ai.DEFAULT_PORT}/v1"
        return run_servers(environment, token, language, args.web_port, args.admin_port,
                           open_browser=not args.no_browser)
    except local_ai.LocalAIError as exc:
        print(f"FrontDesk could not start: {exc}")
        return 2
    finally:
        local_ai.stop_server(local_process)


if __name__ == "__main__":
    raise SystemExit(main())
