from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, NoReturn

import typer
from typer import _click as click
from typer.core import TyperGroup

from paper_reader_batch import __version__
from paper_reader_batch.v2_contracts import COMMAND_RESULT_SCHEMA_VERSION, CommandError, CommandResult
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_manifest import (
    create_pdf_folder_manifest,
    create_pdf_paths_manifest,
    create_zotero_collection_manifest,
    create_zotero_titles_manifest,
    validate_manifest_file,
)
from paper_reader_batch.v2_receipts import RequestOutcome, validate_request_id
from paper_reader_batch.v2_run import initialize_run, recover_run, run_status, validate_run
from paper_reader_batch.v2_report import run_report as generate_report
from paper_reader_batch.v2_worker import (
    claim_worker,
    finish_worker,
    release_worker,
    renew_worker,
    retry_worker,
    worker_prompt,
)
from paper_reader_batch.v2_local_prepare import (
    MAX_CHILD_TIMEOUT_SECONDS,
    claim_local_prepare,
    finish_local_prepare,
    release_local_prepare,
    renew_local_prepare,
    run_local_prepare,
)
from paper_reader_batch.v2_write import (
    begin_write,
    claim_write,
    commit_write,
    mark_write_uncertain,
    preview_write,
    reconcile_write,
    release_write,
    renew_write,
    retry_write,
)


PUBLIC_GROUPS = frozenset({"manifest", "run", "worker", "local-prepare", "write"})


def _emit_result(result: CommandResult) -> None:
    click.echo(result.model_dump_json())


def emit_success(
    command: str,
    result: dict,
    *,
    request_id: str | None = None,
    replayed: bool = False,
) -> None:
    _emit_result(
        CommandResult(
            schema_version=COMMAND_RESULT_SCHEMA_VERSION,
            command=command,
            request_id=request_id,
            replayed=replayed,
            ok=True,
            result=result,
            error=None,
        )
    )


def emit_failure(
    command: str,
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    details: dict | None = None,
    exit_code: int = 2,
) -> NoReturn:
    _emit_result(
        CommandResult(
            schema_version=COMMAND_RESULT_SCHEMA_VERSION,
            command=command,
            request_id=request_id,
            replayed=False,
            ok=False,
            result=None,
            error=CommandError(code=code, message=message, details=details or {}),
        )
    )
    raise typer.Exit(exit_code)


def _batch_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _error_request_id(request_id: str | None) -> str | None:
    if request_id is None:
        return None
    try:
        return validate_request_id(request_id)
    except BatchRuntimeError:
        return None


def _command_from_args(raw_args: list[str]) -> str:
    command_tokens = [token for token in raw_args if not token.startswith("-")]
    if command_tokens and command_tokens[0] in PUBLIC_GROUPS:
        return ".".join(command_tokens[:2]) if len(command_tokens) > 1 else command_tokens[0]
    return command_tokens[0] if command_tokens else "cli"


def _request_id_from_args(raw_args: list[str]) -> str | None:
    try:
        index = raw_args.index("--request-id") + 1
    except ValueError:
        return None
    if index >= len(raw_args):
        return None
    return _error_request_id(raw_args[index])


def _run_mutation(command: str, request_id: str, operation: Callable[[], RequestOutcome]) -> None:
    try:
        validated_request_id = validate_request_id(request_id)
    except BatchRuntimeError as exc:
        emit_failure(command, exc.code, exc.message, details=exc.details)
    try:
        outcome = operation()
    except BatchRuntimeError as exc:
        emit_failure(
            command,
            exc.code,
            exc.message,
            request_id=validated_request_id,
            details=exc.details,
        )
    emit_success(command, outcome.result, request_id=validated_request_id, replayed=outcome.replayed)


def _run_read(command: str, operation: Callable[[], dict]) -> None:
    try:
        result = operation()
    except BatchRuntimeError as exc:
        emit_failure(command, exc.code, exc.message, details=exc.details)
    emit_success(command, result)


