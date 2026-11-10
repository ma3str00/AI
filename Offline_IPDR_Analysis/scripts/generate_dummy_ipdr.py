"""Load dummy IPDR into ClickHouse, capped by the table's on-disk size.

Reads scripts/config.yaml (copy scripts/config.yaml.example). Replays the
sample's flow rate across the configured number of days, creates the table
when it is missing, and stops before the table's compressed size passes
max_disk_gb.

Subscriber identifiers, client addresses, device identities, and street-level
locations are synthetic. Columns that are always empty or zero in the sample
stay that way.

    python scripts/generate_dummy_ipdr.py
    python scripts/generate_dummy_ipdr.py --config /etc/ipdr/config.yaml
"""

from __future__ import annotations

import argparse
import csv
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

COLUMNS = [
    "recv_time", "log_id", "direction", "stream_dir", "start_time", "end_time",
    "duration", "establish_latency", "processing_time", "device_id",
    "egress_link_id", "ingress_link_id", "data_center", "cap_ip", "action",
    "requested_server", "client_ip", "client_port", "client_geo",
    "client_location", "client_asn", "subscriber_id", "imei", "imsi",
    "server_ip", "server_port", "server_geo", "server_location", "server_asn",
    "app_category", "app_label", "l4_protocol", "l7_protocol", "sessions",
    "c2s_pkt_num", "s2c_pkt_num", "c2s_byte_num", "s2c_byte_num", "first_ttl",
    "address_type", "schema_type", "client_type", "usr_loc_info", "mcc", "mnc",
    "lac", "cid", "geo", "loc", "version", "user_agent", "ssl_con_latency",
    "ssl_ja3_fingerprint", "ssl_ja3_hash", "ssl_cert_issuer", "ssl_cert_subject",
    "block_type", "device_vendor", "device_model", "is_roaming",
]

# Types and codecs match the existing ntc_ssl_log description.
COLUMN_DEFS = """
    recv_time UInt32 CODEC(ZSTD(1)),
    log_id String,
    direction LowCardinality(String),
    stream_dir LowCardinality(String),
    start_time UInt32 CODEC(ZSTD(1)),
    end_time UInt32 CODEC(ZSTD(1)),
    duration UInt64 CODEC(ZSTD(1)),
    establish_latency UInt32,
    processing_time UInt32,
    device_id LowCardinality(String),
    egress_link_id Int32,
    ingress_link_id Int32,
    data_center LowCardinality(String),
    cap_ip LowCardinality(String),
    action LowCardinality(String),
    requested_server String,
    client_ip String CODEC(ZSTD(1)),
    client_port UInt16,
    client_geo LowCardinality(String),
    client_location LowCardinality(String),
    client_asn LowCardinality(String),
    subscriber_id String CODEC(ZSTD(1)),
    imei String CODEC(ZSTD(1)),
    imsi String,
    server_ip String CODEC(ZSTD(1)),
    server_port UInt16,
    server_geo LowCardinality(String),
    server_location LowCardinality(String),
    server_asn LowCardinality(String),
    app_category LowCardinality(String),
    app_label LowCardinality(String),
    l4_protocol LowCardinality(String),
    l7_protocol LowCardinality(String),
    sessions UInt32,
    c2s_pkt_num UInt32 CODEC(ZSTD(1)),
    s2c_pkt_num UInt32 CODEC(ZSTD(1)),
    c2s_byte_num UInt64 CODEC(ZSTD(1)),
    s2c_byte_num UInt64 CODEC(ZSTD(1)),
    first_ttl UInt32,
    address_type LowCardinality(String),
    schema_type LowCardinality(String),
    client_type LowCardinality(String),
    usr_loc_info String CODEC(ZSTD(1)),
    mcc LowCardinality(String),
    mnc LowCardinality(String),
    lac String CODEC(ZSTD(1)),
    cid String CODEC(ZSTD(1)),
    geo String,
    loc String,
    version LowCardinality(String),
    user_agent String,
    ssl_con_latency UInt32,
    ssl_ja3_fingerprint String,
    ssl_ja3_hash String,
    ssl_cert_issuer String,
    ssl_cert_subject String,
    block_type UInt32,
    device_vendor String,
    device_model String,
    is_roaming String
"""

HOUR_WEIGHTS = [
    2, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8,
    9, 8, 8, 8, 9, 10, 12, 14, 14, 12, 8, 5,
]
HOUR_MAX = max(HOUR_WEIGHTS)
PKT_OFFSET = 5 * 3600
SUBSCRIBERS = 250_000
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

STREET = re.compile(
    r"\b(plot|street|road|rd|house|lane|town|colony|near|st)\b",
    re.IGNORECASE,
)
ORG = re.compile(
    r"(pvt|ltd|limited|network|broadband|telecom|internet|fiber|communication|isp|transit)",
    re.IGNORECASE,
)


