from pathlib import Path

import pytest


BATCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BATCH_ROOT.parent
SKILL = BATCH_ROOT / "SKILL.md"
OPENAI_YAML = BATCH_ROOT / "agents" / "openai.yaml"
BATCH_WORKFLOW = BATCH_ROOT / "references" / "batch-workflow.md"
PARALLEL_DISPATCH = BATCH_ROOT / "references" / "parallel-dispatch.md"
WORKER_RESULT_CONTRACT = BATCH_ROOT / "references" / "worker-result-contract.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch_skill_declares_grouped_routing_and_safety() -> None:
    text = read(SKILL)
    for phrase in [
        "Paper Reader Batch 2.0 runtime contract",
        "grouped CLI",
        "$paper_reader",
        "zotero_write",
        "prepare_only",
        "Default Codex concurrency is 3",
        "Typical Use",
        "fallback pre-extraction",
        "must not call",
        "write_note",
        "uv run paper_reader_batch manifest",
        "uv run paper_reader_batch run init",
        "uv run paper_reader_batch run validate",
        "uv run paper_reader_batch run status",
        "uv run paper_reader_batch run recover",
        "uv run paper_reader_batch run report",
        "uv run paper_reader_batch worker claim",
        "uv run paper_reader_batch worker prompt",
        "uv run paper_reader_batch worker renew",
        "uv run paper_reader_batch worker finish",
        "uv run paper_reader_batch worker release",
        "uv run paper_reader_batch worker retry",
        "uv run paper_reader_batch local-prepare claim",
        "uv run paper_reader_batch local-prepare run",
        "uv run paper_reader_batch write claim",
        "uv run paper_reader_batch write preview",
        "uv run paper_reader_batch write renew",
        "uv run paper_reader_batch write release",
        "uv run paper_reader_batch write begin",
        "uv run paper_reader_batch write commit",
        "uv run paper_reader_batch write mark-uncertain",
        "uv run paper_reader_batch write reconcile",
        "uv run paper_reader_batch write retry",
        "explicit real-write intent",
        "Chinese-first",
        "external agent",
        "paper_reader_batch.command-result.v2",
        "unsupported_run_schema",
        "historical-only",
        "An exact parent + title + canonical HTML hash match locates one note but does not verify it",
        "only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash",
    ]:
        assert phrase in text


def test_batch_workflow_declares_journal_leases_and_write_sequence() -> None:
    text = read(BATCH_WORKFLOW)
    for phrase in [
        "zotero-mcp-plugin",
        "http://127.0.0.1:23120/mcp",
        "30 秒结论",
        "tldr",
        "one_sentence_summary",
        "zotero_item",
        "zotero_title",
        "pdf_path",
        "PDF folder and PDF path items are local-only",
        "do not run Zotero lookup, duplicate checks, or Zotero write-through",
        "runs/YYYY-MM-DD/<batch-slug>/",
        "collection.key",
        "events/<20-digit-seq>.json",
        "append-only hash-chain",
        "state.json",
        "reconstructable snapshot",
        ".run.lock",
        "--request-id UUID",
        "900 seconds",
        "120 seconds",
        "30 seconds",
        "lease token",
        "stale lease",
        "same-PDF mutual exclusion",
        "journal_corrupt",
        "skill-root request receipt",
        "global no-follow allocation lock",
        "write.started",
        "uncertain",
        "never queued",
        "takeaway_source_sha256",
        "worker renew <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "worker prompt <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "worker finish <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "worker release <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "local-prepare renew <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "local-prepare finish <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "local-prepare release <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "local-prepare run <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --paper-reader-root <paper_reader_root>",
        "--acknowledge-no-side-effects",
        "write preview <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "write renew <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "write release <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "write begin <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --authorization <authorization.json>",
        "write commit <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "write mark-uncertain <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "write reconcile <batch_run_dir> <item_id> --readback <readback.json> --request-id UUID",
        "write preview shows only the immutable candidate",
        "explicit real-write intent",
        "external claim id",
        "candidate digest",
        "write_attempt_id",
        "does not bind lease_token",
        "write begin independently validates the current claim_id, lease_token, and write_attempt_id",
        "write events and results bind claim_id, lease_token, and write_attempt_id",
        "Chinese-first",
        "unsupported_run_schema",
        "An exact parent + title + canonical HTML hash match locates one note but does not verify it",
        "only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash",
    ]:
        assert phrase in text

    assert "One match commits written" not in text


def test_parallel_dispatch_declares_claim_bound_authorization_handoff() -> None:
    text = read(PARALLEL_DISPATCH)
    for phrase in [
        "claim id",
        "lease token",
        "exact attempt",
        "write claim",
        "write preview",
        "explicit real-write intent",
        "$paper_reader zotero authorize",
        "external claim id",
        "write begin",
        "uv run paper_reader_batch worker claim <batch_run> --worker-id <worker_id> --request-id UUID",
        "uv run paper_reader_batch worker prompt <batch_run> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "uv run paper_reader_batch worker renew <batch_run> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "uv run paper_reader_batch local-prepare claim <batch_run> --worker-id <worker_id> --request-id UUID",
        "uv run paper_reader_batch local-prepare renew <batch_run> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "uv run paper_reader_batch local-prepare finish <batch_run> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "uv run paper_reader_batch local-prepare release <batch_run> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>",
        "uv run paper_reader_batch local-prepare run <batch_run> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --paper-reader-root <paper_reader_root>",
        "uv run paper_reader_batch write claim <batch_run> --writer-id <writer_id> --request-id UUID",
        "uv run paper_reader_batch write preview <batch_run> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "uv run paper_reader_batch write renew <batch_run> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "uv run paper_reader_batch write release <batch_run> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "uv run paper_reader_batch write commit <batch_run> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "uv run paper_reader_batch write mark-uncertain <batch_run> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>",
        "authorization binds the external claim id, candidate digest, and write_attempt_id",
        "authorization does not bind the lease token",
        "write begin independently validates the current claim id, lease token, and write attempt id",
        "Chinese-first",
        "uncertain, never queued",
        "An exact parent + title + canonical HTML hash match locates one note but does not verify it",
    ]:
        assert phrase in text


