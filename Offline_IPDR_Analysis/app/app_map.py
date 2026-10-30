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

# Conceptual classes on top of DPI app_category. One stem may sit in several classes
# (WhatsApp → messaging + social_media, YouTube → video_streaming + social_media).
# Matching uses LOWER(app_label) LIKE '%stem%'. Prefer distinctive stems.
APP_CLASSES: dict[str, dict[str, tuple[str, ...]]] = {
    "social_media": {
        "phrases": (
            "social media",
            "social networking",
            "social network",
            "social apps",
            "social applications",
        ),
        "stems": (
            "facebook", "instagram", "tiktok", "whatsapp", "twitter", "snapchat",
            "linkedin", "reddit", "pinterest", "threads", "youtube", "discord",
            "telegram", "signal", "viber", "wechat", "imo", "botim", "bigo",
            "likee", "kuaishou", "clubhouse", "twitch", "tinder", "badoo", "grindr",
        ),
    },
    "messaging": {
        "phrases": (
            "messaging apps",
            "messaging applications",
            "chat apps",
            "chat applications",
            "messenger apps",
            "im apps",
        ),
        "stems": (
            "whatsapp", "telegram", "signal", "viber", "wechat", "imo", "botim",
            "discord", "snapchat", "line_im", "facebook_messenger", "imessage",
            "skype_im", "slack", "bipmessenger", "beetalk", "firechat",
        ),
    },
    "video_streaming": {
        "phrases": (
            "video streaming",
            "streaming video",
            "video apps",
            "ott apps",
            "ott platforms",
        ),
        "stems": (
            "youtube", "netflix", "disney", "hotstar", "hulu", "amazoninstantvideo",
            "twitch", "tiktok", "kuaishou", "bigo", "dazn", "megavideo",
        ),
    },
    "music": {
        "phrases": (
            "music apps",
            "music streaming",
            "audio streaming",
            "music applications",
        ),
        "stems": (
            "spotify", "soundcloud", "deezer", "youtubemusic", "itunes", "anghami",
            "pandora",
        ),
    },
    "gaming": {
        "phrases": (
            "gaming apps",
            "game apps",
            "mobile games",
            "online games",
        ),
        "stems": (
            "roblox", "pubg", "freefire", "fortnite", "steam", "youtubegaming",
            "discord",
        ),
    },
    "dating": {
        "phrases": (
            "dating apps",
            "dating applications",
            "dating sites",
        ),
        "stems": (
            "tinder", "badoo", "grindr", "meetme", "meetic",
        ),
    },
    "conferencing": {
        "phrases": (
            "video conferencing",
            "conferencing apps",
            "meeting apps",
            "online meetings",
            "collaboration apps",
        ),
        "stems": (
            "zoom", "microsoftteams", "googlemeet", "skype", "jitsimeet",
            "gotomeeting", "webex", "slack",
        ),
    },
    "email": {
        "phrases": (
            "email apps",
            "mail apps",
            "email applications",
        ),
        "stems": (
            "gmail", "outlook", "protonmail", "webmail", "yahoo",
        ),
    },
    "cloud_storage": {
        "phrases": (
            "cloud storage",
            "cloud apps",
            "file storage apps",
            "storage apps",
        ),
        "stems": (
            "dropbox", "onedrive", "icloud", "googledrive", "google_drive",
            "amazons3", "huaweicloud", "megashare", "megaupload",
        ),
    },
    "vpn": {
        "phrases": (
            "vpn apps",
            "vpn applications",
            "proxy apps",
            "tunnel apps",
        ),
        "stems": (
            "nordvpn", "expressvpn", "protonvpn", "psiphon", "hotspotvpn",
            "icloudprivaterelay",
        ),
    },
    "shopping": {
        "phrases": (
            "shopping apps",
            "ecommerce",
            "e-commerce",
            "online shopping",
            "retail apps",
        ),
        "stems": (
            "amazon", "ebay", "aliexpress", "shopify", "daraz",
        ),
    },
    "maps_transport": {
        "phrases": (
            "maps apps",
            "navigation apps",
            "ride hailing",
            "ride-hailing",
            "transport apps",
        ),
        "stems": (
            "waze", "yandexmaps", "uber", "careem", "googlemaps", "google_maps",
        ),
    },
    "payments": {
        "phrases": (
            "payment apps",
            "fintech apps",
            "wallet apps",
            "banking apps",
        ),
        "stems": (
            "paypal", "jazzcash", "easypaisa", "stripe", "venmo",
        ),
    },
    "ai_assistants": {
        "phrases": (
            "ai apps",
            "ai assistants",
            "chatgpt apps",
            "generative ai",
        ),
        "stems": (
            "chatgpt", "openai", "gemini", "copilot", "claude",
        ),
    },
    "live_streaming": {
        "phrases": (
            "live streaming",
            "livestream apps",
            "live video apps",
        ),
        "stems": (
            "bigo", "kuaishou", "tiktok_live", "twitch", "youtube_openlive",
            "facebook_openlive",
        ),
    },
}

