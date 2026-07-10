from __future__ import annotations

import importlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import (
    PaperReaderCommandResult,
    PaperReaderReconciliation,
    PaperReaderVerification,
)
from paper_reader.public_cli import app

from test_v2_zotero_authorization import NOW, _authorize, _candidate
from test_v2_zotero_candidate import InMemoryZoteroProvider
from test_v2_zotero_verification import _note_snapshot


def _module():
    module_name = "paper_reader.zotero_reconciliation"
    assert importlib.util.find_spec(module_name) is not None, "Zotero reconciliation module is missing"
    return importlib.import_module(module_name)


def _authorized(tmp_path: Path):
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(candidate_path, provider, ttl_seconds=1)
    return authorized.authorization_path, authorized.authorization


def _reconcile(authorization_path: Path, provider):
    return _module().reconcile_zotero_authorization(
        authorization_path,
        provider=provider,
    )


def test_reconcile_zero_is_terminal_not_found_and_requires_retry_confirmation(
    tmp_path: Path,
) -> None:
    authorization_path, _authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(children=[], notes={})

    reconciled = _reconcile(authorization_path, provider)

    record = PaperReaderReconciliation.model_validate_json(
        reconciled.reconciliation_path.read_bytes()
    )
    assert record.outcome == "not_found"
    assert record.match_count == 0
    assert record.matched_note_keys == ()
    assert record.retry_confirmation_required is True
    assert record.verification is None
    assert record.children_snapshot in record.artifacts
    assert sorted(path.name for path in reconciled.reconciliation_dir.iterdir()) == [
        "authorization.json",
        "children.json",
        "record.json",
    ]


def test_reconciliation_main_artifact_uses_authorization_topology(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)

    reconciled = _reconcile(
        authorization_path,
        InMemoryZoteroProvider(children=[], notes={}),
    )

    expected = (
        reconciled.run_dir
        / "reconciliations"
        / f"{authorization.authorization_id}.json"
    )
    assert reconciled.reconciliation_path == expected
    assert expected.is_file()
    assert reconciled.reconciliation_dir == expected.with_suffix("")


