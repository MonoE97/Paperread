from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.public_cli import app


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "minimal.pdf"


def _bundle(
    pdf_path: Path,
    *,
    selected_key: str = "PARENT1",
    selected_title: str = "A <i>Useful</i> Paper &amp; Result",
    search_results: list[dict[str, object]] | None = None,
    raw_selected: bool = False,
) -> dict[str, object]:
    item = {
        "key": selected_key,
        "version": 17,
        "itemType": "journalArticle",
        "title": selected_title,
        "DOI": "10.1000/Example.DOI",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace", "creatorType": "author"}],
        "date": "2026",
        "url": "https://example.test/paper",
        "zoteroUrl": f"zotero://select/library/items/{selected_key}",
        "abstractNote": "Abstract",
        "attachments": [
            {
                "key": "ATTACH1",
                "version": 3,
                "itemType": "attachment",
                "title": "Full Text PDF",
                "filename": pdf_path.name,
                "contentType": "application/pdf",
                "path": str(pdf_path),
            }
        ],
        "notes": [],
        "tags": [{"tag": "materials"}],
    }
    selected: object = item
    if raw_selected:
        selected = {
            "jsonrpc": "2.0",
            "id": 42,
            "result": {"content": [{"type": "text", "text": json.dumps(item)}]},
        }
    return {
        "search_results": search_results
        if search_results is not None
        else [
            {
                "key": selected_key,
                "version": 17,
                "itemType": "journalArticle",
                "title": selected_title,
                "DOI": "10.1000/Example.DOI",
            }
        ],
        "selected_item": selected,
    }


def _initialize(bundle_path: Path, expected_key: str, skill_root: Path):
    module_name = "paper_reader.zotero_lifecycle"
    assert importlib.util.find_spec(module_name) is not None, "Zotero V2 lifecycle module is missing"
    module = importlib.import_module(module_name)
    return module.initialize_zotero_run(
        bundle_path,
        expected_item_key=expected_key,
        skill_root=skill_root,
    )


