"""Per-page classification shared by info, extract_text, scanned_pages, etc."""

from __future__ import annotations

from typing import Any

NO_OCR = "pdfops does not perform OCR"

_NAMED = {
    "Letter": (612, 792), "Legal": (612, 1008), "Tabloid": (792, 1224),
    "A3": (842, 1191), "A4": (595, 842), "A5": (420, 595),
}


def size_name(w: float, h: float) -> str | None:
    a, b = sorted((w, h))
    for name, (x, y) in _NAMED.items():
        if abs(a - x) <= 3 and abs(b - y) <= 3:
            return name + (" landscape" if w > h else "")
    return None


def page_size(page) -> dict[str, Any]:
    """Displayed size of a pypdf page (rotation applied)."""
    box = page.cropbox if page.cropbox is not None else page.mediabox
    w, h = float(box.width), float(box.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    if rot in (90, 270):
        w, h = h, w
    return {"width_pt": round(w, 2), "height_pt": round(h, 2),
            "width_in": round(w / 72, 2), "height_in": round(h / 72, 2),
            "rotation": rot, "name": size_name(w, h)}


def _image_coverage(ppage) -> float:
    w, h = float(ppage.width), float(ppage.height)
    if w <= 0 or h <= 0:
        return 0.0
    x0p, topp = float(ppage.bbox[0]), float(ppage.bbox[1])
    total = 0.0
    for im in ppage.images:
        x0 = max(float(im["x0"]), x0p)
        x1 = min(float(im["x1"]), x0p + w)
        t = max(float(im["top"]), topp)
        b = min(float(im["bottom"]), topp + h)
        if x1 > x0 and b > t:
            total += (x1 - x0) * (b - t)
    return min(1.0, total / (w * h))


def classify(ppage) -> dict[str, Any]:
    """Classify a pdfplumber page.

    kind: "text"            has a text layer, not dominated by an image
          "scanned"         no text layer, has image(s) -> text needs OCR
          "scanned_with_text" large image plus a text layer (e.g. already OCR'd)
          "graphics"        no text, no images, only vector drawing
          "empty"           nothing drawn
    """
    chars = sum(1 for c in ppage.chars if c.get("text", "").strip())
    n_images = len(ppage.images)
    cov = _image_coverage(ppage)
    vector = bool(ppage.rects or ppage.lines or ppage.curves)
    if chars == 0:
        kind = "scanned" if n_images else ("graphics" if vector else "empty")
    else:
        kind = "scanned_with_text" if cov >= 0.5 else "text"
    return {"kind": kind, "chars": chars, "images": n_images,
            "image_coverage": round(cov, 3), "has_text_layer": chars > 0}


def scanned_warning(pages_1based: list[int]) -> str:
    return (f"page(s) {fmt(pages_1based)} are image-only with no text layer; "
            f"their text cannot be extracted or searched ({NO_OCR})")


def fmt(pages_1based: list[int]) -> str:
    if len(pages_1based) > 15:
        return ", ".join(map(str, pages_1based[:15])) + f", ... ({len(pages_1based)} total)"
    return ", ".join(map(str, pages_1based))
