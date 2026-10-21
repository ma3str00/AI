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
) -> list[dict]:
    blocks = []
    if history:
        turns = []
        for item in history[-3:]:
            turns.append(
                f"Q: {item.get('question', '')}\n"
                f"SQL: {item.get('sql') or '(none)'}\n"
                f"Answer: {(item.get('explanation') or '')[:500]}"
            )
        blocks.append(
            "This is a follow-up. Keep every filter from the previous SQL unless the user changes it.\n"
            + "\n\n".join(turns)
        )
    blocks.extend([f"Question:\n{question.strip()}", f"Label hints:\n{hints}", "Similar questions:"])
    if full_scan:
        blocks.append(
            "The user explicitly asked for the whole table and told you not to apply a time filter. "
            "Return SQL with no start_time, end_time, or recv_time predicate. "
            "Traffic volume means SUM(c2s_byte_num + s2c_byte_num). Applications means app_label. "
            "Do not mention data_center. Do not return a clarify answer."
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
        {"role": "system", "content": system_prompt(session_time_zone)},
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
