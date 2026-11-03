"""LLM referee / follow-up normalizer. Confirms slots; does not invent free SQL."""

from __future__ import annotations

import json
import re

from app.intent import (
    GRAIN_AGGREGATE_TOTAL,
    GRAIN_APPS_BY_CATEGORY,
    GRAIN_APPS_BY_CLASS,
    GRAIN_TOP_APP_PER_CITY,
    GRAIN_TOP_APP_PER_ISP,
    GRAIN_TOP_ISP_PER_CITY,
    GRAIN_TOP_USERS,
    GRAIN_USAGE_BY_ISP,
    Intent,
    apply_followup_normalization,
    apply_referee_decision,
    intent_from_prior_sql,
)
from app.llama_client import LlamaError, LlamaServer

_ALLOWED_GRAINS = (
    GRAIN_AGGREGATE_TOTAL,
    GRAIN_TOP_USERS,
    GRAIN_USAGE_BY_ISP,
    GRAIN_TOP_APP_PER_ISP,
    GRAIN_TOP_APP_PER_CITY,
    GRAIN_TOP_ISP_PER_CITY,
    GRAIN_APPS_BY_CLASS,
    GRAIN_APPS_BY_CATEGORY,
)


def referee_intent(
    llama: LlamaServer,
    intent: Intent,
    question: str,
    history: list[dict] | None = None,
) -> Intent | str | None:
    """Return a confirmed Intent, a clarify string, or None to skip the rule path."""
    if not intent.ambiguous:
        return intent
    messages = [
        {
            "role": "system",
            "content": (
                "You verify an IPDR analytics intent. Do not write SQL. "
                "Choose whether the proposed grain matches the user question. "
                "Reply with JSON only: "
                '{"action":"accept"|"revise"|"clarify"|"reject","grain":"...","measure":"flows|total_mb|users","clarify":"..."} . '
                f"Allowed grain values: {', '.join(_ALLOWED_GRAINS)}. "
                "accept = proposed grain is right. "
                "revise = change grain/measure only. "
                "clarify = ask one short question when users vs totals (or apps vs per-ISP) is unclear. "
                "reject = this rule family does not fit; another path should handle it."
            ),
        },
        {
            "role": "user",
            "content": _user_payload(intent, question, history),
        },
    ]
    try:
        raw = llama.chat(messages, json_mode=True, temperature=0)
    except LlamaError:
        # Offline/model down: keep deterministic default grain.
        return intent
    decision = _parse(raw)
    if not decision:
        return intent
    action = str(decision.get("action") or "accept").strip().lower()
    if action == "accept":
        return apply_referee_decision(
            intent, {"action": "accept", "grain": intent.grain, "measure": intent.measure}
        )
    return apply_referee_decision(intent, decision)


def normalize_followup(
    llama: LlamaServer,
    question: str,
    history: list[dict] | None = None,
) -> Intent | str | None:
    """Map a vague follow-up onto the prior structured Intent (measure/top_n), not free SQL.

    Returns Intent to build, a clarify string, or None to fall through to NL→SQL.
    """
    history = history or []
    prior_question, prior_sql = _prior_turn(history)
    if not prior_sql:
        return None
    prior = intent_from_prior_sql(prior_sql)
    if prior is None:
        return None
    messages = [
        {
            "role": "system",
            "content": (
                "You normalize an IPDR analytics follow-up. Do not write SQL. "
                "The previous answer already used a fixed query shape (grain). "
                "Decide how the new user message relates to that result. "
                "Reply with JSON only: "
                '{"action":"rerank_measure"|"revise"|"clarify"|"keep"|"new_question"|"reject",'
                '"measure":"flows|total_mb|users","top_n":null,"clarify":""} . '
                "rerank_measure = same list/shape, change ranking measure "
                "(flow/session/hit count → flows; traffic/volume/bytes → total_mb; "
                "most users/subscribers on ISP-per-city → users). "
                "revise = same grain with measure and/or top_n change (top_n = apps/ISPs per group). "
                "clarify = ask one short question when the follow-up is unclear. "
                "keep = no meaningful change. "
                "new_question or reject = this is not a tweak of the previous result. "
                "Typos in measure words still count (e.g. volme → volume)."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Previous question: {prior_question}\n"
                f"Previous grain: {prior.summary()} (top_n={prior.top_n})\n"
                f"Previous SQL (truncated): {prior_sql[:700]}\n"
                f"New follow-up: {question}\n"
                "Normalize the follow-up."
            ),
        },
    ]
    try:
        raw = llama.chat(messages, json_mode=True, temperature=0)
    except LlamaError:
        return None
    decision = _parse(raw)
    if not decision:
        return None
    return apply_followup_normalization(prior, decision)


def _prior_turn(history: list[dict]) -> tuple[str, str]:
    for turn in reversed(history):
        sql = (turn.get("sql") or "").strip()
        if not sql:
            continue
        return (turn.get("question") or "").strip(), sql
    return "", ""


def _user_payload(intent: Intent, question: str, history: list[dict] | None) -> str:
    prior = ""
    if history:
        last = history[-1]
        prior = f"Previous question: {last.get('question', '')}\nPrevious SQL: {(last.get('sql') or '')[:500]}\n"
    return (
        f"User question: {question}\n"
        f"{prior}"
        f"Proposed intent: {intent.summary()}\n"
        f"Ambiguity note: {intent.clarify_prompt or 'n/a'}\n"
        "Decide accept, revise, clarify, or reject."
    )


def _parse(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
