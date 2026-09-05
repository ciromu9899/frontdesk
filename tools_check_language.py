"""Keep accidental Japanese out of the shipped English and Spanish surfaces.

Everything the model reads - personas, tool descriptions - and everything a
customer reads is English, because mixing languages in the prompt makes the
reply language drift. The author writes Japanese, so this is easy to undo by
accident; this check makes it visible instead.

    python tools_check_language.py

Generated build caches and packaged runtimes are excluded. Exits 1 if Japanese
appears in source or customer-facing files.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Hiragana, katakana and CJK ideographs. Written as escapes so that this file is
# itself ASCII and does not trip the check it implements.
JAPANESE = re.compile("[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]")

BINARY = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".zip", ".pdf", ".woff2"}
SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".build-cache",
    "build", "dist", "windows-build", "windows-dist",
}


def main() -> int:
    offenders: list[str] = []
    scanned = 0

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or set(path.parts) & SKIP_DIRS:
            continue
        if path.suffix.lower() in BINARY:
            continue
        name = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        hits = [
            f"{name}:{number}"
            for number, line in enumerate(text.splitlines(), 1)
            if JAPANESE.search(line)
        ]
        offenders.extend(hits[:5])

    if offenders:
        print("Japanese found in the tree:")
        for line in offenders:
            print("  " + line)
        print("\nTranslate source and customer-facing content before release.")
        return 1
    print(f"English/Spanish surface confirmed across {scanned} files; no Japanese found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
