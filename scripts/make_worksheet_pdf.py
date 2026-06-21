"""Generate student and answer PDFs from worksheet JSON/YAML data.

Usage:
    python make_worksheet_pdf.py worksheet.json
    python make_worksheet_pdf.py worksheet.yaml --out output/pdf

JSON is supported with the Python standard library. YAML requires PyYAML.
The PDF backend is XeLaTeX so math formulas render as real typeset formulas.
Diagrams can be embedded with TikZ via question-level figure_tex/figures_tex,
or with cropped source images via figure_image/figure_images.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_SUBTITLE = "清晰打印版"


def load_data(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                "YAML input requires PyYAML. Use worksheet.json instead, or install PyYAML."
            ) from exc
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise SystemExit("Worksheet YAML must contain a mapping/object at the top level.")
        return loaded
    raise SystemExit("Unsupported worksheet file. Use .json, .yaml, or .yml.")


def latex_escape(text: Any) -> str:
    value = "" if text is None else str(text)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in value)


def tex_value(item: dict[str, Any], tex_key: str, plain_key: str) -> str:
    if item.get(tex_key):
        return str(item[tex_key])
    return latex_escape(item.get(plain_key, ""))


def inline_math_if_needed(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    if "$" in stripped or r"\(" in stripped or r"\[" in stripped:
        return stripped
    math_commands = (
        r"\mathrm",
        r"\frac",
        r"\dfrac",
        r"\sqrt",
        r"\angle",
        r"\triangle",
        r"\lim",
        r"\sum",
        r"\vec",
    )
    if any(command in stripped for command in math_commands):
        return f"${stripped}$"
    return stripped


def find_xelatex() -> str:
    found = shutil.which("xelatex")
    if found:
        return found

    candidates = [
        Path.home() / "AppData" / "Local" / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "xelatex.exe",
        Path(r"C:\Program Files\MiKTeX\miktex\bin\x64\xelatex.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise SystemExit(
        "xelatex was not found. Install MiKTeX or TeX Live first.\n"
        "Windows user-scope MiKTeX command:\n"
        "winget install --id MiKTeX.MiKTeX --scope user "
        "--accept-package-agreements --accept-source-agreements --silent"
    )


def tex_preamble(data: dict[str, Any], include_answers: bool) -> str:
    title = latex_escape(data.get("title", "练习题"))
    subtitle = latex_escape(data.get("subtitle", DEFAULT_SUBTITLE))
    version = "答案详解版" if include_answers else "无答案习题版"
    return rf"""\documentclass[12pt]{{article}}