def luhn_digit(body: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(body)):
        n = ord(ch) - 48
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - (total % 10)) % 10)


def safe_asn(value: str) -> bool:
    if not value or STREET.search(value):
        return False
    if any(ch.isdigit() for ch in value) and not ORG.search(value):
        return False
    return True


def quote_ident(name: str) -> str:
    if not IDENT.match(name):
        raise SystemExit(f"Unsafe ClickHouse identifier: {name!r}")
    return f"`{name}`"


def as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def as_port(value: str) -> int:
    port = as_int(value)
    if port < 0:
        return 0
    if port > 65535:
        return 65535
    return port


def load_sample(path: Path):
    templates = []
    probes = []
    seen_caps = set()
    cities = Counter()
    city_geos = defaultdict(Counter)
    asns = Counter()
    min_start = None
    max_start = None

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            start = int(row["start_time"])
            min_start = start if min_start is None else min(min_start, start)
            max_start = start if max_start is None else max(max_start, start)
            templates.append((
                row["requested_server"],
                row["server_ip"],
                as_port(row["server_port"]),
                row["server_geo"],
                row["server_location"],
                row["server_asn"],
                row["app_category"],
                row["app_label"],
                row["l4_protocol"],
                as_int(row["c2s_pkt_num"]),
                as_int(row["s2c_pkt_num"]),
                as_int(row["c2s_byte_num"]),
                as_int(row["s2c_byte_num"]),
                as_int(row["duration"]),
                as_int(row["block_type"]),
            ))
            if row["cap_ip"] not in seen_caps:
                seen_caps.add(row["cap_ip"])
                probes.append(row["cap_ip"])
            cities[row["client_location"]] += 1
            if row["client_geo"]:
                city_geos[row["client_location"]][row["client_geo"]] += 1
            if safe_asn(row["client_asn"]):
                asns[row["client_asn"]] += 1

    centroids = {}
    for city, geos in city_geos.items():
        raw = geos.most_common(1)[0][0]
        try:
            lon, lat = raw.split(",", 1)
            centroids[city] = f"{float(lon):.2f},{float(lat):.2f}"
        except ValueError:
            centroids[city] = ""
    span = max(1, (max_start or 0) - (min_start or 0))
    return templates, probes, cities, centroids, asns, len(templates), span


ROADS = (
    "Station Road", "Mall Road", "Canal Road", "Link Road", "Airport Road",
    "Circular Road", "College Road", "Market Road", "Garden Road", "Park Road",
)
BLOCKS = (
    "Block A", "Block B", "Sector C", "Phase 1", "Phase 2",
    "Scheme 1", "Civil Lines", "Satellite Town", "Model Town", "Town Ward",
)


def place_name(city: str) -> str:
    return city.strip() or "Pakistan"


def jitter_geo(rng: random.Random, raw: str) -> str:
    if "," not in raw:
        return ""
    lon, lat = raw.split(",", 1)
    try:
        lon_f = float(lon) + rng.uniform(-0.04, 0.04)
        lat_f = float(lat) + rng.uniform(-0.04, 0.04)
    except ValueError:
        return ""
    return f"{lon_f:.5f},{lat_f:.5f}"


def premises_address(rng: random.Random, city: str) -> str:
    return (
        f"House {rng.randrange(1, 2000)}, Street {rng.randrange(1, 80)}, "
        f"{rng.choice(BLOCKS)}, {place_name(city)}"
    )


def tower_address(rng: random.Random, city: str) -> str:
    return (
        f"Cell site {rng.randrange(1, 9000)}, {rng.choice(ROADS)}, "
        f"{rng.choice(BLOCKS)}, {place_name(city)}"
    )


def fixed_subscriber_id(rng: random.Random) -> str:
    """Identified fixed-line id. Not an IP_port pair and not a 923 mobile number."""
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        kind = rng.randrange(4)
        if kind == 0:
            value = f"03{rng.randrange(10**9):09d}"
        elif kind == 1:
            prefix = rng.choice(("user", "acct", "fiber", "gpon", "bb"))
            value = f"{prefix}{rng.randrange(10**8):08d}"
        elif kind == 2:
            token = "".join(rng.choice(letters) for _ in range(rng.randrange(2, 5)))
            value = f"{token}{rng.randrange(10**8):08d}"
        else:
            token = "".join(rng.choice(letters.lower()) for _ in range(rng.randrange(3, 8)))
            value = f"{rng.randrange(10**6):06d}{token}"
        if "_" not in value and ":" not in value and not value.startswith("923"):
            return value


def ipv4(index: int) -> str:
    host = index + 1
    second = 64 + ((host >> 16) & 0x3F)
    third = (host >> 8) & 0xFF
    fourth = host & 0xFF
    return f"100.{second}.{third}.{fourth}"


