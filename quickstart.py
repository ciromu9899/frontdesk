"""Guided local launcher for a salon FrontDesk installation.

The launcher keeps generated authentication material in the process environment.
It does not create or update .env files, and the existing individual server
commands remain available for advanced deployments.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import auth


ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "qwen3:8b"


def _choose(prompt: str, choices: tuple[tuple[str, str], ...]) -> str:
    print(f"\n{prompt}")
    for index, (label, _) in enumerate(choices, 1):
        suffix = " (recommended)" if index == 1 else ""
        print(f"  {index}. {label}{suffix}")
    while True:
        answer = input("Choose [1]: ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][1]
        print(f"Enter a number from 1 to {len(choices)}.")


def guided_settings() -> tuple[str, str, str]:
    region = _choose("Where is this FrontDesk installation used?", (
        ("United States", "us"), ("United Kingdom", "uk")))
    language_choices = (("English", "en"),)
    if region == "us":
        language_choices += (("US Spanish", "es"),)
    language = _choose("What language should customer chat open in?", language_choices)
    provider = _choose("Which answer engine should FrontDesk use?", (
        ("Ollama - real local AI", "ollama"), ("Echo - safe demonstration", "echo")))
    return region, language, provider


def build_runtime_environment(
    base: dict[str, str], *, provider: str, region: str, tenant: str,
    subject: str = "owner@frontdesk.local",
) -> tuple[dict[str, str], str]:
    secret = secrets.token_urlsafe(32)
    token = auth.issue_token(auth.Principal(subject, ("admin",), tenant), secret,
                             expires_in=12 * 60 * 60)
    environment = dict(base)
    environment.update({
        "FRONTDESK_AUTH_SECRET": secret,
        "FRONTDESK_ACCESS_TOKEN": token,
        "FRONTDESK_INDUSTRY": "salon",
        "FRONTDESK_WEB_PERSONA": "salon",
        "FRONTDESK_WEB_PROVIDER": provider,
        "FRONTDESK_WEB_TENANT_ID": tenant,
        "FRONTDESK_REGION": region,
        "FRONTDESK_MULTI_TENANT_KNOWLEDGE": "1",
    })
    return environment, token


def ollama_model_available(model: str = DEFAULT_MODEL, timeout: float = 2.0) -> bool:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    expected = model.removesuffix(":latest")
    names = {
        str(item.get("name", "")).removesuffix(":latest")
        for item in payload.get("models", []) if isinstance(item, dict)
    }
    return expected in names


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def wait_until_ready(process: subprocess.Popen[bytes], host: str, port: int,
                     timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def copy_to_clipboard(value: str) -> bool:
    try:
        import tkinter
    except ImportError:
        return False
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update()
        root.destroy()
        return True
    except tkinter.TclError:
        return False


def stop_processes(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start salon FrontDesk web chat and administration together")
    parser.add_argument("--guided", action="store_true", help="ask three setup questions")
    parser.add_argument("--provider", choices=("ollama", "echo"), default="ollama")
    parser.add_argument("--region", choices=("us", "uk"), default="us")
    parser.add_argument("--lang", choices=("en", "es"), default="en")
    parser.add_argument("--tenant", default="salon:default")
    parser.add_argument("--web-port", type=int, default=8766)
    parser.add_argument("--admin-port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-clipboard", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hide-token", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    region, language, provider = args.region, args.lang, args.provider
    if args.guided:
        region, language, provider = guided_settings()
    if region == "uk" and language == "es":
        parser.error("US Spanish is available only with --region us")

    if provider == "ollama" and not ollama_model_available():
        print("\nOllama with qwen3:8b is not ready.")
        print("Start Ollama, then run: ollama pull qwen3:8b")
        print("For a no-AI demonstration, run: python quickstart.py --provider echo")
        return 2

    host = "127.0.0.1"
    occupied = [port for port in (args.web_port, args.admin_port)
                if not port_available(host, port)]
    if occupied:
        print(f"Cannot start: port already in use: {', '.join(map(str, occupied))}")
        return 2

    environment, token = build_runtime_environment(
        os.environ, provider=provider, region=region, tenant=args.tenant)
    commands = [
        [sys.executable, str(ROOT / "webchat.py"), "--port", str(args.web_port)],
        [sys.executable, str(ROOT / "admin.py"), "--port", str(args.admin_port)],
    ]
    processes = [subprocess.Popen(command, cwd=ROOT, env=environment) for command in commands]
    try:
        web_ready = wait_until_ready(processes[0], host, args.web_port)
        admin_ready = wait_until_ready(processes[1], host, args.admin_port)
        if not web_ready or not admin_ready:
            print("FrontDesk could not finish starting. Review the messages above.")
            return 1

        web_url = f"http://{host}:{args.web_port}/?lang={language}"
        admin_url = f"http://{host}:{args.admin_port}/login"
        copied = False if args.no_clipboard else copy_to_clipboard(token)
        print("\nFrontDesk is ready.")
        print(f"Customer chat: {web_url}")
        print(f"Shared inbox:  {admin_url}")
        if copied:
            print("The administrator token is copied. Press Ctrl+V on the sign-in page.")
        elif args.hide_token:
            print("Administrator token output is hidden for this startup check.")
        else:
            print("Administrator token (keep private and paste on the sign-in page):")
            print(token)
        print("Press Ctrl+C here to stop FrontDesk.")
        if not args.no_browser:
            webbrowser.open(web_url)
            webbrowser.open(admin_url)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        print("\nStopping FrontDesk...")
        return 0
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
