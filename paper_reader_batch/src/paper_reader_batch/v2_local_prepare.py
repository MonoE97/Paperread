from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from paper_reader_batch.v2_artifacts import (
    local_prepare_result_artifact_commit_guard,
    paper_reader_root_identity,
    validate_local_prepare_result_artifacts,
)
from paper_reader_batch.v2_contracts import (
    LOCAL_PREPARE_RESULT_SCHEMA_VERSION,
    LOCAL_PREPARE_COORDINATION_UUID_NAME,
    ArtifactRef,
    ClaimedData,
    ClaimAssignment,
    FinishedData,
    FileIdentity,
    LeaseMutationData,
    LocalPrepareCoordinationReservedData,
    LocalPrepareResult,
    PdfManifestItem,
    PdfSource,
    SkillRootIdentity,
    StateItem,
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
    locked_run,
)
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    active_transition_targets,
    canonical_json_bytes,
    canonical_sha256,
    ensure_directory,
    entry_exists,
    entry_exists_allow_missing_parent,
    locked_file,
    normalized_absolute_path,
    open_directory_fd,
    publish_bytes_no_replace,
    read_bytes,
    read_active_transition_owner,
    read_committed_transitions,
    read_relative_bytes,
    read_locked_bytes,
    read_pending_swap,
    read_json_bytes,
    replace_bytes_atomic,
    sha256_bytes,
    utc_now,
)
from paper_reader_batch.v2_manifest import validate_pdf_source
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome, validate_request_id
from paper_reader_batch.v2_worker import DEFAULT_LEASE_SECONDS, MAX_LEASE_SECONDS, derive_lease_token


COORDINATION_SCHEMA_VERSION = "paper_reader_batch.local-prepare-coordination.v2-internal"
ATTEMPT_OWNER_SCHEMA_VERSION = "paper_reader_batch.local-prepare-attempt-owner.v2-internal"
CHILD_COMMAND_RESULT_SCHEMA_VERSION = "paper_reader.command-result.v2"
CHILD_STARTED_SCHEMA_VERSION = "paper_reader_batch.local-prepare-child-started.v2-internal"
MAX_CHILD_STDOUT_BYTES = 1024 * 1024
DEFAULT_CHILD_TIMEOUT_SECONDS = 600
INIT_CHILD_TIMEOUT_SECONDS = 60
COMMIT_BUFFER_SECONDS = 60
CLAIM_TO_RUN_COORDINATION_MARGIN_SECONDS = 60
MAX_CHILD_TIMEOUT_SECONDS = (
    MAX_LEASE_SECONDS
    - INIT_CHILD_TIMEOUT_SECONDS
    - COMMIT_BUFFER_SECONDS
    - CLAIM_TO_RUN_COORDINATION_MARGIN_SECONDS
)


def _raise_input_error(existing_event, code: str, message: str) -> None:
    if existing_event is not None:
        raise BatchRuntimeError(
            "idempotency_conflict",
            "request id is already bound to different command input",
        )
    raise BatchRuntimeError(code, message)


class _InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _AttemptOwner(_InternalModel):
    schema_version: Literal["paper_reader_batch.local-prepare-attempt-owner.v2-internal"]
    request_id: str
    request_fingerprint: str
    manifest_sha256: str
    item_id: str
    claim_id: str
    attempt_id: str
    request_dir_device: int = Field(ge=0)
    request_dir_inode: int = Field(gt=0)
    hmac_sha256: str


class _CoordinationRecord(_InternalModel):
    schema_version: Literal["paper_reader_batch.local-prepare-coordination.v2-internal"]
    request_id: str
    request_fingerprint: str
    manifest_sha256: str
    item_id: str
    worker_id: str
    claim_id: str
    attempt_id: str
    attempt_number: int
    lease_token_sha256: str
    request_dir_device: int = Field(ge=0)
    request_dir_inode: int = Field(gt=0)
    source: PdfSource
    paper_reader_root: SkillRootIdentity
    timeout_seconds: int
    stage: Literal["reserved", "initialized", "result_ready", "finished"]
    init_invoked: bool = False
    init_execution_released: bool = False
    init_argv: list[str]
    init_stdout_sha256: str | None = None
    paper_reader_run_dir: str | None = None
    paper_reader_run_id: str | None = None
    local_target_path: str | None = None
    prepare_invoked: bool = False
    prepare_execution_released: bool = False
    prepare_argv: list[str] | None = None
    prepare_stdout_sha256: str | None = None
    evidence_dir: str | None = None
    evidence_id: str | None = None
    evidence_digest: str | None = None
    result_sha256: str | None = None
    hmac_sha256: str

    @model_validator(mode="after")
    def validate_progress(self) -> "_CoordinationRecord":
        init_values = (
            self.init_stdout_sha256,
            self.paper_reader_run_dir,
            self.paper_reader_run_id,
            self.local_target_path,
        )
        prepare_values = (
            self.prepare_stdout_sha256,
            self.evidence_dir,
            self.evidence_id,
            self.evidence_digest,
        )
        if self.stage == "reserved" and any(value is not None for value in (*init_values, *prepare_values)):
            raise ValueError("reserved coordination record cannot bind completed child output")
        if self.stage == "initialized":
            if not all(value is not None for value in init_values) or any(
                value is not None for value in prepare_values
            ):
                raise ValueError("initialized coordination record has inconsistent child identities")
        if self.stage in {"result_ready", "finished"} and self.result_sha256 is None:
            raise ValueError("terminal coordination stage requires a strict result digest")
        if self.stage in {"reserved", "initialized"} and self.result_sha256 is not None:
            raise ValueError("nonterminal coordination stage cannot bind a result")
        if self.prepare_invoked and self.stage == "reserved":
            raise ValueError("prepare cannot be invoked before init is accepted")
        if self.init_execution_released and not self.init_invoked:
            raise ValueError("init execution release requires an invocation reservation")
        if self.prepare_execution_released and not self.prepare_invoked:
            raise ValueError("prepare execution release requires an invocation reservation")
        return self


class _ChildStarted(_InternalModel):
    schema_version: Literal["paper_reader_batch.local-prepare-child-started.v2-internal"]
    request_id: str
    request_fingerprint: str
    manifest_sha256: str
    item_id: str
    worker_id: str
    claim_id: str
    attempt_id: str
    attempt_number: int
    lease_token_sha256: str
    request_dir_device: int = Field(ge=0)
    request_dir_inode: int = Field(gt=0)
    step: Literal["init", "prepare"]
    argv: list[str]
    hmac_sha256: str


class _ChildCommandResult(_InternalModel):
    schema_version: Literal["paper_reader.command-result.v2"]
    command: str
    ok: bool
    code: str = Field(min_length=1)
    created_at: str
    message: str | None = None
    data: dict[str, JsonValue]


@dataclass(frozen=True)
class _ChildInvocation:
    started_path: Path
    stdout_path: Path
    started_payload: bytes
    request_dir_device: int
    request_dir_inode: int
    run_lock_descriptors: tuple[int, ...]
    launcher_fault_stage: str | None = None
    started_callback: Callable[[], None] | None = None

    def mark_started(self) -> None:
        publish_bytes_no_replace(
            self.started_path,
            self.started_payload,
            allow_existing_exact=True,
        )
        if self.started_callback is not None:
            self.started_callback()

    def write_stdout(self, payload: bytes) -> None:
        publish_bytes_no_replace(
            self.stdout_path,
            payload,
            allow_existing_exact=True,
        )


@dataclass
class _RunningChild:
    process: subprocess.Popen[bytes]
    deadline: float
    returncode_reliable: bool = True

    def wait(self) -> int | None:
        remaining = max(0.001, self.deadline - time.monotonic())
        try:
            returncode = self.process.wait(timeout=remaining)
            return returncode if self.returncode_reliable else None
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            raise _ChildProtocolError(
                "child_timeout",
                "paper_reader child command exceeded its timeout",
            ) from exc


ChildRunner = Callable[
    [tuple[str, ...], Path, int, _ChildInvocation],
    int | _RunningChild,
]

_COORDINATION_TRANSITION_TARGETS = frozenset(
    {"record.json", "init.started", "prepare.started"}
)


