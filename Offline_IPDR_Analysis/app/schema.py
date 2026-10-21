"""Column rules for ipdr.ntc_ssl_logs. The model, the example bank, and the UI all use these."""

TABLE = "ntc_ssl_logs"
DATABASE = "ipdr"

# A numeric subscriber_id can still be fixed-line. Mobile vs fixed is decided only by cell fields.
MOBILE_SQL = (
    "(NULLIF(mcc, '') IS NOT NULL OR NULLIF(mnc, '') IS NOT NULL "
    "OR NULLIF(lac, '') IS NOT NULL OR NULLIF(cid, '') IS NOT NULL "
    "OR NULLIF(usr_loc_info, '') IS NOT NULL)"
)
UNIDENTIFIED_SQL = "LOCATE('_', IFNULL(subscriber_id, '')) > 0"
IDENTIFIED_SQL = "IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0"
# Valid subscriber whose cell fields are empty. geo/loc are not required.
FIXED_SQL = f"({IDENTIFIED_SQL} AND NOT {MOBILE_SQL})"
ALLOWED_SQL = "block_type = 0"
BLOCKED_SQL = "(block_type IS NOT NULL AND block_type <> 0)"

# Columns the description marks as constant. Asking about them should be refused, not queried.
DEAD_COLUMNS = {
    "direction": "always empty",
    "stream_dir": "always empty",
    "establish_latency": "always 0",
    "processing_time": "always 0",
    "device_id": "always -1",
    "egress_link_id": "always 0",
    "ingress_link_id": "always 0",
    "data_center": "always empty",
    "action": "always empty",
    "l7_protocol": "always empty",
    "sessions": "always 0",
    "first_ttl": "always 0",
    "address_type": "always empty",
    "schema_type": "always empty",
    "client_type": "always empty",
    "version": "always empty",
    "user_agent": "always empty",
    "ssl_con_latency": "always 0",
    "ssl_ja3_fingerprint": "always empty",
    "ssl_ja3_hash": "always empty",
    "ssl_cert_issuer": "always empty",
    "ssl_cert_subject": "always empty",
    "device_vendor": "always empty",
    "device_model": "always empty",
    "is_roaming": "always empty",
}

USEFUL_COLUMNS = """
id bigint primary key
recv_time bigint, unix seconds, when the DPI log was received. Indexed.
log_id varchar(32), log id. Indexed.
start_time bigint, unix seconds, when the flow started. This is the column for "when did this happen".
end_time bigint, unix seconds, when the flow ended. Whole seconds only.
duration int, flow length in milliseconds. More precise than end_time - start_time.
cap_ip varchar(45), internal DPI address. Not the subscriber and not useful for analysis.
requested_server varchar(255), domain or TLS SNI.
client_ip varchar(45), IP of the subscriber side. Indexed.
client_port int
client_geo varchar(64), IP-database coordinates of the client. Not the handset GPS and not the cell location.
client_location text, IP-database city and country of the client. Not the true subscriber address.
client_asn text, client ASN name, not the ASN number.
subscriber_id varchar(128), username. Indexed. Mobile entries are usually a phone number (03... or 923...). Fixed-line entries are a phone number or an alphanumeric username. A numeric value can still be fixed-line. When the subscriber was not identified, this is clientIP_port and contains an underscore.
imei varchar(32), handset IMEI. Empty for fixed-line and for some mobile rows.
imsi varchar(32), SIM IMSI. Empty for fixed-line and for some mobile rows.
server_ip varchar(45)
server_port int
server_geo varchar(64), IP-database coordinates of the server. Not a precise site location.
server_location text, IP-database city and country of the server.
server_asn text, server ASN name, not the ASN number.
app_category varchar(64), category stored in mixed case (Web_Browsing, IM, VoIP, Tunneling, Game, Streaming, Email, Network_Storage, Software_Update, File_Access_Protocol, HTTP_Video, Attack). Compare with LOWER(app_category).
app_label varchar(128), application or protocol name, for example TikTok, Facebook, SSL, Youtube_Others. Indexed. Compare with LOWER(app_label).
l4_protocol varchar(16), TCP or UDP.
c2s_pkt_num bigint, packets sent by the client to the server.
s2c_pkt_num bigint, packets sent by the server to the client.
c2s_byte_num bigint, bytes sent by the client to the server. This is upload from the subscriber.
s2c_byte_num bigint, bytes sent by the server to the client. This is download to the subscriber.
usr_loc_info varchar(64), raw 3GPP user-location hex. Presence means a mobile cell (LTE, 3G, or 2G).
mcc varchar(8), mobile country code of the cell. 410 is Pakistan. Presence means mobile.
mnc varchar(8), mobile network code of the cell.
lac varchar(16), location area code of the cell.
cid varchar(16), cell id (LTE, 3G, or 2G).
geo varchar(64), coordinates of the cell, or of the fixed-line premises. Not client_geo.
loc text, street address of the cell, or of the fixed-line customer. Not client_location.
block_type int, 0 means the DPI allowed the flow. Any other value means the flow was blocked.
""".strip()


