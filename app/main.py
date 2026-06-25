from __future__ import annotations

import hmac
import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

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
from app.ipinfo import lookup_ip_geo, lookup_ip_geos
from app.trial_tokens import (
    TrialTokenError,
    create_trial_token,
    inspect_trial_token,
    list_trial_tokens,
    recent_trial_ips,
    release_trial_reservation,
    reserve_trial_token,
    revoke_trial_token,
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
    if request.url.path.startswith("/api/internal/") or request.url.path.startswith("/internal/trial-tokens"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/status")
def system_status(request: Request) -> dict[str, object]:
    client_ip = client_ip_from_request(request)
    geo = lookup_ip_geo(client_ip)
    access = shared_access_status(request, client_ip)
    return {
        "status": "ok",
        "active_jobs": active_job_count(),
        "max_active_jobs": settings.max_active_jobs,
        "hourly_jobs_for_ip": hourly_job_count_for_ip(client_ip),
        "max_jobs_per_ip_per_hour": settings.max_jobs_per_ip_per_hour,
        "max_upload_mb": settings.max_upload_mb,
        "max_active_jobs_per_ip": settings.max_active_jobs_per_ip,
        "visitor_country": geo.get("country") or "",
        "visitor_country_code": geo.get("country_code") or "",
        "visitor_as_name": geo.get("as_name") or "",
        "shared_access_authorized": access["authorized"],
        "hourly_limit_exempt": access["authorized"],
        "access_mode": access["mode"],
        "trial_remaining": access.get("remaining"),
        "trial_max_uses": access.get("max_uses"),
        "trial_expires_at": access.get("expires_at"),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    record_request_event(request, event_type="page_view")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "feishu_qr_available": (Path("static") / "feishu-qr.png").exists(),
        },
    )


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


def legacy_shared_access_authorized(request: Request) -> bool:
    expected = settings.shared_access_token.strip()
    supplied = request.headers.get("X-Service-Token", "").strip()
    shared_config_ready = bool(settings.shared_ai_api_key.strip() and settings.shared_ai_base_url.strip())
    return bool(expected and supplied and shared_config_ready and hmac.compare_digest(supplied, expected))


def shared_access_status(request: Request, client_ip: str) -> dict[str, object]:
    supplied = request.headers.get("X-Service-Token", "").strip()
    if legacy_shared_access_authorized(request):
        return {"authorized": True, "mode": "legacy_shared", "remaining": None, "max_uses": None}
    if supplied:
        trial = inspect_trial_token(supplied, client_ip)
        if trial and settings.shared_ai_api_key.strip() and settings.shared_ai_base_url.strip():
            return {"authorized": True, "mode": "trial", **trial}
        return {"authorized": False, "mode": "invalid"}
    return {"authorized": False, "mode": "user_key"}


def expires_at_from_created_at(created_at: str) -> str | None:
    try:
        created = datetime.fromisoformat(created_at)
        return (created + timedelta(hours=settings.job_retention_hours)).isoformat()
    except ValueError:
        return None


def enforce_job_limits(client_ip: str, *, hourly_limit_exempt: bool = False) -> None:
    active_total = active_job_count()
    if active_total >= settings.max_active_jobs:
        raise HTTPException(
            status_code=429,
            detail=f"当前排队任务已满（最多 {settings.max_active_jobs} 个），请稍后再试。",
        )
    active_for_ip = active_job_count_for_ip(client_ip)
    if active_for_ip >= settings.max_active_jobs_per_ip:
        raise HTTPException(status_code=429, detail="同一 IP 同时只能有 1 个排队或运行中的任务。")
    if not hourly_limit_exempt:
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
    client_id = require_client_id(client_id)
    client_ip = client_ip_from_request(request)
    access = shared_access_status(request, client_ip)
    supplied_service_token = request.headers.get("X-Service-Token", "").strip()
    if access["mode"] == "invalid":
        raise HTTPException(status_code=422, detail="试用授权无效，请检查链接或联系管理员。")
    use_shared_access = bool(access["authorized"])
    if not use_shared_access and not api_key.strip():
        raise HTTPException(status_code=422, detail="请填写 API Key。")

    enforce_job_limits(client_ip, hourly_limit_exempt=use_shared_access)

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb}MB 限制。")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=422, detail="仅支持 PDF、PNG、JPG、JPEG 或 WEBP 文件。")

    job_id = uuid.uuid4().hex
    trial_token_id = ""
    if access["mode"] == "trial":
        try:
            reservation = reserve_trial_token(supplied_service_token, client_ip, job_id)
            trial_token_id = str(reservation["token_id"])
        except (TrialTokenError, ValueError):
            raise HTTPException(status_code=422, detail="试用授权无效，请检查链接或联系管理员。")

    work_dir = settings.jobs_dir / job_id
    upload_name = safe_filename(file.filename or "upload.bin")
    upload_path = settings.uploads_dir / f"{job_id}-{upload_name}"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        upload_path.write_bytes(content)

        secrets_path = work_dir / "secrets.json"
        secrets = (
            {"shared_access": True}
            if use_shared_access
            else {
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
            }
        )
        secrets_path.write_text(json.dumps(secrets, ensure_ascii=False), encoding="utf-8")

        create_job(
            job_id=job_id,
            subject=subject,
            diagram_strategy=diagram_strategy,
            original_filename=file.filename or upload_name,
            client_id=client_id,
            client_ip=client_ip,
            access_mode=str(access["mode"]),
            trial_token_id=trial_token_id,
            upload_path=upload_path,
            work_dir=work_dir,
        )
    except Exception:
        upload_path.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        if trial_token_id:
            release_trial_reservation(job_id)
        raise
    record_request_event(request, event_type="job_created", job_id=job_id, client_id=client_id)
    return {
        "job_id": job_id,
        "queue_position": queue_position(job_id),
        "active_jobs": active_job_count(),
        "shared_access": use_shared_access,
        "access_mode": access["mode"],
    }


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


