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
) -> list[dict]:
    blocks = [f"Question:\n{question.strip()}", f"Label hints:\n{hints}", "Similar questions:"]
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


def explain_messages(question: str, sql: str, columns: list[str], rows: list[dict], truncated: bool) -> list[dict]:
    preview = rows[:40]
    note = "The result has more rows than are shown here." if truncated or len(rows) > 40 else "This is the full result."
    body = (
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
