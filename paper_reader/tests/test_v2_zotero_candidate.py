from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from paper_reader.contracts import PaperReaderCandidate
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.zotero_live import _parse_headings

from test_v2_review_package import _invoke, _result_payload, _write_summary_and_review
from test_v2_zotero_prepare import _zotero_run


def _parent(*, version: int = 17, doi: str = "10.1000/example.doi") -> dict[str, object]:
    return {
        "key": "PARENT1",
        "version": version,
        "library": {"type": "user", "id": 0, "name": "My Library"},
        "links": {"self": {"href": "http://127.0.0.1:23119/api/users/0/items/PARENT1"}},
        "meta": {"creatorSummary": "Lovelace", "parsedDate": "2026"},
        "data": {
            "key": "PARENT1",
            "version": version,
            "itemType": "journalArticle",
            "title": "A Useful Paper & Result",
            "DOI": doi,
            "creators": [],
            "tags": [],
            "collections": [],
            "relations": {},
            "dateAdded": "2026-07-10T00:00:00Z",
            "dateModified": "2026-07-10T00:00:00Z",
        },
    }


def _note(key: str, title: str, *, body: str = "existing body") -> dict[str, object]:
    return {
        "key": key,
        "version": 4,
        "library": {"type": "user", "id": 0, "name": "My Library"},
        "links": {"self": {"href": f"http://127.0.0.1:23119/api/users/0/items/{key}"}},
        "meta": {},
        "data": {
            "key": key,
            "version": 4,
            "itemType": "note",
            "parentItem": "PARENT1",
            "note": f"<h1>{title}</h1><p>{body}</p>",
            "tags": [{"tag": "codex-summary", "type": 1}],
            "collections": [],
            "relations": {},
            "dateAdded": "2026-07-10T00:00:00Z",
            "dateModified": "2026-07-10T00:00:00Z",
        },
    }


class InMemoryZoteroProvider:
    def __init__(
        self,
        *,
        parent: dict[str, object] | None = None,
        children: list[dict[str, object]] | None = None,
        notes: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.parent = parent or _parent()
        self.children = children or []
        self.notes = notes or {
            str(item["key"]): item for item in self.children
        }

    def get_parent(self, _item_key: str) -> dict[str, object]:
        return copy.deepcopy(self.parent)

    def get_children(self, _parent_key: str) -> list[dict[str, object]]:
        return copy.deepcopy(self.children)

    def get_note(self, note_key: str) -> dict[str, object]:
        return copy.deepcopy(self.notes[note_key])


def _sealed_zotero_run(tmp_path: Path) -> Path:
    run_dir, _pdf_path = _zotero_run(tmp_path)
    prepared = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])
    assert prepared.exit_code == 0, prepared.stderr
    evidence_digest = _result_payload(prepared)["data"]["evidence_digest"]
    _write_summary_and_review(run_dir, evidence_digest)
    sealed = _invoke(["review", "seal", str(run_dir)])
    assert sealed.exit_code == 0, sealed.stderr
    return run_dir


def _build(run_dir: Path, provider: InMemoryZoteroProvider):
    import paper_reader.candidate_builder as candidate_builder

    assert hasattr(candidate_builder, "build_candidate"), "source-dispatched candidate builder is missing"
    return candidate_builder.build_candidate(run_dir, provider=provider)


def test_zotero_candidate_uses_fresh_children_for_exact_title_and_canonical_html(
    tmp_path: Path,
) -> None:
    run_dir = _sealed_zotero_run(tmp_path)
    run_date = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["created_at"][:10]
    base = f"[Codex Summary] A Useful Paper & Result - {run_date}"
    provider = InMemoryZoteroProvider(
        children=[_note("NOTE1", base), _note("NOTE2", f"{base} (v2)")]
    )

    built = _build(run_dir, provider)

    candidate_path = built.candidate_dir / "candidate.json"
    candidate = PaperReaderCandidate.model_validate_json(candidate_path.read_bytes())
    assert built.candidate_digest == hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    assert candidate.target.target_type == "zotero"
    assert candidate.target.parent_key == "PARENT1"
    assert candidate.target.parent_fingerprint == candidate.source.parent_fingerprint
    assert candidate.target.note_title == f"{base} (v3)"
    assert candidate.note_title == candidate.target.note_title
    assert candidate.tags == ("codex-summary", "paper-summary")
    assert candidate.live_preflight is not None
    assert candidate.live_preflight.title_available is True
    assert candidate.live_preflight.matching_note_keys == ()
    assert candidate.live_preflight.parent_snapshot in candidate.artifacts
    assert candidate.live_preflight.children_snapshot in candidate.artifacts
    note_md = (built.candidate_dir / "note.md").read_text(encoding="utf-8")
    note_html = (built.candidate_dir / "note.html").read_text(encoding="utf-8")
    assert note_md.splitlines()[0] == f"# {base} (v3)"
    html_title, _headings = _parse_headings(note_html)
    assert html_title == f"{base} (v3)"
    canonical_html = canonicalize_note_html_for_hash(note_html)
    assert candidate.content_sha256 == note_html_sha256(note_html)
    assert candidate.content_length == len(canonical_html)
    assert sorted(path.name for path in built.candidate_dir.iterdir()) == [
        "candidate.json",
        "children.json",
        "discovery.raw.json",
        "evidence.json",
        "note.html",
        "note.md",
        "parent.json",
        "review-package.json",
        "review.json",
        "run.json",
        "source.json",
        "summary.json",
        "validation.json",
    ]
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "candidate_built"
    assert run["target"] == candidate.target.model_dump(mode="json")
    assert run["live_preflight"] == candidate.live_preflight.model_dump(mode="json")