def test_reconcile_many_exact_matches_is_ambiguous_and_blocked(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    first = _note_snapshot(authorization, requested_key="NOTE1")
    second = _note_snapshot(authorization, requested_key="NOTE2")
    provider = InMemoryZoteroProvider(
        children=[first, second],
        notes={"NOTE1": first, "NOTE2": second},
    )

    reconciled = _reconcile(authorization_path, provider)

    record = reconciled.reconciliation
    assert record.outcome == "ambiguous"
    assert record.match_count == 2
    assert record.matched_note_keys == ("NOTE1", "NOTE2")
    assert record.retry_confirmation_required is False
    assert record.verification is None
    assert record.gate.status == "blocked"


def test_reconcile_one_exact_match_runs_full_verify_before_verified(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    note = _note_snapshot(authorization, requested_key="NOTE1")
    provider = InMemoryZoteroProvider(
        children=[note],
        notes={"NOTE1": note},
    )

    reconciled = _reconcile(authorization_path, provider)

    record = reconciled.reconciliation
    assert record.outcome == "verified"
    assert record.match_count == 1
    assert record.matched_note_keys == ("NOTE1",)
    assert record.retry_confirmation_required is False
    assert record.verification is not None
    verification_path = reconciled.run_dir / record.verification.path
    verification = PaperReaderVerification.model_validate_json(
        verification_path.read_bytes()
    )
    assert verification.verified is True
    assert all(check.passed for check in verification.checks)
    assert sorted(path.name for path in reconciled.reconciliation_dir.iterdir()) == [
        "authorization.json",
        "checks.json",
        "children.json",
        "note.json",
        "record.json",
        "verification.json",
    ]
    run = json.loads((reconciled.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "published"


def test_reconcile_expired_authorization_still_runs_full_readback_verification(
    tmp_path: Path,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(
        candidate_path,
        provider,
        now=NOW.replace(year=2000),
        ttl_seconds=1,
    )
    authorization_path = authorized.authorization_path
    note = _note_snapshot(authorized.authorization, requested_key="NOTE1")
    provider.children = [note]
    provider.notes = {"NOTE1": note}

    reconciled = _reconcile(authorization_path, provider)

    assert authorized.authorization.expires_at == "2000-07-10T12:00:01Z"
    assert reconciled.reconciliation.outcome == "verified"
    assert reconciled.reconciliation.verification is not None
    verification = PaperReaderVerification.model_validate_json(
        (
            reconciled.run_dir
            / reconciled.reconciliation.verification.path
        ).read_bytes()
    )
    assert verification.verified is True
    assert all(check.passed for check in verification.checks)


def test_reconcile_unique_locator_with_wrong_tags_is_blocked_not_verified(
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    located = _note_snapshot(authorization, requested_key="NOTE1")
    readback = _note_snapshot(
        authorization,
        requested_key="NOTE1",
        tags=[authorization.tags[0]],
    )
    provider = InMemoryZoteroProvider(
        children=[located],
        notes={"NOTE1": readback},
    )

    reconciled = _reconcile(authorization_path, provider)

    record = reconciled.reconciliation
    assert record.outcome == "blocked"
    assert record.match_count == 1
    assert record.verification is not None
    verification = PaperReaderVerification.model_validate_json(
        (reconciled.run_dir / record.verification.path).read_bytes()
    )
    assert verification.verified is False
    checks = {item.name: item for item in verification.checks}
    assert checks["tag_set"].passed is False


@pytest.mark.parametrize("mismatch", ["parent", "title", "hash"])
def test_reconcile_locator_requires_exact_parent_title_and_canonical_hash(
    mismatch: str,
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    note = _note_snapshot(authorization, requested_key="NOTE1")
    data = note["data"]
    assert isinstance(data, dict)
    if mismatch == "parent":
        data["parentItem"] = "OTHER"
    elif mismatch == "title":
        data["note"] = str(data["note"]).replace("<h1>", "<h1>Wrong ", 1)
    else:
        data["note"] = str(data["note"]) + "<p>changed</p>"
    provider = InMemoryZoteroProvider(children=[note], notes={"NOTE1": note})

    reconciled = _reconcile(authorization_path, provider)

    assert reconciled.reconciliation.outcome == "not_found"
    assert reconciled.reconciliation.match_count == 0


def test_reconciliation_is_fixed_terminal_per_authorization(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(children=[], notes={})

    first = _reconcile(authorization_path, provider)
    exact = _note_snapshot(authorization, requested_key="NOTE1")
    provider.children = [exact]
    provider.notes = {"NOTE1": exact}
    second = _reconcile(authorization_path, provider)

    assert second.reconciliation_dir == first.reconciliation_dir
    assert second.reconciliation == first.reconciliation
    assert second.replayed is True
    assert second.reconciliation.outcome == "not_found"


def test_reconcile_cli_emits_one_structured_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    authorization_path, _authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(children=[], notes={})
    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", lambda: provider)

    result = CliRunner().invoke(app, ["zotero", "reconcile", str(authorization_path)])

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "reconciliation_not_found"
    assert payload["data"]["outcome"] == "not_found"
    assert payload["data"]["retry_confirmation_required"] is True


def test_concurrent_reconcile_converges_on_one_fixed_terminal_tree(tmp_path: Path) -> None:
    authorization_path, _authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(children=[], notes={})

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _index: _reconcile(authorization_path, provider),
                range(2),
            )
        )

    assert outcomes[0].reconciliation_dir == outcomes[1].reconciliation_dir
    assert {item.replayed for item in outcomes} == {False, True}
    run_dir = authorization_path.parent.parent
    run = json.loads((run_dir / "run.json").read_text())
    assert [item["role"] for item in run["artifacts"]].count("zotero_reconciliation") == 1


def test_reconciliation_size_and_run_binding_faults_ignore_unbound_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    size_dir = tmp_path / "size"
    size_dir.mkdir()
    authorization_path, _authorization = _authorized(size_dir)
    provider = InMemoryZoteroProvider(children=[], notes={})
    run_dir = authorization_path.parent.parent
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        module,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
    )

    with pytest.raises(Exception) as size_error:
        _reconcile(authorization_path, provider)

    assert getattr(size_error.value, "code", None) == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "reconciliations").exists()

    monkeypatch.setattr(module, "V2_RESOURCE_POLICY", V2_RESOURCE_POLICY)
    fault_dir = tmp_path / "fault"
    fault_dir.mkdir()
    authorization_path, _authorization = _authorized(fault_dir)
    provider = InMemoryZoteroProvider(children=[], notes={})
    run_dir = authorization_path.parent.parent
    run_before = (run_dir / "run.json").read_bytes()
    original_write = module.atomic_write_json
    failed = False

    def fail_once(path: Path, value):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected reconciliation run binding failure")
        return original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", fail_once)

    with pytest.raises(Exception) as fault_error:
        _reconcile(authorization_path, provider)

    assert getattr(fault_error.value, "code", None) == "reconciliation_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_main = (
        run_dir
        / "reconciliations"
        / f"{_authorization.authorization_id}.json"
    )
    assert orphan_main.is_file()

    retry = _reconcile(authorization_path, provider)

    assert retry.reconciliation_path == orphan_main
    assert retry.replayed is True
    run = json.loads((run_dir / "run.json").read_text())
    bound = [item for item in run["artifacts"] if item["role"] == "zotero_reconciliation"]
    assert len(bound) == 1
    assert run_dir / bound[0]["path"] == retry.reconciliation_path
