from __future__ import annotations

from datetime import timedelta
import sqlite3

from services.access_control import (
    SQLiteAccessRepository,
    create_password_hash,
    secret_hash,
    utc_now,
    verify_password_hash,
)


def repository(tmp_path):
    return SQLiteAccessRepository(tmp_path / "auth.db", "invite-secret", "session-secret")


def test_valid_invite_creates_refreshable_persistent_session(tmp_path):
    repo = repository(tmp_path)
    code = repo.create_invites(1, created_by="admin")[0]

    token = repo.activate_invite(code, device_label="browser", session_days=30)

    assert token
    assert repo.validate_session(token)["used_count"] == 1
    restarted = repository(tmp_path)
    assert restarted.validate_session(token) is not None
    with sqlite3.connect(tmp_path / "auth.db") as db:
        stored_hash, masked = db.execute(
            "SELECT code_hash,code_prefix FROM invite_codes"
        ).fetchone()
    assert code not in (stored_hash, masked)
    assert masked.startswith("TAIL-****-")


def test_invalid_expired_and_inactive_invites_are_rejected(tmp_path):
    repo = repository(tmp_path)
    expired = repo.create_invites(
        1, expires_at=utc_now() - timedelta(minutes=1), created_by="admin"
    )[0]
    inactive = repo.create_invites(1, created_by="admin")[0]
    inactive_id = repo.list_invites()[0]["id"]
    repo.set_invite_active(inactive_id, False)

    assert repo.activate_invite("TAIL-AAAA-AAAA", device_label="", session_days=30) is None
    assert repo.activate_invite(expired, device_label="", session_days=30) is None
    assert repo.activate_invite(inactive, device_label="", session_days=30) is None


def test_single_use_and_multi_use_limits(tmp_path):
    repo = repository(tmp_path)
    single = repo.create_invites(1, created_by="admin")[0]
    multi = repo.create_invites(1, max_uses=2, created_by="admin")[0]

    assert repo.activate_invite(single, device_label="", session_days=30)
    assert repo.activate_invite(single, device_label="", session_days=30) is None
    assert repo.activate_invite(multi, device_label="", session_days=30)
    assert repo.activate_invite(multi, device_label="", session_days=30)
    assert repo.activate_invite(multi, device_label="", session_days=30) is None


def test_revocation_logout_and_expiry_invalidate_old_tokens(tmp_path):
    repo = repository(tmp_path)
    first = repo.create_invites(1, created_by="admin")[0]
    token = repo.activate_invite(first, device_label="", session_days=30)
    repo.revoke_session(token)
    assert repo.validate_session(token) is None

    second = repo.create_invites(1, created_by="admin")[0]
    second_token = repo.activate_invite(second, device_label="", session_days=30)
    invite_id = repo.list_invites()[0]["id"]
    repo.set_invite_active(invite_id, False)
    assert repo.validate_session(second_token) is None

    third = repo.create_invites(1, created_by="admin")[0]
    third_token = repo.activate_invite(third, device_label="", session_days=30)
    digest = secret_hash("session-secret", third_token)
    with sqlite3.connect(tmp_path / "auth.db") as db:
        db.execute(
            "UPDATE access_sessions SET expires_at=? WHERE session_token_hash=?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), digest),
        )
    assert repo.validate_session(third_token) is None


def test_rate_attempts_store_no_plain_invite_and_count_failures(tmp_path):
    repo = repository(tmp_path)
    for _ in range(5):
        repo.record_attempt("masked-ip", "test-agent", False)

    assert repo.failure_count("masked-ip") == 5
    with sqlite3.connect(tmp_path / "auth.db") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(auth_attempts)")}
        values = " ".join(str(value) for row in db.execute("SELECT * FROM auth_attempts") for value in row)
    assert "code" not in columns
    assert "TAIL-" not in values


def test_password_hash_is_salted_and_constant_time_verifiable():
    encoded = create_password_hash("correct horse battery staple", rounds=10_000)

    assert "correct horse" not in encoded
    assert verify_password_hash("correct horse battery staple", encoded)
    assert not verify_password_hash("wrong", encoded)