def test_batch_authorization_requires_paired_external_identity() -> None:
    for path in [SKILL, BATCH_WORKFLOW, PARALLEL_DISPATCH]:
        text = read(path)
        for phrase in [
            "Batch authorization requires both --external-claim-id and --write-attempt-id",
            "both options must appear together",
            "must not generate `direct_<uuid>` identities",
        ]:
            assert phrase in text


def test_started_write_lease_expiry_uses_idempotent_recover() -> None:
    for path in [SKILL, BATCH_WORKFLOW, PARALLEL_DISPATCH]:
        text = read(path)
        for phrase in [
            "write.lease_expired_uncertain",
            "`run recover` holds `.run.lock`",
            "expired lease token is neither required nor accepted",
            "same recover request id replays idempotently",
            "never returns queued and cannot begin again",
            "`write mark-uncertain` accepts only an unexpired exact claim/token/write-attempt identity",
        ]:
            assert phrase in text

    assert "Any crash/error/expiry after `write.started` uses" not in read(PARALLEL_DISPATCH)


def test_recover_documents_read_only_single_reader_reconciliation() -> None:
    for path in [SKILL, BATCH_WORKFLOW, PARALLEL_DISPATCH]:
        text = read(path)
        for phrase in [
            "--paper-reader-root",
            "uv run --locked paper_reader zotero reconcile",
            "read-only",
            "written",
            "retry_confirmation_required",
            "blocked",
        ]:
            assert phrase in text

    workflow = read(BATCH_WORKFLOW)
    assert "does not import `paper_reader`" in workflow
    assert "cannot call `write_note`" in workflow


def test_batch_docs_exclude_active_v1_commands() -> None:
    for path in [SKILL, BATCH_WORKFLOW, PARALLEL_DISPATCH, WORKER_RESULT_CONTRACT, OPENAI_YAML]:
        text = read(path)
        for forbidden in [
            "copies single-paper prompts",
            "automatic Zotero writing",
            "uv run paper_reader_batch next ",
            "uv run paper_reader_batch next-write",
            "uv run paper_reader_batch record-result",
            "uv run paper_reader_batch record-write",
            "uv run paper_reader_batch prepare-local-pdfs",
            "uv run paper_reader_batch local-prepare retry",
        ]:
            assert forbidden not in text


def test_openai_metadata_marks_v2_as_released_runtime() -> None:
    text = read(OPENAI_YAML)
    for phrase in [
        'display_name: "paper_reader_batch"',
        "Paper Reader Batch 2.0 released grouped CLI runtime",
        "local-only",
        "external agent",
        "write begin",
        "write.started",
        "returned MCP write_note envelope at most once",
        "historical-only",
        "allow_implicit_invocation: true",
    ]:
        assert phrase in text

    assert "staged" not in text
    assert "without treating this metadata as proof" not in text


def test_batch_v2_schema_contract_is_exhaustive() -> None:
    text = read(WORKER_RESULT_CONTRACT)

    for phrase in [
        "paper_reader_batch.manifest.v2",
        "paper_reader_batch.state.v2",
        "paper_reader_batch.event.v2",
        "paper_reader_batch.worker-result.v2",
        "paper_reader_batch.local-prepare-result.v2",
        "paper_reader_batch.write-result.v2",
        "paper_reader_batch.reconciliation.v2",
        "paper_reader_batch.report.v2",
        "paper_reader_batch.command-result.v2",
        "extra=forbid",
    ]:
        assert phrase in text

    for phrase in [
        "Chinese-first",
        "sealed review",
        "candidate",
        "lease token",
        "write_attempt_id",
        "An exact parent + title + canonical HTML hash match locates one note but does not verify it",
        "only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash",
    ]:
        assert phrase in text

    assert "one exact parent + title + hash match may become written" not in text

    for stale_schema in [
        "paper_reader_batch.manifest.v1",
        "paper_reader_batch.state.v1",
        "paper_reader_batch.item-result.v1",
        "paper_reader_batch.local-prepare-result.v1",
        "paper_reader_batch.write-result.v1",
    ]:
        assert stale_schema not in text


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


def test_root_readmes_publish_v2_release_and_clean_install() -> None:
    if not (REPO_ROOT / "README.md").exists():
        pytest.skip("root documentation is validated only in the source repository")

    english = read(REPO_ROOT / "README.md")
    chinese = read(REPO_ROOT / "README.zh-CN.md")

    for text in [english, chinese]:
        for phrase in [
            "Paper Reader 2.0",
            "2.0.0",
            "clean install",
            "uv sync --locked",
            "uv run paper_reader --version",
            "uv run paper_reader_batch --version",
            "unsupported_run_schema",
        ]:
            assert phrase in text

        for phrase in [
            "paper_reader zotero authorize <candidate.json> --external-claim-id <claim_id> --write-attempt-id <write_attempt_id>",
            "paper_reader_batch write begin",
            "write.started",
            "paper_reader zotero verify",
            "paper_reader_batch write commit",
            "recovery",
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
