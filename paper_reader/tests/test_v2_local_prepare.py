from __future__ import annotations

import hashlib
import json
import os
import fcntl
import multiprocessing
import queue
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import fitz
from typer.testing import CliRunner

from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.local_lifecycle import initialize_local_run
from paper_reader.public_cli import app


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "minimal.pdf"


def _process_prepare_local(run_dir: str, start, messages) -> None:
    from paper_reader.evidence_bundle import prepare_local_evidence

    messages.put(("ready", None))
    start.wait(timeout=10)
    try:
        prepared = prepare_local_evidence(Path(run_dir), figure_limit=0)
    except Exception as exc:
        messages.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        messages.put(("done", str(prepared.evidence_dir)))


def _invoke(arguments: list[str]):
    return CliRunner().invoke(app, arguments)


def _result_payload(result) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    return payload

def test_prepare_builds_one_immutable_complete_pdf_evidence_bundle(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "prepared"
    assert payload["data"]["complete"] is True
    assert payload["data"]["degraded"] is False
    evidence_dir = Path(payload["data"]["evidence_dir"])
    assert evidence_dir.parent == run_dir / "evidence"
    assert sorted(path.name for path in evidence_dir.iterdir()) == [
        "context.md",
        "evidence.json",
        "extract.json",
        "metadata.json",
        "secondary_sources.json",
        "section_context.md",
    ]

    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "paper_reader.evidence.v2-internal"
    assert manifest["run_id"] == json.loads((run_dir / "run.json").read_text())["run_id"]
    assert manifest["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["complete"] is True
    assert manifest["degraded"] is False
    assert manifest["preview_pages"] is None
    assert manifest["pages"] == [1]
    assert manifest["figures"] == []
    assert {item["role"] for item in manifest["files"]} == {
        "context",
        "extract",
        "metadata",
        "secondary_sources",
        "section_context",
    }
    for artifact in manifest["files"]:
        artifact_path = run_dir / artifact["path"]
        assert artifact_path.is_file()
        assert artifact["size_bytes"] == artifact_path.stat().st_size
        assert artifact["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    resource_checks = {item["name"]: item for item in manifest["resource_checks"]}
    assert resource_checks["pdf_page_count"]["status"] == "passed"
    assert resource_checks["extracted_text_chars"]["status"] == "passed"
    assert resource_checks["figure_limit"]["actual"] == 0
    run_manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_manifest["status"] == "prepared"
    assert any(item["role"] == "evidence_manifest" for item in run_manifest["artifacts"])
    final_run_size = sum(
        path.stat().st_size
        for path in run_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    assert resource_checks["run_size_bytes"]["actual"] == final_run_size


def test_prepare_extracts_from_the_pdf_identity_verified_before_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    replacement = tmp_path / "replacement.pdf"
    for path, text in (
        (source, "ORIGINAL VERIFIED CONTENT"),
        (replacement, "REPLACEMENT CONTENT"),
    ):
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), text)
        document.save(path)
        document.close()
    original_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    original_page_count = evidence_bundle._page_count

    def replace_source_after_page_count(path: Path) -> int:
        page_count = original_page_count(path)
        os.replace(replacement, source)
        return page_count

    monkeypatch.setattr(evidence_bundle, "_page_count", replace_source_after_page_count)

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 0, result.stderr
    evidence_dir = Path(_result_payload(result)["data"]["evidence_dir"])
    extraction = json.loads((evidence_dir / "extract.json").read_text(encoding="utf-8"))
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert "ORIGINAL VERIFIED CONTENT" in extraction["text"]
    assert "REPLACEMENT CONTENT" not in extraction["text"]
    assert manifest["source_sha256"] == original_sha256


@pytest.mark.skipif(os.name != "posix", reason="anonymous descriptor paths are POSIX-only")
def test_prepare_uses_an_unlinked_verified_pdf_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    original_page_count = evidence_bundle._page_count
    observed: dict[str, int] = {}

    def inspect_snapshot(path: Path) -> int:
        observed["nlink"] = os.stat(path).st_nlink
        return original_page_count(path)

    monkeypatch.setattr(evidence_bundle, "_page_count", inspect_snapshot)

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 0, result.stderr
    assert observed["nlink"] == 0


def test_prepare_page_count_failure_reports_only_the_original_pdf_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    observed: dict[str, str] = {}

    def fail_page_count(path: Path) -> int:
        observed["internal_path"] = str(path)
        raise evidence_bundle.EvidenceBundleError(
            "invalid_local_pdf",
            f"local PDF cannot be opened for preparation: {path}",
            data={"source_pdf": str(path)},
        )

    monkeypatch.setattr(evidence_bundle, "_page_count", fail_page_count)

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "invalid_local_pdf"
    assert payload["data"]["source_pdf"] == str(source.resolve())
    rendered = result.stdout + result.stderr
    assert str(source.resolve()) in rendered
    assert observed["internal_path"] not in rendered


def test_prepare_text_extraction_failure_reports_only_the_original_pdf_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    observed: dict[str, str] = {}

    def fail_extraction(_pdf_path: Path, **kwargs) -> dict:
        internal_path = str(kwargs["_verified_pdf_path"])
        observed["internal_path"] = internal_path
        raise ValueError(f"text extraction failed: {internal_path}")

    monkeypatch.setattr(evidence_bundle, "extract_pdf", fail_extraction)

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "invalid_local_pdf"
    assert payload["data"]["source_pdf"] == str(source.resolve())
    rendered = result.stdout + result.stderr
    assert str(source.resolve()) in rendered
    assert observed["internal_path"] not in rendered


def test_prepare_figure_degradation_persists_only_the_original_pdf_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    observed: dict[str, str] = {}

    def fail_figure_extraction(_pdf_path: Path, output_dir: Path, **kwargs) -> dict:
        assert output_dir.name == "figures"
        internal_path = str(kwargs["_verified_pdf_path"])
        observed["internal_path"] = internal_path
        raise RuntimeError(f"figure read failed: {internal_path}")

    monkeypatch.setattr(
        "paper_reader.evidence_figures.extract_figures",
        fail_figure_extraction,
    )

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "1"])

    assert result.exit_code == 0, result.stderr
    evidence_dir = Path(_result_payload(result)["data"]["evidence_dir"])
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    figure_check = next(
        item for item in manifest["resource_checks"] if item["name"] == "figure_extraction"
    )
    assert figure_check["message"] == (
        f"RuntimeError: figure read failed: {source.resolve()}"
    )
    persisted = json.dumps(manifest, ensure_ascii=False) + result.stdout + result.stderr
    assert observed["internal_path"] not in persisted


