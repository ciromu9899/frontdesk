"""Build a deterministic FrontDesk release ZIP, SBOM and hash manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT.parent.parent / "outputs"
ZIP_TIMESTAMP = (2026, 9, 5, 0, 0, 0)
SKIP_PARTS = {
    ".git", "__pycache__", ".pytest_cache", "data", "dist", "build",
    "test-artifacts", "tests", ".build-cache", "windows-build", "windows-dist",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".zip"}
CUSTOMER_EXCLUDED_FILES = {
    "CHANGELOG.md",
    "build_release.py",
    "paypal.py",
    "paypal_checkout.py",
    "verify_paypal.py",
    "sales.py",
    "sales_server.py",
    "sales_admin.py",
    "docs/guide.md",
    "docs/design-notes.md",
    "docs/make_images.py",
    "docs/demo/index.html",
    "docs/product-en.md",
    "docs/product-uk.md",
    "docs/sales-operations.md",
    "docs/images/demo-paypal-refund-declined.svg",
    "docs/images/demo-paypal.svg",
    "docs/images/demo-refund.svg",
    "docs/images/frontdesk-paypal-demo.png",
    "docs/images/paypal-flow.svg",
}


def version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"',
                      (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    if not match:
        raise RuntimeError("project version not found")
    return match.group(1)


def source_files() -> list[Path]:
    files = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if set(relative.parts) & SKIP_PARTS or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if relative.as_posix() in CUSTOMER_EXCLUDED_FILES:
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        files.append(path)
    return files


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dependencies() -> list[dict]:
    result = []
    dependency_file = ROOT / "requirements.lock.txt"
    for raw in dependency_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)(.*)", line)
        if not match:
            continue
        constraint = match.group(2).strip()
        pinned = constraint[2:] if constraint.startswith("==") else constraint
        result.append({"type": "library", "name": match.group(1),
                       "version": pinned or "unspecified",
                       "purl": f"pkg:pypi/{match.group(1).lower()}"})
    return result


def sbom(files: list[Path], release_version: str) -> dict:
    components = dependencies()
    components.extend({
        "type": "file", "name": path.relative_to(ROOT).as_posix(),
        "hashes": [{"alg": "SHA-256", "content": digest(path.read_bytes())}],
    } for path in files)
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {
            "timestamp": "2026-09-05T00:00:00Z",
            "component": {"type": "application", "name": "FrontDesk",
                          "version": release_version,
                          "licenses": [{"license": {"id": "Apache-2.0"}}]},
        },
        "components": components,
    }


def zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def build(output: Path, *, tests_passed: int, allow_unsigned: bool) -> dict:
    if not allow_unsigned:
        raise RuntimeError("A signing certificate is not configured. Use --allow-unsigned only for a release candidate.")
    release_version = version()
    files = source_files()
    output.mkdir(parents=True, exist_ok=True)
    package = output / f"frontdesk-complete-{release_version}-2026-09-05.zip"
    file_hashes = {path.relative_to(ROOT).as_posix(): digest(path.read_bytes()) for path in files}
    bill = sbom(files, release_version)
    internal_manifest = {
        "product": "FrontDesk", "version": release_version,
        "built_at": "2026-09-05T00:00:00Z", "tests_passed": tests_passed,
        "signature_status": "UNSIGNED_RELEASE_CANDIDATE",
        "rollback": "Use rollback.ps1 with the backup created by install.ps1.",
        "files": file_hashes,
    }
    with zipfile.ZipFile(package, "w") as archive:
        for path in files:
            zip_entry(archive, f"frontdesk/{path.relative_to(ROOT).as_posix()}", path.read_bytes())
        zip_entry(archive, "frontdesk/SBOM.cdx.json",
                  json.dumps(bill, indent=2, sort_keys=True).encode())
        zip_entry(archive, "frontdesk/RELEASE-MANIFEST.json",
                  json.dumps(internal_manifest, indent=2, sort_keys=True).encode())
    package_hash = digest(package.read_bytes())
    hash_file = package.with_suffix(package.suffix + ".sha256")
    hash_file.write_text(f"{package_hash}  {package.name}\n", encoding="ascii")
    manifest = {
        "product": "FrontDesk", "version": release_version,
        "package": package.name, "size": package.stat().st_size,
        "sha256": package_hash, "sbom_format": "CycloneDX 1.5",
        "tests_passed": tests_passed,
        "signature_status": "UNSIGNED_RELEASE_CANDIDATE",
        "signing_required_before_general_availability": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_file = package.with_suffix(package.suffix + ".manifest.json")
    manifest_file.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return {"package": str(package), "sha256_file": str(hash_file),
            "manifest": str(manifest_file), **manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the FrontDesk release candidate")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--tests-passed", type=int, required=True)
    parser.add_argument("--allow-unsigned", action="store_true")
    args = parser.parse_args()
    try:
        result = build(args.output.resolve(), tests_passed=args.tests_passed,
                       allow_unsigned=args.allow_unsigned)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
