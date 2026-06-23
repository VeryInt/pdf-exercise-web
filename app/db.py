from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    for path in [settings.uploads_dir, settings.jobs_dir, settings.artifacts_dir, settings.tmp_dir]:
        path.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                subject TEXT NOT NULL,
                diagram_strategy TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT '',
                client_ip TEXT NOT NULL DEFAULT '',
                upload_path TEXT NOT NULL,
                work_dir TEXT NOT NULL,
                artifacts_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "client_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN client_id TEXT NOT NULL DEFAULT ''")
        if "client_ip" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visitor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                client_ip TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                referer TEXT NOT NULL DEFAULT '',
                job_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_events_created_at ON visitor_events(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_events_type_created_at ON visitor_events(event_type, created_at)")


def create_job(
    *,
    job_id: str,
    subject: str,
    diagram_strategy: str,
    original_filename: str,
    client_id: str,
    client_ip: str,
    upload_path: Path,
    work_dir: Path,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, progress, subject, diagram_strategy, original_filename,
                client_id, client_ip, upload_path, work_dir, created_at, updated_at
            ) VALUES (?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                subject,
                diagram_strategy,
                original_filename,
                client_id,
                client_ip,
                str(upload_path),
                str(work_dir),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, level, message, created_at) VALUES (?, 'info', ?, ?)",
            (job_id, "任务已创建，等待 worker 处理。", now),
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_events(job_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT level, message, created_at FROM job_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def active_job_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued', 'running')").fetchone()
    return int(row["count"])


def active_job_count_for_ip(client_ip: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE client_ip = ? AND status IN ('queued', 'running')",
            (client_ip,),
        ).fetchone()
    return int(row["count"])


def hourly_job_count_for_ip(client_ip: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE client_ip = ? AND created_at >= ?",
            (client_ip, cutoff),
        ).fetchone()
    return int(row["count"])


def queue_position(job_id: str) -> int | None:
    job = get_job(job_id)
    if not job:
        return None
    if job["status"] == "running":
        return 0
    if job["status"] != "queued":
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM jobs
            WHERE status = 'queued'
              AND created_at <= (SELECT created_at FROM jobs WHERE id = ?)
            """,
            (job_id,),
        ).fetchone()
    return int(row["count"])


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    error: str | None = None,
    artifacts: dict[str, str] | None = None,
    event: str | None = None,
    level: str = "info",
) -> None:
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [utc_now()]
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if progress is not None:
        fields.append("progress = ?")
        values.append(progress)
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if artifacts is not None:
        fields.append("artifacts_json = ?")
        values.append(json.dumps(artifacts, ensure_ascii=False))
    values.append(job_id)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
        if event:
            conn.execute(
                "INSERT INTO job_events (job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (job_id, level, event, utc_now()),
            )


def claim_next_job() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        conn.execute(
            "UPDATE jobs SET status = 'running', progress = 5, updated_at = ? WHERE id = ?",
            (utc_now(), job["id"]),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, level, message, created_at) VALUES (?, 'info', ?, ?)",
            (job["id"], "worker 已开始处理。", utc_now()),
        )
        return job


def artifacts_for(job: dict[str, Any]) -> dict[str, str]:
    try:
        loaded = json.loads(job.get("artifacts_json") or "{}")
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


def record_visitor_event(
    *,
    event_type: str,
    client_ip: str,
    client_id: str = "",
    method: str = "",
    path: str = "",
    user_agent: str = "",
    referer: str = "",
    job_id: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO visitor_events (
                event_type, client_ip, client_id, method, path, user_agent,
                referer, job_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                client_ip,
                client_id,
                method,
                path,
                user_agent[:500],
                referer[:500],
                job_id,
                utc_now(),
            ),
        )


def visitor_summary(*, since: str) -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_events,
                COUNT(DISTINCT NULLIF(client_ip, '')) AS unique_ips,
                COUNT(DISTINCT NULLIF(client_id, '')) AS unique_clients,
                SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) AS page_views,
                SUM(CASE WHEN event_type = 'job_created' THEN 1 ELSE 0 END) AS job_created,
                SUM(CASE WHEN event_type = 'artifact_download' THEN 1 ELSE 0 END) AS artifact_downloads
            FROM visitor_events
            WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def visitor_daily_counts(*, since: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                substr(created_at, 1, 10) AS day,
                SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) AS page_views,
                SUM(CASE WHEN event_type = 'job_created' THEN 1 ELSE 0 END) AS job_created,
                COUNT(DISTINCT NULLIF(client_ip, '')) AS unique_ips
            FROM visitor_events
            WHERE created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day
            """,
            (since,),
        ).fetchall()
    return [dict(row) for row in rows]


def recent_visitor_events(*, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT event_type, client_ip, client_id, method, path, user_agent, referer, job_id, created_at
            FROM visitor_events
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def visitor_ip_rank(*, since: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                client_ip,
                COUNT(*) AS total_events,
                SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) AS page_views,
                SUM(CASE WHEN event_type = 'job_created' THEN 1 ELSE 0 END) AS job_created,
                COUNT(DISTINCT NULLIF(client_id, '')) AS unique_clients,
                MAX(created_at) AS last_seen
            FROM visitor_events
            WHERE created_at >= ? AND client_ip != ''
            GROUP BY client_ip
            ORDER BY total_events DESC, last_seen DESC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_visitor_events_before(cutoff: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM visitor_events WHERE created_at < ?", (cutoff,)).fetchone()
        conn.execute("DELETE FROM visitor_events WHERE created_at < ?", (cutoff,))
    return int(row["count"])
