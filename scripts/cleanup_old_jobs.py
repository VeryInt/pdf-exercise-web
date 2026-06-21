from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db import connect, init_db


def main() -> None:
    init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.job_retention_hours)
    cutoff_text = cutoff.isoformat()
    with connect() as conn:
        rows = conn.execute("SELECT id, upload_path, work_dir, artifacts_json FROM jobs WHERE created_at < ?", (cutoff_text,)).fetchall()
        for row in rows:
            upload = Path(row["upload_path"])
            upload.unlink(missing_ok=True)
            shutil.rmtree(row["work_dir"], ignore_errors=True)
            shutil.rmtree(settings.artifacts_dir / row["id"], ignore_errors=True)
        conn.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE created_at < ?)", (cutoff_text,))
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff_text,))
    print(f"Cleaned {len(rows)} expired jobs.")


if __name__ == "__main__":
    main()
