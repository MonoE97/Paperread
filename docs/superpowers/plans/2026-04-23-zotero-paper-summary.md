# Zotero Paper Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Zotero-first paper summary workflow where a user gives a Zotero paper title, Codex finds the item through `zotero-mcp`, extracts the attached PDF with a `uv`-managed Python CLI, generates a structured Chinese research note, validates it, and writes it as a Zotero child note only after explicit write intent.

**Architecture:** The project has two layers. The Python package `zotero_paperread` handles deterministic local work: PDF extraction, note rendering, note validation, and CLI preview. The Codex skill `skills/zotero-paper-summary/SKILL.md` handles orchestration: Zotero MCP lookup/write, paper reasoning, summary schema production, and dry-run/write gating.

**Tech Stack:** Python 3.13, `uv`, Typer, Rich, PyMuPDF, Jinja2, pytest, Codex skills, Zotero MCP.

---

## Current Repo State

- `/Users/jwxi/Desktop/AIflow/Zotero_paperread` has been initialized as a local Git repository on branch `main`.
- No remote is configured.
- No commits have been created.
- Do not run `git push`, create a GitHub repository, or publish anything without explicit user approval.

## File Structure

- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/.gitignore`: ignore Python, `uv`, editor, macOS, and generated local preview artifacts.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/AGENTS.md`: project rules, mutation boundaries, validation commands, and `uv` policy.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/pyproject.toml`: Python package metadata, dependencies, CLI entry point, pytest config.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/`: package code.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`: Zotero/Better Notes friendly note template.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/`: unit and CLI tests.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`: Codex orchestration skill adapted from `evil-read-arxiv` ideas.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/references/evil-read-arxiv-adaptation.md`: record what is borrowed and what is rejected.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/github-publication.md`: local-to-GitHub publication checklist and approval gates.

## Non-Negotiable Boundaries

- Do not modify Zotero SQLite files.
- Do not modify Better Notes preferences or templates.
- Do not install global Python packages.
- Do not write Zotero notes during tests or dry-run.
- Use `uv run` for all local Python commands.
- Use `uv add` for missing project dependencies when implementation reaches the dependency setup task.
- For the first implementation pass, support one paper at a time by Zotero title; collection batch processing is out of V1.
- Keep GitHub publication as a separate gated action: local commits are allowed by this plan, but remote creation and `git push` require explicit user confirmation.

---

### Task 0: Repository Hygiene

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/.gitignore`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/.git/config`

- [ ] **Step 1: Verify repository state**

Run:

```bash
git status --short --branch
git remote -v
```

Expected: branch is `main`, the repository has no commits yet, and `git remote -v` prints no remotes.

- [ ] **Step 2: Create `.gitignore`**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/.gitignore` with this content:

```gitignore
# macOS
.DS_Store

# Python
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/

# uv and virtual environments
.venv/

# Local outputs and previews
*.log
tmp/
.tmp/
dist/
build/
*.pdf.txt
*.extract.json
*.summary.json
*.note.md
```

- [ ] **Step 3: Verify ignored macOS metadata**

Run:

```bash
git status --short --ignored
```

Expected: `.DS_Store` is ignored and does not appear as an untracked file.

- [ ] **Step 4: Record GitHub publication boundary**

Run:

```bash
git remote -v
```

Expected: no output. If a remote appears unexpectedly, stop and ask the user before changing it.

---

### Task 1: Establish Project Rules

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/AGENTS.md`

- [ ] **Step 1: Create the project rule file**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/AGENTS.md` with this content:

