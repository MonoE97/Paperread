from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hmac
from html.parser import HTMLParser
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Callable, Iterator
from uuid import uuid4

from pydantic import ValidationError

from paper_reader_batch.v2_artifacts import (
    _OpaqueRecord,
    _parent_fingerprint as _consumed_parent_fingerprint,
    _plain,
    validate_worker_result_artifacts,
)
from paper_reader_batch.v2_contracts import (
    WORKER_RESULT_SCHEMA_VERSION,
    WRITE_RESULT_SCHEMA_VERSION,
    RECONCILIATION_SCHEMA_VERSION,
    ArtifactRef,
    ReconciliationResult,
    WorkerResult,
    WriteResult,
    WriteClaimedData,
    WriteLeaseMutationData,
    WriteReconciledData,
    WriteRetriedData,
    WriteStartedData,
    WriteUncertainData,
    WriteWrittenData,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import (
    ProposedTransition,
    ResultPublication,
    RunView,
    append_transaction,
    load_request_preflight,
    load_run_view,
    load_run_view_for_mutation,
)
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    MAX_OPAQUE_ARTIFACT_BYTES,
    canonical_json_bytes,
    canonical_sha256,
    held_exact_sibling_files,
    list_directory,
    locked_file,
    normalized_absolute_path,
    read_bytes as _read_bytes,
    read_json_bytes as _read_json_bytes,
    sha256_bytes,
    validate_locked_path,
)
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome, validate_request_id
from paper_reader_batch.v2_worker import derive_lease_token


DEFAULT_WRITE_LEASE_SECONDS = 120
MAX_WRITE_LEASE_SECONDS = 300
MIN_AUTHORIZATION_REMAINING_SECONDS = 30
_PORTABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$")


class _ExternalClosureCollector:
    def __init__(self, *, code: str) -> None:
        self.code = code
        self.files: dict[Path, bytes] = {}
        self.closed_directories: dict[Path, frozenset[str]] = {}
        self.frozen = False

    def add(self, path: Path, raw: bytes) -> None:
        if self.frozen:
            raise BatchRuntimeError("journal_corrupt", "external closure collector is frozen")
        normalized = normalized_absolute_path(path)
        existing = self.files.get(normalized)
        if existing is not None and existing != raw:
            raise BatchRuntimeError(self.code, f"external artifact changed while read: {normalized}")
        self.files[normalized] = raw

    def close_directory(self, directory: Path, names: set[str]) -> None:
        if self.frozen:
            raise BatchRuntimeError("journal_corrupt", "external closure collector is frozen")
        normalized = normalized_absolute_path(directory)
        expected = frozenset(names)
        existing = self.closed_directories.get(normalized)
        if existing is not None and existing != expected:
            raise BatchRuntimeError(self.code, f"external directory closure changed: {normalized}")
        self.closed_directories[normalized] = expected

    def freeze(self) -> None:
        if not self.files:
            raise BatchRuntimeError("journal_corrupt", "external closure collector captured no files")
        for directory, names in self.closed_directories.items():
            captured = {path.name for path in self.files if path.parent == directory}
            if captured != set(names):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    f"external directory closure was not fully captured: {directory}",
                )
        self.frozen = True

    @contextmanager
    def hold(self) -> Iterator[Callable[[], None]]:
        if not self.frozen:
            raise BatchRuntimeError("journal_corrupt", "external closure collector is not frozen")
        by_parent: dict[Path, dict[str, bytes]] = {}
        for path, raw in self.files.items():
            by_parent.setdefault(path.parent, {})[path.name] = raw
        with ExitStack() as stack:
            guards = [
                stack.enter_context(
                    held_exact_sibling_files(
                        parent,
                        expected,
                        require_private=False,
                        exact_membership=parent in self.closed_directories,
                    )
                )
                for parent, expected in sorted(by_parent.items(), key=lambda item: str(item[0]))
            ]

            def verify() -> None:
                for guard in guards:
                    guard()

            verify()
            yield verify
            verify()


_ACTIVE_EXTERNAL_COLLECTOR: ContextVar[_ExternalClosureCollector | None] = ContextVar(
    "paper_reader_batch_write_external_collector",
    default=None,
)


def read_bytes(path: Path, *args, **kwargs) -> bytes:
    raw = _read_bytes(path, *args, **kwargs)
    collector = _ACTIVE_EXTERNAL_COLLECTOR.get()
    if collector is not None:
        collector.add(path, raw)
    return raw


def read_json_bytes(path: Path, *args, **kwargs) -> tuple[bytes, Any]:
    raw, payload = _read_json_bytes(path, *args, **kwargs)
    collector = _ACTIVE_EXTERNAL_COLLECTOR.get()
    if collector is not None:
        collector.add(path, raw)
    return raw, payload


@contextmanager
def _collect_external_closure(*, code: str) -> Iterator[_ExternalClosureCollector]:
    collector = _ExternalClosureCollector(code=code)
    token = _ACTIVE_EXTERNAL_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _ACTIVE_EXTERNAL_COLLECTOR.reset(token)


def _active_collector_close(directory: Path, names: set[str]) -> None:
    collector = _ACTIVE_EXTERNAL_COLLECTOR.get()
    if collector is not None:
        collector.close_directory(directory, names)


@contextmanager
def _locked_single_run_closure(
    view: RunView,
    *,
    item_id: str,
    authorization_sha256: str,
    collector: _ExternalClosureCollector,
    validate: Callable[[], None],
) -> Iterator[Callable[[], None]]:
    authorization_path = _authorization_path_for_digest(
        view,
        item_id=item_id,
        authorization_sha256=authorization_sha256,
    )
    single_run_root = authorization_path.parent.parent
    with locked_file(
        single_run_root / ".run.lock",
        create=False,
        guard_parent_replacement=False,
    ) as lock_descriptor, collector.hold() as closure_guard:
        def verify() -> None:
            validate_locked_path(single_run_root / ".run.lock", lock_descriptor)
            closure_guard()

        verify()
        validate()
        verify()
        yield verify


def _raise_input_error(existing_event, code: str, message: str) -> None:
    if existing_event is not None:
        raise BatchRuntimeError(
            "idempotency_conflict",
            "request id is already bound to different command input",
        )
    raise BatchRuntimeError(code, message)


def _ref_read_limit(size_bytes: object, *, code: str, json_artifact: bool) -> int:
    if type(size_bytes) is not int or size_bytes < 0:
        raise BatchRuntimeError(code, "artifact reference must declare non-negative integer size_bytes")
    limit = MAX_JSON_ARTIFACT_BYTES if json_artifact else MAX_OPAQUE_ARTIFACT_BYTES
    return min(size_bytes, limit)


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.level: str | None = None
        self.parts: list[str] = []
        self.title = ""
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        if lowered in {"h1", "h2"}:
            self.level = lowered
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.level is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.level != tag.lower():
            return
        text = " ".join("".join(self.parts).split())
        if self.level == "h1" and not self.title:
            self.title = text
        elif self.level == "h2" and text:
            self.headings.append(text)
        self.level = None
        self.parts = []


def _verification_actuals(
    note_snapshot: dict[str, Any],
    *,
    note_key: str,
    authorization: _OpaqueRecord,
    invalid_code: str = "reconciliation_tampered",
) -> tuple[dict[str, bool], str, int]:
    data = note_snapshot.get("data")
    if not isinstance(data, dict):
        data = {}
    note_html = str(data.get("note") or "")
    canonical_html = note_html.rstrip("\r\n")
    parser = _HeadingParser()
    parser.feed(note_html)
    tags = _snapshot_tag_set(data, code=invalid_code)
    headings = set(parser.headings)
    actuals = {
        "note_key": (
            str(note_snapshot.get("key") or "").strip() == note_key
            and str(data.get("key") or "").strip() == note_key
        ),
        "item_type": data.get("itemType") == "note",
        "parent_key": str(data.get("parentItem") or "").strip() == authorization.target.parent_key,
        "note_title": parser.title == authorization.note_title,
        "tag_set": tags == set(authorization.tags),
        "required_headings": all(heading in headings for heading in authorization.required_headings),
        "forbidden_headings": all(heading not in headings for heading in authorization.forbidden_headings),
        "minimum_content_length": len(canonical_html) >= authorization.minimum_content_length,
        "content_length": len(canonical_html) == authorization.content_length,
        "content_sha256": sha256_bytes(canonical_html.encode("utf-8")) == authorization.content_sha256,
    }
    return actuals, sha256_bytes(canonical_html.encode("utf-8")), len(canonical_html)


def _snapshot_tag_set(data: dict[str, Any], *, code: str) -> set[str]:
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list):
        raise BatchRuntimeError(code, "Zotero note snapshot tags must be an array")
    tags: set[str] = set()
    for entry in raw_tags:
        if not isinstance(entry, dict) or not isinstance(entry.get("tag"), str):
            raise BatchRuntimeError(code, "Zotero note snapshot tags must contain tag objects")
        tag = entry["tag"].strip()
        if not tag:
            raise BatchRuntimeError(code, "Zotero note snapshot tags must be non-empty strings")
        tags.add(tag)
    return tags


def _reconciliation_exact_match_key(
    child: object,
    authorization: _OpaqueRecord,
) -> str | None:
    if not isinstance(child, dict):
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation child is not an object")
    data = child.get("data")
    if not isinstance(data, dict) or data.get("itemType") != "note":
        return None
    parent_key = str(data.get("parentItem") or "").strip()
    note_html = str(data.get("note") or "")
    parser = _HeadingParser()
    parser.feed(note_html)
    if (
        parent_key != authorization.target.parent_key
        or parser.title != authorization.note_title
        or sha256_bytes(note_html.rstrip("\r\n").encode("utf-8"))
        != authorization.content_sha256
    ):
        return None
    top_note_key = str(child.get("key") or "").strip()
    data_note_key = str(data.get("key") or "").strip()
    if top_note_key and data_note_key and top_note_key != data_note_key:
        raise BatchRuntimeError(
            "reconciliation_tampered",
            "exact reconciliation match has inconsistent note keys",
        )
    note_key = top_note_key or data_note_key
    if (
        not note_key
        or _PORTABLE_IDENTIFIER.fullmatch(note_key) is None
    ):
        raise BatchRuntimeError(
            "reconciliation_tampered",
            "exact reconciliation match has an invalid or inconsistent note key",
        )
    return note_key


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, IndexError) as exc:
        raise BatchRuntimeError("invalid_timestamp", f"invalid RFC3339 UTC timestamp: {value}") from exc
    if not value.endswith("Z") or parsed.utcoffset() != timedelta(0):
        raise BatchRuntimeError("invalid_timestamp", f"timestamp must use UTC Z form: {value}")
    return parsed


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_committed_worker_result(view: RunView, item_id: str) -> WorkerResult:
    state_item = next((item for item in view.state.items if item.item_id == item_id), None)
    if state_item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
    if state_item.worker_status != "succeeded" or state_item.worker_result_sha256 is None:
        raise BatchRuntimeError("candidate_unavailable", f"worker candidate is not committed: {item_id}")
    result_path = view.run_dir / "results" / "worker" / f"{state_item.worker_result_sha256}.json"
    raw, payload = read_json_bytes(result_path, code="journal_corrupt")
    if not isinstance(payload, dict) or payload.get("schema_version") != WORKER_RESULT_SCHEMA_VERSION:
        raise BatchRuntimeError("unsupported_run_schema", "committed worker result has unsupported schema")
    try:
        result = WorkerResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("journal_corrupt", "committed worker result failed strict validation") from exc
    if raw != canonical_json_bytes(result) or sha256_bytes(raw) != state_item.worker_result_sha256:
        raise BatchRuntimeError("journal_corrupt", "committed worker result digest or canonical bytes changed")
    if result.item_id != item_id or result.candidate is None or result.candidate.sha256 != state_item.candidate_sha256:
        raise BatchRuntimeError("journal_corrupt", "committed worker candidate does not match batch state")
    validate_worker_result_artifacts(view.manifest, result, allow_mutable_run=True)
    return result


