"""Check the example bank and the query guard. No database required."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.present import present_rows
from app.sql_guard import GuardError, prepare
from app.config import Config


def main() -> None:
    path = ROOT / "data" / "training_examples.jsonl"
    examples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(examples) < 300:
        raise SystemExit(f"expected at least 300 examples, got {len(examples)}")
    sql_count = 0
    for item in examples:
        if not item.get("sql"):
            if not item.get("clarify"):
                raise SystemExit(f"example has neither sql nor clarify: {item['question']}")
            continue
        sql_count += 1
        try:
            prepare(item["sql"], max_rows=200)
        except GuardError as exc:
            raise SystemExit(f"guard rejected training SQL:\n{item['question']}\n{item['sql']}\n{exc}") from exc
    rejected = [
        "DROP TABLE ntc_ssl_logs",
        "SELECT * FROM ntc_ssl_logs",
        "SELECT * FROM ntc_ssl_logs WHERE client_ip = '203.0.113.10'",
        "SELECT SLEEP(10) FROM ntc_ssl_logs WHERE start_time > 1",
        "SELECT * FROM ntc_ssl_logs WHERE start_time > 1; SELECT * FROM ntc_ssl_logs",
        "SELECT * FROM other_table WHERE start_time > 1",
        "SELECT * FROM ntc_ssl_logs WHERE start_time > 1 INTO OUTFILE '/tmp/x'",
    ]
    for sql in rejected:
        try:
            prepare(sql, max_rows=200)
        except GuardError:
            continue
        raise SystemExit(f"guard allowed unsafe SQL: {sql}")
    allowed = prepare(
        "SELECT app_label, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE start_time >= UNIX_TIMESTAMP(CURDATE()) GROUP BY app_label ORDER BY flows DESC",
        max_rows=200,
    )
    if "LIMIT 200" not in allowed:
        raise SystemExit(f"limit was not applied:\n{allowed}")
    capped = prepare(
        "SELECT id FROM ntc_ssl_logs WHERE start_time >= UNIX_TIMESTAMP(CURDATE()) LIMIT 5000",
        max_rows=200,
    )
    if "5000" in capped:
        raise SystemExit(f"limit was not capped:\n{capped}")

    config = Config(
        mysql_host="127.0.0.1",
        mysql_port=3306,
        mysql_user="x",
        mysql_password="",
        mysql_database="ipdr",
        llama_base_url="http://127.0.0.1:8080",
        llama_model="qwen2.5-coder-3b-instruct-q4_k_m.gguf",
        llama_timeout=30,
        llama_sql_max_tokens=512,
        llama_explain_max_tokens=400,
        example_count=4,
        server_host="127.0.0.1",
        server_port=8090,
        stored_as="utc_epoch",
        display_offset="+05:00",
        max_rows=200,
        max_execution_ms=1000,
        mapping_csv=path,
        examples_jsonl=path,
    )
    shown = present_rows(["start_time"], [{"start_time": 1790604000}], config)
    if shown[0]["start_time"] != "2026-09-28 19:00:00":
        raise SystemExit(f"GMT+5 conversion failed: {shown}")
    print(f"ok examples={len(examples)} sql={sql_count} clarify={len(examples) - sql_count}")


if __name__ == "__main__":
    main()