\usepackage[a4paper,left=18mm,right=18mm,top=18mm,bottom=18mm]{{geometry}}
\usepackage{{fontspec}}
\usepackage{{xeCJK}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{tikz}}
\usepackage{{enumitem}}
\usepackage{{fancyhdr}}
\usepackage[most]{{tcolorbox}}
\usepackage{{xcolor}}
\usetikzlibrary{{arrows.meta,calc,angles,quotes,intersections,patterns}}
\setmainfont{{{data.get("latin_font", "Times New Roman")}}}
\setCJKmainfont{{{data.get("cjk_font", "Microsoft YaHei")}}}
\linespread{{1.15}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{6pt}}
\setlength{{\headheight}}{{15pt}}
\emergencystretch=3em
\setlist[enumerate]{{leftmargin=*, itemsep=9pt, topsep=5pt}}
\pagestyle{{fancy}}
\fancyhf{{}}
\lhead{{{subtitle}}}
\rhead{{{version}}}
\cfoot{{第 \thepage 页}}
\definecolor{{ink}}{{HTML}}{{050505}}
\definecolor{{warnbg}}{{HTML}}{{FFF7E6}}
\definecolor{{warnline}}{{HTML}}{{F4D58D}}
\definecolor{{answerbg}}{{HTML}}{{F4F7FB}}
\definecolor{{answerline}}{{HTML}}{{D9E2F0}}
\newcommand{{\fillblank}}{{\underline{{\hspace{{3cm}}}}}}
\newcommand{{\figuregap}}{{\vspace{{3pt}}}}
\newtcolorbox{{warnbox}}{{colback=warnbg,colframe=warnline,boxrule=0.5pt,arc=1mm,left=5pt,right=5pt,top=5pt,bottom=5pt}}
\newtcolorbox{{answerbox}}{{colback=answerbg,colframe=answerline,boxrule=0.5pt,arc=1mm,left=5pt,right=5pt,top=5pt,bottom=5pt,before skip=2pt,after skip=6pt}}
\begin{{document}}
\color{{ink}}
\begin{{center}}
{{\LARGE\bfseries {title}}}\\[4pt]
{{\large {subtitle} -- {version}}}
\end{{center}}
"""


def render_figures(question: dict[str, Any]) -> list[str]:
    figures: list[str] = []
    if question.get("figure_tex"):
        figures.append(str(question["figure_tex"]))
    figures.extend(str(item) for item in question.get("figures_tex", []))

    image_figures = []
    if question.get("figure_image"):
        image_figures.append(question["figure_image"])
    image_figures.extend(question.get("figure_images", []))

    if not figures and not image_figures:
        return []

    lines = [r"\figuregap", r"\begin{center}"]
    for figure in figures:
        lines.append(figure)
        lines.append(r"\figuregap")
    for image in image_figures:
        width = question.get("figure_image_width", r"0.62\linewidth")
        path = image.get("path") if isinstance(image, dict) else image
        image_width = image.get("width", width) if isinstance(image, dict) else width
        normalized = str(path).replace("\\", "/")
        lines.append(rf"\includegraphics[width={image_width}]{{{normalized}}}")
        lines.append(r"\figuregap")
    lines.append(r"\end{center}")
    return lines


def render_question(question: dict[str, Any], include_answers: bool) -> list[str]:
    item_label = question.get("no")
    if item_label not in (None, ""):
        lines = [rf"\item[{latex_escape(item_label)}.] {tex_value(question, 'prompt_tex', 'prompt')}"]
    else:
        lines = [rf"\item {tex_value(question, 'prompt_tex', 'prompt')}"]

    options = question.get("options_tex") or question.get("options") or []
    if options:
        lines.append(r"\begin{enumerate}[label=\Alph*.,itemsep=3pt,topsep=3pt]")
        for option in options:
            lines.append(rf"\item {option if question.get('options_tex') else latex_escape(option)}")
        lines.append(r"\end{enumerate}")

    lines.extend(render_figures(question))

    if include_answers:
        answer = inline_math_if_needed(tex_value(question, "answer_tex", "answer"))
        explanation = tex_value(question, "explanation_tex", "explanation")
        review_note = question.get("review_note")
        lines.append(r"\begin{answerbox}")
        lines.append(rf"\textbf{{答案：}}{answer}\\[2pt]")
        lines.append(rf"\textbf{{讲解：}}{explanation}")
        if review_note:
            lines.append(rf"\\[2pt]\textbf{{复核：}}{latex_escape(review_note)}")
        lines.append(r"\end{answerbox}")
    else:
        lines.append(rf"\vspace{{{question.get('student_space', '5mm')}}}")
    return lines


def build_tex(data: dict[str, Any], include_answers: bool) -> str:
    lines = [tex_preamble(data, include_answers)]
    notes = data.get("notes")
    if notes:
        lines.append(r"\begin{warnbox}")
        lines.append(latex_escape(notes))
        lines.append(r"\end{warnbox}")

    for section in data.get("sections", []):
        lines.append(rf"\section*{{{tex_value(section, 'title_tex', 'title')}}}")
        lines.append(r"\begin{enumerate}")
        for question in section.get("questions", []):
            lines.extend(render_question(question, include_answers))
        lines.append(r"\end{enumerate}")
    lines.append(r"\end{document}")
    return "\n".join(lines)


def build_markdown(data: dict[str, Any], out_path: Path) -> None:
    title = data.get("title", "练习题")
    lines = [f"# {title}-卷面转录", ""]
    if data.get("source"):
        lines += [f"来源：`{data['source']}`", ""]
    if data.get("notes"):
        lines += [f"> {data['notes']}", ""]

    for section in data.get("sections", []):
        lines += [f"## {section.get('title', '')}"]
        for question in section.get("questions", []):
            no = question.get("no", "")
            prompt = question.get("prompt", question.get("prompt_tex", ""))
            lines.append(f"{no}. {prompt}")
            for option in question.get("options", []):
                lines.append(f"   - {option}")
            if question.get("figure_tex") or question.get("figures_tex"):
                lines.append("   - [图形：见 PDF 中的 TikZ 重绘图]")
            if question.get("figure_image") or question.get("figure_images"):
                lines.append("   - [图形：见 PDF 中的原图裁切图]")
        lines.append("")

    lines += ["## 答案速查"]
    for section in data.get("sections", []):
        for question in section.get("questions", []):
            lines.append(f"- {question.get('no', '')}. {question.get('answer', question.get('answer_tex', ''))}")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def compile_tex(tex_path: Path, build_dir: Path, cwd: Path) -> None:
    xelatex = find_xelatex()
    cmd = [
        xelatex,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-directory",
        str(build_dir),
        str(tex_path),
    ]
    for _ in range(2):
        result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
        if result.returncode != 0:
            raise SystemExit(
                f"XeLaTeX failed for {tex_path.name}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Log: {build_dir / (tex_path.stem + '.log')}\n"
                f"STDOUT:\n{result.stdout[-4000:]}\nSTDERR:\n{result.stderr[-2000:]}"
            )


def write_pdf(data: dict[str, Any], out_dir: Path, build_dir: Path, stem: str, include_answers: bool) -> Path:
    tex_path = build_dir / f"{stem}.tex"
    tex_path.write_text(build_tex(data, include_answers), encoding="utf-8")
    compile_tex(tex_path, build_dir, Path.cwd())
    final_path = out_dir / f"{stem}.pdf"
    shutil.copy2(build_dir / f"{stem}.pdf", final_path)
    return final_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("worksheet", type=Path)
    parser.add_argument("--out", type=Path, default=Path("output/pdf"))
    parser.add_argument("--keep-build", action="store_true")
    args = parser.parse_args()

    data = load_data(args.worksheet)
    title = str(data.get("title", args.worksheet.stem))
    out_dir = args.out
    build_dir = out_dir / "latex-build"
    out_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    outputs = [
        out_dir / f"{title}-卷面转录.md",
        write_pdf(data, out_dir, build_dir, f"{title}-无答案习题", include_answers=False),
        write_pdf(data, out_dir, build_dir, f"{title}-答案详解", include_answers=True),
    ]
    build_markdown(data, outputs[0])

    if not args.keep_build:
        shutil.rmtree(build_dir, ignore_errors=True)

    for output in outputs:
        print(f"Generated {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main())
