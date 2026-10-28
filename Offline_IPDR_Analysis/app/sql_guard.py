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


def wants_full_table(question: str) -> bool:
    text = question.lower()
    explicit = (
        "no time filter",
        "without a time",
        "without any time",
        "do not apply any time",
        "do not apply a time",
        "don't apply any time",
        "don't apply a time",
        "do not use a time",
        "do not filter by time",
    )
    if any(marker in text for marker in explicit):
        return True
    whole_table = ("all the data", "all data", "entire table", "whole table", "all history", "every row", "check all")
    has_range = any(word in text for word in ("yesterday", "today", "last ", "hour", "between", "since", "this week"))
    return any(marker in text for marker in whole_table) and not has_range


def wants_all_groups(question: str) -> bool:
    """The user asked to see every grouped row, not a capped sample."""
    folded = question.lower().replace("'", "").replace("’", "")
    return any(
        phrase in folded
        for phrase in (
            "no limit",
            "without a limit",
            "without the limit",
            "dont apply the limit",
            "do not apply the limit",
            "dont need to apply the limit",
            "do not need to apply the limit",
            "remove the limit",
            "see all of them",
            "all of them",
            "every one of them",
            "every row",
            "all rows",
            "every website",
            "every subscriber",
        )
    )


def prepare(sql: str, max_rows: int, allow_full_scan: bool = False, allow_all_groups: bool = False) -> str:
    text = _strip(sql)
    try:
        statements = [item for item in sqlglot.parse(text, read="mysql") if item is not None]
    except sqlglot.errors.SqlglotError as exc:
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
    columns = {col.name.lower() for col in where.find_all(exp.Column)} if where is not None else set()
    has_time = bool(columns & TIME_COLUMNS or columns & POINT_COLUMNS)
    has_one_subscriber = any(
        isinstance(eq, exp.EQ)
        and isinstance(eq.left, exp.Column)
        and eq.left.name.lower() == "subscriber_id"
        and isinstance(eq.right, exp.Literal)
        and eq.right.is_string
        for eq in expr.find_all(exp.EQ)
    )
    if not has_time and not has_one_subscriber:
        if not (allow_full_scan and _is_aggregate(expr)):
            raise GuardError(
                "Filter on start_time, end_time, recv_time, id, or log_id. "
                "A question with no time range is not run unless you explicitly ask for all of the data."
            )

    if allow_all_groups and _groups_by_label(expr):
        expr.set("limit", None)
        return expr.sql(dialect="mysql")

    limit = expr.args.get("limit")
    if limit is None:
        return f"{text}\nLIMIT {int(max_rows)}"
    value = limit.args.get("expression")
    if isinstance(value, exp.Literal) and not value.is_string and int(value.this) > max_rows:
        expr.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return expr.sql(dialect="mysql")
    return text


def _groups_by_label(expr: exp.Select) -> bool:
    group = expr.args.get("group")
    if group is None:
        return False
    names = {column.name.lower() for column in group.find_all(exp.Column)}
    return bool(names) and names <= {"app_label", "app_category", "requested_server", "subscriber_id"}


def _is_aggregate(expr: exp.Select) -> bool:
    if expr.args.get("group") is not None:
        return True
    return any(isinstance(node, exp.AggFunc) for node in expr.find_all(exp.AggFunc))


def _strip(sql: str) -> str:
    text = sql.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if text.endswith(";"):
        text = text[:-1].strip()
    return text
