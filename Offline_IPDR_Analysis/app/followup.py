"""Apply a follow-up once the user has already settled the open choice."""

from __future__ import annotations

import re

from app.schema import MOBILE_SQL
from app.sql_guard import wants_full_table

_TIME_WORDS = ("yesterday", "today", "last ", "hour", "between", "since", "this week")


def resolve_followup(question: str, history: list[dict] | None) -> str | None:
    """Build SQL from the last real query when the reply only changes one part of it."""
    history = history or []
    apps = _apps_of_one_subscriber(question, history)
    if apps:
        return apps
    if _pending(history) and _answer_keeps(question):
        return _revise(str(history[-1].get("question") or ""), history[:-1], force_keep=True)
    if _pending(history) and _answer_drops(question):
        return _revise(str(history[-1].get("question") or ""), history[:-1], force_drop=True)
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    revised = _revise(question, history)
    if revised:
        return revised
    texts = [str(item.get("question") or "") for item in history]
    kind = _subscriber_kind(texts + [question])
    if kind not in {"mobile", "fixed"}:
        return None
    if _names_time(question):
        return None
    blob = " ".join(texts + [question]).lower()
    keep, drop = _app_choice(blob, previous)
    if "for the same" in question.lower():
        keep = True
    if _has_app_filter(previous) and not keep and not drop:
        return None
    if _subscriber_request(question) and not keep and not drop:
        return None
    limit = _limit_in(question) or _latest_limit(texts) or _limit_of(previous) or 5
    return _rewrite(previous, keep_app=not drop, kind=kind, limit=limit)


def conversation_full_scan(question: str, history: list[dict] | None) -> bool:
    if _names_time(question):
        return False
    blob = " ".join(str(item.get("question") or "") for item in (history or [])) + " " + question
    folded = blob.lower()
    if any(
        phrase in folded
        for phrase in (
            "no time filter",
            "no time range",
            "doesnt specify",
            "doesn't specify",
            "does not specify",
            "all the data",
            "all data",
        )
    ):
        return True
    previous = _last_sql(history or [])
    return bool(previous) and not any(column in previous.lower() for column in ("start_time", "end_time", "recv_time"))


def use_full_scan(question: str, history: list[dict] | None, missing_time_means_all: bool) -> bool:
    """A saved rule runs the whole table when this question names no time window.

    A follow-up of a query that already has a time filter keeps that filter.
    """
    if _names_time(question):
        return False
    if conversation_full_scan(question, history) or wants_full_table(question):
        return True
    if _last_sql(history or []):
        return False
    return missing_time_means_all


def _apps_of_one_subscriber(question: str, history: list[dict]) -> str | None:
    """Applications used by one subscriber. "Other" drops the application already shown."""
    if not re.search(r"\b(application|applications|apps)\b", question.lower()):
        return None
    subscriber = named_subscriber(question)
    if not subscriber:
        return None
    where = [f"subscriber_id = '{subscriber}'"]
    previous = _last_sql(history)
    if re.search(r"\bother\b", question.lower()) and previous:
        predicate = _app_predicate(previous)
        if predicate:
            where.append(f"NOT ({predicate})")
    time_clause = _time_clause(previous)
    if time_clause:
        where.append(time_clause)
    limit = _limit_in(question) or 20
    return (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        f"FROM ntc_ssl_logs WHERE {' AND '.join(where)} "
        f"GROUP BY app_label ORDER BY total_bytes DESC LIMIT {int(limit)}"
    )


