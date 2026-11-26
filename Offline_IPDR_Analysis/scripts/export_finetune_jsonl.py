#!/usr/bin/env python3
"""Export IPDR question/SQL pairs into chat JSONL for QLoRA fine-tuning.

Reads training_examples.jsonl and learned_examples.jsonl (if present), keeps
rows that have SQL, and writes messages suitable for Unsloth / Hugging Face.

Usage (from Offline_IPDR_Analysis):

  python scripts/export_finetune_jsonl.py
  python scripts/export_finetune_jsonl.py --out data/finetune_ipdr.jsonl --min-sql 1
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Stable system text for fine-tune (mirrors app/schema.py core rules).
SYSTEM = """Write one SELECT for ipdr.ntc_ssl_log (ClickHouse; MySQL-style functions IFNULL, LOCATE, GROUP_CONCAT, UNIX_TIMESTAMP are accepted and translated).
Session display offset is +05:00. Timestamps are unix seconds. UNIX_TIMESTAMP('YYYY-MM-DD HH:MM:SS') uses GMT+5.
Use start_time for when a flow happened. Bytes: c2s_byte_num is upload, s2c_byte_num is download, total is their sum. MB is / 1048576.
Traffic volume means that byte total. Applications means GROUP BY app_label. Users or subscribers means GROUP BY subscriber_id.
Exclude unidentified ids with AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0.
If the question names an app and asks for users, filter that app with LOWER(app_label) LIKE '%name%' and still group by subscriber_id.
Prefer WHERE start_time. If the user gives no time range, clarify and ask for one unless they explicitly want all data.
Aggregate unless they ask to list flows. Rankings need ORDER BY and LIMIT. No SELECT *, JOIN, UNION, or other tables.
Return only {"sql":"SELECT ..."} or {"clarify":"one sentence"}."""


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def user_content(question: str) -> str:
    return (
        f"Question:\n{question.strip()}\n\n"
        "Label hints:\n(none)\n\n"
        "Similar questions:\n(none)"
    )


def assistant_content(row: dict) -> str | None:
    sql = (row.get("sql") or "").strip()
    clarify = (row.get("clarify") or "").strip()
    if sql:
        return json.dumps({"sql": sql}, ensure_ascii=False)
    if clarify:
        return json.dumps({"clarify": clarify}, ensure_ascii=False)
    return None


def to_messages(row: dict) -> dict | None:
    question = (row.get("question") or "").strip()
    if not question:
        return None
    assistant = assistant_content(row)
    if not assistant:
        return None
    return {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_content(question)},
            {"role": "assistant", "content": assistant},
        ],
        "meta": {
            "tags": row.get("tags") or [],
            "learned": bool(row.get("learned")),
            "full_scan": bool(row.get("full_scan")),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--training",
        type=Path,
        default=ROOT / "data" / "training_examples.jsonl",
    )
    parser.add_argument(
        "--learned",
        type=Path,
        default=ROOT / "data" / "learned_examples.jsonl",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "finetune_ipdr.jsonl",
    )
    parser.add_argument(
        "--sql-only",
        action="store_true",
        default=True,
        help="Keep only rows with SQL (default).",
    )
    parser.add_argument(
        "--include-clarify",
        action="store_true",
        help="Also keep clarify-only rows.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--val-out",
        type=Path,
        default=None,
        help="Optional validation split path (10%% holdout).",
    )
    args = parser.parse_args()

    raw = load_jsonl(args.training) + load_jsonl(args.learned)
    seen: set[str] = set()
    records: list[dict] = []
    for row in raw:
        if args.sql_only and not args.include_clarify and not (row.get("sql") or "").strip():
            continue
        item = to_messages(row)
        if not item:
            continue
        key = item["messages"][1]["content"] + "\n" + item["messages"][2]["content"]
        if key in seen:
            continue
        seen.add(key)
        records.append(item)

    random.Random(args.seed).shuffle(records)
    val: list[dict] = []
    train = records
    if args.val_out and len(records) >= 20:
        n_val = max(1, len(records) // 10)
        val = records[:n_val]
        train = records[n_val:]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for item in train:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    if args.val_out:
        args.val_out.parent.mkdir(parents=True, exist_ok=True)
        with args.val_out.open("w", encoding="utf-8") as handle:
            for item in val:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    sql_n = sum(1 for r in train if '"sql"' in r["messages"][2]["content"])
    print(f"wrote {len(train)} train rows -> {args.out} (sql~{sql_n})")
    if args.val_out:
        print(f"wrote {len(val)} val rows -> {args.val_out}")


if __name__ == "__main__":
    main()
