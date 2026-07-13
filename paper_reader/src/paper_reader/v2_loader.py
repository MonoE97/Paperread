from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.contracts import PaperReaderRun
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import canonical_json_sha256


MAX_RUN_MANIFEST_BYTES = V2_RESOURCE_POLICY.structured_artifact_max_bytes


@dataclass(frozen=True, slots=True)
class LoadedRun:
    run: PaperReaderRun
    manifest_path: Path
    manifest_bytes: bytes
    manifest_sha256: str
    canonical_digest: str
    run_directory_device: int
    run_directory_inode: int
    run_directory_anchor: DirectoryAnchor | None = None


@dataclass(slots=True)
class DirectoryAnchor:
    path: Path
    descriptor: int
    device: int
    inode: int

    @classmethod
    def open(cls, path: Path, *, manifest_path: Path) -> DirectoryAnchor:
        lexical_path = Path(os.path.abspath(path))
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(lexical_path.anchor or os.sep, flags)
            for component in lexical_path.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or not stat.S_ISDIR(named.st_mode)
                        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                    ):
                        raise OSError(errno.ELOOP, "unsafe directory component")
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
        except FileNotFoundError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise RunLoadError(
                "run_manifest_missing",
                f"run manifest not found: {manifest_path}",
                manifest_path=manifest_path,
            ) from exc
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            code = (
                "run_manifest_unsafe"
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                else "run_manifest_unreadable"
            )
            raise RunLoadError(
                code,
                f"run directory is not safely readable: {lexical_path}: {exc}",
                manifest_path=manifest_path,
            ) from exc

        assert descriptor is not None
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            os.close(descriptor)
            raise RunLoadError(
                "run_manifest_unsafe",
                f"run directory is not a directory: {lexical_path}",
                manifest_path=manifest_path,
            )
        return cls(
            path=lexical_path,
            descriptor=descriptor,
            device=directory_stat.st_dev,
            inode=directory_stat.st_ino,
        )

    def close(self) -> None:
        os.close(self.descriptor)

    def __enter__(self) -> DirectoryAnchor:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class RunLoadError(ValueError):
    def __init__(self, code: str, message: str, *, manifest_path: Path) -> None:
        super().__init__(message)
        self.code = code
        self.manifest_path = manifest_path


def run_manifest_path(run_path: Path | str) -> Path:
    path = Path(run_path).expanduser()
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path_stat = None
    if (
        path_stat is not None
        and stat.S_ISDIR(path_stat.st_mode)
        or path_stat is None
        and path.suffix.lower() != ".json"
    ):
        return path / "run.json"
    return path


