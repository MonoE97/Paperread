from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tomllib
from typing import Callable, Iterator

from paper_reader_batch.v2_contracts import (
    ArtifactRef,
    BatchManifest,
    FileIdentity,
    LocalPrepareResult,
    PdfManifestItem,
    PdfSource,
    SkillRootIdentity,
    WorkerResult,
    ZoteroItemManifestItem,
    ZoteroTitleManifestItem,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    MAX_OPAQUE_ARTIFACT_BYTES,
    _bounded_sorted_names,
    canonical_json_bytes,
    canonical_sha256,
    list_directory,
    locked_file,
    normalized_absolute_path,
    open_directory_fd,
    read_bytes,
    read_relative_bytes,
    sha256_bytes,
    validate_locked_path,
    walk_relative_regular_files,
)
from paper_reader_batch.v2_manifest import _pdf_source


_RUN_ARTIFACT_ANCHOR: ContextVar[tuple[Path, int] | None] = ContextVar(
    "paper_reader_batch_run_artifact_anchor",
    default=None,
)
_ARTIFACT_READ_LIMIT: ContextVar[int] = ContextVar(
    "paper_reader_batch_artifact_read_limit",
    default=MAX_JSON_ARTIFACT_BYTES,
)
_MAX_FOREIGN_BUNDLE_MEMBERS = 100_000
_MAX_HELD_COMMIT_CLOSURE_FILES = 256
_MAX_HELD_COMMIT_CLOSURE_DIRECTORIES = 256
_REQUIRED_REVIEW_PROOFS = frozenset(
    {
        "summary_schema",
        "review_schema",
        "run_binding",
        "evidence_binding",
        "locator_membership",
        "resolved_render_chinese_prose",
    }
)
_REQUIRED_LOCAL_CANDIDATE_PROOFS = frozenset(
    {
        "source_identity",
        "evidence_hashes",
        "sealed_review_hashes",
        "rendered_note_hash",
        "fixed_local_target",
    }
)
_REQUIRED_ZOTERO_CANDIDATE_PROOFS = frozenset(
    {
        "source_identity",
        "evidence_hashes",
        "sealed_review_hashes",
        "parent_fingerprint",
        "live_title_availability",
        "canonical_html_binding",
    }
)


