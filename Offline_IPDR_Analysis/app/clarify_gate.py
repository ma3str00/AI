"""Prefer asking over guessing when the request is not fully determined."""

from __future__ import annotations

import re

from app.answer_check import drops_previous_apps
from app.followup import (
    _app_stem_in_question,
    looks_like_result_tweak,
    preserves_prior_filters,
    preserves_result_shape,
)
from app.places import mentioned_areas, mentioned_cities, mentioned_isps, mentioned_provinces


def uncertain_followup_prompt(question: str, history: list[dict] | None = None) -> str:
    """Clarify when a follow-up cannot be applied safely to the previous result."""
    history = history or []
    prior_q = ""
    for turn in reversed(history):
        if str(turn.get("sql") or "").strip():
            prior_q = str(turn.get("question") or "").strip()
            break
    prior_bit = f' after “{prior_q}”' if prior_q else ""
    if looks_like_result_tweak(question, history):
        return (
            f"I am not sure how to change the previous result{prior_bit}. "
            "Should I keep the same filters (app, ISP, city) and only change the ranking "
            "(flow count vs traffic volume) and/or the row limit, or is this a new question?"
        )
    return (
        f"I am not sure what you want{prior_bit}. "
        "Please confirm whether this should keep the previous filters and only adjust ranking/limit, "
        "or describe the new question with the app, ISP/city (if any), ranking measure, and how many rows."
    )


def silently_weakens_prior(
    question: str,
    prior_sql: str | None,
    new_sql: str | None,
) -> bool:
    """True when new SQL drops prior scope without the user clearly changing scope."""
    if not prior_sql or not new_sql:
        return False
    if preserves_prior_filters(prior_sql, new_sql) and preserves_result_shape(prior_sql, new_sql):
        return False
    # User named a place / ISP / app → allowed to rebuild with that scope.
    if (
        mentioned_cities(question)
        or mentioned_areas(question)
        or mentioned_provinces(question)
        or mentioned_isps(question)
        or _app_stem_in_question(question)
    ):
        return False
    if drops_previous_apps(question):
        # "Every application" may drop app_label only — not ISP/city filters.
        from app.followup import _where_like_predicates

        prior = _where_like_predicates(prior_sql)
        now = _where_like_predicates(new_sql)
        prior_scope = {item for item in prior if "app_label" not in item}
        if prior_scope.issubset(now) and preserves_result_shape(prior_sql, new_sql):
            return False
        return True
    if looks_like_result_tweak(question, [{"sql": prior_sql}]):
        return not (
            preserves_prior_filters(prior_sql, new_sql) and preserves_result_shape(prior_sql, new_sql)
        )
    # Follow-up with history but no new scope words, yet filters/shape changed.
    return not preserves_prior_filters(prior_sql, new_sql) or not preserves_result_shape(
        prior_sql, new_sql
    )


