"""Check every relative Markdown link: the file exists, and the anchor exists.

Documentation that points at a heading which has since been renamed is worse
than no link at all, and nothing about a broken anchor is visible in review.

    python tools_check_links.py

Exits 1 if anything is unresolved.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)


def slug(text: str) -> str:
    """Reproduce how GitHub turns a heading into an anchor."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    out = []
    for char in text.lower():
        if unicodedata.category(char).startswith("P") and char not in "-_":
            continue
        if char.isalnum() or char in "-_" or ord(char) > 0x7F:
            out.append(char)
        elif char in " \t":
            out.append("-")
    return "".join(out)


def anchors(path: Path) -> set[str]:
    return {slug(title) for title in HEADING.findall(path.read_text(encoding="utf-8"))}


def main() -> int:
    problems: list[str] = []
    checked = 0

    for document in sorted(ROOT.rglob("*.md")):
        if "__pycache__" in document.parts:
            continue
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            checked += 1
            file_part, _, anchor = target.partition("#")
            destination = document.parent / file_part if file_part else document
            where = document.relative_to(ROOT)
            if not destination.exists():
                problems.append(f"{where}: no such file -> {target}")
            elif anchor and destination.suffix == ".md" and anchor not in anchors(destination):
                problems.append(f"{where}: no such heading -> {target}")

    if problems:
        print(f"{len(problems)} broken link(s) out of {checked}:")
        for line in problems:
            print("  " + line)
        return 1
    print(f"All {checked} relative links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
