"""Read-only operations: info, extract_text, search, scanned_pages."""

from __future__ import annotations

import re

from ._analysis import NO_OCR, classify, page_size, scanned_warning
from ._core import BOOL, PAGES, STR, Doc, Result, UserError, operation, parse_pages, schema


def _metadata(reader) -> dict:
    out = {}
    try:
        meta = reader.metadata or {}
        for k, v in meta.items():
            key = str(k).lstrip("/")
            try:
                out[key] = str(v)
            except Exception:
                out[key] = None
    except Exception:
        pass
    return out


def _has_form(reader) -> tuple[bool, int]:
    try:
        fields = reader.get_fields() or {}
    except Exception:
        fields = {}
    return bool(fields), len(fields)


@operation(schema("Document overview: pages, sizes, text/scanned per page, form, encryption, metadata.",
                  ["path"], path=STR, password=STR))
def info(res: Result, path: str, password: str | None = None) -> None:
    doc = Doc(path, password, require=False)
    d = res.details
    d["path"] = path
    d["encrypted"] = doc.encrypted
    d["file_bytes"] = len(doc.raw)
    if doc.encrypted:
        d["opened"] = doc.decrypted
        d["opened_with_password"] = doc.used_password
        if not doc.decrypted:
            d["open_error"] = doc.decrypt_error
            res.warn(f"PDF is encrypted and could not be opened: {doc.decrypt_error}; "
                     "page details are unavailable")
            return
    r = doc.reader
    d["pdf_version"] = getattr(r, "pdf_header", "").replace("%PDF-", "") or None
    d["pages"] = doc.n
    d["metadata"] = _metadata(r)
    d["has_form"], d["form_field_count"] = _has_form(r)
    per_page = []
    with doc.plumber() as pdf:
        for i, (pg, pp) in enumerate(zip(r.pages, pdf.pages), start=1):
            entry = {"page": i, **page_size(pg)}
            try:
                entry.update(classify(pp))
            except Exception as exc:
                entry["kind"] = "unknown"
                res.warn(f"page {i}: could not analyse content ({exc})")
            per_page.append(entry)
    d["page_details"] = per_page
    sizes = {(p["width_pt"], p["height_pt"]) for p in per_page}
    d["uniform_page_size"] = len(sizes) <= 1
    scanned = [p["page"] for p in per_page if p.get("kind") == "scanned"]
    d["scanned_pages"] = scanned
    d["text_pages"] = [p["page"] for p in per_page if p.get("has_text_layer")]
    if scanned:
        res.warn(scanned_warning(scanned))
    if per_page and len(scanned) == len(per_page):
        d["document_kind"] = "scanned"
    elif scanned:
        d["document_kind"] = "mixed"
    else:
        d["document_kind"] = "text"


@operation(schema("Text per page. mode 'plain' or 'layout' (keeps columns/spacing). No OCR.",
                  ["path"], path=STR, pages=PAGES, mode={"enum": ["plain", "layout"]},
                  max_chars={"type": "integer", "minimum": 1}, password=STR))
def extract_text(res: Result, path: str, pages=None, mode: str = "plain",
                 max_chars: int | None = None, password: str | None = None) -> None:
    doc = Doc(path, password)
    idx = parse_pages(pages, doc.n, allow_dupes=False)
    out, no_text, budget, truncated = [], [], max_chars, False
    with doc.plumber() as pdf:
        for i in idx:
            pp = pdf.pages[i]
            cls = classify(pp)
            if mode == "layout":
                text = pp.extract_text(layout=True) or ""
                text = "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")
            else:
                text = pp.extract_text() or ""
            if budget is not None:
                if len(text) > budget:
                    text, truncated = text[:budget], True
                budget -= len(text)
            entry = {"page": i + 1, "kind": cls["kind"], "chars": len(text), "text": text}
            if not cls["has_text_layer"]:
                no_text.append(i + 1)
                entry["note"] = ("no text layer: image-only page, " + NO_OCR) if cls["images"] else \
                    "no text on this page"
            out.append(entry)
            if budget is not None and budget <= 0:
                truncated = truncated or i != idx[-1]
                break
    scanned = [e["page"] for e in out if e["kind"] == "scanned"]
    if scanned:
        res.warn(scanned_warning(scanned))
    if truncated:
        res.warn(f"output truncated at max_chars={max_chars}")
    res.details.update({"mode": mode, "pages": out, "truncated": truncated,
                        "pages_without_text": no_text})


