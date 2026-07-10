from __future__ import annotations

from pathlib import Path

import typer

from paper_reader import __version__
from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.routing import RoutingError, route_input
from paper_reader.storage import canonical_json_bytes, rfc3339_utc
from paper_reader.v2_loader import RunLoadError, load_v2_run


app = typer.Typer(
    help="Paper Reader V2 grouped CLI for local PDF and Zotero-title workflows.",
    no_args_is_help=True,
)
run_app = typer.Typer(help="Initialize, prepare, inspect, and validate V2 runs.", no_args_is_help=True)
review_app = typer.Typer(help="Validate and seal immutable review packages.", no_args_is_help=True)
candidate_app = typer.Typer(help="Build immutable publication candidates.", no_args_is_help=True)
local_app = typer.Typer(help="Publish fixed local candidates without replacement.", no_args_is_help=True)
zotero_app = typer.Typer(help="Authorize and verify external Zotero writes.", no_args_is_help=True)
maintenance_app = typer.Typer(help="Pure utilities outside the V2 lifecycle.", no_args_is_help=True)

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
    result = PaperReaderCommandResult(
        command=command,
        ok=ok,
        code=code,
        created_at=rfc3339_utc(),
        message=message,
        data=data or {},
    )
    typer.echo(canonical_json_bytes(result).decode("utf-8"))
    if diagnostic:
        typer.echo(diagnostic, err=True)
    if not ok:
        raise typer.Exit(exit_code)


def _not_implemented(command: str, **data: str | int | None) -> None:
    message = f"{command} is reserved by the V2 public contract but is not implemented in Task 2"
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
    """Initialize a local-PDF V2 run (implemented in the next lifecycle task)."""
    _not_implemented("run init-local", source_pdf=str(source_pdf))


@run_app.command("init-zotero")
def run_init_zotero(
    raw_mcp_response: Path = typer.Option(..., "--raw-mcp-response"),
    expected_item_key: str = typer.Option(..., "--expected-item-key"),
) -> None:
    """Initialize a Zotero V2 run from a saved raw discovery bundle."""
    _not_implemented(
        "run init-zotero",
        raw_mcp_response=str(raw_mcp_response),
        expected_item_key=expected_item_key,
    )


@run_app.command("prepare")
def run_prepare(run_path: Path) -> None:
    """Prepare immutable evidence for a V2 run."""
    _not_implemented("run prepare", run_path=str(run_path))


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
    _not_implemented("review validate", run_path=str(run_path))


@review_app.command("seal")
def review_seal(run_path: Path) -> None:
    """Seal an immutable V2 review package."""
    _not_implemented("review seal", run_path=str(run_path))


@candidate_app.command("build")
def candidate_build(run_path: Path) -> None:
    """Build an immutable target-bound candidate."""
    _not_implemented("candidate build", run_path=str(run_path))


@local_app.command("publish")
def local_publish(candidate: Path) -> None:
    """Publish a fixed local candidate using atomic no-replace."""
    _not_implemented("local publish", candidate=str(candidate))


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
    _not_implemented(
        "zotero authorize",
        candidate=str(candidate),
        external_claim_id=external_claim_id,
        write_attempt_id=write_attempt_id,
        ttl_seconds=ttl_seconds,
    )


@zotero_app.command("verify")
def zotero_verify(
    authorization: Path,
    note_key: str = typer.Option(..., "--note-key"),
) -> None:
    """Verify one exact note readback against an immutable authorization."""
    _not_implemented("zotero verify", authorization=str(authorization), note_key=note_key)


@zotero_app.command("reconcile")
def zotero_reconcile(authorization: Path) -> None:
    """Locate and fully verify an uncertain external write read-only."""
    _not_implemented("zotero reconcile", authorization=str(authorization))


@maintenance_app.command("extract-pdf")
def maintenance_extract_pdf(
    pdf_path: Path,
    max_pages: int | None = typer.Option(None, "--max-pages", min=1),
) -> None:
    """Run the existing pure PDF text extractor under the V2 result envelope."""
    from paper_reader.pdf_extract import extract_pdf

    try:
        extraction = extract_pdf(pdf_path, max_pages=max_pages)
    except Exception as exc:
        message = f"PDF extraction failed: {exc}"
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
