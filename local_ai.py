"""Pinned local-AI runtime and model management for the Windows desktop build."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, BinaryIO


LLAMA_CPP_VERSION = "b10516"
LLAMA_CPP_ASSET = "llama-b10516-bin-win-cpu-x64.zip"
LLAMA_CPP_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b10516/"
    + LLAMA_CPP_ASSET
)
LLAMA_CPP_SHA256 = "fbbbc55e0eb2e1b07f9dcb9488616c98ed47d9003b90e15e7c8c7812c4307cd3"

MODEL_NAME = "Qwen3-8B-Q4_K_M.gguf"
MODEL_REPOSITORY = "Qwen/Qwen3-8B-GGUF"
MODEL_REVISION = "1d54a16a18cba0d8fbad4a16db801decc729e099"
MODEL_URL = (
    f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/"
    f"{MODEL_NAME}?download=true"
)
MODEL_SHA256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
MODEL_DISPLAY_SIZE = "5.03 GB"
MODEL_ALIAS = "frontdesk-local"
DEFAULT_PORT = 11435


class LocalAIError(RuntimeError):
    """A local-runtime problem suitable for display to the operator."""


def resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def application_data_dir() -> Path:
    parent = os.environ.get("LOCALAPPDATA", "").strip()
    if parent:
        return Path(parent) / "ShellieSoftwareTools" / "FrontDesk"
    return Path.home() / ".frontdesk"


def server_executable() -> Path:
    override = os.environ.get("FRONTDESK_LLAMA_SERVER", "").strip()
    if override:
        return Path(override)
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    return resource_root() / "runtime" / "llama.cpp" / name


def default_model_path() -> Path:
    configured = os.environ.get("FRONTDESK_LOCAL_MODEL", "").strip()
    return Path(configured) if configured else application_data_dir() / "models" / MODEL_NAME


def sha256_path(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_ready(path: Path | None = None) -> bool:
    path = path or default_model_path()
    if not path.is_file():
        return False
    marker = path.with_suffix(path.suffix + ".verified.json")
    stat = path.stat()
    try:
        saved = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    if (saved.get("sha256") == MODEL_SHA256 and saved.get("size") == stat.st_size and
            saved.get("mtime_ns") == stat.st_mtime_ns):
        return True
    if sha256_path(path) != MODEL_SHA256:
        return False
    marker.write_text(json.dumps({"sha256": MODEL_SHA256, "size": stat.st_size,
                                  "mtime_ns": stat.st_mtime_ns}, indent=2) + "\n",
                      encoding="utf-8")
    return True


def _open_download(url: str):
    request = urllib.request.Request(
        url, headers={"User-Agent": "ShellieSoftwareTools-FrontDesk/1.5"})
    return urllib.request.urlopen(request, timeout=60)  # nosec B310 - fixed HTTPS URL


def download_model(
    destination: Path | None = None, *, opener: Callable[[str], BinaryIO] = _open_download,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    destination = destination or default_model_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(MODEL_URL) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0") or 0)
            while True:
                chunk = response.read(8 * 1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress(received, total)
    except (OSError, urllib.error.URLError) as exc:
        partial.unlink(missing_ok=True)
        if opener is _open_download and os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
            command = ["curl.exe", "--fail", "--location", "--proto", "=https",
                       "--tlsv1.2", "--output", str(partial), MODEL_URL]
            completed = subprocess.run(command, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, creationflags=flags)
            if completed.returncode:
                partial.unlink(missing_ok=True)
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise LocalAIError(f"The model download failed: {detail or exc}") from None
            download_hash = sha256_path(partial)
        else:
            raise LocalAIError(f"The model download failed: {exc}") from None
    else:
        download_hash = digest.hexdigest()
    if download_hash != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise LocalAIError("The downloaded model failed SHA-256 verification.")
    partial.replace(destination)
    stat = destination.stat()
    destination.with_suffix(destination.suffix + ".verified.json").write_text(
        json.dumps({"sha256": MODEL_SHA256, "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns}, indent=2) + "\n", encoding="utf-8")
    return destination


def server_command(executable: Path, model: Path, port: int = DEFAULT_PORT) -> list[str]:
    return [str(executable), "-m", str(model), "--host", "127.0.0.1",
            "--port", str(port), "--ctx-size", "8192", "--parallel", "2",
            "--jinja", "--alias", MODEL_ALIAS, "--reasoning", "off",
            "--reasoning-budget", "0", "--cors-origins", "localhost",
            "--no-cors-credentials", "--no-webui"]


def start_server(executable: Path, model: Path, port: int = DEFAULT_PORT) -> subprocess.Popen:
    if not executable.is_file():
        raise LocalAIError("The bundled llama.cpp runtime is missing.")
    if not model_ready(model):
        raise LocalAIError("The local model is missing or failed SHA-256 verification.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(server_command(executable, model, port),
                            cwd=executable.parent, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags)


def wait_until_ready(process: subprocess.Popen, port: int = DEFAULT_PORT,
                     timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    return False


def stop_server(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
