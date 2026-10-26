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
    users = resolve_followup(
        "also show the corresponding number of unique users against each app",
        [{"question": "gigabytes", "sql": gigabytes}],
    )
    if (
        not users
        or "count(distinct case when" not in users.lower()
        or " as users" not in users.lower()
        or "flow_count" not in users.lower()
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
        or "loc like '%lahore%'" not in lahore.lower()
        or "loc = '" in lahore.lower()
        or "app_label" in lahore.lower()
        or "limit 10" not in lahore.lower()
        or "limit 20" in lahore.lower()
        or "client_asn" not in lahore.lower()
        or "client_location" in lahore.lower()
    ):
        raise SystemExit(f"Lahore was not matched inside the address: {lahore}")
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
    print(f"ok examples={len(examples)} sql={sql_count} clarify={len(examples) - sql_count}")


if __name__ == "__main__":
    main()