def system_prompt(session_time_zone: str) -> str:
    return f"""Write one MySQL SELECT for {DATABASE}.{TABLE}, or a clarify JSON if a time range is missing.
Session time_zone is {session_time_zone}. Timestamps are unix seconds. UNIX_TIMESTAMP('YYYY-MM-DD HH:MM:SS') and FROM_UNIXTIME use GMT+5.
Use start_time for when a flow happened. recv_time only if the user says received. end_time only if the user says ended.
duration is milliseconds. Bytes: c2s_byte_num is upload, s2c_byte_num is download, total is their sum. MB is / 1048576.
Traffic volume means that byte total. Applications means GROUP BY app_label. Categories means LOWER(app_category).
Users or subscribers means GROUP BY subscriber_id. Do not group a users question by app_label. Exclude unidentified ids with AND IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0. A value such as 203.0.113.10_56175 is client IPv4 and client port, not a subscriber.
If the question names an app and asks for users, filter that app with LOWER(app_label) LIKE '%name%' and still group by subscriber_id.
Compare categories in lowercase: web_browsing, im, voip, tunneling, game, streaming, email. VPN means tunneling.
Mobile users means a valid subscriber_id and at least one of mcc, mnc, lac, cid, or usr_loc_info is filled. Fixed-line users means a valid subscriber_id and all of those cell fields are empty. A numeric subscriber_id can be fixed-line. Never filter app_label for mobile or fixed-line, and never write LIKE '%mobile%' or LIKE '%fixed%'.
Match 03... and 923... as the same Pakistani mobile number.
client_location and server_location are IP-database places. geo and loc are the cell or the fixed-line address. data_center is empty. Never use it.
Prefer WHERE start_time. If the user gives no time range, clarify and ask for one. If they explicitly say no time filter or all the data, omit the time predicate.
A follow-up keeps the previous SQL's filters unless the user changes them.
Aggregate unless they ask to list flows. Rankings need ORDER BY and LIMIT. No SELECT *, JOIN, UNION, or other tables.
Return only {{"sql":"SELECT ..."}} or {{"clarify":"one sentence"}}.
"""


EXPLAIN_SYSTEM = """You explain the result of a MySQL query over DPI flow records to the person who asked the question.
Use only the numbers and rows in the result. If the result is empty, say there were no matching flows in that window.
Times in the result are already GMT+5 wall time.
duration is milliseconds.
c2s bytes are upload from the subscriber. s2c bytes are download to the subscriber.
A subscriber_id that contains an underscore is an unidentified session (client IP and port), not a username.
A numeric subscriber_id can be either a mobile number or a fixed-line username. Mobile rows are the ones with mcc, mnc, lac, cid, or usr_loc_info. Fixed-line rows are the ones without those cell fields and with geo or loc.
client_location and server_location are IP-database places, not GPS and not the cell address.
geo and loc are the cell or the fixed-line street address.
block_type 0 was allowed. Any other value was blocked.
Say which columns the answer came from when location or subscriber type matters.
Write a short answer in plain sentences. Do not repeat the SQL unless the result is surprising. Do not invent rows, causes, or identities that are not in the result.
"""

STARTER_QUESTIONS = [
    "How many flows started yesterday?",
    "Top 15 app labels by total bytes over the last 24 hours",
    "How many blocked flows were there yesterday?",
    "How much traffic did TikTok generate yesterday, in MB?",
    "Top subscribers by download bytes yesterday",
    "How many mobile flows and how many fixed-line flows started yesterday?",
    "Which domains did subscriber 923000000000 request in the last 7 days?",
    "How many unidentified sessions started in the last 24 hours?",
    "Flows longer than 60 seconds in the last 6 hours",
    "Hourly flow count and total bytes for today",
]
