"""Intent + slots for IPDR follow-ups. SQL is built from this object, not from free text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from app.app_map import (
    category_from_question,
    class_from_question,
    class_label_predicate,
)
from app.followup import (
    _address_expression,
    _asks_aggregate_total,
    _asks_isp_breakdown,
    _app_isp_where,
    _app_stem_in_question,
    _last_sql,
    _limit_in,
    _merge_app_exclusions,
    _ranks_isp_entities,
    _inherited_time_predicate,
    _relative_start_time_predicate,
    looks_like_result_tweak,
)
from app.places import (
    asks_same_city,
    city_from_loc,
    column_predicate,
    mentioned_areas,
    mentioned_cities,
    mentioned_isps,
    mentioned_provinces,
    street_before_city,
)

GRAIN_AGGREGATE_TOTAL = "aggregate_total"
GRAIN_TOP_USERS = "top_users"
GRAIN_USAGE_BY_ISP = "usage_by_isp"
GRAIN_TOP_APP_PER_ISP = "top_app_per_isp"
GRAIN_TOP_APP_PER_CITY = "top_app_per_city"
GRAIN_TOP_ISP_PER_CITY = "top_isp_per_city"
GRAIN_APPS_BY_CLASS = "apps_by_class"
GRAIN_APPS_BY_CATEGORY = "apps_by_category"

_GRAIN_LABELS = {
    GRAIN_AGGREGATE_TOTAL: "one total for traffic/flows (not a user list)",
    GRAIN_TOP_USERS: "top subscribers/users ranked by usage",
    GRAIN_USAGE_BY_ISP: "totals grouped by ISP/operator",
    GRAIN_TOP_APP_PER_ISP: "top application within each ISP",
    GRAIN_TOP_APP_PER_CITY: "top application within each city",
    GRAIN_TOP_ISP_PER_CITY: "top ISP/operator within each city",
    GRAIN_APPS_BY_CLASS: "applications in a conceptual class, ranked",
    GRAIN_APPS_BY_CATEGORY: "applications in a DPI category, ranked",
}


@dataclass
class Intent:
    grain: str
    where: list[str] = field(default_factory=list)
    app: str | None = None
    class_id: str | None = None
    category: str | None = None
    measure: str = "flows"  # flows | total_mb | total_bytes
    top_n: int = 1
    limit: int = 20
    ambiguous: bool = False
    clarify_prompt: str | None = None
    source: str = ""

    def summary(self) -> str:
        bits = [f"grain={self.grain}"]
        if self.app:
            bits.append(f"app={self.app}")
        if self.class_id:
            bits.append(f"class={self.class_id}")
        if self.category:
            bits.append(f"category={self.category}")
        bits.append(f"measure={self.measure}")
        return ", ".join(bits)


def _asks_place_breakdown(folded: str) -> bool:
    """True for per-city / each-city style questions the ISP intent path must not steal."""
    if re.search(
        r"\b(?:per|by|across|against|for each|against each|on each|in each|"
        r"in every|for every|against every|grouped by)\s+"
        r"(?:the\s+)?(?:cities|city|areas?|towns?|districts?|provinces?)\b",
        folded,
    ):
        return True
    return bool(re.search(r"\b(?:city|area|town|district|province)[\s\-]?wise\b", folded))


def _top_app_per_city_intent(
    question: str,
    history: list[dict],
    folded: str,
    measure: str,
    limit: int,
) -> Intent | None:
    """Top app/platform within each city from loc (ROW_NUMBER), e.g. social media in each city."""
    wants_top = bool(
        re.search(
            r"\b(top|most\s+used|most\s+popular|highest|best|rank|popular|popularity)\b",
            folded,
        )
    )
    names_item = bool(
        re.search(r"\b(apps?|applications?|platforms?|protocols?|labels?|games?)\b", folded)
    )
    class_id = class_from_question(question)
    category = None if class_id else category_from_question(question)
    app = _app_stem_in_question(question)
    has_subject = bool(names_item or class_id or category or app)
    if not (wants_top and has_subject) or re.search(r"\b(websites?|domains?)\b", folded):
        return None
    # Bare "highest used app per city" has no class/app filter — still valid.
    if class_id or category or app:
        filters = _subject_filters(question, history, class_id=class_id, category=category)
        if not filters:
            return None
    else:
        filters = [
            "IFNULL(subscriber_id, '') <> ''",
            "LOCATE('_', subscriber_id) = 0",
            "IFNULL(loc, '') <> ''",
            *_merge_app_exclusions(question, _last_sql(history)),
        ]
    if not any(re.search(r"\bloc\b", part, flags=re.IGNORECASE) for part in filters):
        filters.append("IFNULL(loc, '') <> ''")
    top_n = 1
    top_match = re.search(r"\btop\s+(\d+)\b", folded)
    if top_match:
        top_n = max(1, min(int(top_match.group(1)), 20))
    elif re.search(r"\blists?\b", folded):
        top_n = 5
    # "highest/most popular" defaults to traffic unless flows/hits are named.
    if (
        measure == "flows"
        and re.search(r"\b(highest|most)\s+(used|popular)\b|\bpopular(?:ity)?\b", folded)
        and not re.search(r"\b(flows?|flow count|hits|sessions?|session count)\b", folded)
    ):
        measure = "total_mb"
    subject = (
        "social media apps"
        if class_id == "social_media"
        else (
            "IM platforms"
            if category == "im" or class_id == "messaging"
            else (
                f"{category} apps"
                if category
                else (f"{class_id.replace('_', ' ')} apps" if class_id else "apps")
            )
        )
    )
    if not _explicit_measure(folded) and not re.search(
        r"\b(highest|most)\s+(used|popular)\b|\bpopular(?:ity)?\b", folded
    ):
        return Intent(
            grain=GRAIN_TOP_APP_PER_CITY,
            where=filters,
            app=app,
            class_id=class_id,
            category=category,
            measure="total_mb",
            top_n=top_n,
            limit=limit,
            ambiguous=True,
            clarify_prompt=(
                f"Do you want the top {subject} in each city based on traffic volume "
                "or number of flows?"
            ),
            source="top_app_per_city_measure",
        )
    return Intent(
        grain=GRAIN_TOP_APP_PER_CITY,
        where=filters,
        app=app,
        class_id=class_id,
        category=category,
        measure=measure,
        top_n=top_n,
        limit=limit,
        ambiguous=False,
        source="top_app_per_city",
    )


def _top_isp_per_city_intent(
    question: str,
    history: list[dict],
    folded: str,
    measure: str,
    limit: int,
) -> Intent | None:
    """ISP/operator with the most users (or traffic) within each city."""
    if not re.search(r"\b(isps?|operators?|providers?|networks?|asns?)\b", folded):
        return None
    if re.search(r"\b(apps?|applications?|platforms?|websites?|domains?)\b", folded):
        return None
    wants_rank = bool(
        re.search(r"\b(top|most|highest|best|rank|popular|popularity)\b", folded)
    )
    wants_users = bool(re.search(r"\b(users?|subscribers?)\b", folded))
    if not wants_rank:
        return None
    # "ISP with most users in each city" — users is the ranking measure.
    if wants_users:
        measure = "users"
    elif measure == "flows" and re.search(r"\b(highest|most)\b", folded) and not re.search(
        r"\b(flows?|flow count|hits)\b", folded
    ):
        measure = "total_mb"
    filters = [
        "IFNULL(client_asn, '') <> ''",
        "IFNULL(subscriber_id, '') <> ''",
        "LOCATE('_', subscriber_id) = 0",
        "IFNULL(loc, '') <> ''",
        *_merge_app_exclusions(question, _last_sql(history)),
    ]
    top_n = 1
    top_match = re.search(r"\btop\s+(\d+)\b", folded)
    if top_match:
        top_n = max(1, min(int(top_match.group(1)), 20))
    return Intent(
        grain=GRAIN_TOP_ISP_PER_CITY,
        where=filters,
        measure=measure,
        top_n=top_n,
        limit=limit,
        ambiguous=False,
        source="top_isp_per_city",
    )


def _pending_usage_clarify_question(history: list[dict]) -> str | None:
    """Prior question that produced the totals-vs-users clarify prompt."""
    if not history:
        return None
    last = history[-1]
    if (last.get("sql") or "").strip():
        return None
    note = (last.get("explanation") or "").lower()
    if "do you want the total" not in note or "ranked by usage" not in note:
        return None
    prior = (last.get("question") or "").strip()
    return prior or None


def _pending_open_clarify_question(history: list[dict]) -> str | None:
    """Any prior turn that asked a clarify question and has no SQL yet."""
    if not history:
        return None
    last = history[-1]
    if (last.get("sql") or "").strip():
        return None
    note = (last.get("explanation") or "").lower()
    if "do you want" not in note:
        return None
    prior = (last.get("question") or "").strip()
    return prior or None


def _explicit_measure(folded: str) -> bool:
    return bool(
        re.search(
            r"\b(bytes|traffic|volume|mb|gb|tb|flows?|flow count|hits|sessions?|session count)\b",
            folded,
        )
    )


def _measure_only_reply(question: str) -> str | None:
    """Map a short clarify reply to total_mb or flows."""
    folded = question.lower().strip()
    has_volume = bool(re.search(r"\b(bytes|traffic|volume|mb|gb|tb)\b", folded))
    has_flows = bool(
        re.search(r"\b(flows?|flow count|hits|sessions?|session count)\b", folded)
    )
    if has_volume and not has_flows:
        return "total_mb"
    if has_flows and not has_volume:
        return "flows"
    if re.search(r"\b(traffic\s+volume|by\s+volume|by\s+traffic|based on traffic)\b", folded):
        return "total_mb"
    if re.search(
        r"\b(number of flows|number of sessions|based on flows|by flows|by sessions)\b",
        folded,
    ):
        return "flows"
    return None


def resolve_intent(question: str, history: list[dict] | None = None) -> Intent | None:
    """Parse a structured intent for the high-value ISP/app follow-up family."""
    history = history or []
    folded = question.lower()

    # Same/repeat/instead/rank-them tweaks belong to SQL patching / LLM normalize — not a new grain.
    if (
        _last_sql(history)
        and looks_like_result_tweak(question, history)
        and not _pending_open_clarify_question(history)
        and not _pending_usage_clarify_question(history)
    ):
        return None

    # "based on traffic volume" after a clarify — redo the prior question with that measure.
    pending_open = _pending_open_clarify_question(history)
    measure_reply = _measure_only_reply(question) if pending_open else None
    if pending_open and measure_reply and not re.search(
        r"\b(top|highest|isp|users?|subscribers?|apps?|platforms?)\b", folded
    ):
        suffix = "by traffic volume" if measure_reply == "total_mb" else "by number of flows"
        return resolve_intent(f"{pending_open} {suffix}", history[:-1])

    measure = "total_mb" if re.search(r"\b(bytes|traffic|volume|mb|gb|tb)\b", folded) else "flows"
    if re.search(r"\b(flows?|flow count|hits|sessions?|session count)\b", folded) and not re.search(
        r"\b(bytes|traffic|volume|mb|gb|tb)\b", folded
    ):
        measure = "flows"
    limit = _limit_in(question) or 20

    # Per-city window grains (top app / top ISP) — deterministic SQL, not the LLM.
    if _asks_place_breakdown(folded):
        city_intent = _top_app_per_city_intent(question, history, folded, measure, limit)
        if city_intent is not None:
            return city_intent
        isp_city = _top_isp_per_city_intent(question, history, folded, measure, limit)
        if isp_city is not None:
            return isp_city
        # Plain city rankings stay on _cities_by_usage / LLM via follow-up.
        # Do not claim the question here.

    if (
        mentioned_cities(question)
        or mentioned_areas(question)
        or mentioned_provinces(question)
        or street_before_city(question)
        or asks_same_city(question)
        or re.search(r"\bsame\s+city\b", folded)
    ):
        # Named city/area rankings stay on the older follow-up path.
        return None

    # Answer to "totals or top users?" — keep slots from the clarified question.
    pending = _pending_usage_clarify_question(history)
    if pending:
        prior_history = history[:-1]
        where = _app_isp_where(pending, prior_history)
        if not where:
            # Reply may say "on that ISP"; merge reply text for ISP resolution.
            where = _app_isp_where(f"{pending} {question}", prior_history)
        app = _app_stem_in_question(pending) or _app_stem_in_question(question)
        if where and (
            _asks_aggregate_total(question)
            or re.search(r"\btotals?\b", folded)
            or ("total" in folded and "flow count" in folded)
        ):
            return Intent(
                grain=GRAIN_AGGREGATE_TOTAL,
                where=where,
                app=app,
                measure=measure,
                limit=1,
                ambiguous=False,
                source="clarify_aggregate_app_on_isp",
            )
        if where and re.search(r"\b(users?|subscribers?|ranked)\b", folded):
            return Intent(
                grain=GRAIN_TOP_USERS,
                where=where,
                app=app,
                measure=measure,
                limit=limit,
                ambiguous=False,
                source="clarify_top_users_on_isp",
            )

    # Top app/platform/game per ISP/operator (including "ISP wise" / "on each ISP").
    if _asks_isp_breakdown(question):
        wants_top = bool(
            re.search(r"\b(top|most\s+used|most\s+popular|highest|best|popular)\b", folded)
        )
        names_item = bool(
            re.search(r"\b(apps?|applications?|platforms?|protocols?|labels?|games?)\b", folded)
        )
        class_id = class_from_question(question)
        category = None if class_id else category_from_question(question)
        has_subject = bool(names_item or class_id or category or _app_stem_in_question(question))
        # "top ISPs by social media" ranks operators; "top social media apps per ISP" ranks apps.
        ranks_isps = _ranks_isp_entities(question)
        if (
            wants_top
            and has_subject
            and not ranks_isps
            and not re.search(r"\b(websites?|domains?)\b", folded)
        ):
            filters = _subject_filters(question, history, class_id=class_id, category=category)
            if not filters:
                return None
            top_n = 1
            top_match = re.search(r"\btop\s+(\d+)\b", folded)
            if top_match:
                top_n = max(1, min(int(top_match.group(1)), 20))
            # "highest/most popular used" defaults to traffic unless flows/hits are named.
            if (
                measure == "flows"
                and re.search(r"\b(highest|most)\s+(used|popular)\b|\bpopular\b", folded)
                and not re.search(r"\b(flows?|flow count|hits)\b", folded)
            ):
                measure = "total_mb"
            subject = (
                "IM platforms"
                if category == "im" or class_id == "messaging"
                else (
                    f"{category} apps"
                    if category
                    else (f"{class_id.replace('_', ' ')} apps" if class_id else "apps")
                )
            )
            if not _explicit_measure(folded) and not re.search(
                r"\b(highest|most)\s+(used|popular)\b|\bpopular\b", folded
            ):
                return Intent(
                    grain=GRAIN_TOP_APP_PER_ISP,
                    where=filters,
                    app=_app_stem_in_question(question),
                    class_id=class_id,
                    category=category,
                    measure="total_mb",
                    top_n=top_n,
                    limit=limit,
                    ambiguous=True,
                    clarify_prompt=(
                        f"Do you want the top {subject} per ISP based on traffic volume "
                        "or number of flows?"
                    ),
                    source="top_app_per_isp_measure",
                )
            return Intent(
                grain=GRAIN_TOP_APP_PER_ISP,
                where=filters,
                app=_app_stem_in_question(question),
                class_id=class_id,
                category=category,
                measure=measure,
                top_n=top_n,
                limit=limit,
                ambiguous=False,
                source="top_app_per_isp",
            )

        # ISP totals for a class/category/app ("network-wise usage pattern…").
        if re.search(r"\b(usage|pattern|traffic|volume|flows?|bytes|show|display|top)\b", folded):
            if re.search(r"\b(websites?|domains?)\b", folded):
                return None
            class_id = class_from_question(question)
            category = None if class_id else category_from_question(question)
            filters = _subject_filters(question, history, class_id=class_id, category=category)
            if not filters and not class_id and not category:
                # Bare ISP breakdown without subject — leave to other handlers.
                return None
            if not filters:
                return None
            return Intent(
                grain=GRAIN_USAGE_BY_ISP,
                where=filters,
                class_id=class_id,
                category=category,
                measure=measure,
                limit=limit,
                ambiguous=False,
                source="usage_by_isp",
            )

    # App + ISP: total vs top users.
    where = _app_isp_where(question, history)
    if where and not class_from_question(question) and not category_from_question(question):
        app = _app_stem_in_question(question)
        if _asks_aggregate_total(question):
            return Intent(
                grain=GRAIN_AGGREGATE_TOTAL,
                where=where,
                app=app,
                measure=measure,
                limit=1,
                ambiguous=False,
                source="aggregate_app_on_isp",
            )
        # Explicit user ranking.
        if re.search(r"\b(users?|subscribers?)\b", folded) or (
            re.search(r"\b(top|highest|most)\b", folded) and re.search(r"\b(users?|subscribers?)\b", folded)
        ):
            return Intent(
                grain=GRAIN_TOP_USERS,
                where=where,
                app=app,
                measure=measure,
                limit=limit,
                ambiguous=False,
                source="top_users_on_isp",
            )
        # Soft "usage/show/traffic on ISP" — default top users but mark ambiguous.
        if re.search(r"\b(?:on|from|for|at|via)\b", folded) and re.search(
            r"\b(usage|traffic|volume|bytes|show|display|list)\b", folded
        ):
            return Intent(
                grain=GRAIN_TOP_USERS,
                where=where,
                app=app,
                measure=measure,
                limit=limit,
                ambiguous=True,
                clarify_prompt=(
                    "Do you want the total TikTok/Facebook-style traffic and flow count on that ISP, "
                    "or the top users ranked by usage?"
                    if not app
                    else (
                        f"Do you want the total {app} traffic and flow count on that ISP, "
                        f"or the top {app} users ranked by usage?"
                    )
                ),
                source="ambiguous_usage_on_isp",
            )

    # Class / category app rankings (no ISP breakdown). Keep named ISP filters (e.g. on Jazz).
    if not _asks_isp_breakdown(question):
        class_id = class_from_question(question)
        if class_id and not re.search(r"\b(users?|subscribers?|operators?|websites?)\b", folded):
            names_apps = bool(re.search(r"\b(apps?|applications?|protocols?)\b", folded))
            ranked = bool(re.search(r"\b(top|list|most|popular|used|show|display|usage|pattern)\b", folded))
            if names_apps or ranked:
                where = _subject_filters(question, history, class_id=class_id, category=None)
                if where:
                    return Intent(
                        grain=GRAIN_APPS_BY_CLASS,
                        where=where,
                        class_id=class_id,
                        measure=measure,
                        limit=limit,
                        ambiguous=False,
                        source="apps_by_class",
                    )
        category = category_from_question(question)
        if category and not class_id and not re.search(r"\b(users?|subscribers?|operators?|websites?)\b", folded):
            names_apps = bool(re.search(r"\b(apps?|applications?|protocols?)\b", folded))
            ranked = bool(re.search(r"\b(top|list|most|popular|used|show|display|usage|pattern)\b", folded))
            if names_apps or ranked:
                where = _subject_filters(question, history, class_id=None, category=category)
                if where:
                    return Intent(
                        grain=GRAIN_APPS_BY_CATEGORY,
                        where=where,
                        category=category,
                        measure=measure,
                        limit=limit,
                        ambiguous=False,
                        source="apps_by_category",
                    )
    return None


def build_sql(intent: Intent) -> str:
    """Compile Intent into a single SELECT (MySQL-shaped; executed on ClickHouse)."""
    where_sql = " AND ".join(intent.where) if intent.where else "1=1"
    order = (
        intent.measure
        if intent.measure in {"flows", "total_mb", "total_bytes", "users"}
        else "flows"
    )
    if intent.grain == GRAIN_AGGREGATE_TOTAL:
        return (
            "SELECT SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            "COUNT(*) AS flows "
            f"FROM ntc_ssl_log WHERE {where_sql}"
        )
    if intent.grain == GRAIN_TOP_USERS:
        return (
            "SELECT subscriber_id, "
            + _address_expression("client_asn")
            + ", SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            "COUNT(*) AS flows "
            f"FROM ntc_ssl_log WHERE {where_sql} "
            f"GROUP BY subscriber_id ORDER BY {order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain == GRAIN_USAGE_BY_ISP:
        filters = list(intent.where)
        if not any("client_asn" in part and "IFNULL" in part for part in filters):
            filters.insert(0, "IFNULL(client_asn, '') <> ''")
        return (
            "SELECT client_asn, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
            f"FROM ntc_ssl_log WHERE {' AND '.join(filters)} "
            f"GROUP BY client_asn ORDER BY {order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain == GRAIN_TOP_APP_PER_ISP:
        filters = list(intent.where)
        if not any("IFNULL(client_asn" in part for part in filters):
            filters.insert(0, "IFNULL(client_asn, '') <> ''")
        window_order = (
            "SUM(c2s_byte_num + s2c_byte_num) DESC"
            if order in {"total_mb", "total_bytes"}
            else "COUNT(*) DESC"
        )
        outer_order = "total_mb" if order in {"total_mb", "total_bytes"} else "flows"
        return (
            "SELECT client_asn, app_label, flows, total_bytes, total_mb FROM ("
            "SELECT client_asn, app_label, COUNT(*) AS flows, "
            "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            f"ROW_NUMBER() OVER (PARTITION BY client_asn ORDER BY {window_order}) AS rn "
            f"FROM ntc_ssl_log WHERE {' AND '.join(filters)} "
            "GROUP BY client_asn, app_label"
            f") ranked WHERE rn <= {int(intent.top_n)} "
            f"ORDER BY {outer_order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain == GRAIN_TOP_APP_PER_CITY:
        filters = list(intent.where)
        if not any(re.search(r"\bloc\b", part, flags=re.IGNORECASE) for part in filters):
            filters.append("IFNULL(loc, '') <> ''")
        window_order = (
            "SUM(c2s_byte_num + s2c_byte_num) DESC"
            if order in {"total_mb", "total_bytes"}
            else "COUNT(*) DESC"
        )
        outer_order = "total_mb" if order in {"total_mb", "total_bytes"} else "flows"
        city_expr = city_from_loc()
        where_sql = " AND ".join(filters)
        return (
            "SELECT city, app_label, flows, total_bytes, total_mb FROM ("
            "SELECT city, app_label, COUNT(*) AS flows, "
            "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            f"ROW_NUMBER() OVER (PARTITION BY city ORDER BY {window_order}) AS rn FROM ("
            f"SELECT {city_expr} AS city, app_label, c2s_byte_num, s2c_byte_num "
            f"FROM ntc_ssl_log WHERE {where_sql}"
            ") labeled WHERE city IS NOT NULL "
            "GROUP BY city, app_label"
            f") ranked WHERE rn <= {int(intent.top_n)} "
            f"ORDER BY {outer_order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain == GRAIN_TOP_ISP_PER_CITY:
        filters = list(intent.where)
        if not any("IFNULL(client_asn" in part for part in filters):
            filters.insert(0, "IFNULL(client_asn, '') <> ''")
        if not any(re.search(r"\bloc\b", part, flags=re.IGNORECASE) for part in filters):
            filters.append("IFNULL(loc, '') <> ''")
        if order == "users":
            window_order = "COUNT(DISTINCT subscriber_id) DESC"
            outer_order = "users"
            inner_measures = (
                "COUNT(DISTINCT subscriber_id) AS users, "
                "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
                "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
                "COUNT(*) AS flows"
            )
            outer_cols = "city, client_asn, users, flows, total_bytes, total_mb"
        elif order in {"total_mb", "total_bytes"}:
            window_order = "SUM(c2s_byte_num + s2c_byte_num) DESC"
            outer_order = "total_mb"
            inner_measures = (
                "COUNT(DISTINCT subscriber_id) AS users, "
                "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
                "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
                "COUNT(*) AS flows"
            )
            outer_cols = "city, client_asn, users, flows, total_bytes, total_mb"
        else:
            window_order = "COUNT(*) DESC"
            outer_order = "flows"
            inner_measures = (
                "COUNT(DISTINCT subscriber_id) AS users, "
                "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
                "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
                "COUNT(*) AS flows"
            )
            outer_cols = "city, client_asn, users, flows, total_bytes, total_mb"
        city_expr = city_from_loc()
        where_sql = " AND ".join(filters)
        return (
            f"SELECT {outer_cols} FROM ("
            f"SELECT city, client_asn, {inner_measures}, "
            f"ROW_NUMBER() OVER (PARTITION BY city ORDER BY {window_order}) AS rn FROM ("
            f"SELECT {city_expr} AS city, client_asn, subscriber_id, c2s_byte_num, s2c_byte_num "
            f"FROM ntc_ssl_log WHERE {where_sql}"
            ") labeled WHERE city IS NOT NULL "
            "GROUP BY city, client_asn"
            f") ranked WHERE rn <= {int(intent.top_n)} "
            f"ORDER BY {outer_order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain in {GRAIN_APPS_BY_CLASS, GRAIN_APPS_BY_CATEGORY}:
        return (
            "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
            f"FROM ntc_ssl_log WHERE {where_sql} "
            f"GROUP BY app_label ORDER BY {order} DESC LIMIT {int(intent.limit)}"
        )
    raise ValueError(f"unsupported grain: {intent.grain}")


def apply_referee_decision(intent: Intent, decision: dict) -> Intent | str | None:
    """Apply referee JSON. Returns Intent, clarify text, or None to fall through."""
    action = str(decision.get("action") or "").strip().lower()
    if action == "clarify":
        text = (decision.get("clarify") or intent.clarify_prompt or "").strip()
        return text or (intent.clarify_prompt or "Do you want totals, or a ranked user list?")
    if action == "reject":
        return None
    grain = str(decision.get("grain") or intent.grain).strip()
    if grain not in _GRAIN_LABELS:
        grain = intent.grain
    measure = str(decision.get("measure") or intent.measure).strip()
    if measure == "total_bytes":
        measure = "total_mb"
    if measure not in {"flows", "total_mb", "users"}:
        measure = intent.measure
    top_n = intent.top_n
    if decision.get("top_n") is not None:
        try:
            top_n = max(1, min(int(decision["top_n"]), 20))
        except (TypeError, ValueError):
            top_n = intent.top_n
    return replace(
        intent,
        grain=grain,
        measure=measure,
        top_n=top_n,
        ambiguous=False,
        clarify_prompt=None,
    )


def _innermost_where_clauses(sql: str) -> list[str]:
    """WHERE predicates on the base ntc_ssl_log scan inside a compiled intent query."""
    match = re.search(
        r"from\s+ntc_ssl_log\s+where\s+(.+?)(?=\)\s*labeled|\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    clause = match.group(1).strip().rstrip(")")
    return [part.strip() for part in re.split(r"\s+AND\s+", clause, flags=re.IGNORECASE) if part.strip()]


def intent_from_prior_sql(sql: str) -> Intent | None:
    """Recover a structured Intent from deterministic SQL we previously built."""
    text = (sql or "").strip()
    if not text:
        return None
    folded = re.sub(r"\s+", " ", text).lower()
    where = _innermost_where_clauses(text)
    top_n = 1
    rn = re.search(r"\brn\s*<=\s*(\d+)\b", folded)
    if rn:
        top_n = max(1, min(int(rn.group(1)), 20))
    limit = 20
    lim = re.search(r"\blimit\s+(\d+)\s*$", folded)
    if lim:
        limit = max(1, min(int(lim.group(1)), 200))

    measure = "flows"
    window = re.search(
        r"row_number\s*\(\s*\)\s*over\s*\(\s*partition\s+by\s+\w+\s+order\s+by\s+(.+?)\s*\)",
        folded,
    )
    if window:
        order_expr = window.group(1)
        if "sum(c2s_byte_num" in order_expr:
            measure = "total_mb"
        elif "count(distinct" in order_expr:
            measure = "users"
        else:
            measure = "flows"
    elif "order by users" in folded:
        measure = "users"
    elif "order by total_mb" in folded or "order by total_bytes" in folded:
        measure = "total_mb"

    grain: str | None = None
    source = "prior_sql"
    if "row_number()" in folded and "partition by city" in folded:
        if re.search(r"group by city,\s*client_asn", folded):
            grain = GRAIN_TOP_ISP_PER_CITY
            source = "prior_top_isp_per_city"
        elif re.search(r"group by city,\s*app_label", folded):
            grain = GRAIN_TOP_APP_PER_CITY
            source = "prior_top_app_per_city"
    elif "row_number()" in folded and "partition by client_asn" in folded:
        grain = GRAIN_TOP_APP_PER_ISP
        source = "prior_top_app_per_isp"
    elif re.search(r"group by client_asn\b", folded) and "row_number()" not in folded:
        grain = GRAIN_USAGE_BY_ISP
        source = "prior_usage_by_isp"
    elif re.search(r"group by app_label\b", folded) and "row_number()" not in folded:
        if any("app_category" in part.lower() for part in where):
            grain = GRAIN_APPS_BY_CATEGORY
            source = "prior_apps_by_category"
        else:
            grain = GRAIN_APPS_BY_CLASS
            source = "prior_apps_by_class"
    elif re.search(r"group by subscriber_id\b", folded):
        grain = GRAIN_TOP_USERS
        source = "prior_top_users"
    elif "group by" not in folded and re.search(r"\b(sum|count)\s*\(", folded):
        grain = GRAIN_AGGREGATE_TOTAL
        source = "prior_aggregate_total"
    if grain is None:
        return None
    return Intent(
        grain=grain,
        where=where or _base_identified_where(),
        measure=measure,
        top_n=top_n,
        limit=limit,
        ambiguous=False,
        source=source,
    )


def apply_followup_normalization(prior: Intent, decision: dict) -> Intent | str | None:
    """Apply follow-up normalizer JSON onto the prior Intent."""
    action = str(decision.get("action") or "").strip().lower()
    if action in {"reject", "new_question", "keep"}:
        return None
    if action == "clarify":
        text = (decision.get("clarify") or "").strip()
        return text or "Do you want to change the ranking measure, or ask something new?"
    if action not in {"rerank_measure", "revise", "accept"}:
        return None
    measure = str(decision.get("measure") or prior.measure).strip().lower()
    if measure in {"flow", "flow_count", "flows", "hits", "session", "sessions", "session_count"}:
        measure = "flows"
    elif measure in {"traffic", "volume", "bytes", "total_bytes", "total_mb", "mb", "gb", "tb"}:
        measure = "total_mb"
    elif measure in {"user", "users", "subscriber", "subscribers"}:
        measure = "users"
    if measure not in {"flows", "total_mb", "users"}:
        measure = prior.measure
    if prior.grain != GRAIN_TOP_ISP_PER_CITY and measure == "users":
        # Non-ISP-per-city grains do not rank by distinct users in build_sql.
        measure = "flows" if action == "rerank_measure" else prior.measure
    payload = {"action": "revise", "grain": prior.grain, "measure": measure}
    if decision.get("top_n") is not None:
        payload["top_n"] = decision.get("top_n")
    result = apply_referee_decision(prior, payload)
    if isinstance(result, Intent):
        return replace(result, source="followup_normalize")
    return result


def _base_identified_where() -> list[str]:
    return [
        "IFNULL(client_asn, '') <> ''",
        "IFNULL(subscriber_id, '') <> ''",
        "LOCATE('_', subscriber_id) = 0",
    ]


def _subject_filters(
    question: str,
    history: list[dict],
    *,
    class_id: str | None,
    category: str | None,
) -> list[str] | None:
    where = _base_identified_where()
    time_pred = _relative_start_time_predicate(question) or _inherited_time_predicate(
        question, history
    )
    if time_pred:
        where.append(time_pred)
    previous = _last_sql(history)
    if class_id:
        predicate = class_label_predicate(class_id)
        if not predicate:
            return None
        where.append(predicate)
    elif category:
        where.append(f"LOWER(app_category) = '{category}'")
    else:
        app = _app_stem_in_question(question)
        if app:
            where.append(f"LOWER(app_label) LIKE '%{app.lower().replace(chr(39), '')}%'")
        elif previous:
            cat = re.search(r"(?i)LOWER\(app_category\)\s*=\s*'([^']+)'", previous)
            if cat:
                where.append(f"LOWER(app_category) = '{cat.group(1)}'")
            else:
                or_block = re.search(
                    r"(?i)(\((?:LOWER\(app_label\)\s+LIKE\s+'[^']+'\s*OR\s*)+"
                    r"LOWER\(app_label\)\s+(?:LIKE\s+'[^']+'|REGEXP\s+'[^']+')\))",
                    previous,
                )
                if or_block:
                    where.append(or_block.group(1))
                else:
                    return None
        else:
            return None
    where.extend(_merge_app_exclusions(question, previous))
    # Named ISP in a non-breakdown question is handled via _app_isp_where; for
    # class/category ISP totals, optional ISP from question narrows the set.
    isps = mentioned_isps(question)
    if isps and not _asks_isp_breakdown(question):
        where.append(column_predicate("client_asn", isps[-1]))
    return where
