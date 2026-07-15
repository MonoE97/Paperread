from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import fitz
import pytest

from paper_reader.arxiv_source import (
    collect_source_figures,
    download_arxiv_source,
    extract_source_package,
    render_source_figure_pdfs,
    resolve_arxiv_id,
)
from paper_reader.resource_policy import V2_RESOURCE_POLICY


def _make_tarball(path: Path, members: list[tuple[str, bytes, int]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data, tar_type in members:
            info = tarfile.TarInfo(name=name)
            info.type = tar_type
            if tar_type == tarfile.SYMTYPE:
                info.linkname = "target"
                archive.addfile(info)
                continue
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _make_source_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=240, height=160)
    page.draw_rect(fitz.Rect(30, 30, 200, 120), color=(0, 0, 0), fill=(0.7, 0.9, 0.7))
    doc.save(path)
    doc.close()


def _source_archive_bytes(
    member_name: str = "paper/figures/a.png",
    payload: bytes = b"png",
) -> bytes:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return archive_buffer.getvalue()


class _BytesResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = io.BytesIO(payload)

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)


def _run_with_low_nofile(script: str, *args: Path) -> subprocess.CompletedProcess[str]:
    pytest.importorskip("resource")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_resolve_arxiv_id_prefers_metadata_then_attachment_hints() -> None:
    details = {
        "url": "https://arxiv.org/abs/2402.12345v2",
        "archiveLocation": "arXiv:2301.00001",
        "extra": "Preprint arXiv:2201.00002",
        "attachments": [{"filename": "2101.00003v3-paper.pdf"}],
    }

    assert resolve_arxiv_id(details) == "2402.12345"


def test_resolve_arxiv_id_uses_pdf_path_only_when_metadata_missing() -> None:
    details = {
        "url": "",
        "archiveLocation": "",
        "extra": "",
        "attachments": [{"filename": "appendix-2403.01010v4.pdf"}],
    }

    assert resolve_arxiv_id(details, Path("/tmp/2404.02020v2-main.pdf")) == "2403.01010"
    assert resolve_arxiv_id({"extra": ""}, Path("/tmp/2404.02020v2-main.pdf")) == "2404.02020"


def test_download_arxiv_source_uses_bounded_network_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_urlopen(url: str, *, timeout: float):
        observed["url"] = url
        observed["timeout"] = timeout
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = download_arxiv_source("2401.00001", tmp_path, timeout_seconds=99.0)

    assert result is None
    assert observed["url"] == "https://arxiv.org/e-print/2401.00001"
    assert observed["timeout"] == V2_RESOURCE_POLICY.arxiv_timeout_seconds


def test_download_arxiv_source_stops_at_compressed_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    policy = replace(V2_RESOURCE_POLICY, arxiv_compressed_max_bytes=8)
    monkeypatch.setattr(arxiv_source, "V2_RESOURCE_POLICY", policy)
    extracted = False

    class FakeResponse:
        def __init__(self) -> None:
            self._payload = io.BytesIO(b"123456789")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self._payload.read(size)

    def fake_extract(*args: object, **kwargs: object) -> None:
        nonlocal extracted
        extracted = True

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(arxiv_source, "_extract_source_package_file", fake_extract)

    assert download_arxiv_source("2401.00001", tmp_path) is None
    assert extracted is False
    assert not (tmp_path / "2401.00001.tar.gz").exists()


def test_download_arxiv_source_propagates_figure_candidate_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    policy = replace(
        V2_RESOURCE_POLICY,
        figure_max_candidates=1,
        arxiv_max_figure_files=1_000,
    )
    monkeypatch.setattr(arxiv_source, "V2_RESOURCE_POLICY", policy)
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for name in ("figures/one.png", "figures/two.png"):
            payload = b"candidate"
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    archive_bytes = archive_buffer.getvalue()

    class FakeResponse:
        def __init__(self) -> None:
            self._payload = io.BytesIO(archive_bytes)

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self._payload.read(size)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(arxiv_source.FigureCandidateLimitError) as exc_info:
        download_arxiv_source("2401.00001", tmp_path)

    assert exc_info.value.actual == 2
    assert exc_info.value.limit == 1


def test_download_arxiv_source_does_not_follow_archive_leaf_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "source-cache"
    cache_root.mkdir()
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside")
    (cache_root / "2401.00001.tar.gz").symlink_to(sentinel)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )

    assert download_arxiv_source("2401.00001", cache_root) is None
    assert sentinel.read_bytes() == b"outside"
    assert (cache_root / "2401.00001.tar.gz").is_symlink()


def test_download_arxiv_source_rejects_existing_cache_root_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    cache_root = tmp_path / "source-cache"
    cache_root.symlink_to(outside, target_is_directory=True)

    def forbidden_urlopen(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unsafe cache-root symlink reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)

    assert download_arxiv_source("2401.00001", cache_root) is None
    assert list(outside.iterdir()) == []


def test_download_arxiv_source_rejects_unsealed_existing_cache_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "source-cache"
    unsealed = cache_root / "2401.00001"
    unsealed.mkdir(parents=True)
    (unsealed / "paper.tex").write_text("unsealed", encoding="utf-8")

    def forbidden_urlopen(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an occupied unsealed cache name must fail closed before the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)

    assert download_arxiv_source("2401.00001", cache_root) is None


