from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
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
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.run_lock import ExpectedRunArtifact
from paper_reader.storage import (
    UnsafeStoragePathError,
    canonical_json_bytes,
    canonical_json_sha256,
    open_anchored_directory,
    read_anchored_bytes,
    snapshot_directory_fd,
    validate_directory_anchor,
)
from paper_reader.v2_loader import (
    DirectoryAnchor,
    LoadedRun,
    RunLoadError,
    _load_v2_run_from_anchor,
)


class ZoteroAuthorizationBindingError(LocalPublicationError):
    pass


_AUTHORIZATION_SIDECAR_NAMES = (
    "candidate.json",
    "children.json",
    "content.html",
    "parent.json",
    "record.json",
)


def _read_closed_authorization_sidecar(
    anchor: DirectoryAnchor,
    authorization_path: Path,
) -> tuple[dict[str, bytes], str]:
    sidecar = authorization_path.with_suffix("")
    with open_anchored_directory(anchor, sidecar) as sidecar_anchor:
        before_names = tuple(sorted(os.listdir(sidecar_anchor.descriptor)))
        if before_names != _AUTHORIZATION_SIDECAR_NAMES:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization sidecar membership is not the exact closed set",
            )
        before_snapshot = snapshot_directory_fd(sidecar_anchor.descriptor)
        members = {
            name: read_anchored_bytes(sidecar_anchor, sidecar / name)
            for name in _AUTHORIZATION_SIDECAR_NAMES
        }
        after_names = tuple(sorted(os.listdir(sidecar_anchor.descriptor)))
        after_snapshot = snapshot_directory_fd(sidecar_anchor.descriptor)
        if (
            after_names != before_names
            or after_snapshot != before_snapshot
        ):
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization sidecar changed while it was inspected",
            )
        validate_directory_anchor(sidecar_anchor)
    validate_directory_anchor(anchor)
    return members, canonical_json_sha256(after_snapshot)


@dataclass(frozen=True, slots=True)
class LoadedAuthorization:
    run_dir: Path
    authorization_path: Path
    authorization_bytes: bytes
    authorization_digest: str
    authorization: PaperReaderWriteAuthorization
    candidate: PaperReaderCandidate
    candidate_bytes: bytes


@dataclass(frozen=True, slots=True)
class InspectedAuthorization:
    authorization_path: Path
    authorization_bytes: bytes
    authorization: PaperReaderWriteAuthorization
    run_dir: Path
    run_directory_device: int
    run_directory_inode: int
    run_manifest_bytes: bytes
    run_manifest_sha256: str
    skill_root: Path
    skill_root_device: int
    skill_root_inode: int
    expected_artifacts: tuple[ExpectedRunArtifact, ...]


def authorization_manifest_path(authorization_input: Path) -> Path:
    path = Path(authorization_input).expanduser()
    if path.suffix.lower() == ".json":
        return path
    if path.parent.name == "authorizations":
        return path.parent / f"{path.name}.json"
    return path


