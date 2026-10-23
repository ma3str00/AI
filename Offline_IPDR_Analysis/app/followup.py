"""Apply a follow-up once the user has already settled the open choice."""

from __future__ import annotations

import re

from app.schema import MOBILE_SQL
from app.sql_guard import wants_all_groups, wants_full_table

_TIME_WORDS = ("yesterday", "today", "last ", "hour", "between", "since", "this week")


def resolve_followup(question: str, history: list[dict] | None) -> str | None:
    """Build SQL from the last real query when the reply only changes one part of it."""
    history = history or []
    sites = _websites_of_one_subscriber(question, history)
    if sites:
        return sites
    apps = _apps_of_one_subscriber(question, history)
    if apps:
        return apps
    unlimited = _without_row_limit(question, history)
    if unlimited:
        return unlimited
    if _pending(history) and _answer_keeps(question):
        return _revise(str(history[-1].get("question") or ""), history[:-1], force_keep=True)
    if _pending(history) and _answer_drops(question):
        return _revise(str(history[-1].get("question") or ""), history[:-1], force_drop=True)
    previous = _last_sql(history)
    presented = _present_measures(question, previous)
    if presented:
        return presented
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    revised = _revise(question, history)
    if revised:
        return revised
    texts = [str(item.get("question") or "") for item in history]
    kind = _subscriber_kind(texts + [question])
    if kind not in {"mobile", "fixed"}:
        return None
    if _names_time(question):
        return None
    blob = " ".join(texts + [question]).lower()
    keep, drop = _app_choice(blob, previous)
    if "for the same" in question.lower():
        keep = True
    if _has_app_filter(previous) and not keep and not drop:
        return None
    if _subscriber_request(question) and not keep and not drop:
        return None
    limit = _limit_in(question) or _latest_limit(texts) or _limit_of(previous) or 5
    return _rewrite(previous, keep_app=not drop, kind=kind, limit=limit)


def _without_row_limit(question: str, history: list[dict]) -> str | None:
    previous = _last_sql(history)
    if not previous or not wants_all_groups(question):
        return None
    if _group_key(previous) not in {"app_label", "app_category"}:
        return None
    text = re.sub(r"\s+", " ", previous).strip()
    return re.sub(r"(?i)\s+LIMIT\s+\d+\s*$", "", text).strip()


def conversation_full_scan(question: str, history: list[dict] | None) -> bool:
    if _names_time(question):
        return False
    blob = " ".join(str(item.get("question") or "") for item in (history or [])) + " " + question
    folded = blob.lower()
    if any(
        phrase in folded
        for phrase in (
            "no time filter",
            "no time range",
            "doesnt specify",
            "doesn't specify",
            "does not specify",
            "all the data",
            "all data",
        )
    ):
        return True
    previous = _last_sql(history or [])
    return bool(previous) and not any(column in previous.lower() for column in ("start_time", "end_time", "recv_time"))


def use_full_scan(question: str, history: list[dict] | None, missing_time_means_all: bool) -> bool:
    """A saved rule runs the whole table when this question names no time window.

    A follow-up of a query that already has a time filter keeps that filter.
    """
    if _names_time(question):
        return False
    if conversation_full_scan(question, history) or wants_full_table(question):
        return True
    if _last_sql(history or []):
        return False
    return missing_time_means_all


def _websites_of_one_subscriber(question: str, history: list[dict]) -> str | None:
    """TLS server names requested by one subscriber. A website is requested_server."""
    subscriber = named_subscriber(question)
    if not subscriber and _points_at_known_user(question):
        subscriber = _subscriber_from_history(history)
    if not subscriber:
        return None
    supplying_name = bool(named_subscriber(question)) or "referring to" in question.lower()
    if not _asks_for_websites(question) and not (supplying_name and _recent_unanswered(history, _asks_for_websites)):
        return None
    where = [f"subscriber_id = '{subscriber}'", "IFNULL(requested_server, '') <> ''"]
    time_clause = _time_clause(_last_sql(history))
    if time_clause:
        where.append(time_clause)
    texts = [question, *[str(item.get("question") or "") for item in history[-6:]]]
    limit = _limit_in(question)
    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
    if limit is None and not any(_wants_every_listed_site(text) for text in texts):
        limit_sql = " LIMIT 50"
    return (
        "SELECT requested_server, COUNT(*) AS flows, "
        "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
        f"FROM ntc_ssl_logs WHERE {' AND '.join(where)} "
        f"GROUP BY requested_server ORDER BY total_bytes DESC{limit_sql}"
    )


