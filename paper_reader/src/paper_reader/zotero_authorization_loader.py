from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.candidate_integrity import LocalPublicationError, verify_artifact_ref
from paper_reader.contracts import (
    ArtifactRef,
    McpWriteEnvelope,
    PaperReaderCandidate,
    PaperReaderWriteAuthorization,
    ZoteroSourceIdentity,
)
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.storage import (
    canonical_json_bytes,
    canonical_json_sha256,
    read_anchored_bytes,
)
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
    if path.suffix.lower() == ".json":
        return path
    if path.parent.name == "authorizations":
        return path.parent / f"{path.name}.json"
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
    if (
        path.parent.name != "authorizations"
        or path.name != f"{authorization.authorization_id}.json"
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization does not use authorizations/<authorization_id>.json",
        )
    run_dir = path.parent.parent
    return path, authorization, run_dir


def _verify_authorization_members(
    run_dir: Path,
    authorization_dir: Path,
    authorization: PaperReaderWriteAuthorization,
    loaded: LoadedRun,
) -> dict[str, tuple[Path, bytes]]:
    verified: dict[str, tuple[Path, bytes]] = {}
    for ref in authorization.artifacts:
        try:
            path, raw = verify_artifact_ref(
                run_dir,
                ref,
                anchor=loaded.run_directory_anchor,
            )
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


def _validated_authorization_bytes(
    loaded: LoadedRun,
    authorization_path: Path,
    raw: bytes,
    *,
    bound_ref: ArtifactRef | None,
) -> LoadedAuthorization:
    run_dir = loaded.manifest_path.parent
    try:
        resolved = Path(authorization_path).resolve(strict=False)
        relative = resolved.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization path does not belong to the run",
        ) from exc
    try:
        authorization = PaperReaderWriteAuthorization.model_validate_json(raw)
    except ValidationError as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            f"authorization failed strict validation: {exc}",
        ) from exc
    if (
        resolved.parent.name != "authorizations"
        or resolved.name != f"{authorization.authorization_id}.json"
        or canonical_json_bytes(authorization) != raw
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization bytes are not canonical or path-stable",
        )
    if bound_ref is not None:
        try:
            verified_path, verified_raw = verify_artifact_ref(
                run_dir,
                bound_ref,
                anchor=loaded.run_directory_anchor,
            )
        except LocalPublicationError as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                f"bound authorization failed integrity validation: {exc}",
            ) from exc
        if verified_path != resolved or verified_raw != raw or bound_ref.path != relative:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "bound authorization ref does not identify the exact main artifact",
            )
    digest = hashlib.sha256(raw).hexdigest()
    if authorization.run_id != loaded.run.run_id:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization run_id does not match run.json",
        )
    if (
        not isinstance(loaded.run.source, ZoteroSourceIdentity)
        or authorization.target.parent_key != loaded.run.source.item_key
        or authorization.target.parent_fingerprint != loaded.run.source.parent_fingerprint
        or authorization.gate.status != "write_ready"
        or authorization.gate.blockers
    ):
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization target or gate is not bound to the Zotero run",
        )
    authorization_dir = resolved.with_suffix("")
    if authorization_dir.is_symlink():
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization sidecar directory must not be a symlink",
        )
    members = _verify_authorization_members(
        run_dir,
        authorization_dir,
        authorization,
        loaded,
    )
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
    try:
        _bound_candidate_path, bound_candidate_bytes = verify_artifact_ref(
            run_dir,
            bound_candidates[0],
            anchor=loaded.run_directory_anchor,
        )
    except LocalPublicationError as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            f"run-bound authorization candidate failed integrity validation: {exc}",
        ) from exc
    if bound_candidate_bytes != candidate_bytes:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization candidate snapshot differs from the exact run-bound candidate",
        )
    for artifact in candidate.artifacts:
        try:
            verify_artifact_ref(
                run_dir,
                artifact,
                anchor=loaded.run_directory_anchor,
            )
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


def load_authorization_artifact(
    loaded: LoadedRun,
    authorization_path: Path,
    *,
    require_bound: bool,
    raw_override: bytes | None = None,
) -> LoadedAuthorization:
    run_dir = loaded.manifest_path.parent
    resolved = Path(os.path.abspath(Path(authorization_path).expanduser()))
    try:
        relative = resolved.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization path does not belong to the run",
        ) from exc
    refs = [
        item
        for item in loaded.run.artifacts
        if item.role == "write_authorization" and item.path == relative
    ]
    if len(refs) > 1:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "run binds this authorization more than once",
        )
    if require_bound and len(refs) != 1:
        raise ZoteroAuthorizationBindingError(
            "authorization_not_bound",
            "run does not bind this exact authorization",
        )
    if raw_override is None:
        try:
            if loaded.run_directory_anchor is not None:
                raw = read_anchored_bytes(loaded.run_directory_anchor, resolved)
            else:
                if resolved.is_symlink() or resolved.parent.is_symlink():
                    raise OSError("authorization path uses a symlink")
                raw = resolved.read_bytes()
        except (OSError, ValueError) as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_unreadable",
                f"authorization cannot be read: {resolved}: {exc}",
            ) from exc
    else:
        raw = raw_override
    return _validated_authorization_bytes(
        loaded,
        resolved,
        raw,
        bound_ref=refs[0] if refs else None,
    )


def load_bound_authorization(
    loaded: LoadedRun,
    authorization_path: Path,
) -> LoadedAuthorization:
    return load_authorization_artifact(
        loaded,
        authorization_path,
        require_bound=True,
    )


__all__ = [
    "LoadedAuthorization",
    "ZoteroAuthorizationBindingError",
    "authorization_manifest_path",
    "inspect_authorization_target",
    "load_authorization_artifact",
    "load_bound_authorization",
]