def preflight_authorization_schema_versions(
    authorization_input: Path,
) -> InspectedAuthorization:
    requested = authorization_manifest_path(authorization_input)
    path = Path(os.path.abspath(requested))
    if path.parent.name != "authorizations" or path.suffix.lower() != ".json":
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization does not use authorizations/<authorization_id>.json",
        )
    run_dir = path.parent.parent
    runs_root = run_dir.parent.parent
    if runs_root.name != "runs":
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization run is outside <skill_root>/runs/YYYY-MM-DD",
        )
    skill_root = runs_root.parent
    with ExitStack() as anchors:
        try:
            skill_anchor = anchors.enter_context(
                DirectoryAnchor.open(skill_root, manifest_path=path)
            )
            anchor = anchors.enter_context(
                DirectoryAnchor.open(run_dir, manifest_path=path)
            )
        except RunLoadError as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                f"authorization run or skill root path is unsafe: {requested}: {exc}",
            ) from exc
        loaded = _load_v2_run_from_anchor(
            anchor,
            manifest_name="run.json",
            manifest_path=run_dir / "run.json",
        )
        try:
            raw = read_anchored_bytes(anchor, path)
        except (OSError, ValueError) as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_unreadable",
                f"authorization cannot be inspected safely: {requested}: {exc}",
            ) from exc
        require_raw_schema_version(
            raw,
            expected="paper_reader.write-authorization.v2",
            artifact_path=path,
        )
        try:
            authorization = PaperReaderWriteAuthorization.model_validate_json(raw)
        except ValidationError as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_unreadable",
                f"authorization cannot be inspected: {requested}: {exc}",
            ) from exc
        if path.name != f"{authorization.authorization_id}.json":
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization does not use authorizations/<authorization_id>.json",
            )
        try:
            sidecar_members, sidecar_digest = _read_closed_authorization_sidecar(
                anchor,
                path,
            )
        except (OSError, ValueError) as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization sidecar cannot be inspected safely",
            ) from exc
        if sidecar_members["record.json"] != raw:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization sidecar record differs from its main commit marker",
            )
        candidate_path = path.with_suffix("") / "candidate.json"
        expected_candidate_relative = candidate_path.relative_to(anchor.path).as_posix()
        if authorization.candidate.path != expected_candidate_relative:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization candidate ref does not bind the closed sidecar member",
            )
        candidate_bytes = sidecar_members["candidate.json"]
        require_raw_schema_version(
            candidate_bytes,
            expected="paper_reader.candidate.v2",
            artifact_path=candidate_path,
        )
        try:
            validate_directory_anchor(anchor)
            validate_directory_anchor(skill_anchor)
        except (OSError, UnsafeStoragePathError) as exc:
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization run or skill root changed during preflight",
            ) from exc
        return InspectedAuthorization(
            authorization_path=path,
            authorization_bytes=raw,
            authorization=authorization,
            run_dir=anchor.path,
            run_directory_device=anchor.device,
            run_directory_inode=anchor.inode,
            run_manifest_bytes=loaded.manifest_bytes,
            run_manifest_sha256=loaded.manifest_sha256,
            skill_root=skill_anchor.path,
            skill_root_device=skill_anchor.device,
            skill_root_inode=skill_anchor.inode,
            expected_artifacts=(
                ExpectedRunArtifact(
                    path=path.relative_to(anchor.path).as_posix(),
                    sha256=hashlib.sha256(raw).hexdigest(),
                ),
                ExpectedRunArtifact(
                    path=candidate_path.relative_to(anchor.path).as_posix(),
                    sha256=hashlib.sha256(candidate_bytes).hexdigest(),
                ),
                ExpectedRunArtifact(
                    path=(path.with_suffix("") / "record.json")
                    .relative_to(anchor.path)
                    .as_posix(),
                    sha256=hashlib.sha256(sidecar_members["record.json"]).hexdigest(),
                ),
                ExpectedRunArtifact(
                    path=path.with_suffix("").relative_to(anchor.path).as_posix(),
                    sha256=sidecar_digest,
                    kind="tree",
                ),
            ),
        )


def inspect_authorization_target(
    authorization_input: Path,
) -> tuple[Path, PaperReaderWriteAuthorization, Path]:
    inspected = preflight_authorization_schema_versions(authorization_input)
    return (
        inspected.authorization_path,
        inspected.authorization,
        inspected.run_dir,
    )


def _verify_authorization_members(
    run_dir: Path,
    authorization_path: Path,
    authorization_dir: Path,
    authorization_bytes: bytes,
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
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization sidecar validation requires a locked run anchor",
        )
    try:
        sidecar_members, _sidecar_digest = _read_closed_authorization_sidecar(
            anchor,
            authorization_path,
        )
    except ZoteroAuthorizationBindingError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization sidecar cannot be inspected safely",
        ) from exc
    if sidecar_members["record.json"] != authorization_bytes:
        raise ZoteroAuthorizationBindingError(
            "authorization_tampered",
            "authorization sidecar record differs from its main commit marker",
        )
    expected_member_by_role = {
        "candidate_snapshot": "candidate.json",
        "authorized_content_html": "content.html",
        "zotero_parent_snapshot": "parent.json",
        "zotero_children_snapshot": "children.json",
    }
    for role, filename in expected_member_by_role.items():
        verified_path, verified_bytes = verified[role]
        if (
            verified_path != authorization_dir / filename
            or verified_bytes != sidecar_members[filename]
        ):
            raise ZoteroAuthorizationBindingError(
                "authorization_tampered",
                "authorization refs do not bind the exact closed sidecar members",
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
    require_raw_schema_version(
        raw,
        expected="paper_reader.write-authorization.v2",
        artifact_path=resolved,
    )
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
        resolved,
        authorization_dir,
        raw,
        authorization,
        loaded,
    )
    _candidate_path, candidate_bytes = members["candidate_snapshot"]
    require_raw_schema_version(
        candidate_bytes,
        expected="paper_reader.candidate.v2",
        artifact_path=_candidate_path,
    )
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
    "InspectedAuthorization",
    "LoadedAuthorization",
    "ZoteroAuthorizationBindingError",
    "authorization_manifest_path",
    "inspect_authorization_target",
    "load_authorization_artifact",
    "load_bound_authorization",
    "preflight_authorization_schema_versions",
]
