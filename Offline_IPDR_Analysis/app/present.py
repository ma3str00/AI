"""Render epoch columns as GMT+5 wall time before they reach the page or the explainer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Config

TIME_COLUMNS = {"start_time", "end_time", "recv_time"}


def present_rows(columns: list[str], rows: list[dict], config: Config) -> list[dict]:
    zone = _zone(config)
    rendered = []
    for row in rows:
        item = {}
        for column in columns:
            item[column] = _present_value(column, row.get(column), zone)
        rendered.append(item)
    return rendered


def _zone(config: Config) -> timezone:
    if config.stored_as == "local_epoch":
        return timezone.utc
    text = config.display_offset.strip()
    sign = -1 if text.startswith("-") else 1
    body = text[1:] if text[:1] in "+-" else text
    hours, _, minutes = body.partition(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes or 0)))


def _present_value(column: str, value, zone: timezone):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if column in TIME_COLUMNS and value not in (None, ""):
        try:
            epoch = int(value)
        except (TypeError, ValueError):
            return value
        if epoch > 1_000_000_000:
            return datetime.fromtimestamp(epoch, zone).strftime("%Y-%m-%d %H:%M:%S")
    return value