def _result_payload(result) -> dict[str, object]:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    return payload


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, int, str | None]]:
    snapshot: dict[str, tuple[str, int, int, str | None]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        stat = path.stat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        snapshot[relative] = (
            "dir" if path.is_dir() else "file",
            stat.st_mtime_ns,
            stat.st_size,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
    return snapshot


def test_init_zotero_preserves_raw_bytes_and_binds_normalized_source_and_pdf(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    bundle_path = tmp_path / "discovery.json"
    raw_bytes = json.dumps(_bundle(pdf_path), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    bundle_path.write_bytes(raw_bytes)
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()

    initialized = _initialize(bundle_path, "PARENT1", skill_root)

    assert initialized.run_dir.parent.parent == skill_root / "runs"
    assert initialized.run_dir.name == "a-useful-paper-result"
    raw_snapshot = initialized.run_dir / "source" / "discovery.raw.json"
    normalized_snapshot = initialized.run_dir / "source" / "source.json"
    assert raw_snapshot.read_bytes() == raw_bytes
    normalized = json.loads(normalized_snapshot.read_text(encoding="utf-8"))
    run = json.loads((initialized.run_dir / "run.json").read_text(encoding="utf-8"))
    source = run["source"]
    assert source["item_key"] == "PARENT1"
    assert source["title"] == "A Useful Paper & Result"
    assert source["doi"] == "10.1000/example.doi"
    assert source["parent_version"] == 17
    assert source["attachment_key"] == "ATTACH1"
    assert source["attachment"] == {
        "source_type": "local_pdf",
        "requested_path": str(pdf_path),
        "resolved_path": str(pdf_path.resolve()),
        "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        "size_bytes": pdf_path.stat().st_size,
        "device": pdf_path.stat().st_dev,
        "inode": pdf_path.stat().st_ino,
    }
    assert normalized["selected_item"]["key"] == "PARENT1"
    assert normalized["selected_item"]["title"] == "A Useful Paper & Result"
    assert normalized["selected_attachment"]["key"] == "ATTACH1"
    assert run["artifacts"] == [
        source["raw_discovery_bundle"],
        source["normalized_source"],
    ]


def test_init_zotero_accepts_raw_mcp_selected_item_and_allocates_versioned_runs(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(_bundle(pdf_path, raw_selected=True)), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()

    first = _initialize(bundle_path, "PARENT1", skill_root)
    second = _initialize(bundle_path, "PARENT1", skill_root)

    assert first.run_dir.name == "a-useful-paper-result"
    assert second.run_dir.name == "a-useful-paper-result_v2"
    assert first.run.run_id != second.run.run_id


@pytest.mark.parametrize(
    ("expected_key", "bundle_mutation", "expected_code"),
    [
        ("OTHER", lambda payload: None, "selected_item_key_mismatch"),
        (
            "PARENT1",
            lambda payload: payload["search_results"].append(
                {
                    "key": "PARENT2",
                    "version": 1,
                    "itemType": "journalArticle",
                    "title": "  a USEFUL paper &amp; <b>result</b>  ",
                    "DOI": "10.1000/other",
                }
            ),
            "duplicate_normalized_title",
        ),
        (
            "PARENT1",
            lambda payload: payload["search_results"].append(dict(payload["search_results"][0])),
            "duplicate_search_result_key",
        ),
        (
            "PARENT1",
            lambda payload: payload.update(
                {"search_results": [{"key": "OTHER", "title": "Other", "version": 1}]}
            ),
            "selected_item_not_in_inventory",
        ),
        (
            "PARENT1",
            lambda payload: payload.update({"unexpected": True}),
            "invalid_discovery_bundle",
        ),
    ],
)
def test_init_zotero_blocks_invalid_selection_before_run_allocation(
    expected_key: str,
    bundle_mutation,
    expected_code: str,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    payload = _bundle(pdf_path)
    bundle_mutation(payload)
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()

    module_name = "paper_reader.zotero_lifecycle"
    assert importlib.util.find_spec(module_name) is not None, "Zotero V2 lifecycle module is missing"
    module = importlib.import_module(module_name)
    with pytest.raises(module.ZoteroLifecycleError) as exc_info:
        module.initialize_zotero_run(
            bundle_path,
            expected_item_key=expected_key,
            skill_root=skill_root,
        )

    assert exc_info.value.code == expected_code
    assert not (skill_root / "runs").exists()


@pytest.mark.parametrize("field", ["DOI", "version"])
def test_init_zotero_requires_inventory_doi_and_version_to_match_selected_item(
    field: str,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    payload = _bundle(pdf_path)
    inventory_item = payload["search_results"][0]
    assert isinstance(inventory_item, dict)
    inventory_item[field] = "10.1000/drifted" if field == "DOI" else 18
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()

    with pytest.raises(Exception) as exc_info:
        _initialize(bundle_path, "PARENT1", skill_root)

    assert getattr(exc_info.value, "code", None) == "selected_item_inventory_mismatch"
    assert not (skill_root / "runs").exists()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("nan", "invalid_discovery_bundle"),
        ("infinity", "invalid_discovery_bundle"),
        ("expected_identifier", "invalid_expected_item_key"),
        ("selected_identifier", "invalid_discovery_bundle"),
        ("inventory_identifier", "invalid_discovery_bundle"),
        ("attachment_identifier", "invalid_discovery_bundle"),
    ],
)
def test_init_zotero_rejects_noncanonical_or_invalid_identifiers_before_any_root_mutation(
    case: str,
    expected_code: str,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    payload = _bundle(pdf_path)
    expected_key = "PARENT1"
    selected = payload["selected_item"]
    inventory = payload["search_results"]
    assert isinstance(selected, dict)
    assert isinstance(inventory, list)
    if case == "nan":
        selected["abstractNote"] = float("nan")
    elif case == "infinity":
        selected["abstractNote"] = float("inf")
    elif case == "expected_identifier":
        expected_key = "../PARENT1"
    elif case == "selected_identifier":
        selected["key"] = "BAD/KEY"
    elif case == "inventory_identifier":
        inventory.append({"key": "BAD/KEY", "title": "Other", "DOI": "", "version": 0})
    else:
        attachments = selected["attachments"]
        assert isinstance(attachments, list)
        attachments[0]["key"] = "BAD/KEY"
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()
    sentinel = skill_root / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = _tree_snapshot(skill_root)

    import paper_reader.zotero_lifecycle as module

    with pytest.raises(module.ZoteroLifecycleError) as exc_info:
        _initialize(bundle_path, expected_key, skill_root)

    assert exc_info.value.code == expected_code
    assert _tree_snapshot(skill_root) == before


def test_init_zotero_requires_readable_local_primary_pdf_before_allocation(tmp_path: Path) -> None:
    missing_pdf = tmp_path / "missing.pdf"
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(_bundle(missing_pdf)), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()

    module_name = "paper_reader.zotero_lifecycle"
    assert importlib.util.find_spec(module_name) is not None, "Zotero V2 lifecycle module is missing"
    module = importlib.import_module(module_name)
    with pytest.raises(module.ZoteroLifecycleError) as exc_info:
        module.initialize_zotero_run(
            bundle_path,
            expected_item_key="PARENT1",
            skill_root=skill_root,
        )

    assert exc_info.value.code == "zotero_pdf_unavailable"
    assert not (skill_root / "runs").exists()


def test_init_zotero_cli_uses_installed_skill_root_not_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_lifecycle as zotero_lifecycle

    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(_bundle(pdf_path)), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()
    unrelated_cwd = tmp_path / "elsewhere"
    unrelated_cwd.mkdir()
    monkeypatch.setattr(zotero_lifecycle, "DEFAULT_SKILL_ROOT", skill_root)
    monkeypatch.chdir(unrelated_cwd)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "init-zotero",
            "--raw-mcp-response",
            str(bundle_path),
            "--expected-item-key",
            "PARENT1",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "initialized"
    run_dir = Path(payload["data"]["run_dir"])
    assert run_dir.is_relative_to(skill_root / "runs")
    assert not (unrelated_cwd / "runs").exists()


def test_concurrent_init_zotero_calls_reserve_unique_versioned_runs(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(_bundle(pdf_path)), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()

    with ThreadPoolExecutor(max_workers=4) as executor:
        initialized = list(
            executor.map(
                lambda _index: _initialize(bundle_path, "PARENT1", skill_root),
                range(4),
            )
        )

    assert {item.run_dir.name for item in initialized} == {
        "a-useful-paper-result",
        "a-useful-paper-result_v2",
        "a-useful-paper-result_v3",
        "a-useful-paper-result_v4",
    }
    assert len({item.run.run_id for item in initialized}) == 4
    assert all((item.run_dir / "run.json").is_file() for item in initialized)


def test_init_zotero_run_size_gate_and_atomic_fault_leave_no_partial_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_lifecycle as lifecycle
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(_bundle(pdf_path)), encoding="utf-8")

    size_root = tmp_path / "size-skill"
    size_root.mkdir()
    monkeypatch.setattr(
        lifecycle,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
    )

    with pytest.raises(lifecycle.ZoteroLifecycleError) as size_error:
        _initialize(bundle_path, "PARENT1", size_root)

    assert size_error.value.code == "run_size_limit_exceeded"
    assert not (size_root / "runs").exists()

    monkeypatch.setattr(lifecycle, "V2_RESOURCE_POLICY", V2_RESOURCE_POLICY)
    fault_root = tmp_path / "fault-skill"
    fault_root.mkdir()

    def injected_failure(_staging: Path, _destination: Path) -> Path:
        raise OSError("injected Zotero run reservation failure")

    monkeypatch.setattr(lifecycle, "atomic_publish_tree", injected_failure)

    with pytest.raises(lifecycle.ZoteroLifecycleError) as fault_error:
        _initialize(bundle_path, "PARENT1", fault_root)

    assert fault_error.value.code == "initialization_failed"
    assert not any(path.is_file() for path in fault_root.rglob("*"))
    assert not any(path.name.endswith(".staging") for path in fault_root.rglob("*"))
