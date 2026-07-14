from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

import paper_reader_batch.v2_artifacts as artifact_module
import paper_reader_batch.v2_report as report_module
import paper_reader_batch.v2_write as write_module
from paper_reader_batch.v2_artifacts import _read_inner
from paper_reader_batch.v2_contracts import ArtifactRef
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    MAX_OPAQUE_ARTIFACT_BYTES,
    sha256_bytes,
)


class _TestEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["test.envelope.v2"]
    artifact_id: str


def _opaque_ref(**values: object) -> dict[str, object]:
    return dict(values)


def _large_regular_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024)


def test_foreign_inner_ref_rejects_large_file_at_declared_size(tmp_path: Path) -> None:
    member = tmp_path / "member.bin"
    _large_regular_file(member)
    ref = _opaque_ref(
        role="member",
        path="member.bin",
        sha256="0" * 64,
        size_bytes=1,
        media_type="application/octet-stream",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        _read_inner(tmp_path, ref)

    assert exc_info.value.code == "artifact_invalid"
    assert "read limit of 1 bytes" in str(exc_info.value)


def test_foreign_inner_ref_accepts_exact_empty_file(tmp_path: Path) -> None:
    member = tmp_path / "empty.bin"
    member.write_bytes(b"")
    ref = _opaque_ref(
        role="member",
        path="empty.bin",
        sha256=sha256_bytes(b""),
        size_bytes=0,
        media_type="application/octet-stream",
    )

    path, raw, model = _read_inner(tmp_path, ref)

    assert path == member
    assert raw == b""
    assert model is None


def test_foreign_inner_zero_ref_rejects_nonempty_file_at_zero_limit(tmp_path: Path) -> None:
    member = tmp_path / "member.bin"
    member.write_bytes(b"x")
    ref = _opaque_ref(
        role="member",
        path="member.bin",
        sha256=sha256_bytes(b""),
        size_bytes=0,
        media_type="application/octet-stream",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        _read_inner(tmp_path, ref)

    assert exc_info.value.code == "artifact_invalid"
    assert "read limit of 0 bytes" in str(exc_info.value)


def test_foreign_inner_json_ref_is_also_capped_at_json_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int | None] = []

    def reject_read(path: Path, *, code: str, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        raise BatchRuntimeError(code, f"stopped before reading {path}")

    monkeypatch.setattr(artifact_module, "read_bytes", reject_read)
    ref = _opaque_ref(
        role="snapshot",
        path="snapshot.json",
        sha256="0" * 64,
        size_bytes=MAX_JSON_ARTIFACT_BYTES + 1,
        media_type="application/json",
    )

    with pytest.raises(BatchRuntimeError):
        _read_inner(tmp_path, ref)

    assert observed == [MAX_JSON_ARTIFACT_BYTES]


def test_foreign_inner_opaque_ref_is_capped_at_opaque_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int | None] = []

    def reject_read(path: Path, *, code: str, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        raise BatchRuntimeError(code, f"stopped before reading {path}")

    monkeypatch.setattr(artifact_module, "read_bytes", reject_read)
    ref = _opaque_ref(
        role="member",
        path="member.bin",
        sha256="0" * 64,
        size_bytes=MAX_OPAQUE_ARTIFACT_BYTES + 1,
        media_type="application/octet-stream",
    )

    with pytest.raises(BatchRuntimeError):
        _read_inner(tmp_path, ref)

    assert observed == [MAX_OPAQUE_ARTIFACT_BYTES]


def test_report_outer_ref_rejects_large_file_at_declared_size(tmp_path: Path) -> None:
    envelope = tmp_path / "envelope.json"
    _large_regular_file(envelope)
    ref = ArtifactRef(
        path=str(envelope),
        size_bytes=1,
        sha256="0" * 64,
        schema_version="test.envelope.v2",
        artifact_id="artifact-test",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        report_module._read_outer_model(
            ref,
            _TestEnvelope,
            schema_version="test.envelope.v2",
            basename="envelope.json",
            id_field="artifact_id",
        )

    assert exc_info.value.code == "report_source_invalid"
    assert "read limit of 1 bytes" in str(exc_info.value)


def test_report_outer_json_ref_is_also_capped_at_json_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int | None] = []

    def reject_read(path: Path, *, code: str, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        raise BatchRuntimeError(code, f"stopped before reading {path}")

    monkeypatch.setattr(report_module, "read_bytes", reject_read)
    ref = ArtifactRef(
        path=str(tmp_path / "envelope.json"),
        size_bytes=MAX_JSON_ARTIFACT_BYTES + 1,
        sha256="0" * 64,
        schema_version="test.envelope.v2",
        artifact_id="artifact-test",
    )

    with pytest.raises(BatchRuntimeError):
        report_module._read_outer_model(
            ref,
            _TestEnvelope,
            schema_version="test.envelope.v2",
            basename="envelope.json",
            id_field="artifact_id",
        )

    assert observed == [MAX_JSON_ARTIFACT_BYTES]


def test_write_foreign_ref_rejects_large_file_at_declared_size(tmp_path: Path) -> None:
    member = tmp_path / "member.bin"
    _large_regular_file(member)
    ref = _opaque_ref(
        role="member",
        path="member.bin",
        sha256="0" * 64,
        size_bytes=1,
        media_type="application/octet-stream",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        write_module._ref_bytes(tmp_path, ref, code="candidate_tampered")

    assert exc_info.value.code == "candidate_tampered"
    assert "read limit of 1 bytes" in str(exc_info.value)


def test_write_foreign_ref_accepts_exact_empty_file(tmp_path: Path) -> None:
    member = tmp_path / "empty.bin"
    member.write_bytes(b"")
    ref = _opaque_ref(
        role="member",
        path="empty.bin",
        sha256=sha256_bytes(b""),
        size_bytes=0,
        media_type="application/octet-stream",
    )

    path, raw = write_module._ref_bytes(tmp_path, ref, code="candidate_tampered")

    assert path == member
    assert raw == b""


def test_write_json_ref_is_also_capped_at_json_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int | None] = []

    def reject_read(path: Path, *, code: str, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        raise BatchRuntimeError(code, f"stopped before reading {path}")

    monkeypatch.setattr(write_module, "read_bytes", reject_read)
    ref = _opaque_ref(
        role="snapshot",
        path="snapshot.json",
        sha256="0" * 64,
        size_bytes=MAX_JSON_ARTIFACT_BYTES + 1,
        media_type="application/json",
    )

    with pytest.raises(BatchRuntimeError):
        write_module._ref_bytes(tmp_path, ref, code="candidate_tampered")

    assert observed == [MAX_JSON_ARTIFACT_BYTES]


def test_write_opaque_ref_is_capped_at_opaque_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int | None] = []

    def reject_read(path: Path, *, code: str, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        raise BatchRuntimeError(code, f"stopped before reading {path}")

    monkeypatch.setattr(write_module, "read_bytes", reject_read)
    ref = _opaque_ref(
        role="member",
        path="member.bin",
        sha256="0" * 64,
        size_bytes=MAX_OPAQUE_ARTIFACT_BYTES + 1,
        media_type="application/octet-stream",
    )

    with pytest.raises(BatchRuntimeError):
        write_module._ref_bytes(tmp_path, ref, code="candidate_tampered")

    assert observed == [MAX_OPAQUE_ARTIFACT_BYTES]
