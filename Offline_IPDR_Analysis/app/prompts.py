"""Prompt assembly for the SQL step and the explanation step."""

from __future__ import annotations

from app.schema import EXPLAIN_SYSTEM, system_prompt


def sql_messages(
    question: str,
    examples: list[dict],
    hints: str,
    session_time_zone: str,
    error: str | None = None,
    previous_sql: str | None = None,
    full_scan: bool = False,
    history: list[dict] | None = None,
    missing_time_means_all: bool = False,
) -> list[dict]:
    blocks = []
    if history:
        answering = not str(history[-1].get("sql") or "").strip() and str(history[-1].get("explanation") or "").strip()
        turns = []
        for item in history[-6:]:
            turns.append(
                f"Q: {item.get('question', '')}\n"
                f"SQL: {(item.get('sql') or '(none)')[:700]}"
            )
        if answering:
            lead = "The current message answers the clarification. Apply that choice to the last SQL that ran. Do not ask again.\n"
        else:
            lead = (
                "This is a follow-up. Edit the previous SQL. Keep its GROUP BY column, app filter, and time scope.\n"
                "Change only what the user just asked to change. Do not copy an unrelated example over it.\n"
                "Users means subscriber_id. A subscriber_id containing '_' is client IPv4 and port, not a username.\n"
                "Mobile users means a valid subscriber_id with mcc, mnc, lac, cid, or usr_loc_info filled. "
                "Fixed-line means a valid subscriber_id with those cell fields empty. "
                "Do not use app_label or LIKE '%mobile%' for that. "
                "Do not add a cell filter unless this question asks for mobile or fixed-line.\n"
                "Keep the previous application filter. Change the columns, sort, or limit the user named. "
                "Do not ask whether to keep the application when they only change the measure.\n"
                "If they ask which applications or activities one subscriber used, including 'the rest' and 'this user', GROUP BY app_label and filter that subscriber_id. Show every application. Do not exclude the previous one. Do not ask which application or which user when the subscriber is already known.\n"
                "Websites, domains, and SNI mean GROUP BY requested_server for that same subscriber. Do not repeat the application list.\n"
            )
        blocks.append(lead + "\n\n".join(turns))
    hint_text = hints if len(hints) <= 400 else hints[:400] + "..."
    blocks.extend([f"Question:\n{question.strip()}", f"Label hints:\n{hint_text}", "Similar questions:"])
    if full_scan:
        blocks.append(
            "The user explicitly asked for the whole table and told you not to apply a time filter. "
            "Return SQL with no start_time, end_time, or recv_time predicate. "
            "Traffic volume means SUM(c2s_byte_num + s2c_byte_num). "
            "If they asked for users or subscribers, GROUP BY subscriber_id and exclude ids containing '_'. "
            "If they asked for applications, GROUP BY app_label. An app name in the question is a WHERE filter. "
            "Do not mention data_center. Do not ask for a time range. "
            "If they want the applications or activities of a known subscriber, return that SQL. Do not ask which application or which user. "
            "If you are unsure whether to keep a previous filter, return a clarify question."
        )
    if not examples:
        blocks.append("(none)")
    for item in examples:
        if item.get("sql"):
            blocks.append(f"Q: {item['question']}\nSQL: {item['sql']}")
        else:
            blocks.append(f"Q: {item['question']}\nClarify: {item.get('clarify', '')}")
    if error:
        blocks.append(
            "The previous answer was rejected. Return corrected JSON only.\n"
            f"Error: {error}\n"
            f"Previous SQL: {previous_sql or ''}"
        )
    return [
        {"role": "system", "content": system_prompt(session_time_zone, missing_time_means_all)},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


def explain_messages(
    question: str,
    sql: str,
    columns: list[str],
    rows: list[dict],
    truncated: bool,
    history: list[dict] | None = None,
) -> list[dict]:
    preview = rows[:40]
    note = "The result has more rows than are shown here." if truncated or len(rows) > 40 else "This is the full result."
    prior = ""
    if history:
        last = history[-1]
        prior = f"Previous question: {last.get('question', '')}\nPrevious answer: {(last.get('explanation') or '')[:400]}\n\n"
    body = (
        f"{prior}"
        f"Question:\n{question.strip()}\n\n"
        f"SQL:\n{sql}\n\n"
        f"Columns: {', '.join(columns) if columns else '(none)'}\n"
        f"Row count returned to the app: {len(rows)}\n"
        f"{note}\n"
        f"Rows:\n{_rows_as_text(preview)}"
    )
    return [
        {"role": "system", "content": EXPLAIN_SYSTEM},
        {"role": "user", "content": body},
    ]


def _rows_as_text(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    lines = []
    for index, row in enumerate(rows, start=1):
        parts = [f"{key}={value}" for key, value in row.items()]
        lines.append(f"{index}. " + " | ".join(parts))
    return "\n".join(lines)
