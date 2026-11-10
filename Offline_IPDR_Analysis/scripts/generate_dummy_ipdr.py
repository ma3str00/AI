"""Build a 30-day dummy IPDR CSV from the shape of a sample export.

Traffic totals stay under 220 GB (decimal). Subscriber identifiers, client
addresses, device identities, and street-level locations are synthetic.
Columns that are always empty or zero in the sample stay that way.
"""

from __future__ import annotations

import csv
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = Path(r"C:\Users\A Hamson\Desktop\Dump\Temp\To be deleted\sample-ipdr.csv")
OUTPUT = ROOT / "data" / "dummy_ipdr_30d.csv"

# Decimal gigabytes, matching how IPDR byte counters are usually totaled.
VOLUME_CAP = 220 * 10**9
WINDOW_END = datetime(2026, 11, 10, 4, 41, tzinfo=timezone.utc)  # 09:41 PKT
WINDOW_SECONDS = 30 * 24 * 3600
PKT_OFFSET = 5 * 3600

# Local-hour weights (Pakistan). Evening is busier than the small hours.
HOUR_WEIGHTS = [
    2, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8,
    9, 8, 8, 8, 9, 10, 12, 14, 14, 12, 8, 5,
]
HOUR_MAX = max(HOUR_WEIGHTS)

