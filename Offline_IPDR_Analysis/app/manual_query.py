"""Structured (non-LLM) read queries against ntc_ssl_log for the Query Builder tab."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schema import DEAD_COLUMNS, TABLE, USEFUL_COLUMNS

_SAFE_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
_TIME_COLUMNS = {"start_time", "end_time", "recv_time"}
_DEFAULT_COLUMNS = (
    "start_time",
    "subscriber_id",
    "app_label",
    "app_category",
    "client_ip",
    "requested_server",
    "client_asn",
    "loc",
    "c2s_byte_num",
    "s2c_byte_num",
    "duration",
    "block_type",
)
_OPS = {
    "eq": "=",
    "ne": "<>",
    "contains": "LIKE",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    kind: str  # time | text | number
    label: str
    filterable: bool
    default_selected: bool


def catalog() -> list[ColumnInfo]:
    """Useful columns only — dead/constant fields are omitted."""
    items: list[ColumnInfo] = []
    for line in USEFUL_COLUMNS.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, rest = line.partition(" ")
        name = name.strip().lower()
        if not name or name in DEAD_COLUMNS or not _SAFE_IDENT.match(name):
            continue
        kind = "time" if name in _TIME_COLUMNS else _kind_from_type(rest)
        items.append(
            ColumnInfo(
                name=name,
                kind=kind,
                label=name.replace("_", " "),
                filterable=name not in {"id"},
                default_selected=name in _DEFAULT_COLUMNS,
            )
        )
    return items


def schema_payload() -> dict:
    columns = catalog()
    return {
        "table": TABLE,
        "columns": [
            {
                "name": col.name,
                "kind": col.kind,
                "label": col.label,
                "filterable": col.filterable,
                "default_selected": col.default_selected,
            }
            for col in columns
        ],
        "default_columns": [col.name for col in columns if col.default_selected],
        "time_column": "start_time",
    }


class ManualQueryError(ValueError):
    pass


def build_select(
    *,
    columns: list[str],
    time_from: str,
    time_to: str,
    filters: list[dict],
    max_rows: int,
) -> str:
    """Build a single read-only SELECT. time_from/time_to are 'YYYY-MM-DD HH:MM:SS' GMT+5 wall clocks."""
    allowed = {col.name: col for col in catalog()}
    selected = _validate_columns(columns, allowed)
    start = _validate_wall_clock(time_from, "time_from")
    end = _validate_wall_clock(time_to, "time_to")
    if start >= end:
        raise ManualQueryError("time_from must be earlier than time_to.")
    where = [
        f"start_time >= UNIX_TIMESTAMP('{start}')",
        f"start_time < UNIX_TIMESTAMP('{end}')",
    ]
    for item in filters or []:
        clause = _filter_clause(item, allowed)
        if clause:
            where.append(clause)
    limit = max(1, min(int(max_rows), 5000))
    return (
        f"SELECT {', '.join(selected)} FROM {TABLE} "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY start_time DESC LIMIT {limit}"
    )


def _kind_from_type(rest: str) -> str:
    folded = rest.lower()
    if any(token in folded for token in ("bigint", "int", "float", "double", "decimal")):
        return "number"
    return "text"


def _validate_columns(columns: list[str], allowed: dict[str, ColumnInfo]) -> list[str]:
    if not columns:
        raise ManualQueryError("Select at least one output column.")
    selected: list[str] = []
    seen: set[str] = set()
    for raw in columns:
        name = str(raw or "").strip().lower()
        if name in DEAD_COLUMNS:
            raise ManualQueryError(f"Column {name} is not available.")
        if name not in allowed or not _SAFE_IDENT.match(name):
            raise ManualQueryError(f"Unknown column: {name}")
        if name in seen:
            continue
        selected.append(name)
        seen.add(name)
    return selected


def _validate_wall_clock(value: str, label: str) -> str:
    text = (value or "").strip().replace("T", " ")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", text):
        raise ManualQueryError(f"{label} must look like YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS.")
    if len(text) == 16:
        text += ":00"
    return text


def _filter_clause(item: dict, allowed: dict[str, ColumnInfo]) -> str | None:
    name = str(item.get("column") or "").strip().lower()
    op = str(item.get("op") or "eq").strip().lower()
    value = item.get("value")
    if value is None or str(value).strip() == "":
        return None
    if name in DEAD_COLUMNS or name not in allowed:
        raise ManualQueryError(f"Cannot filter on {name or '(empty)'}.")
    if name == "start_time":
        raise ManualQueryError("Use the required time range for start_time.")
    col = allowed[name]
    if op not in _OPS:
        raise ManualQueryError(f"Unsupported operator: {op}")
    if op == "contains":
        if col.kind == "number":
            raise ManualQueryError(f"contains is not valid for {name}.")
        return f"LOWER({name}) LIKE '%{_escape_like(str(value).lower())}%'"
    if col.kind == "number" or name in _TIME_COLUMNS:
        number = _as_number(value, name)
        return f"{name} {_OPS[op]} {number}"
    if op in {"gt", "gte", "lt", "lte"}:
        raise ManualQueryError(f"{op} is only valid for numeric columns.")
    literal = _escape_literal(str(value))
    if op == "eq":
        return f"LOWER({name}) = LOWER('{literal}')"
    if op == "ne":
        return f"LOWER({name}) <> LOWER('{literal}')"
    raise ManualQueryError(f"Unsupported operator: {op}")


def _as_number(value, name: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        raise ManualQueryError(f"{name} filter must be numeric.")
    return text


def _escape_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _escape_like(value: str) -> str:
    return _escape_literal(value).replace("%", r"\%").replace("_", r"\_")