# Words that come from class phrases and must not become LIKE '%media%' stems.
CLASS_PHRASE_STOPWORDS = frozenset(
    {
        "social", "media", "networking", "network", "apps", "applications",
        "messaging", "chat", "messenger", "video", "streaming", "ott", "platforms",
        "music", "audio", "gaming", "game", "games", "mobile", "online", "dating",
        "sites", "conferencing", "meeting", "meetings", "collaboration", "email",
        "mail", "cloud", "storage", "file", "vpn", "proxy", "tunnel", "shopping",
        "ecommerce", "retail", "maps", "navigation", "ride", "hailing", "transport",
        "payment", "fintech", "wallet", "banking", "ai", "assistants", "chatgpt",
        "generative", "live", "livestream", "instant", "popular", "used", "most",
    }
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
        class_id = class_from_question(question)
        if class_id:
            stems = stems_for_class(class_id)
            preview = ", ".join(f"'%{stem}%'" for stem in stems[:12])
            more = "" if len(stems) <= 12 else f" (+{len(stems) - 12} more)"
            lines.append(
                f"Class '{class_id}' is a multi-label app group (not app_category). "
                f"Filter with OR of LOWER(app_label) LIKE on stems: {preview}{more}."
            )
            siblings = sorted(classes_for_stem(stems[0])) if stems else []
            if len(siblings) > 1:
                lines.append(
                    f"Stems can belong to multiple classes; e.g. '{stems[0]}' → "
                    + ", ".join(siblings)
                    + "."
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


def class_from_question(question: str) -> str | None:
    """Return the conceptual app class named in the question, if any.

    Phrases that already map to DPI app_category (CATEGORY_PHRASES) are left to
    that path so 'cloud storage' / 'video streaming' keep using app_category.
    """
    folded = re.sub(r"\s+", " ", question.lower()).strip()
    dpi_phrases = {phrase for phrase, _ in CATEGORY_PHRASES}
    best: tuple[int, str] | None = None
    for class_id, spec in APP_CLASSES.items():
        for phrase in spec["phrases"]:
            if phrase in dpi_phrases:
                continue
            if phrase in folded:
                score = len(phrase)
                if best is None or score > best[0]:
                    best = (score, class_id)
    return best[1] if best else None


def stems_for_class(class_id: str) -> list[str]:
    spec = APP_CLASSES.get(class_id)
    if not spec:
        return []
    return list(spec["stems"])


def classes_for_stem(stem: str) -> list[str]:
    folded = stem.lower()
    return sorted(
        class_id
        for class_id, spec in APP_CLASSES.items()
        if folded in {item.lower() for item in spec["stems"]}
    )


def _stem_label_predicate(stem: str) -> str:
    """Match an app_label token. Short stems use boundaries so 'imo' ≠ HuaweiMobileCloud."""
    folded = re.sub(r"[^a-z0-9_]+", "", stem.lower())
    if not folded:
        return "1=0"
    # 3-letter stems are common inside unrelated labels (huawei + mobile → …eimo…).
    if len(folded) <= 3:
        safe = re.escape(folded)
        return f"LOWER(app_label) REGEXP '(^|[^a-z0-9]){safe}([^a-z0-9]|$)'"
    return f"LOWER(app_label) LIKE '%{folded}%'"


def class_label_predicate(class_id: str) -> str | None:
    """SQL boolean for labels that belong to a conceptual class."""
    stems = stems_for_class(class_id)
    if not stems:
        return None
    parts = [_stem_label_predicate(stem) for stem in stems]
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def _shared_stem(matched: list[tuple[str, str]], tokens: set[str]) -> str:
    for token in sorted(tokens, key=len, reverse=True):
        if any(token in label.lower() for label, _ in matched):
            return token
    return re.sub(r"[^a-z0-9]+", "", matched[0][0].lower())[:24]
