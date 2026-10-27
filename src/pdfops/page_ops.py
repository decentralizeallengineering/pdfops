"""Page-level operations: extract, delete, split, merge, append, appendix, rotate, blanks."""

from __future__ import annotations

import io
from pathlib import Path

import pypdf

from ._analysis import classify, page_size
from ._core import (BOOL, PAGES, STR, Doc, Result, UserError, _check_target, check_output,
                    copy_metadata, operation, parse_pages, ranges_text, schema, write_pdf)
from ._fpdf import as_reader, title_page

OUT = {"type": "string", "description": "path of the new .pdf"}


def _subset_writer(doc: Doc, idx: list[int]) -> pypdf.PdfWriter:
    w = pypdf.PdfWriter()
    w.append(doc.reader, pages=list(idx))
    copy_metadata(doc.reader, w)
    return w


def _save(res: Result, w: pypdf.PdfWriter, out: Path, doc: Doc | None = None) -> None:
    size = write_pdf(w, out)
    res.changed = True
    res.details["output"] = str(out)
    res.details["output_pages"] = len(w.pages)
    res.details["output_bytes"] = size
    if doc is not None:
        doc.note_encryption(res, True)


@operation(schema("Copy chosen pages (in the order given; repeats allowed) into a new PDF.",
                  ["path", "pages", "output"], path=STR, pages=PAGES, output=OUT,
                  overwrite=BOOL, password=STR))
def extract_pages(res: Result, path: str, pages, output: str, overwrite: bool = False,
                  password: str | None = None) -> None:
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    idx = parse_pages(pages, doc.n)
    if not idx:
        raise UserError("no pages selected", "bad_pages")
    _save(res, _subset_writer(doc, idx), out, doc)
    res.details["source_pages"] = doc.n
    res.details["extracted"] = [i + 1 for i in idx]


@operation(schema("Remove pages and write the rest to a new PDF. Cannot delete every page.",
                  ["path", "pages", "output"], path=STR, pages=PAGES, output=OUT,
                  overwrite=BOOL, password=STR))
def delete_pages(res: Result, path: str, pages, output: str, overwrite: bool = False,
                 password: str | None = None) -> None:
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    drop = set(parse_pages(pages, doc.n))
    keep = [i for i in range(doc.n) if i not in drop]
    if not keep:
        raise UserError("refusing to delete every page", "bad_pages")
    _save(res, _subset_writer(doc, keep), out, doc)
    res.details["deleted"] = sorted(i + 1 for i in drop)
    res.details["kept"] = ranges_text(keep)


@operation(schema("Split into files by 'ranges' (list of specs like '1-3'), 'every' N pages, or 'halves'. Use one.",
                  ["path", "output_dir"], path=STR, output_dir=STR,
                  ranges={"type": "array", "items": STR, "minItems": 1},
                  every={"type": "integer", "minimum": 1}, halves=BOOL, prefix=STR,
                  overwrite=BOOL, password=STR))
def split(res: Result, path: str, output_dir: str, ranges: list[str] | None = None,
          every: int | None = None, halves: bool = False, prefix: str | None = None,
          overwrite: bool = False, password: str | None = None) -> None:
    modes = [m for m, on in (("ranges", ranges), ("every", every), ("halves", halves)) if on]
    if len(modes) != 1:
        raise UserError("give exactly one of: ranges, every, halves")
    odir = Path(output_dir).expanduser()
    if not odir.is_dir():
        raise UserError(f"output_dir does not exist: {output_dir}", "not_found")
    doc = Doc(path, password)
    n = doc.n
    if ranges:
        groups = [parse_pages(r, n) for r in ranges]
    elif every:
        groups = [list(range(s, min(s + every, n))) for s in range(0, n, every)]
    else:
        if n < 2:
            raise UserError("a 1-page document cannot be split into halves", "bad_pages")
        mid = (n + 1) // 2
        groups = [list(range(0, mid)), list(range(mid, n))]
        if n % 2:
            res.warn(f"odd page count ({n}); first half has {mid} pages")
    stem = prefix or Path(path).stem
    targets = []
    for k, g in enumerate(groups, start=1):
        if not g:
            raise UserError(f"part {k} selects no pages", "bad_pages")
        name = f"{stem}_part{k:02d}_p{ranges_text(g).replace(',', '_')}.pdf"
        t = odir / name
        _check_target(t, overwrite)
        targets.append((t, g))
    files = []
    for t, g in targets:
        w = _subset_writer(doc, g)
        size = write_pdf(w, t)
        files.append({"path": str(t), "pages": ranges_text(g), "page_count": len(g), "bytes": size})
    covered = {i for _, g in targets for i in g}
    if len(covered) < n:
        res.warn(f"pages not in any part: {ranges_text(sorted(set(range(n)) - covered))}")
    res.changed = True
    doc.note_encryption(res, True)
    res.details.update({"mode": modes[0], "files": files, "source_pages": n})


