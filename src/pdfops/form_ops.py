"""AcroForm operations: form_fields, fill_form, flatten_form."""

from __future__ import annotations

import difflib
from typing import Any, Iterator

import pypdf
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, IndirectObject,
                           NameObject, TextStringObject)

from ._core import BOOL, STR, Doc, Result, UserError, check_output, operation, schema, write_pdf

FF_READONLY, FF_REQUIRED = 1, 2
FF_MULTILINE = 1 << 12
FF_RADIO, FF_PUSH, FF_COMBO, FF_MULTISELECT = 1 << 15, 1 << 16, 1 << 17, 1 << 21
F_HIDDEN, F_NOVIEW = 2, 32

_TRUE = {"true", "yes", "on", "1", "x", "checked", "y", "t"}
_FALSE = {"false", "no", "off", "0", "", "unchecked", "n", "f", "none"}


class Field:
    def __init__(self, name: str, obj: DictionaryObject, inh: dict, widgets: list):
        self.name = name
        self.obj = obj
        self.ft = str(inh.get("/FT", ""))
        self.ff = int(inh.get("/Ff", 0) or 0)
        self.inh = inh
        self.widgets = [w.get_object() for w in widgets]
        self.widget_refs = widgets

    @property
    def type(self) -> str:
        if self.ft == "/Btn":
            if self.ff & FF_PUSH:
                return "button"
            return "radio" if self.ff & FF_RADIO else "checkbox"
        if self.ft == "/Ch":
            return "combo" if self.ff & FF_COMBO else "list"
        return {"/Tx": "text", "/Sig": "signature"}.get(self.ft, "unknown")

    def widget_states(self, w: DictionaryObject) -> list[str]:
        ap = w.get("/AP")
        if not ap:
            return []
        n = ap.get_object().get("/N")
        if n is None:
            return []
        n = n.get_object()
        if isinstance(n, pypdf.generic.StreamObject):
            return []
        return [str(k)[1:] for k in n.keys() if str(k) != "/Off"]

    def states(self) -> list[str]:
        out: list[str] = []
        for w in self.widgets:
            for s in self.widget_states(w):
                if s not in out:
                    out.append(s)
        return out

    def options(self) -> list[dict]:
        opt = self.inh.get("/Opt")
        if not opt:
            return []
        out = []
        for o in opt.get_object():
            o = o.get_object() if hasattr(o, "get_object") else o
            if isinstance(o, (list, ArrayObject)) and len(o) >= 2:
                out.append({"export": str(o[0]), "label": str(o[1])})
            else:
                out.append({"export": str(o), "label": str(o)})
        return out

    def value(self) -> Any:
        v = self.inh.get("/V")
        if v is None and self.ft == "/Btn":
            for w in self.widgets:
                if str(w.get("/AS", "/Off")) != "/Off":
                    v = w["/AS"]
        if v is None:
            return None
        v = v.get_object() if hasattr(v, "get_object") else v
        if isinstance(v, (list, ArrayObject)):
            return [str(x) for x in v]
        if isinstance(v, NameObject):
            return str(v)[1:]
        return str(v)


def iter_fields(root: DictionaryObject) -> Iterator[Field]:
    af = root.get("/AcroForm")
    if not af:
        return
    af = af.get_object()

    def walk(ref, parent: str, inh: dict, depth: int) -> Iterator[Field]:
        if depth > 50:
            return
        f = ref.get_object()
        t = f.get("/T")
        name = f"{parent}.{t}" if parent and t is not None else (str(t) if t is not None else parent)
        inh = dict(inh)
        for k in ("/FT", "/Ff", "/V", "/Opt", "/MaxLen", "/DA"):
            if k in f:
                inh[k] = f[k]
        kids = list(f.get("/Kids", []) or [])
        field_kids = [k for k in kids if "/T" in k.get_object()]
        if field_kids:
            for k in field_kids:
                yield from walk(k, name, inh, depth + 1)
        else:
            yield Field(name, f, inh, kids if kids else [ref])

    for ref in af.get("/Fields", []) or []:
        yield from walk(ref, "", {}, 0)


def _page_map(pages) -> dict[int, int]:
    m = {}
    for i, p in enumerate(pages):
        for a in p.get("/Annots", []) or []:
            if isinstance(a, IndirectObject):
                m[a.idnum] = i + 1
    return m