```markdown
# AGENTS.md

## 项目目标

本项目实现 Zotero-first 文献总结工作流：输入 Zotero 中的文章标题，Codex 通过 Zotero MCP 定位条目，使用本地 Python 工具抽取 PDF 内容，生成中文结构化论文总结，并在用户明确要求写入时创建 Zotero 子笔记。

## 目录约定

- `src/zotero_paperread/`：Python 包代码，只放确定性工具逻辑。
- `tests/`：pytest 测试，禁止真实写入 Zotero。
- `templates/`：Jinja2 note 模板。
- `skills/`：Codex skill 定义。
- `docs/references/`：外部项目参考与设计取舍记录。
- `docs/superpowers/plans/`：实施计划。

## 环境与依赖

- Python 环境必须用 `uv` 管理。
- 默认执行命令使用 `uv run`。
- 缺少项目依赖时使用 `uv add` 或 `uv add --dev`，不使用 `pip install`、`conda install` 或全局安装。
- 不修改系统 Python、conda base 环境或 shell 全局配置。

## Git 与发布

- 当前项目是本地 Git repo，默认分支 `main`。
- 可以创建本地 commit。
- 禁止在未获用户明确确认前执行 `git push`、创建 GitHub remote、公开发布或部署。
- `.DS_Store`、虚拟环境、缓存和本地预览文件必须被 `.gitignore` 忽略。

## Zotero 边界

- 读取 Zotero 信息优先使用 `zotero-mcp`。
- 写入 Zotero 只能通过 `zotero-mcp write_note`，且必须由用户明确触发。
- 禁止直接修改 Zotero SQLite、Zotero storage 元数据、Better Notes 配置或 Better Notes 模板。
- dry-run 必须只输出预览，不写 Zotero。

## Better Notes 策略

Better Notes 是可选阅读增强层。V1 生成 Zotero 子笔记，保证 Better Notes 能正常显示；不调用 `Zotero.BetterNotes.api`，不依赖 Better Notes 存在。

## 验证命令

改完代码后运行：

```bash
uv run pytest
uv run zotero-paperread --help
```

涉及 PDF 抽取时额外运行：

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

## 写入规则

- 默认先 dry-run。
- 真实写入 Zotero 前，必须展示 note 预览和目标 Zotero item 标题。
- 重复运行不覆盖旧 note，使用 `[Codex Summary] <paper title> - YYYY-MM-DD` 标题创建新版本。
```

- [ ] **Step 2: Verify the rule file exists**

Run:

```bash
test -f AGENTS.md && sed -n '1,220p' AGENTS.md
```

Expected: command exits `0` and prints the project rules.

---

### Task 2: Create `uv` Python Project Skeleton

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/pyproject.toml`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/__init__.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_smoke.py`

- [ ] **Step 1: Create package directories**

Run:

```bash
mkdir -p src/zotero_paperread tests templates skills/zotero-paper-summary docs/references
```

Expected: directories are created with no output.

- [ ] **Step 2: Create `pyproject.toml`**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/pyproject.toml`:

```toml
[project]
name = "zotero-paperread"
version = "0.1.0"
description = "Zotero-first paper summary workflow for Codex."
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
    "jinja2>=3.1.6",
    "pymupdf>=1.26.0",
    "rich>=14.0.0",
    "typer>=0.16.0",
]

[project.scripts]
zotero-paperread = "zotero_paperread.cli:app"

[dependency-groups]
dev = [
    "pytest>=8.3.0",
]

[build-system]
requires = ["uv_build>=0.10.0,<0.11.0"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "-q"
```

- [ ] **Step 3: Create package init**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/__init__.py`:

```python
"""Utilities for Zotero-first paper reading workflows."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Create initial CLI**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`:

```python
from __future__ import annotations

import typer

app = typer.Typer(help="Zotero-first paper reading utilities.")


@app.command()
def version() -> None:
    """Print the package version."""
    from zotero_paperread import __version__

    typer.echo(__version__)
```

- [ ] **Step 5: Create CLI smoke test**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_smoke.py`:

```python
from typer.testing import CliRunner

from zotero_paperread.cli import app


def test_version_command_prints_version() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"
```

- [ ] **Step 6: Lock dependencies**

Run:

```bash
uv lock
```

Expected: `uv.lock` is created and the command exits `0`.

- [ ] **Step 7: Run the first test**

Run:

```bash
uv run pytest tests/test_cli_smoke.py
```

Expected: `1 passed`.

---

### Task 3: Implement PDF Extraction

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/pdf_extract.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_pdf_extract.py`

- [ ] **Step 1: Write PDF extraction tests**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_pdf_extract.py`:

```python
from pathlib import Path

import fitz
import pytest

from zotero_paperread.pdf_extract import extract_pdf


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_extract_pdf_returns_text_and_page_count(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Abstract\nThis is page one.", "Methods\nThis is page two."])

    result = extract_pdf(pdf_path)

    assert result["pdf_path"] == str(pdf_path)
    assert result["page_count"] == 2
    assert "Abstract" in result["text"]
    assert "Methods" in result["text"]
    assert result["warnings"] == []


def test_extract_pdf_respects_max_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["first page", "second page"])

    result = extract_pdf(pdf_path, max_pages=1)

    assert result["page_count"] == 2
    assert "first page" in result["text"]
    assert "second page" not in result["text"]
    assert "truncated_to_1_pages" in result["warnings"]


def test_extract_pdf_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="PDF not found"):
        extract_pdf(tmp_path / "missing.pdf")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_pdf_extract.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zotero_paperread.pdf_extract'`.

