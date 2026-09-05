"""Tenant-scoped local hybrid RAG for text, PDF, and Office documents."""

from __future__ import annotations

import html
import hashlib
import json
import math
import os
import re
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree as ET
from config import DATA_DIR


ROOT = Path(__file__).resolve().parent
PACKAGED_KNOWLEDGE_DIR = ROOT / "knowledge"
PERSISTENT_KNOWLEDGE = DATA_DIR != ROOT / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge" if PERSISTENT_KNOWLEDGE else PACKAGED_KNOWLEDGE_DIR
INDEX_PATH = DATA_DIR / "rag-index.json"
ALLOWED_SUFFIXES = {".md", ".txt", ".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx"}
INDEX_VERSION = 3
MAX_OFFICE_XML_ENTRY = 10 * 1024 * 1024
MAX_OFFICE_XML_TOTAL = 50 * 1024 * 1024
WORD_RE = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)
CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
TAG_RE = re.compile(r"<[^>]+>")


def _tenant_slug(tenant_id: str) -> str:
    return hashlib.sha256((tenant_id or "default").encode("utf-8")).hexdigest()[:20]


def tenant_paths(tenant_id: str) -> tuple[Path, Path]:
    if PERSISTENT_KNOWLEDGE and not KNOWLEDGE_DIR.exists():
        KNOWLEDGE_DIR.parent.mkdir(parents=True, exist_ok=True)
        if PACKAGED_KNOWLEDGE_DIR.exists():
            shutil.copytree(PACKAGED_KNOWLEDGE_DIR, KNOWLEDGE_DIR)
        else:
            KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    if tenant_id == "default":
        return KNOWLEDGE_DIR, INDEX_PATH
    # Only the CLI runs as `default`. Web chat, Slack, Teams, Meta and email each
    # namespace their tenant id, so an exact match here left the knowledge a buyer
    # had just put in knowledge/ unreadable from every surface a customer uses. A
    # tenant with its own documents still reads only those; a tenant with none
    # reads the shared set instead of an empty index.
    slug = _tenant_slug(tenant_id)
    tenant_directory = KNOWLEDGE_DIR / "tenants" / slug
    tenant_index = DATA_DIR / "rag" / f"{slug}.json"
    if multi_tenant_knowledge() or _has_documents(tenant_directory):
        return tenant_directory, tenant_index
    return KNOWLEDGE_DIR, INDEX_PATH


def multi_tenant_knowledge() -> bool:
    """Keep every tenant to its own documents, even when it has none."""
    value = os.environ.get("FRONTDESK_MULTI_TENANT_KNOWLEDGE", "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _has_documents(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(path.suffix.lower() in ALLOWED_SUFFIXES
               for path in directory.glob("**/*") if path.is_file())


@dataclass(frozen=True)
class SearchHit:
    source: str
    chunk: int
    score: float
    text: str


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for word in WORD_RE.findall(text.casefold()):
        cursor = 0
        for match in CJK_RUN_RE.finditer(word):
            if match.start() > cursor:
                tokens.append(word[cursor:match.start()])
            run = match.group(0)
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
            cursor = match.end()
        if cursor < len(word):
            tokens.append(word[cursor:])
        elif cursor == 0:
            tokens.append(word)
    return [token for token in tokens if token]


def _plain_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix in {".docx", ".pptx", ".xlsx"}:
        prefixes = {".docx": ("word/document.xml",),
                    ".pptx": ("ppt/slides/",),
                    ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/")}[suffix]
        parts: list[str] = []
        selected_size = 0
        with zipfile.ZipFile(path) as archive:
            for entry in sorted(archive.infolist(), key=lambda item: item.filename):
                name = entry.filename
                if not any(name == prefix or name.startswith(prefix) for prefix in prefixes):
                    continue
                if entry.flag_bits & 1 or entry.file_size > MAX_OFFICE_XML_ENTRY:
                    raise ValueError(f"unsafe Office XML entry: {name}")
                selected_size += entry.file_size
                if selected_size > MAX_OFFICE_XML_TOTAL:
                    raise ValueError("Office XML content exceeds the extraction limit")
                try:
                    root = ET.fromstring(archive.read(entry))
                except ET.ParseError:
                    continue
                text = " ".join(node.text or "" for node in root.iter()
                                if node.tag.rsplit("}", 1)[-1] == "t")
                if text.strip():
                    parts.append(text)
        return "\n\n".join(parts)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".html", ".htm"}:
        text = html.unescape(TAG_RE.sub(" ", text))
    return text.replace("\r\n", "\n")


