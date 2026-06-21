from __future__ import annotations

import signal
import time

from app.config import settings
from app.db import claim_next_job, init_db
from worker.pipeline import process_job


running = True


def stop(_signum, _frame) -> None:
    global running
    running = False


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    init_db()
    while running:
        job = claim_next_job()
        if job:
            process_job(job)
            continue
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
