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


_COLUMN_LABELS = {
    "subscriber_id": "Subscriber ID",
    "app_label": "Application",
    "requested_server": "Website / hostname",
    "client_asn": "ISP / client ASN",
    "server_asn": "Server ASN",
    "client_ip": "Client IP",
    "server_ip": "Server IP",
    "loc": "Address (loc)",
    "geo": "Geo",
    "imei": "IMEI",
    "imsi": "IMSI",
    "flows": "Flow count",
    "flow_count": "Flow count",
    "sessions": "Session count",
    "hits": "Hit count",
    "total_bytes": "Traffic (bytes)",
    "total_mb": "Traffic (MB)",
    "total_gb": "Traffic (GB)",
    "traffic_volume": "Traffic volume",
    "traffic_volume_mb": "Traffic (MB)",
    "traffic_volume_gb": "Traffic (GB)",
    "upload_bytes": "Upload (bytes)",
    "download_bytes": "Download (bytes)",
    "upload_traffic": "Upload traffic",
    "download_traffic": "Download traffic",
    "upload_traffic_mb": "Upload (MB)",
    "download_traffic_mb": "Download (MB)",
    "upload_traffic_gb": "Upload (GB)",
    "download_traffic_gb": "Download (GB)",
    "users": "User count",
    "city": "City",
    "block_type": "Block type",
    "blocked_flows": "Blocked flow count",
    "allowed_flows": "Allowed flow count",
    "start_gmt5": "Start time (GMT+5)",
    "end_gmt5": "End time (GMT+5)",
    "duration": "Duration",
    "rn": "Rank within group",
}


def build_query_plan(question: str, sql: str, *, full_scan: bool | None = None) -> dict:
    """Structured plan for the confirm UI (conditions + columns)."""
    text = re.sub(r"\s+", " ", sql or "").strip()
    folded = text.lower()
    conditions: list[dict[str, str]] = []

    apps = _app_filter_values(folded)
    if apps:
        conditions.append({"label": "App", "value": ", ".join(_title_tokens(apps))})
    else:
        conditions.append({"label": "App", "value": "Every application (no app filter)"})

    subscriber = _subscriber_filter_value(text)
    if subscriber:
        conditions.append({"label": "Subscriber", "value": subscriber})

    isps = _unique_keep_order(_like_values(folded, "client_asn"))
    if isps:
        conditions.append({"label": "ISP", "value": ", ".join(_title_tokens(isps))})

    places = _canonical_places(_like_values(folded, "loc") + _like_values(folded, "geo"))
    if places:
        conditions.append({"label": "Place", "value": ", ".join(places)})

    if "locate('_', subscriber_id) = 0" in folded.replace(" ", "") and not subscriber:
        conditions.append({"label": "Users", "value": "Identified subscribers only"})

    mobile = re.sub(r"\s+", " ", MOBILE_SQL).lower()
    compact_sql = re.sub(r"\s+", " ", folded)
    if f"not {mobile}" in compact_sql or ("and not (" in compact_sql and "nullif(mcc" in compact_sql):
        conditions.append({"label": "Connection", "value": "Fixed-line"})
    elif "nullif(mcc" in compact_sql:
        if re.search(r"\band\s+\(.*nullif\(mcc", compact_sql) and "and not" not in compact_sql:
            conditions.append({"label": "Connection", "value": "Mobile"})

    measure = _order_measure(folded)
    if measure:
        conditions.append({"label": "Ranked by", "value": measure})

    limit = re.search(r"\blimit\s+(\d+)\b", folded)
    if limit:
        conditions.append({"label": "Limit", "value": f"Top {limit.group(1)} rows"})

    time_label = _time_filter_label(text)
    scan = sql_needs_full_scan(text) if full_scan is None else full_scan
    if time_label:
        conditions.append({"label": "Time", "value": time_label})
    elif scan:
        conditions.append(
            {"label": "Time", "value": "No filter (full table scan; may take several minutes)"}
        )

    grain = _grain(folded)
    if grain:
        conditions.append({"label": "Result shape", "value": grain})

    columns = _display_columns(text)
    ask = (question or "").strip()
    if ask and ask.lower() not in {"accept", "yes", "run it", "go ahead"}:
        title = ask[:120]
    else:
        title = ""
    return {
        "title": title,
        "conditions": conditions,
        "columns": columns,
        "footer": "Accept to run it, or Change if this is wrong.",
    }


