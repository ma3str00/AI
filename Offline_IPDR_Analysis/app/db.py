"""One ClickHouse connection, read-only queries against ntc_ssl_logs."""

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
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def ping(self) -> tuple[bool, str]:
        try:
            with self._lock:
                client = self._connection()
                result = client.query("SELECT 1 AS ok")
                _ = result.result_rows
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
                result = client.query(clickhouse_sql, settings=settings or None)
                columns = list(result.column_names)
                rows = [dict(zip(columns, row)) for row in result.result_rows]
                return columns, rows, warning
            except Exception as exc:
                self._client = None
                raise DatabaseError(str(exc)) from exc

    def _connection(self):
        if self._client is not None:
            return self._client
        try:
            import clickhouse_connect
        except ImportError as exc:
            raise DatabaseError(
                "clickhouse-connect is not installed. Run: pip install clickhouse-connect"
            ) from exc
        self._client = clickhouse_connect.get_client(
            host=self.config.ch_host,
            port=self.config.ch_port,
            username=self.config.ch_user,
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
