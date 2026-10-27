# pdfops

Reliable PDF operations for LLM agents. Each operation is a plain Python function that
**never raises for user errors** and returns a JSON-serializable report:

```json
{"ok": true, "changed": true, "warnings": ["..."], "details": {"output": "out.pdf", "...": "..."}}
```

- `ok`: whether the requested action was performed. When it is `false`, `details.error`
  holds a message and `details.error_type` holds a stable code (`not_found`, `not_pdf`,
  `encrypted`, `bad_pages`, `output_exists`, `invalid_input`, `no_form`, `nothing_filled`,
  `no_tables`, `not_found_in_document`, `too_long`, `all_blank`, `corrupt_pdf`,
  `unsupported`, `internal_error`).
- `changed`: whether a file was written.
- `warnings`: things the agent should tell the user, such as scanned pages or unknown form
  fields. Warnings logged by pypdf are captured here as well (up to 20).
- `details`: operation-specific results.

Each operation also has a JSON Schema (≤ 600 characters) in `pdfops.SCHEMAS[name]`.
Arguments are validated against it before anything runs. `pdfops.run(name, args)`
dispatches by name, so it can sit directly behind a tool-call handler:

```python
import pdfops

tools = [{"name": n, "description": s["description"], "input_schema": s}
         for n, s in pdfops.SCHEMAS.items()]
report = pdfops.run("search", {"path": "report.pdf", "terms": ["invoice"]})
```

Requirements: Python 3.12+ on Windows or Linux. Runtime dependencies are pypdf, pdfplumber,
fpdf2, openpyxl, jsonschema, and numpy. pdfops makes no network calls, sends no telemetry,
and starts no subprocesses (a test enforces this). Tested with pypdf 6.17, pdfplumber
0.11.10, fpdf2 2.8.9, and openpyxl 3.1.5.

## Operations

| Operation | What it does |
|---|---|
| `info` | Page count, displayed page sizes (pt, inches, and name such as Letter/A4), and a per-page kind (`text`, `scanned`, `scanned_with_text`, `graphics`, `empty`). Also form or not, encryption state, and metadata. |
| `extract_text` | Text per page in `plain` or `layout` mode (layout keeps columns and spacing). Optional `max_chars` cap. |
| `search` | Literal terms (case-insensitive by default, matching across line breaks) or regex. Returns page numbers, snippets, and counts. |
| `extract_pages` | Copies the chosen pages, in the given order, into a new PDF. Repeats are allowed. |
| `delete_pages` | Removes pages. Refuses to delete every page. |
| `split` | Splits by `ranges`, `every` N pages, or `halves`. Writes one file per part. |
| `merge` | Merges in order. Each input can be a path or `{path, pages, bookmark, password}`. `bookmarks: true` adds one bookmark per source. |
| `append_pages` | Inserts pages from another PDF at the end, or before page `at`. |
| `insert_appendix` | Appends a PDF behind a generated title separator page (sized like the last page) and adds a bookmark. |
| `rotate` | Rotates by 90, 180, 270, or -90 degrees, relative to the current rotation. |
| `form_fields` | Lists fields: name, type (text/checkbox/radio/combo/list/button/signature), value, options, pages, read-only, required. |
| `fill_form` | Sets values and `NeedAppearances` and generates text appearances. Checkboxes accept true/false; radio groups take an option name. Unknown names are reported with "did you mean" suggestions. |
| `flatten_form` | Burns widget appearances into page content and removes the form. Reports any value that has no appearance. |
| `tables` | Extracts tables with pdfplumber. Detects headers and merges a table that continues onto the next page, dropping a repeated header. |
| `tables_to_csv` | Writes one UTF-8 CSV per table (with BOM, so Excel opens it correctly). |
| `tables_to_xlsx` | Writes one sheet per table. Numbers are stored as numbers (`1,234.5`, `$12`, `(3)`, `12%`); codes with leading zeros such as `00123` stay text. |
| `scanned_pages` | Lists image-only pages that have no text layer. |
| `drop_blank_pages` | Removes pages with no text that render (near-)white, which includes blank scans. `dry_run` only lists them. |
| `summary_note` | Adds a cover or end page with text (fpdf2), at the size of the adjacent page. |
| `replace_name_overlay` | Puts a white box over each match and writes the new text at the matched font size. Reports every place it could not replace. |
| `letter` | Builds a one-page letter from `fields`. The font shrinks to fit, down to 8 pt; past that it refuses. |

### Conventions

- **Pages** are 1-based: a list `[1, 3]` or a string `"1-3,5,8-,last"`.
- **Outputs are never overwritten silently.** An existing output, or an output equal to an
  input, needs `overwrite: true`. Files are written atomically through a temp file and
  `os.replace`, and inputs are read fully into memory first, so in-place edits are safe.
- **Encryption.** `info` reports an encrypted file cleanly (`encrypted`, `opened`,
  `open_error`) and does not fail. Other operations fail with `error_type: "encrypted"`
  unless the right `password` is given. Files with an empty user password open
  automatically. Any output made from an encrypted source is **not** encrypted, and the
  report warns about it.
- **No-ops.** When there is nothing to do (no blank pages, for example), no file is written
  and `details.output` is `null`.

## Honesty rules

- pdfops **never performs OCR**. Image-only pages are reported as `scanned` with no text
  layer, and `extract_text`, `search`, `tables`, and `replace_name_overlay` say so
  explicitly. They do not return empty results silently.
- `replace_name_overlay` is a **visual overlay, not a redaction**. The original text stays
  in the text layer and can still be copied or extracted, and every report says so. Pages
  that are rotated or have an offset media box are skipped and listed under `not_replaced`.
- Text drawn with fpdf2 uses core Helvetica (Latin-1). Smart quotes and dashes are
  transliterated. For other scripts, a system Unicode font (DejaVu, Liberation, Arial, Noto)
  is used if one is installed; otherwise the characters are replaced with `?` and a warning
  is added.

## Development

```bash
pip install -e .[test]
pytest
```

The tests build all of their fixtures with fpdf2: a three-page text file, a ruled table
spanning two pages, a checkbox/radio/combo form (fields added with pypdf), an RC4-encrypted
file, and a scanned page made from a generated PNG. CI runs on Ubuntu and Windows with
Python 3.12.

License: Apache-2.0.
