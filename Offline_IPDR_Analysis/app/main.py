"""Local web API. SQL is generated on this machine, then run against MySQL read-only."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.answer_check import check_sql, needs_clarification
from app.app_map import AppMap
from app.config import Config, load_config
from app.db import Database, DatabaseError
from app.explain_result import explain_rows
from app.followup import resolve_followup, use_full_scan
from app.learned import saved_sql, sql_needs_full_scan
from app.preferences import (
    load_preferences,
    missing_time_means_all,
    remember_time_rule,
    rule_only,
    teaches_missing_time_means_all,
)
from app.llama_client import LlamaError, LlamaServer
from app.present import present_rows
from app.prompts import sql_messages
from app.retrieve import ExampleIndex, load_examples
from app.rules import dictionary_problem, repair_sql
from app.schema import DEAD_COLUMNS, STARTER_QUESTIONS
from app.sql_guard import GuardError, prepare, wants_full_table

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "static" / "index.html"

app = FastAPI(title="IPDR assistant")
_state: dict = {}


class Turn(BaseModel):
    question: str = Field(default="", max_length=4000)
    sql: str | None = Field(default=None, max_length=8000)
    explanation: str | None = Field(default=None, max_length=2000)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[Turn] = Field(default_factory=list, max_length=6)


class LearnRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    sql: str = Field(min_length=1, max_length=8000)
    questions: list[str] = Field(default_factory=list, max_length=8)


@app.on_event("startup")
def startup() -> None:
    config = load_config()
    mapping = AppMap([])
    mapping_error = None
    if config.mapping_csv.exists():
        mapping = AppMap.load(config.mapping_csv)
    else:
        mapping_error = f"Mapping file not found: {config.mapping_csv}"
    _state["config"] = config
    _state["learn_lock"] = threading.Lock()
    _reload_index()
    _state["mapping"] = mapping
    _state["mapping_error"] = mapping_error
    _state["db"] = Database(config)
    _state["llama"] = LlamaServer(
        config.llama_base_url,
        config.llama_model,
        config.llama_timeout,
        config.llama_sql_max_tokens,
        config.llama_explain_max_tokens,
    )


@app.get("/")
def page():
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    config: Config = _state["config"]
    database: Database = _state["db"]
    llama: LlamaServer = _state["llama"]
    mysql_ok, mysql_detail = database.ping()
    model_ok, model_detail = llama.healthy()
    return {
        "mysql": {"ok": mysql_ok, "detail": mysql_detail, "host": config.mysql_host, "database": config.mysql_database},
        "llama": {"ok": model_ok, "detail": model_detail, "model": config.llama_model, "base_url": config.llama_base_url},
        "examples": len(_state["index"].examples),
        "learned": _state.get("learned_count", 0),
        "missing_time": "all" if missing_time_means_all() else "ask",
        "labels": len(_state["mapping"].rows),
        "mapping_error": _state["mapping_error"],
        "time": {"stored_as": config.stored_as, "note": config.time_note, "session_time_zone": config.session_time_zone},
    }


@app.get("/api/starters")
def starters():
    return {"questions": STARTER_QUESTIONS}


@app.post("/api/ask")
def ask(body: Question):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Enter a question.")
    config: Config = _state["config"]
    index: ExampleIndex = _state["index"]
    mapping: AppMap = _state["mapping"]
    database: Database = _state["db"]
    llama: LlamaServer = _state["llama"]

    history = _history_payload(body.history)
    _remember_time_rule([question, *[str(item.get("question") or "") for item in history]])
    if rule_only(question):
        return _clarify(
            "Saved. If a question does not name a time range, the query runs on all of the data. It will not ask for one again."
        )
    saved = saved_sql(_state["index"].examples, question)
    if saved:
        replayed = _replay_saved(saved, config, database)
        if replayed is not None:
            return replayed
    resolved = resolve_followup(question, history)
    if resolved:
        resolved = repair_sql(question, resolved, history)
        replayed = _replay_saved(
            {"sql": resolved, "full_scan": conversation_full_scan(question, history)},
            config,
            database,
            "This changes the previous query. Mobile means the cell fields are filled. Fixed-line means a valid subscriber id with those fields empty.",
        )
        if replayed is not None:
            return replayed
    clarification = needs_clarification(question, history)
    if clarification:
        return _clarify(clarification)
    full_scan = use_full_scan(question, history, missing_time_means_all())
    examples = _select_examples(index, question, history, full_scan)
    hints = mapping.hints(question)
    error = None
    previous_sql = None
    for _attempt in range(3):
        try:
            raw = llama.chat(
                sql_messages(
                    question,
                    examples,
                    hints,
                    config.session_time_zone,
                    error,
                    previous_sql,
                    full_scan=full_scan,
                    history=history,
                ),
                json_mode=True,
                temperature=0,
            )
        except LlamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            parsed = _parse_json(raw)
        except ValueError as exc:
            error = str(exc)
            previous_sql = raw[:800]
            continue
        clarify = (parsed.get("clarify") or "").strip()
        sql = (parsed.get("sql") or "").strip()
        if clarify and not sql:
            if full_scan and _is_time_question(clarify):
                error = (
                    "Return SQL for the whole table and do not ask for a time range. "
                    "Users means GROUP BY subscriber_id with LOCATE('_', subscriber_id) = 0. "
                    "Applications means GROUP BY app_label. Keep any app name as a WHERE filter."
                )
                previous_sql = clarify
                continue
            return _clarify(clarify)
        if not sql:
            error = "The model returned neither sql nor clarify."
            previous_sql = raw[:800]
            continue
        sql = repair_sql(question, sql, history)
        mismatch = check_sql(question, sql, history, full_scan) or dictionary_problem(question, sql)
        if mismatch:
            error = mismatch
            previous_sql = sql
            continue
        try:
            safe_sql = prepare(sql, config.max_rows, allow_full_scan=full_scan)
        except GuardError as exc:
            error = str(exc)
            previous_sql = sql
            continue
        try:
            columns, rows, warning = database.query(
                safe_sql,
                max_execution_ms=config.full_scan_execution_ms if full_scan else None,
            )
        except DatabaseError as exc:
            error = str(exc)
            previous_sql = safe_sql
            continue
        shown = present_rows(columns, rows, config)
        if full_scan:
            scan_note = "This query reads the whole table because you asked for no time filter. On a large table it can take several minutes."
            warning = f"{scan_note} {warning}" if warning else scan_note
        explanation = explain_rows(columns, shown)
        return {
            "ok": True,
            "clarify": None,
            "sql": safe_sql,
            "columns": columns,
            "rows": shown,
            "explanation": explanation,
            "warning": warning,
            "row_count": len(shown),
        }
    return {
        "ok": False,
        "clarify": None,
        "sql": None,
        "columns": [],
        "rows": [],
        "explanation": None,
        "warning": None,
        "error": "I could not turn that into a query. Say whether to keep the previous application, or start a new question.",
    }


@app.post("/api/learn")
def learn(body: LearnRequest):
    config: Config = _state["config"]
    questions = []
    for text in [body.question, *body.questions]:
        cleaned = text.strip()
        if cleaned and cleaned.lower() not in {item.lower() for item in questions}:
            questions.append(cleaned)
    _remember_time_rule(questions)
    full_scan = sql_needs_full_scan(body.sql) or any(wants_full_table(text) for text in questions)
    try:
        safe_sql = prepare(body.sql, config.max_rows, allow_full_scan=full_scan)
    except GuardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _state["learn_lock"]:
        for text in questions:
            record = {
                "question": text,
                "sql": safe_sql,
                "clarify": None,
                "tags": ["learned"],
                "learned": True,
                "full_scan": full_scan or sql_needs_full_scan(safe_sql),
            }
            _write_learned(config.learned_jsonl, record)
        _reload_index()
    return {"ok": True, "learned": _state["learned_count"], "examples": len(_state["index"].examples)}


def _replay_saved(saved: dict, config: Config, database: Database, notice: str | None = None) -> dict | None:
    full_scan = bool(saved.get("full_scan")) or sql_needs_full_scan(str(saved.get("sql") or ""))
    try:
        safe_sql = prepare(str(saved["sql"]), config.max_rows, allow_full_scan=full_scan)
    except GuardError:
        return None
    try:
        columns, rows, warning = database.query(
            safe_sql,
            max_execution_ms=config.full_scan_execution_ms if full_scan else None,
        )
    except DatabaseError as exc:
        return {
            "ok": False,
            "clarify": None,
            "sql": safe_sql,
            "columns": [],
            "rows": [],
            "explanation": None,
            "warning": None,
            "error": str(exc),
        }
    shown = present_rows(columns, rows, config)
    if full_scan:
        scan_note = notice or "This is the query you marked correct. It reads the whole table and can take several minutes."
        warning = f"{scan_note} {warning}" if warning else scan_note
    return {
        "ok": True,
        "clarify": None,
        "sql": safe_sql,
        "columns": columns,
        "rows": shown,
        "explanation": explain_rows(columns, shown),
        "warning": warning,
        "row_count": len(shown),
    }


def _reload_index() -> None:
    config: Config = _state["config"]
    examples = load_examples(config.examples_jsonl)
    learned = load_examples(config.learned_jsonl) if config.learned_jsonl.exists() else []
    _state["learned_count"] = len(learned)
    _state["index"] = ExampleIndex(examples + learned)
    load_preferences(_preferences_path())
    _remember_time_rule([str(item.get("question") or "") for item in learned])


def _preferences_path() -> Path:
    config: Config = _state["config"]
    return config.learned_jsonl.parent / "learned_preferences.json"


def _remember_time_rule(texts: list[str]) -> None:
    if any(teaches_missing_time_means_all(text) for text in texts):
        remember_time_rule(_preferences_path(), texts)


def _write_learned(path: Path, record: dict) -> None:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    key = record["question"].strip().lower()
    rows = [row for row in rows if row.get("question", "").strip().lower() != key]
    rows.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _clarify(text: str) -> dict:
    return {
        "ok": True,
        "clarify": text,
        "sql": None,
        "columns": [],
        "rows": [],
        "explanation": text,
        "warning": None,
    }


def _is_time_question(text: str) -> bool:
    folded = text.lower()
    return "time range" in folded or "time filter" in folded or "time period" in folded


def _history_payload(history: list[Turn]) -> list[dict]:
    payload = []
    for turn in history[-6:]:
        payload.append(
            {
                "question": turn.question.strip(),
                "sql": (turn.sql or "").strip(),
                "explanation": (turn.explanation or "").strip()[:500],
            }
        )
    return payload


def _select_examples(index: ExampleIndex, question: str, history: list[dict], full_scan: bool) -> list[dict]:
    if any(item.get("sql") for item in history):
        if re.search(r"mobile|fixed", question.lower()):
            picked = [
                item for item in index.top(question, 6)
                if item.get("sql") and "mcc" in item["sql"].lower()
            ]
            return picked[:1]
        return []
    seed = question
    if history:
        seed = " ".join(item.get("question") or "" for item in history) + " " + question
    picked = [item for item in index.top(seed, 8) if item.get("sql") or item.get("clarify")]
    if full_scan:
        picked = [
            item
            for item in picked
            if not (item.get("clarify") and "time range" in str(item.get("clarify")).lower())
            and (item.get("full_scan") or not item.get("sql") or sql_needs_full_scan(str(item.get("sql"))))
        ]
    if re.search(r"\b(users|user|subscribers|subscriber_id|subscriber id)\b", seed.lower()):
        about_users = [
            item
            for item in picked
            if (item.get("sql") and "subscriber_id" in item["sql"].lower())
            or (item.get("clarify") and "subscriber_id" in item.get("clarify", "").lower())
        ]
        if about_users:
            picked = about_users
    return picked[:2]


def _asks_dead_column(question: str) -> bool:
    text = question.lower().replace("_", " ")
    return any(name.replace("_", " ") in text for name in DEAD_COLUMNS)


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("The model did not return JSON.")
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError("The model did not return JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("The model JSON was not an object.")
    return value
