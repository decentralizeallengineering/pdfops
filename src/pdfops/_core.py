"""Shared plumbing: report envelope, operation registry, safe I/O, page specs."""

from __future__ import annotations

import functools
import inspect
import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

import jsonschema
import pypdf
from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError

SCHEMAS: dict[str, dict] = {}
OPERATIONS: dict[str, Callable[..., dict]] = {}

SCHEMA_MAX_CHARS = 600
MAX_LIB_WARNINGS = 20


class UserError(Exception):
    """A problem with the caller's input or file; becomes an ok=False report."""

    def __init__(self, message: str, code: str = "invalid_input", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


class Result:
    """Mutable report builder handed to every operation."""

    def __init__(self) -> None:
        self.changed = False
        self.warnings: list[str] = []
        self.details: dict[str, Any] = {}

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _jsonable(value: Any) -> Any:
    """Coerce anything that slipped through into JSON-safe primitives."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):  # numpy scalars
        return _jsonable(value.item())
    return str(value)


class _LibLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = f"pypdf: {record.getMessage()}"
        if msg not in self.messages and len(self.messages) < MAX_LIB_WARNINGS:
            self.messages.append(msg)


def operation(schema: dict) -> Callable[[Callable[..., None]], Callable[..., dict]]:
    """Register an operation.

    The wrapped function receives a ``Result`` as its first argument and fills it.
    The public function validates arguments against ``schema`` and always returns
    a JSON-serializable ``{"ok", "changed", "warnings", "details"}`` report.
    """
    compact = json.dumps(schema, separators=(",", ":"))
    if len(compact) > SCHEMA_MAX_CHARS:
        raise AssertionError(f"schema too long ({len(compact)} chars)")
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)

    def decorate(func: Callable[..., None]) -> Callable[..., dict]:
        name = func.__name__
        sig = inspect.signature(func)
        public_params = list(sig.parameters.values())[1:]
        public_sig = sig.replace(parameters=public_params, return_annotation=dict)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> dict:
            res = Result()
            capture = _LibLogCapture()
            pypdf_logger = logging.getLogger("pypdf")
            pypdf_logger.addHandler(capture)
            ok = True
            try:
                try:
                    bound = public_sig.bind(*args, **kwargs)
                except TypeError as exc:
                    raise UserError(f"bad arguments: {exc}") from None
                supplied = {k: v for k, v in bound.arguments.items() if v is not None}
                errors = sorted(validator.iter_errors(_jsonable(supplied)), key=lambda e: list(e.path))
                if errors:
                    e = errors[0]
                    where = ".".join(str(p) for p in e.path) or "arguments"
                    raise UserError(f"{where}: {e.message}")
                bound.apply_defaults()
                func(res, **bound.arguments)
            except UserError as exc:
                ok = False
                res.details = {"error": str(exc), "error_type": exc.code, **exc.extra}
            except FileNotDecryptedError:
                ok = False
                res.details = {"error": "PDF is encrypted; supply the correct password",
                               "error_type": "encrypted"}
            except DependencyError as exc:
                ok = False
                res.details = {"error": f"cannot process this PDF: {exc}", "error_type": "unsupported"}
            except PdfReadError as exc:
                ok = False
                res.details = {"error": f"could not read PDF: {exc}", "error_type": "corrupt_pdf"}
            except MemoryError:
                ok = False
                res.details = {"error": "out of memory", "error_type": "resource"}
            except Exception as exc:  # never raise to the agent
                ok = False
                res.details = {"error": f"{type(exc).__name__}: {exc}", "error_type": "internal_error"}
            finally:
                pypdf_logger.removeHandler(capture)
            if not ok:
                res.changed = False
            for m in capture.messages:
                res.warn(m)
            report = {"ok": ok, "changed": res.changed, "warnings": res.warnings,
                      "details": res.details}
            return _jsonable(report)

        wrapper.__signature__ = public_sig  # type: ignore[attr-defined]
        SCHEMAS[name] = schema
        OPERATIONS[name] = wrapper
        return wrapper

    return decorate


# --------------------------------------------------------------------------- I/O


def read_bytes(path: str) -> bytes:
    p = Path(path).expanduser()
    if not p.exists():
        raise UserError(f"file not found: {path}", "not_found")
    if not p.is_file():
        raise UserError(f"not a file: {path}", "not_found")
    data = p.read_bytes()
    if not data.lstrip()[:5].startswith(b"%PDF") and b"%PDF" not in data[:1024]:
        raise UserError(f"not a PDF (no %PDF header): {path}", "not_pdf")
    return data


class Doc:
    """An opened PDF: pypdf reader plus decrypted bytes for pdfplumber."""

    def __init__(self, path: str, password: str | None = None, *, require: bool = True):
        self.path = path
        self.raw = read_bytes(path)
        try:
            self.reader = pypdf.PdfReader(io.BytesIO(self.raw))
        except PdfReadError as exc:
            raise UserError(f"could not read PDF {path}: {exc}", "corrupt_pdf") from None
        self.encrypted = bool(self.reader.is_encrypted)
        self.decrypted = not self.encrypted
        self.used_password = False
        self.decrypt_error: str | None = None
        if self.encrypted:
            self._decrypt(password)
            if require and not self.decrypted:
                raise UserError(
                    f"{path} is encrypted and could not be opened: {self.decrypt_error}",
                    "encrypted", encrypted=True)
        self._plain: bytes | None = None

    def _decrypt(self, password: str | None) -> None:
        tries = [password] if password else []
        tries.append("")
        for pw in tries:
            try:
                if self.reader.decrypt(pw) != pypdf.PasswordType.NOT_DECRYPTED:
                    self.decrypted = True
                    self.used_password = bool(pw)
                    return
            except DependencyError as exc:
                self.decrypt_error = f"encryption method not supported here ({exc})"
                return
            except Exception as exc:
                self.decrypt_error = f"decryption failed ({type(exc).__name__}: {exc})"
                return
        self.decrypt_error = ("wrong password" if password else
                              "a password is required (none supplied)")

    @property
    def n(self) -> int:
        return len(self.reader.pages)

    def plain_bytes(self) -> bytes:
        """Unencrypted bytes suitable for pdfplumber/pdfminer."""
        if self._plain is None:
            if not self.encrypted:
                self._plain = self.raw
            else:
                w = pypdf.PdfWriter(clone_from=self.reader)
                buf = io.BytesIO()
                w.write(buf)
                self._plain = buf.getvalue()
        return self._plain

    def plumber(self):
        import pdfplumber
        return pdfplumber.open(io.BytesIO(self.plain_bytes()))

    def note_encryption(self, res: Result, writes_output: bool) -> None:
        if self.encrypted and writes_output:
            res.warn("source PDF was encrypted; the output is written WITHOUT encryption")


def check_output(output: str, overwrite: bool, inputs: list[str] = ()) -> Path:
    out = Path(output).expanduser()
    if out.suffix.lower() != ".pdf":
        raise UserError(f"output must end in .pdf: {output}")
    _check_target(out, overwrite, inputs)
    return out


def _check_target(out: Path, overwrite: bool, inputs: list[str] = ()) -> None:
    if out.exists() and out.is_dir():
        raise UserError(f"output is a directory: {out}")
    if out.exists() and not overwrite:
        raise UserError(f"output exists (pass overwrite=true to replace): {out}", "output_exists")
    if not out.parent.exists():
        raise UserError(f"output folder does not exist: {out.parent}", "not_found")
    for src in inputs:
        try:
            same = out.exists() and Path(src).expanduser().resolve() == out.resolve()
        except OSError:
            same = False
        if same and not overwrite:
            raise UserError("output would overwrite an input; pass overwrite=true", "output_exists")


def atomic_write(out: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".pdfops-", suffix=out.suffix, dir=str(out.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_pdf(writer: pypdf.PdfWriter, out: Path) -> int:
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    atomic_write(out, data)
    return len(data)


def copy_metadata(reader: pypdf.PdfReader, writer: pypdf.PdfWriter) -> None:
    try:
        meta = reader.metadata
        if meta:
            writer.add_metadata({k: v for k, v in meta.items() if isinstance(k, str)})
    except Exception:
        pass


# ------------------------------------------------------------------ page specs

_RANGE = re.compile(r"^(\d+|last)?\s*-\s*(\d+|last)?$")


def parse_pages(spec: Any, n: int, *, allow_dupes: bool = True) -> list[int]:
    """1-based page spec -> 0-based indices.

    Accepts a list of ints, or a string like ``"1-3,5,8-,last"``.
    ``None`` means all pages.
    """
    if spec is None:
        return list(range(n))
    out: list[int] = []

    def num(tok: str) -> int:
        v = n if tok == "last" else int(tok)
        if v < 1 or v > n:
            raise UserError(f"page {tok} is out of range (document has {n} pages)", "bad_pages")
        return v

    if isinstance(spec, int) and not isinstance(spec, bool):
        spec = [spec]
    if isinstance(spec, list):
        for v in spec:
            if not isinstance(v, int) or isinstance(v, bool):
                raise UserError(f"page numbers must be integers, got {v!r}", "bad_pages")
            out.append(num(str(v)) - 1)
    elif isinstance(spec, str):
        parts = [p.strip().lower() for p in spec.split(",")]
        if not any(parts):
            raise UserError("empty page spec", "bad_pages")
        for part in parts:
            if not part:
                continue
            if part.isdigit() or part == "last":
                out.append(num(part) - 1)
                continue
            m = _RANGE.match(part)
            if not m or (m.group(1) is None and m.group(2) is None):
                raise UserError(f"cannot parse page spec {part!r}; use e.g. '1-3,5,8-,last'", "bad_pages")
            a = num(m.group(1)) if m.group(1) else 1
            b = num(m.group(2)) if m.group(2) else n
            if a > b:
                raise UserError(f"descending range {part!r}", "bad_pages")
            out.extend(range(a - 1, b))
    else:
        raise UserError("pages must be a list of integers or a string like '1-3,5'", "bad_pages")
    if not allow_dupes:
        seen: set[int] = set()
        out = [i for i in out if not (i in seen or seen.add(i))]
    return out


def ranges_text(indices: list[int]) -> str:
    """0-based indices -> compact 1-based text, e.g. '1-3,7'."""
    if not indices:
        return ""
    parts, start, prev = [], indices[0], indices[0]
    for i in indices[1:] + [None]:  # type: ignore[list-item]
        if i is not None and i == prev + 1:
            prev = i
            continue
        parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
        if i is not None:
            start = prev = i
    return ",".join(parts)


# -------------------------------------------------------------- schema helpers

PAGES = {"type": ["string", "array", "integer"], "items": {"type": "integer"}}
STR = {"type": "string"}
BOOL = {"type": "boolean"}


def schema(desc: str, required: list[str], **props: Any) -> dict:
    return {"type": "object", "description": desc, "properties": props,
            "required": required, "additionalProperties": False}