def _pages_of(field: Field, pmap: dict[int, int]) -> list[int]:
    out = set()
    for r in field.widget_refs:
        idnum = r.idnum if isinstance(r, IndirectObject) else getattr(
            getattr(r, "indirect_reference", None), "idnum", None)
        if idnum in pmap:
            out.add(pmap[idnum])
    return sorted(out)


def describe(field: Field, pmap: dict[int, int]) -> dict:
    d = {"name": field.name, "type": field.type, "value": field.value(),
         "pages": _pages_of(field, pmap),
         "read_only": bool(field.ff & FF_READONLY), "required": bool(field.ff & FF_REQUIRED)}
    if field.type in ("checkbox", "radio"):
        d["options"] = field.states()
        d["checked"] = d["value"] not in (None, "Off")
    elif field.type in ("combo", "list"):
        d["options"] = [o["label"] if o["label"] == o["export"] else o for o in field.options()]
        d["multi_select"] = bool(field.ff & FF_MULTISELECT)
    elif field.type == "text":
        d["multiline"] = bool(field.ff & FF_MULTILINE)
        if "/MaxLen" in field.inh:
            d["max_length"] = int(field.inh["/MaxLen"])
    return d


@operation(schema("List form fields: name, type, current value, options, pages.",
                  ["path"], path=STR, password=STR))
def form_fields(res: Result, path: str, password: str | None = None) -> None:
    doc = Doc(path, password)
    root = doc.reader.trailer["/Root"].get_object()
    pmap = _page_map(doc.reader.pages)
    fields = [describe(f, pmap) for f in iter_fields(root)]
    if "/AcroForm" in root and "/XFA" in root["/AcroForm"].get_object():
        res.warn("PDF also contains an XFA form; only the AcroForm fields are listed")
    if not fields:
        res.warn("this PDF has no fillable form fields")
    res.details.update({"has_form": bool(fields), "count": len(fields), "fields": fields})


def _parse_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if v is None:
        return False
    s = str(v).strip().lstrip("/").lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _set_button(field: Field, value: Any, res: Result) -> str | None:
    """Returns the state set ('Off' for unchecked) or None if not applied."""
    states = field.states()
    if field.type == "checkbox":
        sval = str(value).strip().lstrip("/") if not isinstance(value, bool) else ""
        named = next((s for s in states if s.lower() == sval.lower()), None) if sval else None
        on = named is not None or _parse_bool(value)
        if on is None:
            res.warn(f"{field.name}: cannot interpret {value!r} as checked/unchecked; "
                     f"use true/false or {states or ['Yes']}")
            return None
        if not states:
            res.warn(f"{field.name}: checkbox has no appearance states; set /V only, "
                     "viewers may not draw the check")
        target = (named or (states[0] if states else "Yes")) if on else "Off"
        field.obj[NameObject("/V")] = NameObject("/" + target)
        for w in field.widgets:
            ws = field.widget_states(w)
            w[NameObject("/AS")] = NameObject("/" + target if (target == "Off" or not ws or target in ws)
                                              else "/" + ws[0])
        return target
    # radio
    if value is None or value is False or (isinstance(value, str) and value.strip().lower() in ("off", "")):
        target = "Off"
    else:
        sval = str(value).strip().lstrip("/")
        target = next((s for s in states if s.lower() == sval.lower()), None)
        if target is None:
            opts = field.options()
            for i, o in enumerate(opts):  # /Opt maps widget order to export values
                if sval.lower() in (o["export"].lower(), o["label"].lower()) and i < len(field.widgets):
                    ws = field.widget_states(field.widgets[i])
                    target = ws[0] if ws else None
                    break
        if target is None:
            res.warn(f"{field.name}: {value!r} is not a radio option; valid: {states}")
            return None
    field.obj[NameObject("/V")] = NameObject("/" + target)
    for w in field.widgets:
        w[NameObject("/AS")] = NameObject("/" + target if target in field.widget_states(w) else "/Off")
    return target


@operation(schema("Fill fields {name: value}. Checkbox: true/false; radio: option name; list: array. Sets NeedAppearances. Reports unknown names.",
                  ["path", "values", "output"], path=STR, values={"type": "object", "minProperties": 1},
                  output=STR, overwrite=BOOL, password=STR))
