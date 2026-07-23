"""邀请码、访问会话、管理员会话和限流的服务端持久化实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import secrets
import sqlite3
from pathlib import Path
from typing import Any


UTC = timezone.utc
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def normalize_invite(code: str) -> str:
    return code.strip().upper()


def generate_invite_code() -> str:
    left = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(4))
    right = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(4))
    return f"TAIL-{left}-{right}"


def secret_hash(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_password_hash(password: str, encoded: str) -> bool:
    """校验 pbkdf2_sha256$迭代次数$salt_b64$digest_b64 格式。"""
    try:
        algorithm, rounds, salt_value, expected_value = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_value.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_password_hash(password: str, rounds: int = 600_000) -> str:
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "$".join(
        (
            "pbkdf2_sha256",
            str(rounds),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


class AccessRepository(ABC):
    @abstractmethod
    def activate_invite(self, code: str, **kwargs: Any) -> str | None: ...

    @abstractmethod
    def validate_session(self, token: str) -> dict[str, Any] | None: ...


class SQLiteAccessRepository(AccessRepository):
    """SQLite 适配器；生产环境必须把文件放在持久卷。"""

    def __init__(self, database_path: str | Path, invite_secret: str, session_secret: str):
        self.database_path = Path(database_path)
        self.invite_secret = invite_secret
        self.session_secret = session_secret
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def migrate(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS invite_codes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  code_hash TEXT NOT NULL UNIQUE,
                  code_prefix TEXT NOT NULL,
                  note TEXT,
                  max_uses INTEGER NOT NULL DEFAULT 1 CHECK(max_uses > 0),
                  used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
                  expires_at TEXT,
                  is_active INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  last_used_at TEXT,
                  created_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_sessions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  invite_code_id INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
                  session_token_hash TEXT NOT NULL UNIQUE,
                  device_label TEXT,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  revoked_at TEXT,
                  is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL,
                  session_token_hash TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  revoked_at TEXT,
                  is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ip_hash TEXT NOT NULL,
                  attempted_at TEXT NOT NULL,
                  user_agent TEXT,
                  was_successful INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_access_session_hash ON access_sessions(session_token_hash);
                CREATE INDEX IF NOT EXISTS idx_admin_session_hash ON admin_sessions(session_token_hash);
                CREATE INDEX IF NOT EXISTS idx_auth_attempt_ip_time ON auth_attempts(ip_hash, attempted_at);
                """
            )

    def create_invites(
        self,
        count: int,
        *,
        max_uses: int = 1,
        expires_at: datetime | None = None,
        note: str = "",
        created_by: str,
    ) -> list[str]:
        if not 1 <= count <= 100 or max_uses < 1:
            raise ValueError("邀请码数量或使用次数无效")
        created: list[str] = []
        now = iso(utc_now())
        with self._connect() as db:
            while len(created) < count:
                code = generate_invite_code()
                digest = secret_hash(self.invite_secret, normalize_invite(code))
                try:
                    db.execute(
                        """INSERT INTO invite_codes
                        (code_hash,code_prefix,note,max_uses,expires_at,is_active,created_at,created_by)
                        VALUES(?,?,?,?,?,1,?,?)""",
                        (digest, f"TAIL-****-{code[-4:]}", note[:200], max_uses, iso(expires_at), now, created_by[:80]),
                    )
                    created.append(code)
                except sqlite3.IntegrityError:
                    continue
        return created

    def activate_invite(
        self,
        code: str,
        *,
        device_label: str,
        session_days: int,
    ) -> str | None:
        normalized = normalize_invite(code)
        digest = secret_hash(self.invite_secret, normalized)
        now = utc_now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM invite_codes WHERE code_hash=?", (digest,)).fetchone()
            if row is None or not hmac.compare_digest(str(row["code_hash"]), digest):
                return None
            expires_at = parse_time(row["expires_at"])
            if not row["is_active"] or row["used_count"] >= row["max_uses"]:
                return None
            if expires_at is not None and expires_at <= now:
                return None
            token = secrets.token_urlsafe(32)
            token_hash = secret_hash(self.session_secret, token)
            session_expires = now + timedelta(days=max(1, min(session_days, 365)))
            db.execute(
                "UPDATE invite_codes SET used_count=used_count+1,last_used_at=? WHERE id=?",
                (iso(now), row["id"]),
            )
            db.execute(
                """INSERT INTO access_sessions
                (invite_code_id,session_token_hash,device_label,created_at,expires_at,last_seen_at,is_active)
                VALUES(?,?,?,?,?,?,1)""",
                (row["id"], token_hash, device_label[:120], iso(now), iso(session_expires), iso(now)),
            )
            return token

    def validate_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        digest = secret_hash(self.session_secret, token)
        now = utc_now()
        with self._connect() as db:
            row = db.execute(
                """SELECT s.*,i.is_active AS invite_active,i.expires_at AS invite_expires_at,
                i.used_count,i.max_uses,i.code_prefix,i.note
                FROM access_sessions s JOIN invite_codes i ON i.id=s.invite_code_id
                WHERE s.session_token_hash=?""",
                (digest,),
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row["session_token_hash"]), digest):
                return None
            invite_expiry = parse_time(row["invite_expires_at"])
            if (
                not row["is_active"]
                or row["revoked_at"]
                or not row["invite_active"]
                or parse_time(row["expires_at"]) <= now
                or (invite_expiry is not None and invite_expiry <= now)
            ):
                return None
            db.execute("UPDATE access_sessions SET last_seen_at=? WHERE id=?", (iso(now), row["id"]))
            return dict(row)

    def revoke_session(self, token: str) -> None:
        digest = secret_hash(self.session_secret, token)
        with self._connect() as db:
            db.execute(
                "UPDATE access_sessions SET is_active=0,revoked_at=? WHERE session_token_hash=?",
                (iso(utc_now()), digest),
            )

    def failure_count(self, ip_hash: str, minutes: int = 15) -> int:
        since = iso(utc_now() - timedelta(minutes=minutes))
        with self._connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM auth_attempts WHERE ip_hash=? AND attempted_at>=? AND was_successful=0",
                    (ip_hash, since),
                ).fetchone()[0]
            )

    def record_attempt(self, ip_hash: str, user_agent: str, successful: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO auth_attempts(ip_hash,attempted_at,user_agent,was_successful) VALUES(?,?,?,?)",
                (ip_hash, iso(utc_now()), user_agent[:240], int(successful)),
            )

    def create_admin_session(self, username: str, session_hours: int = 12) -> str:
        token = secrets.token_urlsafe(32)
        digest = secret_hash(self.session_secret, token)
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO admin_sessions
                (username,session_token_hash,created_at,expires_at,last_seen_at,is_active)
                VALUES(?,?,?,?,?,1)""",
                (username, digest, iso(now), iso(now + timedelta(hours=session_hours)), iso(now)),
            )
        return token

    def validate_admin_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        digest = secret_hash(self.session_secret, token)
        with self._connect() as db:
            row = db.execute("SELECT * FROM admin_sessions WHERE session_token_hash=?", (digest,)).fetchone()
            if (
                row is None
                or not hmac.compare_digest(str(row["session_token_hash"]), digest)
                or not row["is_active"]
                or row["revoked_at"]
                or parse_time(row["expires_at"]) <= utc_now()
            ):
                return None
            db.execute("UPDATE admin_sessions SET last_seen_at=? WHERE id=?", (iso(utc_now()), row["id"]))
            return dict(row)

    def revoke_admin_session(self, token: str) -> None:
        digest = secret_hash(self.session_secret, token)
        with self._connect() as db:
            db.execute(
                "UPDATE admin_sessions SET is_active=0,revoked_at=? WHERE session_token_hash=?",
                (iso(utc_now()), digest),
            )

    def list_invites(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,code_prefix,note,max_uses,used_count,expires_at,is_active,
                created_at,last_used_at,created_by FROM invite_codes ORDER BY id DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def set_invite_active(self, invite_id: int, active: bool, revoke_sessions: bool = True) -> bool:
        with self._connect() as db:
            updated = db.execute(
                "UPDATE invite_codes SET is_active=? WHERE id=?", (int(active), invite_id)
            ).rowcount
            if updated and (not active) and revoke_sessions:
                db.execute(
                    "UPDATE access_sessions SET is_active=0,revoked_at=? WHERE invite_code_id=? AND is_active=1",
                    (iso(utc_now()), invite_id),
                )
            return bool(updated)

    def revoke_invite_sessions(self, invite_id: int) -> int:
        with self._connect() as db:
            return db.execute(
                "UPDATE access_sessions SET is_active=0,revoked_at=? WHERE invite_code_id=? AND is_active=1",
                (iso(utc_now()), invite_id),
            ).rowcount
