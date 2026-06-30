import json
from pathlib import Path

from typer.testing import CliRunner

from paperread import cli


runner = CliRunner()


def test_extract_figures_command_emits_json(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "figures"
    payload = {
        "arxiv_id": "2401.01234",
        "pdf_path": str(pdf_path),
        "candidate_count": 1,
        "selected_figures": [
            {
                "figure_id": "fig-1",
                "caption": "Figure 1. Workflow overview.",
                "caption_bbox": [0.0, 0.0, 10.0, 10.0],
                "bbox": [0.0, 0.0, 100.0, 120.0],
                "page": 1,
                "area": 12000.0,
                "image_path": str(output_dir / "fig-1.png"),
                "priority_score": 9.5,
                "source": "pdf-figure",
                "extraction_strategy": "deterministic",
                "extraction_confidence": 0.95,
                "fallback_reason": None,
                "needs_fallback": False,
            }
        ],
        "source_attempts": [{"stage": "resolve", "status": "resolved", "arxiv_id": "2401.01234"}],
        "warnings": [],
    }
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
        return payload

    monkeypatch.setattr(cli, "extract_figures", fake_extract_figures, raising=False)

    result = runner.invoke(
        cli.app,
        [
            "extract-figures",
            str(pdf_path),
            "--output-dir",
            str(output_dir),
            "--top-k",
            "2",
            "--max-pages",
            "3",
            "--arxiv-id",
            "2401.01234",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload
    assert seen == {
        "pdf_path": pdf_path,
        "output_dir": output_dir,
        "top_k": 2,
        "max_pages": 3,
        "arxiv_id": "2401.01234",
        "item_details": None,
        "enable_ocr_fallback": False,
    }
