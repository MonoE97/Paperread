#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
README_ZH = ROOT / "README.zh-CN.md"
AGENTS = ROOT / "AGENTS.md"
SKILL = ROOT / "skill" / "SKILL.md"
BATCH_SKILL = ROOT / "batch_skill" / "SKILL.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, phrase: str, label: str, errors: list[str]) -> None:
    if phrase not in text:
        errors.append(f"{label} missing phrase: {phrase}")


def reject(text: str, phrase: str, label: str, errors: list[str]) -> None:
    if phrase in text:
        errors.append(f"{label} contains stale phrase: {phrase}")


def validate() -> list[str]:
    errors: list[str] = []
    english = read(README)
    chinese = read(README_ZH)
    agents = read(AGENTS)
    skill = read(SKILL)
    batch_skill = read(BATCH_SKILL)
    combined_public = "\n".join([english, chinese, agents, skill, batch_skill])

    require(english, "[简体中文](README.zh-CN.md)", "README.md", errors)
    require(chinese, "[English](README.md)", "README.zh-CN.md", errors)

    for label, text in [
        ("README.md", english),
        ("README.zh-CN.md", chinese),
    ]:
        for phrase in [
            "skill/",
            "batch_skill/",
            "paperread",
            "paperread-batch",
            "uv --version",
            "uv sync --locked",
            "uv python install 3.13",
            "uv run paperread --help",
            "uv run paperread-batch --help",
            "Use `paperread`",
            "Use `paperread-batch`",
            "https://github.com/cookjohn/zotero-mcp#readme",
            "zotero-mcp-plugin",
            "Tools -> Add-ons",
            "http://127.0.0.1:23120/mcp",
            "test ! -e \"$install_dir\"",
            "cp -R /path/to/Paperread/skill \"$install_dir\"",
            "cp -R /path/to/Paperread/batch_skill \"$install_dir\"",
            "$HOME/.claude/skills/paperread",
            "$HOME/.claude/skills/paperread-batch",
            "prepare_only",
            "write_note",
            "refresh-live-notes",
            "write-payload.json",
            "runs/YYYY-MM-DD/<title-slug>/",
            "<pdf_stem>_analysis/",
            "<pdf_stem>_note.md",
            "context.md",
            "figure_context.md",
            "section_context.md",
            "scripts/capture-secondary-url.mjs",
            "README.md",
        ]:
            require(text, phrase, label, errors)

    for phrase in [
        "Zotero MCP `write_note`",
        "Zotero local API",
        "SQLite",
        "local-output only",
        "Local PDF path and directory path inputs skip Zotero lookup and duplicate checks",
        "Existing local paths are not Zotero title fragments",
    ]:
        require(english, phrase, "README.md", errors)

    for phrase in [
        "Zotero MCP `write_note`",
        "Zotero local API",
        "SQLite",
        "只能输出本地文件",
        "本地 PDF path 和目录 path 输入会跳过 Zotero 搜索和去重检查",
        "已存在的本地路径不是 Zotero 标题片段",
    ]:
        require(chinese, phrase, "README.zh-CN.md", errors)

    for phrase in [
        "skill/pyproject.toml",
        "skill/uv.lock",
        "skill/src/paperread/",
        "skill/tests/",
        "skill/templates/",
        "batch_skill/pyproject.toml",
        "batch_skill/uv.lock",
        "batch_skill/src/paperread_batch/",
        "batch_skill/tests/",
        "batch_skill/references/",
        "python docs/superpowers/scripts/validate-root-docs.py",
        "本地 `.pdf` path 和本地目录 path 优先于 Zotero title routing",
    ]:
        require(agents, phrase, "AGENTS.md", errors)

    for phrase in [
        "skill root",
        "uv --version",
        "uv sync --locked",
        "uv python install 3.13",
        "uv run paperread --help",
        "Typical Use",
        "references/pdf-path-workflow.md",
        "references/zotero-workflow.md",
        "https://github.com/cookjohn/zotero-mcp#readme",
        "Zotero MCP `write_note`",
        "Local PDF path and directory path inputs skip Zotero lookup and duplicate checks",
        "Existing local paths are not Zotero title fragments",
    ]:
        require(skill, phrase, "skill/SKILL.md", errors)

    for phrase in [
        "$paperread",
        "uv --version",
        "uv sync --locked",
        "uv run paperread-batch --help",
        "references/batch-workflow.md",
        "Default Codex concurrency is 3",
        "Typical Use",
        "zotero-mcp-plugin",
        "http://127.0.0.1:23120/mcp",
        "prepare_only",
        "30 秒结论",
        "write_note",
        "PDF folder and PDF path items are local-only",
        "do not run Zotero lookup, duplicate checks, next-write, or Zotero write-through",
    ]:
        require(batch_skill, phrase, "batch_skill/SKILL.md", errors)

    for phrase in [
        "clone-and-run",
        "Public V1",
        "repo-local v1",
        "from the repo root",
        "not a standalone global skill installation",
        "/Users/jwxi",
    ]:
        reject(combined_public, phrase, "public docs", errors)

    for stale_path in ["pyproject.toml", "uv.lock", "src", "templates", "tests"]:
        if (ROOT / stale_path).exists():
            errors.append(f"runtime path remains at repository root: {stale_path}")

    if (ROOT / "skill" / "README.md").exists():
        errors.append("skill/README.md must not exist")
    if (ROOT / "batch_skill" / "README.md").exists():
        errors.append("batch_skill/README.md must not exist")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Root docs are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
