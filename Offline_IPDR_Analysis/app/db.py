"""One MySQL connection, read-only, with the session clock set for GMT+5 questions."""

from __future__ import annotations

import threading

import pymysql
from pymysql.cursors import DictCursor

from app.config import Config


class DatabaseError(Exception):
    pass


class Database:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def ping(self) -> tuple[bool, str]:
        try:
            with self._lock:
                conn = self._connection()
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 AS ok")
                    cursor.fetchone()
            return True, "ok"
        except Exception as exc:  # connection errors should surface as status, not a crash
            self._conn = None
            return False, str(exc)

    def query(self, sql: str, max_execution_ms: int | None = None) -> tuple[list[str], list[dict], str | None]:
        with self._lock:
            conn = None
            try:
                conn = self._connection()
                if max_execution_ms is not None:
                    with conn.cursor() as cursor:
                        cursor.execute("SET SESSION max_execution_time = %s", (max_execution_ms,))
                warning = self._scan_warning(conn, sql)
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    rows = list(cursor.fetchall())
                    columns = [item[0] for item in cursor.description] if cursor.description else []
                return columns, rows, warning
            except Exception as exc:
                self._conn = None
                raise DatabaseError(str(exc)) from exc
            finally:
                if conn is not None and max_execution_ms is not None and self._conn is not None:
                    with conn.cursor() as cursor:
                        cursor.execute("SET SESSION max_execution_time = %s", (self.config.max_execution_ms,))

    def _connection(self):
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
            except Exception:
                self._conn = None
            else:
                self._apply_session(self._conn)
                return self._conn
        self._conn = pymysql.connect(
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password,
            database=self.config.mysql_database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=10,
            read_timeout=int(self.config.max_execution_ms / 1000) + 15,
            write_timeout=30,
        )
        self._apply_session(self._conn)
        return self._conn

    def _apply_session(self, conn) -> None:
        # Reapplied after every ping because a reconnect drops session settings.
        with conn.cursor() as cursor:
            cursor.execute("SET SESSION time_zone = %s", (self.config.session_time_zone,))
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SET SESSION max_execution_time = %s", (self.config.max_execution_ms,))
            cursor.execute("SET SESSION sql_select_limit = %s", (self.config.max_rows,))

    def _scan_warning(self, conn, sql: str) -> str | None:
        try:
            with conn.cursor() as cursor:
                cursor.execute("EXPLAIN " + sql)
                rows = cursor.fetchall()
        except Exception:
            return None
        for row in rows:
            if str(row.get("type", "")).upper() == "ALL" and not row.get("key"):
                return (
                    "MySQL expects a full table scan. The time limit will stop it if it runs too long. "
                    "An index on start_time makes these questions practical. See scripts/recommended-indexes.sql."
                )
        return None
