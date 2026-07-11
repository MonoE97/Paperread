from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paper_reader.candidate_builder import (
    _artifact_ref,
    _latest_review_package,
    _sealed_snapshots,
)
from paper_reader.candidate_integrity import (
    LocalPublicationError,
    candidate_core_digest,
    verify_artifact_ref,
    verify_local_source,
)
from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    LivePreflight,
    PaperReaderCandidate,
    PaperReaderRun,
    PaperReaderSummary,
    ZoteroPublicationTarget,
    ZoteroSourceIdentity,
)
from paper_reader.evidence_manifest import EvidenceManifestError, load_bound_evidence
from paper_reader.note import build_note_labels, next_same_day_version_suffix, render_note_html, validate_note
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    atomic_publish_tree,
    atomic_write_json,
    canonical_json_bytes,
    new_random_id,
    new_uuid,
    rfc3339_utc,
)
from paper_reader.v2_loader import LoadedRun, load_v2_run
from paper_reader.zotero_lifecycle import parent_fingerprint
from paper_reader.zotero_live import _parse_headings
from paper_reader.zotero_lock import locked_zotero_parent
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider


@dataclass(frozen=True, slots=True)
class BuiltZoteroCandidate:
    run_dir: Path
    candidate_dir: Path
    candidate: PaperReaderCandidate
    candidate_digest: str


def _source_snapshot_bytes(
    loaded: LoadedRun,
    source: ZoteroSourceIdentity,
) -> tuple[bytes, bytes]:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    refs_by_role = {
        role: [item for item in loaded.run.artifacts if item.role == role]
        for role in ("raw_discovery_bundle", "normalized_source")
    }
    if refs_by_role["raw_discovery_bundle"] != [source.raw_discovery_bundle]:
        raise LocalPublicationError(
            "source_snapshot_missing",
            "run must bind the exact raw Zotero discovery snapshot",
        )
    if refs_by_role["normalized_source"] != [source.normalized_source]:
        raise LocalPublicationError(
            "source_snapshot_missing",
            "run must bind the exact normalized Zotero source snapshot",
        )
    _raw_path, raw_bytes = verify_artifact_ref(run_dir, source.raw_discovery_bundle)
    _normalized_path, normalized_bytes = verify_artifact_ref(run_dir, source.normalized_source)
    try:
        normalized = json.loads(normalized_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            "normalized Zotero source snapshot is not valid JSON",
        ) from exc
    if (
        not isinstance(normalized, dict)
        or normalized.get("format") != "paper_reader.zotero-source.v2-internal"
        or not isinstance(normalized.get("selected_item"), dict)
    ):
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            "normalized Zotero source snapshot structure changed",
        )
    selected = normalized["selected_item"]
    if (
        str(selected.get("key", "")) != source.item_key
        or str(selected.get("title", "")) != source.title
        or str(selected.get("DOI", "")) != source.doi
        or selected.get("version") != source.parent_version
    ):
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            "normalized Zotero source snapshot no longer matches source identity",
        )
    return raw_bytes, normalized_bytes


def _note_child_view(snapshot: dict[str, Any]) -> tuple[str, str, str] | None:
    data = snapshot.get("data")
    if not isinstance(data, dict) or data.get("itemType") != "note":
        return None
    key = str(snapshot.get("key") or data.get("key") or "").strip()
    parent = str(data.get("parentItem", "")).strip()
    title, _headings = _parse_headings(str(data.get("note", "")))
    if not key:
        raise LocalPublicationError("invalid_live_children", "live note child is missing its key")
    return key, parent, title


def _captured_live_snapshots(
    provider: ZoteroReadProvider,
    *,
    parent_key: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], bytes, bytes]:
    try:
        parent = provider.get_parent(parent_key)
        children = provider.get_children(parent_key)
    except Exception as exc:
        raise LocalPublicationError(
            "zotero_read_failed",
            f"read-only Zotero preflight failed: {exc}",
        ) from exc
    if not isinstance(parent, dict) or not isinstance(children, list) or not all(
        isinstance(item, dict) for item in children
    ):
        raise LocalPublicationError(
            "invalid_live_snapshot",
            "read-only Zotero provider returned an invalid parent or children snapshot",
        )
    try:
        parent_bytes = canonical_json_bytes(parent)
        children_bytes = canonical_json_bytes(children)
    except (TypeError, ValueError) as exc:
        raise LocalPublicationError(
            "invalid_live_snapshot",
            f"read-only Zotero snapshots are not canonicalizable: {exc}",
        ) from exc
    return parent, children, parent_bytes, children_bytes


