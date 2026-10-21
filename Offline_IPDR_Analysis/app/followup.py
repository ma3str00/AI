"""Apply a follow-up once the user has already settled the open choice."""

from __future__ import annotations

import re

from app.schema import MOBILE_SQL

_TIME_WORDS = ("yesterday", "today", "last ", "hour", "between", "since", "this week")


def resolve_followup(question: str, history: list[dict] | None) -> str | None:
    """Build SQL from the last real query when the reply only confirms scope."""
    history = history or []
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    texts = [str(item.get("question") or "") for item in history] + [question]
    blob = " ".join(texts).lower()
    mobile = _mentions_mobile(blob)
    fixed = _mentions_fixed(blob) and not mobile
    if not mobile and not fixed:
        return None
    if _names_time(question):
        return None
    keep, drop = _app_choice(blob, previous)
    if _has_app_filter(previous) and not keep and not drop:
        return None
    if _subscriber_request(question) and not keep and not drop:
        return None
    limit = _latest_limit(texts) or _limit_of(previous) or 5
    return _rewrite(previous, keep_app=not drop, mobile=mobile, fixed=fixed, limit=limit)


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


def _last_sql(history: list[dict]) -> str:
    for item in reversed(history):
        sql = str(item.get("sql") or "").strip()
        if sql:
            return sql
    return ""


def _mentions_mobile(text: str) -> bool:
    folded = text.replace("_", "").replace("-", " ")
    return "mobile user" in folded or "mobile subscriber" in folded or ("exclude" in folded and "fixed" in folded)


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
        for phrase in ("stay limited", "keep whatsapp", "only whatsapp", "limited to whatsapp", "stay on whatsapp")
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


def _rewrite(sql: str, keep_app: bool, mobile: bool, fixed: bool, limit: int) -> str:
    text = re.sub(r"\s+", " ", sql).strip()
    if not keep_app:
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+LIKE\s+'%[^']+%'", "", text, flags=re.IGNORECASE)
        text = re.sub(r"LOWER\(app_label\)\s+LIKE\s+'%[^']+%'\s+AND\s+", "", text, flags=re.IGNORECASE)
    if mobile and "mcc" not in text.lower():
        text = re.sub(r"\bGROUP BY\b", f"AND {MOBILE_SQL} GROUP BY", text, count=1, flags=re.IGNORECASE)
    if fixed and "not (" not in text.lower():
        text = re.sub(
            r"\bGROUP BY\b",
            f"AND NOT {MOBILE_SQL} GROUP BY",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    if re.search(r"\blimit\s+\d+\b", text, flags=re.IGNORECASE):
        text = re.sub(r"\blimit\s+\d+\b", f"LIMIT {int(limit)}", text, count=1, flags=re.IGNORECASE)
    else:
        text = f"{text} LIMIT {int(limit)}"
    return text