def _active_write_lease(
    view: RunView,
    *,
    item_id: str,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    now: str,
    allowed_statuses: set[str],
):
    item = next((candidate for candidate in view.state.items if candidate.item_id == item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
    lease = item.write_lease
    if item.write_status not in allowed_statuses or lease is None:
        raise BatchRuntimeError("write_lease_inactive", f"write lease is not active for {item_id}")
    expected_token = derive_lease_token(
        view.lease_secret,
        lane="write",
        claim_id=lease.claim_id,
        attempt_id=lease.write_attempt_id,
    )
    if (
        lease.writer_id != writer_id
        or lease.claim_id != claim_id
        or lease.write_attempt_id != write_attempt_id
        or not hmac.compare_digest(expected_token, lease_token)
        or lease.lease_token_sha256 != sha256_bytes(lease_token.encode())
        or lease.candidate_sha256 != item.candidate_sha256
    ):
        raise BatchRuntimeError(
            "write_lease_identity_mismatch",
            "writer, claim, write attempt, candidate, or lease token does not match",
        )
    if _parse_utc(now) >= _parse_utc(lease.expires_at):
        raise BatchRuntimeError("write_lease_expired", f"write lease has expired: {item_id}")
    return item, lease


def _candidate_material(view: RunView, item_id: str) -> tuple[WorkerResult, bytes, dict[str, Any], bytes, bytes]:
    result = _load_committed_worker_result(view, item_id)
    assert result.candidate is not None
    candidate_path = normalized_absolute_path(Path(result.candidate.path))
    candidate_raw, candidate = read_json_bytes(
        candidate_path,
        code="candidate_unreadable",
        max_bytes=_ref_read_limit(
            result.candidate.size_bytes,
            code="candidate_tampered",
            json_artifact=True,
        ),
    )
    if (
        not isinstance(candidate, dict)
        or candidate.get("schema_version") != "paper_reader.candidate.v2"
        or candidate_raw != canonical_json_bytes(candidate)
        or sha256_bytes(candidate_raw) != result.candidate.sha256
        or len(candidate_raw) != result.candidate.size_bytes
        or candidate.get("candidate_id") != result.candidate.artifact_id
    ):
        raise BatchRuntimeError("candidate_tampered", "candidate main artifact identity or canonical bytes changed")
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list):
        raise BatchRuntimeError("candidate_tampered", "candidate artifacts are not a list")
    expected_paths = {
        "note_markdown": (candidate_path.parent / "note.md", "text/markdown"),
        "note_html": (candidate_path.parent / "note.html", "text/html"),
    }
    run_root = candidate_path.parent.parent.parent
    members: dict[str, bytes] = {}
    for role, (path, media_type) in expected_paths.items():
        refs = [ref for ref in artifacts if isinstance(ref, dict) and ref.get("role") == role]
        if len(refs) != 1:
            raise BatchRuntimeError("candidate_tampered", f"candidate must bind exactly one {role}")
        ref = refs[0]
        try:
            expected_path = run_root / str(ref["path"])
        except KeyError as exc:
            raise BatchRuntimeError("candidate_tampered", f"candidate {role} ref is incomplete") from exc
        if normalized_absolute_path(expected_path) != path or ref.get("media_type") != media_type:
            raise BatchRuntimeError("candidate_tampered", f"candidate {role} path/media type changed")
        raw = read_bytes(
            path,
            code="candidate_tampered",
            max_bytes=_ref_read_limit(
                ref.get("size_bytes"),
                code="candidate_tampered",
                json_artifact=False,
            ),
        )
        if (
            ref.get("sha256") != sha256_bytes(raw)
            or ref.get("size_bytes") != len(raw)
        ):
            raise BatchRuntimeError("candidate_tampered", f"candidate {role} bytes changed")
        members[role] = raw
    note_md = members["note_markdown"]
    note_html = members["note_html"]
    return result, candidate_raw, candidate, note_md, note_html


def _authorization_present_for_attempt(
    worker_result: WorkerResult,
    *,
    claim_id: str,
    write_attempt_id: str,
) -> bool:
    if worker_result.paper_reader_run is None:
        raise BatchRuntimeError("journal_corrupt", "worker result is missing paper_reader run")
    _raw, run = read_json_bytes(
        Path(worker_result.paper_reader_run.path),
        code="candidate_tampered",
        # run.json is the intentionally mutable single-paper root. The
        # committed ref binds its stable path/run id, not its later byte size.
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    if not isinstance(run, dict):
        raise BatchRuntimeError("candidate_tampered", "paper_reader run is not an object")
    run_root = normalized_absolute_path(Path(worker_result.paper_reader_run.path)).parent
    artifacts = run.get("artifacts")
    if not isinstance(artifacts, list):
        raise BatchRuntimeError("candidate_tampered", "paper_reader run artifacts are not a list")
    for ref in artifacts:
        if not isinstance(ref, dict) or ref.get("role") != "write_authorization":
            continue
        _validate_ref_payload(ref, code="authorization_tampered")
        path = _safe_inner_path(run_root, str(ref["path"]), code="authorization_tampered")
        relative = path.relative_to(run_root)
        if (
            relative.parent != Path("authorizations")
            or path.suffix != ".json"
            or _PORTABLE_IDENTIFIER.fullmatch(path.stem) is None
            or ref.get("media_type") != "application/json"
        ):
            raise BatchRuntimeError(
                "authorization_tampered",
                "run-bound authorization path/topology is invalid",
            )
        raw, authorization = read_json_bytes(
            path,
            code="authorization_tampered",
            max_bytes=_ref_read_limit(
                ref.get("size_bytes"),
                code="authorization_tampered",
                json_artifact=True,
            ),
        )
        record = _opaque_single_artifact(
            raw,
            authorization,
            schema_version="paper_reader.write-authorization.v2",
            code="authorization_tampered",
            label="authorization",
        )
        if (
            record.authorization_id != path.stem
            or ref.get("sha256") != sha256_bytes(raw)
            or ref.get("size_bytes") != len(raw)
        ):
            raise BatchRuntimeError("authorization_tampered", "run-bound authorization changed")
        if (
            record.external_claim_id == claim_id
            and record.write_attempt_id == write_attempt_id
        ):
            return True
    return False


def _safe_inner_path(root: Path, value: str, *, code: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BatchRuntimeError(code, f"artifact ref path is unsafe: {value}")
    path = normalized_absolute_path(root.joinpath(*relative.parts))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BatchRuntimeError(code, f"artifact ref escapes run root: {value}") from exc
    return path


def _ref_bytes(
    run_root: Path,
    ref: object,
    *,
    code: str,
) -> tuple[Path, bytes]:
    payload = _plain(ref)
    _validate_ref_payload(payload, code=code)
    assert isinstance(payload, dict)
    path_value = str(payload["path"])
    path = _safe_inner_path(run_root, path_value, code=code)
    raw = read_bytes(
        path,
        code=code,
        max_bytes=_ref_read_limit(
            payload["size_bytes"],
            code=code,
            json_artifact=(
                payload.get("media_type") == "application/json" or path_value.endswith(".json")
            ),
        ),
    )
    if payload["sha256"] != sha256_bytes(raw) or payload["size_bytes"] != len(raw):
        raise BatchRuntimeError(code, f"artifact ref digest/size changed: {path_value}")
    return path, raw


def _require_sha256(value: str, *, label: str, code: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise BatchRuntimeError(code, f"{label} is not a canonical SHA-256")


def _parse_json_bytes(raw: bytes, *, code: str, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BatchRuntimeError(code, f"{label} is invalid JSON") from exc


def _opaque_single_artifact(
    raw: bytes,
    payload: object,
    *,
    schema_version: str,
    code: str,
    label: str,
) -> _OpaqueRecord:
    if not isinstance(payload, dict) or payload.get("schema_version") != schema_version:
        raise BatchRuntimeError("unsupported_run_schema", f"{label} schema is not {schema_version}")
    if raw != canonical_json_bytes(payload):
        raise BatchRuntimeError(code, f"{label} is not canonical JSON")
    _validate_consumed_single_fields(payload, schema_version=schema_version, code=code)
    return _OpaqueRecord(payload)


def _required_string(payload: dict[str, Any], field: str, *, code: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BatchRuntimeError(code, f"{field} must be a non-empty string")
    return value


def _required_int(
    payload: dict[str, Any],
    field: str,
    *,
    code: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = payload.get(field)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise BatchRuntimeError(code, f"{field} must be an integer in its allowed range")
    return value


def _required_bool(payload: dict[str, Any], field: str, *, code: str) -> bool:
    value = payload.get(field)
    if type(value) is not bool:
        raise BatchRuntimeError(code, f"{field} must be a boolean")
    return value


def _required_object(payload: dict[str, Any], field: str, *, code: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise BatchRuntimeError(code, f"{field} must be a JSON object")
    return value


def _required_array(payload: dict[str, Any], field: str, *, code: str) -> list[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise BatchRuntimeError(code, f"{field} must be a JSON array")
    return value


def _string_array(payload: dict[str, Any], field: str, *, code: str) -> list[str]:
    values = _required_array(payload, field, code=code)
    if any(not isinstance(value, str) or "\x00" in value for value in values):
        raise BatchRuntimeError(code, f"{field} must contain only strings")
    return values


def _nonempty_string_array(payload: dict[str, Any], field: str, *, code: str) -> list[str]:
    values = _string_array(payload, field, code=code)
    if any(not value for value in values):
        raise BatchRuntimeError(code, f"{field} must contain only non-empty strings")
    return values


def _validate_ref_payload(value: object, *, code: str) -> None:
    if not isinstance(value, dict):
        raise BatchRuntimeError(code, "artifact reference must be a JSON object")
    _required_string(value, "role", code=code)
    _required_string(value, "path", code=code)
    digest = _required_string(value, "sha256", code=code)
    _require_sha256(digest, label="artifact reference hash", code=code)
    _required_int(value, "size_bytes", code=code)
    media_type = value.get("media_type")
    if media_type is not None and (not isinstance(media_type, str) or not media_type):
        raise BatchRuntimeError(code, "artifact reference media_type must be a string or null")


def _validate_target_payload(value: object, *, code: str) -> None:
    if not isinstance(value, dict) or value.get("target_type") != "zotero":
        raise BatchRuntimeError(code, "target must be a Zotero target object")
    _required_string(value, "parent_key", code=code)
    _require_sha256(
        _required_string(value, "parent_fingerprint", code=code),
        label="parent fingerprint",
        code=code,
    )
    _required_string(value, "note_title", code=code)


def _validate_gate_payload(value: object, *, code: str) -> None:
    if not isinstance(value, dict):
        raise BatchRuntimeError(code, "gate must be a JSON object")
    if value.get("status") not in {"not_evaluated", "blocked", "passed", "write_ready"}:
        raise BatchRuntimeError(code, "gate status is invalid")
    _required_array(value, "blockers", code=code)


def _validate_authorization_payload(payload: dict[str, Any], *, code: str) -> None:
    for field in (
        "authorization_id",
        "run_id",
        "created_at",
        "expires_at",
        "candidate_digest",
        "note_title",
        "content_html",
        "content_sha256",
        "nonce",
        "token_sha256",
        "external_claim_id",
        "write_attempt_id",
    ):
        _required_string(payload, field, code=code)
    _required_int(payload, "ttl_seconds", code=code, minimum=1, maximum=300)
    _required_int(payload, "content_length", code=code)
    _required_int(payload, "minimum_content_length", code=code)
    _string_array(payload, "tags", code=code)
    _string_array(payload, "required_headings", code=code)
    _string_array(payload, "forbidden_headings", code=code)
    _validate_ref_payload(payload.get("candidate"), code=code)
    _validate_target_payload(payload.get("target"), code=code)
    artifacts = _required_array(payload, "artifacts", code=code)
    for ref in artifacts:
        _validate_ref_payload(ref, code=code)
    envelope = _required_object(payload, "mcp_envelope", code=code)
    if envelope.get("action") != "create":
        raise BatchRuntimeError(code, "MCP envelope action must be create")
    _required_string(envelope, "parentKey", code=code)
    _required_string(envelope, "content", code=code)
    _string_array(envelope, "tags", code=code)
    live = _required_object(payload, "live_preflight", code=code)
    for field in (
        "parent_key",
        "parent_fingerprint",
        "requested_note_title",
    ):
        _required_string(live, field, code=code)
    _required_bool(live, "title_available", code=code)
    _nonempty_string_array(live, "matching_note_keys", code=code)
    _validate_ref_payload(live.get("parent_snapshot"), code=code)
    _validate_ref_payload(live.get("children_snapshot"), code=code)
    _validate_gate_payload(payload.get("gate"), code=code)


def _validate_verification_payload(payload: dict[str, Any], *, code: str) -> None:
    for field in (
        "verification_id",
        "run_id",
        "authorization_digest",
        "note_key",
        "content_sha256",
    ):
        _required_string(payload, field, code=code)
    _required_bool(payload, "verified", code=code)
    _required_int(payload, "content_length", code=code)
    _validate_ref_payload(payload.get("authorization"), code=code)
    _validate_target_payload(payload.get("target"), code=code)
    _validate_ref_payload(payload.get("note_snapshot"), code=code)
    _validate_ref_payload(payload.get("checks_snapshot"), code=code)
    checks = _required_array(payload, "checks", code=code)
    for check in checks:
        if not isinstance(check, dict):
            raise BatchRuntimeError(code, "verification checks must be JSON objects")
        _required_string(check, "name", code=code)
        _required_bool(check, "passed", code=code)
    artifacts = _required_array(payload, "artifacts", code=code)
    for ref in artifacts:
        _validate_ref_payload(ref, code=code)
    _validate_gate_payload(payload.get("gate"), code=code)


def _validate_reconciliation_payload(payload: dict[str, Any], *, code: str) -> None:
    for field in (
        "run_id",
        "authorization_digest",
    ):
        _required_string(payload, field, code=code)
    if payload.get("outcome") not in {"verified", "not_found", "ambiguous", "blocked"}:
        raise BatchRuntimeError(code, "reconciliation outcome is invalid")
    _required_int(payload, "match_count", code=code)
    _required_bool(payload, "retry_confirmation_required", code=code)
    _nonempty_string_array(payload, "matched_note_keys", code=code)
    _validate_ref_payload(payload.get("authorization"), code=code)
    _validate_target_payload(payload.get("target"), code=code)
    _validate_ref_payload(payload.get("children_snapshot"), code=code)
    verification = payload.get("verification")
    if verification is not None:
        _validate_ref_payload(verification, code=code)
    artifacts = _required_array(payload, "artifacts", code=code)
    for ref in artifacts:
        _validate_ref_payload(ref, code=code)
    _validate_gate_payload(payload.get("gate"), code=code)


def _validate_consumed_single_fields(
    payload: dict[str, Any],
    *,
    schema_version: str,
    code: str,
) -> None:
    if schema_version == "paper_reader.write-authorization.v2":
        _validate_authorization_payload(payload, code=code)
    elif schema_version == "paper_reader.verification.v2":
        _validate_verification_payload(payload, code=code)
    elif schema_version == "paper_reader.reconciliation.v2":
        _validate_reconciliation_payload(payload, code=code)


def _authorization_fingerprint_material(
    view: RunView,
    *,
    item_id: str,
    authorization_path: Path,
) -> tuple[Path, bytes]:
    worker_result = _load_committed_worker_result(view, item_id)
    if worker_result.paper_reader_run is None:
        raise BatchRuntimeError("journal_corrupt", "worker result lacks its paper_reader run")
    run_root = normalized_absolute_path(Path(worker_result.paper_reader_run.path)).parent
    path = normalized_absolute_path(authorization_path)
    try:
        relative = path.relative_to(run_root)
    except ValueError as exc:
        raise BatchRuntimeError("authorization_tampered", "authorization is outside the candidate run") from exc
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "authorizations"
        or path.suffix != ".json"
        or _PORTABLE_IDENTIFIER.fullmatch(path.stem) is None
    ):
        raise BatchRuntimeError("authorization_tampered", "authorization path/topology is not deterministic")
    raw, payload = read_json_bytes(
        path,
        code="authorization_tampered",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    authorization = _opaque_single_artifact(
        raw,
        payload,
        schema_version="paper_reader.write-authorization.v2",
        code="authorization_tampered",
        label="authorization",
    )
    if authorization.authorization_id != path.stem:
        raise BatchRuntimeError("authorization_tampered", "authorization id differs from its path")
    return path, raw


def _load_authorization(
    view: RunView,
    *,
    item_id: str,
    authorization_path: Path,
    claim_id: str,
    write_attempt_id: str,
) -> tuple[Path, bytes, _OpaqueRecord, dict[str, Any]]:
    worker_result, candidate_raw, candidate, _note_md, note_html = _candidate_material(view, item_id)
    if worker_result.paper_reader_run is None or worker_result.candidate is None:
        raise BatchRuntimeError("journal_corrupt", "worker result lacks run/candidate artifacts")
    paper_run_path = normalized_absolute_path(Path(worker_result.paper_reader_run.path))
    paper_run_root = paper_run_path.parent
    path = normalized_absolute_path(authorization_path)
    try:
        relative = path.relative_to(paper_run_root)
    except ValueError as exc:
        raise BatchRuntimeError("authorization_tampered", "authorization is outside the candidate run") from exc
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "authorizations"
        or path.suffix != ".json"
        or _PORTABLE_IDENTIFIER.fullmatch(path.stem) is None
    ):
        raise BatchRuntimeError("authorization_tampered", "authorization path/topology is not deterministic")
    raw, payload = read_json_bytes(path, code="authorization_tampered")
    authorization = _opaque_single_artifact(
        raw,
        payload,
        schema_version="paper_reader.write-authorization.v2",
        code="authorization_tampered",
        label="authorization",
    )
    if (
        authorization.authorization_id != path.stem
        or relative.parts != ("authorizations", f"{authorization.authorization_id}.json")
    ):
        raise BatchRuntimeError("authorization_tampered", "authorization path/topology is not deterministic")
    sidecar = path.with_suffix("")
    expected_names = {"candidate.json", "children.json", "content.html", "parent.json", "record.json"}
    if set(list_directory(sidecar)) != expected_names:
        raise BatchRuntimeError("authorization_tampered", "authorization sidecar is not the fixed five-file closure")
    by_role: dict[str, _OpaqueRecord] = {}
    for ref in authorization.artifacts:
        if ref.role in by_role:
            raise BatchRuntimeError("authorization_tampered", "authorization repeats an artifact role")
        by_role[ref.role] = ref
    expected_refs = {
        "candidate_snapshot": ("candidate.json", "application/json"),
        "authorized_content_html": ("content.html", "text/html"),
        "zotero_parent_snapshot": ("parent.json", "application/json"),
        "zotero_children_snapshot": ("children.json", "application/json"),
    }
    if set(by_role) != set(expected_refs):
        raise BatchRuntimeError("authorization_tampered", "authorization artifact roles are not closed-world")
    sidecar_bytes: dict[str, bytes] = {}
    for role, (name, media_type) in expected_refs.items():
        ref = by_role[role]
        member_path, member_raw = _ref_bytes(paper_run_root, ref, code="authorization_tampered")
        if member_path != sidecar / name or ref.media_type != media_type:
            raise BatchRuntimeError("authorization_tampered", f"authorization {role} path/media type changed")
        sidecar_bytes[name] = member_raw
    if read_bytes(
        sidecar / "record.json",
        code="authorization_tampered",
        max_bytes=len(raw),
    ) != raw:
        raise BatchRuntimeError("authorization_tampered", "authorization recovery record differs from main artifact")
    _active_collector_close(sidecar, expected_names)
    candidate_ref = by_role["candidate_snapshot"]
    if authorization.candidate != candidate_ref or sidecar_bytes["candidate.json"] != candidate_raw:
        raise BatchRuntimeError("authorization_tampered", "authorization candidate snapshot differs from batch candidate")
    if sidecar_bytes["content.html"] != note_html:
        raise BatchRuntimeError("authorization_tampered", "authorized HTML differs from immutable candidate")
    canonical_html = authorization.content_html.rstrip("\r\n")
    candidate_target = candidate.get("target")
    if (
        authorization.run_id != candidate.get("run_id")
        or authorization.candidate_digest != worker_result.candidate.sha256
        or authorization.candidate_digest != sha256_bytes(candidate_raw)
        or _plain(authorization.target) != candidate_target
        or authorization.note_title != candidate.get("note_title")
        or list(authorization.tags) != candidate.get("tags")
        or authorization.content_html.encode("utf-8") != note_html
        or authorization.content_sha256 != candidate.get("content_sha256")
        or authorization.content_sha256 != sha256_bytes(canonical_html.encode("utf-8"))
        or authorization.content_length != candidate.get("content_length")
        or authorization.content_length != len(canonical_html)
        or authorization.minimum_content_length > authorization.content_length
        or authorization.external_claim_id != claim_id
        or authorization.write_attempt_id != write_attempt_id
        or _plain(authorization.mcp_envelope)
        != {
            "action": "create",
            "parentKey": authorization.target.parent_key,
            "content": authorization.content_html,
            "tags": list(authorization.tags),
        }
    ):
        raise BatchRuntimeError("authorization_identity_mismatch", "authorization does not bind this exact write attempt")
    _require_sha256(authorization.content_sha256, label="authorization content hash", code="authorization_tampered")
    _require_sha256(authorization.token_sha256, label="authorization token hash", code="authorization_tampered")
    created_at = _parse_utc(authorization.created_at)
    expires_at = _parse_utc(authorization.expires_at)
    if expires_at <= created_at or expires_at - created_at != timedelta(seconds=authorization.ttl_seconds):
        raise BatchRuntimeError("authorization_tampered", "authorization TTL/timestamps are inconsistent")
    if (
        len(authorization.nonce) < 32
        or authorization.gate.status != "write_ready"
        or authorization.gate.blockers
        or not authorization.live_preflight.title_available
        or authorization.live_preflight.matching_note_keys
        or authorization.live_preflight.parent_key != authorization.target.parent_key
        or authorization.live_preflight.parent_fingerprint != authorization.target.parent_fingerprint
        or authorization.live_preflight.requested_note_title != authorization.note_title
        or authorization.live_preflight.parent_snapshot != by_role["zotero_parent_snapshot"]
        or authorization.live_preflight.children_snapshot != by_role["zotero_children_snapshot"]
    ):
        raise BatchRuntimeError("authorization_tampered", "authorization live preflight or gate is invalid")
    parent_payload = _parse_json_bytes(
        sidecar_bytes["parent.json"],
        code="authorization_tampered",
        label="authorization parent snapshot",
    )
    children_payload = _parse_json_bytes(
        sidecar_bytes["children.json"],
        code="authorization_tampered",
        label="authorization children snapshot",
    )
    if (
        canonical_json_bytes(parent_payload) != sidecar_bytes["parent.json"]
        or canonical_json_bytes(children_payload) != sidecar_bytes["children.json"]
        or not isinstance(parent_payload, dict)
        or not isinstance(children_payload, list)
    ):
        raise BatchRuntimeError("authorization_tampered", "authorization live snapshots are not canonical")
    try:
        parent_key, _title, _doi, _version, parent_digest = _consumed_parent_fingerprint(
            parent_payload
        )
    except BatchRuntimeError as exc:
        raise BatchRuntimeError(
            "authorization_tampered",
            "authorization parent snapshot is malformed",
        ) from exc
    if (
        parent_key != authorization.target.parent_key
        or parent_digest != authorization.target.parent_fingerprint
    ):
        raise BatchRuntimeError("authorization_tampered", "authorization parent fingerprint changed")
    for child in children_payload:
        if not isinstance(child, dict):
            raise BatchRuntimeError("authorization_tampered", "authorization child snapshot is malformed")
        child_data = child.get("data")
        if not isinstance(child_data, dict) or child_data.get("itemType") != "note":
            continue
        child_parent = str(child_data.get("parentItem") or "").strip()
        if child_parent != authorization.target.parent_key:
            raise BatchRuntimeError("authorization_tampered", "authorization child belongs to another parent")
        child_parser = _HeadingParser()
        child_parser.feed(str(child_data.get("note") or ""))
        if child_parser.title == authorization.note_title:
            raise BatchRuntimeError("authorization_tampered", "authorization title was not available in live snapshot")
    paper_run_raw, paper_run = read_json_bytes(
        paper_run_path,
        code="authorization_tampered",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    if not isinstance(paper_run, dict) or paper_run_raw != canonical_json_bytes(paper_run):
        raise BatchRuntimeError("authorization_tampered", "paper_reader run is not canonical")
    run_refs = [
        ref
        for ref in paper_run.get("artifacts", [])
        if isinstance(ref, dict)
        and ref.get("role") == "write_authorization"
        and ref.get("path") == relative.as_posix()
    ]
    digest_refs = [
        ref
        for ref in paper_run.get("artifacts", [])
        if isinstance(ref, dict)
        and ref.get("role") == "write_authorization"
        and ref.get("sha256") == sha256_bytes(raw)
    ]
    if (
        len(run_refs) != 1
        or digest_refs != run_refs
        or run_refs[0].get("sha256") != sha256_bytes(raw)
        or run_refs[0].get("size_bytes") != len(raw)
        or run_refs[0].get("media_type") != "application/json"
    ):
        raise BatchRuntimeError("authorization_not_bound", "paper_reader run does not bind this exact authorization")
    return path, raw, authorization, candidate


def _authorization_path_for_digest(
    view: RunView,
    *,
    item_id: str,
    authorization_sha256: str,
) -> Path:
    worker_result = _load_committed_worker_result(view, item_id)
    if worker_result.paper_reader_run is None:
        raise BatchRuntimeError("journal_corrupt", "worker result is missing paper_reader run")
    paper_run_path = normalized_absolute_path(Path(worker_result.paper_reader_run.path))
    run_root = paper_run_path.parent
    _raw, run = read_json_bytes(
        paper_run_path,
        code="authorization_tampered",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    if not isinstance(run, dict) or not isinstance(run.get("artifacts"), list):
        raise BatchRuntimeError("authorization_tampered", "paper_reader run artifacts are invalid")
    matches: list[Path] = []
    for ref in run["artifacts"]:
        if (
            not isinstance(ref, dict)
            or ref.get("role") != "write_authorization"
            or ref.get("sha256") != authorization_sha256
        ):
            continue
        _validate_ref_payload(ref, code="authorization_tampered")
        path = _safe_inner_path(run_root, str(ref.get("path", "")), code="authorization_tampered")
        relative = path.relative_to(run_root)
        if (
            len(relative.parts) != 2
            or relative.parts[0] != "authorizations"
            or path.suffix != ".json"
            or _PORTABLE_IDENTIFIER.fullmatch(path.stem) is None
            or ref.get("media_type") != "application/json"
        ):
            raise BatchRuntimeError(
                "authorization_tampered",
                "run-bound authorization path/topology is invalid",
            )
        raw = read_bytes(
            path,
            code="authorization_tampered",
            max_bytes=_ref_read_limit(
                ref.get("size_bytes"),
                code="authorization_tampered",
                json_artifact=True,
            ),
        )
        if len(raw) != ref.get("size_bytes") or sha256_bytes(raw) != authorization_sha256:
            raise BatchRuntimeError("authorization_tampered", "run-bound authorization bytes changed")
        payload = _parse_json_bytes(
            raw,
            code="authorization_tampered",
            label="run-bound authorization",
        )
        authorization = _opaque_single_artifact(
            raw,
            payload,
            schema_version="paper_reader.write-authorization.v2",
            code="authorization_tampered",
            label="run-bound authorization",
        )
        if authorization.authorization_id != path.stem:
            raise BatchRuntimeError(
                "authorization_tampered",
                "run-bound authorization id differs from its deterministic path",
            )
        matches.append(path)
    if len(matches) != 1:
        raise BatchRuntimeError(
            "authorization_not_bound",
            "paper_reader run must bind exactly one authorization with the started digest",
        )
    return matches[0]


def _load_verification(
    view: RunView,
    *,
    item_id: str,
    verification_ref,
    authorization_raw: bytes,
    authorization: _OpaqueRecord,
) -> tuple[Path, bytes, _OpaqueRecord]:
    worker_result = _load_committed_worker_result(view, item_id)
    if worker_result.paper_reader_run is None:
        raise BatchRuntimeError("journal_corrupt", "worker result is missing paper_reader run")
    paper_run_path = normalized_absolute_path(Path(worker_result.paper_reader_run.path))
    paper_run_root = paper_run_path.parent
    path = normalized_absolute_path(Path(verification_ref.path))
    expected_parent = paper_run_root / "verifications" / authorization.authorization_id
    if (
        path.parent != expected_parent
        or path.suffix != ".json"
        or _PORTABLE_IDENTIFIER.fullmatch(path.stem) is None
    ):
        raise BatchRuntimeError("verification_tampered", "verification path/topology is not deterministic")
    raw, payload = read_json_bytes(
        path,
        code="verification_tampered",
        max_bytes=_ref_read_limit(
            verification_ref.size_bytes,
            code="verification_tampered",
            json_artifact=True,
        ),
    )
    verification = _opaque_single_artifact(
        raw,
        payload,
        schema_version="paper_reader.verification.v2",
        code="verification_tampered",
        label="verification",
    )
    if (
        sha256_bytes(raw) != verification_ref.sha256
        or len(raw) != verification_ref.size_bytes
        or verification_ref.schema_version != "paper_reader.verification.v2"
        or verification_ref.artifact_id != verification.verification_id
    ):
        raise BatchRuntimeError("verification_tampered", "verification outer ref or canonical bytes changed")
    expected_path = (
        paper_run_root
        / "verifications"
        / authorization.authorization_id
        / f"{verification.note_key}.json"
    )
    if not _PORTABLE_IDENTIFIER.fullmatch(verification.note_key) or path != expected_path:
        raise BatchRuntimeError("verification_tampered", "verification path/topology is not deterministic")
    sidecar = path.with_suffix("")
    if set(list_directory(sidecar)) != {"authorization.json", "checks.json", "note.json", "record.json"}:
        raise BatchRuntimeError("verification_tampered", "verification sidecar is not the fixed four-file closure")
    by_role: dict[str, _OpaqueRecord] = {}
    for ref in verification.artifacts:
        if ref.role in by_role:
            raise BatchRuntimeError("verification_tampered", "verification repeats an artifact role")
        by_role[ref.role] = ref
    expected_refs = {
        "authorization_snapshot": ("authorization.json", "application/json"),
        "zotero_note_readback": ("note.json", "application/json"),
        "verification_checks": ("checks.json", "application/json"),
    }
    if set(by_role) != set(expected_refs):
        raise BatchRuntimeError("verification_tampered", "verification artifact roles are not closed-world")
    members: dict[str, bytes] = {}
    for role, (name, media_type) in expected_refs.items():
        ref = by_role[role]
        member_path, member_raw = _ref_bytes(paper_run_root, ref, code="verification_tampered")
        if member_path != sidecar / name or ref.media_type != media_type:
            raise BatchRuntimeError("verification_tampered", f"verification {role} path/media type changed")
        members[name] = member_raw
    if (
        read_bytes(
            sidecar / "record.json",
            code="verification_tampered",
            max_bytes=len(raw),
        )
        != raw
        or members["authorization.json"] != authorization_raw
        or verification.authorization != by_role["authorization_snapshot"]
        or verification.note_snapshot != by_role["zotero_note_readback"]
        or verification.checks_snapshot != by_role["verification_checks"]
    ):
        raise BatchRuntimeError("verification_tampered", "verification sidecar bindings changed")
    _active_collector_close(
        sidecar,
        {"authorization.json", "checks.json", "note.json", "record.json"},
    )
    note_snapshot = _parse_json_bytes(
        members["note.json"],
        code="verification_tampered",
        label="verification note snapshot",
    )
    checks_snapshot = _parse_json_bytes(
        members["checks.json"],
        code="verification_tampered",
        label="verification checks snapshot",
    )
    if (
        canonical_json_bytes(note_snapshot) != members["note.json"]
        or canonical_json_bytes(checks_snapshot) != members["checks.json"]
        or not isinstance(note_snapshot, dict)
        or not isinstance(checks_snapshot, dict)
    ):
        raise BatchRuntimeError("verification_tampered", "verification snapshots are not canonical objects")
    expected_checks = {
        "note_key",
        "item_type",
        "parent_key",
        "note_title",
        "tag_set",
        "required_headings",
        "forbidden_headings",
        "minimum_content_length",
        "content_length",
        "content_sha256",
    }
    checks_by_name = {check.name: check for check in verification.checks}
    if (
        len(checks_by_name) != len(verification.checks)
        or set(checks_by_name) != expected_checks
        or not all(check.passed for check in verification.checks)
        or checks_snapshot.get("format") != "paper_reader.verification-checks.v2-internal"
        or checks_snapshot.get("authorization_digest") != sha256_bytes(authorization_raw)
        or checks_snapshot.get("note_key") != verification.note_key
        or checks_snapshot.get("checks")
        != [_plain(check) for check in verification.checks]
    ):
        raise BatchRuntimeError("verification_failed", "verification does not contain the complete passed check set")
    data = note_snapshot.get("data")
    if not isinstance(data, dict):
        raise BatchRuntimeError("verification_failed", "verification note snapshot has no data object")
    note_html = str(data.get("note") or "")
    canonical_html = note_html.rstrip("\r\n")
    parser = _HeadingParser()
    parser.feed(note_html)
    tags = _snapshot_tag_set(data, code="verification_failed")
    missing_headings = [heading for heading in authorization.required_headings if heading not in parser.headings]
    forbidden_headings = [heading for heading in authorization.forbidden_headings if heading in parser.headings]
    if (
        not verification.verified
        or verification.gate.status != "passed"
        or verification.gate.blockers
        or verification.run_id != authorization.run_id
        or verification.authorization_digest != sha256_bytes(authorization_raw)
        or verification.target != authorization.target
        or str(note_snapshot.get("key") or "").strip() != verification.note_key
        or str(data.get("key") or "").strip() != verification.note_key
        or data.get("itemType") != "note"
        or str(data.get("parentItem") or "").strip() != authorization.target.parent_key
        or parser.title != authorization.note_title
        or tags != set(authorization.tags)
        or missing_headings
        or forbidden_headings
        or len(canonical_html) < authorization.minimum_content_length
        or len(canonical_html) != authorization.content_length
        or sha256_bytes(canonical_html.encode("utf-8")) != authorization.content_sha256
        or verification.content_length != authorization.content_length
        or verification.content_sha256 != authorization.content_sha256
    ):
        raise BatchRuntimeError("verification_failed", "verification readback fails strong authorization checks")
    paper_run_raw, paper_run = read_json_bytes(
        paper_run_path,
        code="verification_tampered",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    if not isinstance(paper_run, dict) or paper_run_raw != canonical_json_bytes(paper_run):
        raise BatchRuntimeError("verification_tampered", "paper_reader run is not canonical")
    relative = path.relative_to(paper_run_root).as_posix()
    run_refs = [
        ref
        for ref in paper_run.get("artifacts", [])
        if isinstance(ref, dict)
        and ref.get("role") == "zotero_verification"
        and ref.get("path") == relative
    ]
    if len(run_refs) != 1 or run_refs[0].get("sha256") != sha256_bytes(raw) or run_refs[0].get("size_bytes") != len(raw):
        raise BatchRuntimeError("verification_not_bound", "paper_reader run does not bind this exact verification")
    return path, raw, verification


def _claim_result(view: RunView, data: WriteClaimedData) -> dict[str, Any]:
    worker_result = _load_committed_worker_result(view, data.item_id)
    assert worker_result.candidate is not None
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "writer_id": data.writer_id,
        "claim_id": data.claim_id,
        "write_attempt_id": data.write_attempt_id,
        "attempt_number": data.attempt_number,
        "lease_token": derive_lease_token(
            view.lease_secret,
            lane="write",
            claim_id=data.claim_id,
            attempt_id=data.write_attempt_id,
        ),
        "issued_at": data.issued_at,
        "expires_at": data.expires_at,
        "candidate_path": worker_result.candidate.path,
        "candidate_sha256": data.candidate_sha256,
    }


def claim_write(
    run_dir: Path,
    *,
    writer_id: str,
    request_id: str,
    lease_seconds: int = DEFAULT_WRITE_LEASE_SECONDS,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.claim",
    )
    if not writer_id.strip():
        _raise_input_error(existing_event, "invalid_writer", "writer id must not be empty")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_WRITE_LEASE_SECONDS:
        _raise_input_error(
            existing_event,
            "invalid_lease",
            f"write lease seconds must be between 1 and {MAX_WRITE_LEASE_SECONDS}",
        )
    fingerprint = canonical_sha256(
        {
            "command": "write.claim",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "writer_id": writer_id,
            "lease_seconds": lease_seconds,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        if view.manifest.write_policy != "zotero_write":
            raise BatchRuntimeError("write_policy_forbidden", "manifest does not authorize the Zotero write lane")
        if any(item.write_status in {"claimed", "started", "uncertain"} for item in view.state.items):
            raise BatchRuntimeError("write_lane_busy", "serial write lane already has active or uncertain work")
        state_item = next((item for item in view.state.items if item.write_status == "queued"), None)
        if state_item is None:
            raise BatchRuntimeError("no_available_write", "no Zotero candidate is currently write-claimable")
        worker_result = _load_committed_worker_result(view, state_item.item_id)
        assert worker_result.candidate is not None
        claim_id = str(uuid4())
        write_attempt_id = state_item.write_pending_attempt_id or str(uuid4())
        lease_token = derive_lease_token(
            view.lease_secret,
            lane="write",
            claim_id=claim_id,
            attempt_id=write_attempt_id,
        )
        expires_at = _format_utc(_parse_utc(transaction_time) + timedelta(seconds=lease_seconds))
        data = WriteClaimedData(
            item_id=state_item.item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            write_attempt_id=write_attempt_id,
            attempt_number=state_item.write_attempt_count + 1,
            lease_token_sha256=sha256_bytes(lease_token.encode()),
            issued_at=transaction_time,
            expires_at=expires_at,
            candidate_sha256=worker_result.candidate.sha256,
        )
        return ProposedTransition(data=data, result=_claim_result(view, data))

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteClaimedData):
            raise BatchRuntimeError("journal_corrupt", "write claim request points to another event type")
        return _claim_result(view, event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.claim",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def preview_write(
    run_dir: Path,
    item_id: str,
    *,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    view = load_run_view(run_dir)
    current_time = now or _format_utc(datetime.now(timezone.utc))
    _active_write_lease(
        view,
        item_id=item_id,
        writer_id=writer_id,
        claim_id=claim_id,
        lease_token=lease_token,
        write_attempt_id=write_attempt_id,
        now=current_time,
        allowed_statuses={"claimed"},
    )
    worker_result, candidate_raw, candidate, note_md, note_html = _candidate_material(view, item_id)
    try:
        candidate_text = candidate_raw.decode("utf-8")
        markdown_text = note_md.decode("utf-8")
        html_text = note_html.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchRuntimeError("candidate_tampered", "candidate preview artifacts are not UTF-8") from exc
    return {
        "run_dir": str(view.run_dir),
        "item_id": item_id,
        "writer_id": writer_id,
        "claim_id": claim_id,
        "write_attempt_id": write_attempt_id,
        "candidate_path": worker_result.candidate.path if worker_result.candidate else "",
        "candidate_sha256": worker_result.candidate.sha256 if worker_result.candidate else "",
        "target": candidate.get("target"),
        "note_title": candidate.get("note_title"),
        "tags": candidate.get("tags"),
        "content_sha256": candidate.get("content_sha256"),
        "candidate_json": candidate_text,
        "note_markdown": markdown_text,
        "note_html": html_text,
        "authorization_present_for_attempt": _authorization_present_for_attempt(
            worker_result,
            claim_id=claim_id,
            write_attempt_id=write_attempt_id,
        ),
    }


def _lease_mutation_result(data: WriteLeaseMutationData) -> dict[str, Any]:
    return {
        "item_id": data.item_id,
        "writer_id": data.writer_id,
        "claim_id": data.claim_id,
        "write_attempt_id": data.write_attempt_id,
        "attempt_number": data.attempt_number,
        "candidate_sha256": data.candidate_sha256,
        "issued_at": data.issued_at,
        "expires_at": data.expires_at,
        "status": "lease_extended" if data.kind == "write.renewed" else "queued",
    }


def renew_write(
    run_dir: Path,
    item_id: str,
    *,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    request_id: str,
    lease_seconds: int = DEFAULT_WRITE_LEASE_SECONDS,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.renew",
    )
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_WRITE_LEASE_SECONDS:
        _raise_input_error(
            existing_event,
            "invalid_lease",
            f"write lease seconds must be between 1 and {MAX_WRITE_LEASE_SECONDS}",
        )
    token_sha256 = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "write.renew",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "writer_id": writer_id,
            "claim_id": claim_id,
            "write_attempt_id": write_attempt_id,
            "lease_token_sha256": token_sha256,
            "lease_seconds": lease_seconds,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        item, lease = _active_write_lease(
            view,
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            now=transaction_time,
            allowed_statuses={"claimed", "started"},
        )
        expires_at = _format_utc(_parse_utc(transaction_time) + timedelta(seconds=lease_seconds))
        if _parse_utc(expires_at) <= _parse_utc(lease.expires_at):
            raise BatchRuntimeError("write_lease_not_extended", "write renewal must extend the current expiry")
        data = WriteLeaseMutationData(
            kind="write.renewed",
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            write_attempt_id=write_attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_sha256,
            candidate_sha256=item.candidate_sha256,
            issued_at=transaction_time,
            expires_at=expires_at,
        )
        return ProposedTransition(data=data, result=_lease_mutation_result(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteLeaseMutationData) or event.data.kind != "write.renewed":
            raise BatchRuntimeError("journal_corrupt", "write renew request points to another event type")
        return _lease_mutation_result(event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.renew",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def release_write(
    run_dir: Path,
    item_id: str,
    *,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, _existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.release",
    )
    token_sha256 = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "write.release",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "writer_id": writer_id,
            "claim_id": claim_id,
            "write_attempt_id": write_attempt_id,
            "lease_token_sha256": token_sha256,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        item, lease = _active_write_lease(
            view,
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            now=transaction_time,
            allowed_statuses={"claimed"},
        )
        data = WriteLeaseMutationData(
            kind="write.released",
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            write_attempt_id=write_attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_sha256,
            candidate_sha256=item.candidate_sha256,
            issued_at=None,
            expires_at=None,
        )
        return ProposedTransition(data=data, result=_lease_mutation_result(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteLeaseMutationData) or event.data.kind != "write.released":
            raise BatchRuntimeError("journal_corrupt", "write release request points to another event type")
        return _lease_mutation_result(event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.release",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def _begin_result(
    view: RunView,
    data: WriteStartedData,
    authorization: _OpaqueRecord,
) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "writer_id": data.writer_id,
        "claim_id": data.claim_id,
        "write_attempt_id": data.write_attempt_id,
        "attempt_number": data.attempt_number,
        "candidate_sha256": data.candidate_sha256,
        "authorization_sha256": data.authorization_sha256,
        "authorization_nonce_sha256": data.authorization_nonce_sha256,
        "external_claim_id": data.external_claim_id,
        "started_at": data.started_at,
        "mcp_envelope": _plain(authorization.mcp_envelope),
        "delivery_rule": "send_only_when_command_result.replayed_is_false",
    }


def validate_write_started_artifacts(
    view: RunView,
    data: WriteStartedData,
) -> _OpaqueRecord:
    item = next((entry for entry in view.state.items if entry.item_id == data.item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {data.item_id}")
    lease = item.write_lease
    if item.write_status != "claimed" or lease is None:
        raise BatchRuntimeError("write_lease_inactive", "write.started requires the prior claimed state")
    if (
        lease.writer_id != data.writer_id
        or lease.claim_id != data.claim_id
        or lease.write_attempt_id != data.write_attempt_id
        or lease.attempt_number != data.attempt_number
        or lease.lease_token_sha256 != data.lease_token_sha256
        or lease.candidate_sha256 != data.candidate_sha256
        or item.candidate_sha256 != data.candidate_sha256
    ):
        raise BatchRuntimeError("authorization_identity_mismatch", "write.started differs from claimed identity")
    authorization_path = _authorization_path_for_digest(
        view,
        item_id=data.item_id,
        authorization_sha256=data.authorization_sha256,
    )
    _path, raw, authorization, _candidate = _load_authorization(
        view,
        item_id=data.item_id,
        authorization_path=authorization_path,
        claim_id=data.claim_id,
        write_attempt_id=data.write_attempt_id,
    )
    started_at = _parse_utc(data.started_at)
    if (
        sha256_bytes(raw) != data.authorization_sha256
        or sha256_bytes(authorization.nonce.encode()) != data.authorization_nonce_sha256
        or authorization.external_claim_id != data.external_claim_id
        or data.external_claim_id != data.claim_id
        or _parse_utc(authorization.created_at) > started_at
        or _parse_utc(authorization.expires_at) - started_at
        < timedelta(seconds=MIN_AUTHORIZATION_REMAINING_SECONDS)
    ):
        raise BatchRuntimeError(
            "authorization_identity_mismatch",
            "write.started authorization digest/nonce/identity/lifetime is invalid",
        )
    return authorization


def begin_write(
    run_dir: Path,
    item_id: str,
    *,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    authorization_path: Path,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.begin",
    )
    if existing_event is None:
        normalized_authorization_path, authorization_raw = _authorization_fingerprint_material(
            preflight,
            item_id=item_id,
            authorization_path=authorization_path,
        )
        authorization_sha256 = sha256_bytes(authorization_raw)
    else:
        if not isinstance(existing_event.data, WriteStartedData):
            raise BatchRuntimeError(
                "journal_corrupt",
                "write begin request is bound to an unexpected event payload",
            )
        normalized_authorization_path = normalized_absolute_path(authorization_path)
        authorization_sha256 = existing_event.data.authorization_sha256
    token_sha256 = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "write.begin",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "writer_id": writer_id,
            "claim_id": claim_id,
            "write_attempt_id": write_attempt_id,
            "lease_token_sha256": token_sha256,
            "authorization_path": str(normalized_authorization_path),
            "authorization_sha256": authorization_sha256,
            "now_override": now,
        }
    )
    if existing_event is not None and existing_event.request_fingerprint != fingerprint:
        raise BatchRuntimeError(
            "idempotency_conflict",
            "request id is already bound to different write begin input",
        )

    proposed_data: WriteStartedData | None = None
    proposed_collector: _ExternalClosureCollector | None = None

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        nonlocal proposed_data, proposed_collector
        item, lease = _active_write_lease(
            view,
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            now=transaction_time,
            allowed_statuses={"claimed"},
        )
        with _collect_external_closure(code="authorization_tampered") as collector:
            _path, current_raw, authorization, _candidate_payload = _load_authorization(
                view,
                item_id=item_id,
                authorization_path=normalized_authorization_path,
                claim_id=claim_id,
                write_attempt_id=write_attempt_id,
            )
            if sha256_bytes(current_raw) != authorization_sha256:
                raise BatchRuntimeError("authorization_tampered", "authorization changed before write begin")
            if _parse_utc(authorization.expires_at) - _parse_utc(transaction_time) < timedelta(
                seconds=MIN_AUTHORIZATION_REMAINING_SECONDS
            ):
                raise BatchRuntimeError(
                    "authorization_expiring",
                    f"authorization must retain at least {MIN_AUTHORIZATION_REMAINING_SECONDS} seconds",
                )
            data = WriteStartedData(
                item_id=item_id,
                writer_id=writer_id,
                claim_id=claim_id,
                write_attempt_id=write_attempt_id,
                attempt_number=lease.attempt_number,
                lease_token_sha256=token_sha256,
                candidate_sha256=item.candidate_sha256,
                authorization_sha256=authorization_sha256,
                authorization_nonce_sha256=sha256_bytes(authorization.nonce.encode()),
                external_claim_id=claim_id,
                started_at=transaction_time,
            )
            validate_write_started_artifacts(view, data)
        collector.freeze()
        proposed_data = data
        proposed_collector = collector
        return ProposedTransition(data=data, result=_begin_result(view, data, authorization))

    def commit_validate(view: RunView) -> None:
        if proposed_data is None or proposed_collector is None:
            raise BatchRuntimeError(
                "journal_corrupt",
                "write begin proposal lost its validated event data",
            )
        validate_write_started_artifacts(view, proposed_data)

    def final_freshness_validate(view: RunView, effective_now: str) -> None:
        if proposed_data is None:
            raise BatchRuntimeError(
                "journal_corrupt",
                "write begin proposal lost its validated event data",
            )
        _active_write_lease(
            view,
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            now=effective_now,
            allowed_statuses={"claimed"},
        )
        authorization = validate_write_started_artifacts(view, proposed_data)
        if _parse_utc(authorization.expires_at) - _parse_utc(effective_now) < timedelta(
            seconds=MIN_AUTHORIZATION_REMAINING_SECONDS
        ):
            raise BatchRuntimeError(
                "authorization_expiring",
                f"authorization must retain at least {MIN_AUTHORIZATION_REMAINING_SECONDS} seconds",
            )

    @contextmanager
    def commit_guard(
        view: RunView,
        event=None,
    ) -> Iterator[Callable[[], None]]:
        nonlocal proposed_data, proposed_collector
        if event is None:
            if proposed_data is None or proposed_collector is None:
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "write begin proposal lost its validated event data",
                )
            data = proposed_data
            collector = proposed_collector
            semantic_validate = lambda: commit_validate(view)
        else:
            if not isinstance(event.data, WriteStartedData):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "pending write begin request points to another event type",
                )
            data = event.data
            with _collect_external_closure(code="authorization_tampered") as collector:
                validate_write_started_artifacts(view, data)
            collector.freeze()
            proposed_data = data
            proposed_collector = collector
            semantic_validate = lambda: validate_write_started_artifacts(view, data)
        with _locked_single_run_closure(
            view,
            item_id=item_id,
            authorization_sha256=data.authorization_sha256,
            collector=collector,
            validate=semantic_validate,
        ) as validate:
            yield validate

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteStartedData):
            raise BatchRuntimeError("journal_corrupt", "write begin request points to another event type")
        _path, current_raw, authorization, _candidate_payload = _load_authorization(
            view,
            item_id=event.data.item_id,
            authorization_path=normalized_authorization_path,
            claim_id=event.data.claim_id,
            write_attempt_id=event.data.write_attempt_id,
        )
        if sha256_bytes(current_raw) != event.data.authorization_sha256:
            raise BatchRuntimeError("journal_corrupt", "write begin authorization digest changed")
        return _begin_result(view, event.data, authorization)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.begin",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        commit_validate=commit_validate,
        commit_guard=commit_guard,
        final_freshness_validate=final_freshness_validate,
        fault=fault,
    )


def _load_write_result(path: Path) -> tuple[Path, bytes, WriteResult, str]:
    result_path = normalized_absolute_path(path)
    raw, payload = read_json_bytes(result_path, code="result_unreadable")
    if not isinstance(payload, dict) or payload.get("schema_version") != WRITE_RESULT_SCHEMA_VERSION:
        raise BatchRuntimeError("unsupported_run_schema", f"write result schema must be {WRITE_RESULT_SCHEMA_VERSION}")
    try:
        result = WriteResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_result", "write result failed strict validation") from exc
    if raw != canonical_json_bytes(result):
        raise BatchRuntimeError("invalid_result", "write result must use canonical JSON")
    return result_path, raw, result, sha256_bytes(raw)


def _commit_result(view: RunView, data: WriteWrittenData) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "writer_id": data.writer_id,
        "claim_id": data.claim_id,
        "write_attempt_id": data.write_attempt_id,
        "attempt_number": data.attempt_number,
        "status": "written",
        "result_path": str(view.run_dir / "results" / "write" / f"{data.result_sha256}.json"),
        "result_sha256": data.result_sha256,
        "candidate_sha256": data.candidate_sha256,
        "authorization_sha256": data.authorization_sha256,
        "note_key": data.note_key,
        "parent_key": data.parent_key,
        "canonical_html_sha256": data.canonical_html_sha256,
    }


def validate_write_result_payload(
    view: RunView,
    data: WriteWrittenData,
    raw: bytes,
) -> WriteResult:
    try:
        payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BatchRuntimeError("invalid_result", "write result publication is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != WRITE_RESULT_SCHEMA_VERSION:
        raise BatchRuntimeError("unsupported_run_schema", f"write result schema must be {WRITE_RESULT_SCHEMA_VERSION}")
    try:
        result = WriteResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_result", "write result failed strict validation") from exc
    if raw != canonical_json_bytes(result) or sha256_bytes(raw) != data.result_sha256:
        raise BatchRuntimeError("invalid_result", "write result digest or canonical bytes changed")
    item = next((entry for entry in view.state.items if entry.item_id == data.item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {data.item_id}")
    lease = item.write_lease
    if item.write_status != "started" or lease is None:
        raise BatchRuntimeError("write_lease_inactive", "write result requires the active started attempt")
    if not all(
        [
            item.write_started_event_sha256,
            item.authorization_sha256,
            item.authorization_nonce_sha256,
            item.external_claim_id,
            item.candidate_sha256,
        ]
    ):
        raise BatchRuntimeError("journal_corrupt", "started write state is missing immutable identities")
    started_events = [
        event
        for event in view.events
        if event.event_sha256 == result.started_event_sha256
        and isinstance(event.data, WriteStartedData)
    ]
    if len(started_events) != 1:
        raise BatchRuntimeError("result_identity_mismatch", "write result does not bind one prior write.started event")
    started = started_events[0].data
    if (
        started.item_id != data.item_id
        or started.writer_id != data.writer_id
        or started.claim_id != data.claim_id
        or started.write_attempt_id != data.write_attempt_id
        or started.attempt_number != data.attempt_number
        or started.lease_token_sha256 != data.lease_token_sha256
        or started.candidate_sha256 != data.candidate_sha256
        or started.authorization_sha256 != data.authorization_sha256
    ):
        raise BatchRuntimeError("result_identity_mismatch", "write result event differs from write.started identity")
    authorization_path = _authorization_path_for_digest(
        view,
        item_id=data.item_id,
        authorization_sha256=data.authorization_sha256,
    )
    _auth_path, authorization_raw, authorization, _candidate = _load_authorization(
        view,
        item_id=data.item_id,
        authorization_path=authorization_path,
        claim_id=data.claim_id,
        write_attempt_id=data.write_attempt_id,
    )
    _verification_path, _verification_raw, verification = _load_verification(
        view,
        item_id=data.item_id,
        verification_ref=result.verification,
        authorization_raw=authorization_raw,
        authorization=authorization,
    )
    if (
        result.manifest_sha256 != view.manifest_sha256
        or result.item_id != data.item_id
        or result.writer_id != data.writer_id
        or result.claim_id != data.claim_id
        or result.write_attempt_id != data.write_attempt_id
        or result.lease_token_sha256 != data.lease_token_sha256
        or result.started_event_sha256 != item.write_started_event_sha256
        or result.candidate_sha256 != data.candidate_sha256
        or result.candidate_sha256 != item.candidate_sha256
        or result.candidate_sha256 != lease.candidate_sha256
        or result.authorization_sha256 != data.authorization_sha256
        or result.authorization_sha256 != item.authorization_sha256
        or result.authorization_sha256 != sha256_bytes(authorization_raw)
        or result.authorization_nonce_sha256 != item.authorization_nonce_sha256
        or result.authorization_nonce_sha256 != started.authorization_nonce_sha256
        or result.authorization_nonce_sha256 != sha256_bytes(authorization.nonce.encode())
        or result.external_claim_id != item.external_claim_id
        or result.external_claim_id != started.external_claim_id
        or result.external_claim_id != authorization.external_claim_id
        or result.note_key != data.note_key
        or result.note_key != verification.note_key
        or result.parent_key != data.parent_key
        or result.parent_key != authorization.target.parent_key
        or result.canonical_html_sha256 != data.canonical_html_sha256
        or result.canonical_html_sha256 != authorization.content_sha256
    ):
        raise BatchRuntimeError("result_identity_mismatch", "write result does not bind the started attempt")
    return result


def commit_write(
    run_dir: Path,
    item_id: str,
    *,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    result_path: Path,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.commit",
    )
    input_path = normalized_absolute_path(result_path)
    raw: bytes | None = None
    result: WriteResult | None = None
    if existing_event is None:
        input_path, raw, result, result_sha256 = _load_write_result(input_path)
    else:
        if not isinstance(existing_event.data, WriteWrittenData):
            raise BatchRuntimeError(
                "journal_corrupt",
                "write commit request is bound to an unexpected event payload",
            )
        result_sha256 = existing_event.data.result_sha256
    token_sha256 = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "write.commit",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "writer_id": writer_id,
            "claim_id": claim_id,
            "write_attempt_id": write_attempt_id,
            "lease_token_sha256": token_sha256,
            "result_input_path": str(input_path),
            "result_sha256": result_sha256,
            "now_override": now,
        }
    )
    if existing_event is not None and existing_event.request_fingerprint != fingerprint:
        raise BatchRuntimeError(
            "idempotency_conflict",
            "request id is already bound to different write commit input",
        )

    proposed_data: WriteWrittenData | None = None
    proposed_collector: _ExternalClosureCollector | None = None

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        nonlocal proposed_data, proposed_collector
        assert raw is not None and result is not None
        item, lease = _active_write_lease(
            view,
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            now=transaction_time,
            allowed_statuses={"started"},
        )
        if not all(
            [
                item.write_started_event_sha256,
                item.authorization_sha256,
                item.authorization_nonce_sha256,
                item.external_claim_id,
                item.candidate_sha256,
            ]
        ):
            raise BatchRuntimeError("journal_corrupt", "started write state is missing immutable identities")
        data = WriteWrittenData(
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            write_attempt_id=write_attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_sha256,
            candidate_sha256=item.candidate_sha256,
            authorization_sha256=item.authorization_sha256,
            result_sha256=result_sha256,
            note_key=result.note_key,
            parent_key=result.parent_key,
            canonical_html_sha256=result.canonical_html_sha256,
        )
        with _collect_external_closure(code="verification_tampered") as collector:
            collector.add(input_path, raw)
            validate_write_result_payload(view, data, raw)
        collector.freeze()
        proposed_data = data
        proposed_collector = collector
        publication = ResultPublication(
            path=view.run_dir / "results" / "write" / f"{result_sha256}.json",
            content=raw,
        )
        return ProposedTransition(data=data, result=_commit_result(view, data), publication=publication)

    def commit_validate(view: RunView) -> None:
        if proposed_data is None or proposed_collector is None or raw is None:
            raise BatchRuntimeError(
                "journal_corrupt",
                "write commit proposal lost its validated result binding",
            )
        validate_write_result_payload(view, proposed_data, raw)

    @contextmanager
    def commit_guard(
        view: RunView,
        event=None,
    ) -> Iterator[Callable[[], None]]:
        if event is None:
            if proposed_data is None or proposed_collector is None or raw is None:
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "write commit proposal lost its validated event data",
                )
            data = proposed_data
            result_raw = raw
            collector = proposed_collector
            semantic_validate = lambda: commit_validate(view)
        else:
            if not isinstance(event.data, WriteWrittenData):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "pending write commit request points to another event type",
                )
            data = event.data
            durable_path = view.run_dir / "results" / "write" / f"{data.result_sha256}.json"
            with _collect_external_closure(code="verification_tampered") as collector:
                result_raw = read_bytes(
                    durable_path,
                    code="journal_corrupt",
                    max_bytes=MAX_JSON_ARTIFACT_BYTES,
                )
                validate_write_result_payload(view, data, result_raw)
            collector.freeze()
            semantic_validate = lambda: validate_write_result_payload(view, data, result_raw)
        with _locked_single_run_closure(
            view,
            item_id=item_id,
            authorization_sha256=data.authorization_sha256,
            collector=collector,
            validate=semantic_validate,
        ) as validate:
            yield validate

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteWrittenData):
            raise BatchRuntimeError("journal_corrupt", "write commit request points to another event type")
        return _commit_result(view, event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.commit",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        commit_validate=commit_validate,
        commit_guard=commit_guard,
        fault=fault,
    )


def _uncertain_result(view: RunView, data: WriteUncertainData) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "writer_id": data.writer_id,
        "claim_id": data.claim_id,
        "write_attempt_id": data.write_attempt_id,
        "attempt_number": data.attempt_number,
        "candidate_sha256": data.candidate_sha256,
        "authorization_sha256": data.authorization_sha256,
        "reason": data.reason,
        "status": "uncertain",
    }


def mark_write_uncertain(
    run_dir: Path,
    item_id: str,
    *,
    writer_id: str,
    claim_id: str,
    lease_token: str,
    write_attempt_id: str,
    reason: str,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.mark-uncertain",
    )
    normalized_reason = reason.strip()
    if not normalized_reason:
        _raise_input_error(existing_event, "invalid_reason", "uncertain reason must not be empty")
    token_sha256 = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "write.mark-uncertain",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "writer_id": writer_id,
            "claim_id": claim_id,
            "write_attempt_id": write_attempt_id,
            "lease_token_sha256": token_sha256,
            "reason": normalized_reason,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        item, lease = _active_write_lease(
            view,
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            lease_token=lease_token,
            write_attempt_id=write_attempt_id,
            now=transaction_time,
            allowed_statuses={"started"},
        )
        if item.authorization_sha256 is None:
            raise BatchRuntimeError("journal_corrupt", "started write is missing authorization digest")
        data = WriteUncertainData(
            kind="write.marked_uncertain",
            item_id=item_id,
            writer_id=writer_id,
            claim_id=claim_id,
            write_attempt_id=write_attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_sha256,
            candidate_sha256=lease.candidate_sha256,
            authorization_sha256=item.authorization_sha256,
            reason=normalized_reason,
        )
        return ProposedTransition(data=data, result=_uncertain_result(view, data))

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteUncertainData) or event.data.kind != "write.marked_uncertain":
            raise BatchRuntimeError("journal_corrupt", "write uncertain request points to another event type")
        return _uncertain_result(view, event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.mark-uncertain",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def _uncertain_item(view: RunView, item_id: str):
    item = next((entry for entry in view.state.items if entry.item_id == item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
    if item.write_status != "uncertain" or item.write_lease is not None:
        raise BatchRuntimeError("reconciliation_not_allowed", "write reconciliation requires an uncertain attempt")
    if not all(
        [
            item.write_last_writer_id,
            item.write_last_claim_id,
            item.write_last_attempt_id,
            item.write_last_lease_token_sha256,
            item.candidate_sha256,
            item.authorization_sha256,
            item.authorization_nonce_sha256,
            item.external_claim_id,
        ]
    ):
        raise BatchRuntimeError("journal_corrupt", "uncertain write state is missing durable attempt identities")
    return item


def _load_reconciliation_readback(
    view: RunView,
    *,
    item_id: str,
    readback_path: Path,
    authorization_raw: bytes,
    authorization: _OpaqueRecord,
) -> tuple[Path, bytes, _OpaqueRecord, _OpaqueRecord | None]:
    worker_result = _load_committed_worker_result(view, item_id)
    if worker_result.paper_reader_run is None:
        raise BatchRuntimeError("journal_corrupt", "worker result is missing paper_reader run")
    paper_run_path = normalized_absolute_path(Path(worker_result.paper_reader_run.path))
    paper_run_root = paper_run_path.parent
    path = normalized_absolute_path(readback_path)
    expected_path = paper_run_root / "reconciliations" / f"{authorization.authorization_id}.json"
    if path != expected_path:
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation path/topology is not deterministic")
    raw, payload = read_json_bytes(path, code="reconciliation_tampered")
    reconciliation = _opaque_single_artifact(
        raw,
        payload,
        schema_version="paper_reader.reconciliation.v2",
        code="reconciliation_tampered",
        label="reconciliation",
    )
    sidecar = path.with_suffix("")
    expected_names = {"authorization.json", "children.json", "record.json"}
    expected_refs = {
        "authorization_snapshot": ("authorization.json", "application/json"),
        "zotero_children_snapshot": ("children.json", "application/json"),
    }
    if (
        reconciliation.outcome in {"verified", "blocked"}
        and (reconciliation.match_count != 1 or reconciliation.verification is None)
    ):
        raise BatchRuntimeError(
            "reconciliation_tampered",
            "unique verified/blocked reconciliation requires an embedded strong verification",
        )
    if reconciliation.verification is not None:
        expected_names |= {"checks.json", "note.json", "verification.json"}
        expected_refs |= {
            "zotero_note_readback": ("note.json", "application/json"),
            "verification_checks": ("checks.json", "application/json"),
            "reconciliation_verification": ("verification.json", "application/json"),
        }
    if set(list_directory(sidecar)) != expected_names:
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation sidecar closure changed")
    by_role: dict[str, _OpaqueRecord] = {}
    for ref in reconciliation.artifacts:
        if ref.role in by_role:
            raise BatchRuntimeError("reconciliation_tampered", "reconciliation repeats an artifact role")
        by_role[ref.role] = ref
    if set(by_role) != set(expected_refs):
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation artifact roles are not closed-world")
    members: dict[str, bytes] = {}
    for role, (name, media_type) in expected_refs.items():
        ref = by_role[role]
        member_path, member_raw = _ref_bytes(paper_run_root, ref, code="reconciliation_tampered")
        if member_path != sidecar / name or ref.media_type != media_type:
            raise BatchRuntimeError("reconciliation_tampered", f"reconciliation {role} path/media type changed")
        members[name] = member_raw
    if (
        read_bytes(
            sidecar / "record.json",
            code="reconciliation_tampered",
            max_bytes=len(raw),
        )
        != raw
        or members["authorization.json"] != authorization_raw
        or reconciliation.authorization != by_role["authorization_snapshot"]
        or reconciliation.children_snapshot != by_role["zotero_children_snapshot"]
        or reconciliation.run_id != authorization.run_id
        or reconciliation.authorization_digest != sha256_bytes(authorization_raw)
        or reconciliation.target != authorization.target
        or reconciliation.match_count != len(reconciliation.matched_note_keys)
        or len(set(reconciliation.matched_note_keys)) != reconciliation.match_count
    ):
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation identity/bindings changed")
    _active_collector_close(sidecar, expected_names)
    children = _parse_json_bytes(
        members["children.json"],
        code="reconciliation_tampered",
        label="reconciliation children snapshot",
    )
    if canonical_json_bytes(children) != members["children.json"] or not isinstance(children, list):
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation children snapshot is not canonical list")
    exact_matches: list[str] = []
    for child in children:
        note_key = _reconciliation_exact_match_key(child, authorization)
        if note_key is not None:
            exact_matches.append(note_key)
    if tuple(exact_matches) != reconciliation.matched_note_keys:
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation exact-match locator result changed")
    verification: _OpaqueRecord | None = None
    if reconciliation.outcome == "not_found":
        valid_outcome = (
            reconciliation.match_count == 0
            and reconciliation.verification is None
            and reconciliation.retry_confirmation_required
            and reconciliation.gate.status == "blocked"
            and bool(reconciliation.gate.blockers)
        )
    elif reconciliation.outcome == "ambiguous":
        valid_outcome = (
            reconciliation.match_count > 1
            and reconciliation.verification is None
            and not reconciliation.retry_confirmation_required
            and reconciliation.gate.status == "blocked"
            and bool(reconciliation.gate.blockers)
        )
    else:
        valid_outcome = (
            reconciliation.match_count == 1
            and reconciliation.verification == by_role.get("reconciliation_verification")
            and not reconciliation.retry_confirmation_required
        )
        if reconciliation.verification is not None:
            _parse_json_bytes(
                members["verification.json"],
                code="reconciliation_tampered",
                label="embedded verification",
            )
            verification_payload = _parse_json_bytes(
                members["verification.json"],
                code="reconciliation_tampered",
                label="embedded verification",
            )
            verification = _opaque_single_artifact(
                members["verification.json"],
                verification_payload,
                schema_version="paper_reader.verification.v2",
                code="reconciliation_tampered",
                label="embedded verification",
            )
            note_snapshot = _parse_json_bytes(
                members["note.json"],
                code="reconciliation_tampered",
                label="embedded verification note snapshot",
            )
            checks_snapshot = _parse_json_bytes(
                members["checks.json"],
                code="reconciliation_tampered",
                label="embedded verification checks snapshot",
            )
            checks_by_name = {check.name: check for check in verification.checks}
            expected_check_names = {
                "note_key",
                "item_type",
                "parent_key",
                "note_title",
                "tag_set",
                "required_headings",
                "forbidden_headings",
                "minimum_content_length",
                "content_length",
                "content_sha256",
            }
            actuals, actual_sha256, actual_length = _verification_actuals(
                note_snapshot if isinstance(note_snapshot, dict) else {},
                note_key=verification.note_key,
                authorization=authorization,
            )
            strong_pass = all(actuals.values())
            embedded_valid = (
                canonical_json_bytes(_plain(verification)) == members["verification.json"]
                and canonical_json_bytes(note_snapshot) == members["note.json"]
                and canonical_json_bytes(checks_snapshot) == members["checks.json"]
                and reconciliation.verification == by_role["reconciliation_verification"]
                and reconciliation.verification.sha256 == sha256_bytes(members["verification.json"])
                and reconciliation.verification.size_bytes == len(members["verification.json"])
                and verification.run_id == authorization.run_id
                and verification.authorization == by_role["authorization_snapshot"]
                and verification.authorization_digest == sha256_bytes(authorization_raw)
                and verification.target == authorization.target
                and verification.note_snapshot == by_role["zotero_note_readback"]
                and verification.checks_snapshot == by_role["verification_checks"]
                and len(verification.artifacts) == 3
                and all(
                    expected in verification.artifacts
                    for expected in (
                        by_role["authorization_snapshot"],
                        by_role["zotero_note_readback"],
                        by_role["verification_checks"],
                    )
                )
                and len(checks_by_name) == len(verification.checks)
                and set(checks_by_name) == expected_check_names
                and all(checks_by_name[name].passed == passed for name, passed in actuals.items())
                and isinstance(checks_snapshot, dict)
                and checks_snapshot.get("format") == "paper_reader.verification-checks.v2-internal"
                and checks_snapshot.get("authorization_digest") == sha256_bytes(authorization_raw)
                and checks_snapshot.get("note_key") == verification.note_key
                and checks_snapshot.get("checks")
                == [_plain(check) for check in verification.checks]
                and verification.content_sha256 == actual_sha256
                and verification.content_length == actual_length
                and verification.verified == strong_pass
                and reconciliation.gate == verification.gate
                and verification.gate.status == ("passed" if strong_pass else "blocked")
                and (not verification.gate.blockers if strong_pass else bool(verification.gate.blockers))
                and verification.note_key == reconciliation.matched_note_keys[0]
            )
            valid_outcome = (
                valid_outcome
                and embedded_valid
                and strong_pass == (reconciliation.outcome == "verified")
            )
    if not valid_outcome:
        raise BatchRuntimeError("reconciliation_tampered", "reconciliation outcome/match invariants changed")
    paper_run_raw, paper_run = read_json_bytes(
        paper_run_path,
        code="reconciliation_tampered",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    if not isinstance(paper_run, dict) or paper_run_raw != canonical_json_bytes(paper_run):
        raise BatchRuntimeError("reconciliation_tampered", "paper_reader run is not canonical")
    relative = path.relative_to(paper_run_root).as_posix()
    refs = [
        ref
        for ref in paper_run.get("artifacts", [])
        if isinstance(ref, dict)
        and ref.get("role") == "zotero_reconciliation"
        and ref.get("path") == relative
    ]
    if len(refs) != 1 or refs[0].get("sha256") != sha256_bytes(raw) or refs[0].get("size_bytes") != len(raw):
        raise BatchRuntimeError("reconciliation_not_bound", "paper_reader run does not bind this reconciliation")
    return path, raw, reconciliation, verification


def _reconciliation_result(view: RunView, data: WriteReconciledData) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "writer_id": data.writer_id,
        "claim_id": data.claim_id,
        "write_attempt_id": data.write_attempt_id,
        "attempt_number": data.attempt_number,
        "candidate_sha256": data.candidate_sha256,
        "authorization_sha256": data.authorization_sha256,
        "reconciliation_path": str(
            view.run_dir / "results" / "reconcile" / f"{data.reconciliation_sha256}.json"
        ),
        "reconciliation_sha256": data.reconciliation_sha256,
        "outcome": data.outcome,
        "status": {
            "verified": "written",
            "not_found": "retry_confirmation_required",
            "ambiguous": "blocked",
            "blocked": "blocked",
        }[data.outcome],
    }


def validate_reconciliation_result_payload(
    view: RunView,
    data: WriteReconciledData,
    raw: bytes,
) -> ReconciliationResult:
    try:
        payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BatchRuntimeError("invalid_result", "reconciliation result publication is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"reconciliation result schema must be {RECONCILIATION_SCHEMA_VERSION}",
        )
    try:
        result = ReconciliationResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_result", "reconciliation result failed strict validation") from exc
    if raw != canonical_json_bytes(result) or sha256_bytes(raw) != data.reconciliation_sha256:
        raise BatchRuntimeError("invalid_result", "reconciliation result digest or canonical bytes changed")
    item = _uncertain_item(view, data.item_id)
    if (
        data.writer_id != item.write_last_writer_id
        or data.claim_id != item.write_last_claim_id
        or data.write_attempt_id != item.write_last_attempt_id
        or data.attempt_number != item.write_attempt_count
        or data.lease_token_sha256 != item.write_last_lease_token_sha256
        or data.candidate_sha256 != item.candidate_sha256
        or data.authorization_sha256 != item.authorization_sha256
    ):
        raise BatchRuntimeError("result_identity_mismatch", "reconciliation event differs from uncertain attempt")
    authorization_path = _authorization_path_for_digest(
        view,
        item_id=data.item_id,
        authorization_sha256=data.authorization_sha256,
    )
    _auth_path, authorization_raw, authorization, _candidate = _load_authorization(
        view,
        item_id=data.item_id,
        authorization_path=authorization_path,
        claim_id=data.claim_id,
        write_attempt_id=data.write_attempt_id,
    )
    worker_result = _load_committed_worker_result(view, data.item_id)
    assert worker_result.paper_reader_run is not None
    paper_run_root = normalized_absolute_path(Path(worker_result.paper_reader_run.path)).parent
    readback_path = paper_run_root / "reconciliations" / f"{authorization.authorization_id}.json"
    _path, readback_raw, readback, verification = _load_reconciliation_readback(
        view,
        item_id=data.item_id,
        readback_path=readback_path,
        authorization_raw=authorization_raw,
        authorization=authorization,
    )
    expected_verification: ArtifactRef | None = None
    if readback.verification is not None:
        assert verification is not None
        verification_path = _safe_inner_path(
            paper_run_root,
            readback.verification.path,
            code="reconciliation_tampered",
        )
        verification_raw = read_bytes(
            verification_path,
            code="reconciliation_tampered",
            max_bytes=_ref_read_limit(
                readback.verification.size_bytes,
                code="reconciliation_tampered",
                json_artifact=True,
            ),
        )
        expected_verification = ArtifactRef(
            path=str(verification_path),
            size_bytes=len(verification_raw),
            sha256=sha256_bytes(verification_raw),
            schema_version="paper_reader.verification.v2",
            artifact_id=verification.verification_id,
        )
    if (
        result.manifest_sha256 != view.manifest_sha256
        or result.item_id != data.item_id
        or result.writer_id != data.writer_id
        or result.claim_id != data.claim_id
        or result.write_attempt_id != data.write_attempt_id
        or result.lease_token_sha256 != data.lease_token_sha256
        or result.candidate_sha256 != data.candidate_sha256
        or result.authorization_sha256 != data.authorization_sha256
        or result.readback_sha256 != sha256_bytes(readback_raw)
        or result.parent_key != authorization.target.parent_key
        or result.exact_title != authorization.note_title
        or result.canonical_html_sha256 != authorization.content_sha256
        or result.matched_note_keys != list(readback.matched_note_keys)
        or result.match_count != readback.match_count
        or result.outcome != data.outcome
        or result.outcome != readback.outcome
        or result.verification != expected_verification
        or result.matched_note_key
        != (readback.matched_note_keys[0] if readback.outcome in {"verified", "blocked"} else None)
    ):
        raise BatchRuntimeError("result_identity_mismatch", "batch reconciliation result differs from readback")
    return result


def reconcile_write(
    run_dir: Path,
    item_id: str,
    *,
    readback_path: Path,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, replay_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.reconcile",
    )
    normalized_readback_path = normalized_absolute_path(readback_path)
    if replay_event is not None:
        if not isinstance(replay_event.data, WriteReconciledData):
            raise BatchRuntimeError(
                "journal_corrupt",
                "write reconciliation request is bound to an unexpected event payload",
            )
        durable_path = (
            preflight.run_dir
            / "results"
            / "reconcile"
            / f"{replay_event.data.reconciliation_sha256}.json"
        )
        durable_raw, durable_payload = read_json_bytes(
            durable_path,
            code="journal_corrupt",
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        try:
            durable_result = ReconciliationResult.model_validate(durable_payload)
        except ValidationError as exc:
            raise BatchRuntimeError(
                "journal_corrupt",
                "committed reconciliation result failed strict validation",
            ) from exc
        if (
            durable_raw != canonical_json_bytes(durable_result)
            or sha256_bytes(durable_raw) != replay_event.data.reconciliation_sha256
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                "committed reconciliation result digest or canonical bytes changed",
            )
        input_readback_sha256 = durable_result.readback_sha256
    else:
        input_readback_sha256 = ""
    fingerprint = canonical_sha256(
        {
            "command": "write.reconcile",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "readback_path": str(normalized_readback_path),
            "readback_sha256": input_readback_sha256,
            "now_override": now,
        }
    )

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteReconciledData):
            raise BatchRuntimeError("journal_corrupt", "write reconcile request points to another event type")
        return _reconciliation_result(view, event.data)

    if replay_event is not None:
        if replay_event.request_fingerprint != fingerprint:
            raise BatchRuntimeError(
                "idempotency_conflict",
                "request id is already bound to different reconciliation input",
            )

        def replay_only_proposal(_view: RunView, _transaction_time: str) -> ProposedTransition:
            raise BatchRuntimeError(
                "journal_corrupt",
                "exact write reconciliation replay unexpectedly lost its journal event",
            )

        @contextmanager
        def replay_commit_guard(
            view: RunView,
            event=None,
        ) -> Iterator[Callable[[], None]]:
            if event is None or not isinstance(event.data, WriteReconciledData):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "pending write reconcile request points to another event type",
                )
            data = event.data
            durable_path = (
                view.run_dir
                / "results"
                / "reconcile"
                / f"{data.reconciliation_sha256}.json"
            )
            with _collect_external_closure(code="reconciliation_tampered") as collector:
                result_raw = read_bytes(
                    durable_path,
                    code="journal_corrupt",
                    max_bytes=MAX_JSON_ARTIFACT_BYTES,
                )
                validate_reconciliation_result_payload(view, data, result_raw)
            collector.freeze()
            with _locked_single_run_closure(
                view,
                item_id=item_id,
                authorization_sha256=data.authorization_sha256,
                collector=collector,
                validate=lambda: validate_reconciliation_result_payload(
                    view,
                    data,
                    result_raw,
                ),
            ) as validate:
                yield validate

        return append_transaction(
            run_dir,
            expected_manifest_sha256=preflight.manifest_sha256,
            expected_run_dir_identity=preflight.run_dir_identity,
            request_id=canonical_request_id,
            command="write.reconcile",
            request_fingerprint=fingerprint,
            occurred_at=now,
            propose=replay_only_proposal,
            reconstruct=reconstruct,
            commit_guard=replay_commit_guard,
            fault=fault,
        )

    item = _uncertain_item(preflight, item_id)
    authorization_path = _authorization_path_for_digest(
        preflight,
        item_id=item_id,
        authorization_sha256=item.authorization_sha256,
    )
    _auth_path, authorization_raw, authorization, _candidate = _load_authorization(
        preflight,
        item_id=item_id,
        authorization_path=authorization_path,
        claim_id=item.write_last_claim_id,
        write_attempt_id=item.write_last_attempt_id,
    )
    normalized_readback_path, readback_raw, readback, verification = _load_reconciliation_readback(
        preflight,
        item_id=item_id,
        readback_path=readback_path,
        authorization_raw=authorization_raw,
        authorization=authorization,
    )
    input_readback_sha256 = sha256_bytes(readback_raw)
    fingerprint = canonical_sha256(
        {
            "command": "write.reconcile",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "readback_path": str(normalized_readback_path),
            "readback_sha256": input_readback_sha256,
            "now_override": now,
        }
    )

    proposed_data: WriteReconciledData | None = None
    proposed_result_raw: bytes | None = None
    proposed_collector: _ExternalClosureCollector | None = None

    def propose(view: RunView, _transaction_time: str) -> ProposedTransition:
        nonlocal proposed_data, proposed_result_raw, proposed_collector
        current = _uncertain_item(view, item_id)
        current_auth_path = _authorization_path_for_digest(
            view,
            item_id=item_id,
            authorization_sha256=current.authorization_sha256,
        )
        _current_auth_path, current_auth_raw, current_auth, _candidate_payload = _load_authorization(
            view,
            item_id=item_id,
            authorization_path=current_auth_path,
            claim_id=current.write_last_claim_id,
            write_attempt_id=current.write_last_attempt_id,
        )
        _path, current_readback_raw, current_readback, current_verification = _load_reconciliation_readback(
            view,
            item_id=item_id,
            readback_path=normalized_readback_path,
            authorization_raw=current_auth_raw,
            authorization=current_auth,
        )
        if current_readback_raw != readback_raw:
            raise BatchRuntimeError("reconciliation_tampered", "reconciliation changed before commit")
        expected_verification: ArtifactRef | None = None
        if current_readback.verification is not None:
            assert current_verification is not None
            worker_result = _load_committed_worker_result(view, item_id)
            assert worker_result.paper_reader_run is not None
            paper_run_root = normalized_absolute_path(Path(worker_result.paper_reader_run.path)).parent
            verification_path = _safe_inner_path(
                paper_run_root,
                current_readback.verification.path,
                code="reconciliation_tampered",
            )
            verification_raw = read_bytes(
                verification_path,
                code="reconciliation_tampered",
                max_bytes=_ref_read_limit(
                    current_readback.verification.size_bytes,
                    code="reconciliation_tampered",
                    json_artifact=True,
                ),
            )
            expected_verification = ArtifactRef(
                path=str(verification_path),
                size_bytes=len(verification_raw),
                sha256=sha256_bytes(verification_raw),
                schema_version="paper_reader.verification.v2",
                artifact_id=current_verification.verification_id,
            )
        result = ReconciliationResult(
            schema_version=RECONCILIATION_SCHEMA_VERSION,
            manifest_sha256=view.manifest_sha256,
            item_id=item_id,
            writer_id=current.write_last_writer_id,
            claim_id=current.write_last_claim_id,
            lease_token_sha256=current.write_last_lease_token_sha256,
            write_attempt_id=current.write_last_attempt_id,
            candidate_sha256=current.candidate_sha256,
            authorization_sha256=current.authorization_sha256,
            readback_sha256=sha256_bytes(current_readback_raw),
            parent_key=current_auth.target.parent_key,
            exact_title=current_auth.note_title,
            canonical_html_sha256=current_auth.content_sha256,
            matched_note_keys=list(current_readback.matched_note_keys),
            match_count=current_readback.match_count,
            outcome=current_readback.outcome,
            verification=expected_verification,
            matched_note_key=(
                current_readback.matched_note_keys[0]
                if current_readback.outcome in {"verified", "blocked"}
                else None
            ),
        )
        result_raw = canonical_json_bytes(result)
        result_sha256 = sha256_bytes(result_raw)
        data = WriteReconciledData(
            item_id=item_id,
            writer_id=current.write_last_writer_id,
            claim_id=current.write_last_claim_id,
            write_attempt_id=current.write_last_attempt_id,
            attempt_number=current.write_attempt_count,
            lease_token_sha256=current.write_last_lease_token_sha256,
            candidate_sha256=current.candidate_sha256,
            authorization_sha256=current.authorization_sha256,
            reconciliation_sha256=result_sha256,
            outcome=current_readback.outcome,
        )
        with _collect_external_closure(code="reconciliation_tampered") as collector:
            validate_reconciliation_result_payload(view, data, result_raw)
        collector.freeze()
        proposed_data = data
        proposed_result_raw = result_raw
        proposed_collector = collector
        publication = ResultPublication(
            path=view.run_dir / "results" / "reconcile" / f"{result_sha256}.json",
            content=result_raw,
        )
        return ProposedTransition(
            data=data,
            result=_reconciliation_result(view, data),
            publication=publication,
        )

    def commit_validate(view: RunView) -> None:
        if (
            proposed_data is None
            or proposed_result_raw is None
            or proposed_collector is None
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                "write reconciliation proposal lost its validated result binding",
            )
        validate_reconciliation_result_payload(view, proposed_data, proposed_result_raw)

    @contextmanager
    def commit_guard(
        view: RunView,
        event=None,
    ) -> Iterator[Callable[[], None]]:
        if event is None:
            if (
                proposed_data is None
                or proposed_collector is None
                or proposed_result_raw is None
            ):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "write reconciliation proposal lost its validated event data",
                )
            data = proposed_data
            result_raw = proposed_result_raw
            collector = proposed_collector
            semantic_validate = lambda: commit_validate(view)
        else:
            if not isinstance(event.data, WriteReconciledData):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "pending write reconcile request points to another event type",
                )
            data = event.data
            durable_path = (
                view.run_dir
                / "results"
                / "reconcile"
                / f"{data.reconciliation_sha256}.json"
            )
            with _collect_external_closure(code="reconciliation_tampered") as collector:
                result_raw = read_bytes(
                    durable_path,
                    code="journal_corrupt",
                    max_bytes=MAX_JSON_ARTIFACT_BYTES,
                )
                validate_reconciliation_result_payload(view, data, result_raw)
            collector.freeze()
            semantic_validate = lambda: validate_reconciliation_result_payload(
                view,
                data,
                result_raw,
            )
        with _locked_single_run_closure(
            view,
            item_id=item_id,
            authorization_sha256=data.authorization_sha256,
            collector=collector,
            validate=semantic_validate,
        ) as validate:
            yield validate

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.reconcile",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        commit_validate=commit_validate,
        commit_guard=commit_guard,
        fault=fault,
    )


def _retry_result(view: RunView, data: WriteRetriedData) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "status": "queued",
        "previous_writer_id": data.previous_writer_id,
        "previous_claim_id": data.previous_claim_id,
        "previous_write_attempt_id": data.previous_write_attempt_id,
        "previous_attempt_number": data.previous_attempt_number,
        "candidate_sha256": data.candidate_sha256,
        "authorization_sha256": data.authorization_sha256,
        "reconciliation_sha256": data.reconciliation_sha256,
        "acknowledged_no_match": data.acknowledged_no_match,
        "next_write_attempt_id": data.next_write_attempt_id,
        "next_attempt_number": data.next_attempt_number,
    }


def retry_write(
    run_dir: Path,
    item_id: str,
    *,
    acknowledge_no_match: bool,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="write.retry",
    )
    if acknowledge_no_match is not True:
        _raise_input_error(
            existing_event,
            "acknowledgement_required",
            "write retry requires --acknowledge-no-match",
        )
    fingerprint = canonical_sha256(
        {
            "command": "write.retry",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "acknowledge_no_match": True,
            "now_override": now,
        }
    )

    def propose(view: RunView, _transaction_time: str) -> ProposedTransition:
        item = next((entry for entry in view.state.items if entry.item_id == item_id), None)
        if item is None:
            raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
        if item.write_status != "retry_confirmation_required" or item.write_lease is not None:
            raise BatchRuntimeError("retry_not_allowed", "write retry requires a zero-match reconciliation")
        if not all(
            [
                item.write_last_writer_id,
                item.write_last_claim_id,
                item.write_last_attempt_id,
                item.write_last_lease_token_sha256,
                item.candidate_sha256,
                item.authorization_sha256,
                item.authorization_nonce_sha256,
                item.external_claim_id,
                item.reconciliation_sha256,
            ]
        ):
            raise BatchRuntimeError("journal_corrupt", "retryable write state is missing durable attempt identities")
        next_write_attempt_id = str(uuid4())
        data = WriteRetriedData(
            item_id=item_id,
            previous_writer_id=item.write_last_writer_id,
            previous_claim_id=item.write_last_claim_id,
            previous_write_attempt_id=item.write_last_attempt_id,
            previous_attempt_number=item.write_attempt_count,
            previous_lease_token_sha256=item.write_last_lease_token_sha256,
            candidate_sha256=item.candidate_sha256,
            authorization_sha256=item.authorization_sha256,
            previous_authorization_nonce_sha256=item.authorization_nonce_sha256,
            previous_external_claim_id=item.external_claim_id,
            reconciliation_sha256=item.reconciliation_sha256,
            acknowledged_no_match=True,
            next_write_attempt_id=next_write_attempt_id,
            next_attempt_number=item.write_attempt_count + 1,
        )
        return ProposedTransition(data=data, result=_retry_result(view, data))

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, WriteRetriedData):
            raise BatchRuntimeError("journal_corrupt", "write retry request points to another event type")
        return _retry_result(view, event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="write.retry",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


__all__ = [
    "begin_write",
    "claim_write",
    "commit_write",
    "mark_write_uncertain",
    "preview_write",
    "reconcile_write",
    "release_write",
    "renew_write",
    "retry_write",
    "validate_reconciliation_result_payload",
    "validate_write_started_artifacts",
    "validate_write_result_payload",
]
