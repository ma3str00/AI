"""Apply a follow-up once the user has already settled the open choice."""

from __future__ import annotations

import re

from app.app_map import CATEGORY_PHRASES
from app.glossary import mentions
from app.places import (
    asks_same_city,
    area_predicate,
    column_predicate,
    mentioned_areas,
    mentioned_isps,
    named_in,
    street_before_city,
    street_predicate,
    without_areas,
    without_named,
)
from app.schema import ALLOWED_SQL, BLOCKED_SQL, MOBILE_SQL
from app.sql_guard import wants_all_groups, wants_full_table

_TIME_WORDS = ("yesterday", "today", "last ", "hour", "between", "since", "this week")


def resolve_followup(question: str, history: list[dict] | None) -> str | None:
    """Build SQL from the last real query when the reply only changes one part of it."""
    history = history or []
    swapped = _same_request_other_person(question, history)
    if swapped:
        return swapped
    other_app = _same_request_other_app(question, history)
    if other_app:
        return other_app
    other_isp = _same_request_other_isp(question, history)
    if other_isp:
        return other_isp
    place = _place_of_one_subscriber(question, history)
    if place:
        return place
    places = _addresses_of_current_users(question, history)
    if places:
        return places
    from_place = _users_from_place(question, history)
    if from_place:
        return from_place
    shown = _add_shown_columns(question, history)
    if shown:
        return shown
    blocked_activity = _blocked_activity_of_one(question, history)
    if blocked_activity:
        return blocked_activity
    sites = _websites_of_one_subscriber(question, history)
    if sites:
        return sites
    apps = _apps_of_one_subscriber(question, history)
    if apps:
        return apps
    unlimited = _without_row_limit(question, history)
    if unlimited:
        return unlimited
    # A new list is chosen before a column is added to the list already on screen.
    by_sites = _people_by_website_count(question)
    if by_sites:
        return by_sites
    ranked_sites = _top_websites(question)
    if ranked_sites:
        return ranked_sites
    people = _top_people(question, history)
    if people:
        return people
    counted = _add_user_count(question, history)
    if counted:
        return counted
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
    if _group_key(previous) not in {"app_label", "app_category", "requested_server", "subscriber_id"}:
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


def _id_anywhere(question: str) -> str | None:
    """A subscriber id written in the sentence, including 'apps that 0333...ASD'."""
    named = named_subscriber(question)
    if named:
        return named
    hosts = _named_servers(question)
    cleaned = question.replace("'", "").replace('"', "").replace("`", "")
    for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9-]{5,64}\b", cleaned):
        if any(token.lower() in host for host in hosts) or not _subscriber_token(token):
            continue
        digits = len(re.findall(r"\d", token))
        letters = len(re.findall(r"[A-Za-z]", token))
        if (letters and digits >= 4) or (letters == 0 and digits >= 10):
            return token
    return None


def _blocked_activity_of_one(question: str, history: list[dict]) -> str | None:
    """Blocked websites or apps one subscriber tried to reach."""
    blocked = mentions(question, "blocked")
    allowed = not blocked and mentions(question, "allowed")
    if not blocked and not allowed:
        return None
    subscriber = _id_anywhere(question)
    if not subscriber and _points_at_known_user(question):
        subscriber = _subscriber_from_history(history)
    if not subscriber:
        return None
    if _asks_for_websites(question):
        column = "requested_server"
        extra = " AND IFNULL(requested_server, '') <> ''"
    elif _asks_for_their_apps(question):
        column = "app_label"
        extra = " AND IFNULL(app_label, '') <> ''"
    else:
        return None
    status = BLOCKED_SQL if blocked else ALLOWED_SQL
    measure = "blocked_flows" if blocked else "allowed_flows"
    limit = _limit_in(question)
    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
    return (
        f"SELECT {column}, COUNT(*) AS {measure} FROM ntc_ssl_logs "
        f"WHERE subscriber_id = '{subscriber}' AND {status}{extra} "
        f"GROUP BY {column} ORDER BY {measure} DESC{limit_sql}"
    )


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
    app_label = _named_app_label(question)
    if app_label:
        where.insert(1, f"LOWER(app_label) = '{app_label}'")
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


