"""Preprocess worksheet photos before visual transcription.

The script prefers OpenCV for document contour detection and perspective
correction. If OpenCV is unavailable or cannot find a page contour, it falls
back to a Pillow-only path for small-angle deskew, conservative crop, and
readability enhancement.

Usage:
    python preprocess_worksheet_image.py input.jpg
    python preprocess_worksheet_image.py input.jpg --backend auto
    python preprocess_worksheet_image.py input.jpg --backend opencv
    python preprocess_worksheet_image.py input.jpg --backend pillow
    python preprocess_worksheet_image.py input.jpg --install-opencv
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageOps


def load_image(path: Path) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def ensure_out_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def enhance_readability(image: Image.Image) -> Image.Image:
    result = ImageOps.autocontrast(image, cutoff=1)
    result = ImageEnhance.Contrast(result).enhance(1.12)
    result = ImageEnhance.Sharpness(result).enhance(1.18)
    return result


def conservative_crop(image: Image.Image, padding: int = 28) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    gray = ImageOps.grayscale(image)
    autocontrast = ImageOps.autocontrast(gray)
    mask = autocontrast.point(lambda p: 255 if p < 245 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image, None

    width, height = image.size
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(width, bbox[2] + padding)
    bottom = min(height, bbox[3] + padding)

    if right - left < width * 0.35 or bottom - top < height * 0.35:
        return image, None
    return image.crop((left, top, right, bottom)), (left, top, right, bottom)


def projection_score(image: Image.Image) -> float:
    gray = ImageOps.grayscale(image)
    small = gray.resize((max(1, gray.width // 3), max(1, gray.height // 3)))
    bw = small.point(lambda p: 0 if p < 190 else 255)
    rows: list[int] = []
    pixels = bw.load()
    for y in range(bw.height):
        ink = 0
        for x in range(bw.width):
            if pixels[x, y] == 0:
                ink += 1
        rows.append(ink)
    if not rows:
        return 0.0
    mean = sum(rows) / len(rows)
    return sum((row - mean) ** 2 for row in rows) / len(rows)


def estimate_pillow_angle(image: Image.Image, limit: float = 8.0, step: float = 0.5) -> float:
    gray = ImageOps.grayscale(image)
    max_side = max(gray.size)
    if max_side > 1000:
        scale = 1000 / max_side
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)))

    best_angle = 0.0
    best_score = -1.0
    count = int((limit * 2) / step) + 1
    for index in range(count):
        angle = -limit + index * step
        rotated = gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
        score = projection_score(rotated)
        if score > best_score:
            best_score = score
            best_angle = angle
    return best_angle


def pillow_pipeline(image: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    angle = estimate_pillow_angle(image)
    rotated = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    cropped, crop_box = conservative_crop(rotated)
    enhanced = enhance_readability(cropped)
    report = {
        "backend": "pillow_fallback",
        "deskew_angle_degrees": round(angle, 3),
        "crop_box": crop_box,
        "perspective_corrected": False,
        "notes": [
            "Pillow fallback handles small in-plane rotation and conservative cropping only.",
            "Strong perspective distortion may still need OpenCV or a better source photo.",
        ],
    }
    return enhanced, report


def pil_to_cv2(image: Image.Image) -> Any:
    import numpy as np

    arr = np.array(image)
    return arr[:, :, ::-1].copy()


def cv2_to_pil(image: Any) -> Image.Image:
    import numpy as np

    rgb = image[:, :, ::-1]
    return Image.fromarray(np.asarray(rgb).astype("uint8"), "RGB")


def order_points(points: Any) -> Any:
    import numpy as np

    pts = np.array(points, dtype="float32").reshape(4, 2)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).reshape(4)
    ordered = np.zeros((4, 2), dtype="float32")
    ordered[0] = pts[np.argmin(sums)]
    ordered[2] = pts[np.argmax(sums)]
    ordered[1] = pts[np.argmin(diffs)]
    ordered[3] = pts[np.argmax(diffs)]
    return ordered


def warp_document(cv2: Any, image: Any, points: Any) -> Any:
    import numpy as np

    rect = order_points(points)
    tl, tr, br, bl = rect
    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_width = max(1, int(max(width_a, width_b)))
    max_height = max(1, int(max(height_a, height_b)))
    destination = np.array(
        [[0, 0], [max_width - 1, 0], [max_width - 1, max_height - 1], [0, max_height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(rect, destination)
    return cv2.warpPerspective(image, matrix, (max_width, max_height), borderValue=(255, 255, 255))


def find_document_contour(cv2: Any, image: Any) -> tuple[Any | None, float]:
    import numpy as np

    original_height, original_width = image.shape[:2]
    max_side = max(original_height, original_width)
    scale = 1.0
    resized = image
    if max_side > 1300:
        scale = 1300 / max_side
        resized = cv2.resize(image, (int(original_width * scale), int(original_height * scale)))

    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(gray, 45, 150)
    kernel = np.ones((5, 5), np.uint8)
    edged = cv2.dilate(edged, kernel, iterations=1)
    contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:8]
    image_area = resized.shape[0] * resized.shape[1]

    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        area = cv2.contourArea(approx)
        if len(approx) == 4 and area > image_area * 0.18:
            points = approx.reshape(4, 2).astype("float32") / scale
            return points, float(area / image_area)
    return None, 0.0


def cv2_deskew(cv2: Any, image: Any) -> tuple[Any, float]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.bitwise_not(gray)
    threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(threshold)
    if coords is None:
        return image, 0.0
    rect = cv2.minAreaRect(coords)
    angle = rect[-1]
    if angle < -45:
        angle = 90 + angle
    if abs(angle) > 12:
        return image, 0.0
    height, width = image.shape[:2]
    center = (width // 2, height // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated, float(angle)


def opencv_pipeline(image: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is not installed. Install it with: pip install opencv-python-headless pillow"
        ) from exc

    cv_image = pil_to_cv2(image)
    contour, area_ratio = find_document_contour(cv2, cv_image)
    if contour is not None:
        corrected_cv = warp_document(cv2, cv_image, contour)
        corrected = enhance_readability(cv2_to_pil(corrected_cv))
        report = {
            "backend": "opencv",
            "method": "perspective_contour",
            "perspective_corrected": True,
            "contour_area_ratio": round(area_ratio, 4),
            "document_points": [[round(float(x), 2), round(float(y), 2)] for x, y in order_points(contour)],
            "notes": ["Detected a four-corner page contour and applied perspective correction."],
        }
        return corrected, report

    rotated_cv, angle = cv2_deskew(cv2, cv_image)
    corrected = cv2_to_pil(rotated_cv)
    cropped, crop_box = conservative_crop(corrected)
    enhanced = enhance_readability(cropped)
    report = {
        "backend": "opencv",
        "method": "deskew_fallback",
        "perspective_corrected": False,
        "deskew_angle_degrees": round(angle, 3),
        "crop_box": crop_box,
        "notes": [
            "OpenCV was available, but no reliable four-corner page contour was found.",
            "Used OpenCV deskew plus conservative crop instead.",
        ],
    }
    return enhanced, report


def make_preview(original: Image.Image, corrected: Image.Image, report: dict[str, Any]) -> Image.Image:
    thumb_width = 520
    margin = 28
    label_height = 92

    original_thumb = original.copy()
    original_thumb.thumbnail((thumb_width, 720))
    corrected_thumb = corrected.copy()
    corrected_thumb.thumbnail((thumb_width, 720))

    width = thumb_width * 2 + margin * 3
    height = max(original_thumb.height, corrected_thumb.height) + label_height + margin * 2
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)

    draw.text((margin, margin), "Original", fill=(20, 20, 20))
    method = report.get("method")
    corrected_label = f"Corrected: {report.get('backend')}"
    if method:
        corrected_label += f" / {method}"
    draw.text((thumb_width + margin * 2, margin), corrected_label, fill=(20, 20, 20))
    if report.get("preprocess_status"):
        draw.text((thumb_width + margin * 2, margin + 24), str(report["preprocess_status"]), fill=(130, 50, 20))
    canvas.paste(original_thumb, (margin, margin + label_height))
    canvas.paste(corrected_thumb, (thumb_width + margin * 2, margin + label_height))
    return canvas


def install_opencv_headless() -> None:
    command = [sys.executable, "-m", "pip", "install", "opencv-python-headless", "pillow"]
    subprocess.run(command, check=True)


def geometry_changed(
    original_size: tuple[int, int],
    corrected_size: tuple[int, int],
    report: dict[str, Any],
) -> bool:
    if report.get("perspective_corrected"):
        return True
    angle = abs(float(report.get("deskew_angle_degrees") or 0.0))
    if angle >= 0.25:
        return True
    crop_box = report.get("crop_box")
    if crop_box and len(crop_box) == 4:
        left, top, right, bottom = [int(value) for value in crop_box]
        if left > 2 or top > 2 or abs(right - original_size[0]) > 2 or abs(bottom - original_size[1]) > 2:
            return True
    width_delta = abs(corrected_size[0] - original_size[0]) / max(1, original_size[0])
    height_delta = abs(corrected_size[1] - original_size[1]) / max(1, original_size[1])
    return width_delta > 0.03 or height_delta > 0.03


def process_image(input_path: Path, out_dir: Path, backend: str, install_opencv: bool = False) -> dict[str, Any]:
    source = load_image(input_path)
    install_hint = "pip install opencv-python-headless pillow"
    warnings: list[str] = []

    if backend in {"auto", "opencv"}:
        try:
            corrected, report = opencv_pipeline(source)
        except RuntimeError as exc:
            if install_opencv:
                try:
                    install_opencv_headless()
                    corrected, report = opencv_pipeline(source)
                except Exception as install_exc:  # pragma: no cover - environment dependent
                    warnings.append(str(exc))
                    warnings.append(f"OpenCV headless installation failed: {install_exc}")
                    warnings.append("Falling back to Pillow.")
                    corrected, report = pillow_pipeline(source)
                    report["opencv_install_hint"] = install_hint
            else:
                warnings.append(str(exc))
                warnings.append(
                    "Falling back to Pillow. In Codex or other agent environments, ask the user before installing OpenCV headless."
                )
                corrected, report = pillow_pipeline(source)
                report["opencv_install_hint"] = install_hint
    else:
        corrected, report = pillow_pipeline(source)

    if (
        backend in {"auto", "opencv"}
        and report.get("backend") == "pillow_fallback"
        and not report.get("opencv_install_hint")
    ):
        report["opencv_install_hint"] = install_hint

    if not report.get("perspective_corrected") and not geometry_changed(source.size, corrected.size, report):
        message = "No reliable page contour found; image was enhanced but not perspective-corrected."
        report["preprocess_status"] = message
        report["geometry_changed"] = False
        report.setdefault("notes", []).append(message)
    else:
        report["geometry_changed"] = True

    stem = input_path.stem
    corrected_path = out_dir / f"{stem}-corrected.png"
    preview_path = out_dir / f"{stem}-preview.png"
    report_path = out_dir / f"{stem}-report.json"

    corrected.save(corrected_path)
    preview = make_preview(source, corrected, report)
    preview.save(preview_path)

    full_report: dict[str, Any] = {
        "source": str(input_path),
        "backend_requested": backend,
        "install_opencv_requested": install_opencv,
        "backend_used": report.get("backend"),
        "corrected_image": str(corrected_path),
        "preview_image": str(preview_path),
        "original_size": list(source.size),
        "corrected_size": list(corrected.size),
        "warnings": warnings,
        **report,
    }
    report_path.write_text(json.dumps(full_report, ensure_ascii=False, indent=2), encoding="utf-8")
    full_report["report"] = str(report_path)
    return full_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preprocess worksheet photos with OpenCV-first correction.")
    parser.add_argument("image", type=Path, help="Input JPG/PNG worksheet photo.")
    parser.add_argument("--backend", choices=["auto", "opencv", "pillow"], default="auto")
    parser.add_argument("--out", type=Path, default=Path("output") / "preprocess")
    parser.add_argument(
        "--install-opencv",
        action="store_true",
        help="If cv2 is missing, install opencv-python-headless and pillow before falling back. In Codex/agent environments, ask the user before using this flag.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.image.exists():
        raise SystemExit(f"Input image not found: {args.image}")
    out_dir = ensure_out_dir(args.out)
    report = process_image(args.image, out_dir, args.backend, install_opencv=args.install_opencv)
    print(f"Generated {report['corrected_image']}")
    print(f"Generated {report['preview_image']}")
    print(f"Generated {report['report']}")
    if report.get("warnings"):
        print("\nWarnings:")
        for warning in report["warnings"]:
            print(f"- {warning}")


if __name__ == "__main__":
    main()
