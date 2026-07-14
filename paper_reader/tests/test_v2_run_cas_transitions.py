from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from paper_reader.storage import canonical_json_bytes

from test_v2_local_publication import _built_candidate, _sealed_run
from test_v2_review_package import (
    FIXTURE_PDF,
    _invoke,
    _prepared_run,
    _result_payload,
    _write_summary_and_review,
)
from test_v2_zotero_authorization import _authorize, _candidate
from test_v2_zotero_candidate import InMemoryZoteroProvider, _build, _sealed_zotero_run
from test_v2_zotero_reconciliation import _authorized as _reconcile_authorized
from test_v2_zotero_reconciliation import _reconcile
from test_v2_zotero_verification import _authorized as _verify_authorized
from test_v2_zotero_verification import _note_snapshot, _verify


def _install_external_run_replacement(module, monkeypatch: pytest.MonkeyPatch):
    original_cas = module.cas_update_run
    state: dict[str, bytes | None] = {"external": None}

    def replace_then_cas(loaded, value, **kwargs):
        path = loaded.manifest_path
        if state["external"] is None:
            payload = json.loads(path.read_bytes())
            payload["created_at"] = "2026-07-10T00:00:01Z"
            external = canonical_json_bytes(payload)
            path.write_bytes(external)
            state["external"] = external
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(module, "cas_update_run", replace_then_cas)
    return state


@pytest.mark.parametrize(
    "transition",
    [
        "evidence",
        "review",
        "zotero_candidate",
        "authorization",
        "verification",
        "reconciliation",
        "local_publish",
    ],
)
def test_run_transition_preserves_concurrent_external_manifest(
    transition: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if transition == "evidence":
        import paper_reader.evidence_bundle as module

        source = tmp_path / "paper.pdf"
        shutil.copyfile(FIXTURE_PDF, source)
        initialized = _invoke(["run", "init-local", str(source)])
        run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
        operation = lambda: _invoke(
            ["run", "prepare", str(run_dir), "--figure-limit", "0"]
        )
    elif transition == "review":
        import paper_reader.review_package as module

        run_dir, evidence_digest = _prepared_run(tmp_path)
        _write_summary_and_review(run_dir, evidence_digest)
        operation = lambda: _invoke(["review", "seal", str(run_dir)])
    elif transition == "zotero_candidate":
        import paper_reader.zotero_candidate as module

        run_dir = _sealed_zotero_run(tmp_path)
        provider = InMemoryZoteroProvider()
        operation = lambda: _build(run_dir, provider)
    elif transition == "authorization":
        import paper_reader.zotero_authorization as module

        candidate_path, provider = _candidate(tmp_path)
        run_dir = candidate_path.parents[2]
        operation = lambda: _authorize(candidate_path, provider)
    elif transition == "verification":
        import paper_reader.zotero_verification as module

        authorization_path, authorization = _verify_authorized(tmp_path)
        run_dir = authorization_path.parent.parent
        provider = InMemoryZoteroProvider(
            notes={"NOTE1": _note_snapshot(authorization)}
        )
        operation = lambda: _verify(authorization_path, provider)
    elif transition == "reconciliation":
        import paper_reader.zotero_reconciliation as module

        authorization_path, _authorization = _reconcile_authorized(tmp_path)
        run_dir = authorization_path.parent.parent
        provider = InMemoryZoteroProvider(children=[], notes={})
        operation = lambda: _reconcile(authorization_path, provider)
    else:
        import paper_reader.local_publish as module

        run_dir, candidate_path = _built_candidate(tmp_path)
        operation = lambda: module.publish_local_candidate(candidate_path)

    state = _install_external_run_replacement(module, monkeypatch)
    outcome = None
    error: Exception | None = None
    try:
        outcome = operation()
    except Exception as exc:  # expected for direct APIs after CAS is enforced
        error = exc

    if hasattr(outcome, "exit_code"):
        assert outcome.exit_code != 0
    else:
        assert error is not None
    assert state["external"] is not None
    assert (run_dir / "run.json").read_bytes() == state["external"]


@pytest.mark.parametrize(
    ("transition", "artifact_role"),
    [
        ("evidence", "evidence_manifest"),
        ("review", "review_package"),
        ("local_candidate", "candidate"),
        ("zotero_candidate", "candidate"),
    ],
)
@pytest.mark.parametrize("drift_kind", ["member_in_place", "tree_path_swap"])
def test_run_transition_does_not_bind_published_tree_drift_before_cas(
    transition: str,
    artifact_role: str,
    drift_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if transition == "evidence":
        import paper_reader.evidence_bundle as module

        source = tmp_path / "paper.pdf"
        shutil.copyfile(FIXTURE_PDF, source)
        initialized = _invoke(["run", "init-local", str(source)])
        run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
        operation = lambda: _invoke(
            ["run", "prepare", str(run_dir), "--figure-limit", "0"]
        )
    elif transition == "review":
        import paper_reader.review_package as module

        run_dir, evidence_digest = _prepared_run(tmp_path)
        _write_summary_and_review(run_dir, evidence_digest)
        operation = lambda: _invoke(["review", "seal", str(run_dir)])
    elif transition == "local_candidate":
        import paper_reader.candidate_builder as module

        run_dir = _sealed_run(tmp_path)
        operation = lambda: module.build_local_candidate(run_dir)
    else:
        import paper_reader.zotero_candidate as module

        run_dir = _sealed_zotero_run(tmp_path)
        provider = InMemoryZoteroProvider()
        operation = lambda: _build(run_dir, provider)

    run_before = (run_dir / "run.json").read_bytes()
    original_cas = module.cas_update_run
    drifted_path: Path | None = None
    detached_tree: Path | None = None

    def drift_published_member_then_cas(loaded, value, **kwargs):
        nonlocal detached_tree, drifted_path
        if drifted_path is None:
            added = [
                artifact
                for artifact in value.artifacts
                if artifact.role == artifact_role
                and artifact not in loaded.run.artifacts
            ]
            assert len(added) == 1
            drifted_path = run_dir / added[0].path
            if drift_kind == "member_in_place":
                drifted_path.write_bytes(b"tampered after immutable tree publication")
            else:
                published_tree = drifted_path.parent
                detached_tree = published_tree.with_name(
                    f".{published_tree.name}.detached-by-race"
                )
                published_tree.rename(detached_tree)
                published_tree.mkdir()
                drifted_path.write_bytes(b"tampered after immutable tree publication")
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(module, "cas_update_run", drift_published_member_then_cas)
    outcome = None
    error: Exception | None = None
    try:
        outcome = operation()
    except Exception as exc:
        error = exc

    assert drifted_path is not None
    if hasattr(outcome, "exit_code"):
        assert outcome.exit_code != 0
    else:
        assert error is not None
    assert (run_dir / "run.json").read_bytes() == run_before
    run = json.loads(run_before)
    assert not any(item["role"] == artifact_role for item in run["artifacts"])
    assert drifted_path.read_bytes() == b"tampered after immutable tree publication"
    if detached_tree is not None:
        assert detached_tree.is_dir()
