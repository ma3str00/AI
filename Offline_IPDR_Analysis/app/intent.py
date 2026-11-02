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
)
from app.places import (
    asks_same_city,
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
GRAIN_APPS_BY_CLASS = "apps_by_class"
GRAIN_APPS_BY_CATEGORY = "apps_by_category"

_GRAIN_LABELS = {
    GRAIN_AGGREGATE_TOTAL: "one total for traffic/flows (not a user list)",
    GRAIN_TOP_USERS: "top subscribers/users ranked by usage",
    GRAIN_USAGE_BY_ISP: "totals grouped by ISP/operator",
    GRAIN_TOP_APP_PER_ISP: "top application within each ISP",
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


def resolve_intent(question: str, history: list[dict] | None = None) -> Intent | None:
    """Parse a structured intent for the high-value ISP/app follow-up family."""
    history = history or []
    folded = question.lower()
    if (
        mentioned_cities(question)
        or mentioned_areas(question)
        or mentioned_provinces(question)
        or street_before_city(question)
        or asks_same_city(question)
        or re.search(r"\bsame\s+city\b", folded)
    ):
        # City/area rankings stay on the older follow-up path.
        return None

    measure = "total_mb" if re.search(r"\b(bytes|traffic|volume|mb|gb|tb)\b", folded) else "flows"
    if re.search(r"\b(flows?|flow count|hits)\b", folded) and not re.search(
        r"\b(bytes|traffic|volume|mb|gb|tb)\b", folded
    ):
        measure = "flows"
    limit = _limit_in(question) or 20

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
        wants_top = bool(re.search(r"\b(top|most\s+used|highest|best)\b", folded))
        names_item = bool(
            re.search(r"\b(apps?|applications?|platforms?|protocols?|labels?|games?)\b", folded)
        )
        class_id = class_from_question(question)
        category = None if class_id else category_from_question(question)
        has_subject = bool(names_item or class_id or category or _app_stem_in_question(question))
        if wants_top and has_subject and not re.search(r"\b(websites?|domains?)\b", folded):
            filters = _subject_filters(question, history, class_id=class_id, category=category)
            if not filters:
                return None
            top_n = 1
            top_match = re.search(r"\btop\s+(\d+)\b", folded)
            if top_match:
                top_n = max(1, min(int(top_match.group(1)), 20))
            # "highest used" without an explicit flow/byte word → traffic volume.
            if measure == "flows" and re.search(r"\b(highest|most)\s+used\b", folded):
                measure = "total_mb"
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

    # Class / category app rankings (no ISP breakdown).
    if not _asks_isp_breakdown(question):
        class_id = class_from_question(question)
        if class_id and not re.search(r"\b(users?|subscribers?|operators?|websites?)\b", folded):
            names_apps = bool(re.search(r"\b(apps?|applications?|protocols?)\b", folded))
            ranked = bool(re.search(r"\b(top|list|most|popular|used|show|display|usage|pattern)\b", folded))
            if names_apps or ranked:
                predicate = class_label_predicate(class_id)
                if predicate:
                    where = [
                        predicate,
                        "IFNULL(subscriber_id, '') <> ''",
                        "LOCATE('_', subscriber_id) = 0",
                        *_merge_app_exclusions(question, _last_sql(history)),
                    ]
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
                where = [
                    f"LOWER(app_category) = '{category}'",
                    "IFNULL(subscriber_id, '') <> ''",
                    "LOCATE('_', subscriber_id) = 0",
                    *_merge_app_exclusions(question, _last_sql(history)),
                ]
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
    """Compile Intent into a single MySQL SELECT."""
    where_sql = " AND ".join(intent.where) if intent.where else "1=1"
    order = intent.measure if intent.measure in {"flows", "total_mb", "total_bytes"} else "flows"
    if intent.grain == GRAIN_AGGREGATE_TOTAL:
        return (
            "SELECT SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            "COUNT(*) AS flows "
            f"FROM ntc_ssl_logs WHERE {where_sql}"
        )
    if intent.grain == GRAIN_TOP_USERS:
        return (
            "SELECT subscriber_id, "
            + _address_expression("client_asn")
            + ", SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            "COUNT(*) AS flows "
            f"FROM ntc_ssl_logs WHERE {where_sql} "
            f"GROUP BY subscriber_id ORDER BY {order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain == GRAIN_USAGE_BY_ISP:
        filters = list(intent.where)
        if not any("client_asn" in part and "IFNULL" in part for part in filters):
            filters.insert(0, "IFNULL(client_asn, '') <> ''")
        return (
            "SELECT client_asn, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
            f"FROM ntc_ssl_logs WHERE {' AND '.join(filters)} "
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
            f"FROM ntc_ssl_logs WHERE {' AND '.join(filters)} "
            "GROUP BY client_asn, app_label"
            f") ranked WHERE rn <= {int(intent.top_n)} "
            f"ORDER BY {outer_order} DESC LIMIT {int(intent.limit)}"
        )
    if intent.grain in {GRAIN_APPS_BY_CLASS, GRAIN_APPS_BY_CATEGORY}:
        return (
            "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
            f"FROM ntc_ssl_logs WHERE {where_sql} "
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
    if measure not in {"flows", "total_mb", "total_bytes"}:
        measure = intent.measure
    return replace(intent, grain=grain, measure=measure, ambiguous=False, clarify_prompt=None)


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
