"""Generated content: summary_note, replace_name_overlay, letter."""

from __future__ import annotations

import datetime as _dt
import re
import statistics

import pypdf

from ._analysis import NO_OCR, page_size
from ._core import (BOOL, PAGES, STR, Doc, Result, UserError, atomic_write, check_output,
                    operation, parse_pages, schema, write_pdf)
from ._fpdf import TextPdf, as_reader


@operation(schema("Add a note page (optional title) at the start or end, sized like the adjacent page.",
                  ["path", "text", "output"], path=STR, text=STR, title=STR,
                  position={"enum": ["start", "end"]},
                  font_size={"type": "number", "minimum": 6, "maximum": 36}, output=STR,
                  overwrite=BOOL, password=STR))
def summary_note(res: Result, path: str, text: str, output: str, title: str | None = None,
                 position: str = "start", font_size: float = 11, overwrite: bool = False,
                 password: str | None = None) -> None:
    if not text.strip() and not (title or "").strip():
        raise UserError("text is empty")
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    ref_page = doc.reader.pages[0 if position == "start" else -1]
    sz = page_size(ref_page)
    tp = TextPdf(sz["width_pt"], sz["height_pt"], [text, title or ""], res.warn,
                 margin=min(54.0, sz["width_pt"] * 0.09))
    pdf = tp.pdf
    pdf.add_page()
    if title:
        tp.font(font_size * 1.5, bold=True)
        pdf.multi_cell(0, font_size * 1.9, tp.clean(title), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(font_size * 0.6)
    tp.font(font_size)
    for para in re.split(r"\n\s*\n", text.strip()):
        pdf.multi_cell(0, font_size * 1.35, tp.clean(para.strip()), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(font_size * 0.6)
    added = pdf.pages_count
    note = as_reader(tp.bytes())
    w = pypdf.PdfWriter(clone_from=doc.reader)
    if position == "start":
        w.merge(0, note)
    else:
        w.append(note)
    if added > 1:
        res.warn(f"note text did not fit on one page; {added} pages were added")
    size = write_pdf(w, out)
    res.changed = True
    doc.note_encryption(res, True)
    res.details.update({"output": str(out), "output_bytes": size, "position": position,
                        "pages_added": added, "output_pages": len(w.pages),
                        "page_size": {"width_pt": sz["width_pt"], "height_pt": sz["height_pt"]},
                        "font": "unicode-ttf" if tp.unicode else "helvetica"})


def _rgb(color) -> tuple[int, int, int]:
    try:
        c = [float(x) for x in (color or [])]
    except (TypeError, ValueError):
        return (0, 0, 0)
    if len(c) == 1:
        g = round(c[0] * 255)
        return (g, g, g)
    if len(c) == 3:
        return tuple(round(x * 255) for x in c)  # type: ignore[return-value]
    if len(c) == 4:
        k = c[3]
        return tuple(round(255 * (1 - x) * (1 - k)) for x in c[:3])  # type: ignore[return-value]
    return (0, 0, 0)


@operation(schema("White box over each match of 'find', then 'replace' at matched font size. Visual only, not redaction: old text stays in text layer.",
                  ["path", "find", "replace", "output"], path=STR, find=STR, replace=STR,
                  pages=PAGES, case_sensitive=BOOL, whole_word=BOOL, shrink_to_fit=BOOL,
                  output=STR, overwrite=BOOL, password=STR))
def replace_name_overlay(res: Result, path: str, find: str, replace: str, output: str,
                         pages=None, case_sensitive: bool = True, whole_word: bool = False,
                         shrink_to_fit: bool = False, overwrite: bool = False,
                         password: str | None = None) -> None:
    if not find.strip():
        raise UserError("find is empty")
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    idx = parse_pages(pages, doc.n, allow_dupes=False)
    pat = r"\s+".join(re.escape(p) for p in find.split())
    if whole_word:
        pat = rf"(?<!\w){pat}(?!\w)"
    replaced, unmatched, by_page = [], [], {}
    with doc.plumber() as pdf:
        for i in idx:
            pp = pdf.pages[i]
            pg = doc.reader.pages[i]
            if not any(c.get("text", "").strip() for c in pp.chars):
                unmatched.append({"page": i + 1, "reason": "no text layer" +
                                  (f" (image-only page; {NO_OCR})" if pp.images else "")})
                continue
            hits = pp.search(pat, regex=True, case=case_sensitive)
            if not hits:
                continue
            mb = pg.mediabox
            rot = int(pg.get("/Rotate", 0) or 0) % 360
            if rot or float(mb.left) != 0 or float(mb.bottom) != 0:
                for h in hits:
                    unmatched.append({"page": i + 1, "text": h["text"], "reason":
                                      "page is rotated or has an offset media box; overlay not supported"})
                continue
            for h in hits:
                chars = [c for c in h["chars"] if c.get("text", "").strip()]
                if not chars:
                    continue
                size = statistics.median(float(c["size"]) for c in chars)
                tops = [float(c["top"]) for c in chars]
                if max(tops) - min(tops) > size * 0.5:
                    unmatched.append({"page": i + 1, "text": h["text"],
                                      "reason": "match spans more than one line"})
                    continue
                if not all(c.get("upright", True) for c in chars):
                    unmatched.append({"page": i + 1, "text": h["text"],
                                      "reason": "text is not horizontal"})
                    continue
                fontname = str(chars[0].get("fontname", ""))
                baseline = float(chars[0]["matrix"][5])
                by_page.setdefault(i, {"w": float(pp.width), "h": float(pp.height), "items": []})
                by_page[i]["items"].append({
                    "x0": float(h["x0"]), "x1": float(h["x1"]), "top": float(h["top"]),
                    "bottom": float(h["bottom"]), "size": size, "baseline": baseline,
                    "bold": "bold" in fontname.lower(), "color": _rgb(chars[0].get("non_stroking_color")),
                    "text": h["text"], "fontname": fontname,
                })
    if not by_page:
        raise UserError(f"{find!r} was not found where it could be replaced", "not_found_in_document",
                        not_replaced=unmatched, pages_checked=len(idx))

    w = pypdf.PdfWriter(clone_from=doc.reader)
    used_unicode = False
    for i, info in sorted(by_page.items()):
        tp = TextPdf(info["w"], info["h"], [replace], res.warn, margin=0)
        used_unicode = used_unicode or tp.unicode
        pdf = tp.pdf
        pdf.set_auto_page_break(False)
        pdf.add_page()
        for it in info["items"]:
            size = it["size"]
            tp.font(size, bold=it["bold"])
            text = tp.clean(replace)
            width = pdf.get_string_width(text)
            box_w = it["x1"] - it["x0"]
            note = None
            if width > box_w + 0.5:
                if shrink_to_fit and width > 0:
                    size = max(4.0, size * box_w / width)
                    tp.font(size, bold=it["bold"])
                    width = pdf.get_string_width(text)
                    note = f"shrunk to {size:.1f}pt to fit"
                else:
                    note = f"replacement is {width - box_w:.1f}pt wider than the original"
                    res.warn(f"page {i + 1}: {note}; it may overlap the following text")
            pdf.set_fill_color(255, 255, 255)
            pdf.rect(it["x0"] - 0.5, it["top"] - 0.5, box_w + 1.0, it["bottom"] - it["top"] + 1.0,
                     style="F")
            pdf.set_text_color(*it["color"])
            pdf.text(it["x0"], info["h"] - it["baseline"], text)
            replaced.append({"page": i + 1, "found": it["text"], "font_size": round(size, 2),
                             "original_font": it["fontname"],
                             "bbox": [round(it["x0"], 1), round(it["top"], 1),
                                      round(it["x1"], 1), round(it["bottom"], 1)],
                             **({"note": note} if note else {})})
        overlay = as_reader(tp.bytes()).pages[0]
        w.pages[i].merge_page(overlay)
    size = write_pdf(w, out)
    res.changed = True
    doc.note_encryption(res, True)
    res.warn("visual overlay only: the original text is still in the PDF text layer and can be "
             "copied or extracted; this is NOT a redaction")
    if unmatched:
        res.warn(f"{len(unmatched)} location(s) could not be replaced; see details.not_replaced")
    matched_pages = {r["page"] for r in replaced}
    res.details.update({
        "output": str(out), "output_bytes": size, "replaced": replaced, "count": len(replaced),
        "not_replaced": unmatched,
        "pages_without_match": [i + 1 for i in idx if i + 1 not in matched_pages
                                and not any(u["page"] == i + 1 for u in unmatched)],
        "font_used": "unicode-ttf" if used_unicode else "helvetica",
    })


LETTER_KEYS = ("sender_name", "sender_address", "recipient_name", "recipient_address", "date",
               "subject", "salutation", "body", "closing", "signature")


def _lines(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [ln for ln in str(v).splitlines() if ln.strip()]


@operation(schema("One-page letter. fields keys: body (required), sender_name, sender_address, "
                  "recipient_name, recipient_address, date, subject, salutation, closing, signature. "
                  "Addresses: newline string or list. Font shrinks to fit.",
                  ["output", "fields"], output=STR,
                  fields={"type": "object", "required": ["body"],
                          "additionalProperties": {"type": ["string", "array"], "items": STR}},
                  page_size={"enum": ["letter", "a4"]},
                  font_size={"type": "number", "minimum": 8, "maximum": 16}, overwrite=BOOL))
def letter(res: Result, output: str, fields: dict, page_size: str = "letter",
           font_size: float = 11, overwrite: bool = False) -> None:
    unknown = sorted(set(fields) - set(LETTER_KEYS))
    if unknown:
        raise UserError(f"unknown letter field(s) {unknown}; allowed: {list(LETTER_KEYS)}")
    for k in LETTER_KEYS:
        if k not in ("sender_address", "recipient_address") and isinstance(fields.get(k), list):
            raise UserError(f"fields.{k} must be a string")
    f = dict(fields)
    body = f["body"]
    sender_name, sender_address = f.get("sender_name"), f.get("sender_address")
    recipient_name, recipient_address = f.get("recipient_name"), f.get("recipient_address")
    date, subject, salutation = f.get("date"), f.get("subject"), f.get("salutation")
    closing, signature = f.get("closing"), f.get("signature")
    if not body.strip():
        raise UserError("body is empty")
    out = check_output(output, overwrite)
    if date is None:
        today = _dt.date.today()
        date = f"{today:%B} {today.day}, {today.year}"
    if salutation is None:
        salutation = f"Dear {recipient_name}," if recipient_name else ""
    closing = "Sincerely," if closing is None else closing
    signature = sender_name if signature is None else signature
    w_pt, h_pt = (612.0, 792.0) if page_size == "letter" else (595.28, 841.89)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    all_text = [body, sender_name or "", recipient_name or "", date, subject or "", salutation,
                closing, signature or ""] + _lines(sender_address) + _lines(recipient_address)

    def render(size: float, warn) -> tuple[bytes, int]:
        tp = TextPdf(w_pt, h_pt, all_text, warn, margin=72)
        pdf = tp.pdf
        pdf.add_page()
        lh = size * 1.3

        def line(text: str, bold: bool = False) -> None:
            tp.font(size, bold)
            pdf.multi_cell(0, lh, tp.clean(text), new_x="LMARGIN", new_y="NEXT")

        if sender_name:
            line(sender_name, bold=True)
        for ln in _lines(sender_address):
            line(ln)
        if sender_name or sender_address:
            pdf.ln(lh)
        line(date)
        pdf.ln(lh)
        if recipient_name:
            line(recipient_name)
        for ln in _lines(recipient_address):
            line(ln)
        if recipient_name or recipient_address:
            pdf.ln(lh)
        if subject:
            line(f"Re: {subject}", bold=True)
            pdf.ln(lh * 0.5)
        if salutation:
            line(salutation)
            pdf.ln(lh * 0.5)
        for p in paras:
            tp.font(size)
            pdf.multi_cell(0, lh, tp.clean(" ".join(p.split())), align="J",
                           new_x="LMARGIN", new_y="NEXT")
            pdf.ln(lh * 0.5)
        if closing:
            pdf.ln(lh * 0.5)
            line(closing)
        if signature:
            pdf.ln(lh * 2.5)
            line(signature)
        return tp.bytes(), pdf.pages_count

    size = float(font_size)
    data, pages = render(size, res.warn)
    while pages > 1 and size > 8:
        size = max(8.0, size - 0.5)
        data, pages = render(size, lambda m: None)
    if pages > 1:
        raise UserError("letter does not fit on one page even at 8pt; shorten the body", "too_long")
    if size < font_size:
        res.warn(f"font reduced from {font_size}pt to {size}pt to fit one page")
    atomic_write(out, data)
    res.changed = True
    res.details.update({"output": str(out), "output_bytes": len(data), "pages": 1,
                        "font_size": size, "page_size": page_size, "paragraphs": len(paras),
                        "date": date})
