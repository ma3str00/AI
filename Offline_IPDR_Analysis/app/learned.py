"""Replay a query the user has already marked correct."""

from __future__ import annotations

import re

_TIME_COLUMNS = ("start_time", "end_time", "recv_time")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def sql_needs_full_scan(sql: str) -> bool:
    """True when the query has no WHERE time window. SELECT FROM_UNIXTIME(start_time) is not a filter."""
    return not bool(
        re.search(
            r"\b(?:start_time|end_time|recv_time)\s*(?:>=|<=|<>|!=|=|>|<)",
            sql or "",
            flags=re.IGNORECASE,
        )
    )


def is_relative_followup(question: str) -> bool:
    """A reply that only makes sense against the previous query, not as a standalone replay."""
    folded = (question or "").lower().strip()
    if not folded:
        return False
    if re.match(
        r"^(also|and also|and then|then|plus|additionally|"
        r"same for|show (?:me )?the same|continue|continuing|"
        r"building on|based on|corresponding)\b",
        folded,
    ):
        return True
    if re.search(r"\b(previous (?:query|question|result|data|output)|the same for)\b", folded):
        return True
    return False


def saved_sql(examples: list[dict], question: str, history: list[dict] | None = None) -> dict | None:
    """Exact replay of a marked-correct answer. Never hijack an in-progress follow-up thread."""
    if history and any(str(item.get("sql") or "").strip() for item in history):
        return None
    if is_relative_followup(question):
        return None
    key = normalize(question)
    if not key:
        return None
    for item in reversed(examples):
        if item.get("learned") and item.get("sql") and normalize(str(item.get("question") or "")) == key:
            return item
    return None