def _time_filter_label(sql: str) -> str | None:
    """Human label for a start_time / end_time / recv_time window already in SQL."""
    folded = re.sub(r"\s+", " ", (sql or "").lower())
    if not re.search(r"\b(?:start_time|end_time|recv_time)\s*(?:>=|<=|<>|!=|=|>|<)", folded):
        return None
    if "curdate() - interval 1 day" in folded and "start_time < unix_timestamp(curdate())" in folded:
        return "Yesterday"
    if "unix_timestamp(curdate())" in folded and "curdate() + interval 1 day" in folded:
        return "Today"
    relative = re.search(
        r"unix_timestamp\s*\(\s*now\s*\(\s*\)\s*-\s*interval\s+(\d+)\s+(minute|hour|day|week)\s*\)",
        folded,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if amount == 1:
            return f"Last {unit}"
        return f"Last {amount} {unit}s"
    return "Filtered by start/end/recv time"


def summarize_query_plan(question: str, sql: str, *, full_scan: bool | None = None) -> str:
    """Plain-text fallback of the structured plan (history / logs)."""
    plan = build_query_plan(question, sql, full_scan=full_scan)
    if plan["title"]:
        header = f'I am going to run a query for:\n"{plan["title"]}"'
    else:
        header = "I am going to run a query"
    lines = [f"- {item['label']}: {item['value']}" for item in plan["conditions"]]
    if plan["columns"]:
        lines.append("- Columns shown: " + ", ".join(plan["columns"]))
    bullets = "\n".join(lines) if lines else "- the generated SQL"
    return f"{header}\n\nConditions:\n{bullets}\n\n{plan['footer']}"


def _display_columns(sql: str) -> list[str]:
    """Friendly names for SELECT aliases that appear in the result table."""
    # Prefer the outermost SELECT list (before the first top-level FROM).
    select = _outer_select_list(sql or "")
    if not select:
        return []
    names: list[str] = []
    for part in _split_select_list(select):
        alias = _select_alias(part)
        if not alias or alias in {"rn"}:
            continue
        label = _COLUMN_LABELS.get(alias, alias.replace("_", " ").title())
        if label not in names:
            names.append(label)
    return names


def _outer_select_list(sql: str) -> str:
    text = sql.strip()
    match = re.match(r"(?is)^select\b", text)
    if not match:
        return ""
    start = match.end()
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and re.match(r"(?is)\bfrom\b", text[i:]):
            return text[start:i].strip()
        i += 1
    return ""


def _split_select_list(select: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in select:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            continue
        buf.append(ch)
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def _select_alias(expression: str) -> str | None:
    text = expression.strip().rstrip(",")
    as_match = re.search(r"(?is)\bas\s+(`?[a-zA-Z_][a-zA-Z0-9_]*`?)\s*$", text)
    if as_match:
        return as_match.group(1).strip("`").lower()
    bare = re.match(r"(?is)^(?:\w+\.)?(`?[a-zA-Z_][a-zA-Z0-9_]*`?)\s*$", text)
    if bare:
        return bare.group(1).strip("`").lower()
    return None


def _title_tokens(items: list[str]) -> list[str]:
    return [item[:1].upper() + item[1:] if item else item for item in items]


def _canonical_places(tokens: list[str]) -> list[str]:
    """Collapse loc/geo LIKE stems (lahore, lhr) to one city name when known."""
    from app.places import CITIES

    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        folded = (token or "").strip().lower()
        if not folded:
            continue
        label = None
        for place in CITIES:
            aliases = {place.name.lower(), *(alias.lower() for alias in place.aliases)}
            if folded in aliases:
                label = place.name
                break
        if label is None:
            label = folded[:1].upper() + folded[1:]
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _like_values(folded_sql: str, column: str) -> list[str]:
    """Extract LIKE stems, including multi-token sector patterns ('%f-6%islamabad%')."""
    out: list[str] = []
    for match in re.finditer(
        rf"lower\s*\(\s*{re.escape(column)}\s*\)\s+like\s+'%((?:[^%']+%)*[^%']+)%'",
        folded_sql,
        flags=re.IGNORECASE,
    ):
        token = re.sub(r"%+", " ", match.group(1)).strip()
        if token:
            out.append(token)
    return out


def _app_filter_values(folded_sql: str) -> list[str]:
    """App stems from LIKE and equality filters."""
    found = _like_values(folded_sql, "app_label")
    for match in re.finditer(
        r"lower\s*\(\s*app_label\s*\)\s*=\s*'([^']+)'",
        folded_sql,
        flags=re.IGNORECASE,
    ):
        value = match.group(1).strip()
        if value and value not in found:
            found.append(value)
    return found


def _subscriber_filter_value(folded_sql: str) -> str | None:
    match = re.search(r"subscriber_id\s*=\s*'([^']+)'", folded_sql, flags=re.IGNORECASE)
    return match.group(1) if match else None


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
    match = re.search(r"\border\s+by\s+([a-z0-9_]+)\s+(?:desc|asc)\s+limit\b", folded_sql)
    if not match:
        matches = list(re.finditer(r"\border\s+by\s+([a-z0-9_]+)\s+(?:desc|asc)\b", folded_sql))
        match = matches[-1] if matches else None
    if not match:
        return None
    name = match.group(1)
    if name in {"flows", "flow_count", "sessions", "hits"}:
        return "Flow count"
    if name in {"total_bytes", "total_mb", "traffic_volume", "traffic_volume_mb"}:
        return "Traffic volume"
    if name == "users":
        return "User count"
    return name.replace("_", " ").title()


def _grain(folded_sql: str) -> str | None:
    if "row_number()" in folded_sql:
        return "Windowed ranking"
    if "group by subscriber_id" in folded_sql:
        return "One row per subscriber"
    if "group by app_label" in folded_sql:
        return "One row per application"
    if "group by client_asn" in folded_sql:
        return "One row per ISP"
    if "group by" in folded_sql:
        return "Grouped result"
    return None
