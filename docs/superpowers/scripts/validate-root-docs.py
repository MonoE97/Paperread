#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
README_ZH = ROOT / "README.zh-CN.md"
AGENTS = ROOT / "AGENTS.md"
SKILL = ROOT / "skill" / "SKILL.md"


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
    combined_public = "\n".join([english, chinese, agents, skill])

    require(english, "[简体中文](README.zh-CN.md)", "README.md", errors)
    require(chinese, "[English](README.md)", "README.zh-CN.md", errors)

    for label, text in [
        ("README.md", english),
        ("README.zh-CN.md", chinese),
    ]:
        for phrase in [
            "skill/",
            "paperread",
            "uv sync --locked",
            "uv run paperread --help",
            "test ! -e \"$install_dir\"",
            "cp -R /path/to/Paperread/skill \"$install_dir\"",
            "$HOME/.claude/skills/paperread",
            "write_note",
            "refresh-live-notes",
            "write-payload.json",
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
    ]:
        require(english, phrase, "README.md", errors)

    for phrase in [
        "Zotero MCP `write_note`",
        "Zotero local API",
        "SQLite",
        "只能输出本地文件",
    ]:
        require(chinese, phrase, "README.zh-CN.md", errors)

    for phrase in [
        "skill/pyproject.toml",
        "skill/uv.lock",
        "skill/src/paperread/",
        "skill/tests/",
        "skill/templates/",
        "python docs/superpowers/scripts/validate-root-docs.py",
    ]:
        require(agents, phrase, "AGENTS.md", errors)

    for phrase in [
        "skill root",
        "uv sync --locked",
        "references/pdf-path-workflow.md",
        "references/zotero-workflow.md",
        "Zotero MCP `write_note`",
    ]:
        require(skill, phrase, "skill/SKILL.md", errors)

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
