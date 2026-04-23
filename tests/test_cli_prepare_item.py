import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from zotero_paperread.cli import app


def make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_prepare_item_command_outputs_bundle_paths(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, "Abstract\nA short paper.")
    details_path = tmp_path / "details.json"
    workdir = tmp_path / "bundle"
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
    runner = CliRunner()

    result = runner.invoke(app, ["prepare-item", str(details_path), "--workdir", str(workdir), "--max-pages", "1"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["has_pdf"] is True
    assert Path(payload["metadata_json"]).exists()
    assert Path(payload["extract_json"]).exists()
    assert Path(payload["context_md"]).exists()
