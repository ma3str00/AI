"""Reject anything except a single read of ntc_ssl_logs, and cap the result size."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.schema import DATABASE, TABLE

TIME_COLUMNS = {"start_time", "end_time", "recv_time"}
POINT_COLUMNS = {"id", "log_id"}
BANNED_FUNCTIONS = {
    "sleep",
    "benchmark",
    "load_file",
    "updatexml",
    "extractvalue",
    "get_lock",
    "release_lock",
    "is_free_lock",
    "user",
    "system_user",
    "session_user",
    "current_user",
}


class GuardError(Exception):
    pass


def prepare(sql: str, max_rows: int) -> str:
    text = _strip(sql)
    try:
        statements = [item for item in sqlglot.parse(text, read="mysql") if item is not None]
    except sqlglot.errors.ParseError as exc:
        raise GuardError(f"SQL could not be parsed: {exc}") from exc
    if len(statements) != 1:
        raise GuardError("Only one SELECT statement is allowed.")
    expr = statements[0]
    if not isinstance(expr, exp.Select):
        raise GuardError("Only a single SELECT is allowed.")
    if expr.args.get("with") is not None or expr.find(exp.With):
        raise GuardError("WITH queries are not allowed.")
    if list(expr.find_all(exp.Join)) or list(expr.find_all(exp.Union)):
        raise GuardError("JOIN and UNION are not allowed.")
    if expr.find(exp.Into) or any(word in text.lower() for word in ("outfile", "dumpfile", "for update")):
        raise GuardError("This query is not read-only.")

    tables = list(expr.find_all(exp.Table))
    if not tables:
        raise GuardError(f"The query must read {TABLE}.")
    for table in tables:
        if table.name.lower() != TABLE:
            raise GuardError(f"Only {TABLE} can be queried.")
        schema = (table.db or "").lower()
        if schema and schema != DATABASE:
            raise GuardError(f"Only the {DATABASE} database can be queried.")

    for fn in expr.find_all(exp.Anonymous):
        if fn.name.lower() in BANNED_FUNCTIONS:
            raise GuardError(f"Function {fn.name} is not allowed.")
    for fn in expr.find_all(exp.Func):
        if not isinstance(fn, exp.Anonymous) and fn.sql_name().lower() in BANNED_FUNCTIONS:
            raise GuardError(f"Function {fn.sql_name()} is not allowed.")

    where = expr.args.get("where")
    if where is None:
        raise GuardError("A WHERE clause is required so the full table is not scanned.")
    columns = {col.name.lower() for col in where.find_all(exp.Column)}
    if not (columns & TIME_COLUMNS or columns & POINT_COLUMNS):
        raise GuardError(
            "Filter on start_time, end_time, recv_time, id, or log_id. "
            "A question with no time range is not run."
        )

    limit = expr.args.get("limit")
    if limit is None:
        return f"{text}\nLIMIT {int(max_rows)}"
    value = limit.args.get("expression")
    if isinstance(value, exp.Literal) and not value.is_string and int(value.this) > max_rows:
        expr.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return expr.sql(dialect="mysql")
    return text


def _strip(sql: str) -> str:
    text = sql.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if text.endswith(";"):
        text = text[:-1].strip()
    return text