def _rewrite_markdown_h1(note_bytes: bytes, note_title: str) -> bytes:
    try:
        note = note_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalPublicationError("invalid_sealed_review", "sealed note.md is not UTF-8") from exc
    lines = note.splitlines(keepends=True)
    if not lines or not lines[0].startswith("# "):
        raise LocalPublicationError("invalid_sealed_review", "sealed note.md is missing its H1")
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n" if lines[0].endswith("\n") else ""
    lines[0] = f"# {_markdown_literal_note_title(note_title)}{newline}"
    rewritten = "".join(lines)
    errors = validate_note(rewritten)
    if errors:
        raise LocalPublicationError(
            "invalid_sealed_review",
            "; ".join(errors),
        )
    return rewritten.encode("utf-8")


def _markdown_literal_note_title(note_title: str) -> str:
    """Keep the fixed summary prefix readable while neutralizing inline Markdown in the exact title."""

    prefix = "[Codex Summary] "
    fixed_prefix = prefix if note_title.startswith(prefix) else ""
    remainder = note_title[len(fixed_prefix) :]
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "&"):
        remainder = remainder.replace(character, f"\\{character}")
    return f"{fixed_prefix}{remainder}"


def _updated_run(
    loaded: LoadedRun,
    *,
    target: ZoteroPublicationTarget,
    preflight: LivePreflight,
    candidate_ref: ArtifactRef,
    gate: GateState,
) -> PaperReaderRun:
    run = loaded.run
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=target,
        status="candidate_built",
        artifacts=(*run.artifacts, candidate_ref),
        gate=gate,
        live_preflight=preflight,
    )


def build_zotero_candidate(
    run_path: Path,
    *,
    provider: ZoteroReadProvider | None = None,
) -> BuiltZoteroCandidate:
    initial = load_v2_run(run_path)
    source = initial.run.source
    if not isinstance(source, ZoteroSourceIdentity):
        raise LocalPublicationError("not_zotero_candidate", "Zotero candidate requires a Zotero run")
    run_dir = initial.manifest_path.resolve(strict=True).parent
    resolved_provider = provider or LocalApiZoteroReadProvider()
    with locked_zotero_parent(run_dir, source.item_key):
        with locked_v2_run(run_dir) as loaded:
            if loaded.run.source != source:
                raise LocalPublicationError("source_changed", "run source changed before candidate build")
            return _build_zotero_candidate_locked(loaded, resolved_provider)


