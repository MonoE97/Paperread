from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from paper_reader.candidate_integrity import (
    LocalPublicationError,
    candidate_manifest_path,
    verify_artifact_ref,
    verify_local_source,
)
from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    Identifier,
    LivePreflight,
    McpWriteEnvelope,
    PaperReaderCandidate,
    PaperReaderRun,
    PaperReaderWriteAuthorization,
    ZoteroPublicationTarget,
    ZoteroSourceIdentity,
)
from paper_reader.local_publish import _candidate_run_dir, _load_candidate
from paper_reader.note import FORBIDDEN_RENDERED_HEADINGS, REQUIRED_SECTIONS
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    random_token,
    remove_anchored_tree,
    rfc3339_utc,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.zotero_candidate import _artifact_ref, _captured_live_snapshots, _note_child_view
from paper_reader.zotero_lifecycle import parent_fingerprint
from paper_reader.zotero_lock import locked_zotero_parent
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider
from paper_reader.zotero_authorization_loader import (
    ZoteroAuthorizationBindingError,
    load_authorization_artifact,
)
from paper_reader.zotero_artifact_paths import (
    DeterministicArtifactPaths,
    UnsafeZoteroArtifactPathError,
    anchored_artifact_publication,
    inspect_deterministic_artifact_paths,
)


class ZoteroAuthorizationError(LocalPublicationError):
    pass


_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


def _authorization_artifact_paths(
    run_dir: Path,
    authorization_id: str,
    *,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
) -> DeterministicArtifactPaths:
    try:
        return inspect_deterministic_artifact_paths(
            run_dir,
            root_name="authorizations",
            parent_parts=(),
            stem=authorization_id,
            allow_existing_sidecar=allow_existing_sidecar,
            allow_existing_main=allow_existing_main,
        )
    except UnsafeZoteroArtifactPathError as exc:
        raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc


@dataclass(frozen=True, slots=True)
class AuthorizedZoteroWrite:
    run_dir: Path
    authorization_path: Path
    authorization_dir: Path
    authorization: PaperReaderWriteAuthorization
    authorization_digest: str
    write_token: str


def _candidate_target_without_network(candidate_input: Path) -> tuple[Path, PaperReaderCandidate]:
    requested = candidate_manifest_path(candidate_input)
    try:
        candidate_path = requested.resolve(strict=True)
        candidate = PaperReaderCandidate.model_validate_json(candidate_path.read_bytes())
    except Exception as exc:
        raise ZoteroAuthorizationError(
            "candidate_unreadable",
            f"candidate cannot be inspected before authorization: {requested}: {exc}",
        ) from exc
    if not isinstance(candidate.source, ZoteroSourceIdentity) or not isinstance(
        candidate.target, ZoteroPublicationTarget
    ):
        raise ZoteroAuthorizationError(
            "local_candidate_forbidden",
            "local candidates cannot produce Zotero write authorization",
        )
    return candidate_path, candidate


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _reject_active_authorization(
    run_dir: Path,
    loaded: LoadedRun,
    *,
    parent_key: str,
    note_title: str,
    now: datetime,
) -> None:
    run = loaded.run
    for ref in (item for item in run.artifacts if item.role == "write_authorization"):
        try:
            _path, raw = verify_artifact_ref(
                run_dir,
                ref,
                anchor=loaded.run_directory_anchor,
            )
            authorization = PaperReaderWriteAuthorization.model_validate_json(raw)
        except Exception as exc:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                f"bound authorization failed integrity validation: {ref.path}: {exc}",
            ) from exc
        if canonical_json_bytes(authorization) != raw:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                f"bound authorization is not canonical: {ref.path}",
            )
        if authorization.run_id != run.run_id:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                f"bound authorization run identity mismatch: {ref.path}",
            )
        if (
            authorization.target.parent_key == parent_key
            and authorization.target.note_title == note_title
            and authorization.note_title == note_title
            and _parse_utc(authorization.expires_at) > now
        ):
            raise ZoteroAuthorizationError(
                "authorization_active",
                "this candidate already has an unexpired write authorization",
                data={
                    "authorization_id": authorization.authorization_id,
                    "candidate_digest": authorization.candidate_digest,
                    "expires_at": authorization.expires_at,
                },
            )


