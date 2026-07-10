from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.candidate_integrity import LocalPublicationError, verify_artifact_ref
from paper_reader.contracts import (
    McpWriteEnvelope,
    PaperReaderCandidate,
    PaperReaderWriteAuthorization,
    ZoteroPublicationTarget,
    ZoteroSourceIdentity,
)
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.storage import canonical_json_bytes, canonical_json_sha256
from paper_reader.v2_loader import LoadedRun


class ZoteroAuthorizationBindingError(LocalPublicationError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedAuthorization:
    run_dir: Path
    authorization_path: Path
    authorization_bytes: bytes
    authorization_digest: str
    authorization: PaperReaderWriteAuthorization
    candidate: PaperReaderCandidate
    candidate_bytes: bytes


def authorization_manifest_path(authorization_input: Path) -> Path:
    path = Path(authorization_input).expanduser()
    if path.is_dir() or (not path.exists() and path.suffix.lower() != ".json"):
        return path / "authorization.json"
    return path


def inspect_authorization_target(
    authorization_input: Path,
) -> tuple[Path, PaperReaderWriteAuthorization, Path]:
    requested = authorization_manifest_path(authorization_input)
    if requested.is_symlink() or requested.parent.is_symlink():
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization path must not use symlinks",
        )
    try:
        path = requested.resolve(strict=True)
        raw = path.read_bytes()
        authorization = PaperReaderWriteAuthorization.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_unreadable",
            f"authorization cannot be inspected: {requested}: {exc}",
        ) from exc
    if path.parent.parent.name != "authorizations":
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization is outside its run authorizations directory",
        )
    run_dir = path.parent.parent.parent
    return path, authorization, run_dir


def _verify_authorization_members(
    run_dir: Path,
    authorization_dir: Path,
    authorization: PaperReaderWriteAuthorization,
) -> dict[str, tuple[Path, bytes]]:
    verified: dict[str, tuple[Path, bytes]] = {}
    for ref in authorization.artifacts:
        try:
            path, raw = verify_artifact_ref(run_dir, ref)
        except LocalPublicationError as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                f"authorization artifact failed integrity validation: {ref.path}: {exc}",
            ) from exc
        if not path.is_relative_to(authorization_dir):
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                f"authorization artifact escapes its immutable tree: {ref.path}",
            )
        if ref.role in verified:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                f"authorization repeats artifact role: {ref.role}",
            )
        verified[ref.role] = (path, raw)
    required = {
        "candidate_snapshot",
        "authorized_content_html",
        "zotero_parent_snapshot",
        "zotero_children_snapshot",
    }
    if set(verified) != required:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization artifact membership is not the exact closed set",
        )
    if (
        authorization.candidate not in authorization.artifacts
        or authorization.live_preflight.parent_snapshot not in authorization.artifacts
        or authorization.live_preflight.children_snapshot not in authorization.artifacts
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization refs are not members of its immutable artifact set",
        )
    return verified


def load_bound_authorization(
    loaded: LoadedRun,
    authorization_path: Path,
) -> LoadedAuthorization:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    try:
        resolved = Path(authorization_path).resolve(strict=True)
        relative = resolved.relative_to(run_dir).as_posix()
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization path does not belong to the run",
        ) from exc
    refs = [
        item
        for item in loaded.run.artifacts
        if item.role == "write_authorization" and item.path == relative
    ]
    if len(refs) != 1:
        raise ZoteroAuthorizationBindingError(
            "authorization_not_bound",
            "run does not bind this exact authorization",
        )
    try:
        verified_path, raw = verify_artifact_ref(run_dir, refs[0])
        authorization = PaperReaderWriteAuthorization.model_validate_json(raw)
    except (LocalPublicationError, ValidationError) as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            f"bound authorization failed strict validation: {exc}",
        ) from exc
    if verified_path != resolved or canonical_json_bytes(authorization) != raw:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "bound authorization bytes are not canonical or path-stable",
        )
    digest = hashlib.sha256(raw).hexdigest()
    if authorization.run_id != loaded.run.run_id:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization run_id does not match run.json",
        )
    if (
        not isinstance(loaded.run.source, ZoteroSourceIdentity)
        or not isinstance(loaded.run.target, ZoteroPublicationTarget)
        or authorization.target != loaded.run.target
        or authorization.gate.status != "write_ready"
        or authorization.gate.blockers
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization target or gate is not bound to the Zotero run",
        )
    members = _verify_authorization_members(run_dir, resolved.parent, authorization)
    _candidate_path, candidate_bytes = members["candidate_snapshot"]
    try:
        candidate = PaperReaderCandidate.model_validate_json(candidate_bytes)
    except ValidationError as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            f"authorization candidate snapshot is invalid: {exc}",
        ) from exc
    if (
        canonical_json_bytes(candidate) != candidate_bytes
        or canonical_json_sha256(candidate) != authorization.candidate_digest
        or candidate.run_id != authorization.run_id
        or candidate.source != loaded.run.source
        or candidate.target != authorization.target
        or candidate.note_title != authorization.note_title
        or candidate.tags != authorization.tags
        or candidate.content_sha256 != authorization.content_sha256
        or candidate.content_length != authorization.content_length
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization and candidate bindings are inconsistent",
        )
    bound_candidates = [
        item
        for item in loaded.run.artifacts
        if item.role == "candidate" and item.sha256 == authorization.candidate_digest
    ]
    if len(bound_candidates) != 1:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "run does not bind the authorization candidate digest exactly once",
        )
    for artifact in candidate.artifacts:
        try:
            verify_artifact_ref(run_dir, artifact)
        except LocalPublicationError as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                f"authorization candidate member changed: {artifact.path}: {exc}",
            ) from exc
    _content_path, content_bytes = members["authorized_content_html"]
    try:
        content_html = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorized content.html is not UTF-8",
        ) from exc
    exact_envelope = McpWriteEnvelope(
        parentKey=authorization.target.parent_key,
        content=authorization.content_html,
        tags=authorization.tags,
    )
    if (
        content_html != authorization.content_html
        or note_html_sha256(content_html) != authorization.content_sha256
        or len(canonicalize_note_html_for_hash(content_html)) != authorization.content_length
        or authorization.minimum_content_length > authorization.content_length
        or authorization.mcp_envelope != exact_envelope
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization content or MCP envelope binding changed",
        )
    return LoadedAuthorization(
        run_dir=run_dir,
        authorization_path=resolved,
        authorization_bytes=raw,
        authorization_digest=digest,
        authorization=authorization,
        candidate=candidate,
        candidate_bytes=candidate_bytes,
    )


__all__ = [
    "LoadedAuthorization",
    "ZoteroAuthorizationBindingError",
    "authorization_manifest_path",
    "inspect_authorization_target",
    "load_bound_authorization",
]