def _top_websites(question: str) -> str | None:
    """A ranking of websites. Hits are flows, not bytes."""
    folded = question.lower()
    if not _asks_for_websites(folded):
        return None
    if named_subscriber(question) or _points_at_known_user(question):
        return None
    if re.search(r"\b(users?|subscribers?)\b", folded):
        return None
    if not re.search(r"\b(top|highest|maximum|most)\b", folded):
        return None
    limit = _limit_in(question) or 10
    blocked = mentions(question, "blocked")
    allowed = mentions(question, "allowed")
    by_volume = bool(re.search(r"\b(bytes|traffic|volume|download|upload)\b", folded))
    order = "flows" if _means_hits(folded) or ((blocked or allowed) and not by_volume) else "total_bytes"
    where = ["IFNULL(requested_server, '') <> ''"]
    if blocked:
        where.append(BLOCKED_SQL)
    elif allowed:
        where.append(ALLOWED_SQL)
    return (
        "SELECT requested_server, COUNT(*) AS flows, "
        "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
        f"FROM ntc_ssl_logs WHERE {' AND '.join(where)} "
        f"GROUP BY requested_server ORDER BY {order} DESC LIMIT {int(limit)}"
    )


def _people_by_website_count(question: str) -> str | None:
    """Subscribers ranked by how many different websites they requested."""
    folded = question.lower()
    if not re.search(r"\b(users?|subscribers?|people)\b", folded):
        return None
    if not mentions(question, "website"):
        return None
    if not re.search(r"\b(count|number|most|highest|maximum|unique|distinct)\b", folded):
        return None
    if _wants_user_count(folded) or named_subscriber(question):
        return None
    limit = _limit_in(question) or 10
    return (
        "SELECT subscriber_id, COUNT(DISTINCT requested_server) AS websites, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND IFNULL(requested_server, '') <> '' "
        f"GROUP BY subscriber_id ORDER BY websites DESC LIMIT {int(limit)}"
    )


_HOSTNAME = re.compile(
    r"(?<![a-z0-9@])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?![a-z0-9])",
    re.IGNORECASE,
)


def _named_servers(question: str) -> list[str]:
    """A website named in the question, such as static-nss.xhcdn.com."""
    found: list[str] = []
    for match in _HOSTNAME.finditer(question.lower()):
        host = match.group(0).strip(".")
        if host not in found:
            found.append(host)
    return found


def _means_hits(text: str) -> bool:
    return mentions(text, "hit")


def _named_app_label(text: str) -> str | None:
    """The application named as a filter, as in 'against app type HTTP'."""
    folded = text.lower()
    match = re.search(r"\bapp(?:lication)?\s+type\s+([a-z0-9][a-z0-9_.-]{1,40})\b", folded)
    if not match:
        match = re.search(
            r"\b(?:for|against|of|in)\s+(?:the\s+)?app(?:lication)?\s+([a-z0-9][a-z0-9_.-]{1,40})\b",
            folded,
        )
    if not match:
        return None
    label = match.group(1)
    if label in _NOT_AN_APP or label in {"type", "label", "category", "name"}:
        return None
    return label


def _same_request_other_app(question: str, history: list[dict]) -> str | None:
    """'The same for app type SSL' keeps the previous result and changes the application."""
    if not re.search(r"\bsame\b", question.lower()):
        return None
    label = _named_app_label(question)
    if not label:
        return None
    previous = _last_sql(history)
    if not previous:
        return None
    updated = re.sub(
        r"(?i)LOWER\(app_label\)\s*=\s*'[^']+'",
        f"LOWER(app_label) = '{label}'",
        previous,
        count=1,
    )
    if updated == previous:
        updated = re.sub(
            r"(?i)LOWER\(app_label\)\s+LIKE\s+'%[^']+%'",
            f"LOWER(app_label) LIKE '%{label}%'",
            previous,
            count=1,
        )
    if updated == previous:
        return None
    return re.sub(r"\s+", " ", updated).strip()


