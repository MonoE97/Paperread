from pathlib import Path

import pytest


BATCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BATCH_ROOT.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch_skill_docs_preserve_scheduler_boundary() -> None:
    combined = "\n".join(
        [
            read(BATCH_ROOT / "SKILL.md"),
            read(BATCH_ROOT / "references" / "batch-workflow.md"),
        ]
    )

    for phrase in [
        "$paperread",
        "zotero_write",
        "prepare_only",
        "Default Codex concurrency is 3",
        "Typical Use",
        "Claude-compatible fallback is sequential",
        "must not call",
        "write_note",
        "next-write",
        "record-write",
        "zotero-mcp-plugin",
        "http://127.0.0.1:23120/mcp",
        "30 秒结论",
        "tldr",
        "one_sentence_summary",
        "zotero_item",
        "zotero_title",
        "pdf_path",
        "runs/YYYY-MM-DD/<batch-slug>/",
        "collection.key",
        "worker_id",
        "attempt_count",
        "takeaway_source_sha256",
    ]:
        assert phrase in combined

    for forbidden in [
        "copies single-paper prompts",
        "automatic Zotero writing",
    ]:
        assert forbidden not in combined


def test_root_docs_describe_two_installable_skill_sources() -> None:
    if not (REPO_ROOT / "README.md").exists():
        pytest.skip("root documentation is validated only in the source repository")

    english = read(REPO_ROOT / "README.md")
    chinese = read(REPO_ROOT / "README.zh-CN.md")
    agents = read(REPO_ROOT / "AGENTS.md")

    for text in [english, chinese, agents]:
        for phrase in [
            "skill/",
            "batch_skill/",
            "paperread",
            "paperread-batch",
            "uv run paperread-batch --help",
            "Use `paperread`",
            "Use `paperread-batch`",
            "https://github.com/cookjohn/zotero-mcp#readme",
            "zotero-mcp-plugin",
            "zotero_write",
            "prepare_only",
        ]:
            assert phrase in text


def test_batch_validator_tracks_required_runtime_modules() -> None:
    validator = read(BATCH_ROOT / "scripts" / "validate-skill.py")

    for phrase in [
        "src/paperread_batch/io.py",
        "src/paperread_batch/manifest.py",
        "src/paperread_batch/runs.py",
        "src/paperread_batch/state.py",
        "src/paperread_batch/takeaway.py",
        "src/paperread_batch/report.py",
        "src/paperread_batch/cli.py",
        "references/batch-workflow.md",
    ]:
        assert phrase in validator