HEADER = [
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
    reverse = body[::-1]
    for i, ch in enumerate(reverse):
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


def load_sample(path: Path):
    templates = []
    cap_ips = Counter()
    cities = Counter()
    city_geos = defaultdict(Counter)
    asns = Counter()
    real_ids = set()
    real_ips = set()

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            templates.append((
                row["requested_server"],
                row["server_ip"],
                row["server_port"],
                row["server_geo"],
                row["server_location"],
                row["server_asn"],
                row["app_category"],
                row["app_label"],
                row["l4_protocol"],
                row["c2s_pkt_num"],
                row["s2c_pkt_num"],
                int(row["c2s_byte_num"] or 0),
                int(row["s2c_byte_num"] or 0),
                int(row["duration"] or 0),
                row["block_type"] or "0",
            ))
            cap_ips[row["cap_ip"]] += 1
            cities[row["client_location"]] += 1
            if row["client_geo"]:
                city_geos[row["client_location"]][row["client_geo"]] += 1
            if safe_asn(row["client_asn"]):
                asns[row["client_asn"]] += 1
            real_ids.add(row["subscriber_id"])
            real_ids.add(row["imei"])
            real_ids.add(row["imsi"])
            real_ips.add(row["client_ip"])

    real_ids.discard("")
    centroids = {}
    for city, geos in city_geos.items():
        raw = geos.most_common(1)[0][0]
        try:
            lon, lat = raw.split(",", 1)
            centroids[city] = f"{float(lon):.2f},{float(lat):.2f}"
        except ValueError:
            centroids[city] = ""
    return templates, cap_ips, cities, centroids, asns, real_ids, real_ips


def weighted_choice(rng: random.Random, counter: Counter) -> str:
    keys = list(counter.keys())
    weights = [counter[k] for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


def make_ipv4(rng: random.Random) -> str:
    # RFC 2544 benchmarking range. Not a subscriber prefix.
    n = rng.randrange(65536)
    second = 18 if n < 32768 else 19
    third = (n >> 8) & 0xFF
    fourth = n & 0xFF
    if fourth == 0:
        fourth = 1
    return f"198.{second}.{third}.{fourth}"


def make_ipv6(rng: random.Random) -> str:
    n = rng.randrange(1, 2**32)
    return f"2001:db8:{n >> 16:x}:{n & 0xFFFF:x}::10"


def build_subscribers(rng: random.Random, cities, centroids, asns, count: int):
    city_keys = list(cities.keys())
    city_w = [cities[k] for k in city_keys]
    asn_keys = list(asns.keys())
    asn_w = [asns[k] for k in asn_keys]
    subs = []
    for i in range(count):
        mobile = i < int(count * 0.304)
        ipv6 = rng.random() < 0.14942
        ip = make_ipv6(rng) if ipv6 else make_ipv4(rng)
        city = rng.choices(city_keys, weights=city_w, k=1)[0]
        asn = "" if rng.random() < 0.02031 else rng.choices(asn_keys, weights=asn_w, k=1)[0]
        if mobile:
            mnc = rng.choices(
                ["04", "01", "06", "03", "07", "12", "11"],
                weights=[8778, 8113, 6573, 6439, 384, 50, 21],
                k=1,
            )[0]
            msin = f"{rng.randrange(10**10):010d}"
            imsi = f"410{mnc}{msin}"[:15]
            imei_body = f"99000000{rng.randrange(10**6):06d}"
            imei = imei_body + luhn_digit(imei_body)
            msisdn = f"92399{rng.randrange(10**7):07d}"
            lac = str(10000 + rng.randrange(50000))
            cid = str(100000 + rng.randrange(900000))
            usr = f"99{mnc}{int(lac):04X}{int(cid) & 0xFFFF:04X}"
        else:
            mnc = imsi = imei = msisdn = lac = cid = usr = ""
        subs.append({
            "mobile": mobile,
            "ip": ip,
            "city": city,
            "geo": centroids.get(city, ""),
            "asn": asn,
            "msisdn": msisdn,
            "imei": imei,
            "imsi": imsi,
            "mnc": mnc,
            "lac": lac,
            "cid": cid,
            "usr": usr,
            "has_cell_geo": mobile and rng.random() < 0.7856,
            "has_fixed_geo": (not mobile) and rng.random() < 0.1010,
        })
    return subs


def random_start(rng: random.Random, start: int) -> int:
    while True:
        ts = start + rng.randrange(WINDOW_SECONDS)
        hour = ((ts + PKT_OFFSET) // 3600) % 24
        if rng.random() < HOUR_WEIGHTS[hour] / HOUR_MAX:
            return ts


def main() -> None:
    rng = random.Random(20261110)
    print("loading sample", SAMPLE)
    templates, cap_ips, cities, centroids, asns, real_ids, real_ips = load_sample(SAMPLE)
    print(f"templates {len(templates)} cities {len(cities)} isps {len(asns)}")
    subs = build_subscribers(rng, cities, centroids, asns, 20000)
    probes = list(cap_ips.keys())
    window_start = int(WINDOW_END.timestamp()) - WINDOW_SECONDS

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    rows = 0
    leaked = 0
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(HEADER)
        while True:
            tpl = templates[rng.randrange(len(templates))]
            c2s = tpl[11]
            s2c = tpl[12]
            flow_bytes = c2s + s2c
            if total_bytes + flow_bytes > VOLUME_CAP:
                break
            sub = subs[rng.randrange(len(subs))]
            if sub["ip"] in real_ips or sub["msisdn"] in real_ids or sub["imsi"] in real_ids:
                leaked += 1
                continue
            port = rng.randrange(1024, 65535)
            start = random_start(rng, window_start)
            duration = tpl[13]
            end = start + max(1, (duration + 999) // 1000)
            recv = end + rng.randrange(5, 41)
            show_cell = sub["has_cell_geo"] or sub["has_fixed_geo"]
            show_mobile = sub["mobile"]
            show_imei = show_mobile and rng.random() < 0.9577
            subscriber_id = sub["msisdn"] if show_mobile else f"{sub['ip']}_{port}"
            writer.writerow([
                recv,
                70_000_000_000_000_000 + rows,
                "", "",
                start, end, duration,
                0, 0, "-1", 0, 0, "",
                probes[rng.randrange(len(probes))],
                "",
                tpl[0],
                sub["ip"], port,
                sub["geo"] if sub["city"] else "",
                sub["city"],
                sub["asn"],
                subscriber_id,
                sub["imei"] if show_imei else "",
                sub["imsi"] if show_mobile else "",
                tpl[1], tpl[2], tpl[3], tpl[4], tpl[5], tpl[6], tpl[7], tpl[8],
                "", 0,
                tpl[9], tpl[10], c2s, s2c,
                0, "", "", "",
                sub["usr"] if show_mobile else "",
                "410" if show_mobile else "",
                sub["mnc"] if show_mobile else "",
                sub["lac"] if show_mobile else "",
                sub["cid"] if show_mobile else "",
                sub["geo"] if show_cell and sub["geo"] else "",
                sub["city"] if show_cell and sub["city"] else "",
                "", "", 0, "", "", "", "",
                tpl[14],
                "", "", "",
            ])
            total_bytes += flow_bytes
            rows += 1
            if rows % 200_000 == 0:
                print(f"rows {rows} volume_gb {total_bytes / 1e9:.2f}")

    print(
        f"wrote {OUTPUT} rows={rows} volume_gb={total_bytes / 1e9:.4f} "
        f"skipped_collisions={leaked}"
    )


if __name__ == "__main__":
    main()