def _asks_same_result(question: str) -> bool:
    """'The same but on Jazz' keeps the result. 'The same city' does not."""
    folded = question.lower()
    if re.search(r"\bsame\s+(?:thing|result|query|list|ranking)\b", folded):
        return True
    without_city = re.sub(r"\bsame\s+city\b", " ", folded)
    return bool(re.search(r"\bsame\b", without_city))


def _same_request_other_isp(question: str, history: list[dict]) -> str | None:
    """Keep the ranking and replace the client ASN filter with the named ISP.

    'The same but on Jazz' and the shorter 'now show me for Ufone' are the same change.
    A fresh ranking such as 'top 20 Ufone users' is not.
    """
    if _named_app_label(question) or mentions(question, "website") or mentions(question, "their_apps"):
        return None
    if any(place.kind == "city" for place in named_in(question)):
        return None
    isps = mentioned_isps(question)
    if len(isps) != 1:
        return None
    folded = question.lower()
    for alias in sorted(isps[0].aliases, key=len, reverse=True):
        folded = re.sub(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", " ", folded)
    if _subject_filter(folded):
        return None
    if not _asks_same_result(question) and re.search(r"\b(top|highest|maximum|most)\b", folded):
        return None
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    text = _strip_column_group(re.sub(r"\s+", " ", previous).strip(), "client_asn")
    text = re.sub(
        r"(?i)\bGROUP BY\b",
        f"AND {column_predicate('client_asn', isps[-1])} GROUP BY",
        text,
        count=1,
    )
    if not re.search(r"(?i)\bas client_asn\b", text):
        text = re.sub(r"(?i)\bFROM\b", ", " + _address_expression("client_asn") + " FROM", text, count=1)
    limit = _limit_in(question)
    if limit is not None:
        text = re.sub(r"(?i)\s+LIMIT\s+\d+\s*$", "", text).strip()
        text = f"{text} LIMIT {int(limit)}"
    return re.sub(r"\s+", " ", text).strip()


def _asks_for_place(text: str) -> bool:
    """The subscriber's premises. An IP address is a different column."""
    folded = text.lower()
    if re.search(r"\bip address\b", folded) or mentions(text, "ip_place"):
        return False
    if mentions(text, "real_place"):
        return True
    return bool(re.search(r"\b(address|addresses|location|locations|whereabouts)\b", folded))


def _place_of_one_subscriber(question: str, history: list[dict]) -> str | None:
    """loc and geo are the cell or fixed-line address, not the IP-database place."""
    if not _asks_for_place(question):
        return None
    subscriber = named_subscriber(question)
    if not subscriber and (_points_at_known_user(question) or _recent_unanswered(history, _asks_for_place)):
        subscriber = _subscriber_from_history(history)
    if not subscriber:
        return None
    return (
        "SELECT loc, geo, COUNT(*) AS flows FROM ntc_ssl_logs "
        f"WHERE subscriber_id = '{subscriber}' AND (IFNULL(loc, '') <> '' OR IFNULL(geo, '') <> '') "
        "GROUP BY loc, geo ORDER BY flows DESC LIMIT 20"
    )


def _address_expression(column: str) -> str:
    return (
        f"GROUP_CONCAT(DISTINCT CASE WHEN IFNULL({column}, '') <> '' THEN {column} END "
        f"ORDER BY {column} SEPARATOR ', ') AS {column}"
    )


def _from_place(question: str) -> str | None:
    """An unknown place word after from or in. Known cities and ISPs are handled separately."""
    if named_in(question):
        return None
    match = re.search(r"\b(?:from|near|within)\s+([A-Za-z][A-Za-z-]{2,40})\b", question)
    if not match:
        return None
    place = match.group(1)
    if place.lower() in _NOT_AN_APP or place.lower() in {"the", "all", "this", "that", "these", "those", "and"}:
        return None
    return place


def _apply_place_filters(text: str, question: str) -> str:
    """Keep a subscriber ranking and set the city and ISP named in the question."""
    cities = [place for place in named_in(question) if place.kind == "city"]
    isps = [place for place in named_in(question) if place.kind == "isp"]
    areas = mentioned_areas(question)
    unknown = _from_place(question)
    if not cities and not isps and not unknown and not areas:
        return text
    if cities or unknown or areas:
        text = _strip_column_group(text, "loc")
        if cities or unknown:
            predicate = column_predicate("loc", cities[-1]) if cities else f"LOWER(loc) LIKE '%{unknown}%'"
            text = re.sub(r"(?i)\bGROUP BY\b", f"AND {predicate} GROUP BY", text, count=1)
        for area in areas:
            text = re.sub(r"(?i)\bGROUP BY\b", f"AND {area_predicate(area)} GROUP BY", text, count=1)
        if not re.search(r"(?i)\bas loc\b", text):
            text = re.sub(
                r"(?i)\bFROM\b",
                ", " + _address_expression("loc") + ", " + _address_expression("geo") + " FROM",
                text,
                count=1,
            )
    if isps:
        text = _strip_column_group(text, "client_asn")
        text = re.sub(
            r"(?i)\bGROUP BY\b",
            f"AND {column_predicate('client_asn', isps[-1])} GROUP BY",
            text,
            count=1,
        )
        if not re.search(r"(?i)\bas client_asn\b", text):
            text = re.sub(r"(?i)\bFROM\b", ", " + _address_expression("client_asn") + " FROM", text, count=1)
    return text


def _strip_column_group(sql: str, column: str) -> str:
    pattern = re.compile(rf"(?i)\s+AND\s+\(LOWER\({re.escape(column)}\)\s+LIKE\b")
    while True:
        match = pattern.search(sql)
        if not match:
            break
        open_at = sql.find("(", match.start())
        depth = 0
        end = None
        for index in range(open_at, len(sql)):
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            break
        sql = sql[: match.start()] + sql[end:]
    sql = re.sub(rf"(?i)\s+AND\s+{re.escape(column)}\s+LIKE\s+'%[^']+'", "", sql)
    sql = re.sub(rf"(?i)\s+AND\s+LOWER\({re.escape(column)}\)\s+LIKE\s+'%[^']+'", "", sql)
    sql = re.sub(rf"(?i)\s+AND\s+{re.escape(column)}\s*=\s*'[^']+'", "", sql)
    return sql


def _users_from_place(question: str, history: list[dict]) -> str | None:
    """Keep the subscriber ranking and apply a named city or ISP."""
    if _subject_filter(without_areas(without_named(question)).lower()):
        return None
    if not named_in(question) and _from_place(question) is None and not mentioned_areas(question):
        return None
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    text = _apply_place_filters(re.sub(r"\s+", " ", previous).strip(), question)
    limit = _limit_in(question)
    if limit is not None:
        text = re.sub(r"(?i)\s+LIMIT\s+\d+\s*$", "", text).strip()
        text = f"{text} LIMIT {int(limit)}"
    return re.sub(r"\s+", " ", text).strip()


def _addresses_of_current_users(question: str, history: list[dict]) -> str | None:
    """Add loc and geo to the subscriber list on screen. Not the IP-database place."""
    if not _asks_for_place(question) or named_subscriber(question):
        return None
    if not re.search(r"\b(these|those|their)\s+(users?|subscribers?|people|address|addresses)\b", question.lower()):
        return None
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    text = re.sub(r"\s+", " ", previous).strip()
    additions = [
        _address_expression(column)
        for column in ("loc", "geo")
        if not re.search(rf"(?i)\bas {column}\b", text)
    ]
    if not additions:
        return text
    return re.sub(r"(?i)\bFROM\b", ", " + ", ".join(additions) + " FROM", text, count=1)


def _asks_for_websites(text: str) -> bool:
    return mentions(text, "website")


def _same_request_other_person(question: str, history: list[dict]) -> str | None:
    """'What about <id>?' keeps the previous result and changes the subscriber."""
    if not mentions(question, "other_person"):
        return None
    subscriber = named_subscriber(question)
    if not subscriber:
        return None
    previous = _last_sql(history)
    if not previous or not re.search(r"subscriber_id\s*=\s*'[^']+'", previous, flags=re.IGNORECASE):
        return None
    updated = re.sub(
        r"subscriber_id\s*=\s*'[^']+'",
        f"subscriber_id = '{subscriber}'",
        previous,
        count=1,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", updated).strip()


def _saved_loc_predicate(sql: str) -> str | None:
    match = re.search(
        r"(?i)\(\s*LOWER\(loc\)\s+LIKE\s+'%[^']+%'(?:\s+OR\s+LOWER\(loc\)\s+LIKE\s+'%[^']+%')*\s*\)",
        sql or "",
    )
    return match.group(0) if match else None


def _top_people(question: str, history: list[dict] | None = None) -> str | None:
    """A ranking of identified subscribers. Unidentified IP_port ids are not users."""
    folded = question.lower()
    if not re.search(r"\b(top|highest|maximum|most)\b", folded):
        return None
    if not re.search(r"\b(users?|subscribers?)\b", folded):
        return None
    if _wants_user_count(folded) or mentions(question, "website") or mentions(question, "their_apps"):
        return None
    if named_subscriber(question) or _rejects_connection(question):
        return None
    if subscriber_kind([question]) in {"mobile", "fixed"}:
        return None
    limit = _limit_in(question) or 20
    blocked = mentions(question, "blocked")
    allowed = not blocked and mentions(question, "allowed")
    by_volume = bool(re.search(r"\b(bytes|traffic|volume|download|upload)\b", folded))
    if blocked or allowed:
        flow_name = "blocked_flows" if blocked else "allowed_flows"
        order = "total_bytes" if by_volume else flow_name
    elif _means_hits(folded) or re.search(r"\b(flows?|flow count)\b", folded):
        flow_name = "flows"
        order = "flows"
    else:
        flow_name = "flows"
        order = "total_bytes"
    where = ["IFNULL(subscriber_id, '') <> ''", "LOCATE('_', subscriber_id) = 0"]
    cities = [item for item in named_in(question) if item.kind == "city"]
    isps = mentioned_isps(question)
    street = street_before_city(question)
    areas = [] if street else mentioned_areas(question)
    servers = _named_servers(question)
    place = _from_place(question)
    subject_text = without_areas(without_named(folded))
    for host in servers:
        subject_text = re.sub(re.escape(host), " ", subject_text, flags=re.IGNORECASE)
    for word in street:
        subject_text = re.sub(rf"(?i)(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", " ", subject_text)
    for isp in isps:
        for alias in sorted(isp.aliases, key=len, reverse=True):
            subject_text = re.sub(rf"(?i)(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", " ", subject_text)
    if place:
        subject_text = re.sub(rf"(?i)\b(?:from|near|within)\s+{re.escape(place)}\b", " ", subject_text)
    subject = _subject_filter(subject_text)
    previous = _last_sql(history or [])
    saved_city = _saved_loc_predicate(previous) if asks_same_city(question) else None
    if (
        previous
        and "group by subscriber_id" in previous.lower()
        and not subject
        and not street
        and not servers
        and not blocked
        and not allowed
    ):
        return None
    if subject:
        where.insert(0, subject)
    if blocked:
        where.append(BLOCKED_SQL)
    elif allowed:
        where.append(ALLOWED_SQL)
    if street:
        where.append(street_predicate(street))
    elif cities:
        where.append(column_predicate("loc", cities[-1]))
    elif saved_city:
        where.append(saved_city)
    elif place:
        where.append(f"LOWER(loc) LIKE '%{place}%'")
    for area in areas:
        where.append(area_predicate(area))
    if isps:
        where.append(column_predicate("client_asn", isps[-1]))
    for host in servers:
        where.append(f"LOWER(requested_server) = '{host.replace(chr(39), '')}'")
    columns = ["subscriber_id"]
    for column in _display_columns(question):
        columns.append(_address_expression(column))
    if (_asks_for_place(question) or place or cities or saved_city or areas or street) and not any(column.lower().endswith(" as loc") for column in columns):
        columns.extend(_address_expression(column) for column in ("loc", "geo"))
    if isps and not any(column.lower().endswith(" as client_asn") for column in columns):
        columns.append(_address_expression("client_asn"))
    columns.extend(
        [
            "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes",
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb",
            f"COUNT(*) AS {flow_name}",
        ]
    )
    return (
        f"SELECT {', '.join(columns)} "
        f"FROM ntc_ssl_logs WHERE {' AND '.join(where)} "
        f"GROUP BY subscriber_id ORDER BY {order} DESC LIMIT {int(limit)}"
    )


_NOT_AN_APP = {
    "user", "users", "show", "with", "maximum", "activity", "traffic", "highest", "most",
    "each", "against", "unique", "count", "number", "these", "apps", "from", "their",
    "this", "that", "application", "applications", "subscriber", "subscribers", "volume",
    "bytes", "flows", "flow", "download", "upload", "mobile", "fixed", "line", "data",
    "what", "about", "give", "want", "please", "only", "just", "same", "done", "used",
    "using", "other", "activities", "details", "detail", "have", "been", "them", "those",
    "along", "with", "also", "list", "listed", "respective", "corresponding", "related",
    "address", "addresses", "location", "locations", "whereabouts", "asn",
    "instead", "showing", "rather", "sorted", "ordering", "city", "cities",
    "area", "locality", "neighborhood", "neighbourhood", "vicinity",
    "tried", "try", "access", "accessed", "accessing", "visit", "visited", "visiting",
    "blocked", "denied", "rejected", "allowed", "permitted",
}


_DISPLAY_COLUMNS = (
    ("internet service provider", "client_asn"),
    ("client asn", "client_asn"),
    ("server asn", "server_asn"),
    ("client ip", "client_ip"),
    ("server ip", "server_ip"),
    ("imei", "imei"),
    ("imsi", "imsi"),
    ("isp", "client_asn"),
    ("isps", "client_asn"),
    ("provider", "client_asn"),
    ("providers", "client_asn"),
    ("operator", "client_asn"),
    ("operators", "client_asn"),
)


def _display_columns(text: str) -> list[str]:
    folded = text.lower()
    columns = []
    for phrase, column in _DISPLAY_COLUMNS:
        if column not in columns and re.search(rf"\b{re.escape(phrase)}\b", folded):
            columns.append(column)
    if "client_asn" not in columns and "server_asn" not in columns and re.search(r"\basns?\b", folded):
        columns.append("client_asn")
    if (
        "client_asn" not in columns
        and "server_asn" not in columns
        and "network storage" not in folded
        and re.search(r"\bnetworks?\b", folded)
    ):
        columns.append("client_asn")
    return columns


def _add_shown_columns(question: str, history: list[dict]) -> str | None:
    """'Also show the ISP' adds that column to the subscriber ranking already on screen."""
    wanted = list(_display_columns(question))
    if _asks_for_place(question):
        wanted.extend(column for column in ("loc", "geo") if column not in wanted)
    if not wanted:
        return None
    previous = _last_sql(history)
    if not previous or "group by subscriber_id" not in previous.lower():
        return None
    text = re.sub(r"\s+", " ", previous).strip()
    additions = [
        _address_expression(column)
        for column in wanted
        if not re.search(rf"(?i)\bas {column}\b", text)
    ]
    if not additions:
        return None
    return re.sub(r"(?i)\bFROM\b", ", " + ", ".join(additions) + " FROM", text, count=1)


def _subject_filter(folded: str) -> str | None:
    for phrase, _column in _DISPLAY_COLUMNS:
        folded = re.sub(rf"\b{re.escape(phrase)}\b", " ", folded)
    for phrase, category in CATEGORY_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", folded):
            return f"LOWER(app_category) = '{category}'"
    tokens = [token for token in re.findall(r"[a-z0-9]{4,}", folded) if token not in _NOT_AN_APP]
    if not tokens:
        return None
    stem = max(tokens, key=len)
    return f"LOWER(app_label) LIKE '%{stem}%'"


def _add_user_count(question: str, history: list[dict]) -> str | None:
    """Add identified subscribers beside the current rows and keep that ranking."""
    folded = question.lower()
    if not _wants_user_count(folded):
        return None
    if _unit_of(question) or ("upload" in folded and "download" in folded):
        return None
    previous = _last_sql(history)
    if not previous or not re.search(r"\bgroup\s+by\b", previous, flags=re.IGNORECASE):
        return None
    if _group_key(previous) == "subscriber_id":
        return None
    base = previous
    if not re.search(r"\border\s+by\b", previous, flags=re.IGNORECASE):
        base = _last_ordered_like(history, _group_key(previous)) or previous
    if not _has_call(base, r"count\s*\(\s*distinct\b"):
        base = re.sub(
            r"(?i)\bFROM\b",
            ", COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
            "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users FROM",
            base,
            count=1,
        )
    return re.sub(r"\s+", " ", base).strip()


def _last_ordered_like(history: list[dict], group_key: str | None) -> str | None:
    if not group_key:
        return None
    for item in reversed(history):
        sql = str(item.get("sql") or "")
        if _group_key(sql) == group_key and re.search(r"\border\s+by\b", sql, flags=re.IGNORECASE):
            return sql
    return None


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
    if mentions(text, "their_apps"):
        return True
    folded = text.lower()
    if re.search(r"\b(applications?|apps)\b", folded):
        return True
    if re.search(r"\bactivity\s+details?\b", folded) or re.search(r"\bdetails?\s+of\b", folded):
        return True
    return False


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
    cleaned = question.replace("'", "").replace("’", "").replace('"', "").replace("`", "")
    patterns = (
        r"\b(?:what|how)\s+about\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
        r"\b(?:user|subscriber)\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
        r"\bby\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
        r"\bof\s+([A-Za-z0-9][A-Za-z0-9._-]{2,64})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned):
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
    match = re.search(r"\b(?:top|first|limit)\s+(\d+)\b", text.lower())
    return int(match.group(1)) if match else None


_LIMIT_FILLER = {
    "show", "me", "the", "a", "an", "please", "of", "them", "rows", "row",
    "give", "just", "only", "list", "lists",
}
_LIMIT_MARK = "Which result should use the new limit?"


def _is_limit_only(question: str) -> bool:
    if _limit_in(question) is None:
        return False
    rest = re.sub(r"\b(?:top|first|limit)\s+\d+\b", " ", question.lower())
    rest = re.sub(r"[^a-z\s]", " ", rest)
    return not [word for word in rest.split() if word not in _LIMIT_FILLER]


def _saved_sqls(history: list[dict]) -> list[str]:
    return [str(item.get("sql") or "").strip() for item in history if str(item.get("sql") or "").strip()]


def _result_description(sql: str) -> str:
    folded = sql.lower()
    named = re.search(r"subscriber_id\s*=\s*'([^']+)'", sql, flags=re.IGNORECASE)
    if "group by requested_server" in folded and named:
        return f"the websites for {named.group(1)}"
    if "count(distinct requested_server)" in folded and "group by subscriber_id" in folded:
        return "the subscribers ranked by website count"
    if "group by app_label" in folded and named:
        return f"the applications for {named.group(1)}"
    if "group by subscriber_id" in folded:
        return "the subscriber ranking"
    if "group by app_label" in folded:
        return "the application list"
    if "group by requested_server" in folded:
        return "the website list"
    return "the previous result"


def _pending_limit(history: list[dict]) -> bool:
    if not history:
        return False
    last = history[-1]
    return not str(last.get("sql") or "").strip() and _LIMIT_MARK in str(last.get("explanation") or "")


def _with_limit(sql: str, limit: int | None, drop: bool) -> str:
    text = re.sub(r"\s+", " ", sql).strip()
    text = re.sub(r"(?i)\s+LIMIT\s+\d+\s*$", "", text)
    if drop or limit is None:
        return text
    return f"{text} LIMIT {int(limit)}"


def _settled_sql(explanation: str, sqls: list[str]) -> str | None:
    text = explanation.lower()
    hits = [sql for sql in sqls if _result_description(sql).lower() in text]
    return hits[0] if len(hits) == 1 else None


def _pick_saved_sql(question: str, sqls: list[str]) -> str | None:
    folded = question.lower()
    website = bool(re.search(r"\b(websites?|sites?|domains?)\b", folded))
    people = bool(re.search(r"\b(users?|subscribers?|ranking|people)\b", folded))
    if website and people:
        if re.search(r"\b(ranking|by website count|the users|the subscribers)\b", folded):
            website = False
        else:
            people = False
    last = bool(re.search(r"\b(those|that list|last one|on screen|this list)\b", folded))
    if last and not website and not people:
        return sqls[-1]
    chosen = []
    for sql in sqls:
        described = _result_description(sql)
        if website and described.startswith("the website"):
            chosen.append(sql)
        elif people and "subscriber" in described:
            chosen.append(sql)
    if len(chosen) == 1:
        return chosen[0]
    if not website and not people and len(sqls) == 1:
        return sqls[-1]
    return None


def limit_followup(question: str, history: list[dict] | None, max_rows: int) -> tuple[str | None, str | None]:
    """A bare limit either edits the last result or asks which result and which size."""
    history = history or []
    sqls = _saved_sqls(history)
    if _pending_limit(history):
        pending = str(history[-1].get("explanation") or "")
        chosen = _pick_saved_sql(question, sqls) or _settled_sql(pending, sqls)
        if chosen is None:
            return None, f"{_LIMIT_MARK} Say which list you mean, and whether you want the top {max_rows} or every row."
        if wants_all_groups(question) or re.search(r"\b(every row|all rows|every website|every subscriber)\b", question.lower()):
            return _with_limit(chosen, None, True), None
        number = _limit_in(question)
        if number is None:
            asked = re.search(r"\btop\s+(\d+)\b", str(history[-1].get("explanation") or "").lower())
            number = int(asked.group(1)) if asked else None
        if number is not None and number > max_rows:
            return None, (
                f"{_LIMIT_MARK} One result can show {max_rows} rows of {_result_description(chosen)}. "
                f"Do you want the top {max_rows}, or every row?"
            )
        if number is None:
            return None, f"{_LIMIT_MARK} Say the top {max_rows}, or every row, for {_result_description(chosen)}."
        return _with_limit(chosen, number, False), None
    number = _limit_in(question)
    if number is None or not _is_limit_only(question):
        if number is not None and number > max_rows:
            return None, (
                f"{_LIMIT_MARK} One result can show {max_rows} rows. "
                f"Do you want the top {max_rows}, or every row?"
            )
        return None, None
    if not sqls:
        return None, f"{_LIMIT_MARK} Top {number} of what? Name the users, applications, or websites."
    if len(sqls) >= 2 and _group_key(sqls[-1]) != _group_key(sqls[-2]):
        cap = f" One result can show {max_rows} rows, so also say {max_rows} or all." if number > max_rows else ""
        return None, (
            f"{_LIMIT_MARK} The top {number} of {_result_description(sqls[-1])}, "
            f"or the top {number} of {_result_description(sqls[-2])}?{cap}"
        )
    if number > max_rows:
        return None, (
            f"{_LIMIT_MARK} One result can show {max_rows} rows of {_result_description(sqls[-1])}. "
            f"Do you want the top {max_rows}, or every row?"
        )
    return _with_limit(sqls[-1], number, False), None


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
    """A user count beside the current rows. 'Users against TikTok' is a new ranking."""
    return bool(
        re.search(
            r"\b(unique users|number of users|user count|users per|users for each|users against each)\b",
            question,
        )
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
