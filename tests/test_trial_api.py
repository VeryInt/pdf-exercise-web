from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_job, init_db
from app.main import app


class TrialApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original = {
            "database_path": settings.database_path,
            "data_dir": settings.data_dir,
            "token_admin_token": settings.token_admin_token,
            "shared_ai_base_url": settings.shared_ai_base_url,
            "shared_ai_api_key": settings.shared_ai_api_key,
            "public_base_url": settings.public_base_url,
        }
        root = Path(self.temp.name)
        settings.database_path = root / "test.sqlite3"
        settings.data_dir = root / "data"
        settings.token_admin_token = "admin-test-token"
        settings.shared_ai_base_url = "https://provider.example/v1"
        settings.shared_ai_api_key = "private-test-key"
        settings.public_base_url = "https://example.test"
        init_db()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        for key, value in self.original.items():
            setattr(settings, key, value)
        self.temp.cleanup()

    def test_admin_api_and_trial_job_creation(self) -> None:
        self.assertEqual(self.client.get("/api/internal/trial-tokens").status_code, 404)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        created_response = self.client.post(
            "/api/internal/trial-tokens",
            headers={"X-Token-Admin": "admin-test-token"},
            json={
                "bound_ip": "203.0.113.10",
                "max_uses": 2,
                "expires_at": expires_at,
                "note": "api test",
            },
        )
        self.assertEqual(created_response.status_code, 200)
        created = created_response.json()
        self.assertTrue(created["token"].startswith("trial_"))
        self.assertTrue(created["url"].startswith("https://example.test/#token="))

        status = self.client.get(
            "/api/status",
            headers={
                "CF-Connecting-IP": "203.0.113.10",
                "X-Service-Token": created["token"],
            },
        ).json()
        self.assertTrue(status["shared_access_authorized"])
        self.assertEqual(status["access_mode"], "trial")
        self.assertEqual(status["trial_remaining"], 2)

        wrong_ip = self.client.get(
            "/api/status",
            headers={
                "CF-Connecting-IP": "203.0.113.11",
                "X-Service-Token": created["token"],
            },
        ).json()
        self.assertFalse(wrong_ip["shared_access_authorized"])
        self.assertEqual(wrong_ip["access_mode"], "invalid")

        job_response = self.client.post(
            "/api/jobs",
            headers={
                "CF-Connecting-IP": "203.0.113.10",
                "X-Service-Token": created["token"],
                "X-Client-Id": "client-test",
            },
            data={"client_id": "client-test"},
            files={"file": ("worksheet.png", b"not-a-real-image", "image/png")},
        )
        self.assertEqual(job_response.status_code, 200)
        job = get_job(job_response.json()["job_id"])
        self.assertEqual(job["access_mode"], "trial")
        secrets = json.loads((Path(job["work_dir"]) / "secrets.json").read_text(encoding="utf-8"))
        self.assertEqual(secrets, {"shared_access": True})

    def test_admin_api_can_batch_create_unbound_tokens(self) -> None:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        response = self.client.post(
            "/api/internal/trial-tokens",
            headers={"X-Token-Admin": "admin-test-token"},
            json={
                "bound_ip": "",
                "max_uses": 1,
                "expires_at": expires_at,
                "note": "batch",
                "count": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        created = response.json()
        self.assertEqual(created["count"], 2)
        self.assertEqual(len(created["tokens"]), 2)
        self.assertTrue(all(item["bound_ip"] == "" for item in created["tokens"]))

        first = created["tokens"][0]["token"]
        status = self.client.get(
            "/api/status",
            headers={
                "CF-Connecting-IP": "203.0.113.40",
                "X-Service-Token": first,
            },
        ).json()
        self.assertTrue(status["shared_access_authorized"])
        self.assertEqual(status["trial_remaining"], 1)

        wrong_ip = self.client.get(
            "/api/status",
            headers={
                "CF-Connecting-IP": "203.0.113.41",
                "X-Service-Token": first,
            },
        ).json()
        self.assertFalse(wrong_ip["shared_access_authorized"])


if __name__ == "__main__":
    unittest.main()
