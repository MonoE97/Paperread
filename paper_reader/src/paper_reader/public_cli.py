from __future__ import annotations

import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Sequence

import click
import typer
from typer.core import TyperGroup

from paper_reader import __version__
from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.routing import RoutingError, route_input
from paper_reader.storage import canonical_json_bytes, rfc3339_utc
from paper_reader.v2_loader import RunLoadError, load_v2_run


_RESULT_EMITTED: ContextVar[bool] = ContextVar(
    "paper_reader_result_emitted",
    default=False,
)
_PUBLIC_GROUPS = frozenset(
    {"run", "review", "candidate", "local", "zotero", "maintenance"}
)


def _command_from_args(raw_args: Sequence[str]) -> str:
    command_tokens = [token for token in raw_args if not token.startswith("-")]
    if command_tokens and command_tokens[0] in _PUBLIC_GROUPS:
        if len(command_tokens) > 1:
            return " ".join(command_tokens[:2])
        return command_tokens[0]
    return command_tokens[0] if command_tokens else "paper_reader"


class StructuredTyperGroup(TyperGroup):
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        raw_args = list(sys.argv[1:] if args is None else args)
        result_token = _RESULT_EMITTED.set(False)
        try:
            try:
                outcome = super().main(
                    args=args,
                    prog_name=prog_name,
                    complete_var=complete_var,
                    standalone_mode=False,
                    windows_expand_args=windows_expand_args,
                    **extra,
                )
            except click.UsageError as exc:
                if isinstance(exc, click.exceptions.NoArgsIsHelpError):
                    if standalone_mode:
                        raise SystemExit(exc.exit_code) from None
                    return exc.exit_code
                command_path = exc.ctx.command_path if exc.ctx is not None else "paper_reader"
                path_parts = command_path.split()
                command = " ".join(path_parts[1:]) if len(path_parts) > 1 else "paper_reader"
                message = exc.format_message()
                _write_result(
                    command=command,
                    ok=False,
                    code="invalid_command_usage",
                    data={"error_type": type(exc).__name__},
                    message=message,
                )
                typer.echo(f"{command}: {message}", err=True)
                if standalone_mode:
                    raise SystemExit(exc.exit_code) from None
                return exc.exit_code
            except Exception as exc:
                command = _command_from_args(raw_args)
                if not _RESULT_EMITTED.get():
                    _write_result(
                        command=command,
                        ok=False,
                        code="internal_error",
                        data={"error_type": type(exc).__name__},
                        message="unexpected internal error",
                    )
                typer.echo(f"{command}: internal_error ({type(exc).__name__})", err=True)
                if standalone_mode:
                    raise SystemExit(1) from None
                return 1

            if standalone_mode:
                exit_code = outcome if isinstance(outcome, int) else 0
                raise SystemExit(exit_code)
            return outcome
        finally:
            _RESULT_EMITTED.reset(result_token)


app = typer.Typer(
    help="Paper Reader V2 grouped CLI for local PDF and Zotero-title workflows.",
    no_args_is_help=True,
    add_completion=False,
    cls=StructuredTyperGroup,
)
run_app = typer.Typer(
    help="Initialize, prepare, inspect, and validate V2 runs.",
    no_args_is_help=True,
    add_completion=False,
)
review_app = typer.Typer(
    help="Validate and seal immutable review packages.",
    no_args_is_help=True,
    add_completion=False,
)
candidate_app = typer.Typer(
    help="Build immutable publication candidates.",
    no_args_is_help=True,
    add_completion=False,
)
local_app = typer.Typer(
    help="Publish fixed local candidates without replacement.",
    no_args_is_help=True,
    add_completion=False,
)
zotero_app = typer.Typer(
    help="Authorize and verify external Zotero writes.",
    no_args_is_help=True,
    add_completion=False,
)
maintenance_app = typer.Typer(
    help="Pure utilities outside the V2 lifecycle.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(run_app, name="run")
app.add_typer(review_app, name="review")
app.add_typer(candidate_app, name="candidate")
app.add_typer(local_app, name="local")
app.add_typer(zotero_app, name="zotero")
app.add_typer(maintenance_app, name="maintenance")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the package version and exit.",
    ),
) -> None:
    """Expose only the breaking V2 public command surface."""