@operation(schema("Merge PDFs in order. inputs: paths or {path,pages,bookmark,password}. bookmarks=true adds one per source (file name unless given).",
                  ["inputs", "output"],
                  inputs={"type": "array", "minItems": 1, "items": {"type": ["string", "object"]}},
                  output=OUT, bookmarks=BOOL, overwrite=BOOL))
def merge(res: Result, inputs: list, output: str, bookmarks: bool = False,
          overwrite: bool = False) -> None:
    items = []
    for k, it in enumerate(inputs):
        if isinstance(it, str):
            it = {"path": it}
        extra = set(it) - {"path", "pages", "bookmark", "password"}
        if "path" not in it or not isinstance(it["path"], str) or extra:
            raise UserError(f"inputs[{k}] needs 'path' and only pages/bookmark/password"
                            + (f" (unknown: {sorted(extra)})" if extra else ""))
        items.append(it)
    out = check_output(output, overwrite, [it["path"] for it in items])
    w = pypdf.PdfWriter()
    sources, with_forms = [], 0
    for it in items:
        doc = Doc(it["path"], it.get("password"))
        idx = parse_pages(it.get("pages"), doc.n)
        mark = it.get("bookmark") or (Path(it["path"]).stem if bookmarks else None)
        start = len(w.pages)
        w.append(doc.reader, outline_item=mark, pages=idx)
        if doc.reader.get_fields():
            with_forms += 1
        if doc.encrypted:
            doc.note_encryption(res, True)
        sources.append({"path": it["path"], "pages": ranges_text(idx) if it.get("pages") else "all",
                        "page_count": len(idx), "starts_at_page": start + 1, "bookmark": mark})
    if with_forms > 1:
        res.warn("more than one input has form fields; same-named fields may collide - check with form_fields")
    _save(res, w, out)
    res.details["sources"] = sources


@operation(schema("Insert pages from another PDF into a copy of path: at the end, or before page 'at'.",
                  ["path", "source", "output"], path=STR, source=STR, pages=PAGES,
                  at={"type": "integer", "minimum": 1}, output=OUT, overwrite=BOOL,
                  password=STR, source_password=STR))
def append_pages(res: Result, path: str, source: str, output: str, pages=None,
                 at: int | None = None, overwrite: bool = False, password: str | None = None,
                 source_password: str | None = None) -> None:
    out = check_output(output, overwrite, [path, source])
    doc = Doc(path, password)
    src = Doc(source, source_password)
    idx = parse_pages(pages, src.n)
    if at is not None and at > doc.n + 1:
        raise UserError(f"at={at} is beyond the end (document has {doc.n} pages)", "bad_pages")
    w = pypdf.PdfWriter(clone_from=doc.reader)
    pos = doc.n if at is None else at - 1
    w.merge(pos, src.reader, pages=idx)
    _save(res, w, out, doc)
    src.note_encryption(res, True)
    res.details.update({"inserted": len(idx), "inserted_at_page": pos + 1,
                        "source_pages": ranges_text(idx)})


@operation(schema("Append another PDF as an appendix, with an optional title separator page (sized like the last page) and a bookmark.",
                  ["path", "appendix", "output"], path=STR, appendix=STR, output=OUT, title=STR,
                  separator=BOOL, bookmark=BOOL, overwrite=BOOL, password=STR,
                  appendix_password=STR))
