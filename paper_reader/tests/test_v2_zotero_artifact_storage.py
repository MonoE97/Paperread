from __future__ import annotations

from pathlib import Path

import pytest

import paper_reader.zotero_artifact_paths as artifact_module
from paper_reader.storage import tree_snapshot_from_bytes
from paper_reader.v2_loader import DirectoryAnchor
from paper_reader.zotero_artifact_paths import (
    UnsafeZoteroArtifactPathError,
    anchored_artifact_publication,
    inspect_deterministic_artifact_paths,
)


def test_zotero_sidecar_publication_preserves_unknown_child_swap_during_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staging = run_dir / ".authorization.staging"
    sidecar = staging / "sidecar"
    sidecar.mkdir(parents=True)
    (sidecar / "content.html").write_bytes(b"expected")
    paths = inspect_deterministic_artifact_paths(
        run_dir,
        root_name="authorizations",
        parent_parts=(),
        stem="authorization-id",
        allow_existing_sidecar=False,
        allow_existing_main=False,
    )
    original_rename = artifact_module._renameat_no_replace
    injected = False

    def swap_child_then_rename(
        source_dir_fd,
        source_name,
        destination_dir_fd,
        destination_name,
        **kwargs,
    ) -> None:
        nonlocal injected
        if source_name == "sidecar" and not injected:
            injected = True
            sidecar_fd = artifact_module.os.open(
                source_name,
                artifact_module._DIRECTORY_FLAGS,
                dir_fd=source_dir_fd,
            )
            try:
                artifact_module.os.rename(
                    "content.html",
                    "content.detached.html",
                    src_dir_fd=sidecar_fd,
                    dst_dir_fd=sidecar_fd,
                )
                attacker_fd = artifact_module.os.open(
                    "content.html",
                    artifact_module.os.O_WRONLY
                    | artifact_module.os.O_CREAT
                    | artifact_module.os.O_EXCL,
                    0o644,
                    dir_fd=sidecar_fd,
                )
                try:
                    artifact_module.os.write(attacker_fd, b"attacker")
                finally:
                    artifact_module.os.close(attacker_fd)
            finally:
                artifact_module.os.close(sidecar_fd)
        original_rename(
            source_dir_fd,
            source_name,
            destination_dir_fd,
            destination_name,
            **kwargs,
        )

    monkeypatch.setattr(artifact_module, "_renameat_no_replace", swap_child_then_rename)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as run_anchor, DirectoryAnchor.open(
        staging,
        manifest_path=staging / "record.json",
    ) as staging_anchor:
        with pytest.raises(UnsafeZoteroArtifactPathError):
            with anchored_artifact_publication(
                paths,
                staging_dir=staging,
                allow_existing_sidecar=False,
                allow_existing_main=False,
                expected_run_anchor=run_anchor,
                expected_staging_anchor=staging_anchor,
                expected_sidecar_snapshot=tree_snapshot_from_bytes(
                    {"content.html": b"expected"}
                ),
            ) as publication:
                publication.publish_sidecar(sidecar)

    assert injected is True
    assert (paths.sidecar / "content.html").read_bytes() == b"attacker"
    assert (paths.sidecar / "content.detached.html").read_bytes() == b"expected"
    assert not paths.main.exists()


def test_zotero_main_publication_detects_name_swap_after_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staging = run_dir / ".authorization.staging"
    sidecar = staging / "sidecar"
    sidecar.mkdir(parents=True)
    (sidecar / "content.html").write_bytes(b"expected-sidecar")
    main_source = staging / "authorization-id.json"
    main_source.write_bytes(b"exact-main")
    paths = inspect_deterministic_artifact_paths(
        run_dir,
        root_name="authorizations",
        parent_parts=(),
        stem="authorization-id",
        allow_existing_sidecar=False,
        allow_existing_main=False,
    )

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as run_anchor, DirectoryAnchor.open(
        staging,
        manifest_path=main_source,
    ) as staging_anchor:
        with anchored_artifact_publication(
            paths,
            staging_dir=staging,
            allow_existing_sidecar=False,
            allow_existing_main=False,
            expected_run_anchor=run_anchor,
            expected_staging_anchor=staging_anchor,
            expected_sidecar_snapshot=tree_snapshot_from_bytes(
                {"content.html": b"expected-sidecar"}
            ),
        ) as publication:
            publication.publish_sidecar(sidecar)
            original_read = artifact_module._read_all
            read_count = 0

            def read_then_swap(descriptor: int) -> bytes:
                nonlocal read_count
                raw = original_read(descriptor)
                read_count += 1
                if read_count == 2:
                    paths.main.rename(paths.parent / "authorization-id.detached.json")
                    paths.main.write_bytes(b"attacker")
                return raw

            monkeypatch.setattr(artifact_module, "_read_all", read_then_swap)
            with pytest.raises(UnsafeZoteroArtifactPathError):
                publication.publish_main(main_source, expected_bytes=b"exact-main")

    assert paths.main.read_bytes() == b"attacker"
    assert (paths.parent / "authorization-id.detached.json").read_bytes() == b"exact-main"


def test_zotero_main_failure_cleanup_preserves_unknown_temporary_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staging = run_dir / ".authorization.staging"
    sidecar = staging / "sidecar"
    sidecar.mkdir(parents=True)
    (sidecar / "content.html").write_bytes(b"expected-sidecar")
    main_source = staging / "authorization-id.json"
    main_source.write_bytes(b"exact-main")
    paths = inspect_deterministic_artifact_paths(
        run_dir,
        root_name="authorizations",
        parent_parts=(),
        stem="authorization-id",
        allow_existing_sidecar=False,
        allow_existing_main=False,
    )

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as run_anchor, DirectoryAnchor.open(
        staging,
        manifest_path=main_source,
    ) as staging_anchor:
        with anchored_artifact_publication(
            paths,
            staging_dir=staging,
            allow_existing_sidecar=False,
            allow_existing_main=False,
            expected_run_anchor=run_anchor,
            expected_staging_anchor=staging_anchor,
            expected_sidecar_snapshot=tree_snapshot_from_bytes(
                {"content.html": b"expected-sidecar"}
            ),
        ) as publication:
            publication.publish_sidecar(sidecar)
            original_require = artifact_module._require_real_sidecar
            call_count = 0
            replacement_path: Path | None = None
            detached_path: Path | None = None

            def swap_temporary_then_fail(anchor) -> None:
                nonlocal call_count, replacement_path, detached_path
                call_count += 1
                original_require(anchor)
                if call_count != 2:
                    return
                matches = list(
                    paths.parent.glob(f".{paths.main.name}.*.tmp")
                )
                assert len(matches) == 1
                replacement_path = matches[0]
                detached_path = replacement_path.with_name(
                    f"{replacement_path.name}.detached"
                )
                replacement_path.rename(detached_path)
                replacement_path.write_bytes(b"attacker")
                raise OSError("injected failure after temporary replacement")

            monkeypatch.setattr(
                artifact_module,
                "_require_real_sidecar",
                swap_temporary_then_fail,
            )
            with pytest.raises(OSError, match="injected failure"):
                publication.publish_main(main_source, expected_bytes=b"exact-main")

    assert replacement_path is not None
    assert detached_path is not None
    assert replacement_path.read_bytes() == b"attacker"
    assert detached_path.read_bytes() == b"exact-main"
    assert not paths.main.exists()
