"""Rules learned from answers the user marked correct. Nothing here is on by default."""

from __future__ import annotations

import json
from pathlib import Path

from app.learned import sql_needs_full_scan

_TIME_WORDS = ("yesterday", "today", "last ", "hour", "between", "since", "this week")
_STATE = {"missing_time": "ask"}


def missing_time_means_all() -> bool:
    return _STATE.get("missing_time") == "all" and _STATE.get("source") == "marked"


def load_preferences(path: Path) -> None:
    """Only a preference written from a marked answer counts. A hand-set default does not."""
    _STATE.clear()
    _STATE.update({"missing_time": "ask"})
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if isinstance(data, dict) and data.get("source") == "marked" and data.get("missing_time") == "all":
        _STATE["missing_time"] = "all"
        _STATE["source"] = "marked"


def absorb_marked_answers(path: Path, rows: list[dict]) -> None:
    """Re-learn from answers already marked correct, including ones saved before this check existed."""
    for row in rows:
        if not row.get("learned"):
            continue
        if lesson_from_marked_answer([str(row.get("question") or "")], str(row.get("sql") or "")):
            _store_marked_time_rule(path)
            return


def lesson_from_marked_answer(questions: list[str], sql: str) -> bool:
    """True when a marked query read the whole table for a question that named no time window."""
    if not sql or not sql_needs_full_scan(sql):
        return False
    return not any(_names_time(text) for text in questions)


def _store_marked_time_rule(path: Path) -> None:
    _STATE["missing_time"] = "all"
    _STATE["source"] = "marked"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"missing_time": "all", "source": "marked"}, ensure_ascii=False) + "\n", encoding="utf-8")


def _names_time(text: str) -> bool:
    folded = text.lower()
    return any(word in folded for word in _TIME_WORDS)
