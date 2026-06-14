"""
ingest.py — turn an uploaded file into (a) a human-readable summary and
(b) a RIGOR-format schema JSON the pipeline can consume.

Supports two input kinds:
  - CSV  : types are inferred from the data; a schema JSON is synthesised.
  - JSON : assumed to already be a RIGOR/SQL schema; summarised as-is.

Stdlib only (csv, json, re) so it has no heavy dependencies and is easy to test.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")
_BOOL_VALUES = {"true", "false"}
_BOOL_VALUES_YN = {"yes", "no"}


def detect_input_kind(path: str | Path) -> str:
    """Return 'csv' or 'schema_json' from extension/content."""
    p = Path(path)
    if p.suffix.lower() in {".csv", ".tsv"}:
        return "csv"
    if p.suffix.lower() == ".json":
        return "schema_json"
    # Fallback: sniff first non-space char.
    head = p.read_text(encoding="utf-8", errors="replace").lstrip()[:1]
    return "schema_json" if head in "{[" else "csv"


def _read_csv_rows(path: str | Path, max_rows: int | None = None) -> Tuple[List[str], List[List[str]]]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    sample = raw[:4096]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        pass
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    body = rows[1:]
    if max_rows is not None:
        body = body[:max_rows]
    return header, body


def _classify_column(values: List[str]) -> str:
    """Infer a SQL type name recognised by app.py's SQL_TO_XSD map."""
    non_empty = [v.strip() for v in values if v is not None and v.strip() != ""]
    if not non_empty:
        return "TEXT"
    low = [v.lower() for v in non_empty]

    if all(v in _BOOL_VALUES for v in low) or all(v in _BOOL_VALUES_YN for v in low):
        return "BOOLEAN"

    def is_int(v: str) -> bool:
        try:
            int(v)
            return True
        except ValueError:
            return False

    def is_float(v: str) -> bool:
        try:
            float(v)
            return True
        except ValueError:
            return False

    if all(is_int(v) for v in non_empty):
        return "INTEGER"
    if all(is_float(v) for v in non_empty):
        return "FLOAT"
    if all(_DATETIME_RE.match(v) for v in non_empty):
        return "DATETIME"
    if all(_DATE_RE.match(v) for v in non_empty):
        return "DATE"
    return "TEXT"


def _table_name_from_path(path: str | Path) -> str:
    stem = Path(path).stem
    name = re.sub(r"[^A-Za-z0-9_]", "_", stem).strip("_").lower()
    return name or "table"


def _infer_primary_key(table_name: str, columns: Dict[str, str], header: List[str],
                       col_values: Dict[str, List[str]]) -> str | None:
    candidates = ["id", f"{table_name}_id", f"{table_name.rstrip('s')}_id"]
    lower_map = {c.lower(): c for c in header}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    # First integer column whose non-empty values are all unique.
    for col in header:
        if columns.get(col) == "INTEGER":
            vals = [v.strip() for v in col_values[col] if v.strip() != ""]
            if vals and len(set(vals)) == len(vals):
                return col
    return None


def summarize_csv(path: str | Path, preview_rows: int = 5) -> Dict[str, Any]:
    header, body = _read_csv_rows(path)
    if not header:
        return {"kind": "csv", "error": "Empty or unreadable CSV", "warnings": ["File has no rows"]}

    col_values = {c: [] for c in header}
    for row in body:
        for i, c in enumerate(header):
            col_values[c].append(row[i] if i < len(row) else "")

    columns = {c: _classify_column(col_values[c]) for c in header}
    missing = {c: sum(1 for v in col_values[c] if v is None or v.strip() == "") for c in header}
    table_name = _table_name_from_path(path)
    pk = _infer_primary_key(table_name, columns, header, col_values)

    warnings: List[str] = []
    if pk is None:
        warnings.append("No primary key could be inferred (no 'id'/'<table>_id' and no unique integer column).")
    for c in header:
        if missing[c] == len(body) and body:
            warnings.append(f"Column '{c}' is entirely empty.")
        elif missing[c] > 0:
            warnings.append(f"Column '{c}' has {missing[c]} missing value(s).")
    dup_headers = [h for h in set(header) if header.count(h) > 1]
    if dup_headers:
        warnings.append(f"Duplicate column name(s): {', '.join(dup_headers)}.")
    if "" in header:
        warnings.append("One or more columns have empty names.")

    return {
        "kind": "csv",
        "table_name": table_name,
        "row_count": len(body),
        "column_count": len(header),
        "columns": [{"name": c, "type": columns[c], "missing": missing[c],
                     "primary_key": (c == pk)} for c in header],
        "primary_key": pk,
        "preview": [dict(zip(header, (row + [""] * len(header))[:len(header)])) for row in body[:preview_rows]],
        "warnings": warnings,
    }


def infer_schema_from_csv(path: str | Path) -> Dict[str, Any]:
    """Build a RIGOR-format schema JSON (single table) from a CSV file."""
    s = summarize_csv(path)
    if s.get("error"):
        raise ValueError(s["error"])
    pk = s["primary_key"]
    columns: Dict[str, Any] = {}
    for col in s["columns"]:
        meta: Dict[str, Any] = {"type": col["type"]}
        if col["name"] == pk:
            meta["primary_key"] = True
            meta["not_null"] = True
        columns[col["name"]] = meta
    return {s["table_name"]: {"columns": columns, "foreign_keys": []}}


def summarize_schema_json(path: str | Path) -> Dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    tables = []
    warnings: List[str] = []
    for tname, tval in raw.items():
        if isinstance(tval, dict) and "columns" in tval:
            cols = tval.get("columns", {})
            fks = tval.get("foreign_keys", [])
        else:
            cols = tval
            fks = []
        col_list = []
        has_pk = False
        for cname, cval in cols.items():
            meta = cval if isinstance(cval, dict) else {"type": cval}
            is_pk = bool(meta.get("primary_key") or meta.get("pk"))
            has_pk = has_pk or is_pk
            col_list.append({
                "name": cname,
                "type": meta.get("type", meta.get("datatype", "TEXT")),
                "primary_key": is_pk,
            })
        if not has_pk:
            warnings.append(f"Table '{tname}' has no declared primary key.")
        tables.append({"name": tname, "columns": col_list, "foreign_keys": fks})
    return {
        "kind": "schema_json",
        "table_count": len(tables),
        "tables": tables,
        "warnings": warnings,
    }


def prepare_schema_file(path: str | Path, work_dir: str | Path) -> Tuple[Path, str]:
    """
    Return (schema_json_path, kind). For CSV, a schema is inferred and written to
    work_dir; for JSON, the original path is returned unchanged.
    """
    kind = detect_input_kind(path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if kind == "csv":
        schema = infer_schema_from_csv(path)
        out = work_dir / "inferred_schema.json"
        out.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        return out, "csv"
    return Path(path), "schema_json"
