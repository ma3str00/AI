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


class AppMap:
    def __init__(self, rows: list[tuple[str, str]]):
        self.rows = rows

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
        tokens = {token for token in folded.split() if len(token) >= 4}

        matched: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, category in self.rows:
            key = label.lower()
            if key in seen:
                continue
            parts = [part for part in re.split(r"[^a-z0-9]+", key) if len(part) >= 4]
            normalized = re.sub(r"[^a-z0-9]+", "", key)
            if (len(normalized) >= 4 and normalized in compact) or any(part in tokens for part in parts):
                seen.add(key)
                matched.append((label, category))

        lines = []
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


def _shared_stem(matched: list[tuple[str, str]], tokens: set[str]) -> str:
    for token in sorted(tokens, key=len, reverse=True):
        if any(token in label.lower() for label, _ in matched):
            return token
    return re.sub(r"[^a-z0-9]+", "", matched[0][0].lower())[:24]