def _grams(text: str) -> set[str]:
    compact = re.sub(r"\s+", " ", text.casefold()).strip()
    return {compact[index:index + 3] for index in range(max(0, len(compact) - 2))}


def _chunks(text: str, max_chars: int = 1200) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = ""
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(paragraph[start:start + max_chars] for start in range(0, len(paragraph), max_chars))
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


def build_index(directory: Path | None = None, index_path: Path | None = None,
                tenant_id: str = "default") -> dict:
    tenant_directory, tenant_index = tenant_paths(tenant_id)
    directory = directory or tenant_directory
    index_path = index_path or tenant_index
    directory.mkdir(parents=True, exist_ok=True)
    documents: list[dict] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        source = path.relative_to(directory).as_posix()
        for index, chunk in enumerate(_chunks(_plain_text(path)), start=1):
            tokens = _tokens(chunk)
            if tokens:
                documents.append({
                    "source": source,
                    "chunk": index,
                    "text": chunk,
                    "length": len(tokens),
                    "terms": dict(Counter(tokens)),
                    "grams": sorted(_grams(chunk)),
                })
    payload = {"version": INDEX_VERSION, "tenant": tenant_id, "documents": documents}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"files": len({doc["source"] for doc in documents}), "chunks": len(documents)}


def index_status(index_path: Path | None = None, tenant_id: str = "default") -> dict:
    index_path = index_path or tenant_paths(tenant_id)[1]
    if not index_path.exists():
        return {"ready": False, "files": 0, "chunks": 0}
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if payload.get("version") != INDEX_VERSION:
        return {"ready": False, "files": 0, "chunks": 0, "reason": "reindex_required"}
    documents = payload.get("documents", [])
    return {
        "ready": True,
        "files": len({doc.get("source") for doc in documents}),
        "chunks": len(documents),
    }


def search(query: str, limit: int = 5, index_path: Path | None = None,
           tenant_id: str = "default") -> list[SearchHit]:
    index_path = index_path or tenant_paths(tenant_id)[1]
    terms = _tokens(query)
    if not terms:
        return []
    if not index_path.exists():
        build_index(index_path=index_path, tenant_id=tenant_id)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if payload.get("version") != INDEX_VERSION:
        build_index(index_path=index_path, tenant_id=tenant_id)
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    documents = payload.get("documents", [])
    if not documents:
        return []
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(document.get("terms", {}).keys())
    average_length = sum(doc.get("length", 0) for doc in documents) / len(documents)
    query_grams = _grams(query)
    scored: list[SearchHit] = []
    for document in documents:
        score = 0.0
        length = max(1, document.get("length", 1))
        frequencies = document.get("terms", {})
        for term in terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(1 + (len(documents) - document_frequency[term] + 0.5) /
                               (document_frequency[term] + 0.5))
            score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average_length))
        document_grams = set(document.get("grams", []))
        union = query_grams | document_grams
        subword = len(query_grams & document_grams) / len(union) if union else 0.0
        hybrid_score = score + subword * 2.0
        if hybrid_score:
            scored.append(SearchHit(
                str(document["source"]), int(document["chunk"]), round(hybrid_score, 4), str(document["text"])
            ))
    return sorted(scored, key=lambda hit: (-hit.score, hit.source, hit.chunk))[:max(1, min(limit, 10))]


def _main() -> int:
    """Rebuild the index with: python rag.py --build"""
    import argparse

    parser = argparse.ArgumentParser(description="Frontdesk knowledge index")
    parser.add_argument("--build", action="store_true", help="rebuild the index from knowledge/")
    parser.add_argument("--status", action="store_true", help="show the state of the index")
    parser.add_argument("--tenant", default="default", help="tenant id")
    args = parser.parse_args()

    if args.build:
        result = build_index(tenant_id=args.tenant)
        # build_index reports the key as "files"; reading "documents" made every
        # successful build print "0 documents", which reads as a failure.
        print(f"indexed {result.get('chunks', 0)} chunks "
              f"from {result.get('files', 0)} documents")
        return 0
    status = index_status(tenant_id=args.tenant)
    # index_status reports readiness as "ready"; reading "exists" told an operator
    # there was no index seconds after they had built one.
    if status.get("ready"):
        print(f"{status.get('chunks', 0)} chunks from {status.get('files', 0)} "
              f"documents, tenant {args.tenant}")
    else:
        print("no index yet (run: python rag.py --build)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