def ipv6(index: int) -> str:
    return f"2001:db8:{index >> 16:x}:{index & 0xFFFF:x}::10"


def build_subscribers(rng: random.Random, cities, centroids, asns):
    city_keys = list(cities.keys())
    city_w = [cities[k] for k in city_keys]
    asn_keys = list(asns.keys())
    asn_w = [asns[k] for k in asn_keys]
    # Sample mix: identified mobile, identified fixed-line, then IP_port.
    mobile_cut = int(SUBSCRIBERS * 0.304)
    fixed_cut = mobile_cut + int(SUBSCRIBERS * 0.171)
    subs = []
    for i in range(SUBSCRIBERS):
        if i < mobile_cut:
            kind = "mobile"
        elif i < fixed_cut:
            kind = "fixed"
        else:
            kind = "unidentified"
        ip = ipv6(i) if rng.random() < 0.14942 else ipv4(i)
        city = rng.choices(city_keys, weights=city_w, k=1)[0]
        asn = "" if rng.random() < 0.02031 else rng.choices(asn_keys, weights=asn_w, k=1)[0]
        client_geo = centroids.get(city, "")
        if kind == "mobile":
            mnc = rng.choices(
                ["04", "01", "06", "03", "07", "12", "11"],
                weights=[8778, 8113, 6573, 6439, 384, 50, 21],
                k=1,
            )[0]
            imsi = f"410{mnc}{rng.randrange(10**10):010d}"[:15]
            imei = f"99000000{rng.randrange(10**6):06d}"
            imei += luhn_digit(imei)
            subscriber_id = f"923{rng.randrange(10**9):09d}"
            lac = str(10000 + rng.randrange(50000))
            cid = str(100000 + rng.randrange(900000))
            usr = f"99{mnc}{int(lac):04X}{int(cid) & 0xFFFF:04X}"
            site_geo = jitter_geo(rng, client_geo)
            loc = tower_address(rng, city) if rng.random() < 0.7 else premises_address(rng, city)
        elif kind == "fixed":
            mnc = imsi = imei = lac = cid = usr = ""
            subscriber_id = fixed_subscriber_id(rng)
            site_geo = jitter_geo(rng, client_geo)
            loc = premises_address(rng, city)
        else:
            mnc = imsi = imei = lac = cid = usr = subscriber_id = ""
            site_geo = loc = ""
        subs.append((
            kind, ip, client_geo, city, asn, subscriber_id, imei, imsi, usr,
            mnc, lac, cid, site_geo, loc,
        ))
    return subs


