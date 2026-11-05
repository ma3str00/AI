"""MySQL → ClickHouse SQL translation for ntc_ssl_logs queries.

Builders and the LLM historically emit MySQL. Execution targets ClickHouse, so
every SELECT is normalized here before it reaches the database.
"""

from __future__ import annotations

import re


def to_clickhouse(sql: str) -> str:
    """Convert assistant-generated MySQL SELECT fragments into ClickHouse SQL."""
    text = re.sub(r"\s+", " ", (sql or "").strip())
    if not text:
        return text
    text = _rewrite_group_concat(text)
    # MySQL LOCATE(needle, haystack) → ClickHouse position(haystack, needle).
    text = re.sub(
        r"(?i)\bLOCATE\(\s*'([^']*)'\s*,\s*([^)]+?)\)",
        r"position(\2, '\1')",
        text,
    )
    text = re.sub(r"(?i)\bLOCATE\(\s*([^,]+?)\s*,\s*([^)]+?)\)", r"position(\2, \1)", text)
    text = re.sub(r"(?i)\bIFNULL\s*\(", "ifNull(", text)
    text = re.sub(r"(?i)\bNULLIF\s*\(", "nullIf(", text)
    # col REGEXP 'pat' → match(col, 'pat')
    text = re.sub(
        r"(?i)([a-z0-9_().]+)\s+REGEXP\s+'([^']*)'",
        r"match(\1, '\2')",
        text,
    )
    text = _rewrite_unix_timestamp(text)
    # ClickHouse prefers != ; keep <> which also works.
    return re.sub(r"\s+", " ", text).strip()


def address_expression(column: str) -> str:
    """Distinct non-blank values for a subscriber/group, ClickHouse form."""
    return (
        f"arrayStringConcat(arraySort(groupUniqArray(nullIf({column}, ''))), ', ') AS {column}"
    )


def identified_subscriber_sql() -> str:
    return "ifNull(subscriber_id, '') != '' AND position(subscriber_id, '_') = 0"


def unidentified_subscriber_sql() -> str:
    return "position(ifNull(subscriber_id, ''), '_') > 0"


def _rewrite_group_concat(sql: str) -> str:
    text = sql

    def blank_skip(column: str) -> str:
        return (
            f"arrayStringConcat(arraySort(groupUniqArray(nullIf({column}, ''))), ', ') AS {column}"
        )

    # GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(col, '') <> '' THEN col END ORDER BY col SEPARATOR ', ') AS col
    pattern_case = re.compile(
        r"(?i)GROUP_CONCAT\s*\(\s*DISTINCT\s+CASE\s+WHEN\s+IFNULL\(\s*([a-z0-9_]+)\s*,\s*''\s*\)\s*<>\s*''\s+"
        r"THEN\s+\1\s+END\s+ORDER\s+BY\s+\1\s+SEPARATOR\s+', '\s*\)\s+AS\s+\1"
    )
    text = pattern_case.sub(lambda m: blank_skip(m.group(1)), text)

    # Same with ifNull already rewritten? Keep MySQL IFNULL form only above.

    # GROUP_CONCAT(DISTINCT col ORDER BY col SEPARATOR ', ') AS col
    pattern_plain = re.compile(
        r"(?i)GROUP_CONCAT\s*\(\s*DISTINCT\s+([a-z0-9_]+)\s+ORDER\s+BY\s+\1\s+SEPARATOR\s+', '\s*\)\s+AS\s+\1"
    )
    text = pattern_plain.sub(
        lambda m: f"arrayStringConcat(arraySort(groupUniqArray({m.group(1)})), ', ') AS {m.group(1)}",
        text,
    )

    # Fallback: any remaining GROUP_CONCAT(...) AS alias — best-effort.
    def _fallback(match: re.Match[str]) -> str:
        body = match.group(1)
        alias = match.group(2)
        col = re.search(r"(?i)\b([a-z0-9_]+)\b(?:\s+SEPARATOR|\s*\)|\s+ORDER|\s+END)", body)
        name = col.group(1) if col else alias
        if re.search(r"(?i)IFNULL|nullIf|<>|!=", body):
            return blank_skip(name if name != "DISTINCT" else alias)
        return f"arrayStringConcat(arraySort(groupUniqArray({name})), ', ') AS {alias}"

    text = re.sub(
        r"(?i)GROUP_CONCAT\s*\((.+?)\)\s+AS\s+([a-z0-9_]+)",
        _fallback,
        text,
    )
    return text


def _rewrite_unix_timestamp(sql: str) -> str:
    text = sql
    # UNIX_TIMESTAMP(CURDATE()) → start of today in session TZ as unix seconds.
    text = re.sub(
        r"(?i)UNIX_TIMESTAMP\s*\(\s*CURDATE\s*\(\s*\)\s*\)",
        "toUnixTimestamp(toStartOfDay(now()))",
        text,
    )
    text = re.sub(
        r"(?i)UNIX_TIMESTAMP\s*\(\s*CURDATE\s*\(\s*\)\s*\+\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
        r"toUnixTimestamp(toStartOfDay(now()) + toIntervalDay(\1))",
        text,
    )
    text = re.sub(
        r"(?i)UNIX_TIMESTAMP\s*\(\s*CURDATE\s*\(\s*\)\s*-\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
        r"toUnixTimestamp(toStartOfDay(now()) - toIntervalDay(\1))",
        text,
    )
    text = re.sub(
        r"(?i)UNIX_TIMESTAMP\s*\(\s*NOW\s*\(\s*\)\s*-\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
        r"toUnixTimestamp(now() - toIntervalDay(\1))",
        text,
    )
    text = re.sub(
        r"(?i)UNIX_TIMESTAMP\s*\(\s*'([^']+)'\s*\)",
        r"toUnixTimestamp(toDateTime('\1'))",
        text,
    )
    text = re.sub(r"(?i)\bCURDATE\s*\(\s*\)", "toDate(now())", text)
    text = re.sub(r"(?i)\bNOW\s*\(\s*\)", "now()", text)
    return text
