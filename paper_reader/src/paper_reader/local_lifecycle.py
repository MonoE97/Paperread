from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import fitz

from paper_reader.contracts import (
    ArtifactRef,
    GateBlocker,
    GateState,
    LocalPublicationTarget,
    LocalSourceIdentity,
    PaperReaderRun,
)
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import (
    DirectoryAnchorLike,
    HeldExactFileGuard,
    ImmutableTreeSnapshot,
    OwnedPublishedFile,
    OwnedPublishedTree,
    PublishConflictError,
    UnsafeStoragePathError,
    anchored_entry_exists,
    atomic_publish_tree,
    atomic_write_bytes,
    canonical_json_bytes,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    remove_anchored_tree,
    rfc3339_utc,
    snapshot_directory_fd,
    stat_anchored_entry,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.v2_loader import DirectoryAnchor, RunLoadError

MAX_LOCAL_PDF_SIZE_BYTES = V2_RESOURCE_POLICY.local_pdf_max_bytes


class LocalLifecycleError(ValueError):
    def __init__(self, code: str, message: str, *, data: dict[str, str | int] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass(frozen=True, slots=True)
class InitializedLocalRun:
    run_dir: Path
    target_path: Path
    run: PaperReaderRun


@dataclass(slots=True)
class _LockedSourceBinding:
    locked_source: BinaryIO
    resolved_source: Path
    expected: LocalSourceIdentity
    requested_source: Path
    parent_anchor: DirectoryAnchor
    path_descriptor: int

    def close(self) -> None:
        if self.path_descriptor >= 0:
            descriptor = self.path_descriptor
            self.path_descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> _LockedSourceBinding:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        try:
            validate_directory_anchor(self.parent_anchor)
            locked_before = os.fstat(self.locked_source.fileno())
            path_before = os.fstat(self.path_descriptor)
            named_before = os.stat(
                self.resolved_source.name,
                dir_fd=self.parent_anchor.descriptor,
                follow_symlinks=False,
            )
            digest = hashlib.sha256()
            offset = 0
            while offset <= MAX_LOCAL_PDF_SIZE_BYTES:
                chunk = os.pread(
                    self.locked_source.fileno(),
                    min(1024 * 1024, MAX_LOCAL_PDF_SIZE_BYTES - offset + 1),
                    offset,
                )
                if not chunk:
                    break
                digest.update(chunk)
                offset += len(chunk)
                if offset > MAX_LOCAL_PDF_SIZE_BYTES:
                    break
            locked_after = os.fstat(self.locked_source.fileno())
            path_after = os.fstat(self.path_descriptor)
            named_after = os.stat(
                self.resolved_source.name,
                dir_fd=self.parent_anchor.descriptor,
                follow_symlinks=False,
            )
            validate_directory_anchor(self.parent_anchor)
        except (OSError, UnsafeStoragePathError) as exc:
            raise LocalLifecycleError(
                "source_changed",
                f"local PDF source path changed before allocation committed: "
                f"{self.resolved_source}: {exc}",
                data={"source_pdf": str(self.requested_source)},
            ) from exc

        metadata = (
            locked_before,
            path_before,
            named_before,
            locked_after,
            path_after,
            named_after,
        )
        identities = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in metadata
        }
        if (
            len(identities) != 1
            or not all(stat.S_ISREG(item.st_mode) for item in metadata)
            or offset > MAX_LOCAL_PDF_SIZE_BYTES
            or (
                locked_after.st_dev,
                locked_after.st_ino,
                locked_after.st_size,
                digest.hexdigest(),
            )
            != (
                self.expected.device,
                self.expected.inode,
                self.expected.size_bytes,
                self.expected.sha256,
            )
        ):
            raise LocalLifecycleError(
                "source_changed",
                "local PDF source pathname or complete fingerprint changed before "
                "allocation committed",
                data={"source_pdf": str(self.requested_source)},
            )


@dataclass(slots=True)
class _PublishedRunGuard:
    parent_anchor: DirectoryAnchor
    published_tree: OwnedPublishedTree
    destination: Path
    expected_run_bytes: bytes
    expected_tree_snapshot: ImmutableTreeSnapshot
    manifest_exchange_count: int = 0

    def close(self) -> None:
        self.published_tree.close()

    def __enter__(self) -> _PublishedRunGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        run_anchor = self.published_tree.directory
        try:
            validate_directory_anchor(self.parent_anchor)
            validate_directory_anchor(run_anchor)
            directory_before = os.fstat(run_anchor.descriptor)
            named_directory_before = stat_anchored_entry(
                self.parent_anchor,
                self.destination,
            )
            HeldExactFileGuard(
                anchor=run_anchor,
                owned_file=self.published_tree.held_file,
                expected_bytes=self.expected_run_bytes,
                label="local run manifest",
            ).verify()
            observed_tree_snapshot = snapshot_directory_fd(
                run_anchor.descriptor,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            directory_after = os.fstat(run_anchor.descriptor)
            named_directory_after = stat_anchored_entry(
                self.parent_anchor,
                self.destination,
            )
            HeldExactFileGuard(
                anchor=run_anchor,
                owned_file=self.published_tree.held_file,
                expected_bytes=self.expected_run_bytes,
                label="local run manifest",
            ).verify()
            validate_directory_anchor(run_anchor)
            validate_directory_anchor(self.parent_anchor)
        except (OSError, UnsafeStoragePathError) as exc:
            raise LocalLifecycleError(
                "initialization_failed",
                f"published local run identity became uncertain: {self.destination}: {exc}",
                data={"run_dir": str(self.destination)},
            ) from exc

        directory_identities = {
            (item.st_dev, item.st_ino)
            for item in (
                directory_before,
                named_directory_before,
                directory_after,
                named_directory_after,
            )
        }
        expected_directory_identity = {
            (run_anchor.device, run_anchor.inode)
        }
        if (
            directory_identities != expected_directory_identity
            or not all(
                stat.S_ISDIR(item.st_mode)
                for item in (
                    directory_before,
                    named_directory_before,
                    directory_after,
                    named_directory_after,
                )
            )
            or observed_tree_snapshot != self.expected_tree_snapshot
        ):
            raise LocalLifecycleError(
                "initialization_failed",
                f"published local run changed before initialization finalized: "
                f"{self.destination}",
                data={"run_dir": str(self.destination)},
            )

    def _adopt_manifest_exchange_snapshot(
        self,
        *,
        run: PaperReaderRun,
        next_bytes: bytes,
        prior_bytes: bytes,
    ) -> None:
        if self.manifest_exchange_count >= 1:
            raise LocalLifecycleError(
                "initialization_failed",
                "blocked local run exceeded its single manifest transition: "
                f"{self.destination}",
                data={"run_dir": str(self.destination)},
            )
        base = tree_snapshot_from_bytes(
            {
                "source/source.json": canonical_json_bytes(run.source),
                "run.json": next_bytes,
            }
        )
        observed = snapshot_directory_fd(
            self.published_tree.directory.descriptor,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        base_by_path = {entry.path: entry for entry in base.entries}
        observed_by_path = {entry.path: entry for entry in observed.entries}
        if any(observed_by_path.get(path) != entry for path, entry in base_by_path.items()):
            raise LocalLifecycleError(
                "initialization_failed",
                "blocked local run base tree changed during manifest exchange: "
                f"{self.destination}",
                data={"run_dir": str(self.destination)},
            )

        previous_extra = {
            entry.path: entry
            for entry in self.expected_tree_snapshot.entries
            if entry.path not in base_by_path
        }
        observed_extra = {
            path: entry
            for path, entry in observed_by_path.items()
            if path not in base_by_path
        }
        prior_sha256 = hashlib.sha256(prior_bytes).hexdigest()

        def is_exact_retired_manifest(path: str, entry: object) -> bool:
            return (
                path.startswith(".run.json.")
                and path.endswith(".tmp")
                and "/" not in path
                and getattr(entry, "kind", None) == "file"
                and getattr(entry, "size_bytes", None) == len(prior_bytes)
                and getattr(entry, "sha256", None) == prior_sha256
            )

        new_extra = set(observed_extra) - set(previous_extra)
        if (
            any(observed_extra.get(path) != entry for path, entry in previous_extra.items())
            or len(new_extra) != 1
            or not all(
                is_exact_retired_manifest(path, observed_extra[path])
                for path in new_extra
            )
        ):
            raise LocalLifecycleError(
                "initialization_failed",
                "blocked local run manifest exchange left an unexpected tree: "
                f"{self.destination}",
                data={"run_dir": str(self.destination)},
            )
        self.expected_tree_snapshot = observed
        self.manifest_exchange_count += 1

    def replace_manifest(self, run: PaperReaderRun) -> None:
        self.verify()
        next_bytes = canonical_json_bytes(run)
        prior_bytes = self.expected_run_bytes
        try:
            next_manifest = atomic_write_bytes(
                self.destination / "run.json",
                next_bytes,
                anchor=self.published_tree.directory,
                hold_open=True,
                expected_current=self.published_tree.held_file,
            )
        except (OSError, UnsafeStoragePathError) as exc:
            raise LocalLifecycleError(
                "initialization_failed",
                f"published local run could not record a blocked state safely: "
                f"{self.destination}: {exc}",
                data={"run_dir": str(self.destination)},
            ) from exc
        if not isinstance(next_manifest, OwnedPublishedFile):
            raise LocalLifecycleError(
                "initialization_failed",
                "blocked local run update did not retain its manifest identity: "
                f"{self.destination}",
                data={"run_dir": str(self.destination)},
            )
        previous_manifest = self.published_tree.held_file
        self.published_tree.held_file = next_manifest
        self.expected_run_bytes = next_bytes
        try:
            self._adopt_manifest_exchange_snapshot(
                run=run,
                next_bytes=next_bytes,
                prior_bytes=prior_bytes,
            )
        finally:
            previous_manifest.close()
        self.verify()


def _local_source_identity(source_pdf: Path) -> LocalSourceIdentity:
    from paper_reader.storage import fingerprint_resolved_source

    requested_path = str(source_pdf)
    try:
        resolved = source_pdf.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise LocalLifecycleError(
            "source_not_found",
            f"local PDF source does not exist: {source_pdf}",
            data={"source_pdf": requested_path},
        ) from exc
    except OSError as exc:
        raise LocalLifecycleError(
            "source_unreadable",
            f"local PDF source cannot be resolved: {source_pdf}: {exc}",
            data={"source_pdf": requested_path},
        ) from exc

    try:
        initial_stat = resolved.stat()
    except OSError as exc:
        raise LocalLifecycleError(
            "source_unreadable",
            f"local PDF source cannot be stat-ed: {resolved}: {exc}",
            data={"source_pdf": requested_path},
        ) from exc
    if not stat.S_ISREG(initial_stat.st_mode):
        raise LocalLifecycleError(
            "invalid_local_pdf",
            f"local PDF source must be a regular file: {resolved}",
            data={"source_pdf": requested_path},
        )
    if initial_stat.st_size > MAX_LOCAL_PDF_SIZE_BYTES:
        raise LocalLifecycleError(
            "source_too_large",
            f"local PDF source exceeds {MAX_LOCAL_PDF_SIZE_BYTES} bytes: {resolved}",
            data={
                "source_pdf": requested_path,
                "size_bytes": initial_stat.st_size,
                "max_size_bytes": MAX_LOCAL_PDF_SIZE_BYTES,
            },
        )

    try:
        with fitz.open(resolved) as document:
            _page_count = document.page_count
    except Exception as exc:
        raise LocalLifecycleError(
            "invalid_local_pdf",
            f"local PDF source is not a readable PDF: {resolved}: {exc}",
            data={"source_pdf": requested_path},
        ) from exc

    try:
        fingerprint = fingerprint_resolved_source(resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalLifecycleError(
            "source_changed",
            f"local PDF source changed or became unreadable while fingerprinting: {resolved}: {exc}",
            data={"source_pdf": requested_path},
        ) from exc
    initial_identity = (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_size,
        initial_stat.st_mtime_ns,
    )
    fingerprint_identity = (
        fingerprint.device,
        fingerprint.inode,
        fingerprint.size_bytes,
        fingerprint.mtime_ns,
    )
    if fingerprint_identity != initial_identity:
        raise LocalLifecycleError(
            "source_changed",
            f"local PDF source changed before fingerprinting completed: {resolved}",
            data={"source_pdf": requested_path},
        )
    return LocalSourceIdentity(
        requested_path=requested_path,
        resolved_path=fingerprint.resolved_path,
        sha256=fingerprint.sha256,
        size_bytes=fingerprint.size_bytes,
        device=fingerprint.device,
        inode=fingerprint.inode,
    )


def _stage_initialized_run(
    *,
    staging: Path,
    staging_anchor: DirectoryAnchorLike,
    source: LocalSourceIdentity,
    target: LocalPublicationTarget,
) -> PaperReaderRun:
    source_path = staging / "source" / "source.json"
    source_bytes = canonical_json_bytes(source)
    atomic_write_bytes(source_path, source_bytes, anchor=staging_anchor)

    import hashlib

    source_ref = ArtifactRef(
        role="source_snapshot",
        path="source/source.json",
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        size_bytes=len(source_bytes),
        media_type="application/json",
    )
    run = PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=new_random_id("run"),
        created_at=rfc3339_utc(),
        source=source,
        target=target,
        status="initialized",
        artifacts=(source_ref,),
        gate=GateState(status="not_evaluated"),
        live_preflight=None,
    )
    atomic_write_bytes(
        staging / "run.json",
        canonical_json_bytes(run),
        anchor=staging_anchor,
    )
    return run


def initialize_local_run(source_pdf: Path) -> InitializedLocalRun:
    source = _local_source_identity(Path(source_pdf))
    resolved_source = Path(source.resolved_path)
    try:
        lock_handle = resolved_source.open("rb")
    except OSError as exc:
        raise LocalLifecycleError(
            "source_changed",
            f"local PDF source became unavailable before allocation: {resolved_source}: {exc}",
            data={"source_pdf": str(source_pdf)},
        ) from exc
    with lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            return _allocate_local_run(
                source_pdf=Path(source_pdf),
                source=source,
                locked_source=lock_handle,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _open_locked_source_binding(
    *,
    locked_source: BinaryIO,
    resolved_source: Path,
    expected: LocalSourceIdentity,
    requested_source: Path,
    parent_anchor: DirectoryAnchor,
) -> _LockedSourceBinding:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        path_descriptor = os.open(
            resolved_source.name,
            flags,
            dir_fd=parent_anchor.descriptor,
        )
    except OSError as exc:
        raise LocalLifecycleError(
            "source_changed",
            f"local PDF source path changed before allocation: {resolved_source}: {exc}",
            data={"source_pdf": str(requested_source)},
        ) from exc
    binding = _LockedSourceBinding(
        locked_source=locked_source,
        resolved_source=resolved_source,
        expected=expected,
        requested_source=requested_source,
        parent_anchor=parent_anchor,
        path_descriptor=path_descriptor,
    )
    try:
        binding.verify()
    except BaseException:
        binding.close()
        raise
    return binding


def _blocked_target_run(run: PaperReaderRun, target_path: Path) -> PaperReaderRun:
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="blocked",
        artifacts=run.artifacts,
        gate=GateState(
            status="blocked",
            evaluated_at=rfc3339_utc(),
            checks=("fixed_local_target",),
            blockers=(
                GateBlocker(
                    code="local_target_conflict",
                    message=f"fixed local target became occupied during allocation: {target_path}",
                ),
            ),
        ),
        live_preflight=run.live_preflight,
    )


def _blocked_source_run(run: PaperReaderRun, source_path: Path) -> PaperReaderRun:
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="blocked",
        artifacts=run.artifacts,
        gate=GateState(
            status="blocked",
            evaluated_at=rfc3339_utc(),
            checks=("source_identity",),
            blockers=(
                GateBlocker(
                    code="source_changed",
                    message=(
                        "local PDF source pathname or fingerprint changed during "
                        f"run allocation: {source_path}"
                    ),
                ),
            ),
        ),
        live_preflight=run.live_preflight,
    )


def _allocate_local_run(
    *,
    source_pdf: Path,
    source: LocalSourceIdentity,
    locked_source: BinaryIO,
) -> InitializedLocalRun:
    resolved_source = Path(source.resolved_path)
    parent = resolved_source.parent
    stem = resolved_source.stem
    parent_metadata = parent.stat()
    parent_device = parent_metadata.st_dev
    parent_inode = parent_metadata.st_ino

    try:
        parent_anchor_context = DirectoryAnchor.open(
            parent,
            manifest_path=parent / "run.json",
        )
    except RunLoadError as exc:
        raise LocalLifecycleError("initialization_failed", str(exc)) from exc
    with parent_anchor_context as parent_anchor:
        if (parent_anchor.device, parent_anchor.inode) != (parent_device, parent_inode):
            raise LocalLifecycleError(
                "initialization_failed",
                "local PDF parent changed before run allocation",
            )
        source_binding = _open_locked_source_binding(
            locked_source=locked_source,
            resolved_source=resolved_source,
            expected=source,
            requested_source=source_pdf,
            parent_anchor=parent_anchor,
        )
        with source_binding:
            version = 1
            while True:
                suffix = "" if version == 1 else f"_v{version}"
                destination = parent / f"{stem}_analysis{suffix}"
                target_path = parent / f"{stem}_note{suffix}.md"
                if os.path.lexists(target_path):
                    version += 1
                    continue

                source_binding.verify()
                staging = parent / f".{destination.name}.{new_uuid()}.staging"
                staging_anchor = create_anchored_directory(parent_anchor, staging)
                try:
                    target = LocalPublicationTarget(
                        resolved_path=str(target_path),
                        parent_device=parent_device,
                        parent_inode=parent_inode,
                    )
                    run = _stage_initialized_run(
                        staging=staging,
                        staging_anchor=staging_anchor,
                        source=source,
                        target=target,
                    )
                    source_binding.verify()
                    run_bytes = canonical_json_bytes(run)
                    staging_snapshot = tree_snapshot_from_bytes(
                        {
                            "source/source.json": canonical_json_bytes(source),
                            "run.json": run_bytes,
                        }
                    )
                    try:
                        published_tree = atomic_publish_tree(
                            staging,
                            destination,
                            anchor=parent_anchor,
                            expected_staging_anchor=staging_anchor,
                            expected_tree_snapshot=staging_snapshot,
                            hold_open_relative_file="run.json",
                        )
                    except PublishConflictError:
                        version += 1
                        continue
                    except Exception as exc:
                        raise LocalLifecycleError(
                            "initialization_failed",
                            f"local run reservation failed: {destination}: {exc}",
                            data={
                                "source_pdf": str(source_pdf),
                                "run_dir": str(destination),
                            },
                        ) from exc
                    if not isinstance(published_tree, OwnedPublishedTree):
                        raise LocalLifecycleError(
                            "initialization_failed",
                            "local run reservation did not retain its committed identity: "
                            f"{destination}",
                            data={
                                "source_pdf": str(source_pdf),
                                "run_dir": str(destination),
                            },
                        )
                    with _PublishedRunGuard(
                        parent_anchor=parent_anchor,
                        published_tree=published_tree,
                        destination=destination,
                        expected_run_bytes=run_bytes,
                        expected_tree_snapshot=staging_snapshot,
                    ) as published_guard:
                        published_guard.verify()
                        try:
                            source_binding.verify()
                        except LocalLifecycleError as source_exc:
                            published_guard.verify()
                            try:
                                published_guard.replace_manifest(
                                    _blocked_source_run(run, resolved_source)
                                )
                            except LocalLifecycleError as block_exc:
                                raise LocalLifecycleError(
                                    "initialization_failed",
                                    "committed local run could not record source drift "
                                    f"safely: {destination}: {block_exc}",
                                    data={
                                        "source_pdf": str(source_pdf),
                                        "run_dir": str(destination),
                                    },
                                ) from block_exc
                            raise source_exc

                        published_guard.verify()
                        try:
                            target_occupied = anchored_entry_exists(
                                parent_anchor,
                                target_path,
                            )
                        except (OSError, UnsafeStoragePathError) as exc:
                            raise LocalLifecycleError(
                                "initialization_failed",
                                "fixed local target identity became uncertain during "
                                f"allocation: {target_path}: {exc}",
                                data={
                                    "source_pdf": str(source_pdf),
                                    "run_dir": str(destination),
                                },
                            ) from exc
                        published_guard.verify()
                        if target_occupied:
                            try:
                                published_guard.replace_manifest(
                                    _blocked_target_run(run, target_path)
                                )
                            except LocalLifecycleError as exc:
                                raise LocalLifecycleError(
                                    "initialization_failed",
                                    "reserved run could not record a raced target "
                                    f"conflict safely: {destination}: {exc}",
                                    data={
                                        "source_pdf": str(source_pdf),
                                        "run_dir": str(destination),
                                    },
                                ) from exc
                            version += 1
                            continue

                        try:
                            source_binding.verify()
                        except LocalLifecycleError as source_exc:
                            published_guard.verify()
                            try:
                                published_guard.replace_manifest(
                                    _blocked_source_run(run, resolved_source)
                                )
                            except LocalLifecycleError as block_exc:
                                raise LocalLifecycleError(
                                    "initialization_failed",
                                    "committed local run could not record final source "
                                    f"drift safely: {destination}: {block_exc}",
                                    data={
                                        "source_pdf": str(source_pdf),
                                        "run_dir": str(destination),
                                    },
                                ) from block_exc
                            raise source_exc
                        published_guard.verify()
                        try:
                            target_occupied = anchored_entry_exists(
                                parent_anchor,
                                target_path,
                            )
                        except (OSError, UnsafeStoragePathError) as exc:
                            raise LocalLifecycleError(
                                "initialization_failed",
                                "fixed local target identity became uncertain during "
                                f"final allocation: {target_path}: {exc}",
                                data={
                                    "source_pdf": str(source_pdf),
                                    "run_dir": str(destination),
                                },
                            ) from exc
                        published_guard.verify()
                        if target_occupied:
                            try:
                                published_guard.replace_manifest(
                                    _blocked_target_run(run, target_path)
                                )
                            except LocalLifecycleError as exc:
                                raise LocalLifecycleError(
                                    "initialization_failed",
                                    "reserved run could not record a final target "
                                    f"conflict safely: {destination}: {exc}",
                                    data={
                                        "source_pdf": str(source_pdf),
                                        "run_dir": str(destination),
                                    },
                                ) from exc
                            version += 1
                            continue
                        return InitializedLocalRun(
                            run_dir=destination,
                            target_path=target_path,
                            run=run,
                        )
                finally:
                    try:
                        remove_anchored_tree(
                            parent_anchor,
                            staging,
                            expected=staging_anchor,
                        )
                    finally:
                        staging_anchor.close()


__all__ = [
    "InitializedLocalRun",
    "LocalLifecycleError",
    "MAX_LOCAL_PDF_SIZE_BYTES",
    "initialize_local_run",
]