def load_config(path: Path) -> dict:
    if not path.is_file():
        example = path.with_name("config.yaml.example")
        raise SystemExit(f"Missing {path}. Copy {example} to {path} and edit it.")
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    ch = raw.get("clickhouse") or {}
    gen = raw.get("generate") or {}
    required = {
        "host": ch.get("host"),
        "username": ch.get("username"),
        "database": ch.get("database"),
        "table": ch.get("table"),
        "sample": gen.get("sample"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise SystemExit(f"{path} is missing: {', '.join(missing)}")
    if "password" not in ch:
        raise SystemExit(f"{path} is missing: password")
    max_disk = float(gen.get("max_disk_gb", 0))
    if max_disk <= 0:
        raise SystemExit(f"{path} needs generate.max_disk_gb set to a positive number of GB")
    end = datetime.fromisoformat(str(gen.get("end", "2026-11-10T04:41:00+00:00")))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return {
        "host": str(ch["host"]),
        "port": int(ch.get("port", 8123)),
        "username": str(ch["username"]),
        "password": str(ch.get("password", "")),
        "database": str(ch["database"]),
        "table": str(ch["table"]),
        "secure": bool(ch.get("secure", False)),
        "sample": Path(str(gen["sample"])),
        "days": int(gen.get("days", 30)),
        "end": end,
        "max_disk_bytes": int(max_disk * 10**9),
        "max_disk_gb": max_disk,
        "batch_rows": int(gen.get("batch_rows", 50000)),
    }


def connect(cfg: dict):
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise SystemExit("clickhouse-connect is not installed. Run: pip install clickhouse-connect") from exc
    return clickhouse_connect.get_client(
        host=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        password=cfg["password"],
        secure=cfg["secure"],
    )


def ensure_table(client, database: str, table: str) -> None:
    db = quote_ident(database)
    tbl = quote_ident(table)
    client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {db}.{tbl}
        (
            {COLUMN_DEFS}
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMMDD(toDateTime(start_time, 'UTC'))
        ORDER BY (start_time, subscriber_id, log_id)
        """
    )


def table_bytes(client, database: str, table: str) -> int:
    try:
        result = client.query(
            """
            SELECT ifNull(sum(bytes_on_disk), 0)
            FROM system.parts
            WHERE active AND database = {db:String} AND table = {tbl:String}
            """,
            parameters={"db": database, "tbl": table},
        )
    except Exception as exc:
        message = str(exc)
        if "ACCESS_DENIED" in message or "Not enough privileges" in message:
            raise SystemExit(
                "Cannot read system.parts, so the disk cap cannot be enforced. "
                "GRANT SELECT ON system.parts TO this user, or run the load as an admin."
            ) from exc
        raise
    return int(result.result_rows[0][0])


def make_row(rows: int, tpl, sub, probes, randrange, random_fn, window_start, window_seconds) -> tuple:
    while True:
        start = window_start + randrange(window_seconds)
        hour = ((start + PKT_OFFSET) // 3600) % 24
        if randrange(HOUR_MAX) < HOUR_WEIGHTS[hour]:
            break
    port = randrange(1024, 65535)
    duration = tpl[13]
    flow_end = start + max(1, (duration + 999) // 1000)
    recv = flow_end + randrange(5, 41)
    kind, ip, geo, city, asn, subscriber_id, imei, imsi, usr, mnc, lac, cid, site_geo, loc = sub
    if kind == "unidentified":
        # client IP and port, for example 100.64.1.2_44321 or 2001:db8:1:2::10_44321.
        subscriber_id = f"{ip}_{port}"
        imei_field = imsi_field = usr = mcc = mnc = lac = cid = site_geo = loc = ""
    elif kind == "mobile":
        imei_field = imei if random_fn() < 0.9577 else ""
        imsi_field = imsi
        mcc = "410"
    else:
        imei_field = imsi_field = usr = mcc = mnc = lac = cid = ""
    return (
        recv,
        str(70_000_000_000_000_000 + rows),
        "",
        "",
        start,
        flow_end,
        duration,
        0,
        0,
        "-1",
        0,
        0,
        "",
        probes[randrange(len(probes))],
        "",
        tpl[0],
        ip,
        port,
        geo,
        city,
        asn,
        subscriber_id,
        imei_field,
        imsi_field,
        tpl[1],
        tpl[2],
        tpl[3],
        tpl[4],
        tpl[5],
        tpl[6],
        tpl[7],
        tpl[8],
        "",
        0,
        tpl[9],
        tpl[10],
        tpl[11],
        tpl[12],
        0,
        "",
        "",
        "",
        usr,
        mcc,
        mnc,
        lac,
        cid,
        site_geo,
        loc,
        "",
        "",
        0,
        "",
        "",
        "",
        "",
        tpl[14],
        "",
        "",
        "",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.yaml",
        help="Path to config.yaml (default: beside this script)",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    if not cfg["sample"].is_file():
        raise SystemExit(f"Sample CSV not found: {cfg['sample']}")

    window_seconds = cfg["days"] * 24 * 3600
    window_start = int(cfg["end"].timestamp()) - window_seconds
    print(f"loading {cfg['sample']}", flush=True)
    templates, probes, cities, centroids, asns, n_sample, span = load_sample(cfg["sample"])
    target_rows = int(round(n_sample * window_seconds / span))
    print(
        f"sample_rows {n_sample} span_sec {span} target_rows {target_rows} "
        f"max_disk_gb {cfg['max_disk_gb']}",
        flush=True,
    )

    subs = build_subscribers(random.Random(20261110), cities, centroids, asns)
    rng = random.Random(20261111)
    client = connect(cfg)
    ensure_table(client, cfg["database"], cfg["table"])
    used = table_bytes(client, cfg["database"], cfg["table"])
    print(f"table disk_gb {used / 1e9:.3f}", flush=True)
    if used >= cfg["max_disk_bytes"]:
        print("table is already at the disk cap; nothing inserted", flush=True)
        return

    rows = 0
    batch = []
    n_tpl = len(templates)
    while rows < target_rows:
        batch.append(make_row(
            rows, templates[rng.randrange(n_tpl)], subs[rng.randrange(len(subs))],
            probes, rng.randrange, rng.random, window_start, window_seconds,
        ))
        rows += 1
        if len(batch) < cfg["batch_rows"] and rows < target_rows:
            continue
        client.insert(
            cfg["table"],
            batch,
            column_names=COLUMNS,
            database=cfg["database"],
        )
        batch.clear()
        used = table_bytes(client, cfg["database"], cfg["table"])
        print(f"rows {rows} disk_gb {used / 1e9:.3f}", flush=True)
        if used >= cfg["max_disk_bytes"]:
            print("stopped at the disk cap", flush=True)
            break

    print(
        f"inserted {rows} rows into {cfg['database']}.{cfg['table']} "
        f"disk_gb {used / 1e9:.3f} "
        f"from {datetime.fromtimestamp(window_start, timezone.utc).isoformat()} "
        f"to {cfg['end'].isoformat()}",
        flush=True,
    )


if __name__ == "__main__":
    main()
