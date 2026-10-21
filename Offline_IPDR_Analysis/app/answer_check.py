"""Catch SQL that drops the subject of the question or the previous query."""

from __future__ import annotations

import re

APP_NAMES = (
    "whatsapp", "facebook", "messenger", "tiktok", "youtube", "instagram", "snapchat",
    "telegram", "imo", "zoom", "gmail", "netflix", "spotify", "pubg", "telegram",
    "viber", "wechat", "discord", "signal", "skype", "reddit",
)
TIME_COLUMNS = ("start_time", "end_time", "recv_time")


def check_sql(question: str, sql: str, history: list[dict] | None, full_scan: bool) -> str | None:
    prior = [str(item.get("question") or "") for item in (history or [])]
    conversation = " ".join(prior + [question]).lower()
    current = question.lower()
    folded = re.sub(r"\s+", " ", sql.lower())
    problems = []

    if _asks_for_users(conversation) and not _asks_for_apps_of_one_subscriber(conversation):
        if "group by subscriber_id" not in folded:
            problems.append(
                "Group by subscriber_id. Users and subscribers are subscriber_id, not app_label."
            )
        if not _excludes_unidentified(folded):
            problems.append(
                "Exclude unidentified ids with AND IFNULL(subscriber_id, '') <> '' "
                "AND LOCATE('_', subscriber_id) = 0. A value like 203.0.113.10_56175 is client IPv4 and port."
            )

    for name in _required_apps(current, prior):
        if name not in folded:
            problems.append(
                f"Keep the app filter LOWER(app_label) LIKE '%{name}%'. "
                "The app name is a filter. Do not drop it when the grouping column changes."
            )

    if _forbids_new_time_filter(current, history, full_scan) and any(column in folded for column in TIME_COLUMNS):
        problems.append(
            "Do not add start_time, end_time, or recv_time. Keep the previous query's time scope."
        )

    kind = _subscriber_kind(current)
    if kind == "mobile":
        if not any(column in folded for column in ("mcc", "mnc", "lac", "cid", "usr_loc_info")):
            problems.append(
                "Mobile users are valid subscriber ids with cell data. "
                "Add AND (NULLIF(mcc, '') IS NOT NULL OR NULLIF(mnc, '') IS NOT NULL "
                "OR NULLIF(lac, '') IS NOT NULL OR NULLIF(cid, '') IS NOT NULL "
                "OR NULLIF(usr_loc_info, '') IS NOT NULL). "
                "Do not filter app_label."
            )
        if re.search(r"app_label[^;]{0,80}like\s+'%(mobile|fixed)", folded):
            problems.append("Do not use app_label LIKE '%mobile%' or LIKE '%fixed%'. Mobile is not an application.")
    elif kind == "fixed":
        if "not" not in folded or not any(column in folded for column in ("mcc", "mnc", "lac", "cid", "usr_loc_info")):
            problems.append(
                "Fixed-line users are valid subscriber ids whose mcc, mnc, lac, cid, and usr_loc_info are all empty. "
                "Do not filter app_label."
            )

    if re.search(r"like\s+'%_+'", folded) and "\\" not in folded:
        problems.append(
            "Do not use LIKE '%_%'. Underscore is a LIKE wildcard. Use LOCATE('_', subscriber_id) = 0."
        )

    if not problems:
        return None
    return " ".join(problems)


def _subscriber_kind(question: str) -> str | None:
    text = re.sub(r"\s+", " ", question.lower().replace("_", "").replace("-", " "))
    exclude_fixed = "exclude" in text and "fixed" in text
    wants_mobile = bool(re.search(r"\bmobile (users|user|subscribers|subscriber)\b", text)) or exclude_fixed
    wants_fixed = bool(re.search(r"\bfixed ?line (users|user|subscribers|subscriber)\b", text))
    if wants_mobile:
        return "mobile"
    if wants_fixed:
        return "fixed"
    return None


def _asks_for_users(text: str) -> bool:
    if "unidentified" in text and not re.search(r"\b(exclude|not valid|not a valid|invalid)\b", text):
        return False
    return bool(re.search(r"\b(users|user|subscribers|subscriber_id|subscriber ids|subscriber id)\b", text))


def _asks_for_apps_of_one_subscriber(text: str) -> bool:
    return "which apps did subscriber" in text or "which app did subscriber" in text


def _excludes_unidentified(sql: str) -> bool:
    compact = sql.replace(" ", "")
    return (
        "locate('_'" in compact
        or 'locate("_"' in compact
        or "instr(subscriber_id,'_')=0" in compact
        or "notlike'%\\_%'" in compact
    )


def _required_apps(current: str, prior: list[str]) -> list[str]:
    current_apps = _apps(current)
    if current_apps:
        return current_apps
    return _apps(" ".join(prior))


def _apps(text: str) -> list[str]:
    found = []
    for name in APP_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", text) and name not in found:
            found.append(name)
    return found


def _forbids_new_time_filter(current: str, history: list[dict] | None, full_scan: bool) -> bool:
    if any(word in current for word in ("yesterday", "today", "last ", "hour", "between", "since", "this week")):
        return False
    previous = ""
    for item in reversed(history or []):
        if item.get("sql"):
            previous = str(item["sql"]).lower()
            break
    if previous and not any(column in previous for column in TIME_COLUMNS):
        return True
    return full_scan and not previous
