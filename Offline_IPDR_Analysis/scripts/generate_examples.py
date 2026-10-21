"""Build the question/SQL bank used as retrieved examples. Run from the project root."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.schema import (
    ALLOWED_SQL,
    BLOCKED_SQL,
    DEAD_COLUMNS,
    FIXED_SQL,
    IDENTIFIED_SQL,
    MOBILE_SQL,
    STARTER_QUESTIONS,
    UNIDENTIFIED_SQL,
)

MAPPING = ROOT / "data" / "app_category_to_app_label_mapping.csv"
OUTPUT = ROOT / "data" / "training_examples.jsonl"

WINDOWS = [
    ("today", "start_time >= UNIX_TIMESTAMP(CURDATE()) AND start_time < UNIX_TIMESTAMP(CURDATE() + INTERVAL 1 DAY)"),
    ("yesterday", "start_time >= UNIX_TIMESTAMP(CURDATE() - INTERVAL 1 DAY) AND start_time < UNIX_TIMESTAMP(CURDATE())"),
    ("in the last hour", "start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 1 HOUR)"),
    ("in the last 6 hours", "start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 6 HOUR)"),
    ("in the last 24 hours", "start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 24 HOUR)"),
    ("in the last 7 days", "start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 7 DAY)"),
    ("on 28 September 2026", "start_time >= UNIX_TIMESTAMP('2026-09-28 00:00:00') AND start_time < UNIX_TIMESTAMP('2026-09-29 00:00:00')"),
    ("between 19:00 and 20:00 on 28 September 2026", "start_time >= UNIX_TIMESTAMP('2026-09-28 19:00:00') AND start_time < UNIX_TIMESTAMP('2026-09-28 20:00:00')"),
]

APP_NAMES = [
    "whatsapp", "facebook", "messenger", "tiktok", "youtube", "instagram", "snapchat",
    "telegram", "imo", "zoom", "gmail", "google", "netflix", "spotify", "pubg", "freefire",
    "chatgpt", "openai", "viber", "wechat", "discord", "twitter", "bing", "icloud",
    "amazon", "apple", "signal", "skype", "teams", "reddit", "wikipedia", "cloudflare",
    "doubleclick", "shopify", "yandex", "baidu", "vivo", "chartboost", "crashlytics",
]

DETAIL = (
    "FROM_UNIXTIME(start_time) AS start_gmt5, subscriber_id, app_category, app_label, "
    "requested_server, client_ip, server_ip, server_port, c2s_byte_num, s2c_byte_num, "
    "duration, block_type, l4_protocol"
)


def main() -> None:
    labels = _load_labels()
    categories = sorted({category for _, category in labels if category})
    items: list[dict] = []
    seen: set[str] = set()

    def add(question: str, sql: str | None, tags: list[str], clarify: str | None = None, full_scan: bool = False) -> None:
        key = question.strip().lower()
        if key in seen:
            return
        seen.add(key)
        items.append(
            {
                "question": question.strip(),
                "sql": " ".join(sql.split()) if sql else None,
                "clarify": clarify,
                "tags": tags,
                "full_scan": full_scan,
            }
        )

    _exact_starters(add)
    _volumes(add)
    _rankings(add)
    _categories(add, categories)
    _named_apps(add, labels)
    _subscribers_and_network(add)
    _mobile_fixed(add)
    _location(add)
    _time_shapes(add)
    _paraphrases(add)
    _full_table(add)
    _unavailable(add)
    missing = [question for question in STARTER_QUESTIONS if question.lower() not in seen]
    if missing:
        raise SystemExit("Starter questions missing from the bank:\n" + "\n".join(missing))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"wrote {len(items)} examples to {OUTPUT}")


def _exact_starters(add) -> None:
    yesterday = WINDOWS[1][1]
    last_24 = WINDOWS[4][1]
    last_7 = WINDOWS[5][1]
    last_6 = WINDOWS[3][1]
    today = WINDOWS[0][1]
    add(
        "How many flows started yesterday?",
        f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {yesterday}",
        ["starter", "count"],
    )
    add(
        "Top 15 app labels by total bytes over the last 24 hours",
        f"SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
        f"WHERE {last_24} GROUP BY app_label ORDER BY total_bytes DESC LIMIT 15",
        ["starter", "top", "bytes"],
    )
    add(
        "How many blocked flows were there yesterday?",
        f"SELECT COUNT(*) AS blocked_flows FROM ntc_ssl_logs WHERE {yesterday} AND {BLOCKED_SQL}",
        ["starter", "blocked"],
    )
    add(
        "How much traffic did TikTok generate yesterday, in MB?",
        f"SELECT ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
        f"ROUND(SUM(c2s_byte_num) / 1048576, 2) AS upload_mb, "
        f"ROUND(SUM(s2c_byte_num) / 1048576, 2) AS download_mb "
        f"FROM ntc_ssl_logs WHERE {yesterday} AND LOWER(app_label) LIKE '%tiktok%'",
        ["starter", "tiktok"],
    )
    add(
        "Top subscribers by download bytes yesterday",
        f"SELECT subscriber_id, SUM(s2c_byte_num) AS download_bytes, SUM(c2s_byte_num) AS upload_bytes "
        f"FROM ntc_ssl_logs WHERE {yesterday} AND {IDENTIFIED_SQL} "
        f"GROUP BY subscriber_id ORDER BY download_bytes DESC LIMIT 20",
        ["starter", "subscriber"],
    )
    add(
        "How many mobile flows and how many fixed-line flows started yesterday?",
        f"SELECT SUM(CASE WHEN {MOBILE_SQL} THEN 1 ELSE 0 END) AS mobile_flows, "
        f"SUM(CASE WHEN {FIXED_SQL} THEN 1 ELSE 0 END) AS fixed_line_flows, "
        f"COUNT(*) AS all_flows FROM ntc_ssl_logs WHERE {yesterday}",
        ["starter", "mobile", "fixed"],
    )
    add(
        "Which domains did subscriber 923000000000 request in the last 7 days?",
        f"SELECT requested_server, COUNT(*) AS flows, SUM(s2c_byte_num) AS download_bytes FROM ntc_ssl_logs "
        f"WHERE {last_7} AND subscriber_id IN ('923000000000', '03000000000') AND IFNULL(requested_server, '') <> '' "
        f"GROUP BY requested_server ORDER BY flows DESC LIMIT 50",
        ["starter", "subscriber", "domain"],
    )
    add(
        "How many unidentified sessions started in the last 24 hours?",
        f"SELECT COUNT(*) AS unidentified_flows FROM ntc_ssl_logs WHERE {last_24} AND {UNIDENTIFIED_SQL}",
        ["starter", "unidentified"],
    )
    add(
        "Flows longer than 60 seconds in the last 6 hours",
        f"SELECT {DETAIL} FROM ntc_ssl_logs WHERE {last_6} AND duration >= 60000 ORDER BY duration DESC LIMIT 100",
        ["starter", "duration"],
    )
    add(
        "Hourly flow count and total bytes for today",
        "SELECT DATE_FORMAT(FROM_UNIXTIME(start_time), '%Y-%m-%d %H:00') AS hour_gmt5, "
        "COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
        f"WHERE {today} GROUP BY DATE_FORMAT(FROM_UNIXTIME(start_time), '%Y-%m-%d %H:00') ORDER BY hour_gmt5",
        ["starter", "hourly"],
    )


def _volumes(add) -> None:
    for phrase, window in WINDOWS:
        add(f"How many flows started {phrase}?", f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {window}", ["count", "flows", phrase])
        add(f"How many sessions were recorded {phrase}?", f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {window}", ["count", "sessions", phrase])
        add(
            f"What is the total bytes {phrase}?",
            f"SELECT SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
            f"ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
            f"FROM ntc_ssl_logs WHERE {window}",
            ["bytes", phrase],
        )
        add(
            f"How many distinct subscribers were active {phrase}?",
            f"SELECT COUNT(DISTINCT subscriber_id) AS subscribers FROM ntc_ssl_logs "
            f"WHERE {window} AND {IDENTIFIED_SQL}",
            ["subscribers", "distinct", phrase],
        )
        add(
            f"How many blocked flows were there {phrase}?",
            f"SELECT COUNT(*) AS blocked_flows FROM ntc_ssl_logs WHERE {window} AND {BLOCKED_SQL}",
            ["blocked", phrase],
        )
        add(
            f"How many allowed flows were there {phrase}?",
            f"SELECT COUNT(*) AS allowed_flows FROM ntc_ssl_logs WHERE {window} AND {ALLOWED_SQL}",
            ["allowed", phrase],
        )
        add(
            f"How many unidentified sessions started {phrase}?",
            f"SELECT COUNT(*) AS unidentified_flows FROM ntc_ssl_logs WHERE {window} AND {UNIDENTIFIED_SQL}",
            ["unidentified", phrase],
        )
        add(
            f"How many identified subscribers started a flow {phrase}?",
            f"SELECT COUNT(DISTINCT subscriber_id) AS identified_subscribers FROM ntc_ssl_logs "
            f"WHERE {window} AND {IDENTIFIED_SQL}",
            ["identified", phrase],
        )


def _rankings(add) -> None:
    phrases = [WINDOWS[1], WINDOWS[4], WINDOWS[5], WINDOWS[6]]
    for phrase, window in phrases:
        add(
            f"Top 15 app labels by flow count {phrase}",
            f"SELECT app_label, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {window} "
            f"GROUP BY app_label ORDER BY flows DESC LIMIT 15",
            ["top", "app_label", phrase],
        )
        add(
            f"Top 15 app labels by total bytes {phrase}",
            f"SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
            f"WHERE {window} GROUP BY app_label ORDER BY total_bytes DESC LIMIT 15",
            ["top", "bytes", "app_label", phrase],
        )
        add(
            f"Which applications used the most bandwidth {phrase}?",
            f"SELECT app_label, ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
            f"FROM ntc_ssl_logs WHERE {window} GROUP BY app_label ORDER BY total_mb DESC LIMIT 15",
            ["top", "mb", phrase],
        )
        add(
            f"Top 10 app categories by flow count {phrase}",
            f"SELECT app_category, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {window} "
            f"GROUP BY app_category ORDER BY flows DESC LIMIT 10",
            ["top", "category", phrase],
        )
        add(
            f"Top 20 domains {phrase}",
            f"SELECT requested_server, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
            f"FROM ntc_ssl_logs WHERE {window} AND IFNULL(requested_server, '') <> '' "
            f"GROUP BY requested_server ORDER BY flows DESC LIMIT 20",
            ["top", "domain", "sni", phrase],
        )
        add(
            f"Top 20 client IP addresses {phrase}",
            f"SELECT client_ip, COUNT(*) AS flows, SUM(s2c_byte_num) AS download_bytes "
            f"FROM ntc_ssl_logs WHERE {window} GROUP BY client_ip ORDER BY flows DESC LIMIT 20",
            ["top", "client_ip", phrase],
        )
        add(
            f"Top 20 server IP addresses by bytes {phrase}",
            f"SELECT server_ip, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
            f"FROM ntc_ssl_logs WHERE {window} GROUP BY server_ip ORDER BY total_bytes DESC LIMIT 20",
            ["top", "server_ip", phrase],
        )
        add(
            f"Top subscribers by download bytes {phrase}",
            f"SELECT subscriber_id, SUM(s2c_byte_num) AS download_bytes, SUM(c2s_byte_num) AS upload_bytes "
            f"FROM ntc_ssl_logs WHERE {window} AND {IDENTIFIED_SQL} "
            f"GROUP BY subscriber_id ORDER BY download_bytes DESC LIMIT 20",
            ["top", "subscriber", "download", phrase],
        )
        add(
            f"Top subscribers by flow count {phrase}",
            f"SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {window} AND {IDENTIFIED_SQL} "
            f"GROUP BY subscriber_id ORDER BY flows DESC LIMIT 20",
            ["top", "subscriber", phrase],
        )
        add(
            f"Top client ASN names {phrase}",
            f"SELECT client_asn, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {window} AND IFNULL(client_asn, '') <> '' "
            f"GROUP BY client_asn ORDER BY flows DESC LIMIT 15",
            ["top", "asn", "client", phrase],
        )
        add(
            f"Top server ASN names by bytes {phrase}",
            f"SELECT server_asn, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
            f"WHERE {window} AND IFNULL(server_asn, '') <> '' GROUP BY server_asn ORDER BY total_bytes DESC LIMIT 15",
            ["top", "asn", "server", phrase],
        )
        add(
            f"Which protocols were used {phrase}?",
            f"SELECT l4_protocol, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
            f"FROM ntc_ssl_logs WHERE {window} GROUP BY l4_protocol ORDER BY flows DESC",
            ["protocol", "tcp", "udp", phrase],
        )


def _categories(add, categories: list[str]) -> None:
    yesterday = WINDOWS[1][1]
    last_24 = WINDOWS[4][1]
    last_7 = WINDOWS[5][1]
    for category in categories:
        spoken = category.replace("_", " ")
        add(
            f"How many {spoken} flows started yesterday?",
            f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {yesterday} AND LOWER(app_category) = '{category}'",
            ["category", category],
        )
        add(
            f"Total megabytes for {spoken} in the last 24 hours",
            f"SELECT ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
            f"FROM ntc_ssl_logs WHERE {last_24} AND LOWER(app_category) = '{category}'",
            ["category", "bytes", category],
        )
        add(
            f"Top app labels in {spoken} over the last 7 days",
            f"SELECT app_label, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
            f"FROM ntc_ssl_logs WHERE {last_7} AND LOWER(app_category) = '{category}' "
            f"GROUP BY app_label ORDER BY total_bytes DESC LIMIT 20",
            ["category", "top", category],
        )


def _named_apps(add, labels: list[tuple[str, str]]) -> None:
    yesterday = WINDOWS[1][1]
    last_24 = WINDOWS[4][1]
    last_7 = WINDOWS[5][1]
    for name in APP_NAMES:
        matched = [(label, category) for label, category in labels if name in label.lower()]
        if not matched:
            predicate = f"LOWER(app_label) LIKE '%{name}%'"
        elif len(matched) > 20:
            predicate = f"LOWER(app_label) LIKE '%{name}%'"
        else:
            values = ", ".join("'" + label.lower().replace("'", "''") + "'" for label, _ in matched)
            predicate = f"LOWER(app_label) IN ({values})"
        add(
            f"How many {name} flows started yesterday?",
            f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {yesterday} AND {predicate}",
            ["app", name],
        )
        add(
            f"How much traffic did {name} generate yesterday, in MB?",
            f"SELECT ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, "
            f"ROUND(SUM(c2s_byte_num) / 1048576, 2) AS upload_mb, "
            f"ROUND(SUM(s2c_byte_num) / 1048576, 2) AS download_mb "
            f"FROM ntc_ssl_logs WHERE {yesterday} AND {predicate}",
            ["app", "bytes", name],
        )
        add(
            f"How many distinct subscribers used {name} in the last 7 days?",
            f"SELECT COUNT(DISTINCT subscriber_id) AS subscribers FROM ntc_ssl_logs "
            f"WHERE {last_7} AND {predicate} AND {IDENTIFIED_SQL}",
            ["app", "subscribers", name],
        )
        add(
            f"Top domains for {name} in the last 24 hours",
            f"SELECT requested_server, COUNT(*) AS flows FROM ntc_ssl_logs "
            f"WHERE {last_24} AND {predicate} AND IFNULL(requested_server, '') <> '' "
            f"GROUP BY requested_server ORDER BY flows DESC LIMIT 20",
            ["app", "domain", name],
        )
        add(
            f"What app categories does {name} fall into, based on yesterday?",
            f"SELECT LOWER(app_label) AS app_label, LOWER(app_category) AS app_category, COUNT(*) AS flows "
            f"FROM ntc_ssl_logs WHERE {yesterday} AND {predicate} "
            f"GROUP BY LOWER(app_label), LOWER(app_category) ORDER BY flows DESC LIMIT 30",
            ["app", "category", name],
        )


def _subscribers_and_network(add) -> None:
    y = WINDOWS[1][1]
    d7 = WINDOWS[5][1]
    h24 = WINDOWS[4][1]
    h6 = WINDOWS[3][1]
    both = "subscriber_id IN ('923001234567', '03001234567')"
    add(
        "Show the flows for subscriber 923001234567 yesterday",
        f"SELECT {DETAIL} FROM ntc_ssl_logs WHERE {y} AND {both} ORDER BY start_time LIMIT 100",
        ["subscriber", "list"],
    )
    add(
        "Which apps did subscriber 03001234567 use in the last 7 days?",
        f"SELECT app_label, app_category, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        f"FROM ntc_ssl_logs WHERE {d7} AND {both} GROUP BY app_label, app_category ORDER BY total_bytes DESC LIMIT 30",
        ["subscriber", "apps"],
    )
    add(
        "Which domains did subscriber 923000000000 request in the last 7 days?",
        f"SELECT requested_server, COUNT(*) AS flows, SUM(s2c_byte_num) AS download_bytes FROM ntc_ssl_logs "
        f"WHERE {d7} AND subscriber_id IN ('923000000000', '03000000000') AND IFNULL(requested_server, '') <> '' "
        f"GROUP BY requested_server ORDER BY flows DESC LIMIT 50",
        ["subscriber", "domain"],
    )
    add(
        "Is subscriber 923001234567 mobile or fixed-line in the last 7 days?",
        f"SELECT subscriber_id, "
        f"SUM(CASE WHEN {MOBILE_SQL} THEN 1 ELSE 0 END) AS mobile_flows, "
        f"SUM(CASE WHEN {FIXED_SQL} THEN 1 ELSE 0 END) AS fixed_line_flows, "
        f"COUNT(*) AS flows FROM ntc_ssl_logs WHERE {d7} AND {both} GROUP BY subscriber_id",
        ["subscriber", "mobile", "fixed"],
    )
    add(
        "What IMEI and IMSI values are recorded for subscriber 923001234567 in the last 7 days?",
        f"SELECT imei, imsi, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {d7} AND {both} "
        f"AND (IFNULL(imei, '') <> '' OR IFNULL(imsi, '') <> '') GROUP BY imei, imsi ORDER BY flows DESC LIMIT 20",
        ["imei", "imsi", "subscriber"],
    )
    add(
        "Which subscriber ids used IMEI 350000000000000 in the last 7 days?",
        f"SELECT subscriber_id, imsi, COUNT(*) AS flows FROM ntc_ssl_logs "
        f"WHERE {d7} AND imei = '350000000000000' GROUP BY subscriber_id, imsi ORDER BY flows DESC LIMIT 20",
        ["imei"],
    )
    add(
        "Which subscriber ids used IMSI 410000000000000 yesterday?",
        f"SELECT subscriber_id, imei, COUNT(*) AS flows FROM ntc_ssl_logs "
        f"WHERE {y} AND imsi = '410000000000000' GROUP BY subscriber_id, imei ORDER BY flows DESC LIMIT 20",
        ["imsi"],
    )
    add(
        "Show flows from client IP 203.0.113.10 in the last 24 hours",
        f"SELECT {DETAIL} FROM ntc_ssl_logs WHERE {h24} AND client_ip = '203.0.113.10' ORDER BY start_time LIMIT 100",
        ["client_ip"],
    )
    add(
        "How much traffic went to server IP 203.0.113.20 yesterday?",
        f"SELECT COUNT(*) AS flows, SUM(c2s_byte_num) AS upload_bytes, SUM(s2c_byte_num) AS download_bytes "
        f"FROM ntc_ssl_logs WHERE {y} AND server_ip = '203.0.113.20'",
        ["server_ip"],
    )
    add(
        "Show flows whose SNI or domain contains accounts.google.com yesterday",
        f"SELECT {DETAIL} FROM ntc_ssl_logs WHERE {y} AND LOWER(requested_server) LIKE '%accounts.google.com%' "
        f"ORDER BY start_time LIMIT 100",
        ["domain", "sni"],
    )
    add(
        "How many flows used server port 443 in the last 24 hours?",
        f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {h24} AND server_port = 443",
        ["port"],
    )
    add(
        "Breakdown of server ports for the last 24 hours",
        f"SELECT server_port, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        f"FROM ntc_ssl_logs WHERE {h24} GROUP BY server_port ORDER BY flows DESC LIMIT 30",
        ["port"],
    )
    add(
        "How many UDP flows and how many TCP flows started yesterday?",
        f"SELECT l4_protocol, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} GROUP BY l4_protocol",
        ["protocol"],
    )
    add(
        "Flows longer than 60 seconds in the last 6 hours",
        f"SELECT {DETAIL} FROM ntc_ssl_logs WHERE {h6} AND duration >= 60000 ORDER BY duration DESC LIMIT 100",
        ["duration"],
    )
    add(
        "What is the average flow duration in seconds for yesterday?",
        f"SELECT ROUND(AVG(duration) / 1000, 2) AS avg_duration_seconds FROM ntc_ssl_logs WHERE {y}",
        ["duration", "average"],
    )
    add(
        "Which identified subscribers uploaded more than they downloaded yesterday?",
        f"SELECT subscriber_id, SUM(c2s_byte_num) AS upload_bytes, SUM(s2c_byte_num) AS download_bytes "
        f"FROM ntc_ssl_logs WHERE {y} AND {IDENTIFIED_SQL} GROUP BY subscriber_id "
        f"HAVING SUM(c2s_byte_num) > SUM(s2c_byte_num) ORDER BY upload_bytes DESC LIMIT 20",
        ["upload", "download"],
    )
    add(
        "Show upload and download bytes by app category for yesterday",
        f"SELECT app_category, SUM(c2s_byte_num) AS upload_bytes, SUM(s2c_byte_num) AS download_bytes "
        f"FROM ntc_ssl_logs WHERE {y} GROUP BY app_category ORDER BY download_bytes DESC",
        ["upload", "download", "category"],
    )
    add(
        "Which subscribers used both tiktok and facebook yesterday?",
        f"SELECT subscriber_id FROM ntc_ssl_logs WHERE {y} AND {IDENTIFIED_SQL} "
        f"AND (LOWER(app_label) LIKE '%tiktok%' OR LOWER(app_label) LIKE '%facebook%') "
        f"GROUP BY subscriber_id "
        f"HAVING SUM(CASE WHEN LOWER(app_label) LIKE '%tiktok%' THEN 1 ELSE 0 END) > 0 "
        f"AND SUM(CASE WHEN LOWER(app_label) LIKE '%facebook%' THEN 1 ELSE 0 END) > 0 "
        f"LIMIT 50",
        ["subscriber", "tiktok", "facebook"],
    )
    add(
        "Look up the flow with id 1",
        f"SELECT {DETAIL}, id FROM ntc_ssl_logs WHERE id = 1",
        ["id"],
    )
    add(
        "Look up log id 10000000000000001",
        f"SELECT {DETAIL}, log_id FROM ntc_ssl_logs WHERE log_id = '10000000000000001'",
        ["log_id"],
    )
    add(
        "How many packets were exchanged yesterday?",
        f"SELECT SUM(c2s_pkt_num) AS upload_packets, SUM(s2c_pkt_num) AS download_packets "
        f"FROM ntc_ssl_logs WHERE {y}",
        ["packets"],
    )
    add(
        "Top blocked app labels yesterday",
        f"SELECT app_label, COUNT(*) AS blocked_flows FROM ntc_ssl_logs WHERE {y} AND {BLOCKED_SQL} "
        f"GROUP BY app_label ORDER BY blocked_flows DESC LIMIT 20",
        ["blocked", "app"],
    )
    add(
        "Block type breakdown for yesterday",
        f"SELECT block_type, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} GROUP BY block_type ORDER BY flows DESC",
        ["blocked"],
    )


def _mobile_fixed(add) -> None:
    y = WINDOWS[1][1]
    h24 = WINDOWS[4][1]
    d7 = WINDOWS[5][1]
    add(
        "How many mobile flows and how many fixed-line flows started yesterday?",
        f"SELECT SUM(CASE WHEN {MOBILE_SQL} THEN 1 ELSE 0 END) AS mobile_flows, "
        f"SUM(CASE WHEN {FIXED_SQL} THEN 1 ELSE 0 END) AS fixed_line_flows, "
        f"COUNT(*) AS all_flows FROM ntc_ssl_logs WHERE {y}",
        ["mobile", "fixed"],
    )
    add(
        "Top app labels used by mobile subscribers yesterday",
        f"SELECT app_label, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND {MOBILE_SQL} "
        f"GROUP BY app_label ORDER BY flows DESC LIMIT 20",
        ["mobile", "app"],
    )
    add(
        "Top app labels used by fixed-line subscribers in the last 24 hours",
        f"SELECT app_label, COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes "
        f"FROM ntc_ssl_logs WHERE {h24} AND {FIXED_SQL} GROUP BY app_label ORDER BY total_bytes DESC LIMIT 20",
        ["fixed", "app"],
    )
    add(
        "How many distinct mobile subscribers used WhatsApp in the last 7 days?",
        f"SELECT COUNT(DISTINCT subscriber_id) AS subscribers FROM ntc_ssl_logs "
        f"WHERE {d7} AND {MOBILE_SQL} AND {IDENTIFIED_SQL} AND LOWER(app_label) LIKE '%whatsapp%'",
        ["mobile", "whatsapp"],
    )
    add(
        "List cell ids with the most mobile flows yesterday",
        f"SELECT mcc, mnc, lac, cid, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND {MOBILE_SQL} "
        f"GROUP BY mcc, mnc, lac, cid ORDER BY flows DESC LIMIT 20",
        ["cell", "lac", "cid"],
    )
    add(
        "Show fixed-line street addresses with flow counts yesterday",
        f"SELECT loc, geo, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND {FIXED_SQL} AND IFNULL(loc, '') <> '' "
        f"GROUP BY loc, geo ORDER BY flows DESC LIMIT 20",
        ["fixed", "address"],
    )
    add(
        "How many flows have a numeric-looking subscriber id but are fixed-line, yesterday?",
        f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND {FIXED_SQL} "
        f"AND subscriber_id REGEXP '^[0-9]+$'",
        ["fixed", "numeric"],
    )
    add(
        "Top unidentified client IPs in the last 24 hours",
        f"SELECT client_ip, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {h24} AND {UNIDENTIFIED_SQL} "
        f"GROUP BY client_ip ORDER BY flows DESC LIMIT 20",
        ["unidentified", "client_ip"],
    )


def _location(add) -> None:
    y = WINDOWS[1][1]
    h24 = WINDOWS[4][1]
    for city in ("Lahore", "Karachi", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar", "Quetta"):
        add(
            f"How many flows look related to {city} yesterday, either by IP location or by address?",
            f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND "
            f"(client_location LIKE '%{city}%' OR loc LIKE '%{city}%')",
            ["location", city.lower()],
        )
        add(
            f"Top app labels for client IP locations in {city} in the last 24 hours",
            f"SELECT app_label, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {h24} AND client_location LIKE '%{city}%' "
            f"GROUP BY app_label ORDER BY flows DESC LIMIT 15",
            ["location", "client_location", city.lower()],
        )
    add(
        "How many flows have a client IP location in Pakistan yesterday?",
        f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND client_location LIKE '%Pakistan%'",
        ["location", "country", "ip"],
    )
    add(
        "How many flows used a Pakistan cell (MCC 410) yesterday?",
        f"SELECT COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND mcc = '410'",
        ["mcc", "cell", "pakistan"],
    )
    add(
        "Top server countries by bytes yesterday",
        f"SELECT server_location, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        f"FROM ntc_ssl_logs WHERE {y} AND IFNULL(server_location, '') <> '' "
        f"GROUP BY server_location ORDER BY total_bytes DESC LIMIT 20",
        ["server_location"],
    )
    add(
        "How many flows went to servers whose IP location says United States yesterday?",
        f"SELECT COUNT(*) AS flows, SUM(s2c_byte_num) AS download_bytes FROM ntc_ssl_logs "
        f"WHERE {y} AND server_location LIKE '%United States%'",
        ["server_location", "country"],
    )
    add(
        "Top client IP cities yesterday",
        f"SELECT client_location, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND IFNULL(client_location, '') <> '' "
        f"GROUP BY client_location ORDER BY flows DESC LIMIT 20",
        ["client_location"],
    )
    for asn in ("Telenor", "CMPak", "Pakistan Telecommunication", "Nayatel"):
        add(
            f"How many flows have a client ASN containing {asn} yesterday?",
            f"SELECT COUNT(*) AS flows, COUNT(DISTINCT subscriber_id) AS subscriber_ids FROM ntc_ssl_logs "
            f"WHERE {y} AND client_asn LIKE '%{asn}%'",
            ["asn", "client"],
        )


def _time_shapes(add) -> None:
    add(
        "How many logs were received in the last hour?",
        "SELECT COUNT(*) AS logs_received FROM ntc_ssl_logs WHERE recv_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 1 HOUR)",
        ["recv_time", "received"],
    )
    add(
        "How many flows ended yesterday?",
        "SELECT COUNT(*) AS flows_ended FROM ntc_ssl_logs WHERE end_time >= UNIX_TIMESTAMP(CURDATE() - INTERVAL 1 DAY) "
        "AND end_time < UNIX_TIMESTAMP(CURDATE())",
        ["end_time", "ended"],
    )
    add(
        "Hourly flow count and total bytes for today",
        "SELECT DATE_FORMAT(FROM_UNIXTIME(start_time), '%Y-%m-%d %H:00') AS hour_gmt5, "
        "COUNT(*) AS flows, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
        "WHERE start_time >= UNIX_TIMESTAMP(CURDATE()) AND start_time < UNIX_TIMESTAMP(CURDATE() + INTERVAL 1 DAY) "
        "GROUP BY DATE_FORMAT(FROM_UNIXTIME(start_time), '%Y-%m-%d %H:00') ORDER BY hour_gmt5",
        ["hourly", "trend"],
    )
    add(
        "Daily flow count and total gigabytes for the last 7 days",
        "SELECT DATE(FROM_UNIXTIME(start_time)) AS day_gmt5, COUNT(*) AS flows, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1073741824, 3) AS total_gb FROM ntc_ssl_logs "
        "WHERE start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 7 DAY) "
        "GROUP BY DATE(FROM_UNIXTIME(start_time)) ORDER BY day_gmt5",
        ["daily", "trend"],
    )
    add(
        "First and last flow start for subscriber 923001234567 in the last 7 days",
        "SELECT MIN(FROM_UNIXTIME(start_time)) AS first_start_gmt5, MAX(FROM_UNIXTIME(end_time)) AS last_end_gmt5, "
        "COUNT(*) AS flows FROM ntc_ssl_logs WHERE start_time >= UNIX_TIMESTAMP(NOW() - INTERVAL 7 DAY) "
        "AND subscriber_id IN ('923001234567', '03001234567')",
        ["subscriber", "first", "last"],
    )
    add(
        "Compare yesterday's total bytes with the day before",
        "SELECT SUM(CASE WHEN start_time >= UNIX_TIMESTAMP(CURDATE() - INTERVAL 1 DAY) "
        "AND start_time < UNIX_TIMESTAMP(CURDATE()) THEN c2s_byte_num + s2c_byte_num ELSE 0 END) AS yesterday_bytes, "
        "SUM(CASE WHEN start_time >= UNIX_TIMESTAMP(CURDATE() - INTERVAL 2 DAY) "
        "AND start_time < UNIX_TIMESTAMP(CURDATE() - INTERVAL 1 DAY) THEN c2s_byte_num + s2c_byte_num ELSE 0 END) AS day_before_bytes "
        "FROM ntc_ssl_logs WHERE start_time >= UNIX_TIMESTAMP(CURDATE() - INTERVAL 2 DAY) "
        "AND start_time < UNIX_TIMESTAMP(CURDATE())",
        ["compare", "bytes"],
    )


def _paraphrases(add) -> None:
    y = WINDOWS[1][1]
    h24 = WINDOWS[4][1]
    d7 = WINDOWS[5][1]
    night = (
        "start_time >= UNIX_TIMESTAMP(CURDATE() - INTERVAL 1 DAY + INTERVAL 18 HOUR) "
        "AND start_time < UNIX_TIMESTAMP(CURDATE() + INTERVAL 6 HOUR)"
    )
    add(
        "Who were the biggest downloaders yesterday?",
        f"SELECT subscriber_id, SUM(s2c_byte_num) AS download_bytes FROM ntc_ssl_logs "
        f"WHERE {y} AND {IDENTIFIED_SQL} GROUP BY subscriber_id ORDER BY download_bytes DESC LIMIT 20",
        ["downloaders", "biggest", "subscriber"],
    )
    add(
        "Show the heaviest users by total bandwidth in the last 24 hours",
        f"SELECT subscriber_id, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes FROM ntc_ssl_logs "
        f"WHERE {h24} AND {IDENTIFIED_SQL} GROUP BY subscriber_id ORDER BY total_bytes DESC LIMIT 20",
        ["heaviest", "bandwidth", "subscriber"],
    )
    add(
        "Which subscribers uploaded the most yesterday?",
        f"SELECT subscriber_id, SUM(c2s_byte_num) AS upload_bytes FROM ntc_ssl_logs "
        f"WHERE {y} AND {IDENTIFIED_SQL} GROUP BY subscriber_id ORDER BY upload_bytes DESC LIMIT 20",
        ["uploaders", "subscriber"],
    )
    add(
        "What happened last night?",
        f"SELECT COUNT(*) AS flows, ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb "
        f"FROM ntc_ssl_logs WHERE {night}",
        ["last_night"],
    )
    add(
        "Top app labels last night",
        f"SELECT app_label, COUNT(*) AS flows, SUM(s2c_byte_num) AS download_bytes FROM ntc_ssl_logs "
        f"WHERE {night} GROUP BY app_label ORDER BY download_bytes DESC LIMIT 15",
        ["last_night", "app"],
    )
    add(
        "How many WhatsApp calls were there yesterday?",
        f"SELECT COUNT(*) AS flows, COUNT(DISTINCT subscriber_id) AS subscribers FROM ntc_ssl_logs "
        f"WHERE {y} AND LOWER(app_label) LIKE '%whatsapp%' AND LOWER(app_category) = 'voip'",
        ["whatsapp", "calls", "voip"],
    )
    add(
        "Which subscribers in Lahore used WhatsApp yesterday?",
        f"SELECT subscriber_id, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND {IDENTIFIED_SQL} "
        f"AND LOWER(app_label) LIKE '%whatsapp%' AND (client_location LIKE '%Lahore%' OR loc LIKE '%Lahore%') "
        f"GROUP BY subscriber_id ORDER BY flows DESC LIMIT 50",
        ["lahore", "whatsapp", "subscriber"],
    )
    add(
        "Which handset IMEIs were seen for subscriber 923001234567 in the last 7 days?",
        f"SELECT imei, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {d7} "
        f"AND subscriber_id IN ('923001234567', '03001234567') AND IFNULL(imei, '') <> '' "
        f"GROUP BY imei ORDER BY flows DESC LIMIT 10",
        ["handset", "imei"],
    )
    add(
        "Which SIM IMSIs were seen for phone number 03001234567 in the last 7 days?",
        f"SELECT imsi, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {d7} "
        f"AND subscriber_id IN ('923001234567', '03001234567') AND IFNULL(imsi, '') <> '' "
        f"GROUP BY imsi ORDER BY flows DESC LIMIT 10",
        ["sim", "imsi", "phone"],
    )
    add(
        "Which websites or SNIs did client IP 203.0.113.10 open in the last 24 hours?",
        f"SELECT requested_server, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {h24} "
        f"AND client_ip = '203.0.113.10' AND IFNULL(requested_server, '') <> '' "
        f"GROUP BY requested_server ORDER BY flows DESC LIMIT 50",
        ["website", "sni", "host"],
    )
    add(
        "Which VPN or tunnel apps were used yesterday?",
        f"SELECT app_label, COUNT(*) AS flows, COUNT(DISTINCT subscriber_id) AS subscribers "
        f"FROM ntc_ssl_logs WHERE {y} AND LOWER(app_category) = 'tunneling' "
        f"GROUP BY app_label ORDER BY flows DESC LIMIT 20",
        ["vpn", "tunnel"],
    )
    add(
        "How many flows were blocked for TikTok yesterday?",
        f"SELECT COUNT(*) AS blocked_flows FROM ntc_ssl_logs WHERE {y} AND {BLOCKED_SQL} "
        f"AND LOWER(app_label) LIKE '%tiktok%'",
        ["blocked", "tiktok"],
    )
    add(
        "Where was subscriber 923001234567 seen in the last 7 days?",
        f"SELECT loc, geo, client_location, mcc, mnc, lac, cid, COUNT(*) AS flows FROM ntc_ssl_logs "
        f"WHERE {d7} AND subscriber_id IN ('923001234567', '03001234567') "
        f"GROUP BY loc, geo, client_location, mcc, mnc, lac, cid ORDER BY flows DESC LIMIT 20",
        ["where", "subscriber", "cell"],
    )
    add(
        "Which cell towers did subscriber 923001234567 use yesterday?",
        f"SELECT mcc, mnc, lac, cid, loc, COUNT(*) AS flows FROM ntc_ssl_logs WHERE {y} AND {MOBILE_SQL} "
        f"AND subscriber_id IN ('923001234567', '03001234567') "
        f"GROUP BY mcc, mnc, lac, cid, loc ORDER BY flows DESC LIMIT 20",
        ["cell", "tower"],
    )
    add(
        "Was 03001234567 on a cell or a fixed line in the last 7 days?",
        f"SELECT subscriber_id, SUM(CASE WHEN {MOBILE_SQL} THEN 1 ELSE 0 END) AS mobile_flows, "
        f"SUM(CASE WHEN {FIXED_SQL} THEN 1 ELSE 0 END) AS fixed_line_flows FROM ntc_ssl_logs "
        f"WHERE {d7} AND subscriber_id IN ('03001234567', '923001234567') GROUP BY subscriber_id",
        ["cell", "fixed", "phone"],
    )


def _full_table(add) -> None:
    top5 = (
        "SELECT app_label, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, "
        "ROUND(SUM(c2s_byte_num + s2c_byte_num) / 1048576, 2) AS total_mb, COUNT(*) AS flows "
        "FROM ntc_ssl_logs GROUP BY app_label ORDER BY total_bytes DESC LIMIT 5"
    )
    top_categories = (
        "SELECT app_category, SUM(c2s_byte_num + s2c_byte_num) AS total_bytes, COUNT(*) AS flows "
        "FROM ntc_ssl_logs GROUP BY app_category ORDER BY total_bytes DESC LIMIT 5"
    )
    add(
        "give me the top 5 applications used by traffic volume. do not apply any time filter. check all the data",
        top5,
        ["full_scan", "top", "volume", "app_label"],
        full_scan=True,
    )
    add(
        "Top 5 applications by total bytes across the whole table, with no time filter",
        top5,
        ["full_scan", "bytes", "app_label"],
        full_scan=True,
    )
    add(
        "Which five app labels account for the most traffic in all the data?",
        top5,
        ["full_scan", "traffic", "app_label"],
        full_scan=True,
    )
    add(
        "Top 5 app categories by traffic volume with no time filter",
        top_categories,
        ["full_scan", "category", "volume"],
        full_scan=True,
    )


def _unavailable(add) -> None:
    questions = {
        "direction": "What is the direction of flows yesterday?",
        "stream_dir": "Show the stream direction for today's flows",
        "user_agent": "What user agents were seen yesterday?",
        "is_roaming": "How many subscribers are roaming today?",
        "device_model": "Which handset models were used yesterday?",
        "device_vendor": "Break down traffic by device vendor for yesterday",
        "ssl_ja3_hash": "Find flows with this JA3 hash yesterday",
        "ssl_ja3_fingerprint": "Group yesterday's flows by JA3 fingerprint",
        "ssl_cert_subject": "Which certificate subjects appeared yesterday?",
        "ssl_cert_issuer": "Who issued the certificates seen yesterday?",
        "l7_protocol": "What layer 7 protocols were used yesterday?",
        "data_center": "Which data center handled yesterday's flows?",
        "action": "What DPI action was taken yesterday?",
        "cap_ip": "Which capture IP saw the most flows yesterday?",
    }
    for column, question in questions.items():
        why = DEAD_COLUMNS.get(column, "not useful for subscriber analysis")
        if column == "cap_ip":
            clarify = "cap_ip is an internal DPI address. It is not the subscriber and is not useful for this kind of question."
        else:
            clarify = f"{column} is not populated in ntc_ssl_logs ({why}), so this cannot be answered from the table."
        add(question, None, ["unavailable", column], clarify=clarify)


def _load_labels() -> list[tuple[str, str]]:
    rows = []
    with MAPPING.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            label = (record.get("app_label") or "").strip()
            category = (record.get("app_category") or "").strip().lower()
            if label:
                rows.append((label, category))
    return rows


if __name__ == "__main__":
    main()