def insert_appendix(res: Result, path: str, appendix: str, output: str, title: str = "Appendix",
                    separator: bool = True, bookmark: bool = True, overwrite: bool = False,
                    password: str | None = None, appendix_password: str | None = None) -> None:
    out = check_output(output, overwrite, [path, appendix])
    doc = Doc(path, password)
    app = Doc(appendix, appendix_password)
    w = pypdf.PdfWriter(clone_from=doc.reader)
    start = len(w.pages)
    if separator:
        sz = page_size(doc.reader.pages[-1])
        sep = title_page(sz["width_pt"], sz["height_pt"], title, res.warn)
        w.append(as_reader(sep))
    w.append(app.reader)
    if bookmark:
        w.add_outline_item(title, start)
    _save(res, w, out, doc)
    app.note_encryption(res, True)
    res.details.update({"appendix_starts_at_page": start + 1, "separator_page": separator,
                        "appendix_pages": app.n, "bookmark": title if bookmark else None})


@operation(schema("Rotate pages clockwise by degrees (relative to current rotation).",
                  ["path", "degrees", "output"], path=STR,
                  degrees={"enum": [90, 180, 270, -90]}, pages=PAGES, output=OUT,
                  overwrite=BOOL, password=STR))
def rotate(res: Result, path: str, degrees: int, output: str, pages=None,
           overwrite: bool = False, password: str | None = None) -> None:
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    idx = parse_pages(pages, doc.n, allow_dupes=False)
    w = pypdf.PdfWriter(clone_from=doc.reader)
    changed = []
    for i in idx:
        p = w.pages[i]
        p.rotate(degrees)
        changed.append({"page": i + 1, "rotation": int(p.get("/Rotate", 0)) % 360})
    _save(res, w, out, doc)
    res.details["rotated"] = changed


def _ink_ratio(ppage, resolution: int) -> float:
    import numpy as np
    img = ppage.to_image(resolution=resolution).original.convert("L")
    arr = np.asarray(img)
    if arr.size == 0:
        return 0.0
    return float((arr < 200).mean())


@operation(schema("Drop blank pages: no text and (near-)white when rendered, incl. blank scans. dry_run lists only.",
                  ["path"], path=STR, output=OUT,
                  ink_threshold={"type": "number", "minimum": 0, "maximum": 0.2,
                                 "description": "max dark-pixel fraction; default 0.001"},
                  dry_run=BOOL, overwrite=BOOL, password=STR))
def drop_blank_pages(res: Result, path: str, output: str | None = None,
                     ink_threshold: float = 0.001, dry_run: bool = False,
                     overwrite: bool = False, password: str | None = None) -> None:
    if not dry_run and not output:
        raise UserError("output is required unless dry_run=true")
    out = check_output(output, overwrite, [path]) if not dry_run else None
    doc = Doc(path, password)
    per_page, blank = [], []
    render_failed = False
    with doc.plumber() as pdf:
        for i, pp in enumerate(pdf.pages):
            cls = classify(pp)
            entry = {"page": i + 1, "kind": cls["kind"], "text_chars": cls["chars"]}
            if cls["chars"]:
                entry["blank"] = False
            else:
                try:
                    ratio = _ink_ratio(pp, 40)
                    entry["ink_ratio"] = round(ratio, 5)
                    entry["blank"] = ratio <= ink_threshold
                except Exception as exc:
                    render_failed = True
                    entry["blank"] = cls["kind"] == "empty"
                    entry["note"] = f"could not render ({type(exc).__name__}); judged by content only"
            if entry["blank"]:
                blank.append(i)
            per_page.append(entry)
    if render_failed:
        res.warn("some pages could not be rendered; image pages there were kept")
    res.details.update({"blank_pages": [i + 1 for i in blank], "per_page": per_page,
                        "dry_run": dry_run, "ink_threshold": ink_threshold})
    if len(blank) == doc.n:
        raise UserError("every page looks blank; nothing written", "all_blank",
                        blank_pages=[i + 1 for i in blank], per_page=per_page)
    if dry_run or not blank:
        res.details["output"] = None
        if not blank:
            res.details["note"] = "no blank pages found; no file written"
        return
    keep = [i for i in range(doc.n) if i not in set(blank)]
    _save(res, _subset_writer(doc, keep), out, doc)
