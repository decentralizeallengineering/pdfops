"""pdfops: reliable PDF operations for LLM agents.

Every public operation is a plain function returning a JSON-serializable report::

    {"ok": bool, "changed": bool, "warnings": [str, ...], "details": {...}}

and never raises for user errors. ``SCHEMAS[name]`` holds each operation's JSON
Schema; ``run(name, args)`` validates and dispatches by name.
"""

from __future__ import annotations

from ._core import OPERATIONS, SCHEMAS
from .compose_ops import letter, replace_name_overlay, summary_note
from .form_ops import fill_form, flatten_form, form_fields
from .inspect_ops import extract_text, info, scanned_pages, search
from .page_ops import (append_pages, delete_pages, drop_blank_pages, extract_pages,
                       insert_appendix, merge, rotate, split)
from .table_ops import tables, tables_to_csv, tables_to_xlsx

__version__ = "0.1.0"


def run(name: str, args: dict | None = None) -> dict:
    """Call an operation by name with a dict of arguments (agent tool-call entry point)."""
    op = OPERATIONS.get(name)
    if op is None:
        return {"ok": False, "changed": False, "warnings": [],
                "details": {"error": f"unknown operation {name!r}", "error_type": "unknown_operation",
                            "available": sorted(OPERATIONS)}}
    if args is not None and not isinstance(args, dict):
        return {"ok": False, "changed": False, "warnings": [],
                "details": {"error": "args must be an object", "error_type": "invalid_input"}}
    return op(**(args or {}))


__all__ = sorted(OPERATIONS) + ["OPERATIONS", "SCHEMAS", "run", "__version__"]
