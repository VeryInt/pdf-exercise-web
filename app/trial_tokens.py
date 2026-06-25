from __future__ import annotations

import hashlib
import ipaddress
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import connect, ensure_dirs, utc_now


class TrialTokenError(Exception):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_ip(value: str) -> str:
    return str(ipaddress.ip_address(value.strip()))


def parse_datetime(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def create_trial_token(
    *,
    bound_ip: str,
    max_uses: int | None,
    expires_at: str | None,
    note: str = "",
) -> dict[str, Any]:
    bound_ip = normalize_ip(bound_ip)
    if max_uses is not None and max_uses < 1:
        raise ValueError("有限次数必须大于等于 1。")
    expires_at = parse_datetime(expires_at)
    if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
        raise ValueError("过期时间必须晚于当前时间。")

    raw_token = f"trial_{secrets.token_urlsafe(32)}"
    token_id = uuid.uuid4().hex
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO trial_tokens (
                id, token_hash, token_prefix, bound_ip, max_uses, used_count,
                status, note, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?, ?)
            """,
            (
                token_id,
                token_hash(raw_token),
                raw_token[:14],
                bound_ip,
                max_uses,
                note.strip()[:300],
                now,
                expires_at,
            ),
        )
    return {
        "token": raw_token,
        "id": token_id,
        "token_prefix": raw_token[:14],
        "bound_ip": bound_ip,
        "max_uses": max_uses,
        "used_count": 0,
        "reserved_count": 0,
        "remaining": max_uses,
        "status": "active",
        "note": note.strip()[:300],
        "created_at": now,
        "expires_at": expires_at,
    }


def _token_row(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM trial_tokens WHERE token_hash = ?",
        (token_hash(raw_token),),
    ).fetchone()


def _is_usable(row: sqlite3.Row, client_ip: str, reserved_count: int) -> bool:
    if row["status"] != "active" or row["bound_ip"] != normalize_ip(client_ip):
        return False
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        return False
    return row["max_uses"] is None or row["used_count"] + reserved_count < row["max_uses"]


def inspect_trial_token(raw_token: str, client_ip: str) -> dict[str, Any] | None:
    if not raw_token:
        return None
    try:
        client_ip = normalize_ip(client_ip)
    except ValueError:
        return None
    with connect() as conn:
        row = _token_row(conn, raw_token)
        if not row:
            return None
        reserved_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM trial_reservations WHERE token_id = ? AND status = 'reserved'",
                (row["id"],),
            ).fetchone()["count"]
        )
        if not _is_usable(row, client_ip, reserved_count):
            return None
        remaining = None
        if row["max_uses"] is not None:
            remaining = max(0, row["max_uses"] - row["used_count"] - reserved_count)
        return {
            "id": row["id"],
            "token_prefix": row["token_prefix"],
            "max_uses": row["max_uses"],
            "used_count": row["used_count"],
            "reserved_count": reserved_count,
            "remaining": remaining,
            "expires_at": row["expires_at"],
        }


def reserve_trial_token(raw_token: str, client_ip: str, job_id: str) -> dict[str, Any]:
    client_ip = normalize_ip(client_ip)
    ensure_dirs()
    conn = sqlite3.connect(settings.database_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT status FROM trial_reservations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if existing:
            raise TrialTokenError("试用授权无效，请检查链接或联系管理员。")
        row = _token_row(conn, raw_token)
        if not row:
            raise TrialTokenError("试用授权无效，请检查链接或联系管理员。")
        reserved_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM trial_reservations WHERE token_id = ? AND status = 'reserved'",
                (row["id"],),
            ).fetchone()["count"]
        )
        if not _is_usable(row, client_ip, reserved_count):
            raise TrialTokenError("试用授权无效，请检查链接或联系管理员。")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO trial_reservations (job_id, token_id, client_ip, status, created_at, updated_at)
            VALUES (?, ?, ?, 'reserved', ?, ?)
            """,
            (job_id, row["id"], client_ip, now, now),
        )
        conn.commit()
        remaining = None
        if row["max_uses"] is not None:
            remaining = row["max_uses"] - row["used_count"] - reserved_count - 1
        return {
            "token_id": row["id"],
            "remaining": remaining,
            "max_uses": row["max_uses"],
            "expires_at": row["expires_at"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_trial_reservation(job_id: str) -> bool:
    ensure_dirs()
    conn = sqlite3.connect(settings.database_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT token_id, status FROM trial_reservations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row or row["status"] == "finalized":
            conn.commit()
            return bool(row)
        if row["status"] != "reserved":
            conn.commit()
            return False
        now = utc_now()
        conn.execute(
            "UPDATE trial_reservations SET status = 'finalized', updated_at = ? WHERE job_id = ?",
            (now, job_id),
        )
        conn.execute(
            "UPDATE trial_tokens SET used_count = used_count + 1 WHERE id = ?",
            (row["token_id"],),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_trial_reservation(job_id: str) -> bool:
    with connect() as conn:
        result = conn.execute(
            """
            UPDATE trial_reservations
            SET status = 'released', updated_at = ?
            WHERE job_id = ? AND status = 'reserved'
            """,
            (utc_now(), job_id),
        )
    return result.rowcount == 1


def release_stale_trial_reservations() -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=settings.trial_reservation_timeout_hours)
    ).isoformat()
    with connect() as conn:
        result = conn.execute(
            """
            UPDATE trial_reservations
            SET status = 'released', updated_at = ?
            WHERE status = 'reserved' AND created_at < ?
            """,
            (utc_now(), cutoff),
        )
    return result.rowcount


def revoke_trial_token(token_id: str) -> bool:
    with connect() as conn:
        result = conn.execute(
            "UPDATE trial_tokens SET status = 'revoked' WHERE id = ? AND status != 'revoked'",
            (token_id,),
        )
    return result.rowcount == 1


def list_trial_tokens() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                trial_tokens.*,
                COALESCE(SUM(CASE WHEN trial_reservations.status = 'reserved' THEN 1 ELSE 0 END), 0)
                    AS reserved_count,
                MAX(trial_reservations.updated_at) AS last_used_at,
                (
                    SELECT job_id FROM trial_reservations latest
                    WHERE latest.token_id = trial_tokens.id
                    ORDER BY latest.updated_at DESC LIMIT 1
                ) AS last_job_id,
                COALESCE(ip_geo_cache.country, '') AS country,
                COALESCE(ip_geo_cache.as_name, '') AS as_name
            FROM trial_tokens
            LEFT JOIN trial_reservations ON trial_reservations.token_id = trial_tokens.id
            LEFT JOIN ip_geo_cache ON ip_geo_cache.ip = trial_tokens.bound_ip
            GROUP BY trial_tokens.id
            ORDER BY trial_tokens.created_at DESC
            """
        ).fetchall()
    result = []
    now = datetime.now(timezone.utc)
    for row in rows:
        item = dict(row)
        if (
            item["status"] == "active"
            and item["expires_at"]
            and datetime.fromisoformat(item["expires_at"]) <= now
        ):
            item["status"] = "expired"
        item["remaining"] = (
            None
            if item["max_uses"] is None
            else max(0, item["max_uses"] - item["used_count"] - item["reserved_count"])
        )
        result.append(item)
    return result


def recent_trial_ips(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                visitor_events.client_ip,
                MAX(visitor_events.created_at) AS last_seen,
                COUNT(*) AS event_count,
                COALESCE(ip_geo_cache.country, '') AS country,
                COALESCE(ip_geo_cache.as_name, '') AS as_name
            FROM visitor_events
            LEFT JOIN ip_geo_cache ON ip_geo_cache.ip = visitor_events.client_ip
            WHERE visitor_events.client_ip != ''
            GROUP BY visitor_events.client_ip
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
