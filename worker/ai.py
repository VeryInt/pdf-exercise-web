from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from openai import OpenAI


def image_to_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "image/png")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        stripped = stripped[start : end + 1]
    loaded = json.loads(stripped)
    if not isinstance(loaded, dict):
        raise ValueError("AI response JSON root must be an object.")
    return loaded


def transcribe_worksheet(
    *,
    image_paths: list[Path],
    subject: str,
    diagram_strategy: str,
    secrets: dict[str, str],
    source_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = OpenAI(api_key=secrets["api_key"], base_url=secrets.get("base_url") or None)
    model = secrets["model"]

    strategy_text = {
        "source_crop_first": "复杂/不规则图形优先使用原图带留白裁切；只有简单规则图形才建议重绘。",
        "tikz_when_safe": "只有能准确复现的简单规则图形才使用 TikZ；不确定图形使用原图裁切。",
        "tikz_first": "尽量给出 TikZ 重绘方案，但不确定时必须加 review_note。",
    }.get(diagram_strategy, diagram_strategy)

    prompt = f"""
你是试卷整理助手。请从上传的试卷图片/PDF 页面中转录题目，移除手写答案、圈画、红笔批改和草稿痕迹。

目标：生成一个 worksheet JSON，用于制作两份 PDF：
1. 无答案习题版：保留题干、横线、选择项、图形说明，不出现答案和讲解。
2. 答案详解版：每题包含题目、答案、详细讲解。

学科：{subject}
图形策略：{strategy_text}
来源文件：{source_name}

要求：
- 必须按原题顺序。
- 选择题选项必须完整保留。
- 数学/物理公式尽量放在 *_tex 字段，用 LaTeX 表达。
- 图形无法可靠重绘时，不要臆造 figure_tex；必须返回 figure_crop 或 figure_crops，让程序从原图裁切。
- figure_crop 使用归一化坐标，格式为 {{"page": 1, "bbox": [x1, y1, x2, y2], "caption": "图甲/图乙/图丙"}}，坐标范围 0 到 1，基于对应页面图片左上角。
- 对复杂装置图、几何图、函数图、实物图、选项图，如果选择原图裁切策略，必须给出覆盖完整图形且四周留白的 figure_crop 坐标。
- 如果题目被遮挡或模糊，保留可读部分并加 review_note。
- 答案必须基于题目推理，不要盲抄手写答案。
- 只返回 JSON，不要 Markdown。

JSON 结构：
{{
  "title": "试卷标题",
  "subtitle": "清晰打印版",
  "source": "{source_name}",
  "notes": "整体复核说明",
  "sections": [
    {{
      "title": "一、选择题",
      "questions": [
        {{
          "no": 1,
          "prompt": "题干，空格用 ________ 表示",
          "prompt_tex": "含公式时填写 LaTeX 版本，可省略",
          "options": ["A. ...", "B. ..."],
          "options_tex": ["A. ..."],
          "answer": "答案",
          "answer_tex": "LaTeX 答案，可省略",
          "explanation": "详细讲解",
          "explanation_tex": "LaTeX 讲解，可省略",
          "figure_crop": {{"page": 1, "bbox": [0.1, 0.2, 0.8, 0.45], "caption": "题图"}},
          "review_note": "不确定处，可省略"
        }}
      ]
    }}
  ]
}}
""".strip()

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths[:8]:
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(path)}})

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=0.1,
    )
    text = response.choices[0].message.content or ""
    data = extract_json(text)
    usage = response.usage
    token_usage = {
        "input_tokens": getattr(usage, "prompt_tokens", None) or 0,
        "output_tokens": getattr(usage, "completion_tokens", None) or 0,
        "total_tokens": getattr(usage, "total_tokens", None) or 0,
        "model": model,
        "provider": secrets.get("provider", "openai"),
    }
    data.setdefault("title", "试卷练习题")
    data.setdefault("subtitle", "清晰打印版")
    data.setdefault("source", source_name)
    data.setdefault("sections", [])
    return data, token_usage