class V2TopLevelGroup(TyperGroup):
    def main(
        self,
        args: list[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra,
    ):
        raw_args = list(sys.argv[1:] if args is None else args)
        try:
            outcome = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
            if isinstance(outcome, int) and outcome != 0:
                raise SystemExit(outcome)
            return outcome
        except click.exceptions.UsageError as exc:
            result = CommandResult(
                schema_version=COMMAND_RESULT_SCHEMA_VERSION,
                command=_command_from_args(raw_args),
                request_id=_request_id_from_args(raw_args),
                replayed=False,
                ok=False,
                result=None,
                error=CommandError(code="invalid_cli_usage", message=str(exc), details={}),
            )
            click.echo(result.model_dump_json())
            click.echo(f"invalid_cli_usage: {exc}", err=True)
            raise SystemExit(exc.exit_code) from None
        except Exception as exc:
            result = CommandResult(
                schema_version=COMMAND_RESULT_SCHEMA_VERSION,
                command=_command_from_args(raw_args),
                request_id=_request_id_from_args(raw_args),
                replayed=False,
                ok=False,
                result=None,
                error=CommandError(
                    code="internal_error",
                    message="unexpected internal error",
                    details={"exception_type": type(exc).__name__},
                ),
            )
            click.echo(result.model_dump_json())
            click.echo(f"internal_error: {type(exc).__name__}", err=True)
            raise SystemExit(1) from None


app = typer.Typer(
    cls=V2TopLevelGroup,
    help="Paper Reader Batch 2.1 journal runtime.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    suggest_commands=False,
)
manifest_app = typer.Typer(help="Manifest operations.", no_args_is_help=True, add_completion=False, rich_markup_mode=None)
run_app = typer.Typer(help="Run operations.", no_args_is_help=True, add_completion=False, rich_markup_mode=None)
worker_app = typer.Typer(help="Worker operations.", no_args_is_help=True, add_completion=False, rich_markup_mode=None)
local_prepare_app = typer.Typer(
    help="Local preparation operations.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
write_app = typer.Typer(
    help="Recoverable Zotero write coordination.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
app.add_typer(manifest_app, name="manifest")
app.add_typer(run_app, name="run")
app.add_typer(worker_app, name="worker")
app.add_typer(local_prepare_app, name="local-prepare")
app.add_typer(write_app, name="write")


def _version_callback(value: bool) -> bool:
    if value:
        click.echo(f"paper_reader_batch {__version__}")
        raise typer.Exit()
    return value


@app.callback()
def root_command(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the package version and exit.",
    ),
) -> None:
    del version


@manifest_app.command("from-pdf-folder")
def manifest_from_pdf_folder_command(
    folder: Path,
    batch_title: str = typer.Option(..., "--batch-title"),
    output: Path = typer.Option(..., "--output", "-o"),
    request_id: str = typer.Option(..., "--request-id"),
    recursive: bool = typer.Option(False, "--recursive"),
    default_concurrency: int = typer.Option(3, "--default-concurrency", min=1, max=32),
    write_policy: str = typer.Option("zotero_write", "--write-policy"),
) -> None:
    _run_mutation(
        "manifest.from-pdf-folder",
        request_id,
        lambda: create_pdf_folder_manifest(
            folder,
            batch_title=batch_title,
            output=output,
            request_id=request_id,
            skill_root=_batch_root(),
            recursive=recursive,
            default_concurrency=default_concurrency,
            write_policy=write_policy,
        ),
    )


@manifest_app.command("from-pdf-paths")
def manifest_from_pdf_paths_command(
    paths_file: Path,
    batch_title: str = typer.Option(..., "--batch-title"),
    output: Path = typer.Option(..., "--output", "-o"),
    request_id: str = typer.Option(..., "--request-id"),
    default_concurrency: int = typer.Option(3, "--default-concurrency", min=1, max=32),
    write_policy: str = typer.Option("zotero_write", "--write-policy"),
) -> None:
    _run_mutation(
        "manifest.from-pdf-paths",
        request_id,
        lambda: create_pdf_paths_manifest(
            paths_file,
            batch_title=batch_title,
            output=output,
            request_id=request_id,
            skill_root=_batch_root(),
            default_concurrency=default_concurrency,
            write_policy=write_policy,
        ),
    )


@manifest_app.command("from-zotero-titles")
def manifest_from_zotero_titles_command(
    titles_file: Path,
    batch_title: str = typer.Option(..., "--batch-title"),
    output: Path = typer.Option(..., "--output", "-o"),
    request_id: str = typer.Option(..., "--request-id"),
    default_concurrency: int = typer.Option(3, "--default-concurrency", min=1, max=32),
    write_policy: str = typer.Option("zotero_write", "--write-policy"),
) -> None:
    _run_mutation(
        "manifest.from-zotero-titles",
        request_id,
        lambda: create_zotero_titles_manifest(
            titles_file,
            batch_title=batch_title,
            output=output,
            request_id=request_id,
            skill_root=_batch_root(),
            default_concurrency=default_concurrency,
            write_policy=write_policy,
        ),
    )


@manifest_app.command("from-zotero-collection")
def manifest_from_zotero_collection_command(
    collection: str,
    inventory: Path = typer.Option(..., "--inventory"),
    batch_title: str = typer.Option(..., "--batch-title"),
    output: Path = typer.Option(..., "--output", "-o"),
    request_id: str = typer.Option(..., "--request-id"),
    default_concurrency: int = typer.Option(3, "--default-concurrency", min=1, max=32),
    write_policy: str = typer.Option("zotero_write", "--write-policy"),
) -> None:
    _run_mutation(
        "manifest.from-zotero-collection",
        request_id,
        lambda: create_zotero_collection_manifest(
            collection,
            inventory,
            batch_title=batch_title,
            output=output,
            request_id=request_id,
            skill_root=_batch_root(),
            default_concurrency=default_concurrency,
            write_policy=write_policy,
        ),
    )


@manifest_app.command("validate")
def manifest_validate_command(manifest: Path) -> None:
    _run_read("manifest.validate", lambda: validate_manifest_file(manifest))


@run_app.command("init")
def run_init_command(
    manifest: Path = typer.Option(..., "--manifest"),
    request_id: str = typer.Option(..., "--request-id"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    _run_mutation(
        "run.init",
        request_id,
        lambda: initialize_run(
            manifest,
            request_id=request_id,
            skill_root=_batch_root(),
            output=output,
        ),
    )


@run_app.command("validate")
def run_validate_command(run_dir: Path) -> None:
    _run_read("run.validate", lambda: validate_run(run_dir))


@run_app.command("status")
def run_status_command(run_dir: Path) -> None:
    _run_read("run.status", lambda: run_status(run_dir))


@run_app.command("recover")
def run_recover_command(
    run_dir: Path,
    request_id: str = typer.Option(..., "--request-id"),
    paper_reader_root: Path | None = typer.Option(None, "--paper-reader-root"),
    reconciliation_timeout_seconds: int = typer.Option(
        60,
        "--reconciliation-timeout-seconds",
        min=1,
        max=600,
    ),
) -> None:
    _run_mutation(
        "run.recover",
        request_id,
        lambda: recover_run(
            run_dir,
            request_id=request_id,
            paper_reader_root=paper_reader_root,
            reconciliation_timeout_seconds=reconciliation_timeout_seconds,
        ),
    )


@run_app.command("report")
def run_report_command(run_dir: Path) -> None:
    _run_read("run.report", lambda: generate_report(run_dir))


@worker_app.command("claim")
def worker_claim_command(
    run_dir: Path,
    worker_id: str = typer.Option(..., "--worker-id"),
    request_id: str = typer.Option(..., "--request-id"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    lease_seconds: int = typer.Option(900, "--lease-seconds", min=1, max=3600),
) -> None:
    _run_mutation(
        "worker.claim",
        request_id,
        lambda: claim_worker(
            run_dir,
            worker_id=worker_id,
            request_id=request_id,
            limit=limit,
            lease_seconds=lease_seconds,
        ),
    )


@worker_app.command("prompt")
def worker_prompt_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
) -> None:
    _run_read(
        "worker.prompt",
        lambda: worker_prompt(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
        ),
    )


@worker_app.command("renew")
def worker_renew_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    request_id: str = typer.Option(..., "--request-id"),
    lease_seconds: int = typer.Option(900, "--lease-seconds", min=1, max=3600),
) -> None:
    _run_mutation(
        "worker.renew",
        request_id,
        lambda: renew_worker(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            request_id=request_id,
            lease_seconds=lease_seconds,
        ),
    )


@worker_app.command("finish")
def worker_finish_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    result: Path = typer.Option(..., "--result"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "worker.finish",
        request_id,
        lambda: finish_worker(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            result_path=result,
            request_id=request_id,
        ),
    )


@worker_app.command("release")
def worker_release_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    acknowledge_no_side_effects: bool = typer.Option(False, "--acknowledge-no-side-effects"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "worker.release",
        request_id,
        lambda: release_worker(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            acknowledge_no_side_effects=acknowledge_no_side_effects,
            request_id=request_id,
        ),
    )


@worker_app.command("retry")
def worker_retry_command(
    run_dir: Path,
    item_id: str,
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "worker.retry",
        request_id,
        lambda: retry_worker(run_dir, item_id, request_id=request_id),
    )


@local_prepare_app.command("claim")
def local_prepare_claim_command(
    run_dir: Path,
    worker_id: str = typer.Option(..., "--worker-id"),
    request_id: str = typer.Option(..., "--request-id"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    lease_seconds: int = typer.Option(900, "--lease-seconds", min=1, max=3600),
) -> None:
    _run_mutation(
        "local-prepare.claim",
        request_id,
        lambda: claim_local_prepare(
            run_dir,
            worker_id=worker_id,
            request_id=request_id,
            limit=limit,
            lease_seconds=lease_seconds,
        ),
    )


@local_prepare_app.command("renew")
def local_prepare_renew_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    request_id: str = typer.Option(..., "--request-id"),
    lease_seconds: int = typer.Option(900, "--lease-seconds", min=1, max=3600),
) -> None:
    _run_mutation(
        "local-prepare.renew",
        request_id,
        lambda: renew_local_prepare(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            request_id=request_id,
            lease_seconds=lease_seconds,
        ),
    )


@local_prepare_app.command("finish")
def local_prepare_finish_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    result: Path = typer.Option(..., "--result"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "local-prepare.finish",
        request_id,
        lambda: finish_local_prepare(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            result_path=result,
            request_id=request_id,
        ),
    )


@local_prepare_app.command("release")
def local_prepare_release_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    acknowledge_no_side_effects: bool = typer.Option(False, "--acknowledge-no-side-effects"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "local-prepare.release",
        request_id,
        lambda: release_local_prepare(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            acknowledge_no_side_effects=acknowledge_no_side_effects,
            request_id=request_id,
        ),
    )


@local_prepare_app.command("run")
def local_prepare_run_command(
    run_dir: Path,
    item_id: str,
    worker_id: str = typer.Option(..., "--worker-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    paper_reader_root: Path = typer.Option(..., "--paper-reader-root"),
    request_id: str = typer.Option(..., "--request-id"),
    timeout_seconds: int = typer.Option(
        600,
        "--timeout-seconds",
        min=1,
        max=MAX_CHILD_TIMEOUT_SECONDS,
    ),
) -> None:
    _run_mutation(
        "local-prepare.run",
        request_id,
        lambda: run_local_prepare(
            run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            paper_reader_root=paper_reader_root,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        ),
    )


@write_app.command("claim")
def write_claim_command(
    run_dir: Path,
    writer_id: str = typer.Option(..., "--writer-id"),
    request_id: str = typer.Option(..., "--request-id"),
    lease_seconds: int = typer.Option(120, "--lease-seconds", min=1, max=300),
) -> None:
    _run_mutation(
        "write.claim",
        request_id,
        lambda: claim_write(
            run_dir,
            writer_id=writer_id,
            request_id=request_id,
            lease_seconds=lease_seconds,
        ),
    )


@write_app.command("preview")
def write_preview_command(
    run_dir: Path,
    item_id: str,
    writer_id: str = typer.Option(..., "--writer-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    write_attempt_id: str = typer.Option(..., "--write-attempt-id"),
) -> None:
    _run_read(
        "write.preview",
        lambda: preview_write(
            run_dir,
            item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
        ),
    )


@write_app.command("renew")
def write_renew_command(
    run_dir: Path,
    item_id: str,
    writer_id: str = typer.Option(..., "--writer-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    write_attempt_id: str = typer.Option(..., "--write-attempt-id"),
    request_id: str = typer.Option(..., "--request-id"),
    lease_seconds: int = typer.Option(120, "--lease-seconds", min=1, max=300),
) -> None:
    _run_mutation(
        "write.renew",
        request_id,
        lambda: renew_write(
            run_dir,
            item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            request_id=request_id,
            lease_seconds=lease_seconds,
        ),
    )


@write_app.command("release")
def write_release_command(
    run_dir: Path,
    item_id: str,
    writer_id: str = typer.Option(..., "--writer-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    write_attempt_id: str = typer.Option(..., "--write-attempt-id"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "write.release",
        request_id,
        lambda: release_write(
            run_dir,
            item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            request_id=request_id,
        ),
    )


@write_app.command("begin")
def write_begin_command(
    run_dir: Path,
    item_id: str,
    writer_id: str = typer.Option(..., "--writer-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    write_attempt_id: str = typer.Option(..., "--write-attempt-id"),
    authorization: Path = typer.Option(..., "--authorization"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "write.begin",
        request_id,
        lambda: begin_write(
            run_dir,
            item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            authorization_path=authorization,
            request_id=request_id,
        ),
    )


@write_app.command("commit")
def write_commit_command(
    run_dir: Path,
    item_id: str,
    writer_id: str = typer.Option(..., "--writer-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    write_attempt_id: str = typer.Option(..., "--write-attempt-id"),
    result: Path = typer.Option(..., "--result"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "write.commit",
        request_id,
        lambda: commit_write(
            run_dir,
            item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            result_path=result,
            request_id=request_id,
        ),
    )


@write_app.command("mark-uncertain")
def write_mark_uncertain_command(
    run_dir: Path,
    item_id: str,
    writer_id: str = typer.Option(..., "--writer-id"),
    claim_id: str = typer.Option(..., "--claim-id"),
    lease_token: str = typer.Option(..., "--lease-token"),
    write_attempt_id: str = typer.Option(..., "--write-attempt-id"),
    reason: str = typer.Option(..., "--reason"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "write.mark-uncertain",
        request_id,
        lambda: mark_write_uncertain(
            run_dir,
            item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            reason=reason,
            request_id=request_id,
        ),
    )


@write_app.command("reconcile")
def write_reconcile_command(
    run_dir: Path,
    item_id: str,
    readback: Path = typer.Option(..., "--readback"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "write.reconcile",
        request_id,
        lambda: reconcile_write(
            run_dir,
            item_id,
            readback_path=readback,
            request_id=request_id,
        ),
    )


@write_app.command("retry")
def write_retry_command(
    run_dir: Path,
    item_id: str,
    acknowledge_no_match: bool = typer.Option(False, "--acknowledge-no-match"),
    request_id: str = typer.Option(..., "--request-id"),
) -> None:
    _run_mutation(
        "write.retry",
        request_id,
        lambda: retry_write(
            run_dir,
            item_id,
            acknowledge_no_match=acknowledge_no_match,
            request_id=request_id,
        ),
    )
