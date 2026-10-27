"""fpdf2 helpers: page creation at a given size and safe text/font handling."""

from __future__ import annotations

import io
import os
from pathlib import Path

from fpdf import FPDF

_TRANSLIT = {
    "\u2018": "'", "\u2019": "'", "\u201a": ",", "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "--", "\u2022": "\xb7", "\u2026": "...", "\u20ac": "EUR",
    "\u00a0": " ", "\u2009": " ", "\u200b": "", "\u2212": "-", "\u2122": "(TM)",
}

_FONT_DIRS_WIN = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
_CANDIDATES = [
    # (regular, bold) file names, searched in common font folders
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("Arial.ttf", "Arial Bold.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
]
_SEARCH_DIRS = _FONT_DIRS_WIN + [Path(p) for p in (
    "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/liberation-sans",
    "/usr/share/fonts/truetype/noto", "/usr/share/fonts/noto",
    "/Library/Fonts", "/System/Library/Fonts/Supplemental",
)]


def find_unicode_font() -> tuple[str, str | None] | None:
    for reg, bold in _CANDIDATES:
        for d in _SEARCH_DIRS:
            r = d / reg
            if r.is_file():
                b = d / bold
                return str(r), (str(b) if b.is_file() else None)
    return None


def _latin1_ok(text: str) -> bool:
    try:
        text.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


class TextPdf:
    """Wraps FPDF, choosing core Helvetica or a system Unicode font as needed."""

    def __init__(self, width_pt: float, height_pt: float, texts: list[str], warn,
                 margin: float = 54.0):
        self.pdf = FPDF(unit="pt", format=(width_pt, height_pt))
        self.pdf.set_margins(margin, margin, margin)
        self.pdf.set_auto_page_break(True, margin)
        self.pdf.set_creator("pdfops")
        self.family = "helvetica"
        self._warn = warn
        joined = "".join(texts)
        self.unicode = False
        if not _latin1_ok(joined) and not _latin1_ok(self._translit(joined)):
            font = find_unicode_font()
            if font:
                self.pdf.add_font("uni", "", font[0])
                self.pdf.add_font("uni", "B", font[1] or font[0])
                self.family = "uni"
                self.unicode = True
            else:
                warn("text contains characters outside Latin-1 and no Unicode font was found "
                     "on this system; those characters were replaced with '?'")

    @staticmethod
    def _translit(text: str) -> str:
        return "".join(_TRANSLIT.get(c, c) for c in text)

    def clean(self, text: str) -> str:
        if self.unicode:
            return text
        text = self._translit(text)
        return text.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False) -> None:
        self.pdf.set_font(self.family, "B" if bold else "", size)

    def bytes(self) -> bytes:
        return bytes(self.pdf.output())


def title_page(width_pt: float, height_pt: float, title: str, warn) -> bytes:
    """A single page with a centred title (used as an appendix separator)."""
    tp = TextPdf(width_pt, height_pt, [title], warn)
    pdf = tp.pdf
    pdf.add_page()
    size = max(14.0, min(28.0, width_pt / 22))
    tp.font(size, bold=True)
    pdf.set_y(height_pt * 0.42)
    pdf.multi_cell(0, size * 1.3, tp.clean(title), align="C")
    return tp.bytes()


def as_reader(data: bytes):
    import pypdf
    return pypdf.PdfReader(io.BytesIO(data))
