from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db import create_job, init_db
from app.trial_tokens import (
    TrialTokenError,
    create_trial_token,
    create_trial_tokens,
    finalize_trial_reservation,
    inspect_trial_token,
    list_trial_tokens,
    release_trial_reservation,
    reserve_trial_token,
    revoke_trial_token,
    token_hash,
)


class TrialTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        self.original_data = settings.data_dir
        root = Path(self.temp.name)
        settings.database_path = root / "test.sqlite3"
        settings.data_dir = root / "data"
        init_db()

    def tearDown(self) -> None:
        settings.database_path = self.original_database
        settings.data_dir = self.original_data
        self.temp.cleanup()

    def create_token(self, max_uses: int | None = 1) -> dict:
        return create_trial_token(
            bound_ip="203.0.113.10",
            max_uses=max_uses,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            note="test",
        )

    def test_plaintext_is_not_persisted(self) -> None:
        created = self.create_token()
        database_bytes = settings.database_path.read_bytes()
        self.assertNotIn(created["token"].encode(), database_bytes)
        self.assertIn(token_hash(created["token"]).encode(), database_bytes)

    def test_wrong_ip_revoke_and_expiration_are_rejected(self) -> None:
        created = self.create_token()
        self.assertIsNone(inspect_trial_token(created["token"], "203.0.113.11"))
        self.assertTrue(revoke_trial_token(created["id"]))
        self.assertIsNone(inspect_trial_token(created["token"], "203.0.113.10"))

        expired = create_trial_token(
            bound_ip="203.0.113.10",
            max_uses=1,
            expires_at=None,
        )
        from app.db import connect

        with connect() as conn:
            conn.execute(
                "UPDATE trial_tokens SET expires_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), expired["id"]),
            )
        self.assertIsNone(inspect_trial_token(expired["token"], "203.0.113.10"))

    def test_unbound_token_binds_first_access_ip(self) -> None:
        created = create_trial_token(
            bound_ip="",
            max_uses=1,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            note="first-bind",
        )

        inspected = inspect_trial_token(created["token"], "203.0.113.20")
        self.assertIsNotNone(inspected)
        self.assertEqual(inspected["bound_ip"], "203.0.113.20")
        self.assertIsNone(inspect_trial_token(created["token"], "203.0.113.21"))

        reserve_trial_token(created["token"], "203.0.113.20", "job-first-bound")
        listed = {item["id"]: item for item in list_trial_tokens()}[created["id"]]
        self.assertEqual(listed["bound_ip"], "203.0.113.20")

    def test_unbound_last_use_cannot_bind_two_ips_concurrently(self) -> None:
        created = create_trial_token(
            bound_ip="",
            max_uses=1,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def reserve(ip: str, job_id: str) -> None:
            barrier.wait()
            try:
                reserve_trial_token(created["token"], ip, job_id)
                outcomes.append(ip)
            except TrialTokenError:
                outcomes.append("rejected")

        threads = [
            threading.Thread(target=reserve, args=("203.0.113.30", "job-bind-a")),
            threading.Thread(target=reserve, args=("203.0.113.31", "job-bind-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count("rejected"), 1)
        self.assertEqual(len([item for item in outcomes if item != "rejected"]), 1)

    def test_batch_create_unbound_tokens(self) -> None:
        created = create_trial_tokens(
            bound_ip="",
            max_uses=3,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            count=3,
        )
        self.assertEqual(len(created), 3)
        self.assertEqual(len({item["token"] for item in created}), 3)
        self.assertTrue(all(item["bound_ip"] == "" for item in created))

    def test_only_one_concurrent_reservation_gets_last_use(self) -> None:
        created = self.create_token(max_uses=1)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def reserve(job_id: str) -> None:
            barrier.wait()
            try:
                reserve_trial_token(created["token"], "203.0.113.10", job_id)
                outcomes.append("reserved")
            except TrialTokenError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=reserve, args=(f"job-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(outcomes, ["reserved", "rejected"])

    def test_finalize_and_release_are_idempotent(self) -> None:
        created = self.create_token(max_uses=2)
        reserve_trial_token(created["token"], "203.0.113.10", "job-final")
        self.assertTrue(finalize_trial_reservation("job-final"))
        self.assertTrue(finalize_trial_reservation("job-final"))
        self.assertFalse(release_trial_reservation("job-final"))

        reserve_trial_token(created["token"], "203.0.113.10", "job-release")
        self.assertTrue(release_trial_reservation("job-release"))
        self.assertFalse(release_trial_reservation("job-release"))

        listed = {item["id"]: item for item in list_trial_tokens()}[created["id"]]
        self.assertEqual(listed["used_count"], 1)
        self.assertEqual(listed["reserved_count"], 0)
        self.assertEqual(listed["remaining"], 1)

    def test_job_schema_accepts_trial_metadata(self) -> None:
        root = settings.data_dir
        create_job(
            job_id="trial-job",
            subject="auto",
            diagram_strategy="source_crop_first",
            original_filename="test.png",
            client_id="client",
            client_ip="203.0.113.10",
            access_mode="trial",
            trial_token_id="token-id",
            upload_path=root / "upload.png",
            work_dir=root / "job",
        )


if __name__ == "__main__":
    unittest.main()
