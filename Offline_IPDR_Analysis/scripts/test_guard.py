"""Check the example bank and the query guard. No database required."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.present import present_rows
from app.sql_guard import GuardError, prepare, wants_full_table
from app.config import Config


def main() -> None:
    from app.app_map import AppMap, activate_app_map

    activate_app_map(AppMap.load(ROOT / "data" / "app_category_to_app_label_mapping.csv"))
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
    from app.followup import limit_followup, resolve_followup
    from app.places import mentioned_isps
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
        or ("count(*) as flow_count" not in split.lower() and "count(*) as flows" not in split.lower())
        or "c2s_byte_num + s2c_byte_num" in split.lower()
    ):
        raise SystemExit(f"upload and download were combined: {split}")
    kept = resolve_followup(
        "I want to see the upload and download traffic volume separately",
        [{"question": "flows", "sql": split}],
    )
    if not kept or ("flow_count" not in kept.lower() and " as flows" not in kept.lower()):
        raise SystemExit(f"splitting upload and download dropped the flows: {kept}")
    terabytes = resolve_followup(
        "show the volume in Terabytes",
        [{"question": "separately", "sql": kept}],
    )
    if (
        not terabytes
        or "/ 1099511627776" not in terabytes
        or "upload_traffic_tb" not in terabytes.lower()
        or ("flow_count" not in terabytes.lower() and " as flows" not in terabytes.lower())
    ):
        raise SystemExit(f"terabytes used the wrong divisor: {terabytes}")
    if "/ 1073741824" in terabytes or "/ 1048576" in terabytes:
        raise SystemExit(f"terabytes was calculated as gigabytes or megabytes: {terabytes}")
    gigabytes = resolve_followup(
        "the upload & download should also be displayed in Gigabytes",
        [{"question": "terabytes", "sql": terabytes}],
    )
    if (
        not gigabytes
        or "/ 1073741824" not in gigabytes
        or "upload_traffic_gb" not in gigabytes.lower()
        or ("flow_count" not in gigabytes.lower() and " as flows" not in gigabytes.lower())
    ):
        raise SystemExit(f"gigabytes used the wrong divisor or dropped flows: {gigabytes}")
    if "/ 1048576" in gigabytes:
        raise SystemExit(f"gigabytes was calculated as megabytes: {gigabytes}")
    users = resolve_followup(
        "also show the corresponding number of unique users against each app",
        [{"question": "gigabytes", "sql": gigabytes}],
    )
    if (
        not users
        or "count(distinct case when" not in users.lower()
        or " as users" not in users.lower()
        or ("flow_count" not in users.lower() and " as flows" not in users.lower())
        or "upload_traffic_gb" not in users.lower()
        or "group by app_label" not in users.lower()
    ):
        raise SystemExit(f"unique users were not added beside each application: {users}")
    if check_sql(
        "also show the corresponding number of unique users against each app",
        users,
        [{"question": "top applications", "sql": apps}],
        True,
    ):
        raise SystemExit("a user count per application was rejected")
    from app.sql_guard import prepare as guard_prepare

    guard_prepare(users, max_rows=200, allow_full_scan=True)
    streaming = (
        "SELECT app_label, COUNT(*) AS flow_count FROM ntc_ssl_logs "
        "WHERE LOWER(app_category) IN ('streaming') GROUP BY app_label LIMIT 200"
    )
    counted = resolve_followup(
        "show me the unique number of users against each app",
        [{"question": "streaming", "sql": streaming}],
    )
    ordered_users = resolve_followup(
        "Please also show the upload and download traffic volume against each app, order them by user count",
        [{"question": "users", "sql": counted or streaming}],
    )
    if (
        not ordered_users
        or "order by users desc" not in ordered_users.lower()
        or "sum(c2s_byte_num) as upload_traffic" not in ordered_users.lower()
        or "sum(s2c_byte_num) as download_traffic" not in ordered_users.lower()
    ):
        raise SystemExit(f"ordering by user count was ignored: {ordered_users}")
    unsorted = (
        "SELECT app_label, COUNT(*) AS flow_count, SUM(c2s_byte_num) AS upload_traffic, "
        "SUM(s2c_byte_num) AS download_traffic, COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
        "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users "
        "FROM ntc_ssl_logs WHERE LOWER(app_category) IN ('streaming') GROUP BY app_label LIMIT 200"
    )
    by_users = resolve_followup(
        "show the traffic volumes in gigabytes. You are still showing the data in alphabetical order, I want them ordered by user count",
        [{"question": "volumes", "sql": unsorted}],
    )
    if (
        not by_users
        or "order by users desc" not in by_users.lower()
        or "upload_traffic_gb" not in by_users.lower()
        or "download_traffic_gb" not in by_users.lower()
        or "/ 1073741824" not in by_users
    ):
        raise SystemExit(f"gigabytes stayed alphabetical: {by_users}")
    plain = explain_rows(
        ["app_label", "flow_count"],
        [{"app_label": "AhCDN", "flow_count": 54}, {"app_label": "Anghami", "flow_count": 1}],
        streaming,
    )
    if "ranked by" in plain.lower():
        raise SystemExit(f"an unordered result was described as a ranking: {plain}")
    games = (
        "SELECT app_label, COUNT(*) AS flow_count, "
        "ROUND(SUM(c2s_byte_num) / 1073741824, 2) AS upload_traffic_gb, "
        "ROUND(SUM(s2c_byte_num) / 1073741824, 2) AS download_traffic_gb, "
        "COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
        "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users "
        "FROM ntc_ssl_logs WHERE LOWER(app_category) IN ('game') "
        "GROUP BY app_label ORDER BY users DESC LIMIT 200"
    )
    megabytes = resolve_followup(
        "change the traffic number from GB to MB",
        [{"question": "games", "sql": games}],
    )
    if (
        not megabytes
        or "/ 1048576" not in megabytes
        or "upload_traffic_mb" not in megabytes.lower()
        or "download_traffic_mb" not in megabytes.lower()
        or "/ 1073741824" in megabytes
        or "order by users desc" not in megabytes.lower()
    ):
        raise SystemExit(f"from GB to MB stayed in gigabytes: {megabytes}")
    back = resolve_followup(
        "change it from MB to GB",
        [{"question": "megabytes", "sql": megabytes}],
    )
    if not back or "/ 1073741824" not in back or "upload_traffic_gb" not in back.lower() or "/ 1048576" in back:
        raise SystemExit(f"from MB to GB stayed in megabytes: {back}")
    whatsapp_users = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 10"
    )
    ranked_users = [{"question": "give me the users with highest amount of whatsapp activity", "sql": whatsapp_users}]
    rest = resolve_followup("show me the rest of activities done by htr897713", ranked_users)
    if (
        not rest
        or "htr897713" not in rest
        or "group by app_label" not in rest.lower()
        or "whatsapp" in rest.lower()
        or "limit" in rest.lower()
    ):
        raise SystemExit(f"the rest of a subscriber's activities was not listed: {rest}")
    referred = ranked_users + [
        {"question": "show me the rest of activities done by htr897713", "sql": "", "explanation": "Which view do you want?"},
    ]
    by_this_user = resolve_followup("I want to see the applications used by this user", referred)
    if not by_this_user or "htr897713" not in by_this_user or "group by app_label" not in by_this_user.lower():
        raise SystemExit(f"this user did not keep htr897713: {by_this_user}")
    named_again = referred + [
        {"question": "I want to see the applications used by this user", "sql": "", "explanation": "Which application?"},
        {"question": "all the applications that this user has been using", "sql": "", "explanation": "Which measure?"},
        {"question": "all the applications used by this user", "sql": "", "explanation": "Which user?"},
    ]
    referred_user = resolve_followup("I am referring to user htr897713", named_again)
    if (
        not referred_user
        or "subscriber_id = 'htr897713'" not in referred_user
        or "group by app_label" not in referred_user.lower()
        or "limit" in referred_user.lower()
    ):
        raise SystemExit(f"naming the user again did not list their applications: {referred_user}")
    if check_sql("I am referring to user htr897713", referred_user, named_again, True):
        raise SystemExit("listing one subscriber's applications was rejected")
    listing = (
        "SELECT app_label, COUNT(*) AS flow_count FROM ntc_ssl_logs "
        "WHERE LOWER(app_category) = 'web_browsing' GROUP BY app_label LIMIT 200"
    )
    opened = resolve_followup(
        "you dont need to apply the limit, I need to see all of them",
        [{"question": "list of applications", "sql": listing}],
    )
    if not opened or "limit" in opened.lower():
        raise SystemExit(f"the row limit was kept: {opened}")
    if "web_browsing" not in opened.lower():
        raise SystemExit(f"removing the limit dropped the category: {opened}")
    shown_all = guard_prepare(opened, max_rows=200, allow_full_scan=True, allow_all_groups=True)
    if "limit" in shown_all.lower():
        raise SystemExit(f"the guard put the limit back: {shown_all}")
    twitter_users = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) IN ('twitter_upload', 'twitter') "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 20"
    )
    twitter_history = [{"question": "give me the users with highest amount of twitter activity", "sql": twitter_users}]
    other = resolve_followup("show me the other activities done by 03215340135W", twitter_history)
    if not other or "group by app_label" not in other.lower() or "03215340135W" not in other or "twitter" in other.lower():
        raise SystemExit(f"other activities were not listed for that subscriber: {other}")
    after_apps = twitter_history + [{"question": "show me the other activities done by 03215340135W", "sql": other}]
    websites = resolve_followup("show me the websites accessed by this user", after_apps)
    if (
        not websites
        or "group by requested_server" not in websites.lower()
        or "03215340135W" not in websites
        or "group by app_label" in websites.lower()
    ):
        raise SystemExit(f"websites for this user became an application list: {websites}")
    narrowed = after_apps + [
        {"question": "show me the websites accessed by this user", "sql": "", "explanation": "Which user do you mean?"},
    ]
    just_user = resolve_followup("just by 03215340135W", narrowed)
    if (
        not just_user
        or "group by requested_server" not in just_user.lower()
        or "subscriber_id = '03215340135W'" not in just_user
        or "group by app_label" in just_user.lower()
    ):
        raise SystemExit(f"repeating the subscriber repeated the application list: {just_user}")
    if check_sql("show me the websites accessed by this user", websites, after_apps, True):
        raise SystemExit("a website list for one subscriber was rejected")
    if "app_label" in websites.lower():
        raise SystemExit(f"a website list added an application filter: {websites}")
    http_sites = resolve_followup("show me the websites used by this user against app type HTTP", after_apps)
    if (
        not http_sites
        or "group by requested_server" not in http_sites.lower()
        or "03215340135W" not in http_sites
        or "lower(app_label) = 'http'" not in http_sites.lower()
    ):
        raise SystemExit(f"websites for one application were not filtered: {http_sites}")
    with_label = resolve_followup(
        "show me the websites accessed by 0333319067AQ and app_label",
        [{"question": "show me the activities of 0333319067AQ", "sql": "SELECT app_label FROM ntc_ssl_logs WHERE subscriber_id = '0333319067AQ' GROUP BY app_label"}],
    )
    folded_label = (with_label or "").lower()
    if (
        not with_label
        or "group by requested_server, app_label" not in folded_label
        or "subscriber_id = '0333319067AQ'" not in with_label
        or "group by subscriber_id" in folded_label
    ):
        raise SystemExit(f"websites and app_label dropped the application: {with_label}")
    named_flows = resolve_followup(
        "show me all the flows of 923325034073. Include the website and app names",
        [],
    )
    folded_flows = (named_flows or "").lower()
    if (
        not named_flows
        or "subscriber_id = '923325034073'" not in named_flows
        or "group by requested_server, app_label" not in folded_flows
        or "limit" in folded_flows
    ):
        raise SystemExit(f"flows did not include the website and application: {named_flows}")
    facebook_sites = (
        "SELECT requested_server, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        "FROM ntc_ssl_logs WHERE subscriber_id = '03339169479abhd' AND LOWER(app_label) = 'facebook' "
        "AND IFNULL(requested_server, '') <> '' GROUP BY requested_server ORDER BY total_bytes DESC LIMIT 50"
    )
    same_ssl = resolve_followup(
        "show me the same for app type ssl",
        [{"question": "show me the websites access by the user of app type facebook", "sql": facebook_sites}],
    )
    if (
        not same_ssl
        or "lower(app_label) = 'ssl'" not in same_ssl.lower()
        or "facebook" in same_ssl.lower()
        or "03339169479abhd" not in same_ssl
        or "group by requested_server" not in same_ssl.lower()
    ):
        raise SystemExit(f"the same for another application asked again: {same_ssl}")
    ranking = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 25"
    )
    details = resolve_followup(
        "show me the activity details of 03250040727FF",
        [{"question": "show the top 25 users with maximum activity", "sql": ranking}],
    )
    if (
        not details
        or "group by app_label" not in details.lower()
        or "subscriber_id = '03250040727FF'" not in details
        or "group by subscriber_id" in details.lower()
        or "limit" in details.lower()
    ):
        raise SystemExit(f"activity details repeated the subscriber total: {details}")
    punjab_ranking = (
        "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE NOT (NULLIF(mcc, '') IS NULL) AND (LOWER(loc) LIKE '%punjab%' OR LOWER(loc) LIKE '%lahore%') "
        "GROUP BY subscriber_id ORDER BY flows DESC LIMIT 10"
    )
    against = resolve_followup(
        "show me the activity details against 3499227512",
        [{"question": "show me the top 10 fixedline users by flow count from punjab", "sql": punjab_ranking}],
    )
    folded_against = (against or "").lower()
    if (
        not against
        or "group by app_label" not in folded_against
        or "subscriber_id = '3499227512'" not in against
        or "group by subscriber_id" in folded_against
        or "punjab" in folded_against
        or "limit" in folded_against
    ):
        raise SystemExit(f"activity details against an id repeated the ranking: {against}")
    more = resolve_followup(
        "show me more details about 3499227512",
        [{"question": "show me the top 30 fixedline users by hit count from kpk", "sql": (
            "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
            "WHERE NOT (NULLIF(mcc, '') IS NULL) AND (LOWER(loc) LIKE '%peshawar%') "
            "AND LOWER(client_asn) LIKE '%pakistan telecommunication%' "
            "GROUP BY subscriber_id ORDER BY flows DESC LIMIT 30"
        )}],
    )
    folded_more = (more or "").lower()
    if (
        not more
        or "group by app_label" not in folded_more
        or "subscriber_id = '3499227512'" not in more
        or "group by subscriber_id" in folded_more
        or "peshawar" in folded_more
        or "telecommunication" in folded_more
        or "limit" in folded_more
    ):
        raise SystemExit(f"more details about an id repeated the ranking: {more}")
    details_for = resolve_followup(
        "details for 92305319067A",
        [{"question": "show me the top 30 mobile users by hit count from Punjab", "sql": (
            "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
            "WHERE (NULLIF(mcc, '') IS NOT NULL) AND (LOWER(loc) LIKE '%rawalpindi%') "
            "GROUP BY subscriber_id ORDER BY flows DESC LIMIT 30"
        )}],
    )
    folded_for = (details_for or "").lower()
    if (
        not details_for
        or "group by app_label" not in folded_for
        or "subscriber_id = '92305319067A'" not in details_for
        or "group by subscriber_id" in folded_for
        or "rawalpindi" in folded_for
        or "limit" in folded_for
    ):
        raise SystemExit(f"details for an id repeated the ranking: {details_for}")
    one_apps = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE subscriber_id = '923325034073' GROUP BY app_label ORDER BY total_bytes DESC"
    )
    also_sites = resolve_followup(
        "also show the websites",
        [{"question": "details for 923325034073", "sql": one_apps}],
    )
    folded_sites = (also_sites or "").lower()
    if (
        not also_sites
        or "subscriber_id = '923325034073'" not in also_sites
        or "group by requested_server, app_label" not in folded_sites
        or "group by subscriber_id" in folded_sites
    ):
        raise SystemExit(f"also show the websites left the application list: {also_sites}")
    including = resolve_followup(
        "details for a 923325034073 including websites",
        [{"question": "show me the top 30 mobile users by hit count from Peshawar", "sql": (
            "SELECT subscriber_id FROM ntc_ssl_logs WHERE LOWER(loc) LIKE '%peshawar%' GROUP BY subscriber_id LIMIT 30"
        )}],
    )
    folded_including = (including or "").lower()
    if (
        not including
        or "subscriber_id = '923325034073'" not in including
        or "group by requested_server, app_label" not in folded_including
        or "peshawar" in folded_including
    ):
        raise SystemExit(f"details including websites stayed on applications: {including}")
    one_sites = (
        "SELECT requested_server, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE subscriber_id = '923325034073' AND IFNULL(requested_server, '') <> '' "
        "GROUP BY requested_server ORDER BY total_bytes DESC LIMIT 50"
    )
    corresponding = resolve_followup(
        "also show the corresponding apps",
        [{"question": "show the websites accessed by 923325034073", "sql": one_sites}],
    )
    folded_corresponding = (corresponding or "").lower()
    if (
        not corresponding
        or "subscriber_id = '923325034073'" not in corresponding
        or "group by requested_server, app_label" not in folded_corresponding
        or "group by subscriber_id" in folded_corresponding
    ):
        raise SystemExit(f"corresponding apps dropped the websites: {corresponding}")
    hostel_sites = (
        "SELECT requested_server, app_label, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE subscriber_id = 'studentsgirlshostel3' AND IFNULL(requested_server, '') <> '' "
        "AND IFNULL(app_label, '') <> '' GROUP BY requested_server, app_label ORDER BY total_bytes DESC LIMIT 50"
    )
    blank_servers = resolve_followup(
        "include the apps where the requested_server is null",
        [{"question": "apps and websites used by studentsgirlshostel3", "sql": hostel_sites}],
    )
    folded_blank = (blank_servers or "").lower()
    if (
        not blank_servers
        or "subscriber_id = 'studentsgirlshostel3'" not in blank_servers
        or "group by requested_server, app_label" not in folded_blank
        or "requested_server, '') <> ''" in folded_blank
    ):
        raise SystemExit(f"null websites stayed excluded: {blank_servers}")
    same_login = resolve_followup(
        "show me the same for blinkp",
        [{"question": "apps and websites used by studentsgirlshostel3", "sql": hostel_sites}],
    )
    folded_login = (same_login or "").lower()
    if (
        not same_login
        or "subscriber_id = 'blinkp'" not in same_login
        or "studentsgirlshostel3" in same_login
        or "group by requested_server, app_label" not in folded_login
        or "group by subscriber_id" in folded_login
    ):
        raise SystemExit(f"the same for a login rebuilt the ranking: {same_login}")
    hostel_apps = resolve_followup("studentsgirlshostel3 activities", [])
    folded_hostel = (hostel_apps or "").lower()
    if (
        not hostel_apps
        or "subscriber_id = 'studentsgirlshostel3'" not in hostel_apps
        or "group by app_label" not in folded_hostel
        or "group by subscriber_id" in folded_hostel
    ):
        raise SystemExit(f"a login's activities were not listed: {hostel_apps}")
    nayatel = resolve_followup("show top 10 nayatel users by usage from Peshawar", [])
    folded_nayatel = (nayatel or "").lower()
    if (
        not nayatel
        or "like '%peshawar%'" not in folded_nayatel
        or "like '%nayatel%'" not in folded_nayatel
        or "subscriber_id = " in folded_nayatel
    ):
        raise SystemExit(f"Nayatel usage became a subscriber id: {nayatel}")
    cafe = resolve_followup(
        "apps and websites used by farshacafe",
        [{"question": "show top 10 nayatel users by flow count from Islamabad", "sql": (
            "SELECT subscriber_id FROM ntc_ssl_logs WHERE LOWER(loc) LIKE '%islamabad%' "
            "AND LOWER(client_asn) LIKE '%nayatel%' GROUP BY subscriber_id ORDER BY flows DESC LIMIT 10"
        )}],
    )
    folded_cafe = (cafe or "").lower()
    if (
        not cafe
        or "subscriber_id = 'farshacafe'" not in cafe
        or "group by requested_server, app_label" not in folded_cafe
        or "islamabad" in folded_cafe
        or "nayatel" in folded_cafe
    ):
        raise SystemExit(f"apps and websites for a login were not built: {cafe}")
    cafe_details = resolve_followup("farshacafe details", [])
    if (
        not cafe_details
        or "subscriber_id = 'farshacafe'" not in cafe_details
        or "group by app_label" not in cafe_details.lower()
        or "group by subscriber_id" in cafe_details.lower()
    ):
        raise SystemExit(f"login details were not listed: {cafe_details}")
    cnn_users = resolve_followup("now show me all the users who tried to access the cnn website", [])
    folded_cnn = (cnn_users or "").lower()
    if (
        not cnn_users
        or "like 'cnn.%'" not in folded_cnn
        or "like '%.cnn.%'" not in folded_cnn
        or "like '%.cnn'" not in folded_cnn
        or "like '%cnn%'" in folded_cnn
        or "group by subscriber_id" not in folded_cnn
        or "locate('_', subscriber_id) = 0" not in folded_cnn
    ):
        raise SystemExit(f"cnn website was a substring search: {cnn_users}")
    bbc_users = resolve_followup(
        "show me the same for bbc",
        [{"question": "now show me all the users who tried to access the cnn website", "sql": cnn_users}],
    )
    folded_bbc = (bbc_users or "").lower()
    if (
        not bbc_users
        or "like 'bbc.%'" not in folded_bbc
        or "like '%.bbc.%'" not in folded_bbc
        or "cnn" in folded_bbc
        or "group by subscriber_id" not in folded_bbc
    ):
        raise SystemExit(f"the same for bbc did not replace the website label: {bbc_users}")
    bbc_top = resolve_followup("show me the top 20 users who accessed bbc website", [])
    exact_sites = resolve_followup(
        "also display the exact website that user accessed",
        [{"question": "show me the top 20 users who accessed bbc website", "sql": bbc_top}],
    )
    folded_exact = (exact_sites or "").lower()
    if (
        not exact_sites
        or "subscriber_id = 'accessed'" in folded_exact
        or "like 'bbc.%'" not in folded_exact
        or "group by subscriber_id" not in folded_exact
        or "as requested_server" not in folded_exact
        or "limit 20" not in folded_exact
    ):
        raise SystemExit(f"exact website replaced the bbc ranking with a fake subscriber: {exact_sites}")
    one_sites = resolve_followup(
        "what other websites were accessed by mrkashif1",
        [{"question": "show me the exact websites accessed by these users", "sql": bbc_top}],
    )
    with_isp = resolve_followup(
        "also show the ISP name of mrkashif1",
        [{"question": "what other websites were accessed by mrkashif1", "sql": one_sites}],
    )
    folded_isp = (with_isp or "").lower()
    if (
        not with_isp
        or "subscriber_id = 'mrkashif1'" not in folded_isp
        or "group by requested_server" not in folded_isp
        or "as client_asn" not in folded_isp
        or "like 'bbc.%'" in folded_isp
    ):
        raise SystemExit(f"ISP name was not added to that subscriber's websites: {with_isp}")
    city_usage = resolve_followup("which city has the maximum whatsapp usage?", [])
    folded_city = (city_usage or "").lower()
    if (
        not city_usage
        or "client_location" in folded_city
        or "lower(loc) like '%lahore%'" not in folded_city
        or "lower(loc) like '%islamabad%'" not in folded_city
        or "like '%whatsapp%'" not in folded_city
        or "group by city" not in folded_city
        or "order by total_bytes desc" not in folded_city
        or "limit 1" not in folded_city
        or ") as ranked" in folded_city
        or folded_city.find("then 'lahore'") > folded_city.find("then 'shaheed benazirabad'")
        or folded_city.find("then 'mirpur khas'") > folded_city.find("then 'mirpur' when")
    ):
        raise SystemExit(f"city usage was read from the IP database: {city_usage}")
    prepare(city_usage, 200, allow_full_scan=True)
    top_cities = limit_followup(
        "show me the top 5",
        [{"question": "which city has the maximum whatsapp usage?", "sql": city_usage, "explanation": ""}],
        200,
    )[0]
    folded_top = (top_cities or "").lower()
    if (
        not top_cities
        or "like '%whatsapp%'" not in folded_top
        or "group by city" not in folded_top
        or "limit 5" not in folded_top
        or "limit 1" in folded_top
    ):
        raise SystemExit(f"top 5 did not keep the city ranking: {top_cities}")
    prepare(top_cities, 200, allow_full_scan=True)
    listed = resolve_followup(
        "list down the top 5 cities",
        [{"question": "which city has the maximum whatsapp usage?", "sql": city_usage}],
    )
    folded_listed = (listed or "").lower()
    if (
        not listed
        or "like '%whatsapp%'" not in folded_listed
        or "group by city" not in folded_listed
        or "limit 5" not in folded_listed
        or "client_location" in folded_listed
    ):
        raise SystemExit(f"top 5 cities dropped WhatsApp: {listed}")
    several = resolve_followup("which cities have the highest facebook usage?", [])
    folded_several = (several or "").lower()
    if (
        not several
        or "like '%facebook%'" not in folded_several
        or "group by city" not in folded_several
        or "limit 10" not in folded_several
        or "client_location" in folded_several
    ):
        raise SystemExit(f"plural cities returned one row: {several}")
    nayatel_cities = resolve_followup("show me all the cities where nayatel is present", [])
    folded_nayatel = (nayatel_cities or "").lower()
    if (
        not nayatel_cities
        or "group by subscriber_id" in folded_nayatel
        or "group by city" not in folded_nayatel
        or "like '%nayatel%'" not in folded_nayatel
        or "client_asn" not in folded_nayatel
        or "client_location" in folded_nayatel
        or "limit 200" not in folded_nayatel
        or "order by city asc" not in folded_nayatel
    ):
        raise SystemExit(f"Nayatel cities were listed as subscribers: {nayatel_cities}")
    prepare(nayatel_cities, 200, allow_full_scan=True)
    by_users = resolve_followup(
        "order the cities by unique user count",
        [{"question": "show me all the cities where nayatel is present", "sql": nayatel_cities}],
    )
    folded_users = (by_users or "").lower()
    if (
        not by_users
        or "case when" not in folded_users
        or "as users" not in folded_users
        or "order by users desc" not in folded_users
        or "like '%nayatel%'" not in folded_users
    ):
        raise SystemExit(f"user count did not sort the cities: {by_users}")
    again = resolve_followup(
        "order them by users in descending order",
        [{"question": "order the cities by unique user count", "sql": by_users}],
    )
    folded_again = (again or "").lower()
    if (
        not again
        or not folded_again.startswith("select case when")
        or "order by users desc" not in folded_again
        or "like '%nayatel%'" not in folded_again
    ):
        raise SystemExit(f"sorting by users dropped the city expression: {again}")
    prepare(again, 200, allow_full_scan=True)
    hits = resolve_followup(
        "show the top 10 websites with maximum hits",
        [{"question": "show the top 25 snapchat users", "sql": ranking}],
    )
    if (
        not hits
        or "group by requested_server" not in hits.lower()
        or "order by flows desc" not in hits.lower()
        or "order by total_bytes" in hits.lower()
        or "limit 10" not in hits.lower()
    ):
        raise SystemExit(f"website hits were ranked by bytes: {hits}")
    with_users = resolve_followup(
        "also give me the unique number of users that accessed those sites",
        [
            {"question": "show the top 25 snapchat users", "sql": ranking},
            {"question": "show the top 10 websites with maximum hits", "sql": hits},
        ],
    )
    if (
        not with_users
        or " as users" not in with_users.lower()
        or "order by flows desc" not in with_users.lower()
        or "total_bytes" not in with_users.lower()
        or "total_mb" not in with_users.lower()
    ):
        raise SystemExit(f"unique users replaced the top websites: {with_users}")
    lost = (
        "SELECT requested_server, COUNT(*) AS flow_count, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS traffic_volume_mb, "
        "COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
        "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users "
        "FROM ntc_ssl_logs WHERE IFNULL(requested_server, '') <> '' "
        "GROUP BY requested_server LIMIT 10"
    )
    restored = resolve_followup(
        "I meant the unique number of users that accessed the top 10 sites",
        [
            {"question": "show the top 10 websites with maximum hits", "sql": hits},
            {"question": "also give me the unique number of users that accessed those sites", "sql": lost},
        ],
    )
    if not restored or "order by flows desc" not in restored.lower() or " as users" not in restored.lower():
        raise SystemExit(f"the top websites were not restored: {restored}")
    from app.glossary import mentions

    blocked_sites = resolve_followup("show me the top 10 websites that got blocked the most", [])
    folded_blocked = (blocked_sites or "").lower()
    if (
        not blocked_sites
        or "group by requested_server" not in folded_blocked
        or "block_type <> 0" not in folded_blocked
        or "order by flows desc" not in folded_blocked
        or "order by total_bytes" in folded_blocked
        or "limit 10" not in folded_blocked
    ):
        raise SystemExit(f"blocked websites were ranked by traffic: {blocked_sites}")
    site_users = resolve_followup(
        "show me the top 10 users who tried to access static-nss.xhcdn.com",
        [{"question": "show me the top 10 websites that got blocked the most", "sql": blocked_sites}],
    )
    folded_site_users = (site_users or "").lower()
    if (
        not site_users
        or "group by subscriber_id" not in folded_site_users
        or "lower(requested_server) = 'static-nss.xhcdn.com'" not in folded_site_users
        or "app_label" in folded_site_users
        or "like '%access%'" in folded_site_users
        or "limit 10" not in folded_site_users
    ):
        raise SystemExit(f"the website was searched as an application: {site_users}")
    blocked_users = resolve_followup(
        "show me the top 20 users who have the most blocked flows",
        [{"question": "top 10 whatsapp users", "sql": (
            "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
            "WHERE LOWER(app_label) LIKE '%whatsapp%' GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 10"
        )}],
    )
    folded_blocked_users = (blocked_users or "").lower()
    if (
        not blocked_users
        or "group by subscriber_id" not in folded_blocked_users
        or "block_type <> 0" not in folded_blocked_users
        or "app_label" in folded_blocked_users
        or "like '%blocked%'" in folded_blocked_users
        or "order by blocked_flows desc" not in folded_blocked_users
        or "limit 20" not in folded_blocked_users
    ):
        raise SystemExit(f"blocked flows were searched as an application: {blocked_users}")
    gulshan = resolve_followup("show me the top 30 users of gulshan karachi with the most blocked flows", [])
    folded_gulshan = (gulshan or "").lower()
    if (
        not gulshan
        or "like '%gulshan%karachi%'" not in folded_gulshan
        or "block_type <> 0" not in folded_gulshan
        or "app_label" in folded_gulshan
        or "order by blocked_flows desc" not in folded_gulshan
        or "limit 30" not in folded_gulshan
    ):
        raise SystemExit(f"Gulshan Karachi was searched as an application: {gulshan}")
    unknown = resolve_followup("show me the top 10 gulshan users", [])
    if not unknown or "app_label" in unknown.lower() or "like '%gulshan%'" in unknown.lower():
        raise SystemExit(f"an unknown word was used as an application: {unknown}")
    known = resolve_followup("show me the top 10 whatsapp users", [])
    if not known or "like '%whatsapp%'" not in known.lower():
        raise SystemExit(f"a mapped application was ignored: {known}")
    multan = resolve_followup("show me the top 20 multan ptcl whatsapp users", [])
    folded_multan = (multan or "").lower()
    if (
        not multan
        or "like '%whatsapp%'" not in folded_multan
        or "like '%multan%'" not in folded_multan
        or "like '%pakistan telecommunication%'" not in folded_multan
        or "like '%ptcl%'" not in folded_multan
        or "app_label) like '%multan%'" in folded_multan
        or "lower(loc) like '%ptcl%'" in folded_multan
        or "limit 20" not in folded_multan
    ):
        raise SystemExit(f"Multan was dropped from the PTCL WhatsApp ranking: {multan}")
    same_instagram = resolve_followup(
        "show me the same for instagram",
        [{"question": "show me the top 20 multan ptcl whatsapp users", "sql": multan}],
    )
    folded_instagram = (same_instagram or "").lower()
    if (
        not same_instagram
        or "like '%instagram%'" not in folded_instagram
        or "like '%whatsapp%'" in folded_instagram
        or "like '%multan%'" not in folded_instagram
        or "like '%ptcl%'" not in folded_instagram
        or "as loc" not in folded_instagram
        or "limit 20" not in folded_instagram
    ):
        raise SystemExit(f"the same for Instagram did not keep Multan and PTCL: {same_instagram}")
    punjab = resolve_followup(
        "show me the top 10 fixedline users by flow count from punjab",
        [{"question": "keep the same app but operator should be jazz", "sql": (
            "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
            "WHERE LOWER(app_label) LIKE '%instagram%' AND (LOWER(loc) LIKE '%sahiwal%') "
            "AND (LOWER(client_asn) LIKE '%jazz%') GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 20"
        )}],
    )
    folded_punjab = (punjab or "").lower()
    if (
        not punjab
        or "not (nullif(mcc" not in folded_punjab
        or "like '%punjab%'" not in folded_punjab
        or "like '%lahore%'" not in folded_punjab
        or "like '%karachi%'" in folded_punjab
        or "instagram" in folded_punjab
        or "jazz" in folded_punjab
        or "app_label" in folded_punjab
        or "order by flows desc" not in folded_punjab
        or "limit 10" not in folded_punjab
    ):
        raise SystemExit(f"Punjab fixed-line users kept the previous filters: {punjab}")
    mobile_only = resolve_followup(
        "now give me the top 10 mobile users",
        [{"question": "youtube users", "sql": "SELECT subscriber_id FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%youtube%' GROUP BY subscriber_id"}],
    )
    if mobile_only is not None:
        raise SystemExit(f"mobile users were rebuilt instead of keeping the previous app: {mobile_only}")
    blocked_apps = resolve_followup(
        "show me the blocked apps that 03334128404ASD was trying to access",
        [{"question": "show me the top 20 users who have the most blocked flows", "sql": blocked_users}],
    )
    folded_blocked_apps = (blocked_apps or "").lower()
    if (
        not blocked_apps
        or "subscriber_id = '03334128404asd'" not in folded_blocked_apps
        or "group by app_label" not in folded_blocked_apps
        or "block_type <> 0" not in folded_blocked_apps
        or "order by blocked_flows desc" not in folded_blocked_apps
        or "like '%access%'" in folded_blocked_apps
        or "like '%blocked%'" in folded_blocked_apps
    ):
        raise SystemExit(f"blocked apps for one subscriber were not built: {blocked_apps}")
    blocked_sites_one = resolve_followup(
        "show me the blocked websites that 03334128404ASD was trying to access",
        [{"question": "show me the top 20 users who have the most blocked flows", "sql": blocked_users}],
    )
    folded_one_site = (blocked_sites_one or "").lower()
    if (
        not blocked_sites_one
        or "subscriber_id = '03334128404asd'" not in folded_one_site
        or "group by requested_server" not in folded_one_site
        or "block_type <> 0" not in folded_one_site
        or "group by app_label" in folded_one_site
    ):
        raise SystemExit(f"blocked websites for one subscriber were not built: {blocked_sites_one}")
    either = resolve_followup(
        "did 03146883G access any blocked website or app ?",
        [{"question": "show me the websites visited by 03146883G of app type http", "sql": (
            "SELECT requested_server FROM ntc_ssl_logs WHERE subscriber_id = '03146883G' "
            "AND LOWER(app_label) = 'http' GROUP BY requested_server"
        )}],
    )
    folded_either = (either or "").lower()
    if (
        not either
        or "subscriber_id = '03146883g'" not in folded_either
        or "group by requested_server, app_label" not in folded_either
        or "block_type <> 0" not in folded_either
        or "or ifnull(app_label, '') <> ''" not in folded_either
        or "like '%access%'" in folded_either
        or "like '%blocked%'" in folded_either
    ):
        raise SystemExit(f"blocked website or app searched websites only: {either}")
    printed = resolve_followup(
        "also print the block type column",
        [{"question": "show me top 20 users from kpk that tried to access blocked content the most", "sql": (
            "SELECT subscriber_id, COUNT(*) AS blocked_flows FROM ntc_ssl_logs "
            "WHERE (block_type IS NOT NULL AND block_type <> 0) AND (LOWER(loc) LIKE '%peshawar%') "
            "GROUP BY subscriber_id ORDER BY blocked_flows DESC LIMIT 20"
        )}],
    )
    folded_printed = (printed or "").lower()
    if (
        not printed
        or "as block_type" not in folded_printed
        or "group by subscriber_id" not in folded_printed
        or "block_type <> 0" not in folded_printed
        or "like '%peshawar%'" not in folded_printed
        or "limit 20" not in folded_printed
    ):
        raise SystemExit(f"block type was not added to the ranking: {printed}")
    both = resolve_followup(
        "which blocked websites and apps accessed by 923421012400",
        [],
    )
    folded_both = (both or "").lower()
    if (
        not both
        or "subscriber_id = '923421012400'" not in folded_both
        or "group by requested_server, app_label" not in folded_both
        or "block_type <> 0" not in folded_both
        or "requested_server," not in folded_both
    ):
        raise SystemExit(f"blocked websites and apps were not listed together: {both}")
    with_apps = resolve_followup(
        "show me the blocked websites along with the corresponding apps",
        [{"question": "which blocked websites and apps accessed by 923421012400", "sql": (
            "SELECT requested_server, COUNT(*) AS blocked_flows FROM ntc_ssl_logs "
            "WHERE subscriber_id = '923421012400' AND (block_type IS NOT NULL AND block_type <> 0) "
            "GROUP BY requested_server ORDER BY blocked_flows DESC"
        )}],
    )
    folded_with_apps = (with_apps or "").lower()
    if (
        not with_apps
        or "subscriber_id = '923421012400'" not in folded_with_apps
        or "group by requested_server, app_label" not in folded_with_apps
    ):
        raise SystemExit(f"the corresponding apps dropped the subscriber: {with_apps}")
    meant = resolve_followup(
        "I meant for user 923421012400",
        [{"question": "show me the blocked websites along with the corresponding apps", "sql": (
            "SELECT requested_server, app_label, COUNT(*) AS blocked_flows FROM ntc_ssl_logs "
            "WHERE (block_type IS NOT NULL AND block_type <> 0) "
            "GROUP BY requested_server, app_label ORDER BY blocked_flows DESC LIMIT 200"
        )}],
    )
    folded_meant = (meant or "").lower()
    if (
        not meant
        or "subscriber_id = '923421012400'" not in folded_meant
        or "group by requested_server, app_label" not in folded_meant
        or "block_type <> 0" not in folded_meant
    ):
        raise SystemExit(f"the named user was not applied to the blocked list: {meant}")
    if not mentions("top websites by visits", "hit") or not mentions("which domains", "website"):
        raise SystemExit("the glossary missed a known word")
    if mentions("top users with maximum activity", "their_apps"):
        raise SystemExit("a user ranking was read as one person's applications")
    previous_person = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE subscriber_id = '03225007549uu' GROUP BY app_label ORDER BY total_bytes DESC"
    )
    swapped = resolve_followup(
        "what about 03315446519QAE?",
        [{"question": "show me the other activities done by 03225007549uu", "sql": previous_person}],
    )
    if not swapped or "subscriber_id = '03315446519QAE'" not in swapped or "03225007549uu" in swapped:
        raise SystemExit(f"what about did not switch the subscriber: {swapped}")
    quoted = resolve_followup(
        "other activities done by the subscriber '03315446519QAE'",
        [{"question": "show me the other activities done by 03225007549uu", "sql": previous_person}],
    )
    if not quoted or "subscriber_id = '03315446519QAE'" not in quoted or "03225007549uu" in quoted:
        raise SystemExit(f"a quoted subscriber id was ignored: {quoted}")
    apps_with_users = (
        "SELECT app_label, COUNT(*) AS flows, "
        "COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
        "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users "
        "FROM ntc_ssl_logs GROUP BY app_label ORDER BY flows DESC LIMIT 20"
    )
    tiktok_users = resolve_followup(
        "show me the top 50 tiktok users",
        [{"question": "show me the unique user count against each of these 20 apps", "sql": apps_with_users}],
    )
    if (
        not tiktok_users
        or "group by subscriber_id" not in tiktok_users.lower()
        or "locate('_', subscriber_id) = 0" not in tiktok_users.lower()
        or "like '%tiktok%'" not in tiktok_users.lower()
        or "count(distinct" in tiktok_users.lower()
        or "order by total_bytes desc" not in tiktok_users.lower()
        or "limit 50" not in tiktok_users.lower()
    ):
        raise SystemExit(f"tiktok users reused the per-app user count: {tiktok_users}")
    against = resolve_followup(
        "show me the top 10 users against TikTok",
        [{"question": "show me the number of unique users against each app", "sql": apps_with_users}],
    )
    if (
        not against
        or "group by subscriber_id" not in against.lower()
        or "like '%tiktok%'" not in against.lower()
        or "group by app_label" in against.lower()
        or "limit 10" not in against.lower()
    ):
        raise SystemExit(f"users against an application stayed on the application list: {against}")
    with_asn = resolve_followup("show me the top 20 users by hit count along with the client asn", [])
    if (
        not with_asn
        or "group by subscriber_id" not in with_asn.lower()
        or "like '%client%'" in with_asn.lower()
        or "group_concat(distinct case when ifnull(client_asn, '') <> '' then client_asn end" not in with_asn.lower()
        or "order by flows desc" not in with_asn.lower()
        or "limit 20" not in with_asn.lower()
    ):
        raise SystemExit(f"client asn was treated as an application: {with_asn}")
    prepared_asn = prepare(with_asn, 200, allow_full_scan=True)
    if "client_asn" not in prepared_asn.lower() or "like '%client%'" in prepared_asn.lower():
        raise SystemExit(f"the guard dropped the client asn column: {prepared_asn}")
    place = resolve_followup("show me the address of 030944736121", [])
    if (
        not place
        or "subscriber_id = '030944736121'" not in place
        or "group by loc, geo" not in place.lower()
        or "client_location" in place.lower()
    ):
        raise SystemExit(f"a subscriber address was not read from loc: {place}")
    confirmed = resolve_followup(
        "yes, I want to see the location / address of this user",
        [{"question": "show me the address of 030944736121", "sql": "", "explanation": "Do you want to see the address?"}],
    )
    if not confirmed or "subscriber_id = '030944736121'" not in confirmed or "group by loc, geo" not in confirmed.lower():
        raise SystemExit(f"confirming the address did not keep the subscriber: {confirmed}")
    shown_place = prepare(place, 200, allow_full_scan=False)
    if "030944736121" not in shown_place:
        raise SystemExit(f"one subscriber still required a time filter: {shown_place}")
    their_addresses = resolve_followup(
        "show me the addresses of these users",
        [{"question": "show me the top 20 users with highest hit count along with the client asn", "sql": with_asn}],
    )
    if (
        not their_addresses
        or "group by subscriber_id" not in their_addresses.lower()
        or " as loc" not in their_addresses.lower()
        or " as geo" not in their_addresses.lower()
        or "client_location" in their_addresses.lower()
        or "server_location" in their_addresses.lower()
        or "client_asn" not in their_addresses.lower()
        or "order by flows desc" not in their_addresses.lower()
    ):
        raise SystemExit(f"addresses of these users used the IP database: {their_addresses}")
    both = resolve_followup("show me top 20 users. Also list the respective ASN and address.", [])
    if (
        not both
        or "like '%respective%'" in both.lower()
        or "group by subscriber_id" not in both.lower()
        or " as client_asn" not in both.lower()
        or " as loc" not in both.lower()
        or " as geo" not in both.lower()
        or "client_location" in both.lower()
        or "limit 20" not in both.lower()
    ):
        raise SystemExit(f"asn and address were not columns on the user ranking: {both}")
    lahore = resolve_followup(
        "show me the top 10 users from Lahore",
        [{"question": "show me top 20 users. Also list the respective ASN and address.", "sql": both}],
    )
    if (
        not lahore
        or "like '%lahore%'" not in lahore.lower()
        or "loc = '" in lahore.lower()
        or "app_label" in lahore.lower()
        or "limit 10" not in lahore.lower()
        or "limit 20" in lahore.lower()
        or "client_asn" not in lahore.lower()
        or "client_location" in lahore.lower()
    ):
        raise SystemExit(f"Lahore was not matched inside the address: {lahore}")
    karachi = resolve_followup("show me the top 10 whatsapp users from Karachi", [])
    isp = resolve_followup(
        "also show the corresponding ISP",
        [{"question": "show me the top 10 whatsapp users from Karachi", "sql": karachi}],
    )
    if (
        not karachi
        or "like '%whatsapp%'" not in karachi.lower()
        or "like '%karachi%'" not in karachi.lower()
        or not isp
        or "as client_asn" not in isp.lower()
        or "like '%whatsapp%'" not in isp.lower()
        or "like '%karachi%'" not in isp.lower()
        or "limit 10" not in isp.lower()
    ):
        raise SystemExit(f"the ISP was not added to the Karachi ranking: {karachi} {isp}")
    isps = resolve_followup(
        "also show the corresponding ISPs",
        [{"question": "show me the top 10 whatsapp users from Rawalpindi", "sql": karachi}],
    )
    if not isps or "as client_asn" not in isps.lower() or "like '%karachi%'" not in isps.lower():
        raise SystemExit(f"the plural ISP was not added: {isps}")
    nayatel = resolve_followup(
        "show me the top 10 whatsapp users in Rawalpindi from Nayatel",
        [{"question": "show me the top 10 whatsapp users from Rawalpindi", "sql": karachi}],
    )
    folded_nayatel = (nayatel or "").lower()
    if (
        not nayatel
        or "like '%whatsapp%'" not in folded_nayatel
        or "like '%rawalpindi%'" not in folded_nayatel
        or "like '%nayatel%'" not in folded_nayatel
        or "loc) like '%nayatel%'" in folded_nayatel
        or "limit 10" not in folded_nayatel
    ):
        raise SystemExit(f"Nayatel was treated as a city: {nayatel}")
    rwp = resolve_followup("show me the top 10 users from RWP", [])
    if not rwp or "like '%rawalpindi%'" not in rwp.lower() or "like '%rwp%'" not in rwp.lower():
        raise SystemExit(f"RWP was not read as Rawalpindi: {rwp}")
    ptcl = resolve_followup("show me the top 10 users from PTCL", [])
    if (
        not ptcl
        or "like '%pakistan telecommunication%'" not in ptcl.lower()
        or "like '%ptcl%'" not in ptcl.lower()
        or "lower(loc) like '%ptcl%'" in ptcl.lower()
    ):
        raise SystemExit(f"PTCL was not read as the ISP: {ptcl}")
    di_khan = resolve_followup("show me the top 5 users from DI Khan", [])
    if not di_khan or "like '%dera ismail khan%'" not in di_khan.lower() or "limit 5" not in di_khan.lower():
        raise SystemExit(f"DI Khan was not read as Dera Ismail Khan: {di_khan}")
    gulberg = resolve_followup(
        "show me the top 10 jazz twitter users in the area of gulberg in Lahore",
        [],
    )
    folded_gulberg = (gulberg or "").lower()
    if (
        not gulberg
        or "like '%twitter%'" not in folded_gulberg
        or "like '%lahore%'" not in folded_gulberg
        or "like '%jazz%'" not in folded_gulberg
        or "lower(loc) like '%gulberg%'" not in folded_gulberg
        or "like '%gulberg%'" in folded_gulberg.split("lower(loc) like '%gulberg%'")[0]
        or "limit 10" not in folded_gulberg
    ):
        raise SystemExit(f"Gulberg was not kept inside Lahore: {gulberg}")
    zoom = resolve_followup("show me the top 10 zoom users in the area of gulberg in Lahore", [])
    if not zoom or "like '%zoom%'" not in zoom.lower() or "app_label) like '%gulberg%'" in zoom.lower():
        raise SystemExit(f"Gulberg replaced the application: {zoom}")
    dha = resolve_followup("show me the top 10 instagram users in the area of dha lahore", [])
    folded_dha = (dha or "").lower()
    if (
        not dha
        or "like '%instagram%'" not in folded_dha
        or "lower(loc) like '%dha%lahore%'" not in folded_dha
        or "like '%dha lahore%'" in folded_dha
        or "limit 10" not in folded_dha
    ):
        raise SystemExit(f"DHA Lahore required the words to be adjacent: {dha}")
    for phrase, needle in (
        ("show me the top 10 users by flow count in lake city lahore", "like '%lake%city%lahore%'"),
        ("show me the top 10 users by flow count in gulberg lahore", "like '%gulberg%lahore%'"),
    ):
        placed = resolve_followup(phrase, [])
        folded_placed = (placed or "").lower()
        if (
            not placed
            or needle not in folded_placed
            or "app_label" in folded_placed
            or "order by flows desc" not in folded_placed
            or "limit 10" not in folded_placed
        ):
            raise SystemExit(f"the neighbourhood was used as an application: {phrase} -> {placed}")
    gulberg_app = resolve_followup("show me the top 10 twitter users in gulberg lahore", [])
    if (
        not gulberg_app
        or "like '%twitter%'" not in gulberg_app.lower()
        or "like '%gulberg%lahore%'" not in gulberg_app.lower()
        or "like '%gulberg%'" in gulberg_app.lower().split("like '%gulberg%lahore%'")[0]
    ):
        raise SystemExit(f"Gulberg Lahore dropped Twitter: {gulberg_app}")
    same_city = resolve_followup(
        "now show me the top 10 whatsapp Nayatel users from the same city",
        [{"question": "show me the top 10 whatsapp users from Faisalabad", "sql": (
            "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
            "WHERE LOWER(app_label) LIKE '%whatsapp%' AND IFNULL(subscriber_id, '') <> '' "
            "AND LOCATE('_', subscriber_id) = 0 AND (LOWER(loc) LIKE '%faisalabad%' OR LOWER(loc) LIKE '%fsd%' "
            "OR LOWER(loc) LIKE '%lyallpur%') GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 10"
        )}],
    )
    folded_same = (same_city or "").lower()
    if (
        not same_city
        or "like '%whatsapp%'" not in folded_same
        or "like '%faisalabad%'" not in folded_same
        or "like '%nayatel%'" not in folded_same
        or "lower(loc) like '%nayatel%'" in folded_same
        or "limit 10" not in folded_same
    ):
        raise SystemExit(f"the same city and Nayatel were dropped: {same_city}")
    for phrase in ("on Jazz", "but on Jazz", "for Jazz only", "of Jazz"):
        if not any(place.name == "Jazz" for place in mentioned_isps(phrase)):
            raise SystemExit(f"Jazz was not read as an ISP in: {phrase}")
    jazz_previous = (
        "SELECT subscriber_id, "
        "GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(loc, '') <> '' THEN loc END ORDER BY loc SEPARATOR ', ') AS loc, "
        "GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(client_asn, '') <> '' THEN client_asn END ORDER BY client_asn SEPARATOR ', ') AS client_asn, "
        "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%whatsapp%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND (LOWER(loc) LIKE '%dera ghazi khan%' OR LOWER(loc) LIKE '%dg khan%' "
        "OR LOWER(loc) LIKE '%d.g.khan%' OR LOWER(loc) LIKE '%dgk%') "
        "AND (LOWER(client_asn) LIKE '%telenor%') "
        "GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 20"
    )
    same_jazz = resolve_followup(
        "now show me the same but on Jazz",
        [{"question": "top 20 whatsapp users of the same city but of Telenor only", "sql": jazz_previous}],
    )
    folded_jazz = (same_jazz or "").lower()
    if (
        not same_jazz
        or "like '%whatsapp%'" not in folded_jazz
        or "like '%dera ghazi khan%'" not in folded_jazz
        or "like '%jazz%'" not in folded_jazz
        or "like '%mobilink%'" not in folded_jazz
        or "like '%pmcl%'" not in folded_jazz
        or "lower(loc) like '%jazz%'" in folded_jazz
        or "like '%telenor%'" in folded_jazz
        or "limit 20" not in folded_jazz
    ):
        raise SystemExit(f"the same result was not switched to Jazz: {same_jazz}")
    hyderabad = (
        "SELECT subscriber_id, "
        "GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(client_asn, '') <> '' THEN client_asn END ORDER BY client_asn SEPARATOR ', ') AS client_asn, "
        "GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(loc, '') <> '' THEN loc END ORDER BY loc SEPARATOR ', ') AS loc, "
        "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%instagram%' "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND (LOWER(loc) LIKE '%hyderabad%' OR LOWER(loc) LIKE '%hyd%') "
        "AND (LOWER(client_asn) LIKE '%jazz%' OR LOWER(client_asn) LIKE '%pmcl%') "
        "GROUP BY subscriber_id ORDER BY flows DESC LIMIT 20"
    )
    ufone = resolve_followup(
        "now show me for ufone",
        [{"question": "now show me the same but on Jazz only", "sql": hyderabad}],
    )
    folded_ufone = (ufone or "").lower()
    if (
        not ufone
        or "like '%instagram%'" not in folded_ufone
        or "like '%hyderabad%'" not in folded_ufone
        or "like '%ufone%'" not in folded_ufone
        or "like '%ptml%'" not in folded_ufone
        or "lower(loc) like '%ufone%'" in folded_ufone
        or "like '%jazz%'" in folded_ufone
        or "like '%pmcl%'" in folded_ufone
        or "order by flows desc" not in folded_ufone
        or "limit 20" not in folded_ufone
    ):
        raise SystemExit(f"for Ufone did not keep the Instagram Hyderabad ranking: {ufone}")
    fresh_ufone = resolve_followup("show me the top 20 ufone users", [{"question": "instagram", "sql": hyderabad}])
    if fresh_ufone and "like '%instagram%'" in fresh_ufone.lower():
        raise SystemExit(f"a new Ufone ranking kept Instagram: {fresh_ufone}")
    copied = (
        "SELECT subscriber_id, COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' "
        "AND LOCATE('_', subscriber_id) = 0 THEN subscriber_id END) AS users "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) LIKE '%tiktok%' "
        "GROUP BY subscriber_id ORDER BY flows DESC LIMIT 50"
    )
    if not check_sql("show me the top 50 tiktok users", copied, [{"question": "unique users", "sql": apps_with_users}], True):
        raise SystemExit("a user ranking of IP_port ids was accepted")
    one_site = (
        "SELECT requested_server, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE subscriber_id = '92343809067' AND IFNULL(requested_server, '') <> '' "
        "GROUP BY requested_server ORDER BY total_bytes DESC LIMIT 50"
    )
    by_site_count = resolve_followup(
        "give me the top 10 users with the highest website count",
        [{"question": "show me the websites accessed by 92343809067", "sql": one_site}],
    )
    if (
        not by_site_count
        or "group by subscriber_id" not in by_site_count.lower()
        or "count(distinct requested_server) as websites" not in by_site_count.lower()
        or "order by websites desc" not in by_site_count.lower()
        or "limit 10" not in by_site_count.lower()
        or "92343809067" in by_site_count
        or "group by requested_server" in by_site_count.lower()
    ):
        raise SystemExit(f"website count ranked websites instead of subscribers: {by_site_count}")
    from app.followup import limit_followup

    user_rank = (
        "SELECT subscriber_id, COUNT(DISTINCT requested_server) AS websites, COUNT(*) AS flows "
        "FROM ntc_ssl_logs WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id ORDER BY websites DESC LIMIT 10"
    )
    site_list = (
        "SELECT requested_server, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE subscriber_id = '030944736121' AND IFNULL(requested_server, '') <> '' "
        "GROUP BY requested_server ORDER BY total_bytes DESC LIMIT 50"
    )
    both = [
        {"question": "users with maximum website count", "sql": user_rank},
        {"question": "websites accessed by 030944736121", "sql": site_list},
    ]
    _sql, asked = limit_followup("show the top 500", both, 200)
    if _sql or not asked or "websites for 030944736121" not in asked or "subscribers ranked by website count" not in asked:
        raise SystemExit(f"an ambiguous limit was assumed: {asked}")
    chosen, again = limit_followup(
        "the websites for that subscriber, top 200",
        both + [{"question": "show the top 500", "sql": "", "explanation": asked}],
        200,
    )
    if again or not chosen or "030944736121" not in chosen or "limit 200" not in chosen.lower() or "group by requested_server" not in chosen.lower():
        raise SystemExit(f"the website list did not take the new limit: {chosen} {again}")
    sized, size_again = limit_followup(
        "The top 500 of the websites for 030944736121",
        both + [{"question": "show the top 500", "sql": "", "explanation": asked}],
        200,
    )
    if sized or not size_again or "websites for 030944736121" not in size_again or "every row" not in size_again:
        raise SystemExit(f"a limit above 200 did not ask for a size: {size_again}")
    kept, kept_again = limit_followup(
        "top 200 if 500 cannot be displayed",
        both + [{"question": "The top 500 of the websites for 030944736121", "sql": "", "explanation": size_again}],
        200,
    )
    if kept_again or not kept or "030944736121" not in kept or "limit 200" not in kept.lower() or "group by subscriber_id" in kept.lower():
        raise SystemExit(f"a size reply forgot the chosen list: {kept} {kept_again}")
    subscriber_rank = (
        "SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs GROUP BY subscriber_id ORDER BY flows DESC LIMIT 30"
    )
    youtube_protocols = resolve_followup(
        "show the traffic and flows for all the youtube protocols",
        [{"question": "top users by flow count", "sql": subscriber_rank}],
    )
    youtube_folded = (youtube_protocols or "").lower()
    if (
        not youtube_protocols
        or "like '%youtube%'" not in youtube_folded
        or "group by app_label" not in youtube_folded
        or "group by subscriber_id" in youtube_folded
        or "as total_mb" not in youtube_folded
        or "as flows" not in youtube_folded
        or "order by total_mb desc" not in youtube_folded
        or "limit" in youtube_folded
    ):
        raise SystemExit(f"youtube protocols were not listed: {youtube_protocols}")
    prepare(youtube_protocols, 200, allow_full_scan=True, allow_all_groups=True)
    blocked_protocols = resolve_followup(
        "show me the protocols that got blocked the most",
        [{"question": "show me the same for whatsapp", "sql": youtube_protocols.replace("%youtube%", "%whatsapp%")}],
    )
    blocked_folded = (blocked_protocols or "").lower()
    if (
        not blocked_protocols
        or "like '%whatsapp%'" not in blocked_folded
        or "block_type <> 0" not in blocked_folded
        or "group by app_label" not in blocked_folded
        or "group by subscriber_id" in blocked_folded
        or "as blocked_flows" not in blocked_folded
        or "order by blocked_flows desc" not in blocked_folded
    ):
        raise SystemExit(f"blocked protocols were not ranked: {blocked_protocols}")
    prepare(blocked_protocols, 200, allow_full_scan=True, allow_all_groups=True)
    blocked_list = resolve_followup("show me the list of protocols by blocking count", [])
    list_folded = (blocked_list or "").lower()
    if (
        not blocked_list
        or "block_type <> 0" not in list_folded
        or "group by app_label" not in list_folded
        or "as blocked_flows" not in list_folded
        or "order by blocked_flows desc" not in list_folded
        or "like '%" in list_folded
        or "in ('king')" in list_folded
    ):
        raise SystemExit(f"blocking count list was wrong: {blocked_list}")
    prepare(blocked_list, 200, allow_full_scan=True)
    ssl_domains = resolve_followup("show me the blocked domains against ssl app", [])
    ssl_folded = (ssl_domains or "").lower()
    if (
        not ssl_domains
        or "group by requested_server" not in ssl_folded
        or "lower(app_label) = 'ssl'" not in ssl_folded
        or "block_type <> 0" not in ssl_folded
        or "group by app_label" in ssl_folded
        or "like '%ssl%'" in ssl_folded
    ):
        raise SystemExit(f"blocked SSL domains were wrong: {ssl_domains}")
    prepare(ssl_domains, 200, allow_full_scan=True)
    by_operator = resolve_followup("show me the blocked flows by operator", [])
    operator_folded = (by_operator or "").lower()
    if (
        not by_operator
        or "group by client_asn" not in operator_folded
        or "block_type <> 0" not in operator_folded
        or "as blocked_flows" not in operator_folded
        or "group by subscriber_id" in operator_folded
        or " as loc" in operator_folded
    ):
        raise SystemExit(f"blocked flows by operator were wrong: {by_operator}")
    prepare(by_operator, 200, allow_full_scan=True)
    ptcl_apps = resolve_followup("show me the top 10 apps that got blocked in PTCL", [])
    ptcl_folded = (ptcl_apps or "").lower()
    if (
        not ptcl_apps
        or "group by app_label" not in ptcl_folded
        or "block_type <> 0" not in ptcl_folded
        or "like '%ptcl%'" not in ptcl_folded
        or "client_asn" not in ptcl_folded
        or "limit 10" not in ptcl_folded
    ):
        raise SystemExit(f"blocked apps in PTCL dropped the operator: {ptcl_apps}")
    prepare(ptcl_apps, 200, allow_full_scan=True)
    jazz_apps = resolve_followup(
        "show me the same for jazz",
        [{"question": "top apps blocked in PTCL", "sql": ptcl_apps}],
    )
    jazz_folded = (jazz_apps or "").lower()
    if (
        not jazz_apps
        or "group by app_label" not in jazz_folded
        or "like '%jazz%'" not in jazz_folded
        or "like '%ptcl%'" in jazz_folded
        or "block_type <> 0" not in jazz_folded
        or "limit 10" not in jazz_folded
    ):
        raise SystemExit(f"same for Jazz did not swap the operator: {jazz_apps}")
    prepare(jazz_apps, 200, allow_full_scan=True)
    zong_apps = resolve_followup("show me the same for zong", [{"question": "ptcl apps", "sql": ptcl_apps}])
    with_users = resolve_followup(
        "also show the unique number of zong users against the blocked apps",
        [{"question": "same for zong", "sql": zong_apps}],
    )
    users_folded = (with_users or "").lower()
    if (
        not with_users
        or "as users" not in users_folded
        or "group by app_label" not in users_folded
        or "like '%zong%'" not in users_folded
        or "block_type <> 0" not in users_folded
    ):
        raise SystemExit(f"unique Zong users were not added beside the apps: {with_users}")
    prepare(with_users, 200, allow_full_scan=True)
    vk_users = (
        "SELECT subscriber_id, COUNT(*) AS blocked_flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, requested_server, "
        "GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(loc, '') <> '' THEN loc END ORDER BY loc SEPARATOR ', ') AS loc, "
        "GROUP_CONCAT(DISTINCT CASE WHEN IFNULL(geo, '') <> '' THEN geo END ORDER BY geo SEPARATOR ', ') AS geo "
        "FROM ntc_ssl_logs WHERE (block_type IS NOT NULL AND block_type <> 0) AND (LOWER(client_asn) LIKE '%telenor%') "
        "AND LOWER(app_label) IN ('vkontakte_music', 'vkontakte', 'vkontakte_video') "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id, requested_server ORDER BY blocked_flows DESC LIMIT 10"
    )
    swat_users = resolve_followup(
        "building on the previous query, I want to see only those users that belong to swat city",
        [{"question": "also show the address of the users", "sql": vk_users}],
    )
    swat_folded = (swat_users or "").lower()
    if (
        not swat_users
        or "like '%swat%'" not in swat_folded
        or "like '%telenor%'" not in swat_folded
        or "vkontakte" not in swat_folded
        or "bloons_monkey_city" in swat_folded
        or "group by subscriber_id" not in swat_folded
    ):
        raise SystemExit(f"Swat filter dropped the previous ranking: {swat_users}")
    prepare(swat_users, 200, allow_full_scan=True)
    snap_users = (
        "SELECT subscriber_id, COUNT(*) AS blocked_flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
        "FROM ntc_ssl_logs WHERE LOWER(app_label) IN ('snapchat', 'snapchat_voip') "
        "AND (LOWER(client_asn) LIKE '%zong%') AND IFNULL(subscriber_id, '') <> '' "
        "AND LOCATE('_', subscriber_id) = 0 GROUP BY subscriber_id ORDER BY blocked_flows DESC LIMIT 10"
    )
    with_address = resolve_followup(
        "also show the user addresses",
        [{"question": "snapchat zong users blocked", "sql": snap_users}],
    )
    address_folded = (with_address or "").lower()
    if (
        not with_address
        or "subscriber_id = 'addresses'" in address_folded
        or "as loc" not in address_folded
        or "as geo" not in address_folded
        or "group by subscriber_id" not in address_folded
        or "snapchat" not in address_folded
    ):
        raise SystemExit(f"user addresses were treated as a subscriber: {with_address}")
    prepare(with_address, 200, allow_full_scan=True)
    access_times = resolve_followup(
        "at what time did 210317380 try to access xhamster",
        [{"question": "other activities", "sql": "SELECT app_label, COUNT(*) AS flows FROM ntc_ssl_logs WHERE subscriber_id = '210317380' GROUP BY app_label ORDER BY flows DESC"}],
    )
    login_times = resolve_followup(
        "what was the time when ayanjutt accessed nordvpn?",
        [
            {
                "question": "other activities",
                "sql": (
                    "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
                    "COUNT(*) AS flows FROM ntc_ssl_logs WHERE subscriber_id = 'ayanjutt' "
                    "GROUP BY app_label ORDER BY total_bytes DESC"
                ),
            }
        ],
    )
    login_folded = (login_times or "").lower()
    if (
        not login_times
        or "from_unixtime(start_time)" not in login_folded
        or "as duration_seconds" not in login_folded
        or "duration / 1000" not in login_folded
        or "subscriber_id = 'ayanjutt'" not in login_folded
        or "nordvpn" not in login_folded
    ):
        raise SystemExit(f"access times for a letter login were not listed: {login_times}")
    prepare(login_times, 200, allow_full_scan=True)
    weather_times = (
        "SELECT FROM_UNIXTIME(start_time) AS start_gmt5, app_label, requested_server, "
        "c2s_byte_num + s2c_byte_num AS total_bytes, duration, block_type "
        "FROM ntc_ssl_logs WHERE subscriber_id = '03076195547' AND LOWER(app_label) LIKE '%weather%' "
        "ORDER BY start_time LIMIT 50"
    )
    duration_minutes = resolve_followup(
        "also show the duration of each session in minutes",
        [{"question": "what was the time when 03076195547 tried to access weather", "sql": weather_times}],
    )
    minutes_folded = (duration_minutes or "").lower()
    if (
        not duration_minutes
        or "as duration_minutes" not in minutes_folded
        or "duration / 60000" not in minutes_folded
        or "subscriber_id = '03076195547'" not in minutes_folded
        or "weather" not in minutes_folded
        or "as start_gmt5" not in minutes_folded
    ):
        raise SystemExit(f"duration in minutes was not added to the access-time list: {duration_minutes}")
    prepare(duration_minutes, 200, allow_full_scan=True)
    nordvpn_user = (
        "SELECT subscriber_id, app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "COUNT(*) AS flows FROM ntc_ssl_logs WHERE subscriber_id = '03334128404ASD' "
        "AND LOWER(app_label) IN ('nordvpn', 'vpn_connect') "
        "AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY subscriber_id, app_label ORDER BY total_bytes DESC LIMIT 200"
    )
    nord_flows = resolve_followup(
        "display all the flows along with the timestamps and durations in minutes",
        [{"question": "all the nordvpn connections", "sql": nordvpn_user}],
    )
    nord_folded = (nord_flows or "").lower()
    if (
        not nord_flows
        or "as start_gmt5" not in nord_folded
        or "as duration_minutes" not in nord_folded
        or "subscriber_id = '03334128404asd'" not in nord_folded
        or "nordvpn" not in nord_folded
        or "group by" in nord_folded
    ):
        raise SystemExit(f"NordVPN flow times dropped the app filter or minutes: {nord_flows}")
    prepare(nord_flows, 200, allow_full_scan=True)
    with_end = resolve_followup(
        "also display the end time of each flow",
        [{"question": "flows with timestamps", "sql": nord_flows}],
    )
    end_folded = (with_end or "").lower()
    if (
        not with_end
        or "as end_gmt5" not in end_folded
        or "as start_gmt5" not in end_folded
        or "nordvpn" not in end_folded
        or "as duration_minutes" not in end_folded
    ):
        raise SystemExit(f"end time was not added to the flow list: {with_end}")
    prepare(with_end, 200, allow_full_scan=True)
    times_folded = (access_times or "").lower()
    if (
        not access_times
        or "from_unixtime(start_time)" not in times_folded
        or "as start_gmt5" not in times_folded
        or "as duration_seconds" not in times_folded
        or "subscriber_id = '210317380'" not in times_folded
        or "xhamster" not in times_folded
        or re.search(r"start_time\s*(?:>=|<=|=|>|<)", times_folded)
    ):
        raise SystemExit(f"access times for one subscriber were not listed: {access_times}")
    prepare(access_times, 200, allow_full_scan=True)
    from app.answer_check import check_sql
    from app.learned import saved_sql, sql_needs_full_scan

    nayatel_apps = (
        "SELECT app_label, COUNT(*) AS blocked_flows FROM ntc_ssl_logs "
        "WHERE (block_type IS NOT NULL AND block_type <> 0) AND LOWER(client_asn) LIKE '%nayatel%' "
        "GROUP BY app_label ORDER BY blocked_flows DESC LIMIT 10"
    )
    twa_learned = {
        "question": "also show the corresponding number of unique users",
        "sql": (
            "SELECT app_label, COUNT(*) AS blocked_flows, "
            "COUNT(DISTINCT CASE WHEN IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
            "THEN subscriber_id END) AS users FROM ntc_ssl_logs "
            "WHERE (block_type IS NOT NULL AND block_type <> 0) AND LOWER(client_asn) LIKE '%twa%' "
            "GROUP BY app_label ORDER BY blocked_flows DESC LIMIT 10"
        ),
        "learned": True,
        "full_scan": True,
    }
    if saved_sql([twa_learned], "also show the corresponding number of unique users"):
        raise SystemExit("a relative follow-up was replayed as a saved answer with no history")
    if saved_sql(
        [twa_learned],
        "also show the corresponding number of unique users",
        [{"question": "nayatel blocked apps", "sql": nayatel_apps}],
    ):
        raise SystemExit("a saved follow-up overrode the current Nayatel thread")
    with_nayatel_users = resolve_followup(
        "also show the corresponding number of unique users",
        [{"question": "nayatel blocked apps", "sql": nayatel_apps}],
    )
    nayatel_folded = (with_nayatel_users or "").lower()
    if (
        not with_nayatel_users
        or "as users" not in nayatel_folded
        or "like '%nayatel%'" not in nayatel_folded
        or "like '%twa%'" in nayatel_folded
        or "group by app_label" not in nayatel_folded
    ):
        raise SystemExit(f"unique users dropped the Nayatel filter: {with_nayatel_users}")
    prepare(with_nayatel_users, 200, allow_full_scan=True)
    cyber_karachi = (
        "SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs "
        "WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "AND (LOWER(loc) LIKE '%karachi%' OR LOWER(loc) LIKE '%khi%') "
        "AND (LOWER(client_asn) LIKE '%stormfiber%' OR LOWER(client_asn) LIKE '%cybernet%') "
        "GROUP BY subscriber_id ORDER BY flows DESC LIMIT 20"
    )
    twa_karachi = resolve_followup(
        "show me the top 20 twa users from karachi by flow count",
        [{"question": "cybernet karachi users", "sql": cyber_karachi}],
    )
    twa_folded = (twa_karachi or "").lower()
    if (
        not twa_karachi
        or "like '%twa%'" not in twa_folded
        or "like '%transworld%'" not in twa_folded
        or "like '%cybernet%'" in twa_folded
        or "like '%stormfiber%'" in twa_folded
        or "like '%karachi%'" not in twa_folded
        or "group by subscriber_id" not in twa_folded
    ):
        raise SystemExit(f"TWA Karachi kept the Cybernet filter: {twa_karachi}")
    prepare(twa_karachi, 200, allow_full_scan=True)
    site_apps = (
        "SELECT requested_server, app_label, COUNT(*) AS flows, "
        "SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
        "FROM ntc_ssl_logs WHERE subscriber_id = '923000953218' "
        "AND IFNULL(requested_server, '') <> '' AND IFNULL(app_label, '') <> '' "
        "GROUP BY requested_server, app_label ORDER BY total_bytes DESC LIMIT 50"
    )
    activity_times = resolve_followup(
        "also show the activity timings",
        [{"question": "apps and websites accessed by 923000953218", "sql": site_apps}],
    )
    activity_folded = (activity_times or "").lower()
    if (
        not activity_times
        or "as start_gmt5" not in activity_folded
        or "as end_gmt5" not in activity_folded
        or "as duration_seconds" not in activity_folded
        or "subscriber_id = '923000953218'" not in activity_folded
        or "group by" in activity_folded
    ):
        raise SystemExit(f"activity timings were not expanded to flow times: {activity_times}")
    prepare(activity_times, 200, allow_full_scan=True)
    longest = resolve_followup("show me the top 50 flows by duration", [])
    longest_folded = (longest or "").lower()
    if (
        not longest
        or "order by duration desc" not in longest_folded
        or "limit 50" not in longest_folded
        or "as duration_seconds" not in longest_folded
        or "as start_gmt5" not in longest_folded
        or "subscriber_id = 'duration'" in longest_folded
        or "group by" in longest_folded
    ):
        raise SystemExit(f"top flows by duration were not ranked: {longest}")
    prepare(longest, 200, allow_full_scan=True)
    from app.followup import named_subscriber

    if named_subscriber("show me the top 50 flows by duration"):
        raise SystemExit("duration was treated as a subscriber id")
    valid_flows = resolve_followup(
        "only show those flows that have valid users",
        [{"question": "top 50 flows by duration", "sql": longest}],
    )
    valid_folded = (valid_flows or "").lower()
    if (
        not valid_flows
        or "locate('_', subscriber_id) = 0" not in valid_folded
        or "ifnull(subscriber_id, '') <> ''" not in valid_folded
        or "order by duration desc" not in valid_folded
        or (
            "as duration_seconds" not in valid_folded
            and "as duration_minutes" not in valid_folded
        )
    ):
        raise SystemExit(f"valid-user filter was not applied to the flow list: {valid_flows}")
    prepare(valid_flows, 200, allow_full_scan=True)
    with_isp = resolve_followup(
        "also show the corresponding ISP",
        [{"question": "only include the valid users", "sql": valid_flows}],
    )
    isp_folded = (with_isp or "").lower()
    if (
        not with_isp
        or "client_asn" not in isp_folded
        or "order by duration desc" not in isp_folded
        or "locate('_', subscriber_id) = 0" not in isp_folded
        or "group_concat" in isp_folded
    ):
        raise SystemExit(f"ISP was not added beside the flow list: {with_isp}")
    prepare(with_isp, 200, allow_full_scan=True)
    app_duration = (
        "SELECT app_label, SUM(duration) AS total_duration FROM ntc_ssl_logs "
        "WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY app_label ORDER BY total_duration DESC LIMIT 10"
    )
    duration_hours = resolve_followup(
        "show the duration in hours. Also show the corresponding number of sessions",
        [{"question": "top apps by session duration", "sql": app_duration}],
    )
    hours_folded = (duration_hours or "").lower()
    if (
        not duration_hours
        or "as total_duration_hours" not in hours_folded
        or "sum(duration) / 3600000" not in hours_folded
        or "as sessions" not in hours_folded
        or "group by app_label" not in hours_folded
        or "order by total_duration_hours" not in hours_folded
    ):
        raise SystemExit(f"app duration was not converted to hours with sessions: {duration_hours}")
    prepare(duration_hours, 200, allow_full_scan=True)
    hours_only = (
        "SELECT app_label, ROUND(SUM(duration) / 3600000, 2) AS total_duration_hours "
        "FROM ntc_ssl_logs WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY app_label ORDER BY total_duration_hours DESC LIMIT 10"
    )
    with_flows = resolve_followup(
        "show the corresponding number of flows",
        [{"question": "duration in hours", "sql": hours_only}],
    )
    flows_folded = (with_flows or "").lower()
    if (
        not with_flows
        or "as total_duration_hours" not in flows_folded
        or ("as flows" not in flows_folded and "as flow_count" not in flows_folded and "as sessions" not in flows_folded)
        or "order by total_duration_hours" not in flows_folded
        or "group by app_label" not in flows_folded
    ):
        raise SystemExit(f"flow count replaced the duration ranking: {with_flows}")
    prepare(with_flows, 200, allow_full_scan=True)
    hours_sessions = (
        "SELECT app_label, ROUND(SUM(duration) / 3600000, 2) AS total_duration_hours, "
        "COUNT(*) AS sessions FROM ntc_ssl_logs "
        "WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY app_label ORDER BY total_duration_hours DESC LIMIT 10"
    )
    with_volume = resolve_followup(
        "also show the respective traffic volume",
        [{"question": "duration hours with sessions", "sql": hours_sessions}],
    )
    volume_folded = (with_volume or "").lower()
    if (
        not with_volume
        or "as total_duration_hours" not in volume_folded
        or "as sessions" not in volume_folded
        or "c2s_byte_num + s2c_byte_num" not in volume_folded
        or "as traffic_volume" not in volume_folded
        or "order by total_duration_hours" not in volume_folded
        or "group by app_label" not in volume_folded
    ):
        raise SystemExit(f"traffic volume dropped duration ranking: {with_volume}")
    prepare(with_volume, 200, allow_full_scan=True)
    hours_flows_volume = (
        "SELECT app_label, ROUND(SUM(duration) / 3600000, 2) AS total_duration_hours, "
        "COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS traffic_volume "
        "FROM ntc_ssl_logs WHERE IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0 "
        "GROUP BY app_label ORDER BY total_duration_hours DESC LIMIT 10"
    )
    volume_gb = resolve_followup(
        "show the traffic volume in Gigabytes",
        [{"question": "with traffic volume", "sql": hours_flows_volume}],
    )
    gb_folded = (volume_gb or "").lower()
    if (
        not volume_gb
        or "as total_duration_hours" not in gb_folded
        or "as flows" not in gb_folded
        or "as traffic_volume_gb" not in gb_folded
        or "/ 1073741824" not in volume_gb
        or "order by total_duration_hours" not in gb_folded
        or "order by traffic_volume" in gb_folded
    ):
        raise SystemExit(f"GB conversion re-ranked by traffic: {volume_gb}")
    prepare(volume_gb, 200, allow_full_scan=True)

    if not sql_needs_full_scan(access_times):
        raise SystemExit("SELECT FROM_UNIXTIME(start_time) was treated as a time filter")
    rejected = check_sql(
        "at what time did 210317380 try to access xhamster",
        access_times,
        [{"question": "other activities", "sql": "SELECT app_label FROM ntc_ssl_logs WHERE subscriber_id = '210317380' GROUP BY app_label"}],
        True,
    )
    if rejected:
        raise SystemExit(f"listing access times was blocked by the SQL check: {rejected}")
    print(f"ok examples={len(examples)} sql={sql_count} clarify={len(examples) - sql_count}")


if __name__ == "__main__":
    main()