class _OpaqueRecord:
    """Attribute view over one canonical JSON object owned by paper_reader.

    This deliberately has no field schema.  Batch validates only the immutable
    paths, hashes, and cross-bindings it consumes; the single skill remains the
    sole owner of the artifact's full schema and semantic gates.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __getattr__(self, name: str):
        try:
            value = self._payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return _opaque(value)

    def __getitem__(self, name: str):
        return _opaque(self._payload[name])

    def get(self, name: str, default=None):
        return _opaque(self._payload.get(name, default))

    def __eq__(self, other: object) -> bool:
        return self._payload == _plain(other)

    def as_dict(self) -> dict[str, object]:
        return self._payload


@dataclass(slots=True)
class _HeldCommitDirectory:
    path: Path
    manager: object
    descriptor: int
    expected_names: frozenset[str] | None = None


@dataclass(slots=True)
class _HeldCommitFile:
    path: Path
    parent: _HeldCommitDirectory
    descriptor: int
    identity: tuple[int, int, int, int, int, int]
    raw: bytes


class _ArtifactCommitClosure:
    """Hold every consumed inode from its first validation through commit."""

    def __init__(self, run_root: Path) -> None:
        self.run_root = normalized_absolute_path(run_root)
        anchor = _RUN_ARTIFACT_ANCHOR.get()
        if anchor is None or anchor[0] != self.run_root:
            raise _invalid(
                "source_binding_mismatch",
                "foreign commit closure requires the held Reader run anchor",
            )
        self._run_anchor_descriptor = anchor[1]
        self._run_anchor_identity = self._directory_identity(
            os.fstat(self._run_anchor_descriptor)
        )
        self.files: dict[Path, bytes] = {}
        self.closed_directories: dict[Path, frozenset[str]] = {}
        self._held_files: dict[Path, _HeldCommitFile] = {}
        self._held_directories: dict[Path, _HeldCommitDirectory] = {}
        self._directory_order: list[Path] = []
        self._tree_files: dict[Path, frozenset[str]] = {}
        self.frozen = False

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_nlink,
        )

    @staticmethod
    def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    def _verify_run_anchor(self) -> None:
        try:
            held = os.fstat(self._run_anchor_descriptor)
            named = os.stat(self.run_root, follow_symlinks=False)
        except OSError as exc:
            raise _invalid(
                "source_binding_mismatch",
                "held Reader run root became unavailable",
                exc,
            ) from exc
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or self._directory_identity(held) != self._run_anchor_identity
            or self._directory_identity(named) != self._run_anchor_identity
        ):
            raise _invalid(
                "source_binding_mismatch",
                "held Reader run root changed before journal commit",
            )

    def _hold_directory(self, path: Path) -> _HeldCommitDirectory:
        self._verify_run_anchor()
        normalized = normalized_absolute_path(path)
        existing = self._held_directories.get(normalized)
        if existing is not None:
            self._verify_directory(existing)
            return existing
        if self.frozen:
            raise _invalid(
                "artifact_closed_world_mismatch",
                f"foreign artifact directory closure expanded after validation: {normalized}",
            )
        if len(self._held_directories) >= _MAX_HELD_COMMIT_CLOSURE_DIRECTORIES:
            raise _invalid(
                "resource_limit",
                "foreign artifact commit closure has too many directories",
            )
        manager = open_directory_fd(normalized, create=False)
        descriptor, bound = manager.__enter__()
        held = _HeldCommitDirectory(
            path=bound,
            manager=manager,
            descriptor=descriptor,
        )
        self._held_directories[normalized] = held
        self._directory_order.append(normalized)
        try:
            self._verify_run_anchor()
            self._verify_directory(held)
        except BaseException:
            self._held_directories.pop(normalized, None)
            self._directory_order.pop()
            try:
                manager.__exit__(None, None, None)
            except BaseException:
                pass
            raise
        return held

    def _verify_directory(self, held: _HeldCommitDirectory) -> None:
        self._verify_run_anchor()
        try:
            opened = os.fstat(held.descriptor)
            named = os.stat(held.path, follow_symlinks=False)
            names = (
                frozenset(
                    _bounded_sorted_names(
                        held.descriptor,
                        max_entries=_MAX_FOREIGN_BUNDLE_MEMBERS,
                        label="held foreign artifact directory",
                    )
                )
                if held.expected_names is not None
                else None
            )
        except (OSError, BatchRuntimeError) as exc:
            raise _invalid(
                "artifact_closed_world_mismatch",
                f"held foreign artifact directory became unavailable: {held.path}",
                exc,
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (
                held.expected_names is not None
                and names != held.expected_names
            )
        ):
            raise _invalid(
                "artifact_closed_world_mismatch",
                f"held foreign artifact directory changed: {held.path}",
            )
        self._verify_run_anchor()

    @staticmethod
    def _read_descriptor(descriptor: int, *, max_bytes: int, code: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while total <= max_bytes:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, max_bytes - total + 1),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise BatchRuntimeError(
                    code,
                    f"foreign artifact exceeded its read limit of {max_bytes} bytes",
                )
        return b"".join(chunks)

    def _verify_file(self, held: _HeldCommitFile) -> None:
        self._verify_directory(held.parent)
        try:
            opened_before = os.fstat(held.descriptor)
            named_before = os.stat(
                held.path.name,
                dir_fd=held.parent.descriptor,
                follow_symlinks=False,
            )
            raw = self._read_descriptor(
                held.descriptor,
                max_bytes=len(held.raw),
                code="source_binding_mismatch",
            )
            opened_after = os.fstat(held.descriptor)
            named_after = os.stat(
                held.path.name,
                dir_fd=held.parent.descriptor,
                follow_symlinks=False,
            )
        except (OSError, BatchRuntimeError) as exc:
            raise _invalid(
                "source_binding_mismatch",
                f"held foreign artifact became unavailable: {held.path}",
                exc,
            ) from exc
        if (
            {self._identity(item) for item in (
                opened_before,
                named_before,
                opened_after,
                named_after,
            )}
            != {held.identity}
            or not all(
                stat.S_ISREG(item.st_mode)
                for item in (opened_before, named_before, opened_after, named_after)
            )
            or held.identity[5] != 1
            or raw != held.raw
        ):
            raise _invalid(
                "source_binding_mismatch",
                f"held foreign artifact changed: {held.path}",
            )
        self._verify_directory(held.parent)

    def read_and_hold(self, path: Path, *, code: str, max_bytes: int) -> bytes:
        normalized = normalized_absolute_path(path)
        if not normalized.is_relative_to(self.run_root):
            raise ValueError("foreign commit closure may only hold files below its run root")
        existing = self._held_files.get(normalized)
        if existing is not None:
            if len(existing.raw) > max_bytes:
                raise BatchRuntimeError(
                    code,
                    f"foreign artifact exceeds its read limit of {max_bytes} bytes: {normalized}",
                )
            self._verify_file(existing)
            return existing.raw
        if self.frozen:
            raise _invalid(
                "source_binding_mismatch",
                f"foreign artifact closure expanded after validation: {normalized}",
            )
        if len(self._held_files) >= _MAX_HELD_COMMIT_CLOSURE_FILES:
            raise _invalid(
                "resource_limit",
                "foreign artifact commit closure has too many consumed files",
            )
        parent = self._hold_directory(normalized.parent)
        descriptor = -1
        try:
            before = os.stat(
                normalized.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise _invalid(
                    "source_binding_mismatch",
                    f"foreign artifact is not a regular single-link file: {normalized}",
                )
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(normalized.name, flags, dir_fd=parent.descriptor)
            opened_before = os.fstat(descriptor)
            if self._identity(before) != self._identity(opened_before):
                raise _invalid(
                    "source_binding_mismatch",
                    f"foreign artifact changed before first held read: {normalized}",
                )
            raw = self._read_descriptor(descriptor, max_bytes=max_bytes, code=code)
            opened_after = os.fstat(descriptor)
            named_after = os.stat(
                normalized.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            identity = self._identity(opened_before)
            if (
                identity != self._identity(opened_after)
                or identity != self._identity(named_after)
                or len(raw) != identity[2]
            ):
                raise _invalid(
                    "source_binding_mismatch",
                    f"foreign artifact changed during first held read: {normalized}",
                )
            held = _HeldCommitFile(
                path=normalized,
                parent=parent,
                descriptor=descriptor,
                identity=identity,
                raw=raw,
            )
            descriptor = -1
            self._held_files[normalized] = held
            self.files[normalized] = raw
            self._verify_file(held)
            return raw
        except FileNotFoundError as exc:
            raise BatchRuntimeError(code, f"foreign artifact does not exist: {normalized}") from exc
        except OSError as exc:
            raise BatchRuntimeError(code, f"foreign artifact cannot be held safely: {normalized}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def walk_and_hold(self, root: Path) -> set[str]:
        normalized_root = normalized_absolute_path(root)
        if not normalized_root.is_relative_to(self.run_root):
            raise ValueError("foreign commit closure may only walk below its run root")
        existing = self._tree_files.get(normalized_root)
        if existing is not None:
            self.verify()
            return set(existing)
        if self.frozen:
            raise _invalid(
                "artifact_closed_world_mismatch",
                f"foreign artifact tree closure expanded after validation: {normalized_root}",
            )
        found: set[str] = set()
        member_count = 0

        def walk(directory: Path, prefix: PurePosixPath) -> None:
            nonlocal member_count
            held = self._hold_directory(directory)
            before_names = tuple(
                _bounded_sorted_names(
                    held.descriptor,
                    max_entries=_MAX_FOREIGN_BUNDLE_MEMBERS - member_count,
                    label="foreign artifact bundle",
                )
            )
            for name in before_names:
                member_count += 1
                if member_count > _MAX_FOREIGN_BUNDLE_MEMBERS:
                    raise _invalid(
                        "resource_limit",
                        "foreign artifact bundle has too many members",
                    )
                metadata = os.stat(name, dir_fd=held.descriptor, follow_symlinks=False)
                relative = prefix / name
                child_path = directory / name
                if stat.S_ISLNK(metadata.st_mode):
                    raise _invalid(
                        "artifact_closed_world_mismatch",
                        f"foreign bundle contains symlink: {relative}",
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    child = self._hold_directory(child_path)
                    if (metadata.st_dev, metadata.st_ino) != (
                        os.fstat(child.descriptor).st_dev,
                        os.fstat(child.descriptor).st_ino,
                    ):
                        raise _invalid(
                            "artifact_closed_world_mismatch",
                            f"foreign bundle directory changed while held: {relative}",
                        )
                    walk(child_path, relative)
                    current = os.stat(
                        name,
                        dir_fd=held.descriptor,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise _invalid(
                            "artifact_closed_world_mismatch",
                            f"foreign bundle directory changed while walking: {relative}",
                        )
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    found.add(relative.as_posix())
                else:
                    raise _invalid(
                        "artifact_closed_world_mismatch",
                        f"foreign bundle entry is unsafe: {relative}",
                    )
            if tuple(
                _bounded_sorted_names(
                    held.descriptor,
                    max_entries=len(before_names),
                    label="foreign artifact bundle",
                )
            ) != before_names:
                raise _invalid(
                    "artifact_closed_world_mismatch",
                    f"foreign bundle membership changed while walking: {directory}",
                )
            names = frozenset(before_names)
            if held.expected_names is not None and held.expected_names != names:
                raise _invalid(
                    "artifact_closed_world_mismatch",
                    f"foreign artifact directory changed while read: {directory}",
                )
            if (
                held.expected_names is None
                and len(self.closed_directories)
                >= _MAX_HELD_COMMIT_CLOSURE_DIRECTORIES
            ):
                raise _invalid(
                    "resource_limit",
                    "foreign artifact commit closure has too many walked directories",
                )
            held.expected_names = names
            self.closed_directories[directory] = names
            self._verify_directory(held)

        walk(normalized_root, PurePosixPath())
        self._tree_files[normalized_root] = frozenset(found)
        self.verify()
        return found

    def verify(self) -> None:
        self._verify_run_anchor()
        for held in self._held_directories.values():
            self._verify_directory(held)
        self._verify_run_anchor()
        for held in self._held_files.values():
            self._verify_file(held)
        for held in self._held_directories.values():
            self._verify_directory(held)

    def freeze(self) -> None:
        if self.run_root / "run.json" not in self.files:
            raise _invalid(
                "source_binding_mismatch",
                "foreign source commit closure did not bind run.json",
            )
        if not self.closed_directories:
            raise _invalid(
                "artifact_closed_world_mismatch",
                "foreign artifact commit closure did not bind any closed-world directory",
            )
        self.verify()
        self.frozen = True

    @contextmanager
    def hold(self) -> Iterator[Callable[[], None]]:
        if not self.frozen:
            raise RuntimeError("foreign source commit closure must be frozen before hold")
        self.verify()
        yield self.verify

    def close(self) -> None:
        for held in self._held_files.values():
            try:
                os.close(held.descriptor)
            except OSError:
                pass
        self._held_files.clear()
        for path in reversed(self._directory_order):
            held = self._held_directories.get(path)
            if held is None:
                continue
            try:
                held.manager.__exit__(None, None, None)
            except BaseException:
                pass
        self._held_directories.clear()
        self._directory_order.clear()


_ACTIVE_ARTIFACT_COMMIT_CLOSURE = ContextVar(
    "paper_reader_batch_artifact_commit_closure",
    default=None,
)


def _opaque(value: object):
    if isinstance(value, dict):
        return _OpaqueRecord(value)
    if isinstance(value, list):
        return tuple(_opaque(item) for item in value)
    return value


def _plain(value: object):
    if isinstance(value, _OpaqueRecord):
        return value.as_dict()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _require_object(value: object, *, code: str, label: str) -> _OpaqueRecord:
    if isinstance(value, _OpaqueRecord):
        return value
    if not isinstance(value, dict):
        raise _invalid(code, f"{label} must be a JSON object")
    return _OpaqueRecord(value)


def _require_string(value: object, *, code: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _invalid(code, f"{label} must be a non-empty string")
    return value


def _require_nonnegative_int(value: object, *, code: str, label: str) -> int:
    if type(value) is not int or value < 0:
        raise _invalid(code, f"{label} must be a non-negative integer")
    return value


def _require_sha256(value: object, *, code: str, label: str) -> str:
    digest = _require_string(value, code=code, label=label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _invalid(code, f"{label} must be a lowercase SHA-256")
    return digest


def _require_sequence(value: object, *, code: str, label: str) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if not isinstance(value, list):
        raise _invalid(code, f"{label} must be a JSON array")
    return tuple(_opaque(item) for item in value)


def _require_inner_ref(value: object, *, code: str = "artifact_binding_mismatch") -> _OpaqueRecord:
    ref = _require_object(value, code=code, label="artifact reference")
    _require_string(ref.role, code=code, label="artifact role")
    _relative_path(_require_string(ref.path, code=code, label="artifact path"))
    _require_sha256(ref.sha256, code=code, label="artifact SHA-256")
    _require_nonnegative_int(ref.size_bytes, code=code, label="artifact size")
    if ref.media_type is not None and not isinstance(ref.media_type, str):
        raise _invalid(code, "artifact media type must be a string or null")
    return ref


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current = ""
        self.parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "h1" and not self.title:
            self.current = "h1"
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.current:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == self.current:
            self.title = " ".join("".join(self.parts).split())
            self.current = ""


def _html_title(content: str) -> str:
    parser = _HeadingParser()
    parser.feed(content)
    parser.close()
    return parser.title


def _require_ref_shape(
    ref: object,
    *,
    role: str,
    path: str | None = None,
    media_type: str,
) -> None:
    ref = _require_inner_ref(ref)
    if ref.role != role or ref.media_type != media_type or (path is not None and ref.path != path):
        raise _invalid("artifact_binding_mismatch", f"artifact ref shape is invalid for {role}")


class _VisibleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_title(value: object) -> str:
    parser = _VisibleParser()
    parser.feed(unescape(str(value)))
    parser.close()
    return " ".join("".join(parser.parts).split())


def _parent_fingerprint(payload: object) -> tuple[str, str, str, int, str]:
    if not isinstance(payload, dict):
        raise _invalid("zotero_snapshot_invalid", "Zotero parent snapshot must be an object")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = str(payload.get("key") or data.get("key") or "").strip()
    title = _visible_title(data.get("title", ""))
    doi = str(data.get("DOI") or "").strip().casefold()
    version = payload.get("version", data.get("version", 0))
    if not key or not title or type(version) is not int or version < 0:
        raise _invalid("zotero_snapshot_invalid", "Zotero parent snapshot identity is invalid")
    fingerprint = canonical_sha256(
        {"key": key, "title": title.casefold(), "DOI": doi, "version": version}
    )
    return key, title, doi, version, fingerprint


def _json_no_nonfinite(raw: bytes, *, code: str) -> object:
    try:
        return json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(code, "foreign JSON artifact is invalid", exc)


def _raw_selected_item(value: object) -> dict[str, object]:
    raw = value
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("result"), dict)
        and isinstance(raw["result"].get("content"), list)
    ):
        raw = raw["result"]["content"]
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("type") == "text":
        text = raw[0].get("text")
        if not isinstance(text, str):
            raise _invalid("source_binding_mismatch", "raw selected-item MCP text is invalid")
        try:
            raw = json.loads(
                text,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise _invalid("source_binding_mismatch", "raw selected-item MCP JSON is invalid", exc)
    if not isinstance(raw, dict):
        raise _invalid("source_binding_mismatch", "raw selected item must resolve to an object")
    return raw


def _normalized_inventory_entry(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid("source_binding_mismatch", f"{label} must be an object")
    key = str(value.get("key") or "").strip()
    title = _visible_title(value.get("title", ""))
    doi = str(value.get("DOI") or "").strip().casefold()
    version = value.get("version", 0)
    if not key or not title or type(version) is not int or version < 0:
        raise _invalid("source_binding_mismatch", f"{label} identity is invalid")
    return {
        "key": key,
        "title": title,
        "normalized_title": title.casefold(),
        "DOI": doi,
        "version": version,
    }


def _validate_zotero_source_snapshots(
    source: _OpaqueRecord,
    raw_bytes: bytes,
    normalized_bytes: bytes,
) -> str:
    raw_payload = _json_no_nonfinite(raw_bytes, code="source_binding_mismatch")
    normalized_payload = _json_no_nonfinite(normalized_bytes, code="source_binding_mismatch")
    if canonical_json_bytes(normalized_payload) != normalized_bytes:
        raise _invalid("source_binding_mismatch", "normalized Zotero source is not canonical JSON")
    if (
        not isinstance(raw_payload, dict)
        or set(raw_payload) != {"search_results", "selected_item"}
        or not isinstance(normalized_payload, dict)
        or set(normalized_payload) != {"format", "search_inventory", "selected_item", "selected_attachment"}
        or normalized_payload.get("format") != "paper_reader.zotero-source.v2-internal"
    ):
        raise _invalid("source_binding_mismatch", "Zotero source snapshots have invalid structure")
    raw_inventory = raw_payload.get("search_results")
    if not isinstance(raw_inventory, list) or not raw_inventory:
        raise _invalid("source_binding_mismatch", "raw Zotero search inventory is empty or invalid")
    normalized_inventory = [
        _normalized_inventory_entry(item, label=f"search_results[{index}]")
        for index, item in enumerate(raw_inventory)
    ]
    keys = [str(item["key"]) for item in normalized_inventory]
    if len(set(keys)) != len(keys):
        raise _invalid("source_binding_mismatch", "raw Zotero search inventory repeats an item key")
    if normalized_payload.get("search_inventory") != normalized_inventory:
        raise _invalid(
            "source_binding_mismatch",
            "raw search_results do not normalize to normalized search_inventory",
        )
    raw_selected = _raw_selected_item(raw_payload.get("selected_item"))
    normalized_selected = normalized_payload.get("selected_item")
    normalized_attachment = normalized_payload.get("selected_attachment")
    if not isinstance(normalized_selected, dict) or not isinstance(normalized_attachment, dict):
        raise _invalid("source_binding_mismatch", "normalized Zotero selected item/attachment is invalid")
    raw_identity = _normalized_inventory_entry(raw_selected, label="raw selected_item")
    selected_identity = _normalized_inventory_entry(normalized_selected, label="normalized selected_item")
    if raw_identity != selected_identity or selected_identity not in normalized_inventory:
        raise _invalid(
            "source_binding_mismatch",
            "selected item identity is not the exact raw/normalized inventory member",
        )
    if sum(
        item["normalized_title"] == selected_identity["normalized_title"]
        for item in normalized_inventory
    ) != 1:
        raise _invalid("source_binding_mismatch", "selected normalized title is not unique in inventory")
    expected_identity = {
        "key": source.item_key,
        "title": source.title,
        "normalized_title": source.title.casefold(),
        "DOI": source.doi,
        "version": source.parent_version,
    }
    if selected_identity != expected_identity:
        raise _invalid("source_binding_mismatch", "normalized selected item differs from run source")
    selected_attachments = normalized_selected.get("attachments")
    if not isinstance(selected_attachments, list):
        raise _invalid("source_binding_mismatch", "normalized selected item attachments are invalid")
    matching_attachments = [
        item
        for item in selected_attachments
        if isinstance(item, dict) and str(item.get("key") or "") == source.attachment_key
    ]
    if (
        len(matching_attachments) != 1
        or matching_attachments[0] != normalized_attachment
        or str(normalized_attachment.get("key") or "") != source.attachment_key
        or str(normalized_attachment.get("path") or "") != source.attachment.resolved_path
    ):
        raise _invalid("source_binding_mismatch", "normalized selected attachment differs from run source")
    raw_attachments = raw_selected.get("attachments")
    raw_matches = [
        item
        for item in raw_attachments
        if isinstance(item, dict) and str(item.get("key") or "") == source.attachment_key
    ] if isinstance(raw_attachments, list) else []
    if len(raw_matches) != 1 or str(raw_matches[0].get("path") or "") != source.attachment.requested_path:
        raise _invalid("source_binding_mismatch", "raw selected attachment differs from run source")
    return sha256_bytes(canonical_json_bytes(raw_inventory))


def _invalid(code: str, message: str, exc: Exception | None = None) -> BatchRuntimeError:
    error = BatchRuntimeError(code, message)
    if exc is not None:
        error.__cause__ = exc
    return error


@contextmanager
def _bound_paper_reader_run_directory(
    run_ref: ArtifactRef,
    expected_identity: FileIdentity,
) -> Iterator[Path]:
    run_path = _require_normalized_absolute_path(
        run_ref.path,
        code="local_prepare_binding_mismatch",
        label="prepared paper_reader run",
    )
    if run_path.name != "run.json":
        raise _invalid(
            "local_prepare_binding_mismatch",
            "prepared paper_reader run path must end in run.json",
        )
    with open_directory_fd(run_path.parent, create=False) as (
        descriptor,
        bound_run_dir,
    ):
        metadata = os.fstat(descriptor)
        if (
            bound_run_dir != run_path.parent
            or metadata.st_dev != expected_identity.device
            or metadata.st_ino != expected_identity.inode
        ):
            raise _invalid(
                "local_prepare_binding_mismatch",
                "paper_reader run directory differs from the prepared stable identity",
            )
        token = _RUN_ARTIFACT_ANCHOR.set((bound_run_dir, descriptor))
        try:
            yield run_path
        finally:
            _RUN_ARTIFACT_ANCHOR.reset(token)


def _require_normalized_absolute_path(value: str, *, code: str, label: str) -> Path:
    normalized = normalized_absolute_path(Path(value))
    if str(normalized) != value:
        raise _invalid(code, f"{label} must be one normalized absolute path")
    return normalized


def _relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or value != candidate.as_posix() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise _invalid("artifact_path_invalid", f"foreign artifact path is not canonical relative: {value}")
    return candidate


def _artifact_ref_read_limit(
    size_bytes: object,
    *,
    code: str,
    json_artifact: bool,
) -> int:
    if type(size_bytes) is not int or size_bytes < 0:
        raise _invalid(code, "artifact reference must declare non-negative integer size_bytes")
    limit = MAX_JSON_ARTIFACT_BYTES if json_artifact else MAX_OPAQUE_ARTIFACT_BYTES
    return min(size_bytes, limit)


def _read_artifact_bytes(path: Path, *, code: str) -> bytes:
    anchor = _RUN_ARTIFACT_ANCHOR.get()
    max_bytes = _ARTIFACT_READ_LIMIT.get()
    normalized = normalized_absolute_path(path)
    collector = _ACTIVE_ARTIFACT_COMMIT_CLOSURE.get()
    if collector is not None and normalized.is_relative_to(collector.run_root):
        return collector.read_and_hold(
            normalized,
            code=code,
            max_bytes=max_bytes,
        )
    if anchor is not None and normalized.is_relative_to(anchor[0]):
        relative = normalized.relative_to(anchor[0]).as_posix()
        raw = read_relative_bytes(anchor[1], relative, code=code, max_bytes=max_bytes)
    else:
        raw = read_bytes(path, code=code, max_bytes=max_bytes)
    return raw


def _read_model(
    path: Path,
    *,
    code: str,
    max_bytes: int = MAX_JSON_ARTIFACT_BYTES,
):
    token = _ARTIFACT_READ_LIMIT.set(max_bytes)
    try:
        raw = _read_artifact_bytes(path, code=code)
    finally:
        _ARTIFACT_READ_LIMIT.reset(token)
    payload = _json_no_nonfinite(raw, code=code)
    if not isinstance(payload, dict):
        raise _invalid(code, f"foreign artifact must be a JSON object: {path}")
    if raw != canonical_json_bytes(payload):
        raise _invalid(code, f"foreign artifact is not canonical JSON: {path}")
    return raw, _OpaqueRecord(payload)


def _read_envelope(
    ref: ArtifactRef,
    *,
    basename: str,
    schema: str,
    id_field: str,
    bind_bytes: bool = True,
):
    path = normalized_absolute_path(Path(ref.path))
    if str(path) != ref.path or path.name != basename:
        raise _invalid("artifact_path_invalid", f"artifact envelope path is not the required {basename}: {ref.path}")
    raw, model = _read_model(
        path,
        code="artifact_invalid",
        max_bytes=(
            _artifact_ref_read_limit(
                ref.size_bytes,
                code="artifact_binding_mismatch",
                json_artifact=True,
            )
            if bind_bytes
            else MAX_JSON_ARTIFACT_BYTES
        ),
    )
    payload = model.as_dict()
    if ref.schema_version != schema:
        raise _invalid("artifact_binding_mismatch", f"artifact envelope declares the wrong schema: {path}")
    if (
        (bind_bytes and (len(raw) != ref.size_bytes or sha256_bytes(raw) != ref.sha256))
        or payload.get("schema_version", payload.get("format")) != schema
        or payload.get(id_field) != ref.artifact_id
    ):
        raise _invalid("artifact_binding_mismatch", f"artifact envelope does not match bytes/identity: {path}")
    return path, raw, model


def _read_inner(
    run_dir: Path,
    ref: object,
    *,
    canonical_json: bool = False,
    code: str = "artifact_invalid",
):
    ref = _require_inner_ref(ref)
    relative = _relative_path(ref.path)
    path = run_dir.joinpath(*relative.parts)
    max_bytes = _artifact_ref_read_limit(
        ref.size_bytes,
        code="artifact_binding_mismatch",
        json_artifact=(canonical_json or ref.media_type == "application/json"),
    )
    token = _ARTIFACT_READ_LIMIT.set(max_bytes)
    try:
        raw = _read_artifact_bytes(path, code=code)
    finally:
        _ARTIFACT_READ_LIMIT.reset(token)
    if len(raw) != ref.size_bytes or sha256_bytes(raw) != ref.sha256:
        raise _invalid("artifact_binding_mismatch", f"foreign artifact reference hash/size mismatch: {ref.path}")
    if not canonical_json:
        return path, raw, None
    payload = _json_no_nonfinite(raw, code=code)
    if not isinstance(payload, dict):
        raise _invalid(code, f"nested artifact must be a JSON object: {ref.path}")
    if raw != canonical_json_bytes(payload):
        raise _invalid(code, f"nested artifact is not canonical JSON: {ref.path}")
    return path, raw, _OpaqueRecord(payload)


def _run_ref_for(
    run: _OpaqueRecord,
    run_dir: Path,
    absolute_path: Path,
    envelope: ArtifactRef,
    role: str,
) -> _OpaqueRecord:
    try:
        expected_relative = absolute_path.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise _invalid("artifact_not_bound", f"paper_reader run {role} path escapes run", exc)
    matches = [
        ref
        for ref in run.artifacts
        if ref.role == role
        and ref.path == expected_relative
        and ref.sha256 == envelope.sha256
        and ref.size_bytes == envelope.size_bytes
        and ref.media_type == "application/json"
    ]
    if len(matches) != 1:
        raise _invalid("artifact_not_bound", f"paper_reader run must bind exactly one {role} digest")
    expected = run_dir / _relative_path(matches[0].path)
    if expected != absolute_path:
        raise _invalid("artifact_not_bound", f"paper_reader run {role} path does not bind supplied artifact")
    return matches[0]


def _source_matches(manifest_item, source: _OpaqueRecord, *, refingerprint: bool = True) -> str | None:
    expected = manifest_item.source
    if isinstance(manifest_item, PdfManifestItem):
        if source.get("source_type") != "local_pdf":
            raise _invalid("source_binding_mismatch", "PDF item requires a local paper_reader source")
        _require_normalized_absolute_path(
            source.resolved_path,
            code="source_binding_mismatch",
            label="paper_reader local source",
        )
        if (
            source.size_bytes <= 0
            or source.resolved_path != expected.path
            or source.sha256 != expected.sha256
            or source.size_bytes != expected.size_bytes
            or source.device != expected.file_identity.device
            or source.inode != expected.file_identity.inode
        ):
            raise _invalid("source_binding_mismatch", "paper_reader local source differs from manifest")
        if refingerprint:
            current = _pdf_source(Path(expected.path))
            if current != expected:
                raise _invalid("source_drift", f"PDF source changed before finish: {expected.path}")
        return None
    if source.get("source_type") != "zotero":
        raise _invalid("source_binding_mismatch", "Zotero item requires a Zotero paper_reader source")
    _require_normalized_absolute_path(
        source.attachment.resolved_path,
        code="source_binding_mismatch",
        label="paper_reader Zotero attachment",
    )
    if source.attachment.size_bytes <= 0:
        raise _invalid("source_binding_mismatch", "paper_reader Zotero attachment must be non-empty")
    if isinstance(manifest_item, ZoteroItemManifestItem):
        if source.item_key != expected.item_key or source.title != expected.title:
            raise _invalid("source_binding_mismatch", "resolved Zotero source differs from manifest")
    elif isinstance(manifest_item, ZoteroTitleManifestItem):
        if expected.resolved_item_key is not None and source.item_key != expected.resolved_item_key:
            raise _invalid("source_binding_mismatch", "resolved Zotero key differs from manifest")
        if " ".join(expected.title.split()).casefold() not in " ".join(source.title.split()).casefold():
            raise _invalid("source_binding_mismatch", "resolved Zotero title does not match title query")
    if refingerprint:
        attachment = _pdf_source(Path(source.attachment.resolved_path))
        if (
            attachment.sha256 != source.attachment.sha256
            or attachment.size_bytes != source.attachment.size_bytes
            or attachment.file_identity.device != source.attachment.device
            or attachment.file_identity.inode != source.attachment.inode
        ):
            raise _invalid("source_drift", "Zotero attachment changed before finish")
    return source.item_key


def _validate_local_run_target(
    manifest_item: PdfManifestItem,
    run_path: Path,
    target: _OpaqueRecord,
    *,
    check_parent_identity: bool,
) -> None:
    source_path = Path(manifest_item.source.path)
    _require_normalized_absolute_path(
        target.resolved_path,
        code="local_prepare_invalid",
        label="paper_reader local target",
    )
    prefix = f"{source_path.stem}_analysis"
    run_name = run_path.parent.name
    if run_name == prefix:
        suffix = ""
    elif run_name.startswith(prefix + "_v") and run_name[len(prefix) + 2 :].isdigit():
        suffix = run_name[len(prefix) :]
    else:
        raise _invalid("local_prepare_invalid", "paper_reader local run directory does not match source/version")
    expected_target = source_path.parent / f"{source_path.stem}_note{suffix}.md"
    if target.resolved_path != str(expected_target):
        raise _invalid("local_prepare_invalid", "paper_reader local target does not match run/source version")
    if check_parent_identity:
        try:
            with open_directory_fd(source_path.parent, create=False) as (
                parent_descriptor,
                _bound_parent,
            ):
                parent_metadata = os.fstat(parent_descriptor)
        except (BatchRuntimeError, OSError) as exc:
            raise _invalid("source_drift", "local source parent is unavailable", exc)
        if (target.parent_device, target.parent_inode) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise _invalid("local_prepare_invalid", "paper_reader local target parent identity changed")


def _walk_regular_files(root: Path) -> set[str]:
    anchor = _RUN_ARTIFACT_ANCHOR.get()
    normalized_root = normalized_absolute_path(root)
    collector = _ACTIVE_ARTIFACT_COMMIT_CLOSURE.get()
    if collector is not None and normalized_root.is_relative_to(collector.run_root):
        return collector.walk_and_hold(normalized_root)
    if anchor is not None and normalized_root.is_relative_to(anchor[0]):
        relative_root = normalized_root.relative_to(anchor[0]).as_posix()
        try:
            found = walk_relative_regular_files(
                anchor[1],
                relative_root,
            )
        except BatchRuntimeError as exc:
            if exc.code == "resource_limit":
                raise
            raise _invalid(
                "artifact_closed_world_mismatch",
                f"foreign bundle cannot be walked through its held run directory: {root}",
                exc,
            ) from exc
    else:
        found = set()
        member_count = 0

        def walk(directory: Path, prefix: PurePosixPath) -> None:
            nonlocal member_count
            with open_directory_fd(directory, create=False) as (descriptor, _normalized):
                before_names = tuple(
                    _bounded_sorted_names(
                        descriptor,
                        max_entries=_MAX_FOREIGN_BUNDLE_MEMBERS - member_count,
                        label="foreign artifact bundle",
                    )
                )
                for name in before_names:
                    member_count += 1
                    if member_count > _MAX_FOREIGN_BUNDLE_MEMBERS:
                        raise _invalid(
                            "resource_limit",
                            "foreign artifact bundle has too many members",
                        )
                    metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    relative = prefix / name
                    if stat.S_ISLNK(metadata.st_mode):
                        raise _invalid("artifact_closed_world_mismatch", f"foreign bundle contains symlink: {relative}")
                    if stat.S_ISDIR(metadata.st_mode):
                        walk(directory / name, relative)
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        found.add(relative.as_posix())
                    else:
                        raise _invalid("artifact_closed_world_mismatch", f"foreign bundle entry is unsafe: {relative}")
                if tuple(
                    _bounded_sorted_names(
                        descriptor,
                        max_entries=len(before_names),
                        label="foreign artifact bundle",
                    )
                ) != before_names:
                    raise _invalid(
                        "artifact_closed_world_mismatch",
                        f"foreign bundle membership changed while walking: {directory}",
                    )
        walk(root, PurePosixPath())
    return found


def _validate_source_directory(
    run_dir: Path,
    source: _OpaqueRecord,
    run_artifacts: tuple[object, ...],
) -> tuple[_OpaqueRecord, bytes] | None:
    source_type = source.get("source_type")
    plan_refs = [
        _require_inner_ref(ref, code="source_binding_mismatch")
        for ref in run_artifacts
        if _require_object(
            ref,
            code="source_binding_mismatch",
            label="run artifact",
        ).get("role")
        == "secondary_source_plan"
    ]
    if source_type == "local_pdf":
        source_refs = [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in run_artifacts
            if _require_object(
                ref,
                code="source_binding_mismatch",
                label="run artifact",
            ).get("role")
            == "source_snapshot"
        ]
        if len(source_refs) != 1:
            raise _invalid(
                "source_binding_mismatch",
                "local paper_reader run must bind exactly one source snapshot ref",
            )
        _require_ref_shape(
            source_refs[0],
            role="source_snapshot",
            path="source/source.json",
            media_type="application/json",
        )
        if plan_refs:
            raise _invalid(
                "source_binding_mismatch",
                "local paper_reader source must not bind a secondary source plan",
            )
        expected = {"source.json"}
        plan_binding = None
    elif source_type == "zotero":
        fixed_source_refs = (
            (
                "raw_discovery_bundle",
                "source/discovery.raw.json",
                _require_inner_ref(
                    source.get("raw_discovery_bundle"),
                    code="source_binding_mismatch",
                ),
            ),
            (
                "normalized_source",
                "source/source.json",
                _require_inner_ref(
                    source.get("normalized_source"),
                    code="source_binding_mismatch",
                ),
            ),
        )
        for role, path, embedded_ref in fixed_source_refs:
            _require_ref_shape(
                embedded_ref,
                role=role,
                path=path,
                media_type="application/json",
            )
            role_refs = [
                _require_inner_ref(ref, code="source_binding_mismatch")
                for ref in run_artifacts
                if _require_object(
                    ref,
                    code="source_binding_mismatch",
                    label="run artifact",
                ).get("role")
                == role
            ]
            if role_refs != [embedded_ref]:
                raise _invalid(
                    "source_binding_mismatch",
                    "Zotero paper_reader run must retain exact singleton source refs",
                )
        if len(plan_refs) > 1:
            raise _invalid(
                "source_binding_mismatch",
                "Zotero paper_reader run must bind at most one secondary source plan",
            )
        expected = {"discovery.raw.json", "source.json"}
        plan_binding = None
        if plan_refs:
            plan_ref = plan_refs[0]
            _require_ref_shape(
                plan_ref,
                role="secondary_source_plan",
                path="source/secondary-plan.json",
                media_type="application/json",
            )
            if plan_ref.size_bytes > MAX_JSON_ARTIFACT_BYTES:
                raise _invalid(
                    "resource_limit",
                    "secondary source plan exceeds the JSON artifact limit",
                )
            try:
                plan_path, plan_bytes, _model = _read_inner(
                    run_dir,
                    plan_ref,
                    canonical_json=True,
                    code="source_binding_mismatch",
                )
            except BatchRuntimeError as exc:
                if exc.code == "resource_limit":
                    raise
                raise _invalid(
                    "source_binding_mismatch",
                    "secondary source plan failed exact ref verification",
                    exc,
                ) from exc
            if plan_path != run_dir / "source" / "secondary-plan.json":
                raise _invalid(
                    "source_binding_mismatch",
                    "secondary source plan path does not match its fixed source member",
                )
            expected.add("secondary-plan.json")
            plan_binding = (plan_ref, plan_bytes)
    else:
        raise _invalid(
            "source_binding_mismatch",
            "paper_reader source type is unsupported",
        )
    if _walk_regular_files(run_dir / "source") != expected:
        raise _invalid(
            "artifact_closed_world_mismatch",
            "paper_reader source directory is not its exact immutable closure",
        )
    if plan_binding is not None:
        plan_ref, plan_bytes = plan_binding
        try:
            rechecked_path, rechecked_bytes, _model = _read_inner(
                run_dir,
                plan_ref,
                canonical_json=True,
                code="source_binding_mismatch",
            )
        except BatchRuntimeError as exc:
            if exc.code == "resource_limit":
                raise
            raise _invalid(
                "source_binding_mismatch",
                "secondary source plan changed during source closure validation",
                exc,
            ) from exc
        if (
            rechecked_path != run_dir / "source" / "secondary-plan.json"
            or rechecked_bytes != plan_bytes
        ):
            raise _invalid(
                "source_binding_mismatch",
                "secondary source plan changed during source closure validation",
            )
    return plan_binding


def _validate_evidence(
    run_dir: Path,
    evidence_path: Path,
    evidence: _OpaqueRecord,
    source_sha256: str,
    secondary_plan_binding: tuple[_OpaqueRecord, bytes] | None,
) -> None:
    if not evidence.complete or evidence.preview_pages is not None:
        raise _invalid("evidence_incomplete", "batch accepts only complete, non-preview evidence")
    if evidence.source_sha256 != source_sha256:
        raise _invalid("evidence_binding_mismatch", "evidence source digest differs from paper_reader source")
    expected_members: set[str] = set()
    evidence_dir = evidence_path.parent
    evidence_refs = tuple(
        _require_inner_ref(raw_ref, code="evidence_binding_mismatch")
        for raw_ref in _require_sequence(
            evidence.files,
            code="evidence_binding_mismatch",
            label="evidence files",
        )
    )
    secondary_plan_members: list[tuple[_OpaqueRecord, Path, bytes]] = []
    secondary_inventory_members: list[tuple[_OpaqueRecord, str, bytes]] = []
    reserved_secondary_paths = {
        "secondary-plan.json",
        "secondary_context.md",
    }
    for ref in evidence_refs:
        member_path, raw, _model = _read_inner(run_dir, ref)
        try:
            relative = member_path.relative_to(evidence_dir).as_posix()
        except ValueError as exc:
            raise _invalid("evidence_binding_mismatch", "evidence member is outside its immutable bundle", exc)
        if relative == "evidence.json" or relative in expected_members:
            raise _invalid("evidence_binding_mismatch", "evidence manifest has duplicate/recursive member")
        expected_members.add(relative)
        if ref.role == "secondary_plan":
            secondary_plan_members.append((ref, member_path, raw))
        if ref.role == "secondary_sources":
            secondary_inventory_members.append((ref, relative, raw))
        if secondary_plan_binding is None and (
            ref.role in {"secondary_plan", "secondary_capture", "secondary_context"}
            or relative in reserved_secondary_paths
            or relative.startswith("secondary/")
        ):
            raise _invalid(
                "evidence_binding_mismatch",
                "no-plan paper_reader closure contains versioned secondary evidence",
            )
    if len(secondary_inventory_members) != 1:
        raise _invalid(
            "evidence_binding_mismatch",
            "evidence must bind exactly one secondary source inventory",
        )
    inventory_ref, inventory_relative, inventory_raw = secondary_inventory_members[0]
    inventory_payload = _json_no_nonfinite(
        inventory_raw,
        code="evidence_binding_mismatch",
    )
    if (
        inventory_relative != "secondary_sources.json"
        or inventory_ref.media_type != "application/json"
        or canonical_json_bytes(inventory_payload) != inventory_raw
    ):
        raise _invalid(
            "evidence_binding_mismatch",
            "secondary source inventory ref/path/bytes are not canonical",
        )
    declares_inventory_format = (
        isinstance(inventory_payload, dict) and "format" in inventory_payload
    )
    versioned_inventory = declares_inventory_format and (
        inventory_payload.get("format")
        == "paper_reader.secondary-sources.v2-internal"
    )
    if secondary_plan_binding is None and declares_inventory_format:
        raise _invalid(
            "evidence_binding_mismatch",
            "no-plan paper_reader closure contains a versioned secondary inventory",
        )
    if secondary_plan_binding is not None:
        if not versioned_inventory:
            raise _invalid(
                "evidence_binding_mismatch",
                "plan-bound evidence lacks its versioned secondary inventory",
            )
        if len(secondary_plan_members) != 1:
            raise _invalid(
                "evidence_binding_mismatch",
                "plan-bound evidence must bind exactly one secondary plan member",
            )
        source_plan_ref, source_plan_bytes = secondary_plan_binding
        evidence_plan_ref, evidence_plan_path, evidence_plan_bytes = secondary_plan_members[0]
        expected_plan_path = evidence_dir / "secondary-plan.json"
        expected_plan_relative = expected_plan_path.relative_to(run_dir).as_posix()
        if (
            evidence_plan_ref.role != "secondary_plan"
            or evidence_plan_ref.path != expected_plan_relative
            or evidence_plan_ref.media_type != "application/json"
        ):
            raise _invalid(
                "evidence_binding_mismatch",
                "evidence secondary plan ref has the wrong role, path, or media type",
            )
        if (
            evidence_plan_path != expected_plan_path
            or evidence_plan_bytes != source_plan_bytes
            or evidence_plan_ref.sha256 != source_plan_ref.sha256
            or evidence_plan_ref.size_bytes != source_plan_ref.size_bytes
        ):
            raise _invalid(
                "evidence_binding_mismatch",
                "evidence secondary plan differs from the run-bound source plan",
            )
    actual = _walk_regular_files(evidence_dir)
    actual.discard("evidence.json")
    if actual != expected_members:
        raise _invalid("artifact_closed_world_mismatch", "evidence bundle membership differs from manifest")
    _inventory_path, rechecked_inventory_raw, _inventory_model = _read_inner(
        run_dir,
        inventory_ref,
        canonical_json=True,
        code="evidence_binding_mismatch",
    )
    if rechecked_inventory_raw != inventory_raw:
        raise _invalid(
            "evidence_binding_mismatch",
            "secondary source inventory changed during evidence closure validation",
        )
    if secondary_plan_binding is not None:
        source_plan_ref, source_plan_bytes = secondary_plan_binding
        try:
            _source_plan_path, rechecked_source_plan, _source_plan_model = _read_inner(
                run_dir,
                source_plan_ref,
                canonical_json=True,
                code="source_binding_mismatch",
            )
        except BatchRuntimeError as exc:
            if exc.code == "resource_limit":
                raise
            raise _invalid(
                "source_binding_mismatch",
                "secondary source plan changed during evidence closure validation",
                exc,
            ) from exc
        (
            evidence_plan_ref,
            _evidence_plan_path,
            original_evidence_plan,
        ) = secondary_plan_members[0]
        _plan_path, rechecked_evidence_plan, _plan_model = _read_inner(
            run_dir,
            evidence_plan_ref,
            canonical_json=True,
            code="evidence_binding_mismatch",
        )
        if (
            rechecked_source_plan != source_plan_bytes
            or rechecked_evidence_plan != original_evidence_plan
            or rechecked_evidence_plan != rechecked_source_plan
        ):
            raise _invalid(
                "evidence_binding_mismatch",
                "source/evidence secondary plan changed during final closure validation",
            )


def _canonical_snapshot(
    raw: bytes,
    *,
    code: str,
    tag_field: str,
    tag_value: str,
) -> _OpaqueRecord:
    payload = _json_no_nonfinite(raw, code=code)
    if not isinstance(payload, dict):
        raise _invalid(code, "single-paper snapshot must be a JSON object")
    if payload.get(tag_field) != tag_value:
        raise _invalid("unsupported_run_schema", f"single-paper snapshot must use {tag_value}")
    if raw != canonical_json_bytes(payload):
        raise _invalid(code, "single-paper snapshot must be canonical JSON")
    return _OpaqueRecord(payload)


def _validated_gate(
    record: object,
    *,
    status: str,
    required_proofs: frozenset[str],
    code: str,
) -> tuple[str, ...]:
    gate = _require_object(record, code=code, label="sealed gate")
    if gate.get("status") != status:
        raise _invalid(code, f"sealed gate must have status {status}")
    blockers = _require_sequence(gate.get("blockers"), code=code, label="gate blockers")
    checks = _require_sequence(gate.get("checks"), code=code, label="gate checks")
    if blockers or not checks or any(not isinstance(check, str) or not check for check in checks):
        raise _invalid(code, "sealed gate must have non-empty string checks and no blockers")
    normalized = tuple(checks)
    if len(set(normalized)) != len(normalized) or not required_proofs.issubset(normalized):
        raise _invalid(code, "sealed gate is missing or repeats a consumer-required proof")
    return normalized


def _markdown_body(raw: bytes, *, code: str) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(code, "candidate Markdown must be UTF-8", exc)
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith("# "):
        raise _invalid(code, "candidate Markdown must begin with an H1")
    return lines[0][2:].strip(), "".join(lines[1:])


def _html_body(raw: bytes, *, code: str) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(code, "candidate HTML must be UTF-8", exc)
    closing = text.casefold().find("</h1>")
    title = _html_title(text)
    if closing < 0 or not title:
        raise _invalid(code, "candidate HTML must contain one visible H1")
    return title, text[closing + len("</h1>") :]


def _validate_review_and_candidate(
    manifest_item,
    run_path: Path,
    run: _OpaqueRecord,
    review_ref: ArtifactRef,
    candidate_ref: ArtifactRef,
    secondary_plan_binding: tuple[_OpaqueRecord, bytes] | None,
    refingerprint: bool = True,
):
    run_dir = run_path.parent
    review_path, review_raw, package = _read_envelope(
        review_ref,
        basename="review-package.json",
        schema="paper_reader.review-package.v2",
        id_field="review_package_id",
    )
    candidate_path, _candidate_raw, candidate = _read_envelope(
        candidate_ref,
        basename="candidate.json",
        schema="paper_reader.candidate.v2",
        id_field="candidate_id",
    )
    review_id = _require_string(
        package.get("review_package_id"),
        code="review_not_sealed",
        label="review package id",
    )
    candidate_id = _require_string(
        candidate.get("candidate_id"),
        code="candidate_binding_mismatch",
        label="candidate id",
    )
    if (
        review_path != run_dir / "reviews" / review_id / "review-package.json"
        or candidate_path != run_dir / "candidates" / candidate_id / "candidate.json"
    ):
        raise _invalid("artifact_path_invalid", "review/candidate belongs to another run directory")
    if package.get("run_id") != run.run_id or candidate.get("run_id") != run.run_id:
        raise _invalid("artifact_binding_mismatch", "review/candidate run id differs from paper_reader run")
    _run_ref_for(run, run_dir, review_path, review_ref, "review_package")
    _run_ref_for(run, run_dir, candidate_path, candidate_ref, "candidate")

    package_checks = _validated_gate(
        package.get("gate"),
        status="passed",
        required_proofs=_REQUIRED_REVIEW_PROOFS,
        code="review_not_sealed",
    )
    target = _require_object(
        candidate.get("target"),
        code="candidate_binding_mismatch",
        label="candidate target",
    )
    target_type = target.get("target_type")
    candidate_proofs = (
        _REQUIRED_LOCAL_CANDIDATE_PROOFS
        if target_type == "local"
        else _REQUIRED_ZOTERO_CANDIDATE_PROOFS
        if target_type == "zotero"
        else frozenset({"unsupported_target"})
    )
    _validated_gate(
        candidate.get("gate"),
        status="write_ready",
        required_proofs=candidate_proofs,
        code="candidate_not_write_ready",
    )

    review_names = {
        "summary.json": ("summary_snapshot", "application/json"),
        "review.json": ("review_snapshot", "application/json"),
        "evidence.json": ("evidence_manifest_snapshot", "application/json"),
        "validation.json": ("review_validation", "application/json"),
        "note.md": ("review_note_markdown", "text/markdown"),
        "note.html": ("review_note_html", "text/html"),
    }
    if _walk_regular_files(review_path.parent) != {*review_names, "review-package.json"}:
        raise _invalid("artifact_closed_world_mismatch", "sealed review directory is not its fixed closure")
    package_artifacts = _require_sequence(
        package.get("artifacts"),
        code="review_not_sealed",
        label="review package artifacts",
    )
    if len(package_artifacts) != len(review_names):
        raise _invalid("review_not_sealed", "review package artifact count differs from its fixed closure")
    review_snapshots: dict[str, bytes] = {}
    review_refs: dict[str, _OpaqueRecord] = {}
    for name, (role, media_type) in review_names.items():
        matches = [
            _require_inner_ref(ref, code="review_not_sealed")
            for ref in package_artifacts
            if _require_object(ref, code="review_not_sealed", label="review ref").get("role") == role
        ]
        if len(matches) != 1:
            raise _invalid("review_not_sealed", f"review package must bind one {role}")
        ref = matches[0]
        _require_ref_shape(
            ref,
            role=role,
            path=(review_path.parent / name).relative_to(run_dir).as_posix(),
            media_type=media_type,
        )
        path, raw, _ = _read_inner(run_dir, ref)
        if path != review_path.parent / name:
            raise _invalid("review_not_sealed", f"review package {role} path is not fixed")
        review_snapshots[name] = raw
        review_refs[role] = ref
    if (
        package.get("summary") != review_refs["summary_snapshot"]
        or package.get("review") != review_refs["review_snapshot"]
        or package.get("evidence_manifest") != review_refs["evidence_manifest_snapshot"]
    ):
        raise _invalid("review_not_sealed", "review package primary refs differ from closure refs")

    summary_raw = review_snapshots["summary.json"]
    review_json_raw = review_snapshots["review.json"]
    validation_raw = review_snapshots["validation.json"]
    note_md = review_snapshots["note.md"]
    note_html = review_snapshots["note.html"]
    summary = _canonical_snapshot(
        summary_raw,
        code="review_invalid",
        tag_field="schema_version",
        tag_value="paper_reader.summary.v2",
    )
    review_json = _canonical_snapshot(
        review_json_raw,
        code="review_invalid",
        tag_field="schema_version",
        tag_value="paper_reader.review.v2",
    )
    validation = _canonical_snapshot(
        validation_raw,
        code="review_invalid",
        tag_field="format",
        tag_value="paper_reader.review-validation.v2-internal",
    )
    package_summary_sha = _require_sha256(
        package.get("summary_sha256"),
        code="review_not_sealed",
        label="package summary digest",
    )
    package_review_sha = _require_sha256(
        package.get("review_sha256"),
        code="review_not_sealed",
        label="package review digest",
    )
    package_evidence_sha = _require_sha256(
        package.get("evidence_digest"),
        code="review_not_sealed",
        label="package evidence digest",
    )
    validation_checks = _require_sequence(
        validation.get("checks"),
        code="review_not_sealed",
        label="sealed validation checks",
    )
    validation_blockers = _require_sequence(
        validation.get("blockers"),
        code="review_not_sealed",
        label="sealed validation blockers",
    )
    if (
        summary.get("run_id") != run.run_id
        or summary.get("evidence_digest") != package_evidence_sha
        or review_json.get("run_id") != run.run_id
        or review_json.get("summary_sha256") != package_summary_sha
        or review_json.get("evidence_digest") != package_evidence_sha
        or sha256_bytes(summary_raw) != package_summary_sha
        or sha256_bytes(review_json_raw) != package_review_sha
        or validation.get("run_id") != run.run_id
        or validation.get("summary_sha256") != package_summary_sha
        or validation.get("review_sha256") != package_review_sha
        or validation.get("evidence_digest") != package_evidence_sha
        or validation.get("rendered_note_sha256") != sha256_bytes(note_md)
        or validation.get("rendered_html_sha256") != sha256_bytes(note_html)
        or validation_blockers
        or tuple(validation_checks) != package_checks
    ):
        raise _invalid("review_not_sealed", "sealed validation hash closure is inconsistent")

    evidence_raw = review_snapshots["evidence.json"]
    evidence = _canonical_snapshot(
        evidence_raw,
        code="evidence_invalid",
        tag_field="format",
        tag_value="paper_reader.evidence.v2-internal",
    )
    if (
        evidence.get("run_id") != run.run_id
        or package_evidence_sha != review_refs["evidence_manifest_snapshot"].sha256
    ):
        raise _invalid("evidence_binding_mismatch", "sealed review evidence identity mismatch")

    candidate_names = {
        "run.json": ("run_snapshot", "application/json"),
        "source.json": ("source_snapshot", "application/json"),
        "evidence.json": ("evidence_manifest_snapshot", "application/json"),
        "summary.json": ("summary_snapshot", "application/json"),
        "review.json": ("review_snapshot", "application/json"),
        "review-package.json": ("review_package_snapshot", "application/json"),
        "validation.json": ("review_validation", "application/json"),
        "note.md": ("note_markdown", "text/markdown"),
        "note.html": ("note_html", "text/html"),
    }
    if target_type == "zotero":
        candidate_names.update(
            {
                "discovery.raw.json": ("raw_discovery_bundle_snapshot", "application/json"),
                "parent.json": ("zotero_parent_snapshot", "application/json"),
                "children.json": ("zotero_children_snapshot", "application/json"),
            }
        )
    elif target_type != "local":
        raise _invalid("candidate_binding_mismatch", "candidate target type is unsupported")

    candidate_artifacts = _require_sequence(
        candidate.get("artifacts"),
        code="candidate_binding_mismatch",
        label="candidate artifacts",
    )
    if (
        _walk_regular_files(candidate_path.parent) != {*candidate_names, "candidate.json"}
        or len(candidate_artifacts) != len(candidate_names)
    ):
        raise _invalid("artifact_closed_world_mismatch", "candidate directory differs from its fixed closure")
    candidate_snapshots: dict[str, bytes] = {}
    candidate_refs: dict[str, _OpaqueRecord] = {}
    for name, (role, media_type) in candidate_names.items():
        matches = [
            _require_inner_ref(ref, code="candidate_binding_mismatch")
            for ref in candidate_artifacts
            if _require_object(ref, code="candidate_binding_mismatch", label="candidate ref").get("role")
            == role
        ]
        if len(matches) != 1:
            raise _invalid("candidate_binding_mismatch", f"candidate must bind one {role}")
        ref = matches[0]
        _require_ref_shape(
            ref,
            role=role,
            path=(candidate_path.parent / name).relative_to(run_dir).as_posix(),
            media_type=media_type,
        )
        path, raw, _ = _read_inner(run_dir, ref)
        if path != candidate_path.parent / name:
            raise _invalid("candidate_binding_mismatch", f"candidate {role} path is not fixed")
        candidate_snapshots[name] = raw
        candidate_refs[role] = ref

    if (
        candidate.get("sealed_review") != candidate_refs["review_package_snapshot"]
        or candidate.get("evidence_manifest") != candidate_refs["evidence_manifest_snapshot"]
        or candidate_snapshots["review-package.json"] != review_raw
        or candidate_snapshots["evidence.json"] != evidence_raw
        or candidate_snapshots["summary.json"] != summary_raw
        or candidate_snapshots["review.json"] != review_json_raw
        or candidate_snapshots["validation.json"] != validation_raw
    ):
        raise _invalid("candidate_binding_mismatch", "candidate snapshots differ from sealed review package")
    if (
        candidate_refs["review_package_snapshot"].sha256 != review_ref.sha256
        or candidate_refs["evidence_manifest_snapshot"].sha256 != package_evidence_sha
    ):
        raise _invalid("candidate_binding_mismatch", "candidate does not bind supplied review/evidence")

    run_snapshot = _canonical_snapshot(
        candidate_snapshots["run.json"],
        code="candidate_binding_mismatch",
        tag_field="schema_version",
        tag_value="paper_reader.run.v2",
    )
    source = _require_object(
        candidate.get("source"),
        code="candidate_binding_mismatch",
        label="candidate source",
    )
    source_type = source.get("source_type")
    if (
        run_snapshot.get("run_id") != run.run_id
        or run_snapshot.get("source") != source
        or run_snapshot.get("status") != "reviewed"
        or run_snapshot.get("gate") != package.get("gate")
        or run_snapshot.get("live_preflight") is not None
        or (source_type == "local_pdf" and run_snapshot.get("target") != target)
        or (source_type == "zotero" and run_snapshot.get("target") is not None)
    ):
        raise _invalid("candidate_binding_mismatch", "candidate run snapshot does not bind source/review")
    _run_ref_for(run_snapshot, run_dir, review_path, review_ref, "review_package")

    run_snapshot_artifacts = _require_sequence(
        run_snapshot.get("artifacts"),
        code="evidence_binding_mismatch",
        label="run snapshot artifacts",
    )
    evidence_refs = [
        _require_inner_ref(ref, code="evidence_binding_mismatch")
        for ref in run_snapshot_artifacts
        if _require_object(ref, code="evidence_binding_mismatch", label="run ref").get("role")
        == "evidence_manifest"
        and _require_object(ref, code="evidence_binding_mismatch", label="run ref").get("sha256")
        == package_evidence_sha
    ]
    if len(evidence_refs) != 1:
        raise _invalid("evidence_binding_mismatch", "candidate run snapshot must bind canonical evidence")
    evidence_path, canonical_evidence_raw, canonical_evidence = _read_inner(
        run_dir,
        evidence_refs[0],
        canonical_json=True,
        code="evidence_invalid",
    )
    if canonical_evidence_raw != evidence_raw or canonical_evidence != evidence:
        raise _invalid("evidence_binding_mismatch", "sealed evidence differs from canonical evidence")
    current_run_artifacts = _require_sequence(
        run.get("artifacts"),
        code="evidence_binding_mismatch",
        label="current run artifacts",
    )
    if [ref for ref in current_run_artifacts if ref == evidence_refs[0]] != [evidence_refs[0]]:
        raise _invalid("evidence_binding_mismatch", "current run does not retain canonical evidence")

    run_snapshot_plan_refs = [
        _require_inner_ref(ref, code="source_binding_mismatch")
        for ref in run_snapshot_artifacts
        if _require_object(
            ref,
            code="source_binding_mismatch",
            label="candidate run snapshot artifact",
        ).get("role")
        == "secondary_source_plan"
    ]
    if secondary_plan_binding is None:
        if run_snapshot_plan_refs:
            raise _invalid(
                "source_binding_mismatch",
                "candidate run snapshot adds a secondary plan absent from the current run",
            )
    else:
        source_plan_ref, _source_plan_bytes = secondary_plan_binding
        if run_snapshot_plan_refs != [source_plan_ref]:
            raise _invalid(
                "source_binding_mismatch",
                "candidate run snapshot does not retain the current secondary plan ref",
            )

    source_sha = (
        _require_sha256(source.get("sha256"), code="source_binding_mismatch", label="source digest")
        if source_type == "local_pdf"
        else _require_sha256(
            _require_object(
                source.get("attachment"),
                code="source_binding_mismatch",
                label="Zotero attachment",
            ).get("sha256"),
            code="source_binding_mismatch",
            label="attachment digest",
        )
    )
    _validate_evidence(
        run_dir,
        evidence_path,
        canonical_evidence,
        source_sha,
        secondary_plan_binding,
    )
    if run.get("source") != source:
        raise _invalid("candidate_binding_mismatch", "current run source differs from candidate")
    if refingerprint and (
        run.get("target") != target
        or run.get("gate") != candidate.get("gate")
        or run.get("live_preflight") != candidate.get("live_preflight")
    ):
        raise _invalid("candidate_binding_mismatch", "current run target/gate differs from candidate")

    tags = _require_sequence(
        candidate.get("tags"),
        code="candidate_binding_mismatch",
        label="candidate tags",
    )
    if not tags or any(not isinstance(tag, str) or not tag for tag in tags) or len(set(tags)) != len(tags):
        raise _invalid("candidate_binding_mismatch", "candidate tags must be unique non-empty strings")
    note_title = _require_string(
        candidate.get("note_title"),
        code="candidate_binding_mismatch",
        label="candidate title",
    )
    note_md_bytes = candidate_snapshots["note.md"]
    note_html_bytes = candidate_snapshots["note.html"]
    markdown_title, _markdown_body_text = _markdown_body(
        note_md_bytes,
        code="candidate_binding_mismatch",
    )
    html_title, _html_body_text = _html_body(
        note_html_bytes,
        code="candidate_binding_mismatch",
    )
    content_sha = _require_sha256(
        candidate.get("content_sha256"),
        code="candidate_binding_mismatch",
        label="candidate content digest",
    )
    content_length = _require_nonnegative_int(
        candidate.get("content_length"),
        code="candidate_binding_mismatch",
        label="candidate content length",
    )

    resolved_key = _source_matches(manifest_item, source, refingerprint=refingerprint)
    if isinstance(manifest_item, PdfManifestItem):
        if source_type != "local_pdf" or target_type != "local":
            raise _invalid("candidate_binding_mismatch", "local item requires local source/target")
        if (
            sha256_bytes(note_md_bytes) != content_sha
            or len(note_md_bytes) != content_length
            or markdown_title != note_title
            or note_md_bytes != review_snapshots["note.md"]
            or note_html_bytes != review_snapshots["note.html"]
        ):
            raise _invalid("candidate_binding_mismatch", "local candidate note closure is invalid")
        original_source_refs = [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in run_snapshot_artifacts
            if _require_object(ref, code="source_binding_mismatch", label="source ref").get("role")
            == "source_snapshot"
        ]
        if len(original_source_refs) != 1:
            raise _invalid("source_binding_mismatch", "local run must bind one source snapshot")
        if [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in current_run_artifacts
            if _require_object(
                ref,
                code="source_binding_mismatch",
                label="current local source ref",
            ).get("role")
            == "source_snapshot"
        ] != [original_source_refs[0]]:
            raise _invalid("source_binding_mismatch", "current local run dropped its source snapshot")
        _source_path, original_source_raw, original_source = _read_inner(
            run_dir,
            original_source_refs[0],
            canonical_json=True,
            code="source_binding_mismatch",
        )
        if original_source_raw != candidate_snapshots["source.json"] or original_source != source:
            raise _invalid("source_binding_mismatch", "local source snapshot differs from candidate")
        _validate_local_run_target(
            manifest_item,
            run_path,
            target,
            check_parent_identity=refingerprint,
        )
        return (
            review_path,
            package,
            candidate_path,
            candidate,
            evidence_path,
            canonical_evidence,
            resolved_key,
            None,
        )

    if source_type != "zotero" or target_type != "zotero" or target.get("parent_key") != resolved_key:
        raise _invalid("candidate_binding_mismatch", "Zotero candidate source/parent differs from manifest")
    live = _require_object(
        candidate.get("live_preflight"),
        code="candidate_binding_mismatch",
        label="candidate live preflight",
    )
    if (
        (refingerprint and run.get("live_preflight") != live)
        or live.get("parent_key") != resolved_key
        or live.get("parent_fingerprint") != source.get("parent_fingerprint")
        or target.get("parent_fingerprint") != source.get("parent_fingerprint")
        or live.get("requested_note_title") != note_title
        or target.get("note_title") != note_title
        or live.get("title_available") is not True
        or _require_sequence(
            live.get("matching_note_keys"),
            code="candidate_binding_mismatch",
            label="matching note keys",
        )
        or live.get("parent_snapshot") != candidate_refs["zotero_parent_snapshot"]
        or live.get("children_snapshot") != candidate_refs["zotero_children_snapshot"]
    ):
        raise _invalid("candidate_binding_mismatch", "Zotero live parent/title binding is invalid")
    canonical_html = note_html_bytes.decode("utf-8").rstrip("\r\n")
    if (
        sha256_bytes(canonical_html.encode("utf-8")) != content_sha
        or len(canonical_html) != content_length
        or html_title != note_title
    ):
        raise _invalid(
            "candidate_binding_mismatch",
            "Zotero candidate must bind its exact visible H1 and canonical HTML content",
        )

    parent_payload = _json_no_nonfinite(
        candidate_snapshots["parent.json"],
        code="zotero_snapshot_invalid",
    )
    children_payload = _json_no_nonfinite(
        candidate_snapshots["children.json"],
        code="zotero_snapshot_invalid",
    )
    if (
        canonical_json_bytes(parent_payload) != candidate_snapshots["parent.json"]
        or canonical_json_bytes(children_payload) != candidate_snapshots["children.json"]
        or not isinstance(children_payload, list)
    ):
        raise _invalid("zotero_snapshot_invalid", "Zotero parent/children snapshots are not canonical")
    parent_key, parent_title, parent_doi, parent_version, parent_digest = _parent_fingerprint(parent_payload)
    if (
        parent_key != source.get("item_key")
        or parent_title != source.get("title")
        or parent_doi != source.get("doi")
        or parent_version != source.get("parent_version")
        or parent_digest != source.get("parent_fingerprint")
    ):
        raise _invalid("zotero_snapshot_invalid", "Zotero parent snapshot differs from source identity")
    exact_matches: list[str] = []
    for child in children_payload:
        if not isinstance(child, dict):
            raise _invalid("zotero_snapshot_invalid", "Zotero children snapshot contains a non-object")
        child_data = child.get("data")
        if not isinstance(child_data, dict) or child_data.get("itemType") != "note":
            continue
        child_key = str(child.get("key") or child_data.get("key") or "").strip()
        if not child_key or str(child_data.get("parentItem") or "").strip() != resolved_key:
            raise _invalid("zotero_snapshot_invalid", "Zotero note child identity/parent is invalid")
        if _html_title(str(child_data.get("note") or "")) == note_title:
            exact_matches.append(child_key)
    if exact_matches:
        raise _invalid("candidate_binding_mismatch", "candidate title is unavailable in children snapshot")

    source_raw_ref = _require_inner_ref(
        source.get("raw_discovery_bundle"),
        code="source_binding_mismatch",
    )
    source_normalized_ref = _require_inner_ref(
        source.get("normalized_source"),
        code="source_binding_mismatch",
    )
    _require_ref_shape(
        source_raw_ref,
        role="raw_discovery_bundle",
        path="source/discovery.raw.json",
        media_type="application/json",
    )
    _require_ref_shape(
        source_normalized_ref,
        role="normalized_source",
        path="source/source.json",
        media_type="application/json",
    )
    if (
        [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in run_snapshot_artifacts
            if _require_object(
                ref,
                code="source_binding_mismatch",
                label="candidate raw source ref",
            ).get("role")
            == "raw_discovery_bundle"
        ]
        != [source_raw_ref]
        or [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in run_snapshot_artifacts
            if _require_object(
                ref,
                code="source_binding_mismatch",
                label="candidate normalized source ref",
            ).get("role")
            == "normalized_source"
        ]
        != [source_normalized_ref]
        or [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in current_run_artifacts
            if _require_object(
                ref,
                code="source_binding_mismatch",
                label="current raw source ref",
            ).get("role")
            == "raw_discovery_bundle"
        ]
        != [source_raw_ref]
        or [
            _require_inner_ref(ref, code="source_binding_mismatch")
            for ref in current_run_artifacts
            if _require_object(
                ref,
                code="source_binding_mismatch",
                label="current normalized source ref",
            ).get("role")
            == "normalized_source"
        ]
        != [source_normalized_ref]
    ):
        raise _invalid("source_binding_mismatch", "Zotero run does not retain source closure")
    raw_bytes = candidate_snapshots["discovery.raw.json"]
    normalized_bytes = candidate_snapshots["source.json"]
    raw_path, original_raw, _ = _read_inner(run_dir, source_raw_ref)
    normalized_path, original_normalized, _ = _read_inner(run_dir, source_normalized_ref)
    if (
        raw_path != run_dir / "source" / "discovery.raw.json"
        or normalized_path != run_dir / "source" / "source.json"
        or original_raw != raw_bytes
        or original_normalized != normalized_bytes
        or sha256_bytes(raw_bytes) != source_raw_ref.sha256
        or len(raw_bytes) != source_raw_ref.size_bytes
        or sha256_bytes(normalized_bytes) != source_normalized_ref.sha256
        or len(normalized_bytes) != source_normalized_ref.size_bytes
    ):
        raise _invalid("source_binding_mismatch", "candidate Zotero source snapshots differ from source refs")
    inventory_sha256 = _validate_zotero_source_snapshots(source, raw_bytes, normalized_bytes)
    return (
        review_path,
        package,
        candidate_path,
        candidate,
        evidence_path,
        canonical_evidence,
        resolved_key,
        inventory_sha256,
    )


def _validate_prepared_local_continuation(
    result: WorkerResult,
    prepared: LocalPrepareResult,
    *,
    run_path: Path,
    run: _OpaqueRecord,
    package: _OpaqueRecord,
    candidate: _OpaqueRecord,
    canonical_evidence_path: Path,
    canonical_evidence: _OpaqueRecord,
) -> None:
    if prepared.status != "prepared" or prepared.paper_reader_run is None or prepared.evidence is None:
        raise _invalid(
            "local_prepare_binding_mismatch",
            "worker success requires a complete prepared local result binding",
        )
    assert result.paper_reader_run is not None
    if (
        prepared.manifest_sha256 != result.manifest_sha256
        or prepared.item_id != result.item_id
        or prepared.source != result.source
        or prepared.paper_reader_run.path != result.paper_reader_run.path
        or prepared.paper_reader_run.schema_version != result.paper_reader_run.schema_version
        or prepared.paper_reader_run.artifact_id != result.paper_reader_run.artifact_id
        or run.run_id != prepared.paper_reader_run.artifact_id
    ):
        raise _invalid(
            "local_prepare_binding_mismatch",
            "worker success does not continue the exact prepared paper_reader run identity",
        )
    evidence_path = normalized_absolute_path(Path(prepared.evidence.path))
    expected_evidence_path = (
        run_path.parent
        / "evidence"
        / prepared.evidence.artifact_id
        / "evidence.json"
    )
    if (
        evidence_path != expected_evidence_path
        or evidence_path != canonical_evidence_path
        or prepared.evidence.schema_version != "paper_reader.evidence.v2-internal"
        or prepared.evidence.artifact_id != canonical_evidence.evidence_id
        or canonical_evidence.run_id != run.run_id
        or package.evidence_digest != prepared.evidence.sha256
        or package.evidence_manifest.sha256 != prepared.evidence.sha256
        or package.evidence_manifest.size_bytes != prepared.evidence.size_bytes
        or candidate.evidence_manifest.sha256 != prepared.evidence.sha256
        or candidate.evidence_manifest.size_bytes != prepared.evidence.size_bytes
    ):
        raise _invalid(
            "local_prepare_binding_mismatch",
            "worker success does not use the exact prepared evidence closure",
        )
    evidence_relative = evidence_path.relative_to(run_path.parent).as_posix()
    matches = [
        ref
        for ref in run.artifacts
        if ref.role == "evidence_manifest"
        and ref.path == evidence_relative
        and ref.sha256 == prepared.evidence.sha256
        and ref.size_bytes == prepared.evidence.size_bytes
        and ref.media_type == "application/json"
    ]
    if len(matches) != 1:
        raise _invalid(
            "local_prepare_binding_mismatch",
            "worker success does not retain the exact prepared evidence closure",
        )


def validate_worker_result_artifacts(
    manifest: BatchManifest,
    result: WorkerResult,
    *,
    allow_mutable_run: bool = False,
    prepared_local_result: LocalPrepareResult | None = None,
) -> str | None:
    if prepared_local_result is None or result.status != "succeeded":
        return _validate_worker_result_artifacts_unanchored(
            manifest,
            result,
            allow_mutable_run=allow_mutable_run,
            prepared_local_result=prepared_local_result,
        )
    if (
        prepared_local_result.paper_reader_run_directory is None
        or result.paper_reader_run is None
    ):
        raise _invalid(
            "local_prepare_binding_mismatch",
            "worker success lacks the prepared stable run directory identity",
        )
    with _bound_paper_reader_run_directory(
        result.paper_reader_run,
        prepared_local_result.paper_reader_run_directory,
    ):
        return _validate_worker_result_artifacts_unanchored(
            manifest,
            result,
            allow_mutable_run=allow_mutable_run,
            prepared_local_result=prepared_local_result,
        )


@contextmanager
def _locked_foreign_reader_run(
    run_root: Path,
    *,
    code: str,
) -> Iterator[int]:
    with ExitStack() as stack:
        try:
            descriptor = stack.enter_context(
                locked_file(
                    run_root / ".run.lock",
                    create=False,
                    guard_parent_replacement=False,
                )
            )
        except BatchRuntimeError as exc:
            if exc.code == "lease_secret_missing":
                raise _invalid(
                    code,
                    "paper_reader run lock is missing before journal commit",
                    exc,
                ) from exc
            raise
        yield descriptor


@contextmanager
def worker_result_artifact_commit_guard(
    manifest: BatchManifest,
    result: WorkerResult,
    *,
    prepared_local_result: LocalPrepareResult | None = None,
) -> Iterator[Callable[[], None]]:
    """Hold the foreign run root and revalidate its closure through journal commit."""

    if result.status != "succeeded" or result.paper_reader_run is None:
        def validate_failure() -> None:
            validate_worker_result_artifacts(
                manifest,
                result,
                prepared_local_result=prepared_local_result,
            )

        validate_failure()
        yield validate_failure
        return
    run_path = _require_normalized_absolute_path(
        result.paper_reader_run.path,
        code="source_binding_mismatch",
        label="paper_reader worker run",
    )
    if run_path.name != "run.json":
        raise _invalid(
            "source_binding_mismatch",
            "paper_reader worker run path must end in run.json",
        )
    expected_identity = (
        prepared_local_result.paper_reader_run_directory
        if prepared_local_result is not None
        else None
    )
    with _locked_foreign_reader_run(
        run_path.parent,
        code="source_binding_mismatch",
    ) as reader_lock_descriptor, open_directory_fd(
        run_path.parent,
        create=False,
    ) as (descriptor, bound_run_dir):
        validate_locked_path(run_path.parent / ".run.lock", reader_lock_descriptor)
        metadata = os.fstat(descriptor)
        if bound_run_dir != run_path.parent or (
            expected_identity is not None
            and (
                metadata.st_dev != expected_identity.device
                or metadata.st_ino != expected_identity.inode
            )
        ):
            raise _invalid(
                "source_binding_mismatch",
                "paper_reader worker run directory changed before journal commit",
            )
        token = _RUN_ARTIFACT_ANCHOR.set((bound_run_dir, descriptor))
        closure = _ArtifactCommitClosure(bound_run_dir)
        closure_token = _ACTIVE_ARTIFACT_COMMIT_CLOSURE.set(closure)
        try:
            def validate_semantics() -> None:
                _validate_worker_result_artifacts_unanchored(
                    manifest,
                    result,
                    prepared_local_result=prepared_local_result,
                )

            validate_semantics()
            closure.freeze()
            with closure.hold() as held_closure:
                def validate_closure() -> None:
                    validate_locked_path(
                        run_path.parent / ".run.lock",
                        reader_lock_descriptor,
                    )
                    held_closure()
                    validate_semantics()
                    held_closure()

                validate_closure()
                yield validate_closure
        finally:
            _ACTIVE_ARTIFACT_COMMIT_CLOSURE.reset(closure_token)
            _RUN_ARTIFACT_ANCHOR.reset(token)
            closure.close()


def _validate_worker_result_artifacts_unanchored(
    manifest: BatchManifest,
    result: WorkerResult,
    *,
    allow_mutable_run: bool = False,
    prepared_local_result: LocalPrepareResult | None = None,
) -> str | None:
    manifest_item = next((item for item in manifest.items if item.item_id == result.item_id), None)
    if manifest_item is None:
        raise _invalid("unknown_item", f"worker result references unknown item: {result.item_id}")
    if result.source.source_type != manifest_item.source.source_type:
        raise _invalid("source_binding_mismatch", "worker result source type differs from manifest")
    if isinstance(manifest_item, PdfManifestItem):
        if result.source != manifest_item.source:
            raise _invalid("source_binding_mismatch", "worker result PDF identity differs from manifest")
        if not allow_mutable_run:
            current = _pdf_source(Path(manifest_item.source.path))
            if current != manifest_item.source:
                raise _invalid("source_drift", "worker result PDF changed before finish")
    elif isinstance(manifest_item, ZoteroItemManifestItem):
        if result.source != manifest_item.source:
            raise _invalid("source_binding_mismatch", "worker result Zotero identity differs from manifest")
    if result.status != "succeeded":
        if result.source != manifest_item.source:
            raise _invalid("source_binding_mismatch", "failed worker result source differs from manifest")
        return None
    if isinstance(manifest_item, ZoteroTitleManifestItem) and (
        result.source.title != manifest_item.source.title
        or result.source.resolved_item_key is None
        or (
            manifest_item.source.resolved_item_key is not None
            and result.source.resolved_item_key != manifest_item.source.resolved_item_key
        )
    ):
        raise _invalid("source_binding_mismatch", "worker result title resolution differs from manifest")
    assert result.paper_reader_run and result.review_package and result.candidate
    run_path, _run_raw, run = _read_envelope(
        result.paper_reader_run,
        basename="run.json",
        schema="paper_reader.run.v2",
        id_field="run_id",
        bind_bytes=not allow_mutable_run,
    )
    run_artifacts = _require_sequence(
        run.get("artifacts"),
        code="source_binding_mismatch",
        label="current run artifacts",
    )
    secondary_plan_binding = _validate_source_directory(
        run_path.parent,
        run.source,
        run_artifacts,
    )
    resolved_key = _source_matches(manifest_item, run.source, refingerprint=not allow_mutable_run)
    (
        _review_path,
        package,
        candidate_path,
        candidate,
        canonical_evidence_path,
        canonical_evidence,
        candidate_key,
        candidate_inventory_sha256,
    ) = _validate_review_and_candidate(
        manifest_item,
        run_path,
        run,
        result.review_package,
        result.candidate,
        secondary_plan_binding,
        refingerprint=not allow_mutable_run,
    )
    if prepared_local_result is not None:
        _validate_prepared_local_continuation(
            result,
            prepared_local_result,
            run_path=run_path,
            run=run,
            package=package,
            candidate=candidate,
            canonical_evidence_path=canonical_evidence_path,
            canonical_evidence=canonical_evidence,
        )
    if candidate_key != resolved_key:
        raise _invalid("candidate_binding_mismatch", "candidate and run resolve different sources")
    if isinstance(manifest_item, ZoteroTitleManifestItem):
        if (
            candidate_inventory_sha256 is None
            or result.source.inventory_sha256 != candidate_inventory_sha256
            or (
                manifest_item.source.inventory_sha256 is not None
                and manifest_item.source.inventory_sha256 != candidate_inventory_sha256
            )
        ):
            raise _invalid(
                "source_binding_mismatch",
                "Zotero title result inventory provenance differs from canonical raw search inventory",
            )
    if isinstance(manifest_item, PdfManifestItem):
        if (not allow_mutable_run and run.status != "published") or result.local_publication is None:
            raise _invalid("local_publication_invalid", "local worker success requires a published run and receipt")
        receipt_path, _receipt_raw, receipt = _read_envelope(
            result.local_publication,
            basename=f"{candidate.candidate_id}.json",
            schema="paper_reader.local-receipt.v2-internal",
            id_field="receipt_id",
        )
        if receipt_path.parent.name != "receipts":
            raise _invalid("local_publication_invalid", "local receipt must live in receipts/<candidate_id>.json")
        if receipt_path != run_path.parent / "receipts" / f"{candidate.candidate_id}.json":
            raise _invalid("local_publication_invalid", "local receipt belongs to another paper_reader run")
        expected_candidate_relative = candidate_path.relative_to(run_path.parent).as_posix()
        if (
            receipt.receipt_id != f"local-receipt-{candidate.candidate_id}"
            or receipt.run_id != run.run_id
            or receipt.candidate_path != expected_candidate_relative
            or receipt.candidate_digest != result.candidate.sha256
            or receipt.target_path != candidate.target.resolved_path
            or receipt.content_sha256 != candidate.content_sha256
            or receipt.content_length != candidate.content_length
        ):
            raise _invalid("local_publication_invalid", "local receipt does not bind candidate/publication")
        if receipt.intent_path != "publication-intent.json":
            raise _invalid("local_publication_invalid", "local receipt intent path is not canonical")
        _require_normalized_absolute_path(
            receipt.target_path,
            code="local_publication_invalid",
            label="local publication target",
        )
        run_intent_refs = [ref for ref in run.artifacts if ref.role == "local_publication_intent"]
        run_receipt_refs = [ref for ref in run.artifacts if ref.role == "local_receipt"]
        if (
            len(run_intent_refs) != 1
            or len(run_receipt_refs) != 1
            or run_intent_refs[0].path != receipt.intent_path
            or run_intent_refs[0].sha256 != receipt.intent_sha256
            or run_intent_refs[0].media_type != "application/json"
            or run_receipt_refs[0].path != receipt_path.relative_to(run_path.parent).as_posix()
            or run_receipt_refs[0].sha256 != result.local_publication.sha256
            or run_receipt_refs[0].size_bytes != result.local_publication.size_bytes
            or run_receipt_refs[0].media_type != "application/json"
        ):
            raise _invalid("local_publication_invalid", "published run does not bind exact intent/receipt refs")
        intent_path = run_path.parent / receipt.intent_path
        intent_raw, intent = _read_model(
            intent_path,
            code="local_publication_invalid",
            max_bytes=_artifact_ref_read_limit(
                run_intent_refs[0].size_bytes,
                code="local_publication_invalid",
                json_artifact=True,
            ),
        )
        if (
            sha256_bytes(intent_raw) != receipt.intent_sha256
            or intent.run_id != run.run_id
            or intent.candidate_id != candidate.candidate_id
            or intent.candidate_digest != result.candidate.sha256
            or intent.target_path != receipt.target_path
            or intent.content_sha256 != receipt.content_sha256
            or intent.content_length != receipt.content_length
        ):
            raise _invalid("local_publication_invalid", "local publication intent differs from receipt/candidate")
        if run_intent_refs[0].size_bytes != len(intent_raw):
            raise _invalid("local_publication_invalid", "published run does not bind exact intent/receipt refs")
        note_refs = [ref for ref in candidate.artifacts if ref.role == "note_markdown"]
        if len(note_refs) != 1:
            raise _invalid("local_publication_invalid", "local candidate does not bind one note Markdown")
        note_path, note_bytes, _model = _read_inner(run_path.parent, note_refs[0])
        if not allow_mutable_run:
            target_path = normalized_absolute_path(Path(receipt.target_path))
            published = read_bytes(
                target_path,
                code="local_publication_invalid",
                max_bytes=_artifact_ref_read_limit(
                    note_refs[0].size_bytes,
                    code="local_publication_invalid",
                    json_artifact=False,
                ),
            )
            try:
                target_stat = os.stat(target_path, follow_symlinks=False)
                note_stat = os.stat(note_path, follow_symlinks=False)
                source_stat = os.stat(manifest_item.source.path, follow_symlinks=False)
            except OSError as exc:
                raise _invalid("local_publication_invalid", "local publication identity cannot be stat-ed", exc)
            if (
                published != note_bytes
                or len(published) != receipt.content_length
                or sha256_bytes(published) != receipt.content_sha256
                or (target_stat.st_dev, target_stat.st_ino) in {
                    (note_stat.st_dev, note_stat.st_ino),
                    (source_stat.st_dev, source_stat.st_ino),
                }
            ):
                raise _invalid("local_publication_invalid", "local published note bytes differ from receipt")
    elif not allow_mutable_run and run.status != "candidate_built":
        raise _invalid("candidate_binding_mismatch", "Zotero worker run must be candidate_built")
    if isinstance(manifest_item, ZoteroTitleManifestItem) and result.source.resolved_item_key != resolved_key:
        raise _invalid("source_binding_mismatch", "worker result resolved key differs from run/candidate")
    return resolved_key


def _tree_digest(root: Path, paths: list[Path]) -> str:
    records = []
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        raw = read_bytes(path, code="paper_reader_root_invalid")
        records.append({"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(raw)})
    return canonical_sha256(records)


def paper_reader_root_identity(root: Path) -> SkillRootIdentity:
    normalized = normalized_absolute_path(root)
    with open_directory_fd(normalized, create=False):
        pass
    required = [normalized / "SKILL.md", normalized / "pyproject.toml", normalized / "uv.lock"]
    raw_required = [read_bytes(path, code="paper_reader_root_invalid") for path in required]
    try:
        pyproject = tomllib.loads(raw_required[1].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid("paper_reader_root_invalid", "paper_reader pyproject is invalid", exc)
    script = pyproject.get("project", {}).get("scripts", {}).get("paper_reader")
    if script != "paper_reader.public_cli:app":
        raise _invalid("paper_reader_root_invalid", "paper_reader CLI entrypoint is not the grouped V2 public app")
    runtime_root = normalized / "src"
    schema_root = normalized / "references" / "schemas"
    runtime_files = [path for path in runtime_root.rglob("*.py") if path.is_file()]
    schema_files = [path for path in schema_root.glob("*.schema.json") if path.is_file()]
    required_schemas = {
        "paper_reader.run.v2.schema.json",
        "paper_reader.command-result.v2.schema.json",
        "paper_reader.review-package.v2.schema.json",
        "paper_reader.candidate.v2.schema.json",
    }
    if not runtime_files or not required_schemas.issubset({path.name for path in schema_files}):
        raise _invalid("paper_reader_root_invalid", "paper_reader root lacks V2 runtime/schema capability")
    return SkillRootIdentity(
        path=str(normalized),
        skill_md_sha256=sha256_bytes(raw_required[0]),
        pyproject_sha256=sha256_bytes(raw_required[1]),
        uv_lock_sha256=sha256_bytes(raw_required[2]),
        runtime_sha256=_tree_digest(normalized, runtime_files),
        schemas_sha256=_tree_digest(normalized, schema_files),
    )


def validate_local_prepare_result_artifacts(
    manifest: BatchManifest,
    result: LocalPrepareResult,
    *,
    expected_root: Path | None = None,
    allow_mutable_run: bool = False,
) -> None:
    if result.status != "prepared":
        return _validate_local_prepare_result_artifacts_unanchored(
            manifest,
            result,
            expected_root=expected_root,
            allow_mutable_run=allow_mutable_run,
        )
    if result.paper_reader_run_directory is None or result.paper_reader_run is None:
        raise _invalid(
            "local_prepare_binding_mismatch",
            "prepared result lacks its stable paper_reader run directory identity",
        )
    with _bound_paper_reader_run_directory(
        result.paper_reader_run,
        result.paper_reader_run_directory,
    ):
        return _validate_local_prepare_result_artifacts_unanchored(
            manifest,
            result,
            expected_root=expected_root,
            allow_mutable_run=allow_mutable_run,
        )


@contextmanager
def local_prepare_result_artifact_commit_guard(
    manifest: BatchManifest,
    result: LocalPrepareResult,
    *,
    expected_root: Path | None = None,
) -> Iterator[Callable[[], None]]:
    """Hold a prepared Reader run root through local-prepare event commit."""

    if result.status != "prepared" or result.paper_reader_run is None:
        def validate_failure() -> None:
            validate_local_prepare_result_artifacts(
                manifest,
                result,
                expected_root=expected_root,
            )

        validate_failure()
        yield validate_failure
        return
    if result.paper_reader_run_directory is None:
        raise _invalid(
            "local_prepare_binding_mismatch",
            "prepared result lacks its stable paper_reader run directory identity",
        )
    run_path = _require_normalized_absolute_path(
        result.paper_reader_run.path,
        code="local_prepare_binding_mismatch",
        label="prepared paper_reader run",
    )
    with _locked_foreign_reader_run(
        run_path.parent,
        code="local_prepare_binding_mismatch",
    ) as reader_lock_descriptor, _bound_paper_reader_run_directory(
        result.paper_reader_run,
        result.paper_reader_run_directory,
    ):
        closure = _ArtifactCommitClosure(run_path.parent)
        closure_token = _ACTIVE_ARTIFACT_COMMIT_CLOSURE.set(closure)
        try:
            def validate_semantics() -> None:
                _validate_local_prepare_result_artifacts_unanchored(
                    manifest,
                    result,
                    expected_root=expected_root,
                )

            validate_semantics()
            closure.freeze()
            with closure.hold() as held_closure:
                def validate_closure() -> None:
                    validate_locked_path(
                        run_path.parent / ".run.lock",
                        reader_lock_descriptor,
                    )
                    held_closure()
                    validate_semantics()
                    held_closure()

                validate_closure()
                yield validate_closure
        finally:
            _ACTIVE_ARTIFACT_COMMIT_CLOSURE.reset(closure_token)
            closure.close()


def _validate_local_prepare_result_artifacts_unanchored(
    manifest: BatchManifest,
    result: LocalPrepareResult,
    *,
    expected_root: Path | None = None,
    allow_mutable_run: bool = False,
) -> None:
    manifest_item = next((item for item in manifest.items if item.item_id == result.item_id), None)
    if not isinstance(manifest_item, PdfManifestItem):
        raise _invalid("unknown_item", "local prepare result must reference a PDF manifest item")
    if result.source != manifest_item.source or (
        not allow_mutable_run and _pdf_source(Path(result.source.path)) != result.source
    ):
        raise _invalid("source_drift", "local prepare source identity changed")
    if not allow_mutable_run:
        root = normalized_absolute_path(expected_root or Path(result.paper_reader_root.path))
        if result.paper_reader_root != paper_reader_root_identity(root):
            raise _invalid("paper_reader_root_drift", "paper_reader root identity differs from result")
    if result.status != "prepared":
        return
    assert result.paper_reader_run and result.evidence
    run_path, _run_raw, run = _read_envelope(
        result.paper_reader_run,
        basename="run.json",
        schema="paper_reader.run.v2",
        id_field="run_id",
        bind_bytes=not allow_mutable_run,
    )
    run_artifacts = _require_sequence(
        run.get("artifacts"),
        code="source_binding_mismatch",
        label="current run artifacts",
    )
    secondary_plan_binding = _validate_source_directory(
        run_path.parent,
        run.source,
        run_artifacts,
    )
    if run.target.get("target_type") != "local" or (
        not allow_mutable_run
        and (run.status != "prepared" or run.gate.status == "blocked" or run.gate.blockers)
    ):
        raise _invalid("local_prepare_invalid", "local prepare run must be prepared with its fixed local target")
    _source_matches(manifest_item, run.source, refingerprint=not allow_mutable_run)
    _validate_local_run_target(
        manifest_item,
        run_path,
        run.target,
        check_parent_identity=not allow_mutable_run,
    )
    evidence_path, _evidence_raw, evidence = _read_envelope(
        result.evidence,
        basename="evidence.json",
        schema="paper_reader.evidence.v2-internal",
        id_field="evidence_id",
    )
    if evidence_path != run_path.parent / "evidence" / evidence.evidence_id / "evidence.json":
        raise _invalid("evidence_binding_mismatch", "local prepare evidence path is outside the bound run bundle")
    if evidence.run_id != run.run_id:
        raise _invalid("evidence_binding_mismatch", "local prepare evidence run id differs")
    _run_ref_for(run, run_path.parent, evidence_path, result.evidence, "evidence_manifest")
    _validate_evidence(
        run_path.parent,
        evidence_path,
        evidence,
        manifest_item.source.sha256,
        secondary_plan_binding,
    )


__all__ = [
    "local_prepare_result_artifact_commit_guard",
    "paper_reader_root_identity",
    "validate_local_prepare_result_artifacts",
    "validate_worker_result_artifacts",
    "worker_result_artifact_commit_guard",
]
