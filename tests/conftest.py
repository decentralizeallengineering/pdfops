"""Fixtures: every test PDF is generated here with fpdf2 (form fields are added with pypdf)."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import numpy as np
import pypdf
import pytest
from fpdf import FPDF
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject, TextStringObject)


def png_bytes(gray: np.ndarray) -> bytes:
    """Minimal 8-bit grayscale PNG encoder (keeps tests free of extra imaging deps)."""
    h, w = gray.shape
    raw = b"".join(b"\x00" + gray[y].astype(np.uint8).tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _save(pdf: FPDF, path: Path) -> Path:
    pdf.output(str(path))
    return path


@pytest.fixture
def text_pdf(tmp_path) -> Path:
    """3 Letter pages with known text; 'Jane Doe' on pages 1 and 3."""
    pdf = FPDF(unit="pt", format="letter")
    pdf.set_font("helvetica", size=12)
    texts = [
        ["Quarterly Report", "Prepared for Jane Doe by the farm office.", "Invoice INV-2041 is due."],
        ["Second page about irrigation.", "Drip lines were flushed on Monday."],
        ["Third page.", "Contact Jane Doe for questions.", "Invoice INV-2077 is paid."],
    ]
    for lines in texts:
        pdf.add_page()
        for ln in lines:
            pdf.cell(0, 20, ln, new_x="LMARGIN", new_y="NEXT")
    pdf.set_title("Test Report")
    pdf.set_author("pdfops tests")
    return _save(pdf, tmp_path / "text.pdf")


@pytest.fixture
def make_pdf(tmp_path):
    """Factory: make_pdf(name, n_pages, size=(w,h)) with 'Page k of NAME' on each page."""
    def _make(name: str, n: int, size=(612, 792)) -> Path:
        pdf = FPDF(unit="pt", format=size)
        pdf.set_font("helvetica", size=14)
        for k in range(1, n + 1):
            pdf.add_page()
            pdf.cell(0, 20, f"Page {k} of {name}")
        return _save(pdf, tmp_path / f"{name}.pdf")
    return _make


@pytest.fixture
def table_pdf(tmp_path) -> Path:
    """A ruled table with a header that runs from page 1 onto page 2 (header repeated)."""
    pdf = FPDF(unit="pt", format="letter")
    pdf.set_font("helvetica", size=11)
    pdf.add_page()
    pdf.cell(0, 24, "Harvest log", new_x="LMARGIN", new_y="NEXT")
    rows = [("Crop", "Bed", "Pounds", "Price")]
    for k in range(1, 46):
        rows.append((f"Crop {k}", f"B{k:02d}", f"{k * 12.5:,.1f}", f"${k * 1.25:.2f}"))
    with pdf.table(line_height=18) as table:
        for r in rows:
            row = table.row()
            for c in r:
                row.cell(c)
    return _save(pdf, tmp_path / "table.pdf")


@pytest.fixture
def form_pdf(tmp_path) -> Path:
    """fpdf2 page with labels; AcroForm text, checkbox, radio group and combo added via pypdf."""
    pdf = FPDF(unit="pt", format="letter")
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    for y, label in ((90, "Name:"), (140, "Organic:"), (190, "Size:"), (240, "Crop:")):
        pdf.text(72, y, label)
    base = pypdf.PdfReader(io.BytesIO(bytes(pdf.output())))
    w = pypdf.PdfWriter(clone_from=base)
    page = w.pages[0]

    def ap_stream(draw: bytes, wd: float, ht: float):
        s = DecodedStreamObject()
        s.set_data(draw)
        s.update({NameObject("/Type"): NameObject("/XObject"),
                  NameObject("/Subtype"): NameObject("/Form"),
                  NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0),
                                                    FloatObject(wd), FloatObject(ht)])})
        return w._add_object(s)

    def rect(x0, y0, x1, y1):
        return ArrayObject([FloatObject(v) for v in (x0, y0, x1, y1)])

    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    dr = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/Helv"): w._add_object(font)})})
    annots = ArrayObject()
    fields = ArrayObject()
    check = b"0 0 0 rg 2 2 10 10 re f"
    box = b"0 0 0 RG 0.5 0.5 13 13 re S"

    name = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("grower_name"),
        NameObject("/Rect"): rect(140, 690, 400, 710), NameObject("/F"): NumberObject(4),
        NameObject("/DA"): TextStringObject("/Helv 11 Tf 0 g"), NameObject("/P"): page.indirect_reference,
    })
    nref = w._add_object(name)
    annots.append(nref)
    fields.append(nref)

    cb = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Btn"), NameObject("/T"): TextStringObject("organic"),
        NameObject("/Rect"): rect(140, 640, 154, 654), NameObject("/F"): NumberObject(4),
        NameObject("/V"): NameObject("/Off"), NameObject("/AS"): NameObject("/Off"),
        NameObject("/AP"): DictionaryObject({NameObject("/N"): DictionaryObject({
            NameObject("/Yes"): ap_stream(box + b" " + check, 14, 14),
            NameObject("/Off"): ap_stream(box, 14, 14)})}),
        NameObject("/P"): page.indirect_reference,
    })
    cref = w._add_object(cb)
    annots.append(cref)
    fields.append(cref)

    radio = DictionaryObject({
        NameObject("/FT"): NameObject("/Btn"), NameObject("/T"): TextStringObject("size"),
        NameObject("/Ff"): NumberObject((1 << 15) | (1 << 14)), NameObject("/V"): NameObject("/Off"),
    })
    rref = w._add_object(radio)
    kids = ArrayObject()
    for k, state in enumerate(("Small", "Large")):
        wd = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Parent"): rref, NameObject("/Rect"): rect(140 + 80 * k, 590, 154 + 80 * k, 604),
            NameObject("/F"): NumberObject(4), NameObject("/AS"): NameObject("/Off"),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): DictionaryObject({
                NameObject("/" + state): ap_stream(box + b" " + check, 14, 14),
                NameObject("/Off"): ap_stream(box, 14, 14)})}),
            NameObject("/P"): page.indirect_reference,
        })
        ref = w._add_object(wd)
        kids.append(ref)
        annots.append(ref)
    radio[NameObject("/Kids")] = kids
    fields.append(rref)

    combo = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Ch"), NameObject("/T"): TextStringObject("crop"),
        NameObject("/Ff"): NumberObject(1 << 17), NameObject("/F"): NumberObject(4),
        NameObject("/Opt"): ArrayObject([TextStringObject(x) for x in ("Kale", "Chard", "Beets")]),
        NameObject("/Rect"): rect(140, 540, 300, 560),
        NameObject("/DA"): TextStringObject("/Helv 11 Tf 0 g"), NameObject("/P"): page.indirect_reference,
    })
    chref = w._add_object(combo)
    annots.append(chref)
    fields.append(chref)

    page[NameObject("/Annots")] = annots
    w._root_object[NameObject("/AcroForm")] = w._add_object(DictionaryObject({
        NameObject("/Fields"): fields, NameObject("/DR"): dr,
        NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g"),
        NameObject("/NeedAppearances"): BooleanObject(False),
    }))
    out = tmp_path / "form.pdf"
    with open(out, "wb") as fh:
        w.write(fh)
    return out


@pytest.fixture
def encrypted_pdf(tmp_path) -> Path:
    """RC4-encrypted with user password 'sesame' (fpdf2's built-in encryption)."""
    pdf = FPDF(unit="pt", format="letter")
    pdf.set_encryption(owner_password="owner-pw", user_password="sesame")
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(0, 20, "Confidential crop plan")
    return _save(pdf, tmp_path / "encrypted.pdf")


@pytest.fixture
def scanned_pdf(tmp_path) -> Path:
    """Page 1: real text. Page 2: full-page image of 'ink' (a scan, no text layer).
    Page 3: near-white noisy scan (blank). Page 4: empty page."""
    rng = np.random.default_rng(0)
    scan = np.full((220, 170), 245, np.uint8)
    scan[30:40, 20:150] = 20       # dark bars standing in for printed lines
    scan[60:70, 20:120] = 20
    scan[90:100, 20:140] = 20
    scan += rng.integers(0, 8, scan.shape, dtype=np.uint8)
    blank = np.clip(248 + rng.integers(-4, 6, (220, 170)), 0, 255).astype(np.uint8)
    pdf = FPDF(unit="pt", format="letter")
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(0, 20, "This page has a real text layer.")
    pdf.add_page()
    pdf.image(io.BytesIO(png_bytes(scan)), x=0, y=0, w=612, h=792)
    pdf.add_page()
    pdf.image(io.BytesIO(png_bytes(blank)), x=0, y=0, w=612, h=792)
    pdf.add_page()
    return _save(pdf, tmp_path / "scanned.pdf")


@pytest.fixture
def out(tmp_path):
    def _p(name: str) -> str:
        return str(tmp_path / name)
    return _p