def _load_v2_run_from_anchor(
    anchor: DirectoryAnchor,
    *,
    manifest_name: str,
    manifest_path: Path,
    expose_anchor: bool = False,
) -> LoadedRun:
    manifest_path = anchor.path / manifest_name
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(manifest_name, flags, dir_fd=anchor.descriptor)
    except FileNotFoundError as exc:
        raise RunLoadError(
            "run_manifest_missing",
            f"run manifest not found: {manifest_path}",
            manifest_path=manifest_path,
        ) from exc
    except OSError as exc:
        code = "run_manifest_unsafe" if exc.errno == errno.ELOOP else "run_manifest_unreadable"
        raise RunLoadError(
            code,
            f"run manifest is unreadable: {manifest_path}: {exc}",
            manifest_path=manifest_path,
        ) from exc

    try:
        manifest_stat = os.fstat(descriptor)
        named_before = os.stat(
            manifest_name,
            dir_fd=anchor.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(manifest_stat.st_mode)
            or not stat.S_ISREG(named_before.st_mode)
            or manifest_stat.st_nlink != 1
            or named_before.st_nlink != 1
            or (manifest_stat.st_dev, manifest_stat.st_ino)
            != (named_before.st_dev, named_before.st_ino)
        ):
            raise RunLoadError(
                "run_manifest_unsafe",
                f"run manifest must be a single-link regular file: {manifest_path}",
                manifest_path=manifest_path,
            )
        if manifest_stat.st_size > MAX_RUN_MANIFEST_BYTES:
            raise RunLoadError(
                "run_manifest_too_large",
                (
                    f"run manifest exceeds {MAX_RUN_MANIFEST_BYTES} bytes: "
                    f"{manifest_path}"
                ),
                manifest_path=manifest_path,
            )
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_RUN_MANIFEST_BYTES - total_bytes + 1),
            )
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_RUN_MANIFEST_BYTES:
                raise RunLoadError(
                    "run_manifest_too_large",
                    (
                        f"run manifest exceeded {MAX_RUN_MANIFEST_BYTES} bytes "
                        f"while it was read: {manifest_path}"
                    ),
                    manifest_path=manifest_path,
                )
            chunks.append(chunk)
        manifest_after = os.fstat(descriptor)
        named_after = os.stat(
            manifest_name,
            dir_fd=anchor.descriptor,
            follow_symlinks=False,
        )
        before_identity = (
            manifest_stat.st_dev,
            manifest_stat.st_ino,
            manifest_stat.st_size,
            manifest_stat.st_mtime_ns,
            manifest_stat.st_nlink,
        )
        after_identity = (
            manifest_after.st_dev,
            manifest_after.st_ino,
            manifest_after.st_size,
            manifest_after.st_mtime_ns,
            manifest_after.st_nlink,
        )
        named_identity = (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            named_after.st_mtime_ns,
            named_after.st_nlink,
        )
        if before_identity != after_identity or after_identity != named_identity:
            raise RunLoadError(
                "run_manifest_unsafe",
                f"run manifest changed while it was read: {manifest_path}",
                manifest_path=manifest_path,
            )
        raw_bytes = b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        payload = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunLoadError(
            "invalid_run_json",
            f"run manifest is not valid UTF-8 JSON: {manifest_path}",
            manifest_path=manifest_path,
        ) from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != "paper_reader.run.v2":
        found = payload.get("schema_version") if isinstance(payload, dict) else None
        label = repr(found) if found is not None else "unversioned"
        raise RunLoadError(
            "unsupported_run_schema",
            f"unsupported run schema {label}: {manifest_path}",
            manifest_path=manifest_path,
        )

    try:
        # Validate the original JSON bytes so strict tuple fields still accept
        # their only JSON representation (arrays) without enabling Python-side coercion.
        run = PaperReaderRun.model_validate_json(raw_bytes)
    except ValidationError as exc:
        raise RunLoadError(
            "invalid_run_schema",
            f"paper_reader.run.v2 validation failed: {manifest_path}: {exc.error_count()} error(s)",
            manifest_path=manifest_path,
        ) from exc

    try:
        with DirectoryAnchor.open(anchor.path, manifest_path=manifest_path) as current_anchor:
            current_identity = (current_anchor.device, current_anchor.inode)
    except RunLoadError as exc:
        raise RunLoadError(
            "run_directory_changed",
            f"run directory changed while its manifest was loaded: {anchor.path}",
            manifest_path=manifest_path,
        ) from exc
    if current_identity != (anchor.device, anchor.inode):
        raise RunLoadError(
            "run_directory_changed",
            f"run directory changed while its manifest was loaded: {anchor.path}",
            manifest_path=manifest_path,
        )

    return LoadedRun(
        run=run,
        manifest_path=manifest_path,
        manifest_bytes=raw_bytes,
        manifest_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        canonical_digest=canonical_json_sha256(run),
        run_directory_device=anchor.device,
        run_directory_inode=anchor.inode,
        run_directory_anchor=anchor if expose_anchor else None,
    )


def load_v2_run(run_path: Path | str) -> LoadedRun:
    manifest_path = run_manifest_path(run_path)
    with DirectoryAnchor.open(manifest_path.parent, manifest_path=manifest_path) as anchor:
        return _load_v2_run_from_anchor(
            anchor,
            manifest_name=manifest_path.name,
            manifest_path=manifest_path,
        )


__all__ = [
    "DirectoryAnchor",
    "LoadedRun",
    "RunLoadError",
    "_load_v2_run_from_anchor",
    "load_v2_run",
    "run_manifest_path",
]