def fill_form(res: Result, path: str, values: dict, output: str, overwrite: bool = False,
              password: str | None = None) -> None:
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    w = pypdf.PdfWriter(clone_from=doc.reader)
    root = w._root_object
    fields = list(iter_fields(root))
    if not fields:
        raise UserError("this PDF has no fillable form fields", "no_form")
    by_name = {f.name: f for f in fields}
    by_short: dict[str, list[Field]] = {}
    for f in fields:
        by_short.setdefault(f.name.rsplit(".", 1)[-1], []).append(f)

    filled, unknown, skipped, text_updates = [], [], [], {}
    for key, value in values.items():
        f = by_name.get(key)
        if f is None and len(by_short.get(key, [])) == 1:
            f = by_short[key][0]
            res.warn(f"'{key}' matched field '{f.name}' by its short name")
        if f is None:
            close = difflib.get_close_matches(key, list(by_name), n=3, cutoff=0.6)
            unknown.append({"name": key, "did_you_mean": close})
            continue
        if f.ff & FF_READONLY:
            res.warn(f"{f.name}: field is read-only in viewers; value set anyway")
        kind = f.type
        if kind in ("checkbox", "radio"):
            state = _set_button(f, value, res)
            if state is None:
                skipped.append(f.name)
            else:
                filled.append({"name": f.name, "type": kind, "value": state})
        elif kind in ("text", "combo", "list"):
            if isinstance(value, list) and kind != "list":
                res.warn(f"{f.name}: list given for a {kind} field; skipped")
                skipped.append(f.name)
                continue
            v = [str(x) for x in value] if isinstance(value, list) else \
                ("" if value is None else (("true" if value else "false") if isinstance(value, bool) else str(value)))
            if kind == "text" and "/MaxLen" in f.inh and len(v) > int(f.inh["/MaxLen"]):
                res.warn(f"{f.name}: value longer than the field's max length "
                         f"({int(f.inh['/MaxLen'])}); viewers may truncate it")
            if kind in ("combo", "list"):
                opts = {o["export"] for o in f.options()} | {o["label"] for o in f.options()}
                for item in (v if isinstance(v, list) else [v]):
                    if opts and item not in opts and item != "":
                        res.warn(f"{f.name}: {item!r} is not one of the options {sorted(opts)}")
            if isinstance(v, list):
                f.obj[NameObject("/V")] = ArrayObject(TextStringObject(x) for x in v)
            else:
                f.obj[NameObject("/V")] = TextStringObject(v)
            text_updates[f.name] = v
            filled.append({"name": f.name, "type": kind, "value": v})
        else:
            res.warn(f"{f.name}: {kind} fields cannot be filled")
            skipped.append(f.name)

    for u in unknown:
        hint = f" (did you mean {', '.join(u['did_you_mean'])}?)" if u["did_you_mean"] else ""
        res.warn(f"unknown field name: {u['name']!r}{hint}")
    if not filled:
        raise UserError("no fields were filled", "nothing_filled",
                        unknown_fields=unknown, skipped=skipped,
                        available_fields=sorted(by_name)[:200])

    if text_updates:
        try:
            w.update_page_form_field_values(None, text_updates, auto_regenerate=True)
        except Exception as exc:
            res.warn(f"could not generate text appearances ({exc}); relying on NeedAppearances")
    af = root["/AcroForm"].get_object()
    if "/XFA" in af:
        del af["/XFA"]
        res.warn("removed the XFA form so viewers show the AcroForm values")
    w.set_need_appearances_writer(True)
    size = write_pdf(w, out)
    res.changed = True
    doc.note_encryption(res, True)
    res.details.update({"output": str(out), "output_bytes": size, "filled": filled,
                        "unknown_fields": unknown, "skipped": skipped,
                        "need_appearances": True})


