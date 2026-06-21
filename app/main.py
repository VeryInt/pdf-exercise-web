from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import (
    active_job_count,
    active_job_count_for_ip,
    artifacts_for,
    create_job,
    ensure_dirs,
    get_job,
    hourly_job_count_for_ip,
    init_db,
    list_events,
    queue_position,
)

app = FastAPI(title="PDF Exercise Maker")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    ensure_dirs()
    init_db()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'"
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


def safe_filename(name: str) -> str:
    keep = [ch if ch.isalnum() or ch in ".-_" else "_" for ch in name]
    result = "".join(keep).strip("._")
    return result or "upload.bin"


def client_ip_from_request(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_job_limits(client_ip: str) -> None:
    active_total = active_job_count()
    if active_total >= settings.max_active_jobs:
        raise HTTPException(
            status_code=429,
            detail=f"当前排队任务已满（最多 {settings.max_active_jobs} 个），请稍后再试。",
        )
    active_for_ip = active_job_count_for_ip(client_ip)
    if active_for_ip >= settings.max_active_jobs_per_ip:
        raise HTTPException(status_code=429, detail="同一 IP 同时只能有 1 个排队或运行中的任务。")
    hourly_for_ip = hourly_job_count_for_ip(client_ip)
    if hourly_for_ip >= settings.max_jobs_per_ip_per_hour:
        raise HTTPException(status_code=429, detail="同一 IP 每小时最多提交 5 个任务，请稍后再试。")


@app.post("/api/jobs")
async def create_job_endpoint(
    request: Request,
    file: UploadFile = File(...),
    subject: str = Form("auto"),
    diagram_strategy: str = Form("source_crop_first"),
    provider: str = Form("openai"),
    base_url: str = Form("https://api.openai.com/v1"),
    model: str = Form("gpt-5.5"),
    api_key: str = Form(""),
):
    if not api_key.strip():
        raise HTTPException(status_code=422, detail="请填写 API Key。")

    client_ip = client_ip_from_request(request)
    enforce_job_limits(client_ip)

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb}MB 限制。")

    job_id = uuid.uuid4().hex
    work_dir = settings.jobs_dir / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    upload_name = safe_filename(file.filename or "upload.bin")
    upload_path = settings.uploads_dir / f"{job_id}-{upload_name}"
    upload_path.write_bytes(content)

    secrets_path = work_dir / "secrets.json"
    secrets_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    create_job(
        job_id=job_id,
        subject=subject,
        diagram_strategy=diagram_strategy,
        original_filename=file.filename or upload_name,
        client_ip=client_ip,
        upload_path=upload_path,
        work_dir=work_dir,
    )
    return {"job_id": job_id, "queue_position": queue_position(job_id), "active_jobs": active_job_count()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    artifacts = artifacts_for(job)
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "subject": job["subject"],
        "diagram_strategy": job["diagram_strategy"],
        "error": job["error"],
        "queue_position": queue_position(job_id),
        "active_jobs": active_job_count(),
        "artifacts": {key: f"/api/jobs/{job_id}/artifacts/{key}" for key in artifacts},
        "token_usage": load_token_usage(artifacts),
        "events": list_events(job_id),
    }


def load_token_usage(artifacts: dict[str, str]) -> dict | None:
    path = artifacts.get("token_usage")
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str):
    if not get_job(job_id):
        raise HTTPException(status_code=404, detail="任务不存在。")
    return {"events": list_events(job_id)}


@app.get("/api/jobs/{job_id}/artifacts/{kind}")
def download_artifact(job_id: str, kind: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    artifacts = artifacts_for(job)
    if kind not in artifacts:
        raise HTTPException(status_code=404, detail="文件不存在。")
    path = Path(artifacts[kind])
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已被清理。")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)