def _build_zotero_candidate_locked(
    loaded: LoadedRun,
    provider: ZoteroReadProvider,
) -> BuiltZoteroCandidate:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    source = loaded.run.source
    if not isinstance(source, ZoteroSourceIdentity):
        raise LocalPublicationError("not_zotero_candidate", "Zotero candidate requires a Zotero run")

    verify_local_source(source.attachment)
    raw_source_bytes, normalized_source_bytes = _source_snapshot_bytes(loaded, source)
    package, package_path, package_bytes = _latest_review_package(loaded)
    snapshots = _sealed_snapshots(loaded, package, package_path, package_bytes)
    try:
        load_bound_evidence(loaded, package.evidence_digest)
    except EvidenceManifestError as exc:
        raise LocalPublicationError(exc.code, str(exc)) from exc

    parent, children, parent_bytes, children_bytes = _captured_live_snapshots(
        provider,
        parent_key=source.item_key,
    )
    try:
        observed_parent_fingerprint = parent_fingerprint(parent)
    except Exception as exc:
        raise LocalPublicationError(
            "invalid_live_snapshot",
            f"live parent snapshot is invalid: {exc}",
        ) from exc
    parent_key = str(parent.get("key") or (parent.get("data") or {}).get("key") or "").strip()
    if parent_key != source.item_key or observed_parent_fingerprint != source.parent_fingerprint:
        raise LocalPublicationError(
            "parent_fingerprint_mismatch",
            "live Zotero parent no longer matches the initialized parent fingerprint",
        )

    titles: list[str] = []
    for child in children:
        view = _note_child_view(child)
        if view is None:
            continue
        _key, parent_item, title = view
        if parent_item != source.item_key:
            raise LocalPublicationError(
                "invalid_live_children",
                "live note child has the wrong parent",
            )
        if title:
            titles.append(title)
    generated_date = loaded.run.created_at[:10]
    suffix = next_same_day_version_suffix(
        titles,
        paper_title=source.title,
        generated_date=generated_date,
    )
    note_title = f"[Codex Summary] {source.title} - {generated_date}{suffix}"
    matching_keys = tuple(
        view[0]
        for child in children
        if (view := _note_child_view(child)) is not None and view[2] == note_title
    )
    if matching_keys:
        raise LocalPublicationError(
            "candidate_title_occupied",
            "computed Zotero candidate title is already occupied",
        )

    candidate_id = new_random_id("candidate")
    candidate_dir = run_dir / "candidates" / candidate_id
    staging = run_dir / f".{candidate_id}.{new_uuid()}.staging"
    staging.mkdir()
    try:
        note_md_bytes = _rewrite_markdown_h1(snapshots["note.md"], note_title)
        note_html = render_note_html(note_md_bytes.decode("utf-8"))
        note_html_bytes = note_html.encode("utf-8")
        files = {
            "run.json": loaded.manifest_bytes,
            "discovery.raw.json": raw_source_bytes,
            "source.json": normalized_source_bytes,
            **snapshots,
            "parent.json": parent_bytes,
            "children.json": children_bytes,
            "note.md": note_md_bytes,
            "note.html": note_html_bytes,
        }
        for name, content in files.items():
            (staging / name).write_bytes(content)
        specs = {
            "run.json": ("run_snapshot", "application/json"),
            "discovery.raw.json": ("raw_discovery_bundle_snapshot", "application/json"),
            "source.json": ("source_snapshot", "application/json"),
            "evidence.json": ("evidence_manifest_snapshot", "application/json"),
            "summary.json": ("summary_snapshot", "application/json"),
            "review.json": ("review_snapshot", "application/json"),
            "review-package.json": ("review_package_snapshot", "application/json"),
            "validation.json": ("review_validation", "application/json"),
            "note.md": ("note_markdown", "text/markdown"),
            "note.html": ("note_html", "text/html"),
            "parent.json": ("zotero_parent_snapshot", "application/json"),
            "children.json": ("zotero_children_snapshot", "application/json"),
        }
        refs = {
            name: _artifact_ref(run_dir, staging / name, candidate_dir / name, role, media)
            for name, (role, media) in specs.items()
        }
        preflight = LivePreflight(
            preflight_id=new_random_id("preflight"),
            captured_at=rfc3339_utc(),
            parent_key=source.item_key,
            parent_fingerprint=observed_parent_fingerprint,
            requested_note_title=note_title,
            title_available=True,
            matching_note_keys=(),
            parent_snapshot=refs["parent.json"],
            children_snapshot=refs["children.json"],
        )
        target = ZoteroPublicationTarget(
            parent_key=source.item_key,
            parent_fingerprint=observed_parent_fingerprint,
            note_title=note_title,
        )
        checks = (
            "source_identity",
            "evidence_hashes",
            "sealed_review_hashes",
            "parent_fingerprint",
            "live_title_availability",
            "canonical_html_binding",
        )
        gate = GateState(
            status="write_ready",
            evaluated_at=rfc3339_utc(),
            checks=checks,
            blockers=(),
        )
        summary = PaperReaderSummary.model_validate_json(snapshots["summary.json"])
        canonical_html = canonicalize_note_html_for_hash(note_html)
        candidate = PaperReaderCandidate(
            schema_version="paper_reader.candidate.v2",
            candidate_id=candidate_id,
            run_id=loaded.run.run_id,
            created_at=rfc3339_utc(),
            source=source,
            target=target,
            evidence_manifest=refs["evidence.json"],
            sealed_review=refs["review-package.json"],
            note_title=note_title,
            tags=tuple(build_note_labels(summary.model_dump(mode="json"))),
            content_sha256=note_html_sha256(note_html),
            content_length=len(canonical_html),
            artifacts=tuple(refs.values()),
            gate=gate,
            live_preflight=preflight,
        )
        candidate_bytes = canonical_json_bytes(candidate)
        staged_candidate_path = staging / "candidate.json"
        staged_candidate_path.write_bytes(candidate_bytes)
        candidate_path = candidate_dir / "candidate.json"
        candidate_ref = _artifact_ref(
            run_dir,
            staged_candidate_path,
            candidate_path,
            "candidate",
            "application/json",
        )
        digest = candidate_core_digest(candidate)
        if digest != candidate_ref.sha256:
            raise LocalPublicationError(
                "candidate_digest_mismatch",
                "canonical candidate digest does not match candidate.json bytes",
            )
        updated_run = _updated_run(
            loaded,
            target=target,
            preflight=preflight,
            candidate_ref=candidate_ref,
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
            raise LocalPublicationError(
                "run_size_limit_exceeded",
                str(exc),
                data={"run_size_bytes": exc.actual_bytes, "max_bytes": exc.max_bytes},
            ) from exc
        try:
            atomic_publish_tree(staging, candidate_dir)
        except Exception as exc:
            raise LocalPublicationError(
                "candidate_publication_failed",
                f"immutable Zotero candidate publication failed: {candidate_dir}: {exc}",
            ) from exc
        try:
            atomic_write_json(loaded.manifest_path, updated_run)
        except Exception as exc:
            raise LocalPublicationError(
                "candidate_status_update_failed",
                f"Zotero candidate tree is durable but run binding failed: {exc}",
            ) from exc
        return BuiltZoteroCandidate(run_dir, candidate_dir, candidate, digest)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = ["BuiltZoteroCandidate", "build_zotero_candidate"]
