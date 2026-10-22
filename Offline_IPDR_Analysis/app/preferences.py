"""Standing rules the user has taught, kept across conversations."""

from __future__ import annotations

import json
from pathlib import Path

# The operator has already taught this. A missing file keeps it.
_STATE = {"missing_time": "all"}


def missing_time_means_all() -> bool:
    return _STATE.get("missing_time") == "all"


def load_preferences(path: Path) -> None:
    if not path.exists():
        save_preferences(path)
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if isinstance(data, dict) and data.get("missing_time") in {"all", "ask"}:
        _STATE["missing_time"] = data["missing_time"]


def save_preferences(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_STATE, ensure_ascii=False) + "\n", encoding="utf-8")


def remember_time_rule(path: Path, texts: list[str]) -> bool:
    """Store the rule when the user states it. Returns True if it was stored."""
    if not any(teaches_missing_time_means_all(text) for text in texts):
        return False
    if _STATE.get("missing_time") != "all":
        _STATE["missing_time"] = "all"
    save_preferences(path)
    return True


def teaches_missing_time_means_all(text: str) -> bool:
    """True when the user is stating the rule, not merely asking one whole-table question."""
    folded = text.lower().replace("'", "").replace("’", "")
    if "time" not in folded:
        return False
    unspecified = any(
        phrase in folded
        for phrase in (
            "not specified",
            "isnt specified",
            "doesnt specify",
            "does not specify",
            "not given",
            "unspecified",
            "no time range",
            "no time filter",
            "without a time",
            "without any time",
        )
    )
    if not unspecified:
        return False
    whole = any(
        phrase in folded
        for phrase in (
            "all the data",
            "all data",
            "entire data",
            "entire table",
            "whole table",
            "all of the data",
            "query all",
            "run on the entire",
            "run on all",
            "use all",
        )
    )
    stated = any(
        phrase in folded
        for phrase in (
            "doesnt specify",
            "does not specify",
            "not specified",
            "isnt specified",
            "unspecified",
            "not given",
            "if no time",
            "when no time",
            "if the time",
            "when the time",
            "if time",
            "when time",
        )
    )
    return whole or stated


def rule_only(text: str) -> bool:
    """The message teaches the time rule and does not also ask for a result."""
    if not teaches_missing_time_means_all(text):
        return False
    folded = text.lower()
    intent = (
        "top ",
        "how many",
        "which ",
        "list ",
        "show ",
        "count",
        "traffic",
        "byte",
        "flow",
        "subscriber",
        "whatsapp",
        "facebook",
        "tiktok",
        "youtube",
        "telegram",
        "instagram",
        "download",
        "upload",
        "application",
        "app label",
    )
    return not any(word in folded for word in intent)
