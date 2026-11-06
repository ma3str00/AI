"""MySQL-backed application users and privileges."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass

from app.config import Config


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class AppUser:
    id: int
    username: str
    is_admin: bool
    can_ai_assistant: bool
    can_query_builder: bool
    is_active: bool

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "can_ai_assistant": self.can_ai_assistant,
            "can_query_builder": self.can_query_builder,
            "is_active": self.is_active,
        }


class AuthDatabase:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()

    def ping(self) -> tuple[bool, str]:
        try:
            with self._lock:
                conn = self._connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                finally:
                    conn.close()
            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    def ensure_ready(self) -> None:
        """Verify the users table exists and bootstrap the config admin account."""
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW TABLES LIKE 'ipdr_users'")
                    if not cur.fetchone():
                        raise AuthError(
                            "Table ipdr_users is missing. Run scripts/create_auth_users.sql "
                            f"on database {self.config.auth_mysql_database}."
                        )
                    self._upsert_admin(cur)
                conn.commit()
            finally:
                conn.close()

    def authenticate(self, username: str, password: str) -> AppUser | None:
        user_row = self._fetch_by_username(username)
        if not user_row or not user_row["is_active"]:
            return None
        if not verify_password(password, user_row["password_hash"]):
            return None
        return _row_to_user(user_row)

    def get_user(self, user_id: int) -> AppUser | None:
        row = self._fetch_by_id(user_id)
        if not row or not row["is_active"]:
            return None
        return _row_to_user(row)

    def list_users(self) -> list[AppUser]:
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, is_admin, can_ai_assistant, can_query_builder, is_active "
                        "FROM ipdr_users ORDER BY username"
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
        return [_row_to_user(row) for row in rows]

    def create_user(
        self,
        *,
        username: str,
        password: str,
        is_admin: bool,
        can_ai_assistant: bool,
        can_query_builder: bool,
    ) -> AppUser:
        name = _clean_username(username)
        if len(password) < 6:
            raise AuthError("Password must be at least 6 characters.")
        if is_admin:
            can_ai_assistant = True
            can_query_builder = True
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM ipdr_users WHERE username = %s", (name,))
                    if cur.fetchone():
                        raise AuthError(f"Username {name!r} already exists.")
                    cur.execute(
                        "INSERT INTO ipdr_users "
                        "(username, password_hash, is_admin, can_ai_assistant, can_query_builder, is_active) "
                        "VALUES (%s, %s, %s, %s, %s, 1)",
                        (
                            name,
                            hash_password(password),
                            1 if is_admin else 0,
                            1 if can_ai_assistant else 0,
                            1 if can_query_builder else 0,
                        ),
                    )
                    user_id = int(cur.lastrowid)
                conn.commit()
            finally:
                conn.close()
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("User was created but could not be loaded.")
        return user

    def update_user(
        self,
        user_id: int,
        *,
        password: str | None = None,
        is_admin: bool | None = None,
        can_ai_assistant: bool | None = None,
        can_query_builder: bool | None = None,
        is_active: bool | None = None,
    ) -> AppUser:
        row = self._fetch_by_id(user_id)
        if not row:
            raise AuthError("User not found.")
        admin = row["is_admin"] if is_admin is None else bool(is_admin)
        ai = row["can_ai_assistant"] if can_ai_assistant is None else bool(can_ai_assistant)
        query = row["can_query_builder"] if can_query_builder is None else bool(can_query_builder)
        active = row["is_active"] if is_active is None else bool(is_active)
        if admin:
            ai = True
            query = True
        if password is not None and len(password) < 6:
            raise AuthError("Password must be at least 6 characters.")
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    if password is not None:
                        cur.execute(
                            "UPDATE ipdr_users SET password_hash = %s, is_admin = %s, "
                            "can_ai_assistant = %s, can_query_builder = %s, is_active = %s WHERE id = %s",
                            (hash_password(password), int(admin), int(ai), int(query), int(active), user_id),
                        )
                    else:
                        cur.execute(
                            "UPDATE ipdr_users SET is_admin = %s, can_ai_assistant = %s, "
                            "can_query_builder = %s, is_active = %s WHERE id = %s",
                            (int(admin), int(ai), int(query), int(active), user_id),
                        )
                conn.commit()
            finally:
                conn.close()
        user = self.get_user(user_id) if active else _row_to_user({**row, "is_admin": admin, "can_ai_assistant": ai, "can_query_builder": query, "is_active": active})
        if user is None:
            return AppUser(user_id, row["username"], admin, ai, query, active)
        return user

    def _upsert_admin(self, cur) -> None:
        username = _clean_username(self.config.auth_mysql_user)
        password = self.config.auth_mysql_password
        if not username:
            raise AuthError("auth_mysql.user is required.")
        if not password:
            raise AuthError("auth_mysql.password is required.")
        cur.execute("SELECT id FROM ipdr_users WHERE username = %s", (username,))
        existing = cur.fetchone()
        digest = hash_password(password)
        if existing:
            cur.execute(
                "UPDATE ipdr_users SET password_hash = %s, is_admin = 1, "
                "can_ai_assistant = 1, can_query_builder = 1, is_active = 1 WHERE id = %s",
                (digest, existing["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO ipdr_users "
                "(username, password_hash, is_admin, can_ai_assistant, can_query_builder, is_active) "
                "VALUES (%s, %s, 1, 1, 1, 1)",
                (username, digest),
            )

    def _fetch_by_username(self, username: str) -> dict | None:
        name = _clean_username(username)
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, password_hash, is_admin, can_ai_assistant, "
                        "can_query_builder, is_active FROM ipdr_users WHERE username = %s",
                        (name,),
                    )
                    return cur.fetchone()
            finally:
                conn.close()

    def _fetch_by_id(self, user_id: int) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, password_hash, is_admin, can_ai_assistant, "
                        "can_query_builder, is_active FROM ipdr_users WHERE id = %s",
                        (int(user_id),),
                    )
                    return cur.fetchone()
            finally:
                conn.close()

    def _connect(self):
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise AuthError("pymysql is not installed. Run: pip install pymysql") from exc
        return pymysql.connect(
            host=self.config.auth_mysql_host,
            port=self.config.auth_mysql_port,
            user=self.config.auth_mysql_user,
            password=self.config.auth_mysql_password,
            database=self.config.auth_mysql_database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
        )


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(rounds)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(check, digest)


def _clean_username(username: str) -> str:
    name = (username or "").strip()
    if not name or len(name) > 64 or " " in name:
        raise AuthError("Username must be 1–64 characters with no spaces.")
    return name


def _row_to_user(row: dict) -> AppUser:
    return AppUser(
        id=int(row["id"]),
        username=str(row["username"]),
        is_admin=bool(row["is_admin"]),
        can_ai_assistant=bool(row["can_ai_assistant"]),
        can_query_builder=bool(row["can_query_builder"]),
        is_active=bool(row["is_active"]),
    )