def _asks_for_websites(text: str) -> bool:
    return bool(re.search(r"\b(websites?|sites?|domains?|sni|hostnames?)\b", text.lower()))


def _wants_every_listed_site(text: str) -> bool:
    folded = text.lower()
    return bool(re.search(r"\b(all|every)\b", folded) and _asks_for_websites(folded))


def lists_one_subscriber_apps(question: str, history: list[dict] | None) -> bool:
    return _apps_of_one_subscriber(question, history or []) is not None


def _apps_of_one_subscriber(question: str, history: list[dict]) -> str | None:
    """Every application used by one subscriber, including 'this user' and 'the rest of their activities'."""
    subscriber = named_subscriber(question)
    if not subscriber and _points_at_known_user(question):
        subscriber = _subscriber_from_history(history)
    if not subscriber:
        return None
    supplying_name = bool(named_subscriber(question)) or "referring to" in question.lower()
    if not _asks_for_their_apps(question) and not (supplying_name and _recent_unanswered(history, _asks_for_their_apps)):
        return None
    where = [f"subscriber_id = '{subscriber}'"]
    time_clause = _time_clause(_last_sql(history))
    if time_clause:
        where.append(time_clause)
    texts = [question, *[str(item.get("question") or "") for item in history[-6:]]]
    limit = _limit_in(question)
    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
    if limit is None and not any(_wants_every_listed_app(text) for text in texts):
        limit_sql = " LIMIT 20"
    return (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        f"FROM ntc_ssl_logs WHERE {' AND '.join(where)} "
        f"GROUP BY app_label ORDER BY total_bytes DESC{limit_sql}"
    )


def _asks_for_their_apps(text: str) -> bool:
    folded = text.lower()
    if re.search(r"\b(applications?|apps)\b", folded):
        return True
    if re.search(r"\bactivities\b", folded):
        return True
    if re.search(r"\bactivity\s+details?\b", folded) or re.search(r"\bdetails?\s+of\b", folded):
        return True
    return bool(re.search(r"\b(the rest|rest of|what else|everything else)\b", folded))


def _wants_every_listed_app(text: str) -> bool:
    folded = text.lower()
    if re.search(r"\b(all|every)\b", folded) and re.search(r"\b(applications?|apps|activities|activity)\b", folded):
        return True
    if re.search(r"\bother\s+(activities|activity|applications?|apps)\b", folded):
        return True
    if re.search(r"\bdetails?\b", folded):
        return True
    return bool(re.search(r"\b(the rest|rest of|what else|everything else)\b", folded))


def _points_at_known_user(text: str) -> bool:
    return bool(re.search(r"\b(this|that|same|the)\s+(user|subscriber)\b", text.lower()))


def _subscriber_from_history(history: list[dict]) -> str | None:
    for item in reversed(history):
        found = named_subscriber(str(item.get("question") or ""))
        if found:
            return found
    return None


def _recent_unanswered(history: list[dict], predicate) -> bool:
    """True when a question still waiting for SQL matches the predicate."""
    for item in reversed(history[-8:]):
        if str(item.get("sql") or "").strip():
            break
        if predicate(str(item.get("question") or "")):
            return True
    return False