class _ChildProtocolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _coordination_hmac(secret: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(secret, canonical_json_bytes(payload), "sha256").hexdigest()


def _signed_model_bytes(model_type, payload: dict[str, Any], secret: bytes) -> tuple[Any, bytes]:
    supplied_unsigned = dict(payload)
    supplied_unsigned.pop("hmac_sha256", None)
    try:
        normalized = model_type.model_validate(
            {**supplied_unsigned, "hmac_sha256": "0" * 64}
        )
        unsigned = normalized.model_dump(mode="json", exclude={"hmac_sha256"})
        signed = {**unsigned, "hmac_sha256": _coordination_hmac(secret, unsigned)}
        model = model_type.model_validate(signed)
    except ValidationError as exc:
        raise BatchRuntimeError("coordination_corrupt", "coordination record failed strict validation") from exc
    return model, canonical_json_bytes(model)


def _load_signed_model(
    path: Path,
    model_type,
    secret: bytes,
    *,
    max_bytes: int | None = None,
    recover_pending: bool = False,
):
    transition_targets = (
        _COORDINATION_TRANSITION_TARGETS
        if model_type in {_CoordinationRecord, _ChildStarted}
        else frozenset({path.name})
    )
    if path.name not in transition_targets:
        raise BatchRuntimeError(
            "coordination_corrupt",
            f"signed coordination artifact is outside its legal transition set: {path}",
        )

    def parse_current(raw: bytes) -> _CoordinationRecord:
        try:
            payload = json.loads(
                raw,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            model = _CoordinationRecord.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            raise BatchRuntimeError("coordination_corrupt", "pending coordination transition is invalid") from exc
        unsigned = model.model_dump(mode="json", exclude={"hmac_sha256"})
        if (
            raw != canonical_json_bytes(model)
            or not hmac.compare_digest(model.hmac_sha256, _coordination_hmac(secret, unsigned))
        ):
            raise BatchRuntimeError("coordination_corrupt", "pending coordination transition HMAC is invalid")
        return model

    active = active_transition_targets(
        path.parent,
        replace_targets=transition_targets,
    )
    pending_swap = read_pending_swap(
        path,
        max_bytes=(MAX_CHILD_STDOUT_BYTES if max_bytes is None else max_bytes),
        replace_targets=transition_targets,
    )
    if pending_swap is not None:
        if model_type is not _CoordinationRecord or not recover_pending:
            raise BatchRuntimeError(
                "storage_recovery_required",
                f"signed coordination swap requires its coordinator lock: {path}",
            )
        public_raw, slot_raw = pending_swap

        public_model = parse_current(public_raw)
        slot_model = parse_current(slot_raw)

        def advances(older: _CoordinationRecord, newer: _CoordinationRecord) -> bool:
            old = older.model_dump(mode="json", exclude={"hmac_sha256"})
            new = newer.model_dump(mode="json", exclude={"hmac_sha256"})
            rank = {"reserved": 0, "initialized": 1, "result_ready": 2, "finished": 3}
            if rank[newer.stage] < rank[older.stage]:
                return False
            mutable = {
                "stage",
                "init_invoked",
                "init_execution_released",
                "init_stdout_sha256",
                "paper_reader_run_dir",
                "paper_reader_run_id",
                "local_target_path",
                "prepare_invoked",
                "prepare_execution_released",
                "prepare_argv",
                "prepare_stdout_sha256",
                "evidence_dir",
                "evidence_id",
                "evidence_digest",
                "result_sha256",
            }
            if any(old[key] != new[key] for key in old.keys() - mutable):
                return False
            for key in mutable - {"stage"}:
                previous = old[key]
                current = new[key]
                if isinstance(previous, bool):
                    if previous and not current:
                        return False
                elif previous is not None and previous != current:
                    return False
            return old != new

        if advances(public_model, slot_model):
            replace_bytes_atomic(
                path,
                slot_raw,
                expected_current=public_raw,
                transition_id=(
                    f"coordination:{slot_model.request_id}:{slot_model.attempt_id}:"
                    f"{sha256_bytes(slot_raw)}"
                ),
                allowed_transition_targets=transition_targets,
            )
        else:
            raise BatchRuntimeError(
                "coordination_corrupt",
                "pending coordination swap is not one monotonic signed transition",
            )
    elif path.name in active:
        if model_type is not _CoordinationRecord or not recover_pending:
            raise BatchRuntimeError(
                "storage_recovery_required",
                f"signed coordination transition requires its coordinator lock: {path}",
            )
        committed = read_committed_transitions(
            path,
            max_bytes=(MAX_CHILD_STDOUT_BYTES if max_bytes is None else max_bytes),
            replace_targets=transition_targets,
        )
        if committed:
            public_raw, retired_raw, _transition_name = committed[0]
            public_model = parse_current(public_raw)
            retired_model = parse_current(retired_raw)
            owner_raw = read_active_transition_owner(
                path,
                replace_targets=transition_targets,
            )
            if owner_raw is None:
                raise BatchRuntimeError("coordination_corrupt", "committed coordination transition has no owner")
            owner = json.loads(owner_raw)
            replace_bytes_atomic(
                path,
                public_raw,
                expected_current=retired_raw,
                transition_id=owner["transition_id"],
                allowed_transition_targets=transition_targets,
            )
    raw, payload = read_json_bytes(
        path,
        code="coordination_corrupt",
        max_bytes=max_bytes,
    )
    if not isinstance(payload, dict):
        raise BatchRuntimeError("coordination_corrupt", f"coordination record must be an object: {path}")
    signature = payload.get("hmac_sha256")
    unsigned = dict(payload)
    unsigned.pop("hmac_sha256", None)
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature,
        _coordination_hmac(secret, unsigned),
    ):
        raise BatchRuntimeError("coordination_corrupt", f"coordination HMAC is invalid: {path}")
    release_fields = frozenset({"init_execution_released", "prepare_execution_released"})
    if model_type is _CoordinationRecord and release_fields.difference(payload):
        raise BatchRuntimeError(
            "coordination_corrupt",
            f"coordination record lacks an execution-release identity: {path}",
        )
    try:
        model = model_type.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("coordination_corrupt", f"coordination record is invalid: {path}") from exc
    if raw != canonical_json_bytes(model):
        raise BatchRuntimeError("coordination_corrupt", f"coordination record is not canonical JSON: {path}")
    return model, raw


def _request_directory_identity(path: Path) -> tuple[int, int]:
    with open_directory_fd(path, create=False) as (descriptor, _normalized):
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino


def _assert_request_directory_identity(
    path: Path,
    *,
    device: int,
    inode: int,
) -> None:
    try:
        actual = _request_directory_identity(path)
    except BatchRuntimeError as exc:
        raise BatchRuntimeError(
            "coordination_corrupt",
            "owned local prepare request directory disappeared or became unsafe",
        ) from exc
    if actual != (device, inode):
        raise BatchRuntimeError(
            "coordination_corrupt",
            "owned local prepare request directory identity changed",
        )


def _replace_coordination_record(
    path: Path,
    current: _CoordinationRecord,
    current_raw: bytes,
    secret: bytes,
    **updates: Any,
) -> tuple[_CoordinationRecord, bytes]:
    payload = current.model_dump(mode="json", exclude={"hmac_sha256"})
    payload.update(updates)
    updated, raw = _signed_model_bytes(_CoordinationRecord, payload, secret)
    if raw == current_raw:
        return updated, raw
    replace_bytes_atomic(
        path,
        raw,
        expected_current=current_raw,
        transition_id=(
            f"coordination:{updated.request_id}:{updated.attempt_id}:{sha256_bytes(raw)}"
        ),
        allowed_transition_targets=_COORDINATION_TRANSITION_TARGETS,
    )
    return updated, raw


def _child_started_bytes(
    record: _CoordinationRecord,
    *,
    step: Literal["init", "prepare"],
    secret: bytes,
) -> tuple[_ChildStarted, bytes]:
    stored_argv = record.init_argv if step == "init" else record.prepare_argv
    if stored_argv is None:
        raise BatchRuntimeError("coordination_corrupt", f"{step} argv is not bound")
    return _signed_model_bytes(
        _ChildStarted,
        {
            "schema_version": CHILD_STARTED_SCHEMA_VERSION,
            "request_id": record.request_id,
            "request_fingerprint": record.request_fingerprint,
            "manifest_sha256": record.manifest_sha256,
            "item_id": record.item_id,
            "worker_id": record.worker_id,
            "claim_id": record.claim_id,
            "attempt_id": record.attempt_id,
            "attempt_number": record.attempt_number,
            "lease_token_sha256": record.lease_token_sha256,
            "request_dir_device": record.request_dir_device,
            "request_dir_inode": record.request_dir_inode,
            "step": step,
            "argv": stored_argv,
        },
        secret,
    )


def _load_exact_child_started(
    path: Path,
    record: _CoordinationRecord,
    *,
    step: Literal["init", "prepare"],
    secret: bytes,
) -> _ChildStarted | None:
    if not entry_exists_allow_missing_parent(path):
        return None
    expected, expected_raw = _child_started_bytes(record, step=step, secret=secret)
    actual, _actual_raw = _load_signed_model(
        path,
        _ChildStarted,
        secret,
        max_bytes=len(expected_raw),
    )
    if actual != expected:
        raise BatchRuntimeError(
            "coordination_corrupt",
            f"{step} child-start marker differs from the exact attempt record",
        )
    return actual


_CHILD_LAUNCHER = r"""
import ctypes
import errno
import fcntl
import os
import secrets
import signal
import stat
import subprocess
import sys

ack_fd = int(sys.argv[1])
if sys.argv[2] != "commit-v1":
    raise SystemExit(119)
decision_fd = int(sys.argv[3])
run_lock_fds = [int(value) for value in sys.argv[4].split(",") if value]
request_dir_device = int(sys.argv[5])
request_dir_inode = int(sys.argv[6])
started_path = sys.argv[7]
stdout_path = sys.argv[8]
started_payload = bytes.fromhex(sys.argv[9])
timeout_seconds = int(sys.argv[10])
fault_stage = sys.argv[11]
target_argv = sys.argv[12:]

parent_path = os.path.dirname(started_path)
if parent_path != os.path.dirname(stdout_path) or not target_argv:
    raise SystemExit(120)
directory_fd = os.open(parent_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
directory_metadata = os.fstat(directory_fd)
if (directory_metadata.st_dev, directory_metadata.st_ino) != (request_dir_device, request_dir_inode):
    raise SystemExit(127)

def require_empty_regular(descriptor):
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != 0:
        raise SystemExit(121)
    return descriptor

def open_stdout(name):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    return require_empty_regular(descriptor)

def open_empty(name):
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    return require_empty_regular(descriptor)

def rename_no_replace(source_name, target_name):
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flags = 0x00000004
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        flags = 0x00000001
    else:
        raise SystemExit(128)
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(directory_fd, source, directory_fd, target, flags) != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise SystemExit(122)
        raise SystemExit(128)

stdout_fd = open_stdout(os.path.basename(stdout_path))
fcntl.flock(stdout_fd, fcntl.LOCK_EX)
gate_read, gate_write = os.pipe()
ownership_read, ownership_write = os.pipe()
executor_pid = os.fork()
if executor_pid == 0:
    os.close(ownership_read)
    os.close(gate_write)
    os.close(ack_fd)
    os.close(decision_fd)
    os.write(ownership_write, b"1")
    os.close(ownership_write)
    gate_value = os.read(gate_read, 1)
    os.close(gate_read)
    try:
        marker_fd = os.open(
            os.path.basename(started_path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        marker_raw = b""
    else:
        marker_metadata = os.fstat(marker_fd)
        if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink != 1:
            raise SystemExit(124)
        marker_chunks = []
        marker_size = 0
        marker_limit = len(started_payload)
        while marker_size <= marker_limit:
            marker_chunk = os.read(
                marker_fd,
                min(1024 * 1024, marker_limit - marker_size + 1),
            )
            if not marker_chunk:
                break
            marker_size += len(marker_chunk)
            if marker_size > marker_limit:
                raise SystemExit(125)
            marker_chunks.append(marker_chunk)
        marker_metadata_after = os.fstat(marker_fd)
        try:
            named_marker_metadata = os.stat(
                os.path.basename(started_path),
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError:
            raise SystemExit(124)
        marker_identity = lambda metadata: (
            stat.S_ISREG(metadata.st_mode),
            metadata.st_nlink,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        expected_marker_identity = marker_identity(marker_metadata)
        if (
            expected_marker_identity[0] is not True
            or expected_marker_identity[1] != 1
            or marker_identity(marker_metadata_after) != expected_marker_identity
            or marker_identity(named_marker_metadata) != expected_marker_identity
        ):
            raise SystemExit(124)
        marker_raw = b"".join(marker_chunks)
    if marker_raw != started_payload:
        raise SystemExit(125)
    # EOF is an intentional recovery signal: if the supervisor died after the
    # atomic marker publish, this already-forked executor still runs exactly
    # the bound argv. Without that marker it exits above and performs no work.
    if gate_value not in {b"", b"1"}:
        raise SystemExit(126)
    # Keep the opened marker and its containing directory bound through the
    # exact Popen handoff. Re-read both the held inode and its no-follow name
    # immediately before release so an unlink/replacement after the earlier
    # classification cannot authorize the target.
    final_marker_metadata = os.fstat(marker_fd)
    try:
        final_named_marker_metadata = os.stat(
            os.path.basename(started_path),
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError:
        raise SystemExit(124)
    os.lseek(marker_fd, 0, os.SEEK_SET)
    final_marker_chunks = []
    final_marker_size = 0
    final_marker_limit = len(started_payload)
    while final_marker_size <= final_marker_limit:
        final_marker_chunk = os.read(
            marker_fd,
            min(1024 * 1024, final_marker_limit - final_marker_size + 1),
        )
        if not final_marker_chunk:
            break
        final_marker_size += len(final_marker_chunk)
        if final_marker_size > final_marker_limit:
            raise SystemExit(125)
        final_marker_chunks.append(final_marker_chunk)
    final_marker_metadata_after = os.fstat(marker_fd)
    if (
        marker_identity(final_marker_metadata) != expected_marker_identity
        or marker_identity(final_marker_metadata_after) != expected_marker_identity
        or marker_identity(final_named_marker_metadata) != expected_marker_identity
        or b"".join(final_marker_chunks) != started_payload
    ):
        raise SystemExit(124)
    child = subprocess.Popen(
        target_argv,
        stdin=subprocess.DEVNULL,
        stdout=stdout_fd,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        start_new_session=True,
    )
    for run_lock_fd in run_lock_fds:
        os.close(run_lock_fd)
    os.close(marker_fd)
    os.close(directory_fd)
    try:
        returncode = child.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        raise SystemExit(124)
    finally:
        # A target may exit after spawning a descendant that inherited stdout.
        # Kill the exact child process group before releasing the stage flock.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise SystemExit(returncode if 0 <= returncode <= 255 else 1)

os.close(ownership_write)
ownership = os.read(ownership_read, 1)
os.close(ownership_read)
if ownership != b"1":
    os.close(ack_fd)
    os.close(decision_fd)
    os.close(gate_read)
    os.close(gate_write)
    for run_lock_fd in run_lock_fds:
        os.close(run_lock_fd)
    os.close(directory_fd)
    os.close(stdout_fd)
    os.waitpid(executor_pid, 0)
    raise SystemExit(117)

os.write(ack_fd, b"R")
decision = os.read(decision_fd, 1)
os.close(decision_fd)
if decision != b"1":
    os.close(ack_fd)
    os.close(gate_read)
    os.close(gate_write)
    for run_lock_fd in run_lock_fds:
        os.close(run_lock_fd)
    os.close(directory_fd)
    os.close(stdout_fd)
    os.waitpid(executor_pid, 0)
    raise SystemExit(118)

if fault_stage == "supervisor_before_marker":
    os._exit(97)
writing_name = (
    "."
    + os.path.basename(started_path)
    + "."
    + secrets.token_hex(16)
    + ".writing"
)
writing_fd = open_empty(writing_name)
offset = 0
while offset < len(started_payload):
    written = os.write(writing_fd, started_payload[offset:])
    if written <= 0:
        raise SystemExit(123)
    offset += written
os.fsync(writing_fd)
rename_no_replace(
    writing_name,
    os.path.basename(started_path),
)
os.fsync(directory_fd)
os.close(writing_fd)
if fault_stage == "supervisor_after_marker":
    os._exit(98)
os.close(gate_read)
os.write(gate_write, b"1")
os.close(gate_write)
try:
    os.write(ack_fd, b"S")
except BrokenPipeError:
    pass
os.close(ack_fd)
for run_lock_fd in run_lock_fds:
    os.close(run_lock_fd)
os.close(directory_fd)
_, executor_status = os.waitpid(executor_pid, 0)
if os.WIFEXITED(executor_status):
    raise SystemExit(os.WEXITSTATUS(executor_status))
if os.WIFSIGNALED(executor_status):
    raise SystemExit(128 + os.WTERMSIG(executor_status))
raise SystemExit(127)
"""


def _default_child_runner(
    argv: tuple[str, ...],
    cwd: Path,
    timeout_seconds: int,
    invocation: _ChildInvocation,
) -> _RunningChild:
    request_dir = normalized_absolute_path(invocation.started_path.parent)
    if normalized_absolute_path(invocation.stdout_path.parent) != request_dir:
        raise _ChildProtocolError(
            "coordination_corrupt",
            "child start marker and stdout must share one request directory",
        )
    with open_directory_fd(request_dir, create=False) as (descriptor, bound_request_dir):
        metadata = os.fstat(descriptor)
        if (
            bound_request_dir != request_dir
            or (metadata.st_dev, metadata.st_ino)
            != (invocation.request_dir_device, invocation.request_dir_inode)
        ):
            raise _ChildProtocolError(
                "coordination_corrupt",
                "paper_reader child request directory differs from the bound attempt",
            )
        held_request_descriptor = os.dup(descriptor)
    try:
        return _default_child_runner_anchored(
            argv,
            cwd,
            timeout_seconds,
            invocation,
            held_request_descriptor,
        )
    finally:
        os.close(held_request_descriptor)


def _read_held_started_marker(
    request_dir_descriptor: int,
    invocation: _ChildInvocation,
) -> bytes | None:
    try:
        os.stat(
            invocation.started_path.name,
            dir_fd=request_dir_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _ChildProtocolError(
            "coordination_uncertain",
            "paper_reader child start marker cannot be classified through its held request directory",
        ) from exc
    try:
        return read_relative_bytes(
            request_dir_descriptor,
            invocation.started_path.name,
            code="coordination_marker_invalid",
            max_bytes=len(invocation.started_payload),
        )
    except BatchRuntimeError as exc:
        raise _ChildProtocolError(
            "coordination_uncertain",
            "paper_reader child start marker cannot be classified through its held request directory",
        ) from exc


def _default_child_runner_anchored(
    argv: tuple[str, ...],
    cwd: Path,
    timeout_seconds: int,
    invocation: _ChildInvocation,
    request_dir_descriptor: int,
) -> _RunningChild:
    read_ack, write_ack = os.pipe()
    read_decision, write_decision = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                _CHILD_LAUNCHER,
                str(write_ack),
                "commit-v1",
                str(read_decision),
                ",".join(str(descriptor) for descriptor in invocation.run_lock_descriptors),
                str(invocation.request_dir_device),
                str(invocation.request_dir_inode),
                str(invocation.started_path),
                str(invocation.stdout_path),
                invocation.started_payload.hex(),
                str(timeout_seconds),
                invocation.launcher_fault_stage or "none",
                *argv,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            pass_fds=(write_ack, read_decision, *invocation.run_lock_descriptors),
            start_new_session=True,
        )
    except OSError as exc:
        os.close(read_ack)
        os.close(write_decision)
        raise _ChildProtocolError("child_execution_failed", "paper_reader child command could not start") from exc
    finally:
        os.close(write_ack)
        os.close(read_decision)
    ready, _writable, _errors = select.select(
        [read_ack],
        [],
        [],
        min(10.0, float(timeout_seconds)),
    )
    acknowledgement = os.read(read_ack, 1) if ready else b""
    assert process is not None
    if acknowledgement == b"R":
        try:
            os.write(write_decision, b"1")
        except BrokenPipeError:
            acknowledgement = b""
        finally:
            os.close(write_decision)
        if acknowledgement == b"R":
            started_ready, _writable, _errors = select.select(
                [read_ack],
                [],
                [],
                float(COMMIT_BUFFER_SECONDS),
            )
            started_acknowledgement = os.read(read_ack, 1) if started_ready else b""
            os.close(read_ack)
            if started_acknowledgement == b"S":
                return _RunningChild(
                    process=process,
                    deadline=time.monotonic() + timeout_seconds + 10,
                    # The process is the supervisor, not the target. After the
                    # durable-start ACK a supervisor crash must not override
                    # the exact CLI envelope produced by its owned executor.
                    returncode_reliable=False,
                )

            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            marker_raw = _read_held_started_marker(request_dir_descriptor, invocation)
            if marker_raw == invocation.started_payload:
                return _RunningChild(
                    process=process,
                    deadline=time.monotonic() + timeout_seconds + 10,
                    returncode_reliable=False,
                )
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise _ChildProtocolError(
                "child_execution_failed",
                "paper_reader child launcher did not durably publish its start marker",
            )
        os.close(read_ack)
    else:
        os.close(read_ack)
        try:
            os.write(write_decision, b"0")
        except BrokenPipeError:
            pass
        finally:
            os.close(write_decision)

    if acknowledgement != b"R":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        marker_raw = _read_held_started_marker(request_dir_descriptor, invocation)
        if marker_raw == invocation.started_payload:
            return _RunningChild(
                process=process,
                deadline=time.monotonic() + timeout_seconds + 10,
                returncode_reliable=False,
            )
        raise _ChildProtocolError(
            "child_execution_failed",
            "paper_reader child launcher did not durably start the command",
        )
    raise _ChildProtocolError(
        "child_execution_failed",
        "paper_reader child launcher did not accept the launch decision",
    )


def _read_stdout_stage(path: Path) -> bytes | None:
    if not entry_exists(path):
        return None
    with locked_file(path, create=False) as descriptor:
        return read_locked_bytes(descriptor)


def _parse_child_envelope(
    raw: bytes,
    *,
    expected_command: str,
    returncode: int | None,
) -> _ChildCommandResult:
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise _ChildProtocolError(
            "invalid_child_envelope",
            "paper_reader child stdout must contain exactly one JSON object",
        )
    try:
        payload = json.loads(
            lines[0],
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _ChildProtocolError("invalid_child_envelope", "paper_reader child stdout is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != CHILD_COMMAND_RESULT_SCHEMA_VERSION:
        raise _ChildProtocolError(
            "invalid_child_envelope",
            "paper_reader child returned an unsupported command-result schema",
        )
    try:
        envelope = _ChildCommandResult.model_validate(payload)
    except ValidationError as exc:
        raise _ChildProtocolError(
            "invalid_child_envelope",
            "paper_reader child command-result failed strict validation",
        ) from exc
    expected_raw = canonical_json_bytes(envelope) + b"\n"
    if raw != expected_raw:
        raise _ChildProtocolError(
            "invalid_child_envelope",
            "paper_reader child stdout must be one canonical JSON line",
        )
    try:
        _parse_utc(envelope.created_at)
    except BatchRuntimeError as exc:
        raise _ChildProtocolError("invalid_child_envelope", "child created_at is not RFC3339 UTC") from exc
    if envelope.command != expected_command:
        raise _ChildProtocolError(
            "invalid_child_envelope",
            f"paper_reader child command identity differs from {expected_command}",
        )
    if returncode is not None and ((returncode == 0) != envelope.ok):
        raise _ChildProtocolError(
            "child_exit_mismatch",
            "paper_reader child exit status disagrees with its command-result envelope",
        )
    return envelope


def _active_count(view: RunView) -> int:
    return sum(
        1
        for item in view.state.items
        if item.worker_status == "claimed" or item.local_prepare_status == "claimed"
    )


def _is_claimable_local_prepare_item(item: StateItem) -> bool:
    if item.local_prepare_status not in {"queued", "failed", "blocked"}:
        return False
    # This blocker means the previous child may already have crossed its
    # external side-effect boundary. Only recovery of that exact attempt is
    # safe; allocating a fresh attempt could execute the child twice.
    return not (
        item.local_prepare_status == "blocked"
        and item.local_prepare_failure_code == "coordination_uncertain"
    )


def _claim_result(view: RunView, data: ClaimedData) -> dict[str, Any]:
    manifest_by_id = {item.item_id: item for item in view.manifest.items}
    assignments = []
    for assignment in data.assignments:
        manifest_item = manifest_by_id[assignment.item_id]
        assignments.append(
            {
                "item_id": assignment.item_id,
                "input_type": manifest_item.input_type,
                "expected_output": manifest_item.expected_output,
                "worker_id": assignment.actor_id,
                "claim_id": assignment.claim_id,
                "attempt_id": assignment.attempt_id,
                "attempt_number": assignment.attempt_number,
                "lease_token": derive_lease_token(
                    view.lease_secret,
                    lane="local_prepare",
                    claim_id=assignment.claim_id,
                    attempt_id=assignment.attempt_id,
                ),
                "issued_at": assignment.issued_at,
                "expires_at": assignment.expires_at,
                "source": manifest_item.source.model_dump(mode="json"),
            }
        )
    return {"assignments": assignments}


def _validate_local_pdf_source(view: RunView, item_id: str) -> PdfManifestItem:
    manifest_item = next(
        (item for item in view.manifest.items if item.item_id == item_id),
        None,
    )
    if not isinstance(manifest_item, PdfManifestItem):
        raise BatchRuntimeError("journal_corrupt", "local prepare item is not a PDF manifest item")
    validate_pdf_source(manifest_item.source)
    return manifest_item


def claim_local_prepare(
    run_dir: Path,
    *,
    worker_id: str,
    request_id: str,
    limit: int | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="local-prepare.claim",
    )
    if not worker_id.strip():
        _raise_input_error(existing_event, "invalid_worker", "worker id must not be empty")
    requested_limit = preflight.manifest.default_concurrency if limit is None else limit
    if requested_limit < 1 or requested_limit > preflight.manifest.default_concurrency:
        _raise_input_error(
            existing_event,
            "invalid_limit",
            "local prepare claim limit exceeds manifest concurrency",
        )
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        _raise_input_error(
            existing_event,
            "invalid_lease",
            f"lease seconds must be between 1 and {MAX_LEASE_SECONDS}",
        )
    fingerprint = canonical_sha256(
        {
            "command": "local-prepare.claim",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "worker_id": worker_id,
            "limit": requested_limit,
            "lease_seconds": lease_seconds,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        expires_at = _format_utc(_parse_utc(transaction_time) + timedelta(seconds=lease_seconds))
        capacity = view.manifest.default_concurrency - _active_count(view)
        count = min(requested_limit, capacity, 1)
        manifest_by_id = {item.item_id: item for item in view.manifest.items}
        eligible = [
            item
            for item in view.state.items
            if isinstance(manifest_by_id[item.item_id], PdfManifestItem)
            and _is_claimable_local_prepare_item(item)
            and item.worker_status != "claimed"
        ][:count]
        if not eligible:
            raise BatchRuntimeError("no_available_work", "no local PDF item is currently claimable")
        for item in eligible:
            manifest_item = manifest_by_id[item.item_id]
            assert isinstance(manifest_item, PdfManifestItem)
            validate_pdf_source(manifest_item.source)
        assignments = []
        for item in eligible:
            claim_id = str(uuid4())
            attempt_id = str(uuid4())
            token = derive_lease_token(
                view.lease_secret,
                lane="local_prepare",
                claim_id=claim_id,
                attempt_id=attempt_id,
            )
            assignments.append(
                ClaimAssignment(
                    item_id=item.item_id,
                    lane="local_prepare",
                    actor_id=worker_id,
                    claim_id=claim_id,
                    attempt_id=attempt_id,
                    attempt_number=item.local_prepare_attempt_count + 1,
                    lease_token_sha256=sha256_bytes(token.encode()),
                    issued_at=transaction_time,
                    expires_at=expires_at,
                    source=manifest_by_id[item.item_id].source,
                )
            )
        data = ClaimedData(kind="local_prepare.claimed", assignments=assignments)
        return ProposedTransition(data=data, result=_claim_result(view, data))

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, ClaimedData) or event.data.kind != "local_prepare.claimed":
            raise BatchRuntimeError("journal_corrupt", "local prepare claim request points to another event")
        return _claim_result(view, event.data)

    def replay_validate(view: RunView, event) -> None:
        if not isinstance(event.data, ClaimedData) or event.data.kind != "local_prepare.claimed":
            raise BatchRuntimeError("journal_corrupt", "local prepare claim replay points to another event")
        manifest_by_id = {item.item_id: item for item in view.manifest.items}
        for assignment in event.data.assignments:
            manifest_item = manifest_by_id.get(assignment.item_id)
            if not isinstance(manifest_item, PdfManifestItem) or manifest_item.source != assignment.source:
                raise BatchRuntimeError("journal_corrupt", "local prepare claim source differs from manifest")
            validate_pdf_source(manifest_item.source)

    def commit_validate(view: RunView) -> None:
        capacity = view.manifest.default_concurrency - _active_count(view)
        count = min(requested_limit, capacity, 1)
        manifest_by_id = {item.item_id: item for item in view.manifest.items}
        eligible = [
            item
            for item in view.state.items
            if isinstance(manifest_by_id[item.item_id], PdfManifestItem)
            and _is_claimable_local_prepare_item(item)
            and item.worker_status != "claimed"
        ][:count]
        for item in eligible:
            _validate_local_pdf_source(view, item.item_id)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="local-prepare.claim",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        replay_validate=replay_validate,
        commit_validate=commit_validate,
        fault=fault,
    )


def _active_lease(
    view: RunView,
    *,
    item_id: str,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    now: str,
):
    item = next((entry for entry in view.state.items if entry.item_id == item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
    lease = item.local_prepare_lease
    if item.local_prepare_status != "claimed" or lease is None:
        raise BatchRuntimeError("lease_inactive", f"local prepare lease is not active: {item_id}")
    expected = derive_lease_token(
        view.lease_secret,
        lane="local_prepare",
        claim_id=lease.claim_id,
        attempt_id=lease.attempt_id,
    )
    if (
        lease.actor_id != worker_id
        or lease.claim_id != claim_id
        or lease.attempt_id != attempt_id
        or not hmac.compare_digest(expected, lease_token)
        or lease.lease_token_sha256 != sha256_bytes(lease_token.encode())
    ):
        raise BatchRuntimeError("lease_identity_mismatch", "local prepare lease identity does not match")
    if _parse_utc(now) >= _parse_utc(lease.expires_at):
        raise BatchRuntimeError("lease_expired", f"local prepare lease has expired: {item_id}")
    return item, lease


def _remaining_lease_seconds(lease, now: str) -> float:
    return (_parse_utc(lease.expires_at) - _parse_utc(now)).total_seconds()


def _require_lease_budget(lease, now: str, required_seconds: int) -> None:
    remaining = _remaining_lease_seconds(lease, now)
    if remaining < required_seconds:
        raise BatchRuntimeError(
            "insufficient_lease_time",
            f"local prepare lease has {remaining:.3f}s remaining but requires at least {required_seconds}s",
        )


def _require_exact_data(envelope: _ChildCommandResult, expected_keys: set[str]) -> dict[str, JsonValue]:
    if set(envelope.data) != expected_keys:
        raise _ChildProtocolError(
            "invalid_child_envelope",
            f"{envelope.command} returned unexpected data keys",
        )
    return envelope.data


def _absolute_child_path(value: JsonValue, label: str) -> Path:
    if not isinstance(value, str):
        raise _ChildProtocolError("invalid_child_envelope", f"{label} must be an absolute path string")
    normalized = normalized_absolute_path(Path(value))
    if str(normalized) != value:
        raise _ChildProtocolError("invalid_child_envelope", f"{label} must be normalized and absolute")
    return normalized


def _read_canonical_object(path: Path, *, code: str) -> tuple[bytes, dict[str, Any]]:
    raw, payload = read_json_bytes(path, code=code)
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise _ChildProtocolError(code, f"child artifact must be one canonical JSON object: {path}")
    return raw, payload


def _canonical_object_from_bytes(
    raw: bytes,
    *,
    artifact_path: Path,
    code: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ChildProtocolError(
            code,
            f"child artifact must be valid UTF-8 JSON: {artifact_path}",
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise _ChildProtocolError(
            code,
            f"child artifact must be one canonical JSON object: {artifact_path}",
        )
    return payload


def _validate_initialized_child(
    envelope: _ChildCommandResult,
    source: PdfSource,
) -> tuple[Path, str, Path]:
    if envelope.code != "initialized":
        raise _ChildProtocolError("invalid_child_envelope", "successful init-local returned the wrong result code")
    data = _require_exact_data(envelope, {"run_dir", "run_id", "target_path"})
    run_dir = _absolute_child_path(data["run_dir"], "run_dir")
    target_path = _absolute_child_path(data["target_path"], "target_path")
    run_id = data["run_id"]
    if not isinstance(run_id, str) or not run_id:
        raise _ChildProtocolError("invalid_child_envelope", "run_id must be a nonempty string")
    source_path = Path(source.path)
    if run_dir.parent != source_path.parent or target_path.parent != source_path.parent:
        raise _ChildProtocolError(
            "child_artifact_mismatch",
            "init-local returned paths outside the exact source directory",
        )
    _run_raw, run = _read_canonical_object(run_dir / "run.json", code="child_artifact_mismatch")
    expected_run_keys = {
        "schema_version",
        "run_id",
        "created_at",
        "source",
        "target",
        "status",
        "artifacts",
        "gate",
        "live_preflight",
    }
    if set(run) != expected_run_keys:
        raise _ChildProtocolError("child_artifact_mismatch", "initialized run has an invalid V2 shape")
    expected_source = {
        "source_type": "local_pdf",
        "requested_path": source.path,
        "resolved_path": source.path,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "device": source.file_identity.device,
        "inode": source.file_identity.inode,
    }
    try:
        with open_directory_fd(source_path.parent, create=False) as (
            parent_descriptor,
            _bound_parent,
        ):
            parent_metadata = os.fstat(parent_descriptor)
    except (BatchRuntimeError, OSError) as exc:
        raise _ChildProtocolError(
            "child_artifact_mismatch",
            "init-local source parent identity is unavailable or changed",
        ) from exc
    expected_target = {
        "target_type": "local",
        "resolved_path": str(target_path),
        "parent_device": parent_metadata.st_dev,
        "parent_inode": parent_metadata.st_ino,
    }
    if (
        run.get("schema_version") != "paper_reader.run.v2"
        or run.get("run_id") != run_id
        or run.get("status") != "initialized"
        or run.get("source") != expected_source
        or run.get("target") != expected_target
        or run.get("live_preflight") is not None
    ):
        raise _ChildProtocolError(
            "child_artifact_mismatch",
            "initialized run does not bind the exact manifest source and target",
        )
    _source_raw, source_payload = _read_canonical_object(
        run_dir / "source" / "source.json",
        code="child_artifact_mismatch",
    )
    if source_payload != expected_source:
        raise _ChildProtocolError("child_artifact_mismatch", "initialized source snapshot differs from manifest")
    if entry_exists(target_path):
        raise _ChildProtocolError("child_artifact_mismatch", "init-local unexpectedly occupied its note target")
    return run_dir, run_id, target_path


def _validate_prepared_child(
    envelope: _ChildCommandResult,
    *,
    run_dir: Path,
    run_id: str,
    source: PdfSource,
) -> tuple[ArtifactRef, ArtifactRef, FileIdentity, Path, str, str]:
    if envelope.code != "prepared":
        raise _ChildProtocolError("incomplete_evidence", "local prepare requires complete PDF evidence")
    data = _require_exact_data(
        envelope,
        {"run_dir", "evidence_dir", "evidence_id", "evidence_digest", "complete", "degraded"},
    )
    returned_run_dir = _absolute_child_path(data["run_dir"], "run_dir")
    evidence_dir = _absolute_child_path(data["evidence_dir"], "evidence_dir")
    evidence_id = data["evidence_id"]
    evidence_digest = data["evidence_digest"]
    if returned_run_dir != run_dir:
        raise _ChildProtocolError("child_artifact_mismatch", "prepare returned a different run directory")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise _ChildProtocolError("invalid_child_envelope", "evidence_id must be nonempty")
    if (
        not isinstance(evidence_digest, str)
        or len(evidence_digest) != 64
        or any(char not in "0123456789abcdef" for char in evidence_digest)
    ):
        raise _ChildProtocolError("invalid_child_envelope", "evidence_digest must be lowercase SHA-256")
    if data["complete"] is not True or not isinstance(data["degraded"], bool):
        raise _ChildProtocolError("incomplete_evidence", "prepare returned incomplete or invalid evidence status")
    if evidence_dir != run_dir / "evidence" / evidence_id:
        raise _ChildProtocolError("child_artifact_mismatch", "evidence directory is outside the exact run")
    with open_directory_fd(run_dir, create=False) as (run_descriptor, bound_run_dir):
        if bound_run_dir != run_dir:
            raise _ChildProtocolError(
                "child_artifact_mismatch",
                "prepared run directory is not the exact normalized initialized path",
            )
        run_metadata = os.fstat(run_descriptor)
        run_directory_identity = FileIdentity(
            device=run_metadata.st_dev,
            inode=run_metadata.st_ino,
        )
        run_raw = read_relative_bytes(
            run_descriptor,
            "run.json",
            code="child_artifact_mismatch",
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        run = _canonical_object_from_bytes(
            run_raw,
            artifact_path=run_dir / "run.json",
            code="child_artifact_mismatch",
        )
        evidence_path = evidence_dir / "evidence.json"
        evidence_relative = f"evidence/{evidence_id}/evidence.json"
        evidence_raw = read_relative_bytes(
            run_descriptor,
            evidence_relative,
            code="child_artifact_mismatch",
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        evidence = _canonical_object_from_bytes(
            evidence_raw,
            artifact_path=evidence_path,
            code="child_artifact_mismatch",
        )
        if sha256_bytes(evidence_raw) != evidence_digest:
            raise _ChildProtocolError(
                "child_artifact_mismatch",
                "evidence digest differs from evidence.json",
            )
        if (
            run.get("schema_version") != "paper_reader.run.v2"
            or run.get("run_id") != run_id
            or run.get("status") != "prepared"
            or evidence.get("format") != "paper_reader.evidence.v2-internal"
            or evidence.get("evidence_id") != evidence_id
            or evidence.get("run_id") != run_id
            or evidence.get("source_sha256") != source.sha256
            or evidence.get("complete") is not True
            or evidence.get("degraded") != data["degraded"]
        ):
            raise _ChildProtocolError(
                "child_artifact_mismatch",
                "prepared run/evidence identities differ from the accepted init/source",
            )
    run_ref = ArtifactRef(
        path=str(run_dir / "run.json"),
        size_bytes=len(run_raw),
        sha256=sha256_bytes(run_raw),
        schema_version="paper_reader.run.v2",
        artifact_id=run_id,
    )
    evidence_ref = ArtifactRef(
        path=str(evidence_path),
        size_bytes=len(evidence_raw),
        sha256=evidence_digest,
        schema_version="paper_reader.evidence.v2-internal",
        artifact_id=evidence_id,
    )
    return (
        run_ref,
        evidence_ref,
        run_directory_identity,
        evidence_dir,
        evidence_id,
        evidence_digest,
    )


def _validate_bound_execution_inputs(
    view: RunView,
    *,
    item_id: str,
    source: PdfSource,
    root_identity: SkillRootIdentity,
    cwd: Path,
) -> None:
    manifest_item = next((item for item in view.manifest.items if item.item_id == item_id), None)
    if not isinstance(manifest_item, PdfManifestItem) or manifest_item.source != source:
        raise BatchRuntimeError("source_drift", "local prepare source differs from the accepted manifest")
    bound_root = normalized_absolute_path(Path(root_identity.path))
    if normalized_absolute_path(cwd) != bound_root:
        raise BatchRuntimeError("paper_reader_root_drift", "paper_reader child cwd differs from the bound root")
    try:
        current_root_identity = paper_reader_root_identity(bound_root)
    except BatchRuntimeError as exc:
        raise BatchRuntimeError(
            "paper_reader_root_drift",
            "paper_reader root became invalid before child execution",
        ) from exc
    if current_root_identity != root_identity:
        raise BatchRuntimeError(
            "paper_reader_root_drift",
            "paper_reader root identity changed before child execution",
        )
    validate_pdf_source(manifest_item.source)


def _child_step(
    *,
    step: Literal["init", "prepare"],
    run_dir: Path,
    item_id: str,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    now: str | None,
    required_lease_seconds: int,
    request_dir: Path,
    record_path: Path,
    record: _CoordinationRecord,
    record_raw: bytes,
    secret: bytes,
    runner: ChildRunner,
    cwd: Path,
    timeout_seconds: int,
    fault: FaultHook | None,
) -> tuple[_CoordinationRecord, bytes, _ChildCommandResult, str]:
    stdout_path = request_dir / f"{step}.stdout"
    started_path = request_dir / f"{step}.started"
    _assert_request_directory_identity(
        request_dir,
        device=record.request_dir_device,
        inode=record.request_dir_inode,
    )
    invoked_field = f"{step}_invoked"
    execution_released_field = f"{step}_execution_released"
    stored_argv = record.init_argv if step == "init" else record.prepare_argv
    argv = tuple(stored_argv) if stored_argv is not None else None
    if argv is None:
        raise BatchRuntimeError("coordination_corrupt", f"{step} argv is not bound")
    raw = _read_stdout_stage(stdout_path)
    started = _load_exact_child_started(
        started_path,
        record,
        step=step,
        secret=secret,
    )
    returncode: int | None = None
    execution: int | _RunningChild | None = None

    def persist_execution_release() -> None:
        nonlocal record, record_raw
        if getattr(record, execution_released_field):
            return
        record, record_raw = _replace_coordination_record(
            record_path,
            record,
            record_raw,
            secret,
            **{execution_released_field: True},
        )

    if not raw and started is None:
        if getattr(record, execution_released_field):
            raise _ChildProtocolError(
                "coordination_uncertain",
                f"{step} execution was released but its durable marker is missing; refusing to re-execute",
            )
        with locked_run(run_dir) as current:
            if not hmac.compare_digest(current.lease_secret, secret):
                raise BatchRuntimeError(
                    "coordination_corrupt",
                    "coordination secret differs from the authoritative run secret",
                )
            # The original coordinator may have died after starting its
            # child-owned launcher but before this replacement observed the
            # marker. The launcher inherits .run.lock until the atomic marker
            # is durable, so values must be refreshed only after this lock is
            # acquired; otherwise stale pre-lock reads could launch twice.
            started = _load_exact_child_started(
                started_path,
                record,
                step=step,
                secret=secret,
            )
            if started is None:
                raw = _read_stdout_stage(stdout_path)
            if not raw and started is None:
                if getattr(record, execution_released_field):
                    raise _ChildProtocolError(
                        "coordination_uncertain",
                        f"{step} execution was released but its durable marker is missing; refusing to re-execute",
                    )
                authoritative_now = now or utc_now()
                _item, lease = _active_lease(
                    current,
                    item_id=item_id,
                    worker_id=worker_id,
                    claim_id=claim_id,
                    lease_token=lease_token,
                    attempt_id=attempt_id,
                    now=authoritative_now,
                )
                _require_lease_budget(
                    lease,
                    authoritative_now,
                    required_lease_seconds,
                )
                if not getattr(record, invoked_field):
                    record, record_raw = _replace_coordination_record(
                        record_path,
                        record,
                        record_raw,
                        secret,
                        **{invoked_field: True},
                    )
                if fault is not None:
                    fault(f"after_{step}_invocation_reserved")
                _validate_bound_execution_inputs(
                    current,
                    item_id=item_id,
                    source=record.source,
                    root_identity=record.paper_reader_root,
                    cwd=cwd,
                )
                _assert_request_directory_identity(
                    request_dir,
                    device=record.request_dir_device,
                    inode=record.request_dir_inode,
                )
                _started_model, started_payload = _child_started_bytes(
                    record,
                    step=step,
                    secret=secret,
                )
                if current.lock_descriptor is None:  # pragma: no cover - locked_run always binds it
                    raise BatchRuntimeError("coordination_corrupt", "run lock descriptor is unavailable")
                invocation = _ChildInvocation(
                    started_path=started_path,
                    stdout_path=stdout_path,
                    started_payload=started_payload,
                    request_dir_device=record.request_dir_device,
                    request_dir_inode=record.request_dir_inode,
                    run_lock_descriptors=(
                        current.lock_descriptor,
                        *current.lock_ancestor_descriptors,
                    ),
                    started_callback=persist_execution_release,
                )
                validate_pdf_source(record.source)
                try:
                    execution = runner(argv, cwd, timeout_seconds, invocation)
                except _ChildProtocolError:
                    raise
                except Exception as exc:
                    raise _ChildProtocolError(
                        "child_execution_failed",
                        f"{step} child runner failed before a valid result was accepted",
                    ) from exc
                durable_started = _load_exact_child_started(
                    started_path,
                    record,
                    step=step,
                    secret=secret,
                )
                if durable_started is not None:
                    persist_execution_release()
                elif not getattr(record, execution_released_field):
                    raise _ChildProtocolError(
                        "child_execution_failed",
                        f"{step} child runner returned without a durable start marker",
                    )
                if fault is not None:
                    fault(f"after_{step}_execution_released")
        if execution is not None:
            if isinstance(execution, _RunningChild):
                returncode = execution.wait()
            elif type(execution) is int:
                returncode = execution
            else:  # pragma: no cover - guarded by the internal ChildRunner contract
                raise _ChildProtocolError(
                    "child_execution_failed",
                    f"{step} child runner returned an invalid execution handle",
                )
            if fault is not None:
                fault(f"after_{step}_child")
        started = _load_exact_child_started(
            started_path,
            record,
            step=step,
            secret=secret,
        )
        raw = _read_stdout_stage(stdout_path)
    if started is None:
        raise _ChildProtocolError(
            "coordination_uncertain",
            f"{step} has output without an exact durable child-start marker; refusing to re-execute",
        )
    if not raw:
        raise _ChildProtocolError(
            "coordination_uncertain",
            f"{step} child result is empty; refusing to re-execute",
        )
    envelope = _parse_child_envelope(
        raw,
        expected_command="run init-local" if step == "init" else "run prepare",
        returncode=returncode,
    )
    return record, record_raw, envelope, sha256_bytes(raw)


def _mutation_result(data: LeaseMutationData) -> dict[str, Any]:
    return {
        "item_id": data.item_id,
        "worker_id": data.actor_id,
        "claim_id": data.claim_id,
        "attempt_id": data.attempt_id,
        "attempt_number": data.attempt_number,
        "issued_at": data.issued_at,
        "expires_at": data.expires_at,
        "status": "claimed" if data.kind.endswith("renewed") else "queued",
    }


def renew_local_prepare(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    request_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="local-prepare.renew",
    )
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        _raise_input_error(
            existing_event,
            "invalid_lease",
            f"lease seconds must be between 1 and {MAX_LEASE_SECONDS}",
        )
    token_hash = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "local-prepare.renew",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "lease_seconds": lease_seconds,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        expires_at = _format_utc(_parse_utc(transaction_time) + timedelta(seconds=lease_seconds))
        item, lease = _active_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        if _parse_utc(expires_at) <= _parse_utc(lease.expires_at):
            raise BatchRuntimeError("lease_not_extended", "renewal must extend the current expiry")
        data = LeaseMutationData(
            kind="local_prepare.renewed",
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
            issued_at=transaction_time,
            expires_at=expires_at,
        )
        return ProposedTransition(data=data, result=_mutation_result(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, LeaseMutationData) or event.data.kind != "local_prepare.renewed":
            raise BatchRuntimeError("journal_corrupt", "local prepare renew request points to another event")
        return _mutation_result(event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="local-prepare.renew",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def local_prepare_attempt_has_execution_side_effects(
    view: RunView,
    *,
    item_id: str,
    attempt_id: str,
    claim_id: str | None = None,
) -> bool:
    """Inspect one exact attempt without acquiring the run or coordinator lock.

    Callers that make a recovery decision should pass the ``RunView`` obtained
    while holding ``.run.lock``. A durable reservation without a child-start
    marker is safe to requeue; any started, terminal, or unverifiable execution
    is conservatively treated as having side effects.
    """

    item = next((entry for entry in view.state.items if entry.item_id == item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
    expected_claim_id: str | None = None
    if item.local_prepare_lease is not None and item.local_prepare_lease.attempt_id == attempt_id:
        expected_claim_id = item.local_prepare_lease.claim_id
    elif item.local_prepare_last_attempt_id == attempt_id:
        expected_claim_id = item.local_prepare_last_claim_id
    if expected_claim_id is None:
        raise BatchRuntimeError(
            "coordination_corrupt",
            "local prepare attempt is not bound to the authoritative item state",
        )
    if claim_id is not None and claim_id != expected_claim_id:
        raise BatchRuntimeError(
            "coordination_corrupt",
            "local prepare claim differs from the authoritative attempt state",
        )

    coordination_root = view.run_dir / "results" / "local-prepare" / ".coordination"
    owner_path = coordination_root / ".attempts" / f"{attempt_id}.json"
    if not entry_exists_allow_missing_parent(owner_path):
        if item.local_prepare_coordination_request_id is not None:
            raise BatchRuntimeError(
                "coordination_corrupt",
                "journaled local prepare coordination owner disappeared",
            )
        return False
    owner, _owner_raw = _load_signed_model(owner_path, _AttemptOwner, view.lease_secret)
    if (
        owner.manifest_sha256 != view.manifest_sha256
        or owner.item_id != item_id
        or owner.claim_id != expected_claim_id
        or owner.attempt_id != attempt_id
        or not owner.request_fingerprint.startswith(f"{view.manifest_sha256}:")
    ):
        raise BatchRuntimeError(
            "coordination_corrupt",
            "local prepare attempt owner differs from the authoritative run state",
        )
    if item.local_prepare_coordination_request_id is not None and (
        item.local_prepare_coordination_request_id != owner.request_id
        or item.local_prepare_coordination_fingerprint != owner.request_fingerprint
        or item.local_prepare_coordination_device != owner.request_dir_device
        or item.local_prepare_coordination_inode != owner.request_dir_inode
    ):
        raise BatchRuntimeError(
            "coordination_corrupt",
            "journaled local prepare coordination binding differs from its owner",
        )

    request_dir = coordination_root / owner.request_id
    _assert_request_directory_identity(
        request_dir,
        device=owner.request_dir_device,
        inode=owner.request_dir_inode,
    )
    record_path = request_dir / "record.json"
    if not entry_exists_allow_missing_parent(record_path):
        raise BatchRuntimeError(
            "coordination_corrupt",
            "owned local prepare attempt is missing its exact coordination record",
        )
    record, _record_raw = _load_signed_model(
        record_path,
        _CoordinationRecord,
        view.lease_secret,
        recover_pending=view.lock_descriptor is not None,
    )
    if (
        record.request_id != owner.request_id
        or record.request_fingerprint != owner.request_fingerprint
        or record.manifest_sha256 != owner.manifest_sha256
        or record.item_id != owner.item_id
        or record.claim_id != owner.claim_id
        or record.attempt_id != owner.attempt_id
        or record.request_dir_device != owner.request_dir_device
        or record.request_dir_inode != owner.request_dir_inode
    ):
        raise BatchRuntimeError(
            "coordination_corrupt",
            "local prepare coordination record differs from its exact attempt owner",
        )

    saw_started = False
    for step in ("init", "prepare"):
        started_path = coordination_root / owner.request_id / f"{step}.started"
        if entry_exists_allow_missing_parent(started_path):
            _load_exact_child_started(
                started_path,
                record,
                step=step,
                secret=view.lease_secret,
            )
            saw_started = True
    if record.stage in {"result_ready", "finished"}:
        return True
    if (
        saw_started
        or record.stage == "initialized"
        or record.init_execution_released
        or record.prepare_execution_released
    ):
        return True
    for step in ("init", "prepare"):
        stdout_path = coordination_root / owner.request_id / f"{step}.stdout"
        if entry_exists_allow_missing_parent(stdout_path):
            stdout_raw = read_bytes(
                stdout_path,
                code="coordination_corrupt",
                max_bytes=MAX_CHILD_STDOUT_BYTES,
            )
            if stdout_raw:
                # Output without the atomic marker is unverifiable and must
                # never be converted into a fresh attempt.
                return True
    return False


def release_local_prepare(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    acknowledge_no_side_effects: bool,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="local-prepare.release",
    )
    if not acknowledge_no_side_effects:
        _raise_input_error(
            existing_event,
            "acknowledgement_required",
            "local prepare release requires explicit acknowledgement",
        )
    token_hash = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "local-prepare.release",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "acknowledge_no_side_effects": True,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        item, lease = _active_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        if local_prepare_attempt_has_execution_side_effects(
            view,
            item_id=item_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
        ):
            raise BatchRuntimeError(
                "side_effects_detected",
                "local prepare release cannot acknowledge an attempt with execution side effects",
            )
        data = LeaseMutationData(
            kind="local_prepare.released",
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
        )
        return ProposedTransition(data=data, result=_mutation_result(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, LeaseMutationData) or event.data.kind != "local_prepare.released":
            raise BatchRuntimeError("journal_corrupt", "local prepare release request points to another event")
        return _mutation_result(event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="local-prepare.release",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def _load_result(path: Path) -> tuple[Path, bytes, LocalPrepareResult, str]:
    result_path = normalized_absolute_path(path)
    raw, payload = read_json_bytes(result_path, code="result_unreadable")
    if not isinstance(payload, dict) or payload.get("schema_version") != LOCAL_PREPARE_RESULT_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"local prepare result schema must be exactly {LOCAL_PREPARE_RESULT_SCHEMA_VERSION}",
        )
    try:
        result = LocalPrepareResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_result", "local prepare result failed strict validation") from exc
    if raw != canonical_json_bytes(result):
        raise BatchRuntimeError("invalid_result", "local prepare result must use canonical JSON")
    return result_path, raw, result, sha256_bytes(raw)


def _finish_result(view: RunView, data: FinishedData) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "status": data.status,
        "result_path": str(view.run_dir / "results" / "local-prepare" / f"{data.result_sha256}.json"),
        "result_sha256": data.result_sha256,
    }


def finish_local_prepare(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    result_path: Path,
    request_id: str,
    expected_root: Path | None = None,
    coordination_request_fingerprint: str | None = None,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, _existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="local-prepare.finish",
    )
    input_path, raw, result, result_sha256 = _load_result(result_path)
    token_hash = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "local-prepare.finish",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "result_input_path": str(input_path),
            "result_sha256": result_sha256,
            "expected_root": str(normalized_absolute_path(expected_root)) if expected_root is not None else None,
            "coordination_request_fingerprint": coordination_request_fingerprint,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        item, lease = _active_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        if (
            result.manifest_sha256 != view.manifest_sha256
            or result.item_id != item_id
            or result.worker_id != worker_id
            or result.claim_id != claim_id
            or result.attempt_id != attempt_id
            or result.attempt_number != lease.attempt_number
            or result.lease_token_sha256 != token_hash
        ):
            raise BatchRuntimeError("result_identity_mismatch", "local prepare result does not bind active lease")
        validate_local_prepare_result_artifacts(view.manifest, result, expected_root=expected_root)
        error_code = result.error.code if result.error is not None else None
        error_message = result.error.message if result.error is not None else None
        if error_code == "coordination_uncertain":
            binding = (
                item.local_prepare_coordination_request_id,
                item.local_prepare_coordination_fingerprint,
                item.local_prepare_coordination_device,
                item.local_prepare_coordination_inode,
            )
            if result.status != "blocked" or not all(
                value is not None for value in binding
            ):
                raise BatchRuntimeError(
                    "coordination_corrupt",
                    "coordination uncertainty requires one exact journaled coordinator binding",
                )
            if not local_prepare_attempt_has_execution_side_effects(
                view,
                item_id=item_id,
                claim_id=claim_id,
                attempt_id=attempt_id,
            ):
                raise BatchRuntimeError(
                    "coordination_corrupt",
                    "coordination uncertainty requires durable execution-side-effect evidence",
                )
        data = FinishedData(
            kind="local_prepare.finished",
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
            status=result.status,
            result_sha256=result_sha256,
            failure_code=error_code,
            failure_message=error_message,
        )
        publication = ResultPublication(
            path=view.run_dir / "results" / "local-prepare" / f"{result_sha256}.json",
            content=raw,
        )
        return ProposedTransition(data=data, result=_finish_result(view, data), publication=publication)

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, FinishedData) or event.data.kind != "local_prepare.finished":
            raise BatchRuntimeError("journal_corrupt", "local prepare finish request points to another event")
        return _finish_result(view, event.data)

    def replay_validate(view: RunView, event) -> None:
        if not isinstance(event.data, FinishedData) or event.data.kind != "local_prepare.finished":
            raise BatchRuntimeError("journal_corrupt", "local prepare finish replay points to another event")
        manifest_item = next(
            (item for item in view.manifest.items if item.item_id == event.data.item_id),
            None,
        )
        if not isinstance(manifest_item, PdfManifestItem):
            raise BatchRuntimeError("journal_corrupt", "local prepare finish replay item is not a PDF")
        validate_pdf_source(manifest_item.source)

    def commit_validate(view: RunView) -> None:
        validate_local_prepare_result_artifacts(
            view.manifest,
            result,
            expected_root=expected_root,
        )
        if coordination_request_fingerprint is not None:
            state_item = next(
                (item for item in view.state.items if item.item_id == item_id),
                None,
            )
            if state_item is None or (
                state_item.local_prepare_coordination_request_id != canonical_request_id
                or state_item.local_prepare_coordination_fingerprint
                != coordination_request_fingerprint
                or state_item.local_prepare_coordination_device is None
                or state_item.local_prepare_coordination_inode is None
            ):
                raise BatchRuntimeError(
                    "coordination_corrupt",
                    "local prepare finish lost its exact journaled coordination binding",
                )
            request_dir = (
                view.run_dir
                / "results"
                / "local-prepare"
                / ".coordination"
                / canonical_request_id
            )
            _assert_request_directory_identity(
                request_dir,
                device=state_item.local_prepare_coordination_device,
                inode=state_item.local_prepare_coordination_inode,
            )
        _validate_local_pdf_source(view, item_id)

    def artifact_commit_guard(view: RunView, _event):
        return local_prepare_result_artifact_commit_guard(
            view.manifest,
            result,
            expected_root=expected_root,
        )

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="local-prepare.finish",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        replay_validate=replay_validate,
        commit_validate=commit_validate,
        commit_guard=artifact_commit_guard,
        fault=fault,
    )


def _matching_committed_finish(
    view: RunView,
    *,
    request_id: str,
    item_id: str,
    worker_id: str,
    claim_id: str,
    attempt_id: str,
    lease_token_sha256: str,
):
    candidates = list(view.events)
    if view.pending_event is not None:
        candidates.append(view.pending_event.event)
    for event in candidates:
        if event.request_id != request_id:
            continue
        data = event.data
        if (
            event.command != "local-prepare.finish"
            or not isinstance(data, FinishedData)
            or data.kind != "local_prepare.finished"
            or data.item_id != item_id
            or data.actor_id != worker_id
            or data.claim_id != claim_id
            or data.attempt_id != attempt_id
            or data.lease_token_sha256 != lease_token_sha256
        ):
            raise BatchRuntimeError(
                "idempotency_conflict",
                "request id is already bound to another journal operation or local prepare identity",
            )
        return data
    return None


def _coordination_reservation_request_id(request_id: str) -> str:
    return str(uuid5(UUID(request_id), LOCAL_PREPARE_COORDINATION_UUID_NAME))


def _reserve_coordination_in_journal(
    run_dir: Path,
    *,
    item_id: str,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    coordinator_request_id: str,
    coordinator_request_fingerprint: str,
    record_path: Path,
    now: str | None,
    fault: FaultHook | None,
) -> RequestOutcome:
    preflight = load_run_view_for_mutation(run_dir)
    token_hash = sha256_bytes(lease_token.encode())
    record, _record_raw = _load_signed_model(
        record_path,
        _CoordinationRecord,
        preflight.lease_secret,
        recover_pending=True,
    )
    internal_request_id = _coordination_reservation_request_id(coordinator_request_id)
    fingerprint = canonical_sha256(
        {
            "command": "local-prepare.run.reserve",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "coordinator_request_id": coordinator_request_id,
            "coordinator_request_fingerprint": coordinator_request_fingerprint,
            "request_dir_device": record.request_dir_device,
            "request_dir_inode": record.request_dir_inode,
        }
    )

    def result_payload(data: LocalPrepareCoordinationReservedData) -> dict[str, Any]:
        return {
            "item_id": data.item_id,
            "attempt_id": data.attempt_id,
            "coordinator_request_id": data.coordinator_request_id,
            "request_dir_device": data.request_dir_device,
            "request_dir_inode": data.request_dir_inode,
        }

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        _assert_request_directory_identity(
            record_path.parent,
            device=record.request_dir_device,
            inode=record.request_dir_inode,
        )
        current_record, _current_raw = _load_signed_model(
            record_path,
            _CoordinationRecord,
            view.lease_secret,
            # The caller holds the request's coordinator lock across the
            # complete journal reservation transaction, including the first
            # read-only proposal pass.
            recover_pending=True,
        )
        if current_record != record:
            raise BatchRuntimeError(
                "coordination_corrupt",
                "coordination record changed before its journal reservation",
            )
        _item, lease = _active_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        data = LocalPrepareCoordinationReservedData(
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
            coordinator_request_id=coordinator_request_id,
            coordinator_request_fingerprint=coordinator_request_fingerprint,
            request_dir_device=record.request_dir_device,
            request_dir_inode=record.request_dir_inode,
        )
        return ProposedTransition(data=data, result=result_payload(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        data = event.data
        if not isinstance(data, LocalPrepareCoordinationReservedData) or (
            data.item_id != item_id
            or data.actor_id != worker_id
            or data.claim_id != claim_id
            or data.attempt_id != attempt_id
            or data.lease_token_sha256 != token_hash
            or data.coordinator_request_id != coordinator_request_id
            or data.coordinator_request_fingerprint != coordinator_request_fingerprint
            or data.request_dir_device != record.request_dir_device
            or data.request_dir_inode != record.request_dir_inode
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                "coordination reservation request points to another event",
            )
        return result_payload(data)

    def commit_validate(view: RunView) -> None:
        _assert_request_directory_identity(
            record_path.parent,
            device=record.request_dir_device,
            inode=record.request_dir_inode,
        )
        current_record, _current_raw = _load_signed_model(
            record_path,
            _CoordinationRecord,
            view.lease_secret,
            recover_pending=True,
        )
        if current_record != record:
            raise BatchRuntimeError(
                "coordination_corrupt",
                "coordination record changed before its journal reservation commit",
            )
        _validate_bound_execution_inputs(
            view,
            item_id=item_id,
            source=record.source,
            root_identity=record.paper_reader_root,
            cwd=Path(record.paper_reader_root.path),
        )

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=internal_request_id,
        command="local-prepare.run.reserve",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        commit_validate=commit_validate,
        fault=fault,
    )


def _coordination_setup(
    run_dir: Path,
    *,
    request_id: str,
    request_fingerprint: str,
    item_id: str,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    source: PdfSource,
    root_identity: SkillRootIdentity,
    timeout_seconds: int,
    now: str | None,
) -> tuple[Path, Path, bytes]:
    token_hash = sha256_bytes(lease_token.encode())

    def validate_before_recovery(view: RunView) -> None:
        pre_recovery_now = now if now is not None else utc_now()
        if view.manifest_sha256 != request_fingerprint.split(":", 1)[0]:
            raise BatchRuntimeError(
                "manifest_drift",
                "manifest changed before local prepare coordination",
            )
        manifest_item = next(
            (item for item in view.manifest.items if item.item_id == item_id),
            None,
        )
        if not isinstance(manifest_item, PdfManifestItem) or manifest_item.source != source:
            raise BatchRuntimeError(
                "source_drift",
                "local prepare source differs from the accepted manifest",
            )
        _validate_bound_execution_inputs(
            view,
            item_id=item_id,
            source=source,
            root_identity=root_identity,
            cwd=Path(root_identity.path),
        )
        _item, lease = _active_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=pre_recovery_now,
        )
        state_item = next(entry for entry in view.state.items if entry.item_id == item_id)
        binding = (
            state_item.local_prepare_coordination_request_id,
            state_item.local_prepare_coordination_fingerprint,
            state_item.local_prepare_coordination_device,
            state_item.local_prepare_coordination_inode,
        )
        if any(value is not None for value in binding) and not all(
            value is not None for value in binding
        ):
            raise BatchRuntimeError(
                "coordination_corrupt",
                "journaled local prepare coordination binding is incomplete",
            )
        if binding[0] is not None:
            if binding[0] != request_id or binding[1] != request_fingerprint:
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "local prepare request id is already bound to different input",
                )
            request_dir = (
                view.run_dir
                / "results"
                / "local-prepare"
                / ".coordination"
                / request_id
            )
            assert binding[2] is not None and binding[3] is not None
            _assert_request_directory_identity(
                request_dir,
                device=binding[2],
                inode=binding[3],
            )
        else:
            _require_lease_budget(
                lease,
                pre_recovery_now,
                INIT_CHILD_TIMEOUT_SECONDS + timeout_seconds + COMMIT_BUFFER_SECONDS,
            )

    with locked_run(
        run_dir,
        pre_recovery_validate=validate_before_recovery,
    ) as view:
        locked_now = now if now is not None else utc_now()
        if view.manifest_sha256 != request_fingerprint.split(":", 1)[0]:
            # The fingerprint is prefixed with the manifest digest so drift is
            # detected before creating internal coordination state.
            raise BatchRuntimeError("manifest_drift", "manifest changed before local prepare coordination")
        manifest_item = next((item for item in view.manifest.items if item.item_id == item_id), None)
        if not isinstance(manifest_item, PdfManifestItem) or manifest_item.source != source:
            raise BatchRuntimeError("source_drift", "local prepare source differs from the accepted manifest")
        _validate_bound_execution_inputs(
            view,
            item_id=item_id,
            source=source,
            root_identity=root_identity,
            cwd=Path(root_identity.path),
        )
        _item, lease = _active_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=locked_now,
        )
        coordination_root = view.run_dir / "results" / "local-prepare" / ".coordination"
        attempts_root = coordination_root / ".attempts"
        request_dir = coordination_root / request_id
        record_path = request_dir / "record.json"
        state_item = next(entry for entry in view.state.items if entry.item_id == item_id)
        coordination_committed = state_item.local_prepare_coordination_request_id is not None
        if coordination_committed:
            if (
                state_item.local_prepare_coordination_request_id != request_id
                or state_item.local_prepare_coordination_fingerprint != request_fingerprint
            ):
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "local prepare request id is already bound to different input",
                )
            assert state_item.local_prepare_coordination_device is not None
            assert state_item.local_prepare_coordination_inode is not None
            _assert_request_directory_identity(
                request_dir,
                device=state_item.local_prepare_coordination_device,
                inode=state_item.local_prepare_coordination_inode,
            )
        if not entry_exists_allow_missing_parent(record_path):
            _require_lease_budget(
                lease,
                locked_now,
                INIT_CHILD_TIMEOUT_SECONDS + timeout_seconds + COMMIT_BUFFER_SECONDS,
            )
        owner_path = attempts_root / f"{attempt_id}.json"
        if coordination_committed:
            if not entry_exists_allow_missing_parent(owner_path) or not entry_exists_allow_missing_parent(record_path):
                raise BatchRuntimeError(
                    "coordination_corrupt",
                    "journaled local prepare coordination artifacts disappeared",
                )
        else:
            ensure_directory(attempts_root)
        owner_exists = entry_exists(owner_path)
        record_exists = entry_exists_allow_missing_parent(record_path)
        if owner_exists and not record_exists:
            raise BatchRuntimeError(
                "coordination_corrupt",
                "owned local prepare attempt is missing its exact coordination record",
            )
        if not owner_exists and not record_exists:
            ensure_directory(request_dir)
        request_dir_device, request_dir_inode = _request_directory_identity(request_dir)
        owner_payload = {
            "schema_version": ATTEMPT_OWNER_SCHEMA_VERSION,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "manifest_sha256": view.manifest_sha256,
            "item_id": item_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "request_dir_device": request_dir_device,
            "request_dir_inode": request_dir_inode,
        }
        expected_owner, owner_raw = _signed_model_bytes(_AttemptOwner, owner_payload, view.lease_secret)
        if owner_exists:
            actual_owner, _actual_raw = _load_signed_model(owner_path, _AttemptOwner, view.lease_secret)
            if actual_owner != expected_owner:
                same_logical_owner = actual_owner.model_dump(
                    mode="json",
                    exclude={"hmac_sha256", "request_dir_device", "request_dir_inode"},
                ) == expected_owner.model_dump(
                    mode="json",
                    exclude={"hmac_sha256", "request_dir_device", "request_dir_inode"},
                )
                if same_logical_owner:
                    raise BatchRuntimeError(
                        "coordination_corrupt",
                        "owned local prepare request directory identity changed",
                    )
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "local prepare attempt is already owned by another request or input",
                )
        init_argv = [
            "uv",
            "run",
            "--locked",
            "paper_reader",
            "run",
            "init-local",
            source.path,
        ]
        initial_payload = {
            "schema_version": COORDINATION_SCHEMA_VERSION,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "manifest_sha256": view.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "attempt_number": lease.attempt_number,
            "lease_token_sha256": token_hash,
            "request_dir_device": request_dir_device,
            "request_dir_inode": request_dir_inode,
            "source": source.model_dump(mode="json"),
            "paper_reader_root": root_identity.model_dump(mode="json"),
            "timeout_seconds": timeout_seconds,
            "stage": "reserved",
            "init_invoked": False,
            "init_execution_released": False,
            "init_argv": init_argv,
            "init_stdout_sha256": None,
            "paper_reader_run_dir": None,
            "paper_reader_run_id": None,
            "local_target_path": None,
            "prepare_invoked": False,
            "prepare_execution_released": False,
            "prepare_argv": None,
            "prepare_stdout_sha256": None,
            "evidence_dir": None,
            "evidence_id": None,
            "evidence_digest": None,
            "result_sha256": None,
        }
        expected_initial, initial_raw = _signed_model_bytes(
            _CoordinationRecord,
            initial_payload,
            view.lease_secret,
        )
        if record_exists:
            actual, _actual_raw = _load_signed_model(
                record_path,
                _CoordinationRecord,
                view.lease_secret,
                recover_pending=True,
            )
            fixed_fields = {
                "schema_version",
                "request_id",
                "request_fingerprint",
                "manifest_sha256",
                "item_id",
                "worker_id",
                "claim_id",
                "attempt_id",
                "attempt_number",
                "lease_token_sha256",
                "request_dir_device",
                "request_dir_inode",
                "source",
                "paper_reader_root",
                "timeout_seconds",
                "init_argv",
            }
            actual_payload = actual.model_dump(mode="json")
            expected_payload = expected_initial.model_dump(mode="json")
            if any(actual_payload[field] != expected_payload[field] for field in fixed_fields):
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "local prepare request record is bound to different input",
                )
        else:
            publish_bytes_no_replace(record_path, initial_raw)
        # The exact signed owner is the coordination commit point. A crash may
        # leave an unowned request record, but an owned attempt can never
        # legitimately lack its exact record.
        if not owner_exists:
            publish_bytes_no_replace(owner_path, owner_raw)
        return request_dir, record_path, view.lease_secret


def _result_from_error(
    *,
    manifest_sha256: str,
    item_id: str,
    worker_id: str,
    claim_id: str,
    attempt_id: str,
    attempt_number: int,
    lease_token_sha256: str,
    source: PdfSource,
    root_identity: SkillRootIdentity,
    status: Literal["failed", "blocked"],
    code: str,
    message: str,
) -> LocalPrepareResult:
    return LocalPrepareResult(
        schema_version=LOCAL_PREPARE_RESULT_SCHEMA_VERSION,
        manifest_sha256=manifest_sha256,
        item_id=item_id,
        worker_id=worker_id,
        claim_id=claim_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        lease_token_sha256=lease_token_sha256,
        status=status,
        source=source,
        paper_reader_root=root_identity,
        paper_reader_run_directory=None,
        error={"code": code, "message": message},
    )


def run_local_prepare(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    paper_reader_root: Path,
    request_id: str,
    timeout_seconds: int = DEFAULT_CHILD_TIMEOUT_SECONDS,
    now: str | None = None,
    runner: ChildRunner | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    invalid_timeout = timeout_seconds < 1 or timeout_seconds > MAX_CHILD_TIMEOUT_SECONDS
    try:
        preflight, canonical_request_id, existing_event = load_request_preflight(
            run_dir,
            request_id=request_id,
            command="local-prepare.finish",
        )
    except BatchRuntimeError as exc:
        if invalid_timeout and exc.code == "storage_missing":
            raise BatchRuntimeError(
                "invalid_timeout",
                f"child timeout must be between 1 and {MAX_CHILD_TIMEOUT_SECONDS} seconds",
            ) from exc
        raise
    if invalid_timeout:
        _raise_input_error(
            existing_event,
            "invalid_timeout",
            f"child timeout must be between 1 and {MAX_CHILD_TIMEOUT_SECONDS} seconds",
        )
    root = normalized_absolute_path(paper_reader_root)
    token_hash = sha256_bytes(lease_token.encode())
    manifest_item = next((item for item in preflight.manifest.items if item.item_id == item_id), None)
    if not isinstance(manifest_item, PdfManifestItem):
        raise BatchRuntimeError("unknown_item", "local-prepare run requires one PDF manifest item")
    root_identity = paper_reader_root_identity(root)
    fingerprint_digest = canonical_sha256(
        {
            "command": "local-prepare.run",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "source": manifest_item.source.model_dump(mode="json"),
            "paper_reader_root": root_identity.model_dump(mode="json"),
            "timeout_seconds": timeout_seconds,
            "now_override": now,
        }
    )
    request_fingerprint = f"{preflight.manifest_sha256}:{fingerprint_digest}"
    state_item = next(
        (item for item in preflight.state.items if item.item_id == item_id),
        None,
    )
    if state_item is not None and state_item.local_prepare_coordination_request_id == canonical_request_id:
        if state_item.local_prepare_coordination_fingerprint != request_fingerprint:
            raise BatchRuntimeError(
                "idempotency_conflict",
                "local prepare request id is already bound to different run input",
            )
    validate_pdf_source(manifest_item.source)
    if existing_event is not None:
        committed = _matching_committed_finish(
            preflight,
            request_id=canonical_request_id,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            lease_token_sha256=token_hash,
        )
        if committed is None:
            raise BatchRuntimeError(
                "idempotency_conflict",
                "local prepare request id is already bound to different finish input",
            )
        if (
            state_item is None
            or state_item.local_prepare_coordination_request_id != canonical_request_id
            or state_item.local_prepare_coordination_fingerprint != request_fingerprint
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                "completed local prepare run lacks its exact coordination request binding",
            )
        result_path = preflight.run_dir / "results" / "local-prepare" / f"{committed.result_sha256}.json"
        return finish_local_prepare(
            preflight.run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            result_path=result_path,
            request_id=canonical_request_id,
            expected_root=root,
            coordination_request_fingerprint=request_fingerprint,
            now=now,
            fault=fault,
        )
    request_dir, record_path, secret = _coordination_setup(
        preflight.run_dir,
        request_id=canonical_request_id,
        request_fingerprint=request_fingerprint,
        item_id=item_id,
        worker_id=worker_id,
        claim_id=claim_id,
        lease_token=lease_token,
        attempt_id=attempt_id,
        source=manifest_item.source,
        root_identity=root_identity,
        timeout_seconds=timeout_seconds,
        now=now,
    )
    child_runner = runner or _default_child_runner
    # The exact run-lock descriptor bundle is inherited through marker
    # publication. Avoid a grandparent coordinator guard here so independent
    # item preparations can remain parallel inside one batch run.
    with locked_file(
        request_dir / "coordinator.lock",
        create=True,
        guard_parent_replacement=False,
    ):
        _reserve_coordination_in_journal(
            preflight.run_dir,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            coordinator_request_id=canonical_request_id,
            coordinator_request_fingerprint=request_fingerprint,
            record_path=record_path,
            now=now,
            fault=fault,
        )
        coordination_root = preflight.run_dir / "results" / "local-prepare" / ".coordination"
        owner_path = coordination_root / ".attempts" / f"{attempt_id}.json"
        owner, _owner_raw = _load_signed_model(owner_path, _AttemptOwner, secret)
        if owner.request_id != canonical_request_id or owner.attempt_id != attempt_id:
            raise BatchRuntimeError(
                "coordination_corrupt",
                "coordination owner differs from the exact local prepare request",
            )
        _assert_request_directory_identity(
            request_dir,
            device=owner.request_dir_device,
            inode=owner.request_dir_inode,
        )
        record, record_raw = _load_signed_model(
            record_path,
            _CoordinationRecord,
            secret,
            recover_pending=True,
        )
        _assert_request_directory_identity(
            request_dir,
            device=record.request_dir_device,
            inode=record.request_dir_inode,
        )
        result: LocalPrepareResult | None = None
        if record.stage in {"result_ready", "finished"}:
            assert record.result_sha256 is not None
        else:
            terminal_record_updates: dict[str, Any] = {}
            if record.stage == "reserved":
                try:
                    record, record_raw, envelope, stdout_sha = _child_step(
                        step="init",
                        run_dir=preflight.run_dir,
                        item_id=item_id,
                        worker_id=worker_id,
                        claim_id=claim_id,
                        lease_token=lease_token,
                        attempt_id=attempt_id,
                        now=now,
                        required_lease_seconds=(
                            INIT_CHILD_TIMEOUT_SECONDS + timeout_seconds + COMMIT_BUFFER_SECONDS
                        ),
                        request_dir=request_dir,
                        record_path=record_path,
                        record=record,
                        record_raw=record_raw,
                        secret=secret,
                        runner=child_runner,
                        cwd=root,
                        timeout_seconds=INIT_CHILD_TIMEOUT_SECONDS,
                        fault=fault,
                    )
                    if not envelope.ok:
                        result = _result_from_error(
                            manifest_sha256=preflight.manifest_sha256,
                            item_id=item_id,
                            worker_id=worker_id,
                            claim_id=claim_id,
                            attempt_id=attempt_id,
                            attempt_number=record.attempt_number,
                            lease_token_sha256=token_hash,
                            source=manifest_item.source,
                            root_identity=root_identity,
                            status="failed",
                            code=envelope.code,
                            message=envelope.message or f"run init-local failed with {envelope.code}",
                        )
                    else:
                        paper_run_dir, paper_run_id, target_path = _validate_initialized_child(
                            envelope,
                            manifest_item.source,
                        )
                        record, record_raw = _replace_coordination_record(
                            record_path,
                            record,
                            record_raw,
                            secret,
                            stage="initialized",
                            init_stdout_sha256=stdout_sha,
                            paper_reader_run_dir=str(paper_run_dir),
                            paper_reader_run_id=paper_run_id,
                            local_target_path=str(target_path),
                            prepare_argv=[
                                "uv",
                                "run",
                                "--locked",
                                "paper_reader",
                                "run",
                                "prepare",
                                str(paper_run_dir),
                            ],
                        )
                except _ChildProtocolError as exc:
                    record, record_raw = _load_signed_model(
                        record_path,
                        _CoordinationRecord,
                        secret,
                        recover_pending=True,
                    )
                    result = _result_from_error(
                        manifest_sha256=preflight.manifest_sha256,
                        item_id=item_id,
                        worker_id=worker_id,
                        claim_id=claim_id,
                        attempt_id=attempt_id,
                        attempt_number=record.attempt_number,
                        lease_token_sha256=token_hash,
                        source=manifest_item.source,
                        root_identity=root_identity,
                        status="failed" if exc.code in {"child_timeout", "child_execution_failed"} else "blocked",
                        code=exc.code,
                        message=str(exc),
                    )
                except BatchRuntimeError as exc:
                    if exc.code != "child_artifact_mismatch":
                        raise
                    result = _result_from_error(
                        manifest_sha256=preflight.manifest_sha256,
                        item_id=item_id,
                        worker_id=worker_id,
                        claim_id=claim_id,
                        attempt_id=attempt_id,
                        attempt_number=record.attempt_number,
                        lease_token_sha256=token_hash,
                        source=manifest_item.source,
                        root_identity=root_identity,
                        status="blocked",
                        code=exc.code,
                        message=str(exc),
                    )
            if result is None and record.stage == "initialized":
                assert record.paper_reader_run_dir and record.paper_reader_run_id
                try:
                    record, record_raw, envelope, stdout_sha = _child_step(
                        step="prepare",
                        run_dir=preflight.run_dir,
                        item_id=item_id,
                        worker_id=worker_id,
                        claim_id=claim_id,
                        lease_token=lease_token,
                        attempt_id=attempt_id,
                        now=now,
                        required_lease_seconds=timeout_seconds + COMMIT_BUFFER_SECONDS,
                        request_dir=request_dir,
                        record_path=record_path,
                        record=record,
                        record_raw=record_raw,
                        secret=secret,
                        runner=child_runner,
                        cwd=root,
                        timeout_seconds=timeout_seconds,
                        fault=fault,
                    )
                    if not envelope.ok:
                        result = _result_from_error(
                            manifest_sha256=preflight.manifest_sha256,
                            item_id=item_id,
                            worker_id=worker_id,
                            claim_id=claim_id,
                            attempt_id=attempt_id,
                            attempt_number=record.attempt_number,
                            lease_token_sha256=token_hash,
                            source=manifest_item.source,
                            root_identity=root_identity,
                            status="failed",
                            code=envelope.code,
                            message=envelope.message or f"run prepare failed with {envelope.code}",
                        )
                    else:
                        (
                            run_ref,
                            evidence_ref,
                            run_directory_identity,
                            evidence_dir,
                            evidence_id,
                            evidence_digest,
                        ) = _validate_prepared_child(
                            envelope,
                            run_dir=Path(record.paper_reader_run_dir),
                            run_id=record.paper_reader_run_id,
                            source=manifest_item.source,
                        )
                        result = LocalPrepareResult(
                            schema_version=LOCAL_PREPARE_RESULT_SCHEMA_VERSION,
                            manifest_sha256=preflight.manifest_sha256,
                            item_id=item_id,
                            worker_id=worker_id,
                            claim_id=claim_id,
                            attempt_id=attempt_id,
                            attempt_number=record.attempt_number,
                            lease_token_sha256=token_hash,
                            status="prepared",
                            source=manifest_item.source,
                            paper_reader_root=root_identity,
                            paper_reader_run_directory=run_directory_identity,
                            paper_reader_run=run_ref,
                            evidence=evidence_ref,
                        )
                        terminal_record_updates = {
                            "prepare_stdout_sha256": stdout_sha,
                            "evidence_dir": str(evidence_dir),
                            "evidence_id": evidence_id,
                            "evidence_digest": evidence_digest,
                        }
                except _ChildProtocolError as exc:
                    record, record_raw = _load_signed_model(
                        record_path,
                        _CoordinationRecord,
                        secret,
                        recover_pending=True,
                    )
                    result = _result_from_error(
                        manifest_sha256=preflight.manifest_sha256,
                        item_id=item_id,
                        worker_id=worker_id,
                        claim_id=claim_id,
                        attempt_id=attempt_id,
                        attempt_number=record.attempt_number,
                        lease_token_sha256=token_hash,
                        source=manifest_item.source,
                        root_identity=root_identity,
                        status="failed" if exc.code in {"child_timeout", "child_execution_failed"} else "blocked",
                        code=exc.code,
                        message=str(exc),
                    )
                except BatchRuntimeError as exc:
                    if exc.code != "child_artifact_mismatch":
                        raise
                    result = _result_from_error(
                        manifest_sha256=preflight.manifest_sha256,
                        item_id=item_id,
                        worker_id=worker_id,
                        claim_id=claim_id,
                        attempt_id=attempt_id,
                        attempt_number=record.attempt_number,
                        lease_token_sha256=token_hash,
                        source=manifest_item.source,
                        root_identity=root_identity,
                        status="blocked",
                        code=exc.code,
                        message=str(exc),
                    )
            if result is None:
                raise BatchRuntimeError("coordination_corrupt", "coordination did not produce a terminal result")
            validate_local_prepare_result_artifacts(
                preflight.manifest,
                result,
                expected_root=root,
            )
            result_raw = canonical_json_bytes(result)
            result_sha256 = sha256_bytes(result_raw)
            result_path = preflight.run_dir / "results" / "local-prepare" / f"{result_sha256}.json"
            publish_bytes_no_replace(result_path, result_raw, allow_existing_exact=True)
            record, record_raw = _replace_coordination_record(
                record_path,
                record,
                record_raw,
                secret,
                stage="result_ready",
                result_sha256=result_sha256,
                **terminal_record_updates,
            )
        assert record.result_sha256 is not None
        result_path = preflight.run_dir / "results" / "local-prepare" / f"{record.result_sha256}.json"
        if record.stage != "finished" and fault is not None:
            fault("before_batch_event")
        outcome = finish_local_prepare(
            preflight.run_dir,
            item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            result_path=result_path,
            request_id=canonical_request_id,
            expected_root=root,
            coordination_request_fingerprint=request_fingerprint,
            now=now,
            fault=fault,
        )
        if record.stage != "finished":
            record, record_raw = _replace_coordination_record(
                record_path,
                record,
                record_raw,
                secret,
                stage="finished",
            )
        return outcome


__all__ = [
    "claim_local_prepare",
    "finish_local_prepare",
    "local_prepare_attempt_has_execution_side_effects",
    "release_local_prepare",
    "renew_local_prepare",
    "run_local_prepare",
]
