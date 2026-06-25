from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = "127.0.0.1"
    app_port: int = 8719
    public_base_url: str = "http://127.0.0.1:18437"
    data_dir: Path = Path("/opt/pdf-exercise-web/data")
    database_path: Path = Path("/opt/pdf-exercise-web/var/pdf_exercise.sqlite3")
    job_retention_hours: int = 24
    max_upload_mb: int = 10
    max_active_jobs: int = 2
    max_active_jobs_per_ip: int = 1
    max_jobs_per_ip_per_hour: int = 5
    worker_poll_seconds: int = 3
    visitor_stats_token: str = ""
    visitor_event_retention_days: int = 90
    ipinfo_token: str = ""
    ipinfo_cache_days: int = 30
    shared_access_token: str = ""
    shared_ai_provider: str = "openai"
    shared_ai_base_url: str = ""
    shared_ai_api_key: str = ""
    shared_ai_model: str = "gpt-5.5"
    token_admin_token: str = ""
    trial_reservation_timeout_hours: int = 2
    trial_token_default_days: int = 7

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


settings = Settings()
