"""Write 30 days of dummy IPDR as one CSV, capped by file size.

Replays the sample's flow rate across the 30 days ending 10 Nov 2026 09:41 PKT.
Stops at that row count, or sooner if the file would pass 220 GB.

Subscriber identifiers, client addresses, device identities, and street-level
locations are synthetic. Columns that are always empty or zero in the sample
stay that way.

    python scripts/generate_dummy_ipdr.py SAMPLE.csv -o /data/dummy_ipdr_30d.csv
"""

from __future__ import annotations

import argparse
import csv
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

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

# Local-hour weights (Pakistan). Evening is busier than the small hours.
HOUR_WEIGHTS = [
    2, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8,
    9, 8, 8, 8, 9, 10, 12, 14, 14, 12, 8, 5,
]
HOUR_MAX = max(HOUR_WEIGHTS)
PKT_OFFSET = 5 * 3600
SUBSCRIBERS = 250_000

STREET = re.compile(
    r"\b(plot|street|road|rd|house|lane|town|colony|near|st)\b",
    re.IGNORECASE,
)
ORG = re.compile(
    r"(pvt|ltd|limited|network|broadband|telecom|internet|fiber|communication|isp|transit)",
    re.IGNORECASE,
)


def q(value: str) -> str:
    if any(ch in value for ch in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


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


def load_sample(path: Path):
    servers, tails, blocks, durations, probes = [], [], [], [], []
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
            servers.append(q(row["requested_server"]))
            durations.append(int(row["duration"] or 0))
            tails.append(",".join((
                q(row["server_ip"]),
                row["server_port"],
                q(row["server_geo"]),
                q(row["server_location"]),
                q(row["server_asn"]),
                q(row["app_category"]),
                q(row["app_label"]),
                row["l4_protocol"],
                "",
                "0",
                row["c2s_pkt_num"],
                row["s2c_pkt_num"] or "0",
                row["c2s_byte_num"] or "0",
                row["s2c_byte_num"] or "0",
                "0",
                "", "", "",
            )))
            blocks.append(row["block_type"] or "0")
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
    return servers, tails, blocks, durations, probes, cities, centroids, asns, len(durations), span


def ipv4(index: int) -> str:
    # 100.64.0.0/10 is shared CGNAT space, not an address copied from the sample.
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
    mobile_cut = int(SUBSCRIBERS * 0.304)
    subs = []
    for i in range(SUBSCRIBERS):
        mobile = i < mobile_cut
        ip = ipv6(i) if rng.random() < 0.14942 else ipv4(i)
        city = rng.choices(city_keys, weights=city_w, k=1)[0]
        asn = "" if rng.random() < 0.02031 else rng.choices(asn_keys, weights=asn_w, k=1)[0]
        if mobile:
            mnc = rng.choices(
                ["04", "01", "06", "03", "07", "12", "11"],
                weights=[8778, 8113, 6573, 6439, 384, 50, 21],
                k=1,
            )[0]
            imsi = f"410{mnc}{rng.randrange(10**10):010d}"[:15]
            imei = f"99000000{rng.randrange(10**6):06d}"
            imei += luhn_digit(imei)
            msisdn = f"92399{rng.randrange(10**7):07d}"
            lac = str(10000 + rng.randrange(50000))
            cid = str(100000 + rng.randrange(900000))
            usr = f"99{mnc}{int(lac):04X}{int(cid) & 0xFFFF:04X}"
        else:
            mnc = imsi = imei = msisdn = lac = cid = usr = ""
        geo = centroids.get(city, "")
        has_cell = (mobile and rng.random() < 0.7856) or ((not mobile) and rng.random() < 0.1010)
        subs.append((
            mobile,
            ip,
            q(geo if city else ""),
            q(city),
            q(asn),
            msisdn,
            imei,
            imsi,
            q(usr),
            mnc,
            lac,
            cid,
            q(geo if has_cell and geo else ""),
            q(city if has_cell and city else ""),
        ))
    return subs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path, help="Sample IPDR CSV to copy the shape from")
    parser.add_argument("-o", "--output", type=Path, required=True, help="CSV path to write")
    parser.add_argument("--max-gb", type=float, default=220, help="Stop before the file passes this many decimal GB")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--end",
        default="2026-11-10T04:41:00+00:00",
        help="Window end, ISO-8601. Default is 10 Nov 2026 09:41 PKT",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end = datetime.fromisoformat(args.end)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    window_seconds = args.days * 24 * 3600
    window_start = int(end.timestamp()) - window_seconds
    file_cap = int(args.max_gb * 10**9)

    print(f"loading {args.sample}", flush=True)
    servers, tails, blocks, durations, probes, cities, centroids, asns, n_sample, span = load_sample(args.sample)
    target_rows = int(round(n_sample * window_seconds / span))
    print(
        f"sample_rows {n_sample} span_sec {span} target_rows {target_rows} "
        f"file_cap_gb {args.max_gb}",
        flush=True,
    )

    subs = build_subscribers(random.Random(20261110), cities, centroids, asns)
    rng = random.Random(20261111)
    randrange = rng.randrange
    random_fn = rng.random
    n_sub = len(subs)
    n_tpl = len(servers)
    n_probe = len(probes)
    suffix_tail = ["", "", "0", "", "", "", "", "", "", "", ""]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    header = ",".join(q(name) for name in HEADER) + "\n"
    written = len(header.encode("utf-8"))
    rows = 0
    batch: list[str] = []
    batch_bytes = 0

    with args.output.open("w", encoding="utf-8", newline="", buffering=8 * 1024 * 1024) as handle:
        handle.write(header)

        def flush_batch() -> None:
            nonlocal batch_bytes
            if batch:
                handle.write("".join(batch))
                batch.clear()
                batch_bytes = 0

        while rows < target_rows and written < file_cap:
            while True:
                start = window_start + randrange(window_seconds)
                hour = ((start + PKT_OFFSET) // 3600) % 24
                if randrange(HOUR_MAX) < HOUR_WEIGHTS[hour]:
                    break
            tpl = randrange(n_tpl)
            sub = subs[randrange(n_sub)]
            port = randrange(1024, 65535)
            duration = durations[tpl]
            flow_end = start + max(1, (duration + 999) // 1000)
            recv = flow_end + randrange(5, 41)
            mobile, ip, geo, city, asn, msisdn, imei, imsi, usr, mnc, lac, cid, cell_geo, cell_city = sub
            if mobile:
                imei_field = imei if random_fn() < 0.9577 else ""
                ident = f"{msisdn},{imei_field},{imsi}"
                cell = f"{usr},410,{mnc},{lac},{cid},{cell_geo},{cell_city}"
            else:
                ident = f"{ip}_{port},,"
                cell = f",,,,,{cell_geo},{cell_city}" if (cell_geo or cell_city) else ",,,,,,"
            suffix_tail[7] = blocks[tpl]
            line = (
                f"{recv},{70_000_000_000_000_000 + rows},,,{start},{flow_end},{duration},0,0,-1,0,0,,"
                f"{probes[randrange(n_probe)]},,{servers[tpl]},{ip},{port},{geo},{city},{asn},{ident},"
                f"{tails[tpl]},{cell},"
                + ",".join(suffix_tail)
                + "\n"
            )
            encoded = len(line) if line.isascii() else len(line.encode("utf-8"))
            if written + encoded > file_cap:
                break
            batch.append(line)
            batch_bytes += encoded
            written += encoded
            rows += 1
            if batch_bytes >= 4 * 1024 * 1024:
                flush_batch()
            if rows % 1_000_000 == 0:
                print(f"rows {rows} file_gb {written / 1e9:.2f}", flush=True)
        flush_batch()

    print(
        f"wrote {args.output} rows={rows} file_gb={written / 1e9:.3f} "
        f"from {datetime.fromtimestamp(window_start, timezone.utc).isoformat()} "
        f"to {end.isoformat()}",
        flush=True,
    )


if __name__ == "__main__":
    main()