@pytest.mark.parametrize(
    "parent",
    [_parent(version=18), _parent(doi="10.1000/changed")],
)
def test_zotero_candidate_blocks_fresh_parent_fingerprint_mismatch_before_publication(
    parent: dict[str, object],
    tmp_path: Path,
) -> None:
    run_dir = _sealed_zotero_run(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()

    with pytest.raises(Exception) as exc_info:
        _build(run_dir, InMemoryZoteroProvider(parent=parent))

    assert getattr(exc_info.value, "code", None) == "parent_fingerprint_mismatch"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "candidates").exists()


@pytest.mark.parametrize("role", ["raw_discovery_bundle", "normalized_source"])
def test_zotero_candidate_rehashes_every_source_snapshot(
    role: str,
    tmp_path: Path,
) -> None:
    run_dir = _sealed_zotero_run(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    source_ref = next(item for item in run["artifacts"] if item["role"] == role)
    (run_dir / source_ref["path"]).write_bytes(b"tampered")

    with pytest.raises(Exception) as exc_info:
        _build(run_dir, InMemoryZoteroProvider())

    assert getattr(exc_info.value, "code", None) == "sealed_artifact_tampered"
    assert not (run_dir / "candidates").exists()


def test_concurrent_zotero_candidate_builds_preserve_both_immutable_bindings(
    tmp_path: Path,
) -> None:
    run_dir = _sealed_zotero_run(tmp_path)
    provider = InMemoryZoteroProvider()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(lambda _index: _build(run_dir, provider), range(2))
        )

    assert len({item.candidate_dir for item in outcomes}) == 2
    assert len({item.candidate.candidate_id for item in outcomes}) == 2
    assert len({item.candidate.note_title for item in outcomes}) == 1
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    bound = [item for item in run["artifacts"] if item["role"] == "candidate"]
    assert len(bound) == 2
    assert {run_dir / item["path"] for item in bound} == {
        outcome.candidate_dir / "candidate.json" for outcome in outcomes
    }


def test_zotero_candidate_size_and_publication_faults_leave_no_bound_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_candidate as module
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    size_dir = tmp_path / "size"
    size_dir.mkdir()
    run_dir = _sealed_zotero_run(size_dir)
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        module,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
    )

    with pytest.raises(Exception) as size_error:
        _build(run_dir, InMemoryZoteroProvider())

    assert getattr(size_error.value, "code", None) == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "candidates").exists()

    monkeypatch.setattr(module, "V2_RESOURCE_POLICY", V2_RESOURCE_POLICY)
    fault_dir = tmp_path / "publication-fault"
    fault_dir.mkdir()
    run_dir = _sealed_zotero_run(fault_dir)
    run_before = (run_dir / "run.json").read_bytes()

    def injected_failure(_staging: Path, _destination: Path) -> Path:
        raise OSError("injected Zotero candidate publication failure")

    monkeypatch.setattr(module, "atomic_publish_tree", injected_failure)

    with pytest.raises(Exception) as fault_error:
        _build(run_dir, InMemoryZoteroProvider())

    assert getattr(fault_error.value, "code", None) == "candidate_publication_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "candidates").exists()
    assert not any(path.name.endswith(".staging") for path in run_dir.iterdir())


def test_zotero_candidate_run_binding_fault_leaves_orphan_and_retry_binds_new_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_candidate as module

    run_dir = _sealed_zotero_run(tmp_path)
    provider = InMemoryZoteroProvider()
    run_before = (run_dir / "run.json").read_bytes()
    original_write = module.atomic_write_json
    failed = False

    def fail_once(path: Path, value):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected Zotero candidate run binding failure")
        return original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", fail_once)

    with pytest.raises(Exception) as fault_error:
        _build(run_dir, provider)

    assert getattr(fault_error.value, "code", None) == "candidate_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_dirs = tuple((run_dir / "candidates").iterdir())
    assert len(orphan_dirs) == 1

    retry = _build(run_dir, provider)

    assert retry.candidate_dir not in orphan_dirs
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    bound = [item for item in run["artifacts"] if item["role"] == "candidate"]
    assert len(bound) == 1
    assert run_dir / bound[0]["path"] == retry.candidate_dir / "candidate.json"
