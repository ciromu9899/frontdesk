"""Fail the build if anything that looks like a live credential is committed.

A credential written into a distributed file is a credential that leaked. This
check runs on every push so a secret in an example configuration cannot be
forgotten.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Anthropic key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI key": re.compile(rb"sk-[A-Za-z0-9]{32,}"),
    "Slack bot token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "Long provider id": re.compile(rb"\b[AE][A-Za-z0-9_\-]{55,}\b"),
    "JWT": re.compile(rb"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    "Private key block": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

SKIP_DIRS = {
    ".git", "__pycache__", ".build-cache", "data", "node_modules",
    "dist", "build", "windows-build", "windows-dist",
}
SKIP_SUFFIXES = {".pyc", ".jpg", ".jpeg", ".png", ".webp", ".zip", ".ico"}


def tracked_files(root: Path) -> list[Path]:
    """Files git would ship. Falls back to a walk outside a repository.

    Scanning untracked files would flag the developer's own .env, which is
    gitignored and never leaves the machine - a false alarm that teaches people
    to ignore this check.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, check=True, timeout=30,
        ).stdout.decode("utf-8")
        return [root / name for name in listing.split(chr(0)) if name]
    except (OSError, subprocess.SubprocessError):
        return [
            path for path in sorted(root.rglob("*"))
            if path.is_file()
            # .env holds real values by design; .env.example must still be scanned.
            and not (path.name.startswith(".env") and path.name != ".env.example")
        ]


def main() -> int:
    root = Path(__file__).resolve().parent
    findings: list[str] = []

    for path in tracked_files(root):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if set(relative.parts) & SKIP_DIRS or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        # LICENSE contains long base64-ish runs in its appendix; it is not ours to edit.
        if relative.name in {"LICENSE", "tools_check_secrets.py"}:
            continue

        blob = path.read_bytes()
        for label, pattern in PATTERNS.items():
            match = pattern.search(blob)
            if match:
                excerpt = match.group(0)[:12].decode("utf-8", "replace")
                findings.append(f"{relative}: {label} ({excerpt}...)")

    if findings:
        print("Possible credentials found in tracked files:\n")
        for finding in findings:
            print(f"  {finding}")
        print("\nMove real values into .env, which is gitignored, and rotate anything "
              "that was committed.")
        return 1

    print("No credential-shaped strings in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
