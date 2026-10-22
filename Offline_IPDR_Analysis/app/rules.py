"""Rules taken from IPDR_Data_Description.csv. Every query is corrected against them."""

from __future__ import annotations

import re

from app.followup import subscriber_kind
from app.schema import DEAD_COLUMNS, IDENTIFIED_SQL, MOBILE_SQL

_CELL = re.escape(re.sub(r"\s+", " ", MOBILE_SQL))


def repair_sql(question: str, sql: str, history: list[dict] | None) -> str:
    """Apply the dictionary when the correction is mechanical."""
    text = re.sub(r"\s+", " ", sql).strip()
    text = re.sub(
        r"\s+AND\s+LOWER\(app_label\)\s+LIKE\s+'%(?:mobile|fixed(?:line)?)%'",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"LOWER\(app_label\)\s+LIKE\s+'%(?:mobile|fixed(?:line)?)%'\s+AND\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    kind = subscriber_kind([str(item.get("question") or "") for item in (history or [])] + [question])
    if kind == "mobile":
        text = _set_cell(text, "mobile")
    elif kind == "fixed":
        text = _set_cell(text, "fixed")
    elif _has_cell(text):
        text = _strip_cell(text)
    if _user_ranking(text) and not _excludes_unidentified(text):
        text = re.sub(r"\bGROUP BY\b", f"AND {IDENTIFIED_SQL} GROUP BY", text, count=1, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def dictionary_problem(question: str, sql: str) -> str | None:
    folded = sql.lower()
    for column, why in DEAD_COLUMNS.items():
        if re.search(rf"\b{re.escape(column)}\b", folded):
            return f"{column} is {why} in ntc_ssl_logs, so this cannot be answered from that column."
    kind = subscriber_kind([question])
    if kind == "mobile" and not _positive_cell(sql):
        return (
            "Mobile users need a filled mcc, mnc, lac, cid, or usr_loc_info. "
            "Do not filter app_label for mobile."
        )
    if kind == "fixed" and not _negative_cell(sql):
        return (
            "Fixed-line users are valid subscriber ids whose mcc, mnc, lac, cid, and usr_loc_info are empty. "
            "Do not filter app_label for fixed-line."
        )
    if _user_ranking(sql) and not _excludes_unidentified(sql):
        return "Exclude subscriber ids that contain '_' . Those values are client IP and port."
    return None


def _set_cell(sql: str, kind: str) -> str:
    text = _strip_cell(sql)
    clause = f"AND NOT {MOBILE_SQL}" if kind == "fixed" else f"AND {MOBILE_SQL}"
    if re.search(r"\bGROUP BY\b", text, flags=re.IGNORECASE):
        return re.sub(r"\bGROUP BY\b", f"{clause} GROUP BY", text, count=1, flags=re.IGNORECASE)
    return f"{text} {clause}"


def _strip_cell(sql: str) -> str:
    text = re.sub(rf"\s+AND\s+NOT\s+{_CELL}", "", sql, flags=re.IGNORECASE)
    text = re.sub(rf"\s+AND\s+{_CELL}", "", text, flags=re.IGNORECASE)
    loose = r"\(\s*NULLIF\(\s*mcc\b.*?NULLIF\(\s*usr_loc_info\b.*?\)\s*\)"
    text = re.sub(rf"\s+AND\s+NOT\s+{loose}", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"\s+AND\s+{loose}", "", text, flags=re.IGNORECASE)
    return text


def _has_cell(sql: str) -> bool:
    folded = sql.lower()
    return any(column in folded for column in ("mcc", "mnc", "lac", "cid", "usr_loc_info"))


def _positive_cell(sql: str) -> bool:
    return _has_cell(sql) and not _negative_cell(sql)


def _negative_cell(sql: str) -> bool:
    compact = re.sub(r"\s+", "", sql.lower())
    return "andnot(nullif(mcc" in compact or "andnot(nullif(usr_loc_info" in compact


def _user_ranking(sql: str) -> bool:
    folded = re.sub(r"\s+", " ", sql.lower())
    return "group by subscriber_id" in folded


def _excludes_unidentified(sql: str) -> bool:
    compact = sql.lower().replace(" ", "")
    return "locate('_'" in compact or 'locate("_"' in compact or "instr(subscriber_id,'_')=0" in compact
