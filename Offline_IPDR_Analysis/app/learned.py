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


def saved_sql(examples: list[dict], question: str) -> dict | None:
    key = normalize(question)
    if not key:
        return None
    for item in reversed(examples):
        if item.get("learned") and item.get("sql") and normalize(str(item.get("question") or "")) == key:
            return item
    return None