def require_token_admin(request: Request) -> None:
    expected = settings.token_admin_token.strip()
    supplied = request.headers.get("X-Token-Admin", "").strip()
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Not found")


class TrialTokenCreateRequest(BaseModel):
    bound_ip: str
    max_uses: int | None = Field(default=None, ge=1)
    expires_at: str | None = None
    note: str = Field(default="", max_length=300)


def utc_cutoff(days: int = 0, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat()


def valid_timezone_name(value: str) -> str:
    candidate = value.strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return "UTC"
    return candidate


def local_day_start_utc(timezone_name: str) -> str:
    local_timezone = ZoneInfo(timezone_name)
    local_now = datetime.now(timezone.utc).astimezone(local_timezone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat()


def short(value: str, length: int = 8) -> str:
    return value[:length] if value else ""


def compact_user_agent(value: str) -> str:
    if not value:
        return ""
    return value[:90] + ("..." if len(value) > 90 else "")


@app.get("/internal/visitors", response_class=HTMLResponse, include_in_schema=False)
def visitor_stats_page(
    request: Request,
    token: str = Query(default=""),
    timezone_name: str = Query(default="UTC", alias="timezone"),
):
    require_stats_token(token)
    timezone_name = valid_timezone_name(timezone_name)
    today = local_day_start_utc(timezone_name)
    seven_days_ago = utc_cutoff(days=7)
    recent_events = recent_visitor_events(limit=100)
    ip_rank_24h = visitor_ip_rank(since=utc_cutoff(hours=24), limit=20)
    ip_rank_7d = visitor_ip_rank(since=seven_days_ago, limit=20)
    lookup_ip_geos(
        [row.get("client_ip", "") for row in recent_events + ip_rank_24h + ip_rank_7d if row.get("client_ip")]
    )
    context = {
        "request": request,
        "today": visitor_summary(since=today),
        "last_24h": visitor_summary(since=utc_cutoff(hours=24)),
        "last_7d": visitor_summary(since=seven_days_ago),
        "daily": visitor_daily_counts(since=seven_days_ago, timezone_name=timezone_name),
        "recent_events": recent_visitor_events(limit=100),
        "ip_rank_24h": visitor_ip_rank(since=utc_cutoff(hours=24), limit=20),
        "ip_rank_7d": visitor_ip_rank(since=utc_cutoff(days=7), limit=20),
        "short": short,
        "compact_user_agent": compact_user_agent,
        "timezone_name": timezone_name,
    }
    return templates.TemplateResponse("visitor_stats.html", context)


@app.get("/internal/trial-tokens", response_class=HTMLResponse, include_in_schema=False)
def trial_token_admin_page(request: Request):
    return templates.TemplateResponse(
        "trial_tokens.html",
        {"request": request, "default_days": settings.trial_token_default_days},
    )


@app.get("/api/internal/trial-tokens", include_in_schema=False)
def trial_token_admin_data(request: Request):
    require_token_admin(request)
    return {
        "tokens": list_trial_tokens(),
        "recent_ips": recent_trial_ips(),
        "default_days": settings.trial_token_default_days,
    }


@app.post("/api/internal/trial-tokens", include_in_schema=False)
def trial_token_admin_create(request: Request, payload: TrialTokenCreateRequest):
    require_token_admin(request)
    try:
        created = create_trial_token(
            bound_ip=payload.bound_ip,
            max_uses=payload.max_uses,
            expires_at=payload.expires_at,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    created["url"] = f"{settings.public_base_url.rstrip('/')}/#token={created['token']}"
    return created


@app.post("/api/internal/trial-tokens/{token_id}/revoke", include_in_schema=False)
def trial_token_admin_revoke(request: Request, token_id: str):
    require_token_admin(request)
    if not revoke_trial_token(token_id):
        raise HTTPException(status_code=404, detail="Token 不存在或已撤销。")
    return {"status": "revoked"}