def _fresh_live_preflight(
    candidate: PaperReaderCandidate,
    provider: ZoteroReadProvider,
) -> tuple[bytes, bytes]:
    parent, children, parent_bytes, children_bytes = _captured_live_snapshots(
        provider,
        parent_key=candidate.target.parent_key,
    )
    try:
        observed_fingerprint = parent_fingerprint(parent)
    except Exception as exc:
        raise ZoteroAuthorizationError(
            "invalid_live_snapshot",
            f"fresh Zotero parent snapshot is invalid: {exc}",
        ) from exc
    parent_key = str(parent.get("key") or (parent.get("data") or {}).get("key") or "").strip()
    matching_keys: list[str] = []
    for child in children:
        view = _note_child_view(child)
        if view is None:
            continue
        key, parent_item, title = view
        if parent_item != candidate.target.parent_key:
            raise ZoteroAuthorizationError(
                "invalid_live_snapshot",
                "fresh Zotero note child has the wrong parent",
            )
        if title == candidate.note_title:
            matching_keys.append(key)
    if (
        parent_key != candidate.target.parent_key
        or observed_fingerprint != candidate.target.parent_fingerprint
        or matching_keys
    ):
        raise ZoteroAuthorizationError(
            "stale_candidate",
            "candidate parent fingerprint or title availability changed; rebuild candidate",
            data={"matching_note_keys": ",".join(matching_keys)},
        )
    return parent_bytes, children_bytes


def _authorization_ref(
    run_dir: Path,
    staged_path: Path,
    future_path: Path,
    role: str,
    media_type: str,
) -> ArtifactRef:
    return _artifact_ref(run_dir, staged_path, future_path, role, media_type)


def _updated_run(
    run: PaperReaderRun,
    *,
    authorization_ref: ArtifactRef,
    preflight: LivePreflight,
    gate: GateState,
) -> PaperReaderRun:
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status=run.status,
        artifacts=(*run.artifacts, authorization_ref),
        gate=gate,
        live_preflight=preflight,
    )