def named_subscriber(question: str) -> str | None:
    """The subscriber id named in the question, including 'used by someid123'."""
    patterns = (
        r"\b(?:user|subscriber)\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
        r"\bby\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, question)
        if not match:
            continue
        token = match.group(1)
        if token.lower() in _NOT_SUBSCRIBER:
            continue
        if re.search(r"\d", token) or "-" in token or "_" in token:
            return token
    return None


_NOT_SUBSCRIBER = {
    "the", "number", "traffic", "volume", "flow", "flows", "total", "bytes",
    "application", "applications", "apps", "user", "subscriber", "users",
    "facebook", "youtube", "whatsapp", "tiktok", "instagram", "telegram",
}


def _app_predicate(sql: str) -> str | None:
    text = re.sub(r"\s+", " ", sql)
    match = re.search(r"LOWER\(app_label\)\s+LIKE\s+'%[^']+%'", text, flags=re.IGNORECASE)
    if match:
        return match.group(0)
    match = re.search(r"LOWER\(app_label\)\s+IN\s+\([^)]*\)", text, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _time_clause(sql: str) -> str:
    if not sql:
        return ""
    parts = re.findall(
        r"((?:start_time|end_time|recv_time)\s*(?:>=|<=|<>|!=|=|>|<)\s*(?:UNIX_TIMESTAMP\([^)]*\)|'[^']*'|\d+))",
        sql,
        flags=re.IGNORECASE,
    )
    return " AND ".join(parts)


def _last_sql(history: list[dict]) -> str:
    for item in reversed(history):
        sql = str(item.get("sql") or "").strip()
        if sql:
            return sql
    return ""


def _subscriber_kind(texts: list[str]) -> str | None:
    """The latest mobile, fixed-line, or all-connections request wins."""
    found = None
    for text in texts:
        if _rejects_connection(text):
            found = "any"
        elif _mentions_fixed(text) and not _mentions_mobile(text):
            found = "fixed"
        elif _mentions_mobile(text):
            found = "mobile"
    return found


def subscriber_kind(texts: list[str]) -> str | None:
    return _subscriber_kind(texts)


def _limit_in(text: str) -> int | None:
    match = re.search(r"\btop\s+(\d+)\b", text.lower())
    return int(match.group(1)) if match else None


def _mentions_mobile(text: str) -> bool:
    if _rejects_connection(text):
        return False
    folded = text.replace("_", "").replace("-", " ")
    return "mobile user" in folded or "mobile subscriber" in folded or ("exclude" in folded and "fixed" in folded)


def _rejects_connection(text: str) -> bool:
    folded = text.lower().replace("-", " ")
    if any(phrase in folded for phrase in ("regardless", "irrespective", "nature of connection", "any connection", "all connections")):
        return True
    return "mobile" in folded and bool(re.search(r"\b(not only|not just|gave|given|showing|showed)\b", folded))


def _mentions_fixed(text: str) -> bool:
    folded = text.replace("_", "").replace("-", " ")
    return "fixed line user" in folded or "fixedline user" in folded


def _names_time(question: str) -> bool:
    return any(word in question.lower() for word in _TIME_WORDS)


def _app_choice(blob: str, previous: str) -> tuple[bool, bool]:
    drop = any(
        phrase in blob
        for phrase in ("every application", "all applications", "all apps", "every app", "include every")
    )
    keep = any(
        phrase in blob
        for phrase in ("stay limited", "keep whatsapp", "only whatsapp", "limited to whatsapp", "stay on whatsapp", "for the same")
    )
    if not keep and not drop and "whatsapp" in blob and "whatsapp" in previous.lower():
        # The user named WhatsApp again while answering, which keeps that filter.
        if "stay" in blob or "limited" in blob or "correct" in blob:
            keep = True
    return keep, drop


def _has_app_filter(sql: str) -> bool:
    return "app_label" in sql.lower()


def _subscriber_request(question: str) -> bool:
    return _mentions_mobile(question) or _mentions_fixed(question)


def _latest_limit(texts: list[str]) -> int | None:
    found = None
    for text in texts:
        for match in re.finditer(r"\btop\s+(\d+)\b", text.lower()):
            found = int(match.group(1))
    return found


def _limit_of(sql: str) -> int | None:
    match = re.search(r"\blimit\s+(\d+)\b", sql.lower())
    return int(match.group(1)) if match else None


def _rewrite(sql: str, keep_app: bool, kind: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", sql).strip()
    if not keep_app:
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+LIKE\s+'%[^']+%'", "", text, flags=re.IGNORECASE)
        text = re.sub(r"LOWER\(app_label\)\s+LIKE\s+'%[^']+%'\s+AND\s+", "", text, flags=re.IGNORECASE)
    text = _without_cell(text)
    cell = f"AND NOT {MOBILE_SQL}" if kind == "fixed" else f"AND {MOBILE_SQL}"
    text = re.sub(r"\bGROUP BY\b", f"{cell} GROUP BY", text, count=1, flags=re.IGNORECASE)
    if re.search(r"\blimit\s+\d+\b", text, flags=re.IGNORECASE):
        text = re.sub(r"\blimit\s+\d+\b", f"LIMIT {int(limit)}", text, count=1, flags=re.IGNORECASE)
    else:
        text = f"{text} LIMIT {int(limit)}"
    return text


def _without_cell(sql: str) -> str:
    pattern = re.escape(re.sub(r"\s+", " ", MOBILE_SQL))
    text = re.sub(rf"\s+AND\s+NOT\s+{pattern}", "", sql, flags=re.IGNORECASE)
    text = re.sub(rf"\s+AND\s+{pattern}", "", text, flags=re.IGNORECASE)
    loose = (
        r"\(\s*NULLIF\(\s*mcc\b.*?NULLIF\(\s*usr_loc_info\b.*?\)\s*\)"
    )
    text = re.sub(rf"\s+AND\s+NOT\s+{loose}", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"\s+AND\s+{loose}", "", text, flags=re.IGNORECASE)
    return text


def _pending(history: list[dict]) -> bool:
    if not history:
        return False
    last = history[-1]
    return not str(last.get("sql") or "").strip() and bool(str(last.get("explanation") or "").strip())


def _answer_keeps(question: str) -> bool:
    folded = question.strip().lower()
    if folded in {"yes", "yes.", "ok", "okay", "keep", "keep it"}:
        return True
    return "keep the previous" in folded or "stay limited" in folded or "limited to" in folded


def _answer_drops(question: str) -> bool:
    folded = question.strip().lower()
    return folded in {"no", "no."} or "include every application" in folded or "every application" in folded


def _wants_split(question: str) -> bool:
    folded = question.lower()
    return "upload" in folded and "download" in folded


def _rank_target(question: str) -> str | None:
    folded = question.lower()
    wants_flows = bool(re.search(r"\b(flow count|number of flows|by flows|by flow)\b", folded))
    wants_volume = bool(re.search(r"\b(traffic volume|by volume|by traffic|total bytes)\b", folded))
    if wants_flows and wants_volume:
        return "flows" if folded.rfind("flow") > folded.rfind("volume") else "total_bytes"
    if wants_flows:
        return "flows"
    if wants_volume and "instead" in folded:
        return "total_bytes"
    return None


def _revise(question: str, history: list[dict], force_keep: bool = False, force_drop: bool = False) -> str | None:
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    if _subscriber_request(question) and not _wants_split(question) and _rank_target(question) is None:
        return None
    split = _wants_split(question)
    rank = _rank_target(question)
    reject = _rejects_connection(question) or subscriber_kind(
        [str(item.get("question") or "") for item in history] + [question]
    ) == "any" and _rejects_connection(question)
    if not (split or rank or _rejects_connection(question) or (force_keep and (split or rank)) or (force_drop and (split or rank))):
        return None
    texts = [str(item.get("question") or "") for item in history]
    kind = _subscriber_kind(texts + [question])
    keep, drop = _app_choice(" ".join(texts + [question]).lower(), previous)
    if force_keep or "for the same" in question.lower() or split or rank or _rejects_connection(question):
        if not force_drop and not drop:
            keep = True
    if force_drop:
        keep, drop = False, True
    text = re.sub(r"\s+", " ", previous).strip()
    if drop or not keep:
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+LIKE\s+'%[^']+%'", "", text, flags=re.IGNORECASE)
        text = re.sub(r"LOWER\(app_label\)\s+LIKE\s+'%[^']+%'\s+AND\s+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+IN\s+\([^)]*\)", "", text, flags=re.IGNORECASE)
    if kind == "fixed":
        text = _without_cell(text)
        text = re.sub(r"\bGROUP BY\b", f"AND NOT {MOBILE_SQL} GROUP BY", text, count=1, flags=re.IGNORECASE)
    elif kind == "mobile":
        text = _without_cell(text)
        text = re.sub(r"\bGROUP BY\b", f"AND {MOBILE_SQL} GROUP BY", text, count=1, flags=re.IGNORECASE)
    else:
        text = _without_cell(text)
    if split:
        text = re.sub(
            r"(?is)^SELECT\b.+?\bFROM\b",
            "SELECT subscriber_id, SUM(c2s_byte_num) AS upload_bytes, "
            "SUM(s2c_byte_num) AS download_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            "COUNT(*) AS flows FROM",
            text,
            count=1,
        )
    if rank == "flows" and not re.search(r"\bas\s+flows\b", text, flags=re.IGNORECASE):
        text = re.sub(r"(?i)\bFROM\b", ", COUNT(*) AS flows FROM", text, count=1)
    if rank:
        column = "flows" if rank == "flows" else ("total_bytes" if re.search(r"\bas\s+total_bytes\b", text, flags=re.IGNORECASE) else "total_mb")
        if re.search(r"(?i)\bORDER BY\b", text):
            text = re.sub(r"(?is)\bORDER BY\b.+?(?=\bLIMIT\b|$)", f"ORDER BY {column} DESC ", text, count=1)
        elif re.search(r"(?i)\bLIMIT\b", text):
            text = re.sub(r"(?i)\bLIMIT\b", f"ORDER BY {column} DESC LIMIT", text, count=1)
        else:
            text = f"{text} ORDER BY {column} DESC"
    limit = _limit_in(question) or _limit_of(text)
    if limit and re.search(r"(?i)\blimit\s+\d+\b", text):
        text = re.sub(r"(?i)\blimit\s+\d+\b", f"LIMIT {int(limit)}", text, count=1)
    return re.sub(r"\s+", " ", text).strip()
