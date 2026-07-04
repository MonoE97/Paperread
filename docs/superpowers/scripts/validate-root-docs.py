#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
README_ZH = ROOT / "README.zh-CN.md"
AGENTS = ROOT / "AGENTS.md"
SKILL = ROOT / "paper_reader" / "SKILL.md"
BATCH_SKILL = ROOT / "paper_reader_batch" / "SKILL.md"


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
    paper_reader_batch = read(BATCH_SKILL)
    combined_public = "\n".join([english, chinese, agents, skill, paper_reader_batch])

    require(english, "[简体中文](README.zh-CN.md)", "README.md", errors)
    require(chinese, "[English](README.md)", "README.zh-CN.md", errors)

    for label, text in [
        ("README.md", english),
        ("README.zh-CN.md", chinese),
    ]:
        for phrase in [
            "paper_reader/",
            "paper_reader_batch/",
            "paper_reader",
            "paper_reader_batch",
            "uv --version",
            "uv sync --locked",
            "uv python install 3.13",
            "uv run paper_reader --help",
            "uv run paper_reader_batch --help",
            "Use `paper_reader`",
            "Use `paper_reader_batch`",
            "https://github.com/cookjohn/zotero-mcp#readme",
            "zotero-mcp-plugin",
            "Tools -> Add-ons",
            "http://127.0.0.1:23120/mcp",
            "test ! -e \"$install_dir\"",
            "cp -R /path/to/paper_reader/paper_reader \"$install_dir\"",
            "cp -R /path/to/paper_reader/paper_reader_batch \"$install_dir\"",
            "$HOME/.claude/skills/paper_reader",
            "$HOME/.claude/skills/paper_reader_batch",
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
        "paper_reader/pyproject.toml",
        "paper_reader/uv.lock",
        "paper_reader/src/paper_reader/",
        "paper_reader/tests/",
        "paper_reader/templates/",
        "paper_reader_batch/pyproject.toml",
        "paper_reader_batch/uv.lock",
        "paper_reader_batch/src/paper_reader_batch/",
        "paper_reader_batch/tests/",
        "paper_reader_batch/references/",
        "python docs/superpowers/scripts/validate-root-docs.py",
        "本地 `.pdf` path 和本地目录 path 优先于 Zotero title routing",
    ]:
        require(agents, phrase, "AGENTS.md", errors)

    for phrase in [
        "skill root",
        "uv --version",
        "uv sync --locked",
        "uv python install 3.13",
        "uv run paper_reader --help",
        "Typical Use",
        "references/pdf-path-workflow.md",
        "references/zotero-workflow.md",
        "https://github.com/cookjohn/zotero-mcp#readme",
        "Zotero MCP `write_note`",
        "Local PDF path and directory path inputs skip Zotero lookup and duplicate checks",
        "Existing local paths are not Zotero title fragments",
    ]:
        require(skill, phrase, "paper_reader/SKILL.md", errors)

    for phrase in [
        "$paper_reader",
        "uv --version",
        "uv sync --locked",
        "uv run paper_reader_batch --help",
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
        require(paper_reader_batch, phrase, "paper_reader_batch/SKILL.md", errors)

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

    if (ROOT / "paper_reader" / "README.md").exists():
        errors.append("paper_reader/README.md must not exist")
    if (ROOT / "paper_reader_batch" / "README.md").exists():
        errors.append("paper_reader_batch/README.md must not exist")

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