def named_subscriber(question: str) -> str | None:
    """The subscriber id named in the question, including 'used by someid123'."""
    patterns = (
        r"\b(?:user|subscriber)\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
        r"\bby\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
        r"\bof\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, question)
        if not match:
            continue
        token = match.group(1)
        if _subscriber_token(token):
            return token
    return None


def _subscriber_token(token: str) -> bool:
    if token.lower() in _NOT_SUBSCRIBER:
        return False
    if re.fullmatch(r"\d{1,5}", token):
        return False
    return bool(re.search(r"\d", token) or "-" in token or "_" in token)


_NOT_SUBSCRIBER = {
    "the", "number", "traffic", "volume", "flow", "flows", "total", "bytes",
    "application", "applications", "apps", "user", "subscriber", "users",
    "facebook", "youtube", "whatsapp", "tiktok", "instagram", "telegram",
}


def _time_clause(sql: str) -> str:
    if not sql:
        return ""
    parts = re.findall(
        r"((?:start_time|end_time|recv_time)\s*(?:>=|<=|<>|!=|=|>|<)\s*(?:UNIX_TIMESTAMP\([^)]*\)|'[^']*'|\d+))",
        sql,
        flags=re.IGNORECASE,
    )
    return " AND ".join(parts)


def _last_sql(history: list[dict]) -> str:
    for item in reversed(history):
        sql = str(item.get("sql") or "").strip()
        if sql:
            return sql
    return ""


def _subscriber_kind(texts: list[str]) -> str | None:
    """The latest mobile, fixed-line, or all-connections request wins."""
    found = None
    for text in texts:
        if _rejects_connection(text):
            found = "any"
        elif _mentions_fixed(text) and not _mentions_mobile(text):
            found = "fixed"
        elif _mentions_mobile(text):
            found = "mobile"
    return found


def subscriber_kind(texts: list[str]) -> str | None:
    return _subscriber_kind(texts)


def _limit_in(text: str) -> int | None:
    match = re.search(r"\btop\s+(\d+)\b", text.lower())
    return int(match.group(1)) if match else None


def _mentions_mobile(text: str) -> bool:
    if _rejects_connection(text):
        return False
    folded = text.replace("_", "").replace("-", " ")
    return "mobile user" in folded or "mobile subscriber" in folded or ("exclude" in folded and "fixed" in folded)


def _rejects_connection(text: str) -> bool:
    folded = text.lower().replace("-", " ")
    if any(phrase in folded for phrase in ("regardless", "irrespective", "nature of connection", "any connection", "all connections")):
        return True
    return "mobile" in folded and bool(re.search(r"\b(not only|not just|gave|given|showing|showed)\b", folded))


def _mentions_fixed(text: str) -> bool:
    folded = text.replace("_", "").replace("-", " ")
    return "fixed line user" in folded or "fixedline user" in folded


def _names_time(question: str) -> bool:
    return any(word in question.lower() for word in _TIME_WORDS)


def _app_choice(blob: str, previous: str) -> tuple[bool, bool]:
    drop = any(
        phrase in blob
        for phrase in ("every application", "all applications", "all apps", "every app", "include every")
    )
    keep = any(
        phrase in blob
        for phrase in ("stay limited", "keep whatsapp", "only whatsapp", "limited to whatsapp", "stay on whatsapp", "for the same")
    )
    if not keep and not drop and "whatsapp" in blob and "whatsapp" in previous.lower():
        # The user named WhatsApp again while answering, which keeps that filter.
        if "stay" in blob or "limited" in blob or "correct" in blob:
            keep = True
    return keep, drop


def _has_app_filter(sql: str) -> bool:
    return "app_label" in sql.lower()


def _present_measures(question: str, sql: str) -> str | None:
    """Add or convert measures on the previous result without dropping the ones already shown."""
    if not sql or not re.search(r"\bgroup\s+by\b", sql, flags=re.IGNORECASE):
        return None
    if _group_key(sql) == "subscriber_id" and _unit_of(question) is None:
        return None
    folded = question.lower()
    unit = _unit_of(question)
    wants_users = _wants_user_count(folded)
    wants_split = "upload" in folded and "download" in folded
    wants_flows = bool(re.search(r"\bflows?\b", folded)) or "flow count" in folded or "removed the flow" in folded
    asks_sort = bool(re.search(r"\b(order|ordered|sort|sorted)\b", folded))
    wants_volume = "volume" in folded or "traffic" in folded
    if not (wants_split or wants_flows or unit or asks_sort or wants_users):
        return None
    if wants_flows and not wants_split and not unit and _group_key(sql) == "subscriber_id":
        return None
    keep_flows = wants_flows or _has_call(sql, r"count\s*\(\s*\*\s*\)")
    keep_upload = wants_split or _has_call(sql, r"sum\s*\(\s*c2s_byte_num\s*\)")
    keep_download = wants_split or _has_call(sql, r"sum\s*\(\s*s2c_byte_num\s*\)")
    keep_total = _has_call(sql, r"sum\s*\(\s*c2s_byte_num\s*\+\s*s2c_byte_num\s*\)")
    if wants_split and not re.search(r"\b(also|as well|total)\b", folded):
        keep_total = False
    names_upload = "upload" in folded
    names_download = "download" in folded
    if unit and names_upload and not names_download and not wants_volume:
        upload_unit = unit
        download_unit = _existing_unit(sql, "s2c")
        total_unit = _existing_unit(sql, "total")
    elif unit and names_download and not names_upload and not wants_volume:
        upload_unit = _existing_unit(sql, "c2s")
        download_unit = unit
        total_unit = _existing_unit(sql, "total")
    elif unit:
        upload_unit = unit if keep_upload or wants_split else _existing_unit(sql, "c2s")
        download_unit = unit if keep_download or wants_split else _existing_unit(sql, "s2c")
        total_unit = unit if keep_total else _existing_unit(sql, "total")
    else:
        upload_unit = _existing_unit(sql, "c2s")
        download_unit = _existing_unit(sql, "s2c")
        total_unit = _existing_unit(sql, "total")
    key = _group_key(sql) or "app_label"
    columns = [key]
    if keep_flows:
        columns.append("COUNT(*) AS flow_count")
    if keep_upload:
        columns.append(_byte_column("SUM(c2s_byte_num)", "upload_traffic", upload_unit))
    if keep_download:
        columns.append(_byte_column("SUM(s2c_byte_num)", "download_traffic", download_unit))
    if keep_total:
        columns.append(_byte_column("SUM(c2s_byte_num + s2c_byte_num)", "traffic_volume", total_unit))
    if wants_users or _has_call(sql, r"count\s*\(\s*distinct\b"):
        columns.append(
            "COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
            "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users"
        )
    tail = re.sub(r"\s+", " ", sql)
    match = re.search(r"\bFROM\b", tail, flags=re.IGNORECASE)
    if not match:
        return None
    rest = tail[match.start():]
    rest = re.sub(r"(?is)\bORDER BY\b.+?(?=\bLIMIT\b|$)", "", rest).strip()
    order = _requested_order(folded, columns)
    if order is None:
        order = "flow_count" if wants_flows and "by flow" in folded else _kept_order(sql, columns)
    statement = f"SELECT {', '.join(columns)} {rest}"
    if order:
        if re.search(r"(?i)\bLIMIT\b", statement):
            statement = re.sub(r"(?i)\bLIMIT\b", f"ORDER BY {order} DESC LIMIT", statement, count=1)
        else:
            statement = f"{statement} ORDER BY {order} DESC"
    return re.sub(r"\s+", " ", statement).strip()


def _wants_user_count(question: str) -> bool:
    """A user count beside each application, not a ranking of subscribers."""
    return bool(
        re.search(r"\b(unique users|number of users|user count|users against|users per|users for each)\b", question)
        or ("user" in question and re.search(r"\b(each app|per app|against each|by app)\b", question))
    )


def _group_key(sql: str) -> str | None:
    match = re.search(r"\bgroup\s+by\s+([a-z0-9_]+)", sql, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def asked_unit(question: str) -> str | None:
    """mb, gb, or tb when the question names a display unit."""
    found = _unit_of(question)
    return found[1] if found else None


def _unit_of(question: str) -> tuple[int, str] | None:
    """The unit to display. In 'from GB to MB', MB is the one that was asked for."""
    folded = question.lower()
    destination = None
    for match in re.finditer(r"\b(?:to|into|in|as)\s+(?:the\s+)?([a-z0-9]+)", folded):
        token = _unit_token(match.group(1))
        if token:
            destination = token
    if destination:
        return destination
    found: tuple[int, tuple[int, str]] | None = None
    for pattern, unit in _UNIT_PATTERNS:
        for match in pattern.finditer(folded):
            if found is None or match.start() >= found[0]:
                found = (match.start(), unit)
    return found[1] if found else None


_UNIT_PATTERNS = (
    (re.compile(r"terabytes?|\btb\b"), (1099511627776, "tb")),
    (re.compile(r"gigabytes?|\bgb\b"), (1073741824, "gb")),
    (re.compile(r"megabytes?|\bmb\b"), (1048576, "mb")),
)


def _unit_token(word: str) -> tuple[int, str] | None:
    for pattern, unit in _UNIT_PATTERNS:
        if pattern.fullmatch(word):
            return unit
    return None


def _existing_unit(sql: str, kind: str) -> tuple[int, str] | None:
    folded = sql.lower().replace(" ", "")
    if kind == "c2s":
        blob = folded.split("s2c_byte_num", 1)[0]
    elif kind == "s2c":
        blob = folded.split("s2c_byte_num", 1)[-1]
    else:
        blob = folded
    if "/1099511627776" in blob:
        return 1099511627776, "tb"
    if "/1073741824" in blob:
        return 1073741824, "gb"
    if "/1048576" in blob:
        return 1048576, "mb"
    return None


def _byte_column(expression: str, name: str, unit: tuple[int, str] | None) -> str:
    if unit is None:
        return f"{expression} AS {name}"
    divisor, suffix = unit
    return f"ROUND({expression} / {divisor}, 2) AS {name}_{suffix}"


def _has_call(sql: str, pattern: str) -> bool:
    return bool(re.search(pattern, sql, flags=re.IGNORECASE))


def _requested_order(question: str, columns: list[str]) -> str | None:
    """The sort the latest question asked for, using the last order or sort phrase."""
    pieces = re.split(r"\b(?:order|ordered|sort|sorted)\b", question)
    if len(pieces) < 2:
        return None
    target = pieces[-1]
    aliases = []
    for item in columns:
        match = re.search(r"\bas\s+([a-z0-9_]+)\s*$", item, flags=re.IGNORECASE)
        aliases.append((match.group(1) if match else item).lower())

    def pick(*names: str) -> str | None:
        for name in names:
            for alias in aliases:
                if alias == name or alias.startswith(name + "_"):
                    return alias
        return None

    if re.search(r"\b(user count|users|number of users|unique users)\b", target):
        return pick("users")
    if re.search(r"\b(flows?|flow count)\b", target):
        return pick("flow_count", "flows")
    if "upload" in target and "download" not in target:
        return pick("upload_traffic", "upload_bytes")
    if "download" in target and "upload" not in target:
        return pick("download_traffic", "download_bytes")
    return None


def _kept_order(sql: str, columns: list[str]) -> str | None:
    match = re.search(r"\border\s+by\s+([a-z0-9_]+)", sql, flags=re.IGNORECASE)
    if not match:
        return None
    aliases = {item.rsplit(" AS ", 1)[-1].lower() for item in columns}
    current = match.group(1).lower()
    if current in aliases:
        return current
    if current in {"flows", "flow_count"} and "flow_count" in aliases:
        return "flow_count"
    return None


def _subscriber_request(question: str) -> bool:
    return _mentions_mobile(question) or _mentions_fixed(question)


def _latest_limit(texts: list[str]) -> int | None:
    found = None
    for text in texts:
        for match in re.finditer(r"\btop\s+(\d+)\b", text.lower()):
            found = int(match.group(1))
    return found


def _limit_of(sql: str) -> int | None:
    match = re.search(r"\blimit\s+(\d+)\b", sql.lower())
    return int(match.group(1)) if match else None


def _rewrite(sql: str, keep_app: bool, kind: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", sql).strip()
    if not keep_app:
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+LIKE\s+'%[^']+%'", "", text, flags=re.IGNORECASE)
        text = re.sub(r"LOWER\(app_label\)\s+LIKE\s+'%[^']+%'\s+AND\s+", "", text, flags=re.IGNORECASE)
    text = _without_cell(text)
    cell = f"AND NOT {MOBILE_SQL}" if kind == "fixed" else f"AND {MOBILE_SQL}"
    text = re.sub(r"\bGROUP BY\b", f"{cell} GROUP BY", text, count=1, flags=re.IGNORECASE)
    if re.search(r"\blimit\s+\d+\b", text, flags=re.IGNORECASE):
        text = re.sub(r"\blimit\s+\d+\b", f"LIMIT {int(limit)}", text, count=1, flags=re.IGNORECASE)
    else:
        text = f"{text} LIMIT {int(limit)}"
    return text


def _without_cell(sql: str) -> str:
    pattern = re.escape(re.sub(r"\s+", " ", MOBILE_SQL))
    text = re.sub(rf"\s+AND\s+NOT\s+{pattern}", "", sql, flags=re.IGNORECASE)
    text = re.sub(rf"\s+AND\s+{pattern}", "", text, flags=re.IGNORECASE)
    loose = (
        r"\(\s*NULLIF\(\s*mcc\b.*?NULLIF\(\s*usr_loc_info\b.*?\)\s*\)"
    )
    text = re.sub(rf"\s+AND\s+NOT\s+{loose}", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"\s+AND\s+{loose}", "", text, flags=re.IGNORECASE)
    return text


def _pending(history: list[dict]) -> bool:
    if not history:
        return False
    last = history[-1]
    return not str(last.get("sql") or "").strip() and bool(str(last.get("explanation") or "").strip())


def _answer_keeps(question: str) -> bool:
    folded = question.strip().lower()
    if folded in {"yes", "yes.", "ok", "okay", "keep", "keep it"}:
        return True
    return "keep the previous" in folded or "stay limited" in folded or "limited to" in folded


def _answer_drops(question: str) -> bool:
    folded = question.strip().lower()
    return folded in {"no", "no."} or "include every application" in folded or "every application" in folded


def _wants_split(question: str) -> bool:
    folded = question.lower()
    return "upload" in folded and "download" in folded


def _rank_target(question: str) -> str | None:
    folded = question.lower()
    wants_flows = bool(re.search(r"\b(flow count|number of flows|by flows|by flow)\b", folded))
    wants_volume = bool(re.search(r"\b(traffic volume|by volume|by traffic|total bytes)\b", folded))
    if wants_flows and wants_volume:
        return "flows" if folded.rfind("flow") > folded.rfind("volume") else "total_bytes"
    if wants_flows:
        return "flows"
    if wants_volume and "instead" in folded:
        return "total_bytes"
    return None


def _revise(question: str, history: list[dict], force_keep: bool = False, force_drop: bool = False) -> str | None:
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    if _subscriber_request(question) and not _wants_split(question) and _rank_target(question) is None:
        return None
    split = _wants_split(question)
    rank = _rank_target(question)
    reject = _rejects_connection(question) or subscriber_kind(
        [str(item.get("question") or "") for item in history] + [question]
    ) == "any" and _rejects_connection(question)
    if not (split or rank or _rejects_connection(question) or (force_keep and (split or rank)) or (force_drop and (split or rank))):
        return None
    texts = [str(item.get("question") or "") for item in history]
    kind = _subscriber_kind(texts + [question])
    keep, drop = _app_choice(" ".join(texts + [question]).lower(), previous)
    if force_keep or "for the same" in question.lower() or split or rank or _rejects_connection(question):
        if not force_drop and not drop:
            keep = True
    if force_drop:
        keep, drop = False, True
    text = re.sub(r"\s+", " ", previous).strip()
    if drop or not keep:
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+LIKE\s+'%[^']+%'", "", text, flags=re.IGNORECASE)
        text = re.sub(r"LOWER\(app_label\)\s+LIKE\s+'%[^']+%'\s+AND\s+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+AND\s+LOWER\(app_label\)\s+IN\s+\([^)]*\)", "", text, flags=re.IGNORECASE)
    if kind == "fixed":
        text = _without_cell(text)
        text = re.sub(r"\bGROUP BY\b", f"AND NOT {MOBILE_SQL} GROUP BY", text, count=1, flags=re.IGNORECASE)
    elif kind == "mobile":
        text = _without_cell(text)
        text = re.sub(r"\bGROUP BY\b", f"AND {MOBILE_SQL} GROUP BY", text, count=1, flags=re.IGNORECASE)
    else:
        text = _without_cell(text)
    if split:
        text = re.sub(
            r"(?is)^SELECT\b.+?\bFROM\b",
            "SELECT subscriber_id, SUM(c2s_byte_num) AS upload_bytes, "
            "SUM(s2c_byte_num) AS download_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            "COUNT(*) AS flows FROM",
            text,
            count=1,
        )
    if rank == "flows" and not re.search(r"\bas\s+flows\b", text, flags=re.IGNORECASE):
        text = re.sub(r"(?i)\bFROM\b", ", COUNT(*) AS flows FROM", text, count=1)
    if rank:
        column = "flows" if rank == "flows" else ("total_bytes" if re.search(r"\bas\s+total_bytes\b", text, flags=re.IGNORECASE) else "total_mb")
        if re.search(r"(?i)\bORDER BY\b", text):
            text = re.sub(r"(?is)\bORDER BY\b.+?(?=\bLIMIT\b|$)", f"ORDER BY {column} DESC ", text, count=1)
        elif re.search(r"(?i)\bLIMIT\b", text):
            text = re.sub(r"(?i)\bLIMIT\b", f"ORDER BY {column} DESC LIMIT", text, count=1)
        else:
            text = f"{text} ORDER BY {column} DESC"
    limit = _limit_in(question) or _limit_of(text)
    if limit and re.search(r"(?i)\blimit\s+\d+\b", text):
        text = re.sub(r"(?i)\blimit\s+\d+\b", f"LIMIT {int(limit)}", text, count=1)
    return re.sub(r"\s+", " ", text).strip()
