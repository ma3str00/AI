"""Confirm-before-run: summarize planned SQL and detect Accept of a pending query."""

from __future__ import annotations

import re

from app.learned import sql_needs_full_scan
from app.schema import MOBILE_SQL


def pending_confirm(history: list[dict] | None) -> bool:
    """True when the last turn holds SQL waiting for Accept."""
    if not history:
        return False
    last = history[-1]
    if str(last.get("sql") or "").strip():
        return False
    return bool(str(last.get("pending_sql") or "").strip())


def pending_sql(history: list[dict] | None) -> str | None:
    """Return the SQL waiting for Accept, if any."""
    if not pending_confirm(history):
        return None
    text = str(history[-1].get("pending_sql") or "").strip()
    return text or None


def answer_accepts(question: str) -> bool:
    """True when the user accepts a confirm-before-run prompt."""
    folded = (question or "").strip().lower()
    if folded in {
        "yes",
        "yes.",
        "ok",
        "okay",
        "accept",
        "accepted",
        "run",
        "run it",
        "go",
        "go ahead",
        "proceed",
        "confirm",
        "do it",
        "looks good",
        "correct",
    }:
        return True
    return bool(
        re.search(
            r"\b("
            r"accept|"
            r"run\s+it|"
            r"go\s+ahead|"
            r"looks\s+good|"
            r"proceed|"
            r"confirm(?:\s+it)?"
            r")\b",
            folded,
        )
    )


def summarize_query_plan(question: str, sql: str, *, full_scan: bool | None = None) -> str:
    """Plain-language conditions for the confirm bubble (multi-line for readability)."""
    text = re.sub(r"\s+", " ", sql or "").strip()
    folded = text.lower()
    lines: list[str] = []

    apps = _like_values(folded, "app_label")
    if apps:
        lines.append(f"App: {', '.join(_title_tokens(apps))}")
    else:
        lines.append("App: every application (no app filter)")

    isps = _unique_keep_order(_like_values(folded, "client_asn"))
    if isps:
        lines.append(f"ISP: {', '.join(_title_tokens(isps))}")

    places = _unique_keep_order(_like_values(folded, "loc") + _like_values(folded, "geo"))
    if places:
        lines.append(f"Place: {', '.join(_title_tokens(places))}")

    if "locate('_', subscriber_id) = 0" in folded.replace(" ", ""):
        lines.append("Users: identified subscribers only")

    mobile = re.sub(r"\s+", " ", MOBILE_SQL).lower()
    compact_sql = re.sub(r"\s+", " ", folded)
    if f"not {mobile}" in compact_sql or "and not (" in compact_sql and "nullif(mcc" in compact_sql:
        lines.append("Connection: fixed-line")
    elif "nullif(mcc" in compact_sql and "not " not in compact_sql.split("nullif(mcc")[0][-20:]:
        if re.search(r"\band\s+\(.*nullif\(mcc", compact_sql) and "and not" not in compact_sql:
            lines.append("Connection: mobile")

    measure = _order_measure(folded)
    if measure:
        lines.append(f"Ranked by: {measure}")

    limit = re.search(r"\blimit\s+(\d+)\b", folded)
    if limit:
        lines.append(f"Limit: top {limit.group(1)} rows")

    scan = sql_needs_full_scan(text) if full_scan is None else full_scan
    if scan:
        lines.append("Time: no filter (full table scan; may take several minutes)")

    grain = _grain(folded)
    if grain:
        lines.append(f"Result: {grain}")

    ask = (question or "").strip()
    if ask and ask.lower() not in {"accept", "yes", "run it", "go ahead"}:
        header = f'I am going to run a query for:\n"{ask[:120]}"'
    else:
        header = "I am going to run a query"
    bullets = "\n".join(f"- {line}" for line in lines) if lines else "- the generated SQL"
    return (
        f"{header}\n\n"
        f"Conditions:\n{bullets}\n\n"
        "Accept to run it, or Change if this is wrong."
    )


def _title_tokens(items: list[str]) -> list[str]:
    return [item[:1].upper() + item[1:] if item else item for item in items]


def _like_values(folded_sql: str, column: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(
            rf"lower\s*\(\s*{re.escape(column)}\s*\)\s+like\s+'%([^%']+)%'",
            folded_sql,
            flags=re.IGNORECASE,
        )
    ]


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _order_measure(folded_sql: str) -> str | None:
    match = re.search(r"\border\s+by\s+([a-z0-9_]+)\s+(?:desc|asc)\b", folded_sql)
    if not match:
        # Prefer the statement-level ORDER BY near LIMIT.
        match = re.search(r"\border\s+by\s+([a-z0-9_]+)\s+(?:desc|asc)\s+limit\b", folded_sql)
    if not match:
        return None
    name = match.group(1)
    if name in {"flows", "flow_count", "sessions", "hits"}:
        return "flow count"
    if name in {"total_bytes", "total_mb", "traffic_volume", "traffic_volume_mb"}:
        return "traffic volume"
    if name == "users":
        return "user count"
    return name.replace("_", " ")


def _grain(folded_sql: str) -> str | None:
    if "row_number()" in folded_sql:
        return "windowed ranking"
    if "group by subscriber_id" in folded_sql:
        return "one row per subscriber"
    if "group by app_label" in folded_sql:
        return "one row per application"
    if "group by client_asn" in folded_sql:
        return "one row per ISP"
    if "group by" in folded_sql:
        return "grouped result"
    return None
