import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from zotero_paperread import workflow
from zotero_paperread.cli import app


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_create_run_command_emits_project_local_run_dir(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "create-run",
            "--title",
            "Deep‐Learning Assisted Polarization Holograms",
            "--base-dir",
            str(tmp_path / "runs"),
            "--today",
            "2026-04-24",
            "--item-key",
            "8HCYMEEB",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["slug"] == "deep-learning-assisted-polarization-holograms"
    assert payload["date"] == "2026-04-24"
    assert payload["run_dir"].endswith("runs/2026-04-24/deep-learning-assisted-polarization-holograms")
    manifest_path = Path(payload["manifest_path"])
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["title"] == "Deep‐Learning Assisted Polarization Holograms"
    assert manifest["item_key"] == "8HCYMEEB"
    assert manifest["status"] == "initialized"


def test_create_run_command_uses_allocated_directory_name_for_collision_slug(tmp_path: Path) -> None:
    runner = CliRunner()
    base_dir = tmp_path / "runs"

    first = runner.invoke(
        app,
        [
            "create-run",
            "--title",
            "Same Title",
            "--base-dir",
            str(base_dir),
            "--today",
            "2026-04-24",
        ],
    )
    second = runner.invoke(
        app,
        [
            "create-run",
            "--title",
            "Same Title",
            "--base-dir",
            str(base_dir),
            "--today",
            "2026-04-24",
        ],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0

    payload = json.loads(second.stdout)
    manifest_path = Path(payload["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert Path(payload["run_dir"]).name == "same-title-2"
    assert payload["slug"] == "same-title-2"
    assert manifest["slug"] == "same-title-2"


def test_prepare_item_command_outputs_bundle_paths(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Abstract\nA short paper.", "Methods\nExtra page for truncation."])
    details_path = tmp_path / "details.json"
    workdir = tmp_path / "bundle"
    figures_payload = {
        "arxiv_id": "2401.01234",
        "pdf_path": str(pdf_path),
        "candidate_count": 1,
        "selected_figures": [],
        "source_attempts": [{"stage": "resolve", "status": "resolved", "arxiv_id": "2401.01234"}],
        "warnings": [],
    }
    details_path.write_text(
        json.dumps(
            {
                "key": "CLI123",
                "title": "CLI Paper",
                "creators": [{"firstName": "A", "lastName": "B"}],
                "date": "2026",
                "DOI": "10.1000/cli",
                "url": "https://example.org/cli",
                "zoteroUrl": "zotero://select/library/items/CLI123",
                "abstractNote": "CLI abstract.",
                "attachments": [
                    {
                        "key": "PDF1",
                        "filename": "paper.pdf",
                        "contentType": "application/pdf",
                        "path": str(pdf_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_extract_figures(
        requested_pdf_path: Path,
        output_dir: Path,
        top_k: int = 4,
        max_pages: int | None = None,
        *,
        arxiv_id: str | None = None,
        item_details: dict | None = None,
        enable_ocr_fallback: bool = False,
    ) -> dict:
        seen.update(
            {
                "pdf_path": requested_pdf_path,
                "output_dir": output_dir,
                "top_k": top_k,
                "max_pages": max_pages,
                "arxiv_id": arxiv_id,
                "item_details": item_details,
                "enable_ocr_fallback": enable_ocr_fallback,
            }
        )
        return figures_payload

    monkeypatch.setattr(workflow, "extract_figures", fake_extract_figures)
    runner = CliRunner()

    result = runner.invoke(app, ["prepare-item", str(details_path), "--workdir", str(workdir), "--max-pages", "1"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["has_pdf"] is True
    assert payload["arxiv_id"] == "2401.01234"
    assert payload["warnings"] == ["truncated_to_1_pages"]
    assert Path(payload["metadata_json"]).exists()
    assert Path(payload["extract_json"]).exists()
    assert Path(payload["context_md"]).exists()
    assert Path(payload["figures_json"]).exists()
    assert Path(payload["figure_context_md"]).exists()
    assert seen["pdf_path"] == pdf_path
    assert seen["output_dir"] == workdir / "figures"
    assert seen["top_k"] == 4
    assert seen["max_pages"] == 1
    assert seen["arxiv_id"] is None
    assert seen["enable_ocr_fallback"] is False


def test_prepare_item_command_processes_full_pdf_by_default(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Abstract\nFirst page.", "Methods\nSecond page."])
    details_path = tmp_path / "details.json"
    workdir = tmp_path / "bundle"
    details_path.write_text(
        json.dumps(
            {
                "key": "CLI123",
                "title": "CLI Paper",
                "creators": [{"firstName": "A", "lastName": "B"}],
                "date": "2026",
                "attachments": [
                    {
                        "key": "PDF1",
                        "filename": "paper.pdf",
                        "contentType": "application/pdf",
                        "path": str(pdf_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_extract_figures(
        requested_pdf_path: Path,
        output_dir: Path,
        top_k: int = 4,
        max_pages: int | None = None,
        *,
        arxiv_id: str | None = None,
        item_details: dict | None = None,
        enable_ocr_fallback: bool = False,
    ) -> dict:
        seen["max_pages"] = max_pages
        return {
            "arxiv_id": None,
            "pdf_path": str(requested_pdf_path),
            "candidate_count": 0,
            "selected_figures": [],
            "source_attempts": [],
            "warnings": [],
        }

    monkeypatch.setattr(workflow, "extract_figures", fake_extract_figures)
    runner = CliRunner()

    result = runner.invoke(app, ["prepare-item", str(details_path), "--workdir", str(workdir)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    extract = json.loads((workdir / "extract.json").read_text(encoding="utf-8"))
    assert seen["max_pages"] is None
    assert "truncated_to_1_pages" not in payload["warnings"]
    assert "Second page." in extract["text"]
