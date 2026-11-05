"""One ClickHouse connection (native protocol), read-only queries against ntc_ssl_logs."""

from __future__ import annotations

import threading

from app.config import Config
from app.dialect import to_clickhouse


class DatabaseError(Exception):
    pass


class Database:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._client = None

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.disconnect()
                except Exception:
                    pass
                self._client = None

    def ping(self) -> tuple[bool, str]:
        try:
            with self._lock:
                client = self._connection()
                client.execute("SELECT 1")
            return True, "ok"
        except Exception as exc:
            self._client = None
            return False, str(exc)

    def query(
        self,
        sql: str,
        max_execution_ms: int | None = None,
        unbounded: bool = False,
    ) -> tuple[list[str], list[dict], str | None]:
        with self._lock:
            try:
                client = self._connection()
                clickhouse_sql = to_clickhouse(sql)
                settings: dict = {}
                if max_execution_ms is not None:
                    # ClickHouse max_execution_time is seconds (Float64).
                    settings["max_execution_time"] = max(1.0, float(max_execution_ms) / 1000.0)
                if not unbounded:
                    # Soft cap; LIMIT in SQL is still the primary guard.
                    settings["max_result_rows"] = int(self.config.max_rows)
                warning = self._scan_warning(clickhouse_sql)
                rows, col_types = client.execute(
                    clickhouse_sql,
                    with_column_types=True,
                    settings=settings or None,
                )
                columns = [name for name, _type in col_types]
                dict_rows = [dict(zip(columns, row)) for row in rows]
                return columns, dict_rows, warning
            except Exception as exc:
                self._client = None
                raise DatabaseError(str(exc)) from exc

    def _connection(self):
        if self._client is not None:
            return self._client
        try:
            from clickhouse_driver import Client
        except ImportError as exc:
            raise DatabaseError(
                "clickhouse-driver is not installed. Run: pip install clickhouse-driver"
            ) from exc
        # Native TCP protocol (default port 9000). Not MySQL — ClickHouse's own wire format.
        self._client = Client(
            host=self.config.ch_host,
            port=self.config.ch_port,
            user=self.config.ch_user,
            password=self.config.ch_password,
            database=self.config.ch_database,
            secure=self.config.ch_secure,
            connect_timeout=10,
            send_receive_timeout=int(self.config.full_scan_execution_ms / 1000) + 30,
        )
        return self._client

    def _scan_warning(self, sql: str) -> str | None:
        folded = sql.lower()
        if "start_time" not in folded and "end_time" not in folded and "recv_time" not in folded:
            if "subscriber_id =" not in folded.replace(" ", ""):
                return (
                    "This query may scan a large ClickHouse range. "
                    "Prefer a start_time filter when possible."
                )
        return None