def _recover_matching_orphan_authorization(
    loaded,
    *,
    candidate_digest: str,
) -> None:
    run_dir = loaded.manifest_path.parent
    authorizations_dir = run_dir / "authorizations"
    if not authorizations_dir.exists():
        return
    bound_paths = {
        item.path
        for item in loaded.run.artifacts
        if item.role == "write_authorization"
    }
    candidates: list[tuple[Path, bytes]] = []
    for main_path in sorted(authorizations_dir.glob("*.json")):
        relative = main_path.relative_to(run_dir).as_posix()
        if relative not in bound_paths:
            candidates.append((main_path, main_path.read_bytes()))
    for sidecar_dir in sorted(path for path in authorizations_dir.iterdir() if path.is_dir()):
        main_path = authorizations_dir / f"{sidecar_dir.name}.json"
        record_path = sidecar_dir / "record.json"
        if not main_path.exists() and record_path.is_file():
            candidates.append((main_path, record_path.read_bytes()))

    matches = []
    for main_path, raw in candidates:
        try:
            preview = PaperReaderWriteAuthorization.model_validate_json(raw)
        except Exception as exc:
            raise ZoteroAuthorizationError(
                "authorization_orphan_invalid",
                f"unbound authorization commit candidate is invalid: {main_path}: {exc}",
            ) from exc
        if preview.run_id != loaded.run.run_id or preview.candidate_digest != candidate_digest:
            continue
        try:
            recovered = load_authorization_artifact(
                loaded,
                main_path,
                require_bound=False,
                raw_override=raw,
            )
        except ZoteroAuthorizationBindingError as exc:
            raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
        matches.append(recovered)
    if len(matches) > 1:
        raise ZoteroAuthorizationError(
            "authorization_recovery_conflict",
            "multiple exact unbound authorizations match this candidate",
        )
    if not matches:
        return

    recovered = matches[0]
    if not recovered.authorization_path.exists():
        recovery_record = recovered.authorization_path.with_suffix("") / "record.json"
        recovery_bytes = recovery_record.read_bytes()
        artifact_paths = _authorization_artifact_paths(
            run_dir,
            recovered.authorization.authorization_id,
            allow_existing_sidecar=True,
            allow_existing_main=False,
        )
        try:
            with anchored_artifact_publication(
                artifact_paths,
                staging_dir=None,
                allow_existing_sidecar=True,
                allow_existing_main=False,
                expected_run_anchor=loaded.run_directory_anchor,
            ) as publication:
                publication.publish_main(
                    recovery_record,
                    expected_bytes=recovery_bytes,
                )
        except UnsafeZoteroArtifactPathError as exc:
            raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroAuthorizationError(
                "authorization_recovery_failed",
                f"failed to restore exact authorization commit marker: {exc}",
            ) from exc
    authorization_ref = _artifact_ref(
        run_dir,
        recovered.authorization_path,
        recovered.authorization_path,
        "write_authorization",
        "application/json",
    )
    updated_run = _updated_run(
        loaded.run,
        authorization_ref=authorization_ref,
        preflight=recovered.authorization.live_preflight,
        gate=recovered.authorization.gate,
    )
    try:
        atomic_write_json(
            loaded.manifest_path,
            updated_run,
            anchor=loaded.run_directory_anchor,
        )
    except Exception as exc:
        raise ZoteroAuthorizationError(
            "authorization_status_update_failed",
            f"authorization main artifact is durable but run binding failed: {exc}",
        ) from exc
    raise ZoteroAuthorizationError(
        "authorization_recovered_token_unavailable",
        (
            "the exact orphan authorization was safely bound, but its plaintext write token "
            "cannot be recovered; do not write with this authorization"
        ),
        data={"authorization_path": str(recovered.authorization_path)},
    )


