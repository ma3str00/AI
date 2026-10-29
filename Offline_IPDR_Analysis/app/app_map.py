"""Resolve app names in a question to labels from the mapping file."""

from __future__ import annotations

import csv
import re
from pathlib import Path

# Spoken phrases that mean a table category. The table stores mixed case; SQL uses lowercase.
CATEGORY_PHRASES = (
    ("instant messaging", "im"),
    ("web browsing", "web_browsing"),
    ("software update", "software_update"),
    ("file access", "file_access_protocol"),
    ("cloud storage", "network_storage"),
    ("network storage", "network_storage"),
    ("http video", "http_video"),
    ("video streaming", "streaming"),
    ("p2p", "p2p_download"),
    ("vpn", "tunneling"),
    ("proxy", "tunneling"),
    ("tunnel", "tunneling"),
    ("voip", "voip"),
    ("voice call", "voip"),
    ("gaming", "game"),
    ("games", "game"),
    ("email", "email"),
    ("streaming", "streaming"),
    ("browsing", "web_browsing"),
)

# These words describe the subscriber, not an application label.
_NOT_APP_WORDS = {
    "mobile", "fixed", "fixedline", "line", "cell", "cells",
    "user", "users", "subscriber", "subscribers",
}


class AppMap:
    def __init__(self, rows: list[tuple[str, str]]):
        self.rows = rows
        self.label_tokens: set[str] = set()
        self.exact_labels: set[str] = set()
        for label, _category in rows:
            folded = label.lower()
            self.exact_labels.add(folded)
            for part in re.split(r"[^a-z0-9]+", folded):
                if len(part) >= 4 and part not in _NOT_APP_WORDS:
                    self.label_tokens.add(part)

    def known_app_token(self, text: str, skip: set[str] | None = None) -> str | None:
        """The longest word in the question that is an actual app_label.

        A word is not an application just because it is left over. Gulshan is a
        place, and it is not in the mapping, so it does not become a filter.
        Short exact labels such as ssl are recognized only through exact_app_label.
        """
        ignored = skip or set()
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]{4,}", text.lower())
            if token not in ignored and token not in _NOT_APP_WORDS and token in self.label_tokens
        ]
        if not tokens:
            return None
        return max(tokens, key=len)

    @classmethod
    def load(cls, path: Path) -> "AppMap":
        rows: list[tuple[str, str]] = []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for record in csv.DictReader(handle):
                label = (record.get("app_label") or "").strip()
                category = (record.get("app_category") or "").strip().lower()
                if label:
                    rows.append((label, category))
        return cls(rows)

    def hints(self, question: str) -> str:
        if not self.rows:
            return "No app-label mapping is loaded."
        folded = re.sub(r"[^a-z0-9]+", " ", question.lower())
        compact = folded.replace(" ", "")
        tokens = {token for token in folded.split() if len(token) >= 4 and token not in _NOT_APP_WORDS}

        matched: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, category in self.rows:
            key = label.lower()
            if key in seen:
                continue
            parts = [part for part in re.split(r"[^a-z0-9]+", key) if len(part) >= 4 and part not in _NOT_APP_WORDS]
            normalized = re.sub(r"[^a-z0-9]+", "", key)
            if normalized in _NOT_APP_WORDS:
                continue
            if (len(normalized) >= 4 and normalized in compact) or any(part in tokens for part in parts):
                seen.add(key)
                matched.append((label, category))

        lines = []
        if re.search(r"\bmobile\b|\bfixedline\b|\bfixed line\b", question.lower()):
            lines.append(
                "Mobile and fixed-line are subscriber types, not app labels. "
                "Mobile means mcc, mnc, lac, cid, or usr_loc_info is filled. "
                "Fixed-line means a valid subscriber_id with those cell fields empty."
            )
        categories = []
        for phrase, category in CATEGORY_PHRASES:
            if phrase in folded and category not in categories:
                categories.append(category)
        if categories:
            listed = ", ".join(f"'{item}'" for item in categories)
            lines.append(
                f"Category wording in the question maps to LOWER(app_category) IN ({listed})."
            )

        if not matched:
            if not lines:
                return "No specific app_label matched the question."
            return "\n".join(lines)

        if len(matched) > 20:
            # A stem such as "google" or "apple" covers too many labels for an IN list.
            stem = _shared_stem(matched, tokens)
            lines.append(
                f"{len(matched)} mapping labels match. Filter them with "
                f"LOWER(app_label) LIKE '%{stem}%' instead of listing them."
            )
        else:
            values = ", ".join(f"'{label.lower()}'" for label, _ in matched)
            lines.append(f"Matching mapping labels: LOWER(app_label) IN ({values}).")
            cats = sorted({category for _, category in matched if category})
            if cats:
                lines.append("Those labels are mapped to categories: " + ", ".join(cats) + ".")
        return "\n".join(lines)


_active: AppMap | None = None


def activate_app_map(mapping: AppMap) -> None:
    """The running app and the follow-up rules use this loaded mapping."""
    global _active
    _active = mapping


def app_map_loaded() -> bool:
    return _active is not None and bool(_active.label_tokens)


def known_app_token(text: str, skip: set[str] | None = None) -> str | None:
    if _active is None:
        return None
    return _active.known_app_token(text, skip)


def exact_app_label(token: str) -> bool:
    if _active is None:
        return False
    return token.lower() in _active.exact_labels


def label_token_known(token: str) -> bool:
    if _active is None:
        return False
    folded = token.lower()
    return folded in _active.label_tokens or folded in _active.exact_labels


def _shared_stem(matched: list[tuple[str, str]], tokens: set[str]) -> str:
    for token in sorted(tokens, key=len, reverse=True):
        if any(token in label.lower() for label, _ in matched):
            return token
    return re.sub(r"[^a-z0-9]+", "", matched[0][0].lower())[:24]
