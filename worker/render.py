from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Image as FlowableImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def ensure_cjk_font() -> str:
    name = "STSong-Light"
    try:
        pdfmetrics.getFont(name)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(name))
    return name


def html_escape(text: Any) -> str:
    value = "" if text is None else str(text)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def write_markdown(data: dict[str, Any], path: Path) -> None:
    lines = [f"# {data.get('title', '试卷练习题')}-卷面转录", ""]
    if data.get("source"):
        lines.append(f"来源：`{data['source']}`")
        lines.append("")
    if data.get("notes"):
        lines.append(f"> {data['notes']}")
        lines.append("")
    for section in data.get("sections", []):
        lines.append(f"## {section.get('title', '')}")
        for question in section.get("questions", []):
            no = question.get("no", "")
            prompt = question.get("prompt") or question.get("prompt_tex", "")
            lines.append(f"{no}. {prompt}")
            for option in question.get("options", []):
                lines.append(f"   - {option}")
            if question.get("review_note"):
                lines.append(f"   - 复核：{question['review_note']}")
        lines.append("")
    lines.append("## 答案速查")
    for section in data.get("sections", []):
        for question in section.get("questions", []):
            lines.append(f"- {question.get('no', '')}. {question.get('answer') or question.get('answer_tex', '')}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def reportlab_pdf(data: dict[str, Any], path: Path, include_answers: bool) -> None:
    font = ensure_cjk_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCJK",
        parent=styles["Title"],
        fontName=font,
        fontSize=18,
        leading=24,
        textColor=colors.black,
    )
    section_style = ParagraphStyle(
        "SectionCJK",
        parent=styles["Heading2"],
        fontName=font,
        fontSize=13,
        leading=18,
        spaceBefore=8,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "BodyCJK",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=10.5,
        leading=16,
        spaceAfter=6,
    )
    answer_style = ParagraphStyle(
        "AnswerCJK",
        parent=body_style,
        backColor=colors.HexColor("#F4F7FB"),
        borderColor=colors.HexColor("#D9E2F0"),
        borderWidth=0.5,
        borderPadding=5,
        spaceBefore=2,
        spaceAfter=8,
    )
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=17 * mm,
        rightMargin=17 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    story = [
        Paragraph(html_escape(data.get("title", "试卷练习题")), title_style),
        Paragraph("答案详解版" if include_answers else "无答案习题版", body_style),
        Spacer(1, 5 * mm),
    ]
    if data.get("notes"):
        story.append(Paragraph(f"说明：{html_escape(data['notes'])}", body_style))
    for section in data.get("sections", []):
        story.append(Paragraph(html_escape(section.get("title", "")), section_style))
        for question in section.get("questions", []):
            no = question.get("no", "")
            prompt = question.get("prompt") or question.get("prompt_tex", "")
            story.append(Paragraph(f"<b>{html_escape(no)}.</b> {html_escape(prompt)}", body_style))
            options = question.get("options") or question.get("options_tex") or []
            for option in options:
                story.append(Paragraph(html_escape(option), body_style))
            for image in collect_figure_images(question):
                try:
                    story.append(make_reportlab_image(image))
                    story.append(Spacer(1, 4 * mm))
                except Exception:
                    story.append(Paragraph("【图形裁切图渲染失败，需复核】", body_style))
            if include_answers:
                answer = question.get("answer") or question.get("answer_tex", "")
                explanation = question.get("explanation") or question.get("explanation_tex", "")
                review = question.get("review_note")
                text = f"<b>答案：</b>{html_escape(answer)}<br/><b>讲解：</b>{html_escape(explanation)}"
                if review:
                    text += f"<br/><b>复核：</b>{html_escape(review)}"
                story.append(Paragraph(text, answer_style))
            else:
                story.append(Spacer(1, 6 * mm))
    doc.build(story)


def collect_figure_images(question: dict[str, Any]) -> list[Any]:
    images: list[Any] = []
    if question.get("figure_image"):
        images.append(question["figure_image"])
    if isinstance(question.get("figure_images"), list):
        images.extend(question["figure_images"])
    return images


def make_reportlab_image(image_info: Any) -> FlowableImage:
    raw_path = image_info.get("path") if isinstance(image_info, dict) else image_info
    path = Path(str(raw_path))
    flowable = FlowableImage(str(path))
    max_width = 120 * mm
    max_height = 70 * mm
    ratio = min(max_width / flowable.imageWidth, max_height / flowable.imageHeight, 1)
    flowable.drawWidth = flowable.imageWidth * ratio
    flowable.drawHeight = flowable.imageHeight * ratio
    return flowable


def generate_outputs(data: dict[str, Any], out_dir: Path, scripts_dir: Path) -> tuple[dict[str, str], list[str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data.setdefault("latin_font", "TeX Gyre Termes")
    data.setdefault("cjk_font", "Noto Serif CJK SC")
    worksheet_json = out_dir / "worksheet.json"
    worksheet_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    transcript = out_dir / "transcript.md"
    write_markdown(data, transcript)

    artifacts = {
        "worksheet_json": str(worksheet_json),
        "transcript": str(transcript),
    }
    warnings: list[str] = []

    xelatex = shutil.which("xelatex")
    make_script = scripts_dir / "make_worksheet_pdf.py"
    if xelatex and make_script.exists():
        try:
            result = subprocess.run(
                ["python3", str(make_script), str(worksheet_json), "--out", str(out_dir)],
                cwd=str(out_dir.parent),
                text=True,
                capture_output=True,
                timeout=240,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stdout[-3000:] + "\n" + result.stderr[-1000:])
            title = str(data.get("title", worksheet_json.stem))
            artifacts["student_pdf"] = str(out_dir / f"{title}-无答案习题.pdf")
            artifacts["answer_pdf"] = str(out_dir / f"{title}-答案详解.pdf")
            generated_transcript = out_dir / f"{title}-卷面转录.md"
            if generated_transcript.exists():
                artifacts["transcript"] = str(generated_transcript)
            return artifacts, warnings
        except Exception as exc:
            warnings.append(f"XeLaTeX 生成失败，已降级到 ReportLab：{exc}")
    else:
        warnings.append("未找到 xelatex，已使用 ReportLab 生成 PDF；数学公式排版可能不如 XeLaTeX。")

    student_pdf = out_dir / "student.pdf"
    answer_pdf = out_dir / "answers.pdf"
    reportlab_pdf(data, student_pdf, include_answers=False)
    reportlab_pdf(data, answer_pdf, include_answers=True)
    artifacts["student_pdf"] = str(student_pdf)
    artifacts["answer_pdf"] = str(answer_pdf)
    return artifacts, warnings