def _bbox_after_matrix(bbox, m) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = m
    pts = [(x, y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    tx = [a * x + c * y + e for x, y in pts]
    ty = [b * x + d * y + f for x, y in pts]
    return min(tx), min(ty), max(tx), max(ty)


@operation(schema("Burn form field appearances into page content and remove the fields.",
                  ["path", "output"], path=STR, output=STR, overwrite=BOOL, password=STR))
def flatten_form(res: Result, path: str, output: str, overwrite: bool = False,
                 password: str | None = None) -> None:
    out = check_output(output, overwrite, [path])
    doc = Doc(path, password)
    w = pypdf.PdfWriter(clone_from=doc.reader)
    root = w._root_object
    fields = list(iter_fields(root))
    if not fields:
        raise UserError("this PDF has no form fields to flatten", "no_form")
    af = root["/AcroForm"].get_object()
    need = bool(af.get("/NeedAppearances", False))

    # make sure text/choice values have appearance streams before burning them in
    regen = {}
    for f in fields:
        if f.type in ("text", "combo", "list") and f.value() not in (None, "", []):
            if need or any("/AP" not in wd for wd in f.widgets):
                regen[f.name] = f.value()
    if regen:
        try:
            w.update_page_form_field_values(None, regen, auto_regenerate=False)
        except Exception as exc:
            res.warn(f"could not regenerate some text appearances ({exc})")

    widget_owner = {}
    for f in fields:
        for wd in f.widgets:
            widget_owner[id(wd)] = f

    flattened, lost, k = 0, [], 0
    for pno, page in enumerate(w.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        annots = annots.get_object()
        keep, ops = ArrayObject(), []
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else None
        if resources is None:
            resources = DictionaryObject()
            page[NameObject("/Resources")] = resources
        xobjs = resources.get("/XObject")
        if xobjs is None:
            xobjs = DictionaryObject()
            resources[NameObject("/XObject")] = xobjs
        xobjs = xobjs.get_object()
        for aref in annots:
            a = aref.get_object()
            if a.get("/Subtype") != "/Widget":
                keep.append(aref)
                continue
            owner = widget_owner.get(id(a))
            name = owner.name if owner else str(a.get("/T", "?"))
            if int(a.get("/F", 0) or 0) & (F_HIDDEN | F_NOVIEW):
                continue
            ap = a.get("/AP")
            stream_ref = None
            if ap is not None:
                n = ap.get_object().get("/N")
                if n is not None:
                    n_obj = n.get_object()
                    if isinstance(n_obj, pypdf.generic.StreamObject):
                        stream_ref = n
                    else:
                        stream_ref = n_obj.get(a.get("/AS", "/Off"))
            if stream_ref is None:
                if owner and owner.value() not in (None, "", "Off", []):
                    lost.append(name)
                continue
            stream = stream_ref.get_object()
            if not isinstance(stream_ref, IndirectObject):
                stream_ref = w._add_object(stream)
            bbox = [float(x) for x in stream.get("/BBox", [0, 0, 0, 0])]
            mtx = [float(x) for x in stream.get("/Matrix", [1, 0, 0, 1, 0, 0])]
            rect = [float(x) for x in a["/Rect"]]
            rx0, ry0, rx1, ry1 = min(rect[0], rect[2]), min(rect[1], rect[3]), \
                max(rect[0], rect[2]), max(rect[1], rect[3])
            tx0, ty0, tx1, ty1 = _bbox_after_matrix(bbox, mtx)
            if tx1 - tx0 <= 0 or ty1 - ty0 <= 0:
                continue
            sx, sy = (rx1 - rx0) / (tx1 - tx0), (ry1 - ry0) / (ty1 - ty0)
            k += 1
            xname = f"/PdfopsFlat{k}"
            xobjs[NameObject(xname)] = stream_ref
            ops.append(f"q {sx:.6f} 0 0 {sy:.6f} {rx0 - tx0 * sx:.4f} {ry0 - ty0 * sy:.4f} cm "
                       f"{xname} Do Q")
            flattened += 1
        if ops:
            pre = DecodedStreamObject()
            pre.set_data(b"q\n")
            post = DecodedStreamObject()
            post.set_data(("\nQ\n" + "\n".join(ops) + "\n").encode("latin-1"))
            contents = page.get("/Contents")
            arr = ArrayObject([w._add_object(pre)])
            if contents is not None:
                c = contents.get_object()
                if isinstance(c, ArrayObject):
                    arr.extend(c)
                else:
                    arr.append(contents if isinstance(contents, IndirectObject) else w._add_object(c))
            arr.append(w._add_object(post))
            page[NameObject("/Contents")] = arr
        if len(keep):
            page[NameObject("/Annots")] = keep
        elif "/Annots" in page:
            del page["/Annots"]
    del root["/AcroForm"]
    for name in lost:
        res.warn(f"{name}: has a value but no appearance stream; it is not visible after flattening")
    size = write_pdf(w, out)
    res.changed = True
    doc.note_encryption(res, True)
    res.details.update({"output": str(out), "output_bytes": size, "fields": len(fields),
                        "widgets_flattened": flattened, "values_not_visible": lost})
