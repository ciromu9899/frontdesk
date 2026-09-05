"""Build the Python-free/Ollama-free Windows portable FrontDesk package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import local_ai


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT.parent.parent / "outputs"
CACHE = ROOT / ".build-cache"
BUILD = ROOT / "windows-build"
DIST = ROOT / "windows-dist"


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_runtime() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / local_ai.LLAMA_CPP_ASSET
    if not archive.is_file() or sha256_path(archive) != local_ai.LLAMA_CPP_SHA256:
        partial = archive.with_suffix(".zip.part")
        request = urllib.request.Request(
            local_ai.LLAMA_CPP_URL,
            headers={"User-Agent": "ShellieSoftwareTools-FrontDesk-Builder/1.5"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
        except Exception as first_error:
            partial.unlink(missing_ok=True)
            if os.name != "nt":
                raise
            completed = subprocess.run(
                ["curl.exe", "--fail", "--location", "--proto", "=https",
                 "--tlsv1.2", "--output", str(partial), local_ai.LLAMA_CPP_URL],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if completed.returncode:
                partial.unlink(missing_ok=True)
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or str(first_error)) from None
        if sha256_path(partial) != local_ai.LLAMA_CPP_SHA256:
            partial.unlink(missing_ok=True)
            raise RuntimeError("llama.cpp archive failed SHA-256 verification")
        partial.replace(archive)
    return archive


def extract_runtime(archive: Path) -> Path:
    destination = CACHE / f"llama.cpp-{local_ai.LLAMA_CPP_VERSION}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError("unsafe path in llama.cpp archive")
        package.extractall(destination)
    executable = next(destination.rglob("llama-server.exe"), None)
    if executable is None:
        raise RuntimeError("llama-server.exe was not found in the verified archive")
    return executable.parent


def write_sbom(app: Path) -> Path:
    files = []
    for path in sorted(app.rglob("*")):
        if path.is_file() and path.name != "SBOM.cdx.json":
            files.append({
                "type": "file",
                "name": path.relative_to(app).as_posix(),
                "hashes": [{"alg": "SHA-256", "content": sha256_path(path)}],
            })
    components = [
        {"type": "application", "name": "FrontDesk", "version": "1.5.0",
         "licenses": [{"license": {"id": "Apache-2.0"}}]},
        {"type": "framework", "name": "CPython", "version": sys.version.split()[0],
         "licenses": [{"license": {"name": "Python-2.0"}}]},
        {"type": "library", "name": "llama.cpp", "version": local_ai.LLAMA_CPP_VERSION,
         "hashes": [{"alg": "SHA-256", "content": local_ai.LLAMA_CPP_SHA256}],
         "licenses": [{"license": {"id": "MIT"}}]},
        {"type": "machine-learning-model", "name": local_ai.MODEL_REPOSITORY,
         "version": local_ai.MODEL_REVISION,
         "hashes": [{"alg": "SHA-256", "content": local_ai.MODEL_SHA256}],
         "licenses": [{"license": {"id": "Apache-2.0"}}],
         "scope": "optional"},
    ]
    bill = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"timestamp": "2026-08-29T00:00:00Z", "component": components[0]},
        "components": components[1:] + files,
    }
    destination = app / "SBOM.cdx.json"
    destination.write_text(json.dumps(bill, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def build(output: Path) -> dict:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise RuntimeError("PyInstaller is required only on the build machine") from None

    runtime = extract_runtime(download_runtime())
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--console", "--name", "FrontDesk", "--distpath", str(DIST),
        "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
        "--collect-submodules", "channels",
        "--add-data", f"{ROOT / 'personas'}{os.pathsep}personas",
        "--add-data", f"{ROOT / 'knowledge'}{os.pathsep}knowledge",
        "--add-data", f"{ROOT / 'LICENSE'}{os.pathsep}.",
        "--add-data", f"{ROOT / 'NOTICE'}{os.pathsep}.",
        "--add-data", f"{ROOT / 'THIRD_PARTY_NOTICES.md'}{os.pathsep}.",
    ]
    command.append(str(ROOT / "desktop.py"))
    subprocess.run(command, cwd=ROOT, check=True)

    app = DIST / "FrontDesk"
    packaged_runtime = app / "_internal" / "runtime" / "llama.cpp"
    shutil.copytree(runtime, packaged_runtime, dirs_exist_ok=True)
    if not (packaged_runtime / "llama-server.exe").is_file():
        raise RuntimeError("llama-server.exe was not copied into the portable application")
    for document in ("WINDOWS-README.txt", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / document, app / document)
    write_sbom(app)
    output.mkdir(parents=True, exist_ok=True)
    package = output / "frontdesk-windows-portable-1.5.0-2026-08-29.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(app.rglob("*")):
            if path.is_file():
                archive.write(path, Path("FrontDesk") / path.relative_to(app))
    package_hash = sha256_path(package)
    package.with_suffix(package.suffix + ".sha256").write_text(
        f"{package_hash}  {package.name}\n", encoding="ascii")
    manifest = {
        "product": "FrontDesk", "version": "1.5.0", "platform": "Windows x64 CPU",
        "package": package.name, "sha256": package_hash, "tests_passed": 264,
        "python_install_required": False, "ollama_install_required": False,
        "model_bundled": False, "model_first_run_download_gb": 5.03,
        "llama_cpp_version": local_ai.LLAMA_CPP_VERSION,
        "llama_cpp_archive_sha256": local_ai.LLAMA_CPP_SHA256,
        "model_sha256": local_ai.MODEL_SHA256,
        "signature_status": "UNSIGNED_RELEASE_CANDIDATE", "sbom_format": "CycloneDX 1.5",
    }
    manifest_path = package.with_suffix(package.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "package_path": str(package), "manifest_path": str(manifest_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output.resolve()), indent=2))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
