from pathlib import Path

import pytest


BATCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BATCH_ROOT.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_paper_reader_batch_docs_preserve_scheduler_boundary() -> None:
    combined = "\n".join(
        [
            read(BATCH_ROOT / "SKILL.md"),
            read(BATCH_ROOT / "references" / "batch-workflow.md"),
        ]
    )

    for phrase in [
        "$paper_reader",
        "zotero_write",
        "prepare_only",
        "Default Codex concurrency is 3",
        "Typical Use",
        "fallback pre-extraction",
        "prepare-local-pdfs",
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
        "PDF folder and PDF path items are local-only",
        "do not run Zotero lookup, duplicate checks, next-write, or Zotero write-through",
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
            "paper_reader/",
            "paper_reader_batch/",
            "paper_reader",
            "paper_reader_batch",
            "uv run paper_reader_batch --help",
            "Use `paper_reader`",
            "Use `paper_reader_batch`",
            "https://github.com/cookjohn/zotero-mcp#readme",
            "zotero-mcp-plugin",
            "zotero_write",
            "prepare_only",
        ]:
            assert phrase in text


def test_root_readmes_show_complete_batch_dispatch_loop() -> None:
    if not (REPO_ROOT / "README.md").exists():
        pytest.skip("root documentation is validated only in the source repository")

    english = read(REPO_ROOT / "README.md")
    chinese = read(REPO_ROOT / "README.zh-CN.md")

    for text in [english, chinese]:
        for phrase in [
            "uv run paper_reader_batch validate <batch_run_dir> --paper-reader-root /path/to/paper_reader",
            "uv run paper_reader_batch next <batch_run_dir> --limit 3",
            "uv run paper_reader_batch worker-prompt <batch_run_dir> <item_id>",
            "uv run paper_reader_batch record-result <batch_run_dir> <item_id> --result item-result.json",
            "uv run paper_reader_batch next-write <batch_run_dir> --limit 1",
            "uv run paper_reader_batch record-write <batch_run_dir> <item_id> --result write-result.json",
            "uv run paper_reader_batch report <batch_run_dir>",
        ]:
            assert phrase in text


def test_batch_validator_tracks_required_runtime_modules() -> None:
    validator = read(BATCH_ROOT / "scripts" / "validate-skill.py")

    for phrase in [
        "src/paper_reader_batch/io.py",
        "src/paper_reader_batch/manifest.py",
        "src/paper_reader_batch/runs.py",
        "src/paper_reader_batch/state.py",
        "src/paper_reader_batch/takeaway.py",
        "src/paper_reader_batch/report.py",
        "src/paper_reader_batch/local_prepare.py",
        "src/paper_reader_batch/worker_contract.py",
        "src/paper_reader_batch/cli.py",
        "references/batch-workflow.md",
        "references/parallel-dispatch.md",
        "references/worker-result-contract.md",
    ]:
        assert phrase in validator
