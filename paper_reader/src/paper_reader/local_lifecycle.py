from __future__ import annotations

import fcntl
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

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
    PublishConflictError,
    atomic_publish_tree,
    atomic_write_json,
    canonical_json_bytes,
    new_random_id,
    new_uuid,
    rfc3339_utc,
)

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
    source: LocalSourceIdentity,
    target: LocalPublicationTarget,
) -> PaperReaderRun:
    source_path = staging / "source" / "source.json"
    source_path.parent.mkdir(parents=True)
    source_bytes = canonical_json_bytes(source)
    source_path.write_bytes(source_bytes)

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
    (staging / "run.json").write_bytes(canonical_json_bytes(run))
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
            locked_stat = os.fstat(lock_handle.fileno())
            if (locked_stat.st_dev, locked_stat.st_ino) != (source.device, source.inode):
                raise LocalLifecycleError(
                    "source_changed",
                    "local PDF source inode changed before allocation",
                    data={"source_pdf": str(source_pdf)},
                )
            return _allocate_local_run(source_pdf=Path(source_pdf), source=source)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


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


def _allocate_local_run(
    *,
    source_pdf: Path,
    source: LocalSourceIdentity,
) -> InitializedLocalRun:
    resolved_source = Path(source.resolved_path)
    parent = resolved_source.parent
    stem = resolved_source.stem
    parent_device = parent.stat().st_dev

    version = 1
    while True:
        suffix = "" if version == 1 else f"_v{version}"
        destination = parent / f"{stem}_analysis{suffix}"
        target_path = parent / f"{stem}_note{suffix}.md"
        if os.path.lexists(target_path):
            version += 1
            continue

        staging = parent / f".{destination.name}.{new_uuid()}.staging"
        staging.mkdir()
        try:
            target = LocalPublicationTarget(
                resolved_path=str(target_path),
                parent_device=parent_device,
            )
            run = _stage_initialized_run(staging=staging, source=source, target=target)
            try:
                atomic_publish_tree(staging, destination)
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
            if os.path.lexists(target_path):
                try:
                    atomic_write_json(
                        destination / "run.json",
                        _blocked_target_run(run, target_path),
                    )
                except Exception as exc:
                    raise LocalLifecycleError(
                        "initialization_failed",
                        f"reserved run could not record a raced target conflict: {destination}: {exc}",
                        data={
                            "source_pdf": str(source_pdf),
                            "run_dir": str(destination),
                        },
                    ) from exc
                version += 1
                continue
            return InitializedLocalRun(run_dir=destination, target_path=target_path, run=run)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


__all__ = [
    "InitializedLocalRun",
    "LocalLifecycleError",
    "MAX_LOCAL_PDF_SIZE_BYTES",
    "initialize_local_run",
]