def authorize_zotero_candidate(
    candidate_input: Path,
    *,
    provider: ZoteroReadProvider | None = None,
    ttl_seconds: int = 300,
    external_claim_id: str | None = None,
    write_attempt_id: str | None = None,
    now: datetime | None = None,
) -> AuthorizedZoteroWrite:
    if (external_claim_id is None) != (write_attempt_id is None):
        raise ZoteroAuthorizationError(
            "invalid_identity_options",
            "external_claim_id and write_attempt_id must appear together",
        )
    if external_claim_id is not None and write_attempt_id is not None:
        try:
            external_claim_id = _IDENTIFIER_ADAPTER.validate_python(
                external_claim_id,
                strict=True,
            )
            write_attempt_id = _IDENTIFIER_ADAPTER.validate_python(
                write_attempt_id,
                strict=True,
            )
        except ValidationError as exc:
            raise ZoteroAuthorizationError(
                "invalid_external_identity",
                "external_claim_id and write_attempt_id must both be valid Identifiers",
            ) from exc
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
        raise ZoteroAuthorizationError(
            "invalid_authorization_ttl",
            "authorization TTL must be between 1 and 300 seconds",
        )
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ZoteroAuthorizationError(
            "invalid_authorization_time",
            "authorization time must be timezone-aware",
        )
    instant = instant.astimezone(timezone.utc)
    candidate_path, inspected = _candidate_target_without_network(candidate_input)
    run_dir = _candidate_run_dir(candidate_path)
    resolved_provider = provider or LocalApiZoteroReadProvider()

    with locked_zotero_parent(run_dir, inspected.target.parent_key):
        with locked_v2_run(run_dir) as loaded:
            try:
                loaded, candidate_path, candidate, candidate_digest, verified = _load_candidate(
                    candidate_path,
                    loaded_run=loaded,
                    require_local=False,
                )
            except LocalPublicationError as exc:
                raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
            if not isinstance(candidate.source, ZoteroSourceIdentity) or not isinstance(
                candidate.target, ZoteroPublicationTarget
            ):
                raise ZoteroAuthorizationError(
                    "local_candidate_forbidden",
                    "local candidates cannot produce Zotero write authorization",
                )
            verify_local_source(candidate.source.attachment)
            _reject_active_authorization(
                run_dir,
                loaded,
                parent_key=candidate.target.parent_key,
                note_title=candidate.note_title,
                now=instant,
            )
            authorization_id = new_random_id("authorization")
            artifact_paths = _authorization_artifact_paths(
                run_dir,
                authorization_id,
                allow_existing_sidecar=False,
                allow_existing_main=False,
            )
            _recover_matching_orphan_authorization(
                loaded,
                candidate_digest=candidate_digest,
            )
            parent_bytes, children_bytes = _fresh_live_preflight(candidate, resolved_provider)

            if external_claim_id is None:
                resolved_claim_id = new_random_id("direct")
                resolved_attempt_id = new_random_id("direct")
                while resolved_attempt_id == resolved_claim_id:  # defensive; UUID collision is negligible
                    resolved_attempt_id = new_random_id("direct")
            else:
                resolved_claim_id = external_claim_id
                assert write_attempt_id is not None
                resolved_attempt_id = write_attempt_id

            _html_path, html_bytes = verified["note_html"][0]
            content_html = html_bytes.decode("utf-8")
            candidate_bytes = canonical_json_bytes(candidate)
            authorization_dir = artifact_paths.sidecar
            authorization_path = artifact_paths.main
            staging = run_dir / f".{authorization_id}.{new_uuid()}.staging"
            run_anchor = loaded.run_directory_anchor
            if run_anchor is None:
                raise ZoteroAuthorizationError(
                    "run_directory_changed",
                    "authorization requires a locked run directory anchor",
                )
            staging_anchor = create_anchored_directory(run_anchor, staging)
            try:
                staged_sidecar = staging / "sidecar"
                files = {
                    "candidate.json": candidate_bytes,
                    "content.html": html_bytes,
                    "parent.json": parent_bytes,
                    "children.json": children_bytes,
                }
                for name, content in files.items():
                    atomic_write_bytes(
                        staged_sidecar / name,
                        content,
                        anchor=staging_anchor,
                    )
                specs = {
                    "candidate.json": ("candidate_snapshot", "application/json"),
                    "content.html": ("authorized_content_html", "text/html"),
                    "parent.json": ("zotero_parent_snapshot", "application/json"),
                    "children.json": ("zotero_children_snapshot", "application/json"),
                }
                validate_directory_anchor(staging_anchor)
                refs = {
                    name: _authorization_ref(
                        run_dir,
                        staged_sidecar / name,
                        authorization_dir / name,
                        role,
                        media,
                    )
                    for name, (role, media) in specs.items()
                }
                validate_directory_anchor(staging_anchor)
                preflight = LivePreflight(
                    preflight_id=new_random_id("preflight"),
                    captured_at=rfc3339_utc(instant),
                    parent_key=candidate.target.parent_key,
                    parent_fingerprint=candidate.target.parent_fingerprint,
                    requested_note_title=candidate.note_title,
                    title_available=True,
                    matching_note_keys=(),
                    parent_snapshot=refs["parent.json"],
                    children_snapshot=refs["children.json"],
                )
                gate = GateState(
                    status="write_ready",
                    evaluated_at=rfc3339_utc(instant),
                    checks=(
                        "candidate_integrity",
                        "source_identity",
                        "parent_fingerprint",
                        "live_title_availability",
                        "canonical_html_binding",
                        "authorization_ttl",
                    ),
                    blockers=(),
                )
                write_token = random_token(32)
                authorization = PaperReaderWriteAuthorization(
                    schema_version="paper_reader.write-authorization.v2",
                    authorization_id=authorization_id,
                    run_id=candidate.run_id,
                    created_at=rfc3339_utc(instant),
                    expires_at=rfc3339_utc(instant + timedelta(seconds=ttl_seconds)),
                    ttl_seconds=ttl_seconds,
                    candidate=refs["candidate.json"],
                    candidate_digest=candidate_digest,
                    target=candidate.target,
                    note_title=candidate.note_title,
                    tags=candidate.tags,
                    content_html=content_html,
                    content_sha256=candidate.content_sha256,
                    content_length=candidate.content_length,
                    minimum_content_length=candidate.content_length,
                    required_headings=tuple(REQUIRED_SECTIONS),
                    forbidden_headings=tuple(FORBIDDEN_RENDERED_HEADINGS),
                    nonce=random_token(32),
                    token_sha256=hashlib.sha256(write_token.encode("utf-8")).hexdigest(),
                    external_claim_id=resolved_claim_id,
                    write_attempt_id=resolved_attempt_id,
                    mcp_envelope=McpWriteEnvelope(
                        parentKey=candidate.target.parent_key,
                        content=content_html,
                        tags=candidate.tags,
                    ),
                    artifacts=tuple(refs.values()),
                    live_preflight=preflight,
                    gate=gate,
                )
                authorization_bytes = canonical_json_bytes(authorization)
                atomic_write_bytes(
                    staged_sidecar / "record.json",
                    authorization_bytes,
                    anchor=staging_anchor,
                )
                staged_authorization_path = staging / f"{authorization_id}.json"
                atomic_write_bytes(
                    staged_authorization_path,
                    authorization_bytes,
                    anchor=staging_anchor,
                )
                sidecar_snapshot = tree_snapshot_from_bytes(
                    {**files, "record.json": authorization_bytes}
                )
                authorization_ref = _authorization_ref(
                    run_dir,
                    staged_authorization_path,
                    authorization_path,
                    "write_authorization",
                    "application/json",
                )
                updated_run = _updated_run(
                    loaded.run,
                    authorization_ref=authorization_ref,
                    preflight=preflight,
                    gate=gate,
                )
                try:
                    enforce_projected_run_size(
                        run_dir,
                        max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                        staging_dir=staging,
                        replacements={loaded.manifest_path: canonical_json_bytes(updated_run)},
                    )
                except RunSizeLimitError as exc:
                    raise ZoteroAuthorizationError(
                        "run_size_limit_exceeded",
                        str(exc),
                        data={"run_size_bytes": exc.actual_bytes, "max_bytes": exc.max_bytes},
                    ) from exc
                publication_phase = "sidecar"
                try:
                    with anchored_artifact_publication(
                        artifact_paths,
                        staging_dir=staging,
                        allow_existing_sidecar=False,
                        allow_existing_main=False,
                        expected_run_anchor=loaded.run_directory_anchor,
                        expected_staging_anchor=staging_anchor,
                        expected_sidecar_snapshot=sidecar_snapshot,
                    ) as publication:
                        publication.publish_sidecar(staged_sidecar)
                        publication_phase = "main"
                        publication.publish_main(
                            staged_authorization_path,
                            expected_bytes=authorization_bytes,
                        )
                except UnsafeZoteroArtifactPathError as exc:
                    raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
                except Exception as exc:
                    raise ZoteroAuthorizationError(
                        "authorization_publication_failed",
                        (
                            f"immutable authorization {publication_phase} publication failed: "
                            f"{authorization_path}: {exc}"
                        ),
                    ) from exc
                try:
                    atomic_write_json(
                        loaded.manifest_path,
                        updated_run,
                        anchor=loaded.run_directory_anchor,
                    )
                except Exception as exc:
                    raise ZoteroAuthorizationError(
                        "authorization_status_update_failed",
                        f"authorization tree is durable but run binding failed: {exc}",
                    ) from exc
                return AuthorizedZoteroWrite(
                    run_dir=run_dir,
                    authorization_path=authorization_path,
                    authorization_dir=authorization_dir,
                    authorization=authorization,
                    authorization_digest=authorization_ref.sha256,
                    write_token=write_token,
                )
            finally:
                try:
                    remove_anchored_tree(
                        run_anchor,
                        staging,
                        expected=staging_anchor,
                    )
                finally:
                    staging_anchor.close()


__all__ = [
    "AuthorizedZoteroWrite",
    "ZoteroAuthorizationError",
    "authorize_zotero_candidate",
]