def _finish(
    command: str,
    *,
    ok: bool,
    code: str,
    data: dict | None = None,
    message: str | None = None,
    diagnostic: str | None = None,
    exit_code: int = 1,
) -> None:
    _write_result(
        command=command,
        ok=ok,
        code=code,
        data=data,
        message=message,
    )
    if diagnostic:
        typer.echo(diagnostic, err=True)
    if not ok:
        raise typer.Exit(exit_code)


def _write_result(
    *,
    command: str,
    ok: bool,
    code: str,
    data: dict | None = None,
    message: str | None = None,
) -> None:
    result = PaperReaderCommandResult(
        schema_version="paper_reader.command-result.v2",
        command=command,
        ok=ok,
        code=code,
        created_at=rfc3339_utc(),
        message=message,
        data=data or {},
    )
    typer.echo(canonical_json_bytes(result).decode("utf-8"))
    _RESULT_EMITTED.set(True)


def _not_implemented(command: str, **data: str | int | None) -> None:
    message = f"{command} is reserved by the V2 public contract but is not implemented yet"
    _finish(
        command,
        ok=False,
        code="not_implemented",
        data={key: value for key, value in data.items() if value is not None},
        message=message,
        diagnostic=message,
    )


@app.command("route")
def route_command(input_value: str = typer.Argument(..., metavar="INPUT")) -> None:
    """Route an existing local path before considering a Zotero title query."""
    try:
        decision = route_input(input_value)
    except RoutingError as exc:
        _finish(
            "route",
            ok=False,
            code=exc.code,
            data={"input": exc.raw_input, "route": "unsupported_local_path"},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish("route", ok=True, code="ok", data=decision.as_dict())


@run_app.command("init-local")
def run_init_local(source_pdf: Path) -> None:
    """Initialize a local-PDF V2 run beside its resolved source."""
    from paper_reader.local_lifecycle import LocalLifecycleError, initialize_local_run

    try:
        initialized = initialize_local_run(source_pdf)
    except LocalLifecycleError as exc:
        _finish(
            "run init-local",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish(
        "run init-local",
        ok=True,
        code="initialized",
        data={
            "run_dir": str(initialized.run_dir),
            "run_id": initialized.run.run_id,
            "target_path": str(initialized.target_path),
        },
    )


@run_app.command("init-zotero")
def run_init_zotero(
    raw_mcp_response: Path = typer.Option(..., "--raw-mcp-response"),
    expected_item_key: str = typer.Option(..., "--expected-item-key"),
) -> None:
    """Initialize a Zotero V2 run from a saved raw discovery bundle."""
    from paper_reader.zotero_lifecycle import (
        ZoteroLifecycleError,
        initialize_zotero_run,
    )

    try:
        initialized = initialize_zotero_run(
            raw_mcp_response,
            expected_item_key=expected_item_key,
        )
    except ZoteroLifecycleError as exc:
        _finish(
            "run init-zotero",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish(
        "run init-zotero",
        ok=True,
        code="initialized",
        data={
            "run_dir": str(initialized.run_dir),
            "run_id": initialized.run.run_id,
            "item_key": initialized.run.source.item_key,
            "title": initialized.run.source.title,
        },
    )


@run_app.command("prepare")
def run_prepare(
    run_path: Path,
    preview_pages: int | None = typer.Option(None, "--preview-pages", min=1),
    figure_limit: int | None = typer.Option(None, "--figure-limit", min=0),
) -> None:
    """Prepare immutable evidence for a V2 run."""
    from paper_reader.evidence_bundle import EvidenceBundleError, prepare_local_evidence

    try:
        prepared = prepare_local_evidence(
            run_path,
            preview_pages=preview_pages,
            figure_limit=figure_limit,
        )
    except RunLoadError as exc:
        _finish(
            "run prepare",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except EvidenceBundleError as exc:
        _finish(
            "run prepare",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish(
        "run prepare",
        ok=True,
        code="prepared_preview" if not prepared.evidence_manifest.complete else "prepared",
        data={
            "run_dir": str(prepared.run_dir),
            "evidence_dir": str(prepared.evidence_dir),
            "evidence_id": prepared.evidence_manifest.evidence_id,
            "evidence_digest": prepared.evidence_digest,
            "complete": prepared.evidence_manifest.complete,
            "degraded": prepared.evidence_manifest.degraded,
        },
    )


def _load_run_or_finish(command: str, run_path: Path):
    try:
        return load_v2_run(run_path)
    except RunLoadError as exc:
        _finish(
            command,
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return None


@run_app.command("status")
def run_status(run_path: Path) -> None:
    """Read the status of a strict paper_reader.run.v2 manifest."""
    loaded = _load_run_or_finish("run status", run_path)
    if loaded is None:
        return
    _finish(
        "run status",
        ok=True,
        code="ok",
        data={
            "schema_version": loaded.run.schema_version,
            "run_id": loaded.run.run_id,
            "status": loaded.run.status,
            "manifest_path": str(loaded.manifest_path),
            "manifest_sha256": loaded.manifest_sha256,
            "canonical_digest": loaded.canonical_digest,
        },
    )


@run_app.command("validate")
def run_validate(run_path: Path) -> None:
    """Validate a strict paper_reader.run.v2 manifest without mutation."""
    loaded = _load_run_or_finish("run validate", run_path)
    if loaded is None:
        return
    _finish(
        "run validate",
        ok=True,
        code="valid",
        data={
            "schema_version": loaded.run.schema_version,
            "run_id": loaded.run.run_id,
            "status": loaded.run.status,
            "manifest_path": str(loaded.manifest_path),
            "manifest_sha256": loaded.manifest_sha256,
            "canonical_digest": loaded.canonical_digest,
        },
    )


@review_app.command("validate")
def review_validate(run_path: Path) -> None:
    """Validate summary, review, evidence, and resolved render context."""
    from paper_reader.review_package import validate_review_run

    try:
        validation = validate_review_run(run_path)
    except RunLoadError as exc:
        _finish(
            "review validate",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    blocker_data = [item.model_dump(mode="json") for item in validation.blockers]
    data = {
        "run_id": validation.loaded_run.run.run_id,
        "summary_sha256": validation.summary_sha256,
        "review_sha256": validation.review_sha256,
        "evidence_digest": validation.evidence.digest if validation.evidence else None,
        "rendered_note_sha256": validation.rendered_note_sha256,
        "blockers": blocker_data,
    }
    if validation.blockers:
        _finish(
            "review validate",
            ok=False,
            code="review_blocked",
            data=data,
            message="review validation is blocked",
            diagnostic="review validation is blocked",
        )
        return
    _finish("review validate", ok=True, code="review_valid", data=data)


@review_app.command("seal")
def review_seal(run_path: Path) -> None:
    """Seal an immutable V2 review package."""
    from paper_reader.review_package import ReviewSealError, seal_review_run

    try:
        sealed = seal_review_run(run_path)
    except RunLoadError as exc:
        _finish(
            "review seal",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except ReviewSealError as exc:
        data = {
            **exc.data,
            "blockers": [item.model_dump(mode="json") for item in exc.blockers],
        }
        _finish(
            "review seal",
            ok=False,
            code=exc.code,
            data=data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish(
        "review seal",
        ok=True,
        code="review_sealed",
        data={
            "run_id": sealed.review_package.run_id,
            "review_package_dir": str(sealed.package_dir),
            "review_package_id": sealed.review_package.review_package_id,
            "review_package_digest": sealed.package_digest,
        },
    )


@candidate_app.command("build")
def candidate_build(run_path: Path) -> None:
    """Build an immutable target-bound candidate."""
    from paper_reader.candidate_builder import build_candidate
    from paper_reader.candidate_integrity import LocalPublicationError

    try:
        built = build_candidate(run_path)
    except RunLoadError as exc:
        _finish(
            "candidate build",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except LocalPublicationError as exc:
        _finish(
            "candidate build",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish(
        "candidate build",
        ok=True,
        code="candidate_built",
        data={
            "run_id": built.candidate.run_id,
            "candidate_id": built.candidate.candidate_id,
            "candidate_dir": str(built.candidate_dir),
            "candidate_path": str(built.candidate_dir / "candidate.json"),
            "candidate_digest": built.candidate_digest,
            "target": built.candidate.target.model_dump(mode="json"),
        },
    )


@local_app.command("publish")
def local_publish(candidate: Path) -> None:
    """Publish a fixed local candidate using atomic no-replace."""
    from paper_reader.candidate_integrity import LocalPublicationError
    from paper_reader.local_publish import publish_local_candidate

    try:
        published = publish_local_candidate(candidate)
    except RunLoadError as exc:
        _finish(
            "local publish",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except LocalPublicationError as exc:
        _finish(
            "local publish",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    _finish(
        "local publish",
        ok=True,
        code="published",
        data={
            "candidate_path": str(published.candidate_path),
            "candidate_digest": published.candidate_digest,
            "target_path": str(published.target_path),
            "content_sha256": published.content_sha256,
            "receipt_path": str(published.receipt_path),
        },
    )


@zotero_app.command("authorize")
def zotero_authorize(
    candidate: Path,
    external_claim_id: str | None = typer.Option(None, "--external-claim-id"),
    write_attempt_id: str | None = typer.Option(None, "--write-attempt-id"),
    ttl_seconds: int = typer.Option(300, "--ttl-seconds"),
) -> None:
    """Authorize one exact external Zotero MCP create call."""
    if (external_claim_id is None) != (write_attempt_id is None):
        message = "--external-claim-id and --write-attempt-id must appear together"
        _finish(
            "zotero authorize",
            ok=False,
            code="invalid_identity_options",
            data={"candidate": str(candidate)},
            message=message,
            diagnostic=message,
        )
        return
    if not 1 <= ttl_seconds <= 300:
        message = "--ttl-seconds must be between 1 and 300"
        _finish(
            "zotero authorize",
            ok=False,
            code="invalid_authorization_ttl",
            data={"candidate": str(candidate), "ttl_seconds": ttl_seconds},
            message=message,
            diagnostic=message,
        )
        return
    from paper_reader.zotero_authorization import (
        ZoteroAuthorizationError,
        authorize_zotero_candidate,
    )

    try:
        authorized = authorize_zotero_candidate(
            candidate,
            external_claim_id=external_claim_id,
            write_attempt_id=write_attempt_id,
            ttl_seconds=ttl_seconds,
        )
    except RunLoadError as exc:
        _finish(
            "zotero authorize",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except ZoteroAuthorizationError as exc:
        _finish(
            "zotero authorize",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    authorization = authorized.authorization
    _finish(
        "zotero authorize",
        ok=True,
        code="authorized",
        data={
            "authorization_path": str(authorized.authorization_path),
            "authorization_id": authorization.authorization_id,
            "authorization_digest": authorized.authorization_digest,
            "candidate_digest": authorization.candidate_digest,
            "external_claim_id": authorization.external_claim_id,
            "write_attempt_id": authorization.write_attempt_id,
            "nonce": authorization.nonce,
            "write_token": authorized.write_token,
            "token_sha256": authorization.token_sha256,
            "expires_at": authorization.expires_at,
            "ttl_seconds": authorization.ttl_seconds,
            "mcp_envelope": authorization.mcp_envelope.model_dump(mode="json"),
        },
    )


@zotero_app.command("verify")
def zotero_verify(
    authorization: Path,
    note_key: str = typer.Option(..., "--note-key"),
) -> None:
    """Verify one exact note readback against an immutable authorization."""
    from paper_reader.zotero_verification import (
        ZoteroVerificationError,
        verify_zotero_authorization,
    )

    try:
        verified = verify_zotero_authorization(
            authorization,
            note_key=note_key,
        )
    except RunLoadError as exc:
        _finish(
            "zotero verify",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except ZoteroVerificationError as exc:
        _finish(
            "zotero verify",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    record = verified.verification
    data = {
        "verification_path": str(verified.verification_path),
        "verification_id": record.verification_id,
        "authorization_digest": verified.authorization_digest,
        "note_key": record.note_key,
        "verified": record.verified,
        "replayed": verified.replayed,
        "checks": [item.model_dump(mode="json") for item in record.checks],
    }
    if not record.verified:
        _finish(
            "zotero verify",
            ok=False,
            code="verification_blocked",
            data=data,
            message="Zotero note readback failed one or more exact verification checks",
            diagnostic="Zotero note readback verification is blocked",
        )
        return
    _finish("zotero verify", ok=True, code="verified", data=data)


@zotero_app.command("reconcile")
def zotero_reconcile(authorization: Path) -> None:
    """Locate and fully verify an uncertain external write read-only."""
    from paper_reader.zotero_reconciliation import (
        ZoteroReconciliationError,
        reconcile_zotero_authorization,
    )

    try:
        reconciled = reconcile_zotero_authorization(authorization)
    except RunLoadError as exc:
        _finish(
            "zotero reconcile",
            ok=False,
            code=exc.code,
            data={"manifest_path": str(exc.manifest_path)},
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    except ZoteroReconciliationError as exc:
        _finish(
            "zotero reconcile",
            ok=False,
            code=exc.code,
            data=exc.data,
            message=str(exc),
            diagnostic=str(exc),
        )
        return
    record = reconciled.reconciliation
    data = {
        "reconciliation_path": str(reconciled.reconciliation_path),
        "reconciliation_id": record.reconciliation_id,
        "authorization_digest": reconciled.authorization_digest,
        "outcome": record.outcome,
        "match_count": record.match_count,
        "matched_note_keys": list(record.matched_note_keys),
        "retry_confirmation_required": record.retry_confirmation_required,
        "replayed": reconciled.replayed,
        "verification_path": (
            str(reconciled.run_dir / record.verification.path)
            if record.verification is not None
            else None
        ),
    }
    if record.outcome != "verified":
        _finish(
            "zotero reconcile",
            ok=False,
            code=f"reconciliation_{record.outcome}",
            data=data,
            message=f"Zotero reconciliation ended as {record.outcome}",
            diagnostic=f"Zotero reconciliation ended as {record.outcome}",
        )
        return
    _finish("zotero reconcile", ok=True, code="reconciliation_verified", data=data)


@maintenance_app.command("extract-pdf")
def maintenance_extract_pdf(
    pdf_path: Path,
    max_pages: int | None = typer.Option(None, "--max-pages", min=1),
) -> None:
    """Run the existing pure PDF text extractor under the V2 result envelope."""
    from paper_reader.pdf_extract import extract_pdf

    try:
        extraction = extract_pdf(pdf_path, max_pages=max_pages)
    except (FileNotFoundError, ValueError):
        message = "PDF extraction failed for the requested input"
        _finish(
            "maintenance extract-pdf",
            ok=False,
            code="extraction_failed",
            data={"pdf_path": str(pdf_path)},
            message=message,
            diagnostic=message,
        )
        return
    _finish(
        "maintenance extract-pdf",
        ok=True,
        code="ok",
        data={"pdf_path": str(pdf_path), "extraction": extraction},
    )


__all__ = ["app"]
