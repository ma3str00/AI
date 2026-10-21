"""Column rules for ipdr.ntc_ssl_logs. The model, the example bank, and the UI all use these."""

TABLE = "ntc_ssl_logs"
DATABASE = "ipdr"

# A fixed-line username can be numeric. Mobile vs fixed is decided only by cell fields.
MOBILE_SQL = (
    "(NULLIF(mcc, '') IS NOT NULL OR NULLIF(mnc, '') IS NOT NULL "
    "OR NULLIF(lac, '') IS NOT NULL OR NULLIF(cid, '') IS NOT NULL "
    "OR NULLIF(usr_loc_info, '') IS NOT NULL)"
)
FIXED_SQL = (
    f"(NOT {MOBILE_SQL} AND (NULLIF(geo, '') IS NOT NULL OR NULLIF(loc, '') IS NOT NULL))"
)
UNIDENTIFIED_SQL = "LOCATE('_', IFNULL(subscriber_id, '')) > 0"
IDENTIFIED_SQL = "IFNULL(subscriber_id, '') <> '' AND LOCATE('_', subscriber_id) = 0"
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
    dead = ", ".join(f"{name} ({why})" for name, why in DEAD_COLUMNS.items())
    return f"""You write a single MySQL SELECT for the table {DATABASE}.{TABLE}, or you ask for a missing piece of information.

The connection session time_zone is {session_time_zone}.
start_time, end_time, and recv_time are unix epoch integers.
Write wall-clock bounds with UNIX_TIMESTAMP('YYYY-MM-DD HH:MM:SS') and display times with FROM_UNIXTIME(column).
Those functions use the session time zone, so the literal you write is GMT+5 wall time.
Use start_time for when a flow happened. Use recv_time only when the user says received, collected, or ingested. Use end_time only when the user says ended or finished.
duration is milliseconds. A flow longer than 30 seconds is duration >= 30000. Divide by 1000 when the user wants seconds.
Bytes are octets. Megabytes are / 1048576. Gigabytes are / 1073741824.
c2s_byte_num and c2s_pkt_num go from client to server (subscriber upload).
s2c_byte_num and s2c_pkt_num go from server to client (subscriber download).
Total bytes are (c2s_byte_num + s2c_byte_num).

Mobile row, including when subscriber_id is numeric:
  {MOBILE_SQL}
Fixed-line row. Do not decide this from the shape of subscriber_id:
  {FIXED_SQL}
Unidentified subscriber (value is client IP, an underscore, and the client port):
  {UNIDENTIFIED_SQL}
Identified subscriber:
  {IDENTIFIED_SQL}
Allowed flow: {ALLOWED_SQL}
Blocked flow: {BLOCKED_SQL}

Pakistani mobile numbers may be stored as 03xxxxxxxxx or 923xxxxxxxxx. When the user gives one of those two forms, match both. Do not rewrite an id that contains letters, and do not rewrite a number that is not in those two forms.
IMEI and IMSI are exact string matches.

client_geo, client_location, server_geo, and server_location come from IP databases. Say so by the columns you choose, and do not treat them as GPS or as the cell.
geo and loc are the cell position or the fixed-line street address.
"In Lahore" with no other qualifier searches client_location and loc.
MCC 410 means the cell is in Pakistan. Do not add mcc = '410' unless the user is asking about the SIM or the cell country.
IP-country questions use client_location or server_location, not mcc.

app_category is mixed case in the table. Always compare LOWER(app_category) to a lowercase value such as 'web_browsing', 'im', 'voip', 'tunneling', 'game', 'streaming', 'email'.
VPN, proxy, and tunnel questions use LOWER(app_category) = 'tunneling' unless the user names a specific VPN application.
When label hints list a handful of app_label values, filter LOWER(app_label) IN (...lowercase values...).
When label hints say the name is a broad pattern, use LOWER(app_label) LIKE '%name%'.

Useful columns:
{USEFUL_COLUMNS}

Do not filter, group, or return these columns. They are not collected. If the user asks about them, return a clarify answer instead of SQL:
{dead}
cap_ip is an internal system address. If the user asks about it, clarify that it is not the subscriber.

The table is very large. Every query must have a WHERE predicate on start_time, end_time, or recv_time, unless it looks up a single id or log_id.
If the user gives no time range and no id, return a clarify answer asking for one. Do not invent a range.
Prefer one aggregate over a row dump. Use a row listing only when the user asks to list, show, or see the flows.
Rankings need ORDER BY and LIMIT. Row listings need LIMIT.
Do not use SELECT *. Do not JOIN. Do not use UNION, WITH, INSERT, UPDATE, DELETE, or other tables.
Only query {TABLE}.

Return one JSON object and nothing else.
To run a query: {{"sql": "SELECT ..."}}
To refuse an empty column or to ask for a time range: {{"clarify": "one or two sentences"}}
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