def test_download_arxiv_source_publishes_and_reuses_only_a_sealed_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    source_root = download_arxiv_source("2401.00001", cache_root)

    assert source_root == cache_root / "2401.00001"
    assert (source_root / arxiv_source.CACHE_COMPLETION_MARKER_NAME).is_file()
    assert list(cache_root.iterdir()) == [source_root]

    def forbidden_urlopen(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a valid sealed cache should be reused without the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    assert download_arxiv_source("2401.00001", cache_root) == source_root


def test_download_arxiv_source_rejects_cache_with_extra_unsealed_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    source_root = download_arxiv_source("2401.00001", cache_root)
    assert source_root is not None
    (source_root / "injected.txt").write_text("not sealed", encoding="utf-8")

    def forbidden_urlopen(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an occupied invalid cache name must fail closed before the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    assert download_arxiv_source("2401.00001", cache_root) is None


def test_download_arxiv_source_rejects_cache_with_changed_payload_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    source_root = download_arxiv_source("2401.00001", cache_root)
    assert source_root is not None
    (source_root / "paper" / "figures" / "a.png").write_bytes(b"bad")

    def forbidden_urlopen(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an occupied invalid cache name must fail closed before the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    assert download_arxiv_source("2401.00001", cache_root) is None


def test_download_arxiv_source_recovers_exact_tree_after_post_rename_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    original_atomic_publish_tree = arxiv_source.atomic_publish_tree

    def publish_then_raise(*args: object, **kwargs: object) -> None:
        original_atomic_publish_tree(*args, **kwargs)
        raise OSError("simulated exception after durable rename")

    monkeypatch.setattr(arxiv_source, "atomic_publish_tree", publish_then_raise)

    source_root = download_arxiv_source("2401.00001", cache_root)

    assert source_root == cache_root / "2401.00001"
    assert (source_root / "paper" / "figures" / "a.png").read_bytes() == b"png"


def test_download_arxiv_source_does_not_recover_replaced_post_rename_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    cache_root = tmp_path / "source-cache"
    source_root = cache_root / "2401.00001"
    displaced_root = cache_root / ".displaced-exact-tree"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    original_atomic_publish_tree = arxiv_source.atomic_publish_tree

    def publish_replace_then_raise(*args: object, **kwargs: object) -> None:
        original_atomic_publish_tree(*args, **kwargs)
        source_root.rename(displaced_root)
        shutil.copytree(displaced_root, source_root)
        raise OSError("simulated replacement after durable rename")

    monkeypatch.setattr(arxiv_source, "atomic_publish_tree", publish_replace_then_raise)

    assert download_arxiv_source("2401.00001", cache_root) is None
    assert (source_root / arxiv_source.CACHE_COMPLETION_MARKER_NAME).is_file()


def test_download_arxiv_source_does_not_recover_resealed_post_rename_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    cache_root = tmp_path / "source-cache"
    source_root = cache_root / "2401.00001"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    original_atomic_publish_tree = arxiv_source.atomic_publish_tree

    def publish_reseal_then_raise(*args: object, **kwargs: object) -> None:
        original_atomic_publish_tree(*args, **kwargs)
        payload_path = source_root / "paper" / "figures" / "a.png"
        payload_path.write_bytes(b"forged")
        marker_path = source_root / arxiv_source.CACHE_COMPLETION_MARKER_NAME
        marker = json.loads(marker_path.read_bytes())
        for entry in marker["payload_entries"]:
            if entry["path"] == "paper/figures/a.png":
                entry["size_bytes"] = len(b"forged")
                entry["sha256"] = hashlib.sha256(b"forged").hexdigest()
        marker_path.write_bytes(arxiv_source.canonical_json_bytes(marker))
        raise OSError("simulated reseal after durable rename")

    monkeypatch.setattr(arxiv_source, "atomic_publish_tree", publish_reseal_then_raise)

    assert download_arxiv_source("2401.00001", cache_root) is None
    assert (source_root / "paper" / "figures" / "a.png").read_bytes() == b"forged"


def test_download_arxiv_source_does_not_seal_replaced_extracted_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    original_publish = arxiv_source.publish_bytes_no_replace
    injected = False

    def publish_then_replace(
        content: bytes,
        destination: Path,
        **kwargs: object,
    ):
        nonlocal injected
        published = original_publish(content, destination, **kwargs)
        if not injected and destination.name == "a.png":
            replacement = destination.with_name("replacement.png")
            replacement.write_bytes(b"bad")
            os.replace(replacement, destination)
            injected = True
        return published

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", publish_then_replace)

    assert download_arxiv_source("2401.00001", cache_root) is None
    assert injected is True
    assert not (cache_root / "2401.00001").exists()


def test_extract_source_package_rejects_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("figures/good.txt", b"ok", tarfile.REGTYPE),
            ("../escape.txt", b"bad", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(ValueError, match="unsafe"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_absolute_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("/tmp/escape.txt", b"bad", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(ValueError, match="unsafe"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_symlinks(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("figures/good.txt", b"ok", tarfile.REGTYPE),
            ("figures/link.txt", b"", tarfile.SYMTYPE),
        ],
    )

    with pytest.raises(ValueError, match="symlink"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_cleans_up_partial_files_on_failure(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    output_dir = tmp_path / "out"
    _make_tarball(
        archive_path,
        [
            ("figures/good.txt", b"ok", tarfile.REGTYPE),
            ("../escape.txt", b"bad", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(ValueError, match="unsafe"):
        extract_source_package(archive_path, output_dir)

    assert not (output_dir / "figures" / "good.txt").exists()


def test_extract_source_package_rejects_symlink_output_root_without_writing_outside(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(archive_path, [("paper.tex", b"content", tarfile.REGTYPE)])
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    output_dir = tmp_path / "out"
    output_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        extract_source_package(archive_path, output_dir)

    assert sentinel.read_bytes() == b"outside"
    assert not (outside / "paper.tex").exists()


def test_extract_source_package_rejects_symlink_intermediate_without_writing_outside(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [("figures/plot.png", b"image", tarfile.REGTYPE)],
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output_dir / "figures").symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        extract_source_package(archive_path, output_dir)

    assert not (outside / "plot.png").exists()


def test_extract_source_package_rejects_symlink_leaf_without_overwriting_target(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [("figures/plot.png", b"image", tarfile.REGTYPE)],
    )
    output_dir = tmp_path / "out"
    (output_dir / "figures").mkdir(parents=True)
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside")
    (output_dir / "figures" / "plot.png").symlink_to(sentinel)

    with pytest.raises((OSError, ValueError)):
        extract_source_package(archive_path, output_dir)

    assert sentinel.read_bytes() == b"outside"
    assert (output_dir / "figures" / "plot.png").is_symlink()


def test_extract_source_package_rejects_hardlinked_leaf_without_overwriting_target(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [("figures/plot.png", b"image", tarfile.REGTYPE)],
    )
    output_dir = tmp_path / "out"
    (output_dir / "figures").mkdir(parents=True)
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside")
    (output_dir / "figures" / "plot.png").hardlink_to(sentinel)

    with pytest.raises((OSError, ValueError)):
        extract_source_package(archive_path, output_dir)

    assert sentinel.read_bytes() == b"outside"
    assert (output_dir / "figures" / "plot.png").read_bytes() == b"outside"


def test_extract_source_package_rejects_member_count_over_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, arxiv_max_members=1),
    )
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("one.txt", b"1", tarfile.REGTYPE),
            ("two.txt", b"2", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(ValueError, match="member count"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_duplicate_member_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("figures/plot.png", b"first", tarfile.REGTYPE),
            ("figures/plot.png", b"second", tarfile.REGTYPE),
        ],
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("duplicate archive member reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="duplicate|conflict"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_portable_name_collision_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("figures/A.png", b"first", tarfile.REGTYPE),
            ("figures/a.png", b"second", tarfile.REGTYPE),
        ],
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("portable archive name collision reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="portable.*collision"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_overlong_component_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("ok.txt", b"ok", tarfile.REGTYPE),
            ("x" * 256, b"too-long", tarfile.REGTYPE),
        ],
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("overlong archive component reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="file name.*limit"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_reserves_completion_member_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(
            V2_RESOURCE_POLICY,
            arxiv_max_members=100,
            artifact_tree_max_members=2,
        ),
    )
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("one.txt", b"1", tarfile.REGTYPE),
            ("two.txt", b"2", tarfile.REGTYPE),
        ],
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("effective tree member cap was checked only after extraction")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="member count"):
        extract_source_package(archive_path, tmp_path / "out")


@pytest.mark.parametrize(
    "member_name",
    [
        ".paper-reader-arxiv-cache-completion.v2.json",
        ".paper-reader-arxiv-cache-completion.v2.json/nested",
    ],
)
def test_extract_source_package_rejects_reserved_completion_marker_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    member_name: str,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            (
                member_name,
                b"attacker-controlled-marker",
                tarfile.REGTYPE,
            )
        ],
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("reserved completion marker reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="reserved"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_tree_depth_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, artifact_tree_max_depth=2),
    )
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [("paper/figures/plot.png", b"image", tarfile.REGTYPE)],
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("effective tree depth cap was checked only after extraction")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="depth"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_stays_within_low_fd_budget(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    output_dir = tmp_path / "out"
    _make_tarball(
        archive_path,
        [
            (f"member-{index:03d}.txt", b"x", tarfile.REGTYPE)
            for index in range(300)
        ],
    )

    completed = _run_with_low_nofile(
        """
        import resource
        import sys
        from pathlib import Path
        from paper_reader.arxiv_source import extract_source_package

        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        output = extract_source_package(Path(sys.argv[1]), Path(sys.argv[2]))
        assert len(list(output.glob("member-*.txt"))) == 300
        """,
        archive_path,
        output_dir,
    )

    assert completed.returncode == 0, completed.stderr


def test_extract_source_package_rejects_declared_expanded_size_over_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, arxiv_expanded_max_bytes=3),
    )
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(archive_path, [("paper.tex", b"1234", tarfile.REGTYPE)])

    with pytest.raises(ValueError, match="expanded size"):
        extract_source_package(archive_path, tmp_path / "out")


def test_extract_source_package_rejects_figure_count_over_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, arxiv_max_figure_files=1),
    )
    archive_path = tmp_path / "source.tar.gz"
    _make_tarball(
        archive_path,
        [
            ("figures/one.png", b"1", tarfile.REGTYPE),
            ("figures/two.pdf", b"2", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(ValueError, match="figure count"):
        extract_source_package(archive_path, tmp_path / "out")


def test_collect_source_figures_reads_root_and_common_figure_directories(
    tmp_path: Path,
) -> None:
    root_png = tmp_path / "root-figure.png"
    root_png.write_bytes(b"png")
    (tmp_path / "figures").mkdir()
    nested_pdf = tmp_path / "figures" / "vector-figure.pdf"
    nested_pdf.write_bytes(b"%PDF-1.4")
    (tmp_path / "img").mkdir()
    nested_jpg = tmp_path / "img" / "micrograph.jpg"
    nested_jpg.write_bytes(b"jpg")
    (tmp_path / "notes").mkdir()
    ignored = tmp_path / "notes" / "ignore.png"
    ignored.write_bytes(b"ignore")

    output_dir = tmp_path / "collected"
    figures = collect_source_figures(tmp_path, output_dir)

    assert {(figure["rel_path"], figure["media_type"], figure["source"]) for figure in figures} == {
        ("root-figure.png", "image", "arxiv-source"),
        ("figures/vector-figure.pdf", "pdf", "arxiv-source"),
        ("img/micrograph.jpg", "image", "arxiv-source"),
    }
    assert {Path(figure["image_path"]).parent for figure in figures} == {output_dir}


def test_collect_source_figures_reads_figures_under_outer_wrapper_directory(tmp_path: Path) -> None:
    wrapped_figure_dir = tmp_path / "paper" / "figures"
    wrapped_figure_dir.mkdir(parents=True)
    wrapped_png = wrapped_figure_dir / "a.png"
    wrapped_png.write_bytes(b"png")

    output_dir = tmp_path / "collected"
    figures = collect_source_figures(tmp_path, output_dir)

    assert len(figures) == 1
    assert figures[0]["rel_path"] == "paper/figures/a.png"
    assert Path(figures[0]["image_path"]).name == "paper__figures__a.png"


def test_collect_source_figures_rejects_flattened_name_collision_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "source"
    (source_root / "figures").mkdir(parents=True)
    (source_root / "figures" / "plot.png").write_bytes(b"nested")
    (source_root / "figures__plot.png").write_bytes(b"flat")

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("colliding figure names reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="output name collision"):
        collect_source_figures(source_root, tmp_path / "collected")


def test_collect_source_figures_rejects_overlong_flattened_name_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "source"
    figure_dir = (
        source_root / "figures" / ("a" * 100) / ("b" * 100) / ("c" * 100)
    )
    figure_dir.mkdir(parents=True)
    (figure_dir / "plot.png").write_bytes(b"image")

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("overlong flattened output name reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="file name.*limit"):
        collect_source_figures(source_root, tmp_path / "collected")


def test_closed_published_file_cleanup_keeps_forensic_tombstone(
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source
    from paper_reader.storage import open_directory_anchor, publish_bytes_no_replace

    root = tmp_path / "root"
    with open_directory_anchor(root, create=True) as anchor:
        target = root / "artifact.bin"
        published = publish_bytes_no_replace(
            b"closed-owner",
            target,
            anchor=anchor,
            hold_open=True,
        )
        published.close()

        arxiv_source._remove_closed_published_file(anchor, published)

    assert not target.exists()
    tombstones = list(root.glob(".artifact.bin.*.cleanup-tombstone"))
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == b"closed-owner"


def test_collect_source_figures_rejects_destination_leaf_symlink_without_overwrite(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "plot.png").write_bytes(b"image")
    output_dir = tmp_path / "collected"
    output_dir.mkdir()
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside")
    (output_dir / "plot.png").symlink_to(sentinel)

    with pytest.raises((OSError, ValueError)):
        collect_source_figures(source_root, output_dir)

    assert sentinel.read_bytes() == b"outside"
    assert (output_dir / "plot.png").is_symlink()


def test_collect_source_figures_rejects_destination_root_symlink_without_writing_outside(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "plot.png").write_bytes(b"image")
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir = tmp_path / "collected"
    output_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        collect_source_figures(source_root, output_dir)

    assert not (outside / "plot.png").exists()


def test_collect_source_figures_rejects_symlink_and_hardlink_sources(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    figure_dir = source_root / "figures"
    figure_dir.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.png"
    sentinel.write_bytes(b"outside")
    (figure_dir / "linked.png").symlink_to(sentinel)

    with pytest.raises((OSError, ValueError)):
        collect_source_figures(source_root, tmp_path / "symlink-output")

    (figure_dir / "linked.png").unlink()
    (figure_dir / "linked.png").hardlink_to(sentinel)
    with pytest.raises((OSError, ValueError)):
        collect_source_figures(source_root, tmp_path / "hardlink-output")

    assert not (tmp_path / "symlink-output" / "figures__linked.png").exists()
    assert not (tmp_path / "hardlink-output" / "figures__linked.png").exists()


def test_collect_source_figures_detects_source_identity_swap_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "plot.png"
    source.write_bytes(b"original")
    output_dir = tmp_path / "collected"
    original_publish = arxiv_source.publish_bytes_no_replace
    swapped = False

    def swap_source_then_publish(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.unlink()
            source.write_bytes(b"replacement")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        arxiv_source,
        "publish_bytes_no_replace",
        swap_source_then_publish,
    )

    with pytest.raises(ValueError, match="changed"):
        collect_source_figures(source_root, output_dir)

    assert not (output_dir / "plot.png").exists()


def test_collect_source_figures_rejects_output_replacement_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "plot.png").write_bytes(b"original")
    output_dir = tmp_path / "collected"
    original_publish = arxiv_source.publish_bytes_no_replace
    injected = False

    def publish_then_replace(
        content: bytes,
        destination: Path,
        **kwargs: object,
    ):
        nonlocal injected
        published = original_publish(content, destination, **kwargs)
        if destination.parent == output_dir and not injected:
            replacement = destination.with_name("replacement.png")
            replacement.write_bytes(b"forged!!")
            os.replace(replacement, destination)
            injected = True
        return published

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", publish_then_replace)

    with pytest.raises(ValueError, match="output|tree|changed"):
        collect_source_figures(source_root, output_dir)

    assert injected is True
    assert (output_dir / "plot.png").read_bytes() == b"forged!!"


def test_collect_source_figures_rejects_changed_sealed_cache_before_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    source_root = download_arxiv_source("2401.00001", cache_root)
    assert source_root is not None
    (source_root / "paper" / "figures" / "a.png").write_bytes(b"changed")
    output_dir = tmp_path / "collected"

    with pytest.raises(ValueError, match="completion|closed set"):
        collect_source_figures(
            source_root,
            output_dir,
            expected_arxiv_id="2401.00001",
        )

    assert not output_dir.exists()


def test_collect_source_figures_rechecks_sealed_cache_after_output_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    cache_root = tmp_path / "source-cache"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _BytesResponse(_source_archive_bytes()),
    )
    source_root = download_arxiv_source("2401.00001", cache_root)
    assert source_root is not None
    output_dir = tmp_path / "collected"
    original_publish = arxiv_source.publish_bytes_no_replace
    injected = False

    def publish_then_change_marker(
        content: bytes,
        destination: Path,
        **kwargs: object,
    ):
        nonlocal injected
        published = original_publish(content, destination, **kwargs)
        if destination.parent == output_dir and not injected:
            marker = source_root / arxiv_source.CACHE_COMPLETION_MARKER_NAME
            marker.write_bytes(marker.read_bytes() + b"\n")
            injected = True
        return published

    monkeypatch.setattr(
        arxiv_source,
        "publish_bytes_no_replace",
        publish_then_change_marker,
    )

    with pytest.raises(ValueError, match="completion|sealed"):
        collect_source_figures(
            source_root,
            output_dir,
            expected_arxiv_id="2401.00001",
        )

    assert injected is True


def test_collect_source_figures_rejects_candidate_scan_cap_before_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    for index in range(V2_RESOURCE_POLICY.figure_max_candidates + 1):
        (source_root / f"figure-{index:03d}.png").write_bytes(b"candidate")

    def forbidden_copy(*_args: object, **_kwargs: object) -> None:
        pytest.fail("candidate scan cap was checked only after copying")

    monkeypatch.setattr(
        "paper_reader.arxiv_source.publish_bytes_no_replace",
        forbidden_copy,
    )

    with pytest.raises(ValueError, match=r"figure candidate count 201 exceeds 200"):
        collect_source_figures(source_root, tmp_path / "collected")


def test_collect_source_figures_uses_structured_candidate_cap_before_arxiv_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "source"
    source_root.mkdir()
    for index in range(V2_RESOURCE_POLICY.arxiv_max_figure_files + 1):
        (source_root / f"figure-{index:04d}.png").write_bytes(b"candidate")

    def forbidden_copy(*_args: object, **_kwargs: object) -> None:
        pytest.fail("arXiv cap was checked only after copying")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_copy)

    with pytest.raises(arxiv_source.FigureCandidateLimitError) as exc_info:
        collect_source_figures(source_root, tmp_path / "collected")

    assert exc_info.value.actual == 1_001
    assert exc_info.value.limit == 200


def test_collect_source_figures_stays_within_low_fd_budget(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    output_dir = tmp_path / "collected"
    source_root.mkdir()
    for index in range(130):
        (source_root / f"figure-{index:03d}.png").write_bytes(b"image")

    completed = _run_with_low_nofile(
        """
        import resource
        import sys
        from pathlib import Path
        from paper_reader.arxiv_source import collect_source_figures

        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        figures = collect_source_figures(Path(sys.argv[1]), Path(sys.argv[2]))
        assert len(figures) == 130
        assert len(list(Path(sys.argv[2]).glob("*.png"))) == 130
        """,
        source_root,
        output_dir,
    )

    assert completed.returncode == 0, completed.stderr


def test_render_source_figure_pdfs_renders_pngs_with_pdf_figure_provenance(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "figures" / "figure1.pdf"
    pdf_path.parent.mkdir()
    _make_source_pdf(pdf_path)

    collected = collect_source_figures(tmp_path, tmp_path / "collected")
    rendered = render_source_figure_pdfs(collected, tmp_path / "rendered")

    assert len(rendered) == 1
    pdf_figure = rendered[0]
    assert pdf_figure["rel_path"] == "figures/figure1.pdf"
    assert pdf_figure["source"] == "pdf-figure"
    assert pdf_figure["media_type"] == "image"
    assert pdf_figure["image_path"].endswith(".png")
    assert Path(pdf_figure["image_path"]).exists()


def test_render_source_figure_pdfs_keeps_distinct_names_for_same_stem_pdfs(tmp_path: Path) -> None:
    first_pdf = tmp_path / "source-a" / "plot.pdf"
    second_pdf = tmp_path / "source-b" / "plot.pdf"
    first_pdf.parent.mkdir()
    second_pdf.parent.mkdir()
    _make_source_pdf(first_pdf)
    _make_source_pdf(second_pdf)

    rendered = render_source_figure_pdfs(
        [
            {
                "rel_path": "figures/plot.pdf",
                "media_type": "pdf",
                "image_path": str(first_pdf),
                "source_path": str(first_pdf),
                "source": "arxiv-source",
            },
            {
                "rel_path": "img/plot.pdf",
                "media_type": "pdf",
                "image_path": str(second_pdf),
                "source_path": str(second_pdf),
                "source": "arxiv-source",
            },
        ],
        tmp_path / "rendered",
    )

    assert len(rendered) == 2
    image_names = {Path(figure["image_path"]).name for figure in rendered}
    assert image_names == {"figures__plot.png", "img__plot.png"}


def test_render_source_figure_pdfs_rejects_output_name_collision_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    nested_pdf = tmp_path / "nested.pdf"
    flat_pdf = tmp_path / "flat.pdf"
    _make_source_pdf(nested_pdf)
    _make_source_pdf(flat_pdf)
    figures = [
        {
            "rel_path": "figures/plot.pdf",
            "media_type": "pdf",
            "image_path": str(nested_pdf),
            "source_path": str(nested_pdf),
            "source": "arxiv-source",
        },
        {
            "rel_path": "figures__plot.pdf",
            "media_type": "pdf",
            "image_path": str(flat_pdf),
            "source_path": str(flat_pdf),
            "source": "arxiv-source",
        },
    ]

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("colliding rendered names reached publication")

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="output name collision"):
        render_source_figure_pdfs(figures, tmp_path / "rendered")


def test_render_source_figure_pdfs_rejects_overlong_name_before_source_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"not-decoded")

    def forbidden_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("overlong rendered output name reached source decode")

    monkeypatch.setattr(arxiv_source.fitz, "open", forbidden_open)

    with pytest.raises(ValueError, match="file name.*limit"):
        render_source_figure_pdfs(
            [
                {
                    "rel_path": f"{'a' * 100}/{'b' * 100}/{'c' * 100}.pdf",
                    "media_type": "pdf",
                    "image_path": str(source_pdf),
                    "source_path": str(source_pdf),
                    "source": "arxiv-source",
                }
            ],
            tmp_path / "rendered",
        )


def test_render_source_figure_pdfs_rejects_output_replacement_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_pdf = tmp_path / "source.pdf"
    _make_source_pdf(source_pdf)
    output_dir = tmp_path / "rendered"
    original_publish = arxiv_source.publish_bytes_no_replace
    injected = False

    def publish_then_replace(
        content: bytes,
        destination: Path,
        **kwargs: object,
    ):
        nonlocal injected
        published = original_publish(content, destination, **kwargs)
        if destination.parent == output_dir and not injected:
            replacement = destination.with_name("replacement.png")
            replacement.write_bytes(b"forged")
            os.replace(replacement, destination)
            injected = True
        return published

    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", publish_then_replace)

    with pytest.raises(ValueError, match="output|tree|changed"):
        render_source_figure_pdfs(
            [
                {
                    "rel_path": "figures/source.pdf",
                    "media_type": "pdf",
                    "image_path": str(source_pdf),
                    "source_path": str(source_pdf),
                    "source": "arxiv-source",
                }
            ],
            output_dir,
        )

    assert injected is True
    assert (output_dir / "figures__source.png").read_bytes() == b"forged"


def test_render_source_figure_pdfs_rejects_collected_pdf_binding_mismatch(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_pdf = source_root / "source.pdf"
    _make_source_pdf(source_pdf)
    collected = collect_source_figures(source_root, tmp_path / "collected")
    assert len(collected) == 1
    collected_path = Path(collected[0]["image_path"])
    collected_path.write_bytes(collected_path.read_bytes() + b"\n%forged")

    with pytest.raises(ValueError, match="binding|bound|changed"):
        render_source_figure_pdfs(collected, tmp_path / "rendered")

    assert not (tmp_path / "rendered").exists()


def test_render_source_figure_pdfs_stays_within_low_fd_budget(tmp_path: Path) -> None:
    source_root = tmp_path / "pdfs"
    output_dir = tmp_path / "rendered"
    source_root.mkdir()
    for index in range(130):
        parent = source_root / f"parent-{index:03d}"
        parent.mkdir()
        (parent / f"figure-{index:03d}.pdf").write_bytes(b"pdf")

    completed = _run_with_low_nofile(
        """
        import resource
        import sys
        from pathlib import Path
        import paper_reader.arxiv_source as arxiv_source

        class FakeRect:
            width = 10
            height = 10

        class FakePixmap:
            def tobytes(self, _format):
                return b"png"

        class FakePage:
            rect = FakeRect()
            def get_pixmap(self):
                return FakePixmap()

        class FakeDocument:
            page_count = 1
            def __enter__(self):
                return self
            def __exit__(self, *_exc_info):
                return None
            def load_page(self, index):
                assert index == 0
                return FakePage()

        arxiv_source.fitz.open = lambda *args, **kwargs: FakeDocument()
        source_root = Path(sys.argv[1])
        figures = [
            {
                "rel_path": f"figures/figure-{index:03d}.pdf",
                "media_type": "pdf",
                "image_path": str(
                    source_root
                    / f"parent-{index:03d}"
                    / f"figure-{index:03d}.pdf"
                ),
                "source_path": str(
                    source_root
                    / f"parent-{index:03d}"
                    / f"figure-{index:03d}.pdf"
                ),
                "source": "arxiv-source",
            }
            for index in range(130)
        ]
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        rendered = arxiv_source.render_source_figure_pdfs(figures, Path(sys.argv[2]))
        assert len(rendered) == 130
        assert len(list(Path(sys.argv[2]).glob("*.png"))) == 130
        """,
        source_root,
        output_dir,
    )

    assert completed.returncode == 0, completed.stderr


def test_render_source_figure_pdfs_rejects_aggregate_pixels_before_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "pdfs"
    source_root.mkdir()
    figures: list[dict[str, str]] = []
    for index in range(2):
        figure_path = source_root / f"figure-{index}.pdf"
        figure_path.write_bytes(b"pdf")
        figures.append(
            {
                "rel_path": f"figures/figure-{index}.pdf",
                "media_type": "pdf",
                "image_path": str(figure_path),
                "source_path": str(figure_path),
                "source": "arxiv-source",
            }
        )

    class FakePage:
        rect = type("FakeRect", (), {"width": 10, "height": 10})()

    class FakeDocument:
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def load_page(self, index: int) -> FakePage:
            assert index == 0
            return FakePage()

    monkeypatch.setattr(arxiv_source.fitz, "open", lambda *args, **kwargs: FakeDocument())
    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, figure_max_pixels_total=150),
    )

    with pytest.raises(arxiv_source.FigurePixelLimitError) as exc_info:
        render_source_figure_pdfs(figures, tmp_path / "rendered")

    assert exc_info.value.actual == 200
    assert exc_info.value.limit == 150
    assert exc_info.value.resource_name == "figure_pixels_total"
    assert not (tmp_path / "rendered").exists()


def test_render_source_figure_pdfs_rejects_aggregate_output_bytes_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "pdfs"
    source_root.mkdir()
    figures: list[dict[str, str]] = []
    for index in range(2):
        figure_path = source_root / f"figure-{index}.pdf"
        figure_path.write_bytes(b"pdf")
        figures.append(
            {
                "rel_path": f"figures/figure-{index}.pdf",
                "media_type": "pdf",
                "image_path": str(figure_path),
                "source_path": str(figure_path),
                "source": "arxiv-source",
            }
        )

    class FakePixmap:
        def tobytes(self, _format: str) -> bytes:
            return b"png!"

    class FakePage:
        rect = type("FakeRect", (), {"width": 1, "height": 1})()

        def get_pixmap(self) -> FakePixmap:
            return FakePixmap()

    class FakeDocument:
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def load_page(self, index: int) -> FakePage:
            assert index == 0
            return FakePage()

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("aggregate rendered bytes reached publication")

    monkeypatch.setattr(arxiv_source.fitz, "open", lambda *args, **kwargs: FakeDocument())
    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, figure_max_bytes_total=7),
    )
    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="aggregate.*bytes"):
        render_source_figure_pdfs(figures, tmp_path / "rendered")

    assert not (tmp_path / "rendered").exists()


def test_render_source_figure_pdfs_rejects_aggregate_input_bytes_before_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    source_root = tmp_path / "pdfs"
    source_root.mkdir()
    figures: list[dict[str, str]] = []
    for index in range(2):
        figure_path = source_root / f"figure-{index}.pdf"
        figure_path.write_bytes(b"pdf")
        figures.append(
            {
                "rel_path": f"figures/figure-{index}.pdf",
                "media_type": "pdf",
                "image_path": str(figure_path),
                "source_path": str(figure_path),
                "source": "arxiv-source",
            }
        )

    class FakeRect:
        width = 10
        height = 10

    class FakePage:
        rect = FakeRect()

        def get_pixmap(self):
            class FakePixmap:
                def tobytes(self, _format: str) -> bytes:
                    return b"png"

            return FakePixmap()

    class FakeDocument:
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def load_page(self, index: int) -> FakePage:
            assert index == 0
            return FakePage()

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        pytest.fail("aggregate input cap was checked only after output publication")

    monkeypatch.setattr(
        arxiv_source,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, arxiv_expanded_max_bytes=5),
    )
    monkeypatch.setattr(arxiv_source.fitz, "open", lambda *args, **kwargs: FakeDocument())
    monkeypatch.setattr(arxiv_source, "publish_bytes_no_replace", forbidden_publish)

    with pytest.raises(ValueError, match="aggregate"):
        render_source_figure_pdfs(figures, tmp_path / "rendered")


def test_close_all_closes_every_owner_and_preserves_primary_error() -> None:
    import paper_reader.arxiv_source as arxiv_source

    closed: list[str] = []

    class Owner:
        def __init__(self, name: str, *, fails: bool = False) -> None:
            self.name = name
            self.fails = fails

        def close(self) -> None:
            closed.append(self.name)
            if self.fails:
                raise RuntimeError(f"close failed: {self.name}")

    owners = [Owner("first", fails=True), Owner("second"), Owner("third")]
    with pytest.raises(RuntimeError, match="close failed: first"):
        arxiv_source._close_all(owners)
    assert closed == ["first", "second", "third"]

    closed.clear()
    arxiv_source._close_all(owners, primary_error=ValueError("primary"))
    assert closed == ["first", "second", "third"]


def test_render_source_figure_pdfs_rejects_destination_leaf_symlink_without_overwrite(
    tmp_path: Path,
) -> None:
    figure_path = tmp_path / "figure.pdf"
    _make_source_pdf(figure_path)
    output_dir = tmp_path / "rendered"
    output_dir.mkdir()
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside")
    (output_dir / "figures__plot.png").symlink_to(sentinel)

    with pytest.raises((OSError, ValueError)):
        render_source_figure_pdfs(
            [
                {
                    "rel_path": "figures/plot.pdf",
                    "media_type": "pdf",
                    "image_path": str(figure_path),
                    "source_path": str(figure_path),
                    "source": "arxiv-source",
                }
            ],
            output_dir,
        )

    assert sentinel.read_bytes() == b"outside"
    assert (output_dir / "figures__plot.png").is_symlink()


def test_render_source_figure_pdfs_rejects_destination_root_symlink_without_writing_outside(
    tmp_path: Path,
) -> None:
    figure_path = tmp_path / "figure.pdf"
    _make_source_pdf(figure_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir = tmp_path / "rendered"
    output_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        render_source_figure_pdfs(
            [
                {
                    "rel_path": "figures/plot.pdf",
                    "media_type": "pdf",
                    "image_path": str(figure_path),
                    "source_path": str(figure_path),
                    "source": "arxiv-source",
                }
            ],
            output_dir,
        )

    assert not (outside / "figures__plot.png").exists()


def test_render_source_figure_pdfs_rejects_symlink_and_hardlink_inputs(
    tmp_path: Path,
) -> None:
    actual_pdf = tmp_path / "actual.pdf"
    _make_source_pdf(actual_pdf)
    linked_pdf = tmp_path / "linked.pdf"
    linked_pdf.symlink_to(actual_pdf)

    figure = {
        "rel_path": "figures/plot.pdf",
        "media_type": "pdf",
        "image_path": str(linked_pdf),
        "source_path": str(linked_pdf),
        "source": "arxiv-source",
    }
    with pytest.raises((OSError, ValueError)):
        render_source_figure_pdfs([figure], tmp_path / "symlink-rendered")

    linked_pdf.unlink()
    linked_pdf.hardlink_to(actual_pdf)
    with pytest.raises((OSError, ValueError)):
        render_source_figure_pdfs([figure], tmp_path / "hardlink-rendered")

    assert not (tmp_path / "symlink-rendered" / "figures__plot.png").exists()
    assert not (tmp_path / "hardlink-rendered" / "figures__plot.png").exists()


def test_render_source_figure_pdfs_detects_input_identity_swap_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    figure_path = tmp_path / "figure.pdf"
    replacement = tmp_path / "replacement.pdf"
    _make_source_pdf(figure_path)
    _make_source_pdf(replacement)
    output_dir = tmp_path / "rendered"
    original_publish = arxiv_source.publish_bytes_no_replace
    swapped = False

    def swap_source_then_publish(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            replacement.replace(figure_path)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        arxiv_source,
        "publish_bytes_no_replace",
        swap_source_then_publish,
    )

    with pytest.raises(ValueError, match="changed"):
        render_source_figure_pdfs(
            [
                {
                    "rel_path": "figures/plot.pdf",
                    "media_type": "pdf",
                    "image_path": str(figure_path),
                    "source_path": str(figure_path),
                    "source": "arxiv-source",
                }
            ],
            output_dir,
        )

    assert not (output_dir / "figures__plot.png").exists()


def test_render_source_figure_pdf_rejects_oversized_page_before_get_pixmap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.arxiv_source as arxiv_source

    figure_path = tmp_path / "oversized.pdf"
    figure_path.write_bytes(b"placeholder")

    class FakePage:
        rect = fitz.Rect(0, 0, 5_000, 5_000)

        def get_pixmap(self):
            pytest.fail("oversized source PDF reached get_pixmap")

    class FakeDocument:
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def load_page(self, index: int):
            assert index == 0
            return FakePage()

    monkeypatch.setattr(
        arxiv_source.fitz,
        "open",
        lambda *args, **kwargs: FakeDocument(),
    )

    with pytest.raises(ValueError, match=r"figure pixels 25000000 exceeds 20000000"):
        render_source_figure_pdfs(
            [
                {
                    "rel_path": "figures/oversized.pdf",
                    "media_type": "pdf",
                    "image_path": str(figure_path),
                    "source_path": str(figure_path),
                    "source": "arxiv-source",
                }
            ],
            tmp_path / "rendered",
        )

    assert not list((tmp_path / "rendered").glob("*.png"))


def test_download_arxiv_source_fetches_and_extracts_tarball(monkeypatch, tmp_path: Path) -> None:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name="paper/figures/a.png")
        payload = b"png"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    archive_bytes = archive_buffer.getvalue()
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return archive_bytes

    def fake_urlopen(url: str, *, timeout: float) -> FakeResponse:
        seen["url"] = url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    source_root = download_arxiv_source("2402.12345", tmp_path / "source-cache")

    assert seen["url"] == "https://arxiv.org/e-print/2402.12345"
    assert 0 < float(seen["timeout"]) <= 30
    assert source_root == (tmp_path / "source-cache" / "2402.12345")
    assert (source_root / "paper" / "figures" / "a.png").read_bytes() == b"png"


def test_download_arxiv_source_caches_old_style_ids_without_path_splits(monkeypatch, tmp_path: Path) -> None:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name="paper/figures/a.png")
        payload = b"png"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    archive_bytes = archive_buffer.getvalue()
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return archive_bytes

    def fake_urlopen(url: str, *, timeout: float) -> FakeResponse:
        seen["url"] = url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    source_root = download_arxiv_source("hep-th/9901001", tmp_path / "source-cache")

    assert seen["url"] == "https://arxiv.org/e-print/hep-th/9901001"
    assert 0 < float(seen["timeout"]) <= 30
    assert source_root == (tmp_path / "source-cache" / "hep-th__9901001")
    assert (source_root / "paper" / "figures" / "a.png").read_bytes() == b"png"
    assert not (tmp_path / "source-cache" / "hep-th").exists()


def test_download_arxiv_source_returns_none_on_network_failure(monkeypatch, tmp_path: Path) -> None:
    def fake_urlopen(url: str, *, timeout: float):  # pragma: no cover - explicit failure path
        assert 0 < timeout <= 30
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert download_arxiv_source("2402.12345", tmp_path / "source-cache") is None
