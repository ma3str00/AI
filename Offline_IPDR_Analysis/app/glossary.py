"""Words people use, and the one meaning each word has in this table.

A new habit is a new word on an existing meaning. It is not a new query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Meaning:
    id: str
    means: str
    words: tuple[str, ...]
    grain: str | None = None
    measure: str | None = None


# Longer phrases are checked before single words.
MEANINGS: tuple[Meaning, ...] = (
    Meaning(
        "person",
        "One subscriber. A ranking of people groups by subscriber_id and drops ids that contain '_'.",
        ("subscriber", "subscribers", "user", "users", "person", "people", "customer", "customers", "msisdn", "username"),
        grain="subscriber_id",
    ),
    Meaning(
        "same_number",
        "A Pakistani mobile number written 03... or 923... is the same subscriber.",
        ("03 number", "923 number", "same number", "same mobile"),
    ),
    Meaning(
        "unidentified",
        "subscriber_id with an underscore is client IP and port. Leave these out of user rankings.",
        ("unidentified", "unknown user", "ip port", "client ip and port"),
    ),
    Meaning(
        "this_person",
        "The subscriber already named in this conversation.",
        ("this user", "that user", "this subscriber", "that subscriber", "the same user", "the same subscriber", "same user"),
    ),
    Meaning(
        "other_person",
        "Keep the previous result and change only the subscriber.",
        ("what about", "how about", "same for", "and for", "now for"),
    ),
    Meaning(
        "their_apps",
        "Every application that one subscriber used. Group by app_label. Do not drop the previous application.",
        (
            "activities",
            "other activities",
            "activity details",
            "activity detail",
            "more details",
            "details about",
            "details for",
            "the rest",
            "rest of",
            "what else",
            "everything else",
            "what they used",
            "apps used",
            "applications used",
        ),
        grain="app_label",
    ),
    Meaning(
        "application",
        "An application or protocol. A list of them groups by app_label. A named one is a filter.",
        ("application", "applications", "apps", "app", "service", "services", "protocol"),
        grain="app_label",
    ),
    Meaning(
        "category",
        "app_category, compared in lowercase. VPN, proxy, and tunnel mean tunneling. Voice and calls mean voip.",
        (
            "category",
            "categories",
            "type of app",
            "web browsing",
            "browsing",
            "instant messaging",
            "voip",
            "voice call",
            "voice",
            "gaming",
            "games",
            "streaming",
            "email",
            "vpn",
            "proxy",
            "tunnel",
        ),
        grain="app_category",
    ),
    Meaning(
        "website",
        "requested_server, the TLS name. Not an application and not a location.",
        ("website", "websites", "site", "sites", "domain", "domains", "sni", "hostname", "hostnames"),
        grain="requested_server",
    ),
    Meaning(
        "hit",
        "One flow. Rank and sort by COUNT(*), not by bytes.",
        ("hit", "hits", "visit", "visits", "request", "requests"),
        measure="flows",
    ),
    Meaning(
        "traffic",
        "Bytes. Upload plus download unless upload and download are both named.",
        ("traffic", "volume", "usage", "bandwidth", "consumption", "data used"),
        measure="bytes",
    ),
    Meaning(
        "upload",
        "c2s_byte_num, bytes from the subscriber to the server.",
        ("upload", "uploaded", "uplink", "sent", "outgoing"),
        measure="upload",
    ),
    Meaning(
        "download",
        "s2c_byte_num, bytes from the server to the subscriber.",
        ("download", "downloaded", "downlink", "received", "incoming"),
        measure="download",
    ),
    Meaning(
        "unique_users",
        "Add the count of identified subscribers beside the current rows. Do not regroup by subscriber_id.",
        ("unique users", "unique user", "number of users", "user count", "how many users", "how many people", "users against"),
        measure="users",
    ),
    Meaning(
        "also",
        "Add what was asked. Keep the columns and the order already shown.",
        ("also", "as well", "along with", "together with", "corresponding"),
    ),
    Meaning(
        "mobile",
        "A valid subscriber with mcc, mnc, lac, cid, or usr_loc_info filled. Not an application.",
        ("mobile", "cellular", "handset", "on cell", "cell user"),
    ),
    Meaning(
        "fixed",
        "A valid subscriber whose mcc, mnc, lac, cid, and usr_loc_info are empty. A numeric id can still be fixed-line.",
        ("fixed line", "fixed-line", "fixedline", "landline", "broadband", "dsl"),
    ),
    Meaning(
        "any_connection",
        "Do not filter on cell fields. The latest mobile or fixed request wins until this is said.",
        ("regardless", "irrespective", "any connection", "nature of connection", "all connections"),
    ),
    Meaning(
        "ip_place",
        "client_location, server_location, client_geo, and server_geo come from an IP database. They are not where the person is.",
        ("ip location", "geoip", "ip address location"),
    ),
    Meaning(
        "real_place",
        "geo and loc are the cell or the fixed-line premises. mcc 410 is Pakistan.",
        ("cell location", "where they were", "street address", "premises", "address", "location"),
    ),
    Meaning(
        "when",
        "start_time is when the flow happened. recv_time is when the log was received. end_time is when it ended. duration is milliseconds.",
        ("when", "started", "start time", "received", "ended", "how long", "duration"),
    ),
    Meaning(
        "whole_table",
        "No start_time filter. Use this only after a whole-table answer was marked correct, or when the user asks for all the data.",
        ("no time filter", "no time range", "all the data", "all data", "entire data", "whole table"),
    ),
    Meaning(
        "blocked",
        "block_type is not 0.",
        ("blocked", "blocking", "denied", "rejected"),
        measure="blocked",
    ),
    Meaning(
        "allowed",
        "block_type is 0.",
        ("allowed", "allowing", "permitted", "passed"),
        measure="allowed",
    ),
    Meaning(
        "no_limit",
        "Return every grouped row for an application, category, or website list.",
        ("no limit", "without a limit", "all of them", "every one of them", "see all", "show all"),
    ),
)

# Columns that are always empty or constant. A question about them cannot be answered from this table.
UNUSABLE = {
    "user agent": "user_agent is always empty",
    "ja3": "ssl_ja3_fingerprint and ssl_ja3_hash are always empty",
    "certificate": "ssl_cert_issuer and ssl_cert_subject are always empty",
    "roaming": "is_roaming is always empty",
    "device model": "device_model and device_vendor are always empty",
    "data center": "data_center is always empty",
    "sessions column": "the sessions column is always 0. A person saying session means one flow, which is COUNT(*).",
}

_BY_ID = {item.id: item for item in MEANINGS}


def mentions(text: str, meaning_id: str) -> bool:
    """True when the text uses any word stored for this meaning."""
    meaning = _BY_ID[meaning_id]
    folded = text.lower().replace("'", "").replace("’", "").replace('"', "")
    phrases = sorted(meaning.words, key=len, reverse=True)
    for word in phrases:
        if " " in word:
            if word in folded:
                return True
            continue
        if re.search(rf"\b{re.escape(word)}\b", folded):
            return True
    return False


def prompt_block() -> str:
    """Short enough to sit in the model prompt. The full notes stay in this module."""
    lines = ["Use these words. Do not invent a column for a word that is not here."]
    for meaning in MEANINGS:
        words = ", ".join(meaning.words[:8])
        lines.append(f"{words}: {meaning.means}")
    lines.append("user agent, ja3, certificate, roaming, device model, and data center are empty. Do not query them.")
    lines.append("A person saying session means one flow. The sessions column is always 0.")
    return "\n".join(lines)
