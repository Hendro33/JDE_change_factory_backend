"""
Safe, bounded text extraction for PDF, DOCX and TXT documents.

  * The type is decided from the CONTENT (magic bytes, container layout),
    and must agree with the file extension. Anything else is refused.
  * Parsing runs in a separate Python process (run_isolated) with a time
    limit, a memory limit and an environment without any of the server's
    secrets. Nothing embedded in a document is ever executed: PDF
    JavaScript/forms/attachments are ignored (text only), DOCX macros are
    refused, XML is parsed with defusedxml (no entities, no external
    references), ZIP containers are checked for size and ratio first.
  * Results say exactly what was read. Encrypted files, scanned PDFs without
    a text layer and truncated documents are reported as such -- never as
    "analysed".
  * Each extracted section carries its citation label ("page 3",
    "section: Delivery dates", "lines 1-60").
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import zipfile
from typing import Optional

EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt"}
MAX_TEXT_CHARS = 200_000
MAX_PDF_PAGES = 500
DOCX_MAX_UNCOMPRESSED = 60 * 1024 * 1024
DOCX_MAX_ENTRIES = 3000
DOCX_MAX_RATIO = 200
TIMEOUT_SECONDS = 60
MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
EXTRACTOR_VERSION = 1


class Rejected(ValueError):
    """The file is refused (reason safe to show)."""


def clean_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable())
    if not name or name in (".", "..") or len(name) > 150:
        raise Rejected("the file name is missing or longer than 150 characters")
    if not re.fullmatch(r"[\w\-. ()&,+'\[\]]+", name, flags=re.UNICODE):
        raise Rejected("the file name contains characters that are not allowed")
    return name


def detect(filename: str, data: bytes) -> str:
    """pdf / docx / txt from the content, agreeing with the extension."""
    ext = os.path.splitext(filename.lower())[1]
    declared = EXTENSIONS.get(ext)
    if declared is None:
        raise Rejected("only PDF, DOCX and TXT files can be attached")
    if not data:
        raise Rejected("the file is empty")
    if data.startswith(b"%PDF-"):
        actual = "pdf"
    elif data.startswith(b"PK\x03\x04"):
        actual = "docx" if _looks_like_docx(data) else "zip"
    elif b"\x00" in data[:65536]:
        actual = "binary"
    else:
        try:
            data.decode("utf-8")
            actual = "txt"
        except UnicodeDecodeError:
            actual = "binary"
    if actual != declared:
        raise Rejected(f"the content is not a valid {ext[1:].upper()} file (it looks like {actual})")
    if actual == "docx":
        _check_docx_container(data)
    return actual


def _looks_like_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def _check_docx_container(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > DOCX_MAX_ENTRIES:
            raise Rejected("the DOCX contains too many parts")
        total = sum(i.file_size for i in infos)
        if total > DOCX_MAX_UNCOMPRESSED:
            raise Rejected("the DOCX expands to more than 60 MB")
        for i in infos:
            if i.compress_size and i.file_size / i.compress_size > DOCX_MAX_RATIO and i.file_size > 1024 * 1024:
                raise Rejected("the DOCX has a suspicious compression ratio")
            if i.filename.lower().endswith("vbaproject.bin"):
                raise Rejected("the DOCX contains macros; save it as a macro-free .docx and try again")
        ct = z.read("[Content_Types].xml").decode("utf-8", "replace")
        if "macroEnabled" in ct:
            raise Rejected("the DOCX is macro-enabled; save it as a macro-free .docx and try again")


# -- Parsers (run inside the isolated process) ------------------------------------
def _pdf(data: bytes) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                return {"status": "failed", "reason": "encrypted",
                        "detail": "The PDF is password-protected, so its text cannot be read. Upload an unprotected copy."}
        pages = reader.pages
        n = len(pages)
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        return {"status": "failed", "reason": "unreadable", "detail": f"The PDF could not be parsed ({type(exc).__name__})."}
    sections, total, truncated = [], 0, False
    for i, page in enumerate(pages[:MAX_PDF_PAGES]):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 -- one bad page must not hide the rest
            text = ""
        text = _clean(text)
        if not text.strip():
            continue
        if total + len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS - total], True
        sections.append({"label": f"page {i + 1}", "text": text})
        total += len(text)
        if truncated:
            break
    if n > MAX_PDF_PAGES:
        truncated = True
    if total < 20:
        return {"status": "failed", "reason": "no_text",
                "detail": "No text layer was found (it is probably a scanned image). Jade has no OCR, so its content "
                          "was not read. Upload a text-based PDF, DOCX or TXT instead."}
    return {"status": "ready", "sections": sections, "chars": total, "truncated": truncated, "pages": n}


def _docx(data: bytes) -> dict:
    from defusedxml import ElementTree as ET

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except Exception as exc:  # noqa: BLE001 -- defusedxml refusals included
        return {"status": "failed", "reason": "unreadable", "detail": f"The DOCX could not be parsed ({type(exc).__name__})."}
    body = root.find(f"{w}body")
    sections: list[dict] = []
    label, buf, total, truncated, n_heading = "start of document", [], 0, False, 0

    def flush():
        nonlocal buf, total, truncated
        text = _clean("\n".join(buf))
        buf = []
        if not text.strip() or truncated:
            return
        if total + len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS - total], True
        sections.append({"label": label, "text": text})
        total += len(text)

    for p in (body.iter(f"{w}p") if body is not None else []):
        style = p.find(f"{w}pPr/{w}pStyle")
        text = "".join(t.text or "" for t in p.iter(f"{w}t"))
        is_heading = style is not None and str(style.get(f"{w}val", "")).lower().startswith(("heading", "title"))
        if is_heading and text.strip():
            flush()
            n_heading += 1
            label = f"section: {text.strip()[:80]}"
            continue
        if text:
            buf.append(text)
    flush()
    if total == 0:
        return {"status": "failed", "reason": "no_text", "detail": "The DOCX contains no readable text."}
    return {"status": "ready", "sections": sections, "chars": total, "truncated": truncated}


def _txt(data: bytes) -> dict:
    text = _clean(data.decode("utf-8-sig"))
    truncated = len(text) > MAX_TEXT_CHARS
    text = text[:MAX_TEXT_CHARS]
    lines = text.split("\n")
    sections = []
    for start in range(0, len(lines), 60):
        chunk = "\n".join(lines[start:start + 60])
        if chunk.strip():
            sections.append({"label": f"lines {start + 1}-{min(start + 60, len(lines))}", "text": chunk})
    if not sections:
        return {"status": "failed", "reason": "no_text", "detail": "The text file is empty."}
    return {"status": "ready", "sections": sections, "chars": len(text), "truncated": truncated}


def _clean(text: str) -> str:
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32).strip()


PARSERS = {"pdf": _pdf, "docx": _docx, "txt": _txt}


def _limit_resources() -> None:  # pragma: no cover -- runs in the child
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    except (ImportError, ValueError, OSError):
        pass  # not every platform enforces these; the wall-clock timeout still applies


def run_isolated(file_type: str, data: bytes) -> dict:
    """Extract in a separate, limited process. Never raises for bad input."""
    if file_type not in PARSERS:
        return {"status": "failed", "reason": "unsupported", "detail": "unsupported type"}
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": pkg_root, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}  # no server secrets
    try:
        proc = subprocess.run([sys.executable, "-m", "jde_api_service.knowledge.extract", file_type], input=data,
                              capture_output=True, timeout=TIMEOUT_SECONDS, env=env, cwd=pkg_root,
                              preexec_fn=_limit_resources if os.name == "posix" else None)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "timeout",
                "detail": f"Reading the document took longer than {TIMEOUT_SECONDS} seconds and was stopped."}
    if proc.returncode != 0:
        return {"status": "failed", "reason": "crashed",
                "detail": "The document could not be read safely (the reader stopped, possibly at its memory limit)."}
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "crashed", "detail": "The reader returned no usable result."}
    out["extractor_version"] = EXTRACTOR_VERSION
    return out


def summary(result: dict) -> tuple[str, str, Optional[int]]:
    """(extraction_status, detail, section count) for the metadata row."""
    if result.get("status") == "ready":
        detail = f"{result.get('chars', 0):,} characters in {len(result['sections'])} sections"
        if result.get("pages"):
            detail += f" from {result['pages']} pages"
        if result.get("truncated"):
            detail += f"; TRUNCATED: only the first {MAX_TEXT_CHARS:,} characters are available to agents"
        return "ready", detail, len(result["sections"])
    return "failed", result.get("detail") or "could not be read", None


def _main() -> None:  # pragma: no cover -- exercised through run_isolated
    file_type = sys.argv[1]
    data = sys.stdin.buffer.read()
    try:
        result = PARSERS[file_type](data)
    except MemoryError:
        result = {"status": "failed", "reason": "too_large", "detail": "The document needs more memory than allowed."}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "failed", "reason": "unreadable", "detail": f"The document could not be parsed ({type(exc).__name__})."}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    _main()

