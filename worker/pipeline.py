from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageEnhance, ImageOps

from app.config import settings
from app.db import update_job
from app.trial_tokens import finalize_trial_reservation, release_trial_reservation
from worker.ai import transcribe_worksheet
from worker.render import generate_outputs


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def render_pdf_pages(path: Path, out_dir: Path) -> list[Path]:
    doc = fitz.open(str(path))
    pages: list[Path] = []
    for index, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        out = out_dir / f"page-{index:03d}.png"
        pix.save(str(out))
        pages.append(out)
    return pages


def pillow_enhance(path: Path, out_dir: Path) -> Path:
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(1.12)
    image = ImageEnhance.Sharpness(image).enhance(1.15)
    out = out_dir / f"{path.stem}-enhanced.png"
    image.save(out)
    return out


def preprocess_image(path: Path, out_dir: Path) -> Path:
    script = Path(__file__).resolve().parents[1] / "scripts" / "preprocess_worksheet_image.py"
    if script.exists():
        result = subprocess.run(
            ["python3", str(script), str(path), "--out", str(out_dir), "--backend", "auto"],
            text=True,
            capture_output=True,
            timeout=90,
        )
        corrected = out_dir / f"{path.stem}-corrected.png"
        if result.returncode == 0 and corrected.exists():
            return corrected
    return pillow_enhance(path, out_dir)


def prepare_pages(upload_path: Path, work_dir: Path) -> list[Path]:
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    suffix = upload_path.suffix.lower()
    if suffix == ".pdf":
        rendered = render_pdf_pages(upload_path, pages_dir)
        return [preprocess_image(page, pages_dir) for page in rendered]
    if suffix in IMAGE_SUFFIXES:
        return [preprocess_image(upload_path, pages_dir)]
    raise ValueError(f"不支持的文件类型：{suffix}")


def load_and_delete_secrets(work_dir: Path) -> dict[str, str]:
    secrets_path = work_dir / "secrets.json"
    if not secrets_path.exists():
        raise ValueError("任务缺少临时 API 配置。")
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    secrets_path.unlink(missing_ok=True)
    if secrets.get("shared_access") is True:
        api_key = settings.shared_ai_api_key.strip()
        base_url = settings.shared_ai_base_url.strip()
        if not api_key or not base_url:
            raise ValueError("共享 AI 配置尚未启用。")
        return {
            "provider": settings.shared_ai_provider.strip() or "openai",
            "base_url": base_url,
            "model": settings.shared_ai_model.strip() or "gpt-5.5",
            "api_key": api_key,
        }
    api_key = str(secrets.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("API Key 为空。")
    return {
        "provider": str(secrets.get("provider") or "openai"),
        "base_url": str(secrets.get("base_url") or "https://api.openai.com/v1"),
        "model": str(secrets.get("model") or "gpt-5.5"),
        "api_key": api_key,
    }


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def crop_figure(page_path: Path, crop: dict[str, Any], out_dir: Path, question_no: Any, index: int) -> str | None:
    bbox = crop.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    image = ImageOps.exif_transpose(Image.open(page_path)).convert("RGB")
    width, height = image.size
    x1, y1, x2, y2 = [float(v) for v in bbox]
    x1, x2 = sorted((clamp(x1, 0, 1), clamp(x2, 0, 1)))
    y1, y2 = sorted((clamp(y1, 0, 1), clamp(y2, 0, 1)))
    pad_x = max(12, int(width * 0.025))
    pad_y = max(12, int(height * 0.025))
    left = max(0, int(x1 * width) - pad_x)
    top = max(0, int(y1 * height) - pad_y)
    right = min(width, int(x2 * width) + pad_x)
    bottom = min(height, int(y2 * height) + pad_y)
    if right - left < 24 or bottom - top < 24:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"q{question_no or 'x'}-figure-{index}.png"
    image.crop((left, top, right, bottom)).save(out)
    return str(out)


def apply_figure_crops(worksheet: dict[str, Any], pages: list[Path], artifact_dir: Path) -> int:
    count = 0
    crop_dir = artifact_dir / "figure-crops"
    for section in worksheet.get("sections", []):
        for question in section.get("questions", []):
            crops: list[dict[str, Any]] = []
            if isinstance(question.get("figure_crop"), dict):
                crops.append(question["figure_crop"])
            if isinstance(question.get("figure_crops"), list):
                crops.extend(item for item in question["figure_crops"] if isinstance(item, dict))
            if not crops:
                continue
            images = []
            for index, crop in enumerate(crops, start=1):
                page_no = int(crop.get("page") or 1)
                if page_no < 1 or page_no > len(pages):
                    continue
                path = crop_figure(pages[page_no - 1], crop, crop_dir, question.get("no"), index)
                if path:
                    images.append({"path": path, "width": "0.72\\linewidth"})
                    count += 1
            if images:
                existing = question.get("figure_images") if isinstance(question.get("figure_images"), list) else []
                question["figure_images"] = existing + images
                question.pop("figure_crop", None)
                question.pop("figure_crops", None)
    return count


def process_job(job: dict[str, Any]) -> None:
    job_id = job["id"]
    work_dir = Path(job["work_dir"])
    upload_path = Path(job["upload_path"])
    artifact_dir = settings.artifacts_dir / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    try:
        update_job(job_id, progress=10, event="正在拆页和预处理图片。")
        pages = prepare_pages(upload_path, work_dir)
        update_job(job_id, progress=30, event=f"已生成 {len(pages)} 张页面图片。")

        secrets = load_and_delete_secrets(work_dir)
        update_job(job_id, progress=35, event="正在调用视觉模型转录题目并生成答案。")
        worksheet, token_usage = transcribe_worksheet(
            image_paths=pages,
            subject=job["subject"],
            diagram_strategy=job["diagram_strategy"],
            secrets=secrets,
            source_name=job["original_filename"],
        )
        token_usage_path = artifact_dir / "token_usage.json"
        token_usage_path.write_text(json.dumps(token_usage, ensure_ascii=False, indent=2), encoding="utf-8")
        update_job(
            job_id,
            progress=68,
            event=f"AI 转录完成。Token：输入 {token_usage['input_tokens']}，输出 {token_usage['output_tokens']}，总计 {token_usage['total_tokens']}。",
        )

        crop_count = apply_figure_crops(worksheet, pages, artifact_dir)
        if crop_count:
            update_job(job_id, progress=72, event=f"已生成 {crop_count} 张原图带留白裁切图。")
        else:
            update_job(job_id, progress=72, event="未收到可执行的图形裁切坐标；如题目含图，请选择原图裁切策略并重新提交。", level="warn")

        update_job(job_id, progress=76, event="正在生成 PDF。")

        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        artifacts, warnings = generate_outputs(worksheet, artifact_dir, scripts_dir)
        artifacts["token_usage"] = str(token_usage_path)
        for warning in warnings:
            update_job(job_id, event=warning, level="warn")

        if job.get("access_mode") == "trial":
            if not finalize_trial_reservation(job_id):
                raise RuntimeError("试用额度结算失败。")
        update_job(
            job_id,
            status="completed",
            progress=100,
            artifacts=artifacts,
            event="任务完成，可以下载结果。",
        )
    except Exception as exc:
        (work_dir / "secrets.json").unlink(missing_ok=True)
        if job.get("access_mode") == "trial":
            release_trial_reservation(job_id)
        update_job(job_id, status="failed", progress=100, error=str(exc), event=f"任务失败：{exc}", level="error")
