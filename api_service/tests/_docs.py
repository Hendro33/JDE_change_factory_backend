"""Synthetic test documents (never customer data)."""

from __future__ import annotations

import io
import zipfile


def pdf(pages: list[str]) -> bytes:
    """A minimal, valid text PDF: one line of Helvetica text per page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def blank_pdf() -> bytes:
    """A PDF with a page but no text layer (like a scan)."""
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    for p in PdfReader(io.BytesIO(pdf(["Secret synthetic content"]))).pages:
        w.add_page(p)
    w.encrypt(user_password="synthetic-pw", owner_password="synthetic-owner")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


_CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
       'wordprocessingml.document.main+xml"/></Types>')


def docx(blocks: list[tuple[str, str]], *, macro: bool = False) -> bytes:
    """blocks: (kind, text) with kind "h" (Heading1) or "p"."""
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = []
    for kind, text in blocks:
        ppr = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if kind == "h" else ""
        paras.append(f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>")
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>{"".join(paras)}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("word/document.xml", doc)
        if macro:
            z.writestr("word/vbaProject.bin", b"\x00fake-macro")
    return buf.getvalue()
