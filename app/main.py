from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
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
    recent_visitor_events,
    record_visitor_event,
    visitor_daily_counts,
    visitor_ip_rank,
    visitor_summary,
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
        "default-src 'self'; "
        "script-src 'self' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' https://cloudflareinsights.com"
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/status")
def system_status(request: Request) -> dict[str, int | str]:
    client_ip = client_ip_from_request(request)
    return {
        "status": "ok",
        "active_jobs": active_job_count(),
        "max_active_jobs": settings.max_active_jobs,
        "hourly_jobs_for_ip": hourly_job_count_for_ip(client_ip),
        "max_jobs_per_ip_per_hour": settings.max_jobs_per_ip_per_hour,
        "max_upload_mb": settings.max_upload_mb,
        "max_active_jobs_per_ip": settings.max_active_jobs_per_ip,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    record_request_event(request, event_type="page_view")
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


def client_id_from_request(request: Request, fallback: str = "") -> str:
    return (request.headers.get("X-Client-Id") or fallback or "").strip()


def record_request_event(request: Request, *, event_type: str, job_id: str = "", client_id: str = "") -> None:
    record_visitor_event(
        event_type=event_type,
        client_ip=client_ip_from_request(request),
        client_id=client_id_from_request(request, client_id),
        method=request.method,
        path=request.url.path,
        user_agent=request.headers.get("user-agent", ""),
        referer=request.headers.get("referer", ""),
        job_id=job_id,
    )


def require_client_id(client_id: str) -> str:
    if not client_id:
        raise HTTPException(status_code=422, detail="缺少浏览器任务身份，请刷新页面后重试。")
    return client_id


def require_job_for_client(job_id: str, client_id: str) -> dict:
    job = get_job(job_id)
    if not job or not client_id or job.get("client_id") != client_id:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return job


def expires_at_from_created_at(created_at: str) -> str | None:
    try:
        created = datetime.fromisoformat(created_at)
        return (created + timedelta(hours=settings.job_retention_hours)).isoformat()
    except ValueError:
        return None


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
    client_id: str = Form(""),
):
    if not api_key.strip():
        raise HTTPException(status_code=422, detail="请填写 API Key。")

    client_id = require_client_id(client_id)
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
        client_id=client_id,
        client_ip=client_ip,
        upload_path=upload_path,
        work_dir=work_dir,
    )
    record_request_event(request, event_type="job_created", job_id=job_id, client_id=client_id)
    return {"job_id": job_id, "queue_position": queue_position(job_id), "active_jobs": active_job_count()}


@app.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str, client_id: str = Query(default="")):
    client_id = client_id_from_request(request, client_id)
    job = require_job_for_client(job_id, client_id)
    artifacts = artifacts_for(job)
    encoded_client_id = quote(client_id)
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "subject": job["subject"],
        "diagram_strategy": job["diagram_strategy"],
        "original_filename": job["original_filename"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "expires_at": expires_at_from_created_at(job["created_at"]),
        "error": job["error"],
        "queue_position": queue_position(job_id),
        "active_jobs": active_job_count(),
        "artifacts": {key: f"/api/jobs/{job_id}/artifacts/{key}?client_id={encoded_client_id}" for key in artifacts},
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
def job_events(request: Request, job_id: str, client_id: str = Query(default="")):
    client_id = client_id_from_request(request, client_id)
    require_job_for_client(job_id, client_id)
    return {"events": list_events(job_id)}


@app.get("/api/jobs/{job_id}/artifacts/{kind}")
def download_artifact(request: Request, job_id: str, kind: str, client_id: str = Query(default="")):
    client_id = client_id_from_request(request, client_id)
    job = require_job_for_client(job_id, client_id)
    artifacts = artifacts_for(job)
    if kind not in artifacts:
        raise HTTPException(status_code=404, detail="文件不存在。")
    path = Path(artifacts[kind])
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已被清理。")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
    record_request_event(request, event_type="artifact_download", job_id=job_id, client_id=client_id)
    return FileResponse(path, media_type=media_type, filename=path.name)


def require_stats_token(token: str) -> None:
    expected = settings.visitor_stats_token.strip()
    if not expected or token != expected:
        raise HTTPException(status_code=404, detail="Not found")


def utc_cutoff(days: int = 0, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat()


def short(value: str, length: int = 8) -> str:
    return value[:length] if value else ""


def compact_user_agent(value: str) -> str:
    if not value:
        return ""
    return value[:90] + ("..." if len(value) > 90 else "")


@app.get("/internal/visitors", response_class=HTMLResponse, include_in_schema=False)
def visitor_stats_page(request: Request, token: str = Query(default="")):
    require_stats_token(token)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    context = {
        "request": request,
        "today": visitor_summary(since=today),
        "last_24h": visitor_summary(since=utc_cutoff(hours=24)),
        "last_7d": visitor_summary(since=utc_cutoff(days=7)),
        "daily": visitor_daily_counts(since=utc_cutoff(days=7)),
        "recent_events": recent_visitor_events(limit=100),
        "ip_rank_24h": visitor_ip_rank(since=utc_cutoff(hours=24), limit=20),
        "ip_rank_7d": visitor_ip_rank(since=utc_cutoff(days=7), limit=20),
        "short": short,
        "compact_user_agent": compact_user_agent,
    }
    return templates.TemplateResponse("visitor_stats.html", context)