@operation(schema("Find terms (literal, case-insensitive by default) or a regex; returns pages and snippets.",
                  ["path", "terms"], path=STR,
                  terms={"type": ["string", "array"], "items": STR, "minItems": 1},
                  regex=BOOL, case_sensitive=BOOL, pages=PAGES,
                  context={"type": "integer", "minimum": 0, "maximum": 500},
                  max_results={"type": "integer", "minimum": 1}, password=STR))
def search(res: Result, path: str, terms, regex: bool = False, case_sensitive: bool = False,
           pages=None, context: int = 60, max_results: int = 200,
           password: str | None = None) -> None:
    if isinstance(terms, str):
        terms = [terms]
    terms = [t for t in terms if t.strip()]
    if not terms:
        raise UserError("no non-empty search terms given")
    flags = 0 if case_sensitive else re.IGNORECASE
    patterns = []
    for t in terms:
        if regex:
            try:
                patterns.append((t, re.compile(t, flags)))
            except re.error as exc:
                raise UserError(f"invalid regex {t!r}: {exc}", "bad_regex") from None
        else:
            # whitespace in a term matches any run of whitespace (line breaks included)
            pat = r"\s+".join(re.escape(w) for w in t.split())
            patterns.append((t, re.compile(pat, flags)))
    doc = Doc(path, password)
    idx = parse_pages(pages, doc.n, allow_dupes=False)
    matches, counts, no_text, hit_limit = [], {t: 0 for t in terms}, [], False
    with doc.plumber() as pdf:
        for i in idx:
            pp = pdf.pages[i]
            text = pp.extract_text() or ""
            if not text.strip():
                if pp.images:
                    no_text.append(i + 1)
                continue
            for term, rx in patterns:
                for m in rx.finditer(text):
                    if m.end() == m.start():
                        continue
                    counts[term] += 1
                    if len(matches) >= max_results:
                        hit_limit = True
                        continue
                    a, b = max(0, m.start() - context), min(len(text), m.end() + context)
                    snippet = re.sub(r"\s+", " ", text[a:b]).strip()
                    matches.append({"page": i + 1, "term": term, "match": m.group(0),
                                    "snippet": ("…" if a > 0 else "") + snippet +
                                               ("…" if b < len(text) else "")})
    if no_text:
        res.warn(scanned_warning(no_text) + "; matches there would be missed")
    if hit_limit:
        res.warn(f"more than max_results={max_results} matches; list truncated (counts are complete)")
    for t, c in counts.items():
        if c == 0:
            res.warn(f"term not found: {t!r}")
    res.details.update({
        "total_matches": sum(counts.values()), "counts": counts,
        "pages_with_matches": sorted({m["page"] for m in matches}),
        "matches": matches, "unsearchable_pages": no_text,
    })


@operation(schema("List image-only (scanned) pages that have no text layer. Does not OCR.",
                  ["path"], path=STR, pages=PAGES, password=STR))
def scanned_pages(res: Result, path: str, pages=None, password: str | None = None) -> None:
    doc = Doc(path, password)
    idx = parse_pages(pages, doc.n, allow_dupes=False)
    rows = []
    with doc.plumber() as pdf:
        for i in idx:
            rows.append({"page": i + 1, **classify(pdf.pages[i])})
    scanned = [r["page"] for r in rows if r["kind"] == "scanned"]
    ocrd = [r["page"] for r in rows if r["kind"] == "scanned_with_text"]
    res.details.update({
        "scanned_pages": scanned, "count": len(scanned), "checked_pages": len(rows),
        "image_pages_with_text_layer": ocrd, "per_page": rows,
        "all_scanned": bool(rows) and len(scanned) == len(rows),
    })
    if scanned:
        res.warn(scanned_warning(scanned))
    if ocrd:
        res.warn(f"page(s) {', '.join(map(str, ocrd))} are full-page images that already carry a "
                 "text layer (possibly OCR'd elsewhere); its accuracy is unknown")
