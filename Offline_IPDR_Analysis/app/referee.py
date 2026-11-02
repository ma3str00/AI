"""LLM referee for ambiguous intents. Confirms grain/slots; does not invent free SQL."""

from __future__ import annotations

import json
import re

from app.intent import (
    GRAIN_AGGREGATE_TOTAL,
    GRAIN_APPS_BY_CATEGORY,
    GRAIN_APPS_BY_CLASS,
    GRAIN_TOP_APP_PER_ISP,
    GRAIN_TOP_USERS,
    GRAIN_USAGE_BY_ISP,
    Intent,
    apply_referee_decision,
)
from app.llama_client import LlamaError, LlamaServer

_ALLOWED_GRAINS = (
    GRAIN_AGGREGATE_TOTAL,
    GRAIN_TOP_USERS,
    GRAIN_USAGE_BY_ISP,
    GRAIN_TOP_APP_PER_ISP,
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
                '{"action":"accept"|"revise"|"clarify"|"reject","grain":"...","measure":"flows|total_mb","clarify":"..."} . '
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
        return apply_referee_decision(intent, {"action": "accept", "grain": intent.grain, "measure": intent.measure})
    return apply_referee_decision(intent, decision)


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
