"""Local web API. SQL is generated on this machine, then run against MySQL read-only."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.app_map import AppMap
from app.config import Config, load_config
from app.db import Database, DatabaseError
from app.llama_client import LlamaError, LlamaServer
from app.present import present_rows
from app.prompts import explain_messages, sql_messages
from app.retrieve import ExampleIndex, load_examples
from app.schema import DEAD_COLUMNS, STARTER_QUESTIONS
from app.sql_guard import GuardError, prepare, wants_full_table

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "static" / "index.html"

app = FastAPI(title="IPDR assistant")
_state: dict = {}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


@app.on_event("startup")
def startup() -> None:
    config = load_config()
    examples = load_examples(config.examples_jsonl)
    mapping = AppMap([])
    mapping_error = None
    if config.mapping_csv.exists():
        mapping = AppMap.load(config.mapping_csv)
    else:
        mapping_error = f"Mapping file not found: {config.mapping_csv}"
    _state["config"] = config
    _state["index"] = ExampleIndex(examples)
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
    return FileResponse(PAGE)


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

    full_scan = wants_full_table(question)
    examples = [item for item in index.top(question, config.example_count + 6) if item.get("sql") or _asks_dead_column(question)]
    examples = examples[: config.example_count]
    hints = mapping.hints(question)
    error = None
    previous_sql = None
    for _attempt in range(2):
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
            if full_scan:
                error = (
                    "Return SQL for the whole table. Traffic volume is the sum of bytes grouped by app_label. "
                    "Do not mention data_center and do not ask for a time range."
                )
                previous_sql = clarify
                continue
            return {"ok": True, "clarify": clarify, "sql": None, "columns": [], "rows": [], "explanation": clarify, "warning": None}
        if not sql:
            error = "The model returned neither sql nor clarify."
            previous_sql = raw[:800]
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
        try:
            explanation = llama.chat(
                explain_messages(question, safe_sql, columns, shown, len(shown) >= config.max_rows),
                json_mode=False,
                temperature=0.2,
            )
        except LlamaError as exc:
            explanation = f"The query ran, but the explanation step failed: {exc}"
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
        "sql": previous_sql,
        "columns": [],
        "rows": [],
        "explanation": None,
        "warning": None,
        "error": error or "The question could not be turned into a safe query.",
    }


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