def test_prepare_succeeds_with_no_table_members_when_numeric_signals_have_no_section(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Table 1 Baseline RMSE 0.25 MAE 0.13 R2 0.91 speedup 10x.",
    )
    document.save(source)
    document.close()
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 0, result.stderr
    evidence_dir = Path(_result_payload(result)["data"]["evidence_dir"])
    extraction = json.loads((evidence_dir / "extract.json").read_text(encoding="utf-8"))
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert extraction["sections"] == []
    assert extraction["table_candidates"] == []
    assert manifest["table_candidates"] == []


def test_prepare_rejects_invalid_manifest_membership_before_tree_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    run_before = (run_dir / "run.json").read_bytes()
    original_build = evidence_bundle.build_evidence_manifest

    def build_invalid_manifest(**kwargs):
        manifest = original_build(**kwargs)
        return manifest.model_copy(update={"files": (*manifest.files, manifest.files[0])})

    monkeypatch.setattr(evidence_bundle, "build_evidence_manifest", build_invalid_manifest)

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "evidence_closed_world_mismatch"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "evidence").exists()
    assert not list(run_dir.glob(".*.staging"))


def test_prepare_reloads_inside_the_shared_run_advisory_lock(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    lock_path = run_dir / ".run.lock"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    messages = context.Queue()

    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        process = context.Process(
            target=_process_prepare_local,
            args=(str(run_dir), start, messages),
        )
        process.start()
        assert messages.get(timeout=10) == ("ready", None)
        start.set()
        with pytest.raises(queue.Empty):
            messages.get(timeout=1.5)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    status, detail = messages.get(timeout=10)
    process.join(timeout=10)
    assert process.exitcode == 0
    assert status == "done", detail


def test_prepare_extracts_default_figures_without_enabling_arxiv_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "2401.00001.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    observed: dict[str, object] = {}

    def fake_extract_figures(pdf_path: Path, output_dir: Path, **kwargs) -> dict:
        observed["pdf_path"] = pdf_path
        observed.update(kwargs)
        output_dir.mkdir(parents=True)
        image_path = output_dir / "figure-p1-1.png"
        image_path.write_bytes(b"bounded-figure")
        return {
            "arxiv_id": "2401.00001",
            "pdf_path": str(pdf_path),
            "candidate_count": 1,
            "selected_figures": [
                {
                    "figure_id": "p1-f1",
                    "caption": "Figure 1. Overview.",
                    "caption_confidence": 0.95,
                    "page": 1,
                    "source": "deterministic-pdf",
                    "image_path": str(image_path),
                    "priority_score": 1.0,
                    "needs_fallback": False,
                    "visual_quality": {"status": "ok", "warnings": [], "width": 10, "height": 10},
                    "evidence_tier": "pixel_verified",
                    "evidence_tier_reason": "test",
                }
            ],
            "source_attempts": [{"stage": "resolve", "status": "skipped", "reason": "network_disabled"}],
            "warnings": [],
        }

    import paper_reader.evidence_figures as evidence_figures

    monkeypatch.setattr(evidence_figures, "extract_figures", fake_extract_figures)

    result = _invoke(["run", "prepare", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    evidence_dir = Path(_result_payload(result)["data"]["evidence_dir"])
    assert observed["pdf_path"] == source
    assert observed["top_k"] == 4
    assert observed["allow_network_source"] is False
    assert (evidence_dir / "figures.json").is_file()
    assert (evidence_dir / "figure_context.md").is_file()
    assert (evidence_dir / "figures" / "figure-p1-1.png").read_bytes() == b"bounded-figure"
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert manifest["figures"] == [
        {
            "figure_id": "p1-f1",
            "page": 1,
            "artifact_path": f"evidence/{manifest['evidence_id']}/figures/figure-p1-1.png",
        }
    ]
    assert {item["role"] for item in manifest["files"]} >= {
        "figure_context",
        "figure_image",
        "figures",
    }


def test_prepare_degrades_complete_evidence_when_figure_candidate_cap_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    def too_many_candidates(_pdf_path: Path, output_dir: Path, **_kwargs) -> dict:
        output_dir.mkdir(parents=True)
        return {
            "arxiv_id": None,
            "pdf_path": str(source),
            "candidate_count": 201,
            "selected_figures": [],
            "source_attempts": [],
            "warnings": [],
        }

    monkeypatch.setattr("paper_reader.evidence_figures.extract_figures", too_many_candidates)

    result = _invoke(["run", "prepare", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["data"]["complete"] is True
    assert payload["data"]["degraded"] is True
    evidence_dir = Path(payload["data"]["evidence_dir"])
    assert not (evidence_dir / "figures.json").exists()
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    checks = {item["name"]: item for item in manifest["resource_checks"]}
    assert checks["figure_candidate_count"] == {
        "name": "figure_candidate_count",
        "status": "degraded",
        "actual": 201,
        "limit": 200,
        "message": "figure candidate count exceeds the V2 cap",
    }


@pytest.mark.parametrize(
    ("case", "expected_name", "expected_actual", "expected_limit"),
    [
        ("candidates", "figure_candidate_count", 1_002, 200),
        ("pixels", "figure_pixels_each", 25_000_000, 20_000_000),
        ("total_pixels", "figure_pixels_total", 96_000_000, 80_000_000),
    ],
)
def test_preallocation_resource_limit_degrades_only_complete_pdf_evidence(
    case: str,
    expected_name: str,
    expected_actual: int,
    expected_limit: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader.figures import FigureCandidateLimitError, FigurePixelLimitError

    def preallocation_failure(*_args, **_kwargs):
        if case == "candidates":
            raise FigureCandidateLimitError(actual=1_002, limit=200)
        if case == "pixels":
            raise FigurePixelLimitError(actual=25_000_000, limit=20_000_000)
        raise FigurePixelLimitError(
            actual=96_000_000,
            limit=80_000_000,
            resource_name="figure_pixels_total",
        )

    monkeypatch.setattr("paper_reader.evidence_figures.extract_figures", preallocation_failure)

    preview_source = tmp_path / f"preview-{case}.pdf"
    shutil.copyfile(FIXTURE_PDF, preview_source)
    preview_initialized = _invoke(["run", "init-local", str(preview_source)])
    preview_run = Path(_result_payload(preview_initialized)["data"]["run_dir"])

    preview_result = _invoke(
        ["run", "prepare", str(preview_run), "--preview-pages", "1"]
    )

    assert preview_result.exit_code == 1
    assert _result_payload(preview_result)["code"] == "figure_extraction_failed"
    assert not (preview_run / "evidence").exists()

    complete_source = tmp_path / f"complete-{case}.pdf"
    shutil.copyfile(FIXTURE_PDF, complete_source)
    complete_initialized = _invoke(["run", "init-local", str(complete_source)])
    complete_run = Path(_result_payload(complete_initialized)["data"]["run_dir"])

    complete_result = _invoke(["run", "prepare", str(complete_run)])

    assert complete_result.exit_code == 0, complete_result.stderr
    payload = _result_payload(complete_result)
    assert payload["data"]["complete"] is True
    assert payload["data"]["degraded"] is True
    manifest = json.loads(
        (Path(payload["data"]["evidence_dir"]) / "evidence.json").read_text(
            encoding="utf-8"
        )
    )
    checks = {item["name"]: item for item in manifest["resource_checks"]}
    assert checks[expected_name]["status"] == "degraded"
    assert checks[expected_name]["actual"] == expected_actual
    assert checks[expected_name]["limit"] == expected_limit


@pytest.mark.parametrize(
    ("case", "figure_limit", "expected_name", "expected_actual", "expected_limit"),
    [
        ("each_pixels", 1, "figure_pixels_each", 25_000_000, 20_000_000),
        ("total_pixels", 5, "figure_pixels_total", 82_000_000, 80_000_000),
        ("total_bytes", 1, "figure_bytes_total", (64 * 1024 * 1024) + 1, 64 * 1024 * 1024),
    ],
)
def test_prepare_degrades_complete_evidence_on_figure_image_resource_caps(
    case: str,
    figure_limit: int,
    expected_name: str,
    expected_actual: int,
    expected_limit: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    def resource_heavy_figures(_pdf_path: Path, output_dir: Path, **_kwargs) -> dict:
        output_dir.mkdir(parents=True)
        if case == "total_pixels":
            dimensions = [(4100, 4000)] * 5
        elif case == "each_pixels":
            dimensions = [(5000, 5000)]
        else:
            dimensions = [(10, 10)]
        selected = []
        for index, (width, height) in enumerate(dimensions, start=1):
            image_path = output_dir / f"figure-{index}.png"
            if case == "total_bytes":
                with image_path.open("wb") as handle:
                    handle.seek((64 * 1024 * 1024))
                    handle.write(b"\0")
            else:
                image_path.write_bytes(b"figure")
            selected.append(
                {
                    "figure_id": f"p1-f{index}",
                    "caption": f"Figure {index}.",
                    "caption_confidence": 0.95,
                    "page": 1,
                    "source": "deterministic-pdf",
                    "image_path": str(image_path),
                    "priority_score": 1.0,
                    "needs_fallback": False,
                    "visual_quality": {
                        "status": "ok",
                        "warnings": [],
                        "width": width,
                        "height": height,
                    },
                    "evidence_tier": "pixel_verified",
                    "evidence_tier_reason": "test",
                }
            )
        return {
            "arxiv_id": None,
            "pdf_path": str(source),
            "candidate_count": len(selected),
            "selected_figures": selected,
            "source_attempts": [],
            "warnings": [],
        }

    monkeypatch.setattr("paper_reader.evidence_figures.extract_figures", resource_heavy_figures)

    result = _invoke(
        ["run", "prepare", str(run_dir), "--figure-limit", str(figure_limit)]
    )

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["data"]["degraded"] is True
    evidence_dir = Path(payload["data"]["evidence_dir"])
    assert not (evidence_dir / "figures.json").exists()
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    checks = {item["name"]: item for item in manifest["resource_checks"]}
    assert checks[expected_name]["status"] == "degraded"
    assert checks[expected_name]["actual"] == expected_actual
    assert checks[expected_name]["limit"] == expected_limit


def test_prepare_atomic_publication_fault_leaves_run_unprepared_and_no_partial_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    run_before = (run_dir / "run.json").read_bytes()

    def injected_failure(_staging: Path, _destination: Path) -> Path:
        raise OSError("injected evidence publication failure")

    monkeypatch.setattr("paper_reader.evidence_bundle.atomic_publish_tree", injected_failure)

    result = _invoke(
        ["run", "prepare", str(run_dir), "--figure-limit", "0"]
    )

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "evidence_publication_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "evidence").exists()
    assert not list(run_dir.glob(".*.staging"))


def test_prepare_run_update_fault_leaves_only_unbound_orphan_and_retry_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    run_before = (run_dir / "run.json").read_bytes()
    original_write = evidence_bundle.atomic_write_json
    failed = False

    def fail_once(path: Path, value, **kwargs):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure after evidence tree publication")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(evidence_bundle, "atomic_write_json", fail_once)

    first = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "evidence_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_dirs = tuple((run_dir / "evidence").iterdir())
    assert len(orphan_dirs) == 1

    second = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert second.exit_code == 0, second.stderr
    run = json.loads((run_dir / "run.json").read_text())
    bound_paths = {
        item["path"] for item in run["artifacts"] if item["role"] == "evidence_manifest"
    }
    assert len(bound_paths) == 1
    assert not any(path.startswith(orphan_dirs[0].relative_to(run_dir).as_posix()) for path in bound_paths)


def test_explicit_preview_pages_always_produces_incomplete_evidence(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    result = _invoke(
        [
            "run",
            "prepare",
            str(run_dir),
            "--preview-pages",
            "1",
            "--figure-limit",
            "0",
        ]
    )

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "prepared_preview"
    assert payload["data"]["complete"] is False
    evidence_dir = Path(payload["data"]["evidence_dir"])
    manifest = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
    assert manifest["preview_pages"] == 1
    assert json.loads((evidence_dir / "extract.json").read_text())["extracted_pages"] == 1


def test_preview_figure_failure_is_blocking_and_leaves_no_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    run_before = (run_dir / "run.json").read_bytes()

    def figure_failure(*_args, **_kwargs):
        raise RuntimeError("injected figure failure")

    monkeypatch.setattr("paper_reader.evidence_figures.extract_figures", figure_failure)

    result = _invoke(
        ["run", "prepare", str(run_dir), "--preview-pages", "1"]
    )

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "figure_extraction_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "evidence").exists()


def test_full_pdf_figure_failure_is_recorded_as_degraded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    def figure_failure(*_args, **_kwargs):
        raise RuntimeError("injected figure failure")

    monkeypatch.setattr("paper_reader.evidence_figures.extract_figures", figure_failure)

    result = _invoke(["run", "prepare", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["data"]["complete"] is True
    assert payload["data"]["degraded"] is True
    manifest = json.loads(
        (Path(payload["data"]["evidence_dir"]) / "evidence.json").read_text(encoding="utf-8")
    )
    check = next(item for item in manifest["resource_checks"] if item["name"] == "figure_extraction")
    assert check["status"] == "degraded"
    assert "injected figure failure" in check["message"]


def test_full_pdf_figure_fallback_removes_all_partial_anchored_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])

    def fake_extract(_source: Path, *, output_dir: Path, **_kwargs):
        output_dir.mkdir(parents=True)
        image_path = output_dir / "figure-p1-1.png"
        image_path.write_bytes(b"bounded-figure")
        return {
            "candidate_count": 1,
            "selected_figures": [
                {
                    "figure_id": "p1-f1",
                    "page": 1,
                    "image_path": str(image_path),
                    "visual_quality": {
                        "width": 10,
                        "height": 10,
                    },
                }
            ],
        }

    import paper_reader.evidence_figures as evidence_figures

    original_write = evidence_figures.atomic_write_bytes

    def fail_context(path: Path, content: bytes, **kwargs):
        if Path(path).name == "figure_context.md":
            raise OSError("injected context write failure")
        return original_write(path, content, **kwargs)

    monkeypatch.setattr(evidence_figures, "extract_figures", fake_extract)
    monkeypatch.setattr(evidence_figures, "atomic_write_bytes", fail_context)

    result = _invoke(["run", "prepare", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["data"]["degraded"] is True
    evidence_dir = Path(payload["data"]["evidence_dir"])
    assert not (evidence_dir / "figures").exists()
    assert not (evidence_dir / "figures.json").exists()
    assert not (evidence_dir / "figure_context.md").exists()


def test_prepare_blocks_page_cap_before_extraction_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr("paper_reader.evidence_bundle._page_count", lambda _path: 501)

    def forbidden_extract(*_args, **_kwargs):
        pytest.fail("page-cap failure reached text extraction")

    monkeypatch.setattr("paper_reader.evidence_bundle.extract_pdf", forbidden_extract)

    result = _invoke(["run", "prepare", str(run_dir)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "pdf_page_limit_exceeded"
    assert payload["data"] == {"page_count": 501, "max_pages": 500}
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "evidence").exists()


def test_prepare_blocks_text_and_run_size_caps_without_prepared_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    original_policy = evidence_bundle.V2_RESOURCE_POLICY
    for cap_name, expected_code in (
        ("extracted_text_max_chars", "extracted_text_limit_exceeded"),
        ("run_max_bytes", "run_size_limit_exceeded"),
    ):
        case_dir = tmp_path / cap_name
        case_dir.mkdir()
        source = case_dir / "paper.pdf"
        shutil.copyfile(FIXTURE_PDF, source)
        initialized = _invoke(["run", "init-local", str(source)])
        run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
        run_before = (run_dir / "run.json").read_bytes()
        monkeypatch.setattr(
            evidence_bundle,
            "V2_RESOURCE_POLICY",
            replace(
                original_policy,
                **{cap_name: 1},
            ),
        )

        result = _invoke(
            ["run", "prepare", str(run_dir), "--figure-limit", "0"]
        )

        assert result.exit_code == 1
        assert _result_payload(result)["code"] == expected_code
        assert (run_dir / "run.json").read_bytes() == run_before
        assert not (run_dir / "evidence").exists()


def test_repeat_prepare_counts_existing_evidence_before_publishing_another_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.evidence_bundle as evidence_bundle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    first = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])
    assert first.exit_code == 0, first.stderr
    run_before = (run_dir / "run.json").read_bytes()
    evidence_before = sorted(path.name for path in (run_dir / "evidence").iterdir())
    current_size = sum(path.stat().st_size for path in run_dir.rglob("*") if path.is_file())
    monkeypatch.setattr(
        evidence_bundle,
        "V2_RESOURCE_POLICY",
        replace(evidence_bundle.V2_RESOURCE_POLICY, run_max_bytes=current_size + 1),
    )

    second = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert second.exit_code == 1
    assert _result_payload(second)["code"] == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert sorted(path.name for path in (run_dir / "evidence").iterdir()) == evidence_before


def test_prepare_revalidates_initialized_source_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    run_before = (run_dir / "run.json").read_bytes()
    source.write_bytes(source.read_bytes() + b"\n% source drift")

    result = _invoke(["run", "prepare", str(run_dir)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "source_changed"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "evidence").exists()
