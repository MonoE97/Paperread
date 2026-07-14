from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from paper_reader.summary_preflight_cli import main


def _write_summary(tmp_path, payload: dict[str, object]):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _valid_summary(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "paper_reader.summary.v2",
        "summary_id": "summary_test",
        "run_id": "run_test",
        "created_at": "2026-07-14T00:00:00Z",
        "evidence_digest": "a" * 64,
        "paper_type": "research_article",
        "trust_status": "usable_with_caveats",
        "review_status": "not_reviewed",
        "improvement_status": "not_needed",
        "trust_rationale": "正文证据与主要结论可以相互核对。",
        "one_sentence_summary": "本文分析了堆压对固态电解质变形的影响。",
        "abstract_translation": "作者比较了不同堆压条件下材料的力学响应。",
        "research_question": "堆压如何改变固态电解质的黏塑性变形？",
        "method": "结合原位表征与力学模型分析界面接触演化。",
        "experiments": "实验比较了不同压力和时间条件下的形貌与响应。",
        "ai4s_relevance": "结果可用于固态电池界面与力学耦合建模。",
        "key_points": ["堆压改变界面接触。"],
        "contributions": ["建立压力与黏塑性响应之间的联系。"],
        "limitations": ["适用范围受材料体系限制。"],
        "follow_up_keywords": ["固态电解质", "黏塑性"],
        "evidence_summary": [
            {
                "claim": "堆压会改变材料变形行为。",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "context.md page 1",
                        "summary": "正文给出了压力相关的实验结果。",
                    }
                ],
                "confidence": "medium",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_summary_preflight_reports_english_rendered_prose(tmp_path, capsys) -> None:
    path = _write_summary(
        tmp_path,
        _valid_summary(
            tldr="Use Coble creep to improve interface contact.",
            research_object="Li6PS5Cl 固态电解质",
        ),
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
        _valid_summary(
            tldr="利用 Coble creep 改善 Li6PS5Cl 固态电解质的界面接触。",
            research_object="Li6PS5Cl 固态电解质",
            note_labels=["solid-state-electrolyte"],
        ),
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


def test_summary_preflight_rejects_unversioned_summary(tmp_path, capsys) -> None:
    path = _write_summary(tmp_path, {})

    assert main([str(path)]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is False
    assert result["code"] == "unsupported_run_schema"
    assert result["summary_path"] == str(path.resolve())


def test_summary_preflight_rejects_invalid_v2_schema(tmp_path, capsys) -> None:
    path = _write_summary(tmp_path, {"schema_version": "paper_reader.summary.v2"})

    assert main([str(path)]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is False
    assert result["code"] == "invalid_summary_schema"
    assert result["summary_path"] == str(path.resolve())


def test_summary_preflight_rejects_file_over_resource_limit(tmp_path, capsys) -> None:
    path = tmp_path / "summary.json"
    path.write_bytes(b"x" * 33)

    assert main([str(path)], max_summary_bytes=32) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is False
    assert result["code"] == "resource_limit"
    assert result["summary_path"] == str(path.resolve())


def test_read_bounded_summary_rejects_non_regular_path_before_open() -> None:
    from paper_reader.summary_preflight_cli import _read_bounded_summary

    class NonRegularPath:
        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0)

        def open(self, mode):
            raise AssertionError("non-regular path must not be opened")

    with pytest.raises(OSError):
        _read_bounded_summary(NonRegularPath(), max_bytes=1024)