- [ ] **Step 3: Implement `pdf_extract.py`**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/pdf_extract.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz


def extract_pdf(pdf_path: Path, max_pages: int | None = None) -> dict[str, Any]:
    """Extract text and lightweight metadata from a PDF."""
    resolved = Path(pdf_path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"PDF not found: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"PDF path is not a file: {resolved}")

    warnings: list[str] = []
    doc = fitz.open(resolved)
    try:
        page_count = doc.page_count
        limit = page_count if max_pages is None else min(max_pages, page_count)
        if max_pages is not None and max_pages < page_count:
            warnings.append(f"truncated_to_{max_pages}_pages")

        page_texts: list[str] = []
        for index in range(limit):
            text = doc.load_page(index).get_text("text").strip()
            if text:
                page_texts.append(f"\n\n<!-- page:{index + 1} -->\n{text}")

        combined = "".join(page_texts).strip()
        if not combined:
            warnings.append("no_extractable_text")

        return {
            "pdf_path": str(resolved),
            "page_count": page_count,
            "extracted_pages": limit,
            "text": combined,
            "warnings": warnings,
        }
    finally:
        doc.close()
```

- [ ] **Step 4: Add `extract-pdf` CLI command**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py` to this full content:

```python
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from zotero_paperread.pdf_extract import extract_pdf

app = typer.Typer(help="Zotero-first paper reading utilities.")
console = Console()


@app.command()
def version() -> None:
    """Print the package version."""
    from zotero_paperread import __version__

    typer.echo(__version__)


@app.command("extract-pdf")
def extract_pdf_command(
    pdf_path: Path,
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to this file."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Extract at most this many pages."),
) -> None:
    """Extract text from a PDF and emit JSON."""
    result = extract_pdf(pdf_path, max_pages=max_pages)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if output is None:
        console.print(payload)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")
    console.print(f"Wrote extraction JSON: {output}")
```

- [ ] **Step 5: Run extraction tests**

Run:

```bash
uv run pytest tests/test_pdf_extract.py
```

Expected: `3 passed`.

---

### Task 4: Define Summary Schema and Note Rendering

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py`

- [ ] **Step 1: Write note rendering tests**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py`:

```python
from zotero_paperread.note import render_note, validate_note


METADATA = {
    "key": "ABC123",
    "title": "A Useful Materials Paper",
    "creators": "Ada Lovelace, Chen Ning",
    "date": "2026",
    "DOI": "10.1000/example",
    "url": "https://example.org/paper",
    "zoteroUrl": "zotero://select/library/items/ABC123",
}

SUMMARY = {
    "one_sentence_summary": "这篇论文提出一种用于材料发现的机器学习框架。",
    "abstract_translation": "本文摘要的中文翻译。",
    "key_points": ["提出新框架", "验证材料性质预测"],
    "research_question": "如何更可靠地预测材料性质？",
    "method": "作者结合图神经网络和物理约束。",
    "experiments": "实验覆盖多个材料数据集。",
    "contributions": ["物理约束建模", "系统实验验证"],
    "limitations": ["数据集规模有限"],
    "ai4s_relevance": "可迁移到 AI for Science 的材料性质预测任务。",
    "follow_up_keywords": ["materials discovery", "physics-informed ML"],
    "quality_score": "8.0/10",
    "extraction_warnings": [],
}


def test_render_note_contains_required_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "## 核心结论" in note
    assert "## 研究问题" in note
    assert "## 方法拆解" in note
    assert "## AI+物理/材料启发" in note
    assert "zotero://select/library/items/ABC123" in note


def test_validate_note_accepts_complete_note() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    errors = validate_note(note)

    assert errors == []


def test_validate_note_rejects_missing_required_section() -> None:
    errors = validate_note("# title\n\n## 核心结论\ncontent")

    assert "missing_section: 元数据" in errors
    assert "missing_section: 研究问题" in errors
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_note.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zotero_paperread.note'`.

- [ ] **Step 3: Create note template**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`:

```markdown
# [Codex Summary] {{ title }} - {{ generated_date }}

## 元数据

- **Zotero Key**: {{ key }}
- **标题**: {{ title }}
- **作者**: {{ creators }}
- **日期**: {{ date }}
- **DOI**: {{ doi }}
- **URL**: {{ url }}
- **Zotero 链接**: {{ zotero_url }}
- **质量评分**: {{ quality_score }}

## 核心结论

{{ one_sentence_summary }}

## 摘要翻译

{{ abstract_translation }}

## 关键要点

{% for item in key_points -%}
- {{ item }}
{% endfor %}

## 研究问题

{{ research_question }}

## 方法拆解

{{ method }}

## 实验与证据

{{ experiments }}

## 主要贡献

{% for item in contributions -%}
- {{ item }}
{% endfor %}

## 局限与风险

{% for item in limitations -%}
- {{ item }}
{% endfor %}

## AI+物理/材料启发

{{ ai4s_relevance }}

## 后续关键词

{% for item in follow_up_keywords -%}
- {{ item }}
{% endfor %}

## 抽取告警

{% if extraction_warnings -%}
{% for item in extraction_warnings -%}
- {{ item }}
{% endfor %}
{% else -%}
- none
{% endif %}

---

Tags: codex-summary, paper-summary
```

- [ ] **Step 4: Implement note rendering**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`:

```python
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REQUIRED_SECTIONS = [
    "元数据",
    "核心结论",
    "摘要翻译",
    "关键要点",
    "研究问题",
    "方法拆解",
    "实验与证据",
    "主要贡献",
    "局限与风险",
    "AI+物理/材料启发",
    "后续关键词",
    "抽取告警",
]

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"


def render_note(metadata: dict[str, Any], summary: dict[str, Any], generated_date: str | None = None) -> str:
    """Render a Zotero/Better Notes friendly Markdown note."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("zotero_note.md.j2")
    context = {
        "generated_date": generated_date or date.today().isoformat(),
        "key": metadata.get("key", ""),
        "title": metadata.get("title", ""),
        "creators": metadata.get("creators", ""),
        "date": metadata.get("date", ""),
        "doi": metadata.get("DOI", ""),
        "url": metadata.get("url", ""),
        "zotero_url": metadata.get("zoteroUrl", ""),
        "quality_score": summary.get("quality_score", ""),
        "one_sentence_summary": summary.get("one_sentence_summary", ""),
        "abstract_translation": summary.get("abstract_translation", ""),
        "key_points": summary.get("key_points", []),
        "research_question": summary.get("research_question", ""),
        "method": summary.get("method", ""),
        "experiments": summary.get("experiments", ""),
        "contributions": summary.get("contributions", []),
        "limitations": summary.get("limitations", []),
        "ai4s_relevance": summary.get("ai4s_relevance", ""),
        "follow_up_keywords": summary.get("follow_up_keywords", []),
        "extraction_warnings": summary.get("extraction_warnings", []),
    }
    return template.render(**context).strip() + "\n"


def validate_note(note: str) -> list[str]:
    """Return validation errors for a rendered note."""
    errors: list[str] = []
    for section in REQUIRED_SECTIONS:
        if f"## {section}" not in note:
            errors.append(f"missing_section: {section}")
    if "[Codex Summary]" not in note:
        errors.append("missing_codex_summary_title")
    if "Tags: codex-summary, paper-summary" not in note:
        errors.append("missing_tags")
    return errors
```

- [ ] **Step 5: Run note tests**

Run:

```bash
uv run pytest tests/test_note.py
```

Expected: `3 passed`.

---

### Task 5: Add Note CLI Commands

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Write CLI note tests**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`:

```python
import json
from pathlib import Path

from typer.testing import CliRunner

from zotero_paperread.cli import app


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_render_note_command_writes_markdown(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    write_json(metadata_path, {"key": "ABC123", "title": "Paper", "creators": "A", "date": "2026"})
    write_json(
        summary_path,
        {
            "one_sentence_summary": "一句话总结。",
            "abstract_translation": "摘要翻译。",
            "key_points": ["要点"],
            "research_question": "问题",
            "method": "方法",
            "experiments": "实验",
            "contributions": ["贡献"],
            "limitations": ["局限"],
            "ai4s_relevance": "启发",
            "follow_up_keywords": ["keyword"],
            "quality_score": "8/10",
            "extraction_warnings": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["render-note", str(metadata_path), str(summary_path), "--output", str(output_path)])

    assert result.exit_code == 0
    assert output_path.exists()
    assert "## 核心结论" in output_path.read_text(encoding="utf-8")


def test_validate_note_command_fails_for_incomplete_note(tmp_path: Path) -> None:
    note_path = tmp_path / "bad.md"
    note_path.write_text("# bad\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["validate-note", str(note_path)])

    assert result.exit_code == 1
    assert "missing_section" in result.stdout
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_note.py
```

Expected: FAIL because the `render-note` and `validate-note` commands are not registered.

- [ ] **Step 3: Replace `cli.py` with full CLI**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py` to this full content:

```python
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from zotero_paperread.note import render_note, validate_note
from zotero_paperread.pdf_extract import extract_pdf

app = typer.Typer(help="Zotero-first paper reading utilities.")
console = Console()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@app.command()
def version() -> None:
    """Print the package version."""
    from zotero_paperread import __version__

    typer.echo(__version__)


@app.command("extract-pdf")
def extract_pdf_command(
    pdf_path: Path,
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to this file."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Extract at most this many pages."),
) -> None:
    """Extract text from a PDF and emit JSON."""
    result = extract_pdf(pdf_path, max_pages=max_pages)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if output is None:
        console.print(payload)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")
    console.print(f"Wrote extraction JSON: {output}")


@app.command("render-note")
def render_note_command(
    metadata_json: Path,
    summary_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write Markdown note to this file."),
    generated_date: str | None = typer.Option(None, "--generated-date", help="Override generated date."),
) -> None:
    """Render a Zotero note from metadata and summary JSON."""
    note = render_note(read_json(metadata_json), read_json(summary_json), generated_date=generated_date)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(note, encoding="utf-8")
    console.print(f"Wrote note Markdown: {output}")


@app.command("validate-note")
def validate_note_command(note_path: Path) -> None:
    """Validate a rendered note."""
    errors = validate_note(note_path.read_text(encoding="utf-8"))
    if errors:
        for error in errors:
            console.print(error)
        raise typer.Exit(1)
    console.print("note_valid")


@app.command("preview-note")
def preview_note_command(note_path: Path) -> None:
    """Print a rendered note without writing to Zotero."""
    console.print(note_path.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
uv run pytest tests/test_cli_note.py
```

Expected: `2 passed`.

---

### Task 6: Add Codex Skill

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`

- [ ] **Step 1: Create the skill file**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`:

```markdown
---
name: zotero-paper-summary
description: 输入 Zotero 论文标题，使用 Zotero MCP 定位条目，抽取 PDF，生成中文结构化论文总结，并在明确写入时创建 Zotero 子笔记。
---

# Zotero Paper Summary

## 目标

把 Zotero 中的一篇论文转换为中文结构化研究笔记。默认只 dry-run；只有用户明确要求写入 Zotero 时，才调用 `write_note` 创建子笔记。

## 输入

接受 Zotero 条目标题或标题片段。V1 只处理单篇论文，不处理 collection 批量任务。

## 工具边界

- 用 `zotero-mcp search_library` 搜索条目。
- 用 `zotero-mcp get_item_details` 获取元数据和 PDF attachment path。
- 用本项目 Python CLI 抽取 PDF 与渲染 note。
- 用 `zotero-mcp write_note` 写入 Zotero 子笔记。
- 不修改 Zotero SQLite。
- 不调用 Better Notes API。
- 不修改 Better Notes 配置。

## 工作流

1. 搜索 Zotero 条目：
   - 使用标题 exact 或 contains 搜索。
   - 0 个匹配：停止，告诉用户没有找到。
   - 多个匹配：列出候选标题、作者、年份和 key，停止，不写入。
   - 1 个匹配：继续。

2. 获取条目详情：
   - 读取 title、creators、date、DOI、url、zoteroUrl、attachments。
   - 选择第一个 `contentType` 为 `application/pdf` 且有 `path` 的附件。
   - 无 PDF 时继续生成 abstract-only note，并在 `extraction_warnings` 写入 `missing_pdf_attachment`。

3. 抽取 PDF：
   - 有 PDF path 时运行：

```bash
uv run zotero-paperread extract-pdf "<PDF_PATH>" --output /tmp/zotero-paperread-extract.json
```

4. 生成 summary JSON：
   - 输出必须包含这些字段：

```json
{
  "one_sentence_summary": "",
  "abstract_translation": "",
  "key_points": [],
  "research_question": "",
  "method": "",
  "experiments": "",
  "contributions": [],
  "limitations": [],
  "ai4s_relevance": "",
  "follow_up_keywords": [],
  "quality_score": "",
  "extraction_warnings": []
}
```

5. 分析要求：
   - 参考 `evil-read-arxiv` 的 `paper-analyze` 思路，覆盖摘要翻译、研究背景、研究问题、方法、实验、贡献、局限、相关方向定位。
   - 公式使用 Markdown LaTeX：行内 `$...$`，块级 `$$...$$`。
   - 不编造论文没有支持的数据、实验或结论。
   - 对 AI+物理/材料的启发必须独立成节，结合用户研究方向给出判断。

6. 渲染和验证 note：

```bash
uv run zotero-paperread render-note /tmp/zotero-paperread-metadata.json /tmp/zotero-paperread-summary.json --output /tmp/zotero-paperread-note.md
uv run zotero-paperread validate-note /tmp/zotero-paperread-note.md
uv run zotero-paperread preview-note /tmp/zotero-paperread-note.md
```

7. 写入 Zotero：
   - 只有用户明确要求“写入”“创建 note”“保存到 Zotero”等动作时执行。
   - note 标题由模板生成：`[Codex Summary] <paper title> - YYYY-MM-DD`。
   - 调用 `write_note(action="create", parentKey=<item key>, content=<note markdown>, tags=["codex-summary","paper-summary"])`。

## Better Notes 兼容

生成普通 Zotero 子笔记。Better Notes 如果已安装，可直接显示和管理该 note；本 skill 不依赖 Better Notes。

## V1 不做

- 不批量处理 collection。
- 不抽取和插入图片。
- 不更新 Obsidian vault。
- 不维护 PaperGraph。
- 不下载 arXiv 源码包作为必需步骤。
```

- [ ] **Step 2: Verify the skill file**

Run:

```bash
sed -n '1,240p' skills/zotero-paper-summary/SKILL.md
```

Expected: file prints and includes `write_note` only under the explicit write step.

---

### Task 7: Document `evil-read-arxiv` Adaptation

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/references/evil-read-arxiv-adaptation.md`

- [ ] **Step 1: Create reference note**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/references/evil-read-arxiv-adaptation.md`:

```markdown
# evil-read-arxiv Adaptation Notes

## Source Ideas To Reuse

- Skill-based workflow decomposition.
- `paper-analyze` style deep paper analysis sections:
  - abstract translation
  - research background and motivation
  - research question
  - method overview
  - experiments and results
  - contributions
  - limitations
  - related work positioning
- Markdown LaTeX rule:
  - inline formulas use `$...$`
  - block formulas use `$$...$$`
- `extract-paper-images` insight:
  - original paper figures are better than naive PDF image extraction
  - arXiv source extraction is useful for V2 image support

## Source Ideas Not To Reuse In V1

- arXiv ID as the primary input.
- Obsidian vault path and `OBSIDIAN_VAULT_PATH`.
- `20_Research/Papers` note layout.
- PaperGraph JSON.
- Daily recommendation workflow.
- Automatic arXiv source download as a required step.

## Zotero-First Replacement

- Primary input is Zotero title.
- Source of truth is Zotero MCP.
- Output is Zotero child note.
- Python CLI only performs deterministic local transformations.
- Codex performs interpretation, scientific judgment, and note writing.

## Better Notes Boundary

Better Notes is treated as a viewer and note-management enhancer. The generated child note should be readable in Better Notes, but this project does not call Better Notes internal APIs.
```

- [ ] **Step 2: Verify the reference note**

Run:

```bash
sed -n '1,220p' docs/references/evil-read-arxiv-adaptation.md
```

Expected: file prints the adaptation record.

---

### Task 8: End-to-End Dry-Run Fixture Test

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_end_to_end_dry_run.py`

- [ ] **Step 1: Write dry-run test**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_end_to_end_dry_run.py`:

```python
import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from zotero_paperread.cli import app
from zotero_paperread.pdf_extract import extract_pdf


def make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Abstract\nWe propose a physics-informed model for solid-state battery materials.\n"
        "Methods\nThe method combines graph learning and physical constraints.\n"
        "Results\nThe model improves prediction accuracy on held-out compositions.",
    )
    doc.save(path)
    doc.close()


def test_pdf_to_note_dry_run(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path)
    extraction = extract_pdf(pdf_path)
    assert "physics-informed" in extraction["text"]

    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    note_path = tmp_path / "note.md"

    metadata_path.write_text(
        json.dumps(
            {
                "key": "DRYRUN1",
                "title": "Physics-Informed Materials Prediction",
                "creators": "Mono Researcher",
                "date": "2026",
                "DOI": "10.1000/dryrun",
                "url": "https://example.org/dryrun",
                "zoteroUrl": "zotero://select/library/items/DRYRUN1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "one_sentence_summary": "这篇论文用物理约束增强材料性质预测。",
                "abstract_translation": "作者提出一种面向固态电池材料的物理约束模型。",
                "key_points": ["面向固态电池材料", "结合图学习和物理约束"],
                "research_question": "如何在材料性质预测中融合物理先验？",
                "method": "方法结合 graph learning 和 physical constraints。",
                "experiments": "实验在 held-out compositions 上验证预测精度。",
                "contributions": ["提出物理约束预测框架", "验证泛化性能"],
                "limitations": ["测试 PDF 是最小夹具，不代表真实论文复杂度"],
                "ai4s_relevance": "该路线适合 AI+材料中的小数据泛化问题。",
                "follow_up_keywords": ["physics-informed ML", "solid-state battery"],
                "quality_score": "8/10",
                "extraction_warnings": extraction["warnings"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    render_result = runner.invoke(app, ["render-note", str(metadata_path), str(summary_path), "--output", str(note_path)])
    assert render_result.exit_code == 0

    validate_result = runner.invoke(app, ["validate-note", str(note_path)])
    assert validate_result.exit_code == 0
    assert "note_valid" in validate_result.stdout
```

- [ ] **Step 2: Run dry-run test**

Run:

```bash
uv run pytest tests/test_end_to_end_dry_run.py
```

Expected: `1 passed`.

---

### Task 9: Add README Usage Contract

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`

- [ ] **Step 1: Create README**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`:

```markdown
# Zotero Paperread

Zotero-first literature summary workflow for Codex.

## What It Does

Given a Zotero paper title, Codex can:

1. find the Zotero item through `zotero-mcp`;
2. locate the attached PDF path;
3. extract PDF text with a local `uv`-managed Python CLI;
4. generate a Chinese structured paper summary;
5. preview and validate the note;
6. create a Zotero child note only when explicitly requested.

## Local Commands

```bash
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf path/to/paper.pdf --output /tmp/extract.json
uv run zotero-paperread render-note /tmp/metadata.json /tmp/summary.json --output /tmp/note.md
uv run zotero-paperread validate-note /tmp/note.md
uv run zotero-paperread preview-note /tmp/note.md
```

## Safety

- Dry-run is the default workflow.
- Tests never write to Zotero.
- Zotero writes happen only through `zotero-mcp write_note`.
- Better Notes is optional and not called directly.

## Reference

This project adapts the skill-based paper-analysis ideas from `evil-read-arxiv`, but replaces arXiv/Obsidian assumptions with a Zotero-first workflow.
```

- [ ] **Step 2: Verify README**

Run:

```bash
sed -n '1,220p' README.md
```

Expected: file prints the usage contract.

---

### Task 10: Full Verification

**Files:**
- Read: all files created in previous tasks.

- [ ] **Step 1: Run all tests**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Verify CLI help**

Run:

```bash
uv run zotero-paperread --help
```

Expected: output lists `version`, `extract-pdf`, `render-note`, `validate-note`, and `preview-note`.

- [ ] **Step 3: Verify no Zotero write occurs in tests**

Run:

```bash
rg -n "write_note|write_metadata|write_item|remove_items_from_collection" tests src
```

Expected: no matches in `tests` or `src`.

- [ ] **Step 4: Verify skill contains the only write guidance**

Run:

```bash
rg -n "write_note" skills/zotero-paper-summary/SKILL.md
```

Expected: matches only in the explicit write step.

- [ ] **Step 5: Verify no placeholder language remains**

Run:

```bash
rg -n "TB[D]|TO[D]O|fill[ ]in|implement[ ]later|适[当]|待[定]" AGENTS.md README.md pyproject.toml src tests templates skills docs/references
```

Expected: no matches.

---

### Task 11: GitHub Publication Preparation

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/github-publication.md`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/pyproject.toml`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/.gitignore`

- [ ] **Step 1: Create publication checklist**

Use `apply_patch` to create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/github-publication.md` with this content:

```markdown
# GitHub Publication Checklist

## Local Repository

- Repository path: `/Users/jwxi/Desktop/AIflow/Zotero_paperread`
- Default branch: `main`
- Suggested GitHub repository name: `zotero-paperread`
- Package name: `zotero-paperread`

## Before Creating Remote

Run:

```bash
git status --short --branch
uv run pytest
uv run zotero-paperread --help
rg -n "token|password|secret|api[_-]?key|Bearer" .
```

Expected:

- working tree contains only intended changes;
- tests pass;
- CLI help renders;
- secret scan has no real secrets.

## Approval Gate

Stop before any of these actions unless the user explicitly approves:

- `gh repo create`
- `git remote add origin ...`
- `git push`
- publishing releases or packages

## Suggested First Remote Setup After Approval

```bash
gh repo create zotero-paperread --private --source=. --remote=origin
git push -u origin main
```

Use `--public` only if the user explicitly chooses public visibility.
```

- [ ] **Step 2: Verify checklist**

Run:

```bash
sed -n '1,220p' docs/github-publication.md
```

Expected: checklist prints and contains the approval gate before remote or push.

- [ ] **Step 3: Create local implementation commit**

Run:

```bash
git status --short
git add .gitignore AGENTS.md README.md pyproject.toml uv.lock src tests templates skills docs
git commit -m "feat: add zotero paper summary workflow"
```

Expected: one local commit is created. If `git status --short` shows unexpected files outside this plan, stop and review them before staging.

- [ ] **Step 4: Confirm no remote is configured**

Run:

```bash
git remote -v
```

Expected: no output.

---

## Execution Notes

- The repository has already been initialized locally on branch `main`; do not run `git init` again.
- Commit after each task with a focused message such as `chore: add project rules`, `feat: add pdf extraction`, and `feat: add zotero summary skill`.
- Do not create a GitHub remote or push until the user explicitly approves the publication step.
- Current project directory was empty when this plan was written, so every created file above is part of the new workflow.
- Real Zotero write testing is intentionally excluded from automated tests. Manual acceptance should use a single known Zotero item and only after the dry-run preview is reviewed.
