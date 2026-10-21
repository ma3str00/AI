"""Explain query rows without a second model call."""

from __future__ import annotations


def explain_rows(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return "No matching flows were found."
    label = _first(columns, ("app_label", "app_category", "subscriber_id", "requested_server", "client_ip", "server_ip", "client_location", "server_location", "hour_gmt5", "day_gmt5"))
    measure = _first(columns, ("total_bytes", "total_mb", "total_gb", "download_bytes", "upload_bytes", "flows", "blocked_flows", "flow_count"))
    if label and measure and len(rows) > 1:
        return _ranking(rows, label, measure, columns)
    if len(rows) == 1:
        return _one_row(rows[0])
    return f"{len(rows)} rows matched. The table below is the result."


def _ranking(rows: list[dict], label: str, measure: str, columns: list[str]) -> str:
    parts = []
    for row in rows[:8]:
        name = row.get(label) or "(blank)"
        amount = _measure_text(row.get(measure), measure)
        extra = ""
        if "flows" in columns and measure != "flows" and row.get("flows") not in (None, ""):
            extra = f", {row.get('flows')} flows"
        parts.append(f"{name} ({amount}{extra})")
    more = ""
    if len(rows) > 8:
        more = f" {len(rows) - 8} more are in the table."
    return f"By {label}, ranked by {measure}: " + "; ".join(parts) + "." + more


def _one_row(row: dict) -> str:
    bits = []
    for key, value in row.items():
        if value in (None, ""):
            continue
        if "byte" in key:
            bits.append(f"{key} {_measure_text(value, key)}")
        else:
            bits.append(f"{key} {value}")
    if not bits:
        return "The query returned one empty row."
    text = "Result: " + ", ".join(bits) + "."
    subscriber = str(row.get("subscriber_id") or "")
    if "_" in subscriber:
        text += " The subscriber id is an unidentified client IP and port."
    return text


def _measure_text(value, name: str) -> str:
    if "byte" not in name:
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if name.endswith("_mb"):
        return f"{number:.2f} MB"
    if name.endswith("_gb"):
        return f"{number:.3f} GB"
    if abs(number) >= 1_073_741_824:
        return f"{number / 1_073_741_824:.2f} GB"
    if abs(number) >= 1_048_576:
        return f"{number / 1_048_576:.2f} MB"
    if abs(number) >= 1024:
        return f"{number / 1024:.1f} KB"
    return f"{int(number)} bytes"


def _first(columns: list[str], names: tuple[str, ...]) -> str | None:
    have = set(columns)
    for name in names:
        if name in have:
            return name
    return None
