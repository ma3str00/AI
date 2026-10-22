"""Check the example bank and the query guard. No database required."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.present import present_rows
from app.sql_guard import GuardError, prepare, wants_full_table
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
            prepare(item["sql"], max_rows=200, allow_full_scan=bool(item.get("full_scan")))
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
        full_scan_execution_ms=1000,
        mapping_csv=path,
        examples_jsonl=path,
        learned_jsonl=path,
    )
    shown = present_rows(["start_time"], [{"start_time": 1790604000}], config)
    if shown[0]["start_time"] != "2026-09-28 19:00:00":
        raise SystemExit(f"GMT+5 conversion failed: {shown}")
    question = "give me the top 5 applications used by traffic volume. do not apply any time filter. check all the data"
    if not wants_full_table(question):
        raise SystemExit("full-table question was not recognized")
    top5 = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        "FROM ntc_ssl_logs GROUP BY app_label ORDER BY total_bytes DESC LIMIT 5"
    )
    try:
        prepare(top5, max_rows=200, allow_full_scan=False)
    except GuardError:
        pass
    else:
        raise SystemExit("full scan was allowed without an explicit request")
    if "GROUP BY" not in prepare(top5, max_rows=200, allow_full_scan=True).upper():
        raise SystemExit("full scan aggregate was rejected")
    from app.answer_check import check_sql

    history = [{"question": "give me the top 5 whatsapp users by traffic volume", "sql": ""}]
    grouped_by_app = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        "FROM ntc_ssl_logs GROUP BY app_label ORDER BY total_bytes DESC LIMIT 5"
    )
    rejected = check_sql(
        "If the time filter is not specified then the query should be applied on all the data",
        grouped_by_app,
        history,
        True,
    )
    if not rejected or "subscriber_id" not in rejected:
        raise SystemExit(f"app ranking was accepted for a users question: {rejected}")
    whatsapp_users = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 5"
    )
    if check_sql("applied on all the data", whatsapp_users, history, True):
        raise SystemExit("valid WhatsApp subscriber ranking was rejected")
    without_filter = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        "FROM ntc_ssl_logs GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 5"
    )
    rejected = check_sql("By users I meant the top 5 subscriber_id", without_filter, history, True)
    if not rejected or "whatsapp" not in rejected.lower() or "LOCATE" not in rejected:
        raise SystemExit(f"unfiltered subscriber ranking was accepted: {rejected}")
    lost = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
        "WHERE start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 7 DAY) AND app_label = 'ipv6_over_ipv4' "
        "GROUP BY app_label ORDER BY total_bytes DESC LIMIT 5"
    )
    rejected = check_sql(
        "203.0.113.10_56175 is not a valid subscriber ID. Such subscriber IDs should be excluded",
        lost,
        history + [{"question": "By users I meant the top 5 subscriber_id", "sql": whatsapp_users}],
        True,
    )
    if not rejected:
        raise SystemExit("rewritten query was accepted")
    bad_mobile = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%mobile%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND subscriber_id NOT LIKE '%_%' "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 10"
    )
    rejected = check_sql(
        "now give me the top 10 mobile users, exclude the fixedline users",
        bad_mobile,
        [{"question": "give me the top 5 whatsapp users by traffic volume", "sql": whatsapp_users}],
        True,
    )
    if not rejected or "cell" not in rejected.lower() or "whatsapp" not in rejected.lower():
        raise SystemExit(f"mobile app_label query was accepted: {rejected}")
    mobile_sql = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND (NULLIF(mcc, '') IS NOT NULL OR NULLIF(mnc, '') IS NOT NULL "
        "OR NULLIF(lac, '') IS NOT NULL OR NULLIF(cid, '') IS NOT NULL "
        "OR NULLIF(usr_loc_info, '') IS NOT NULL) "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 10"
    )
    if check_sql(
        "now give me the top 10 mobile users, exclude the fixedline users",
        mobile_sql,
        [{"question": "give me the top 5 whatsapp users by traffic volume", "sql": whatsapp_users}],
        True,
    ):
        raise SystemExit("valid mobile subscriber ranking was rejected")
    from app.answer_check import needs_clarification

    asked = needs_clarification(
        "now give me the top 10 mobile users, exclude the fixedline users",
        [{"question": "give me the top 5 whatsapp users by traffic volume", "sql": whatsapp_users}],
    )
    if not asked or "whatsapp" not in asked.lower() or "every application" not in asked.lower():
        raise SystemExit(f"ambiguous follow-up was not questioned: {asked}")
    if needs_clarification("top 10", [{"question": "whatsapp users", "sql": whatsapp_users}]):
        raise SystemExit("a limit change should not ask about the application")
    mobile_all_apps = mobile_sql.replace("LOWER(app_label) LIKE '%whatsapp%' AND ", "")
    if check_sql(
        "include every application",
        mobile_all_apps,
        [{"question": "give me the top 5 whatsapp users by traffic volume", "sql": whatsapp_users}],
        True,
    ):
        raise SystemExit("dropping the app filter was rejected after the user asked for every application")
    from app.learned import saved_sql, sql_needs_full_scan

    learned_sql = (
        "SELECT subscriber_id FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "GROUP BY subscriber_id LIMIT 5"
    )
    if not sql_needs_full_scan(learned_sql):
        raise SystemExit("saved query without a time column was treated as a time-filtered query")
    bank = [
        {"question": "give me the top 5 whatsapp users by traffic volume", "clarify": "Specify a time range.", "sql": None},
        {"question": "give me the top 5 whatsapp users by traffic volume", "sql": learned_sql, "learned": True, "full_scan": True},
    ]
    found = saved_sql(bank, "Give me the top 5 WhatsApp users by traffic volume.")
    if not found or found.get("sql") != learned_sql:
        raise SystemExit("a marked answer was not reused for the same question")
    from app.followup import resolve_followup
    from app.sql_guard import prepare as guard_prepare

    previous = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 5"
    )
    thread = [
        {"question": "give me the top 5 whatsapp users by traffic volume", "sql": ""},
        {"question": "no time filter required when user doesnt specify it", "sql": previous},
        {"question": "now give me the top 10 mobile users", "sql": ""},
    ]
    if resolve_followup("now give me the top 10 mobile users", thread[:2]):
        raise SystemExit("the app-scope question should be asked before a mobile query is run")
    resolved = resolve_followup(
        "should stay limited to whatsapp. Your understanding of mobile user is correct",
        thread,
    )
    if not resolved or "whatsapp" not in resolved.lower() or "mcc" not in resolved.lower() or "LIMIT 10" not in resolved:
        raise SystemExit(f"confirmed mobile follow-up was not applied: {resolved}")
    if "start_time" in resolved.lower():
        raise SystemExit("confirmed follow-up added a time filter")
    guard_prepare(resolved, max_rows=200, allow_full_scan=True)
    later = resolve_followup("all data", thread + [{"question": "should stay limited to whatsapp", "sql": ""}])
    if not later or "mcc" not in later.lower() or " and not " in later.lower():
        raise SystemExit("all data did not rerun the confirmed mobile query")
    fixed = resolve_followup(
        "now give me the top 10 fixed-line users for the same",
        thread + [{"question": "now give me the top 10 mobile users for the same", "sql": resolved}],
    )
    if not fixed or " and not " not in fixed.lower() or fixed.lower().count("nullif(mcc") != 1:
        raise SystemExit(f"fixed-line follow-up kept the mobile filter: {fixed}")
    if "whatsapp" not in fixed.lower() or "LIMIT 10" not in fixed:
        raise SystemExit(f"fixed-line follow-up dropped the previous query: {fixed}")
    guard_prepare(fixed, max_rows=200, allow_full_scan=True)
    from app.rules import repair_sql

    copied = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%mobile%' "
        "AND (NULLIF(mcc, '') IS NOT NULL OR NULLIF(mnc, '') IS NOT NULL "
        "OR NULLIF(lac, '') IS NOT NULL OR NULLIF(cid, '') IS NOT NULL "
        "OR NULLIF(usr_loc_info, '') IS NOT NULL) "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 10"
    )
    repaired = repair_sql(
        "now give me the top 10 fixed-line users for the same",
        copied,
        [{"question": "top 5 whatsapp users", "sql": previous}],
    )
    if "like '%mobile%'" in repaired.lower() or " and not " not in repaired.lower():
        raise SystemExit(f"dictionary repair left the mobile query in place: {repaired}")
    from app.followup import use_full_scan
    from app.preferences import lesson_from_marked_answer, missing_time_means_all

    whole_table = (
        "SELECT subscriber_id FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "GROUP BY subscriber_id LIMIT 5"
    )
    if not lesson_from_marked_answer(["give me the top 5 whatsapp users by traffic volume"], whole_table):
        raise SystemExit("a marked whole-table answer did not teach the time rule")
    if lesson_from_marked_answer(["top 5 whatsapp users yesterday"], whole_table):
        raise SystemExit("a marked yesterday question was treated as the time rule")
    if lesson_from_marked_answer(
        ["give me the top 5 whatsapp users by traffic volume"],
        "SELECT subscriber_id FROM ntc_ssl_logs WHERE start_time >= 1 GROUP BY subscriber_id LIMIT 5",
    ):
        raise SystemExit("a marked time filter was treated as the time rule")
    if missing_time_means_all():
        raise SystemExit("the time rule was on before any answer was marked")
    if use_full_scan("give me the top 5 whatsapp users by traffic volume", [], False):
        raise SystemExit("a new question scanned the whole table before the rule was learned")
    if not use_full_scan("give me the top 5 whatsapp users by traffic volume", [], True):
        raise SystemExit("a learned rule did not run a new question on all of the data")
    if use_full_scan("top 5 whatsapp users yesterday", [], True):
        raise SystemExit("yesterday was widened to the whole table")
    timed = [{"question": "top apps yesterday", "sql": "SELECT app_label FROM ntc_ssl_logs WHERE start_time >= 1 GROUP BY app_label"}]
    if use_full_scan("top 10 mobile users for the same", timed, True):
        raise SystemExit("a follow-up dropped the previous time filter")
    youtube = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) IN ('youtube') "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND (NULLIF(mcc, '') IS NOT NULL OR NULLIF(mnc, '') IS NOT NULL "
        "OR NULLIF(lac, '') IS NOT NULL OR NULLIF(cid, '') IS NOT NULL "
        "OR NULLIF(usr_loc_info, '') IS NOT NULL) "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 20"
    )
    plain = repair_sql("give me top 20 youtube users", youtube, [])
    if "nullif(mcc" in plain.lower() or "youtube" not in plain.lower():
        raise SystemExit(f"youtube users kept the mobile filter: {plain}")
    complaint = "you gave mobile users only. I need to see the top 20 users regardless of their nature of connection"
    if needs_clarification(complaint, [{"question": "give me top 20 youtube users", "sql": youtube}]):
        raise SystemExit("a connection complaint asked about the application")
    opened = resolve_followup(complaint, [{"question": "give me top 20 youtube users", "sql": youtube}])
    if not opened or "nullif(mcc" in opened.lower() or "youtube" not in opened.lower():
        raise SystemExit(f"connection complaint did not drop the cell filter: {opened}")
    split_q = "now show me the upload and download data separately for each user"
    if needs_clarification(split_q, [{"question": "give me top 20 youtube users", "sql": opened}]):
        raise SystemExit("an upload and download follow-up asked about the application")
    split = resolve_followup(split_q, [{"question": "give me top 20 youtube users", "sql": opened}])
    if not split or "upload_bytes" not in split.lower() or "download_bytes" not in split.lower() or "youtube" not in split.lower():
        raise SystemExit(f"upload and download were not split: {split}")
    flow_q = "now instead of showing the top 20 users by traffic volume, show me the top 20 users by flow count"
    if needs_clarification(flow_q, [{"question": split_q, "sql": split}]):
        raise SystemExit("a flow-count follow-up asked about the application")
    ranked = resolve_followup(flow_q, [{"question": split_q, "sql": split}])
    if not ranked or "order by flows" not in ranked.lower() or "upload_bytes" not in ranked.lower():
        raise SystemExit(f"flow count did not replace the sort: {ranked}")
    affirmed = resolve_followup(
        "yes",
        [
            {"question": "give me top 20 youtube users", "sql": opened},
            {"question": split_q, "sql": "", "explanation": "Do you want to keep the previous application filter?"},
        ],
    )
    if not affirmed or "upload_bytes" not in affirmed.lower() or "youtube" not in affirmed.lower():
        raise SystemExit(f"yes did not keep the application and apply the metric: {affirmed}")
    facebook = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%facebook%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 20"
    )
    by_flows = resolve_followup(
        "now give me the top 20 by number of flows",
        [{"question": "give me top 20 facebook users", "sql": facebook}],
    )
    if not by_flows or "order by flows" not in by_flows.lower() or "facebook" not in by_flows.lower():
        raise SystemExit(f"flow ranking did not keep facebook: {by_flows}")
    others = resolve_followup(
        "what other applications are being used by malikmubeen201",
        [{"question": "now give me the top 20 by number of flows", "sql": by_flows}],
    )
    if (
        not others
        or "group by app_label" not in others.lower()
        or "malikmubeen201" not in others.lower()
        or " not " in others.lower()
    ):
        raise SystemExit(f"applications for one user were filtered: {others}")
    if check_sql(
        "what other applications are being used by malikmubeen201",
        others,
        [{"question": "give me top 20 facebook users", "sql": facebook}],
        True,
    ):
        raise SystemExit("applications for one subscriber were rejected")
    from app.explain_result import explain_rows

    told = explain_rows(
        ["subscriber_id", "total_bytes", "flows"],
        [
            {"subscriber_id": "a", "total_bytes": 10, "flows": 7},
            {"subscriber_id": "b", "total_bytes": 99, "flows": 2},
        ],
        by_flows,
    )
    if "ranked by flows" not in told.lower():
        raise SystemExit(f"flow ranking was described as bytes: {told}")
    apps = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs GROUP BY app_label ORDER BY total_bytes DESC LIMIT 10"
    )
    split = resolve_followup(
        "now give me the corresponding upload and download traffic volume along with the flows",
        [{"question": "top applications", "sql": apps}],
    )
    if (
        not split
        or "sum(c2s_byte_num) as upload_traffic" not in split.lower()
        or "sum(s2c_byte_num) as download_traffic" not in split.lower()
        or "count(*) as flow_count" not in split.lower()
        or "c2s_byte_num + s2c_byte_num" in split.lower()
    ):
        raise SystemExit(f"upload and download were combined: {split}")
    kept = resolve_followup(
        "I want to see the upload and download traffic volume separately",
        [{"question": "flows", "sql": split}],
    )
    if not kept or "flow_count" not in kept.lower():
        raise SystemExit(f"splitting upload and download dropped the flows: {kept}")
    terabytes = resolve_followup(
        "show the volume in Terabytes",
        [{"question": "separately", "sql": kept}],
    )
    if not terabytes or "/ 1099511627776" not in terabytes or "upload_traffic_tb" not in terabytes.lower() or "flow_count" not in terabytes.lower():
        raise SystemExit(f"terabytes used the wrong divisor: {terabytes}")
    if "/ 1073741824" in terabytes or "/ 1048576" in terabytes:
        raise SystemExit(f"terabytes was calculated as gigabytes or megabytes: {terabytes}")
    gigabytes = resolve_followup(
        "the upload & download should also be displayed in Gigabytes",
        [{"question": "terabytes", "sql": terabytes}],
    )
    if not gigabytes or "/ 1073741824" not in gigabytes or "upload_traffic_gb" not in gigabytes.lower() or "flow_count" not in gigabytes.lower():
        raise SystemExit(f"gigabytes used the wrong divisor or dropped flows: {gigabytes}")
    if "/ 1048576" in gigabytes:
        raise SystemExit(f"gigabytes was calculated as megabytes: {gigabytes}")
    print(f"ok examples={len(examples)} sql={sql_count} clarify={len(examples) - sql_count}")


if __name__ == "__main__":
    main()
