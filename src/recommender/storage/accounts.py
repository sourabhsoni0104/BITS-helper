"""Local account and bearer-session storage for a single-user-facing app."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


PASSWORD_ITERATIONS = 310_000
DEFAULT_SESSION_SECONDS = 60 * 60 * 24 * 14
MAX_FAILED_LOGINS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
SESSION_SWEEP_SECONDS = 300.0


class AccountError(ValueError):
    """Account operation failed without disclosing sensitive account state."""


class AccountExistsError(AccountError):
    pass


class AccountLockedError(AccountError):
    """Too many failed login attempts; the caller must wait before retrying."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("Too many failed login attempts. Try again later.")
        self.retry_after = max(1.0, float(retry_after))


class AccountRepository:
    def __init__(self, database: str | Path, *, session_seconds: int = DEFAULT_SESSION_SECONDS) -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self.session_seconds = max(60, int(session_seconds))
        self.connection = sqlite3.connect(self.database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.database != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                profile_id TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'student',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
                csrf_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS account_sessions_user ON account_sessions(user_id);
            CREATE INDEX IF NOT EXISTS account_sessions_expires ON account_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS login_attempts (
                email TEXT PRIMARY KEY,
                failed_count INTEGER NOT NULL DEFAULT 0,
                locked_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS recommendation_history (
                history_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
                entry_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recommendation_history_user ON recommendation_history(user_id,created_at DESC);
        """)

        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(accounts)")}
        if "role" not in columns:
            self.connection.execute("ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'student'")
        
        
        
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(account_sessions)")}
        if "csrf_token" in columns:
            self.connection.execute(
                "CREATE TABLE account_sessions_clean ("
                "token_hash TEXT PRIMARY KEY,"
                "user_id TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,"
                "csrf_hash TEXT NOT NULL,"
                "expires_at REAL NOT NULL,"
                "created_at REAL NOT NULL)"
            )
            self.connection.execute(
                "INSERT INTO account_sessions_clean(token_hash,user_id,csrf_hash,expires_at,created_at) "
                "SELECT token_hash,user_id,csrf_hash,expires_at,created_at FROM account_sessions"
            )
            self.connection.execute("DROP TABLE account_sessions")
            self.connection.execute("ALTER TABLE account_sessions_clean RENAME TO account_sessions")
        self._last_sweep = 0.0
        self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    @staticmethod
    def _email(value: str) -> str:
        if not isinstance(value, str):
            raise AccountError("Enter a valid email address.")
        email = value.strip().casefold()
        if len(email) > 254 or email.count("@") != 1 or any(ch.isspace() for ch in email):
            raise AccountError("Enter a valid email address.")
        local, domain = email.rsplit("@", 1)
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise AccountError("Enter a valid email address.")
        return email

    @staticmethod
    def _password_bytes(password: str) -> bytes:
        if not isinstance(password, str) or len(password) < 8 or len(password) > 1024:
            raise AccountError("Password must be between 8 and 1024 characters.")
        return password.encode("utf-8")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _sweep_expired(self, now: float) -> None:
        """Periodically purge expired sessions so they do not accumulate forever.

        Callers must already hold self.lock. Throttled to once per SESSION_SWEEP_SECONDS.
        """
        if now - self._last_sweep < SESSION_SWEEP_SECONDS:
            return
        self._last_sweep = now
        self.connection.execute("DELETE FROM account_sessions WHERE expires_at <= ?", (now,))
        self.connection.execute("DELETE FROM login_attempts WHERE locked_until > 0 AND locked_until <= ?", (now,))
        self.connection.commit()

    def _check_lockout(self, normalized: str, now: float) -> None:
        row = self.connection.execute(
            "SELECT failed_count,locked_until FROM login_attempts WHERE email=?", (normalized,)
        ).fetchone()
        if row is not None and float(row["locked_until"]) > now:
            raise AccountLockedError(float(row["locked_until"]) - now)

    def _record_failed_login(self, normalized: str, now: float) -> None:
        row = self.connection.execute(
            "SELECT failed_count FROM login_attempts WHERE email=?", (normalized,)
        ).fetchone()
        failed = (int(row["failed_count"]) if row else 0) + 1
        locked_until = now + LOGIN_LOCKOUT_SECONDS if failed >= MAX_FAILED_LOGINS else 0.0
        self.connection.execute(
            "INSERT INTO login_attempts(email,failed_count,locked_until) VALUES(?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET failed_count=?, locked_until=?",
            (normalized, failed, locked_until, failed, locked_until),
        )
        self.connection.commit()

    def _clear_failed_logins(self, normalized: str) -> None:
        self.connection.execute("DELETE FROM login_attempts WHERE email=?", (normalized,))
        self.connection.commit()

    def register(self, email: str, password: str) -> tuple[str, str]:
        normalized = self._email(email)
        secret = self._password_bytes(password)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", secret, salt, PASSWORD_ITERATIONS)
        user_id, profile_id = secrets.token_urlsafe(18), secrets.token_urlsafe(24)
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                count = self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
                role = "admin" if count == 0 else "student"
                self.connection.execute(
                    "INSERT INTO accounts(user_id,email,password_salt,password_hash,profile_id,role,created_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, normalized, salt, digest, profile_id, role, time.time()),
                )
                self.connection.commit()
            except sqlite3.IntegrityError as exc:
                self.connection.rollback()
                raise AccountExistsError("An account with that email already exists.") from exc
            except BaseException:
                self.connection.rollback()
                raise
        return user_id, profile_id

    def login(self, email: str, password: str) -> tuple[str, dict[str, Any]]:
        normalized = self._email(email)
        secret = self._password_bytes(password)
        now = time.time()
        with self.lock:
            self._sweep_expired(now)
            
            
            self._check_lockout(normalized, now)
            row = self.connection.execute("SELECT * FROM accounts WHERE email = ?", (normalized,)).fetchone()
        if row is None:
            
            hashlib.pbkdf2_hmac("sha256", secret, b"account-login-dummy-salt", PASSWORD_ITERATIONS)
            with self.lock:
                self._record_failed_login(normalized, time.time())
            raise AccountError("Email or password is incorrect.")
        actual = hashlib.pbkdf2_hmac("sha256", secret, row["password_salt"], PASSWORD_ITERATIONS)
        if not hmac.compare_digest(actual, row["password_hash"]):
            with self.lock:
                self._record_failed_login(normalized, time.time())
            raise AccountError("Email or password is incorrect.")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = time.time() + self.session_seconds
        with self.lock:
            self._clear_failed_logins(normalized)
            self.connection.execute(
                "INSERT INTO account_sessions(token_hash,user_id,csrf_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (self._token_hash(token), row["user_id"], self._token_hash(csrf_token), expires_at, time.time()),
            )
            self.connection.commit()
        return token, {
            "user_id": row["user_id"],
            "profile_id": row["profile_id"],
            "role": row["role"],
            "csrf_token": csrf_token,
            "expires_at": expires_at,
        }

    def get_session(self, token: str | None) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token:
            return None
        token_hash = self._token_hash(token)
        now = time.time()
        with self.lock:
            self._sweep_expired(now)
            row = self.connection.execute(
                "SELECT a.user_id,a.profile_id,a.role,s.expires_at FROM account_sessions s "
                "JOIN accounts a ON a.user_id=s.user_id WHERE s.token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is not None and row["expires_at"] <= now:
                self.connection.execute("DELETE FROM account_sessions WHERE token_hash=?", (token_hash,))
                self.connection.commit()
                return None
        if row is None:
            return None
        return {
            "user_id": row["user_id"], "profile_id": row["profile_id"], "role": row["role"],
            "expires_at": row["expires_at"],
        }

    def verify_csrf(self, token: str | None, csrf_token: str | None) -> bool:
        if not isinstance(token, str) or not isinstance(csrf_token, str):
            return False
        token_hash = self._token_hash(token)
        now = time.time()
        with self.lock:
            row = self.connection.execute(
                "SELECT csrf_hash,expires_at FROM account_sessions WHERE token_hash=?", (token_hash,)
            ).fetchone()
        return bool(row and row["expires_at"] > now and hmac.compare_digest(row["csrf_hash"], self._token_hash(csrf_token)))

    def logout(self, token: str | None) -> None:
        if not isinstance(token, str) or not token:
            return
        with self.lock:
            self.connection.execute("DELETE FROM account_sessions WHERE token_hash=?", (self._token_hash(token),))
            self.connection.commit()

    def owns_profile(self, user_id: str, profile_id: str) -> bool:
        if not isinstance(user_id, str) or not isinstance(profile_id, str):
            return False
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM accounts WHERE user_id=? AND profile_id=?", (user_id, profile_id)
            ).fetchone()
        return row is not None

    def session_for_profile(self, token: str | None, profile_id: str) -> bool:
        session = self.get_session(token)
        return bool(session and session["profile_id"] == profile_id and self.owns_profile(session["user_id"], profile_id))

    def add_recommendation(self, user_id: str, entry: dict[str, Any]) -> None:
        if not isinstance(user_id, str) or not user_id or not isinstance(entry, dict):
            raise AccountError("Recommendation history entry is invalid.")
        payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 128 * 1024:
            raise AccountError("Recommendation history entry is too large.")
        with self.lock:
            if self.connection.execute("SELECT 1 FROM accounts WHERE user_id=?", (user_id,)).fetchone() is None:
                raise AccountError("Account not found.")
            self.connection.execute(
                "INSERT INTO recommendation_history(history_id,user_id,entry_json,created_at) VALUES(?,?,?,?)",
                (secrets.token_urlsafe(18), user_id, payload, time.time()),
            )
            self.connection.execute(
                "DELETE FROM recommendation_history WHERE user_id=? AND history_id NOT IN "
                "(SELECT history_id FROM recommendation_history WHERE user_id=? ORDER BY created_at DESC LIMIT 50)",
                (user_id, user_id),
            )
            self.connection.commit()

    def recommendation_history(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self.lock:
            rows = self.connection.execute(
                "SELECT history_id,entry_json,created_at FROM recommendation_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [{"history_id": row["history_id"], "created_at": row["created_at"], **json.loads(row["entry_json"])} for row in rows]

    def clear_recommendation_history(self, user_id: str) -> None:
        with self.lock:
            self.connection.execute("DELETE FROM recommendation_history WHERE user_id=?", (user_id,))
            self.connection.commit()
