from __future__ import annotations

import json

from paper_reader.summary_preflight_cli import main


def _write_summary(tmp_path, payload: dict[str, object]):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_summary_preflight_reports_english_rendered_prose(tmp_path, capsys) -> None:
    path = _write_summary(
        tmp_path,
        {
            "tldr": "Use Coble creep to improve interface contact.",
            "research_object": "Li6PS5Cl 固态电解质",
        },
    )

    assert main([str(path)]) == 1
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is False
    assert result["summary_path"] == str(path.resolve())
    assert result["issues"] == [
        {
            "code": "rendered_note_field_english_prose",
            "message": (
                "tldr should use Chinese prose unless it is a proper noun/key: "
                "Use Coble creep to improve interface contact."
            ),
        }
    ]


def test_summary_preflight_accepts_chinese_prose_with_technical_terms(tmp_path, capsys) -> None:
    path = _write_summary(
        tmp_path,
        {
            "tldr": "利用 Coble creep 改善 Li6PS5Cl 固态电解质的界面接触。",
            "research_object": "Li6PS5Cl 固态电解质",
            "note_labels": ["solid-state-electrolyte"],
        },
    )

    assert main([str(path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result == {
        "ok": True,
        "summary_path": str(path.resolve()),
        "issues": [],
    }


def test_summary_preflight_rejects_invalid_json(tmp_path, capsys) -> None:
    path = tmp_path / "summary.json"
    path.write_text("{not json", encoding="utf-8")

    assert main([str(path)]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is False
    assert result["code"] == "invalid_summary_json"
    assert result["summary_path"] == str(path.resolve())
