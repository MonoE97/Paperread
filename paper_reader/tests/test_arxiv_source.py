from __future__ import annotations

import io
import tarfile
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
    monkeypatch.setattr(arxiv_source, "extract_source_package", fake_extract)

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

    monkeypatch.setattr("paper_reader.arxiv_source.shutil.copy2", forbidden_copy)

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

    monkeypatch.setattr(arxiv_source.shutil, "copy2", forbidden_copy)

    with pytest.raises(arxiv_source.FigureCandidateLimitError) as exc_info:
        collect_source_figures(source_root, tmp_path / "collected")

    assert exc_info.value.actual == 1_001
    assert exc_info.value.limit == 200


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

    monkeypatch.setattr(arxiv_source.fitz, "open", lambda _path: FakeDocument())

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
