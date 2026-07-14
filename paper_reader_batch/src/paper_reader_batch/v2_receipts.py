from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Callable, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

import paper_reader_batch.v2_json as v2_json
from paper_reader_batch.v2_contracts import AbsolutePath, Sha256, StrictModel, UuidString
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    active_transition_targets,
    canonical_json_bytes,
    ensure_directory,
    entry_exists_allow_missing_parent,
    initialize_locked_secret,
    internal_zero_tombstone,
    list_directory,
    list_mutable_directory,
    locked_file,
    normalized_absolute_path,
    publish_bytes_no_replace,
    promote_bytes_no_replace,
    read_bytes,
    read_committed_transitions,
    read_json_bytes,
    read_pending_swap,
    replace_bytes_atomic,
    sha256_bytes,
    validate_locked_path,
)


REQUEST_RECEIPT_SCHEMA_VERSION = "paper_reader_batch.request-receipt.v2-internal"
_UUID_ADAPTER = TypeAdapter(UuidString)


class RequestReceipt(StrictModel):
    schema_version: Literal[REQUEST_RECEIPT_SCHEMA_VERSION] = REQUEST_RECEIPT_SCHEMA_VERSION
    request_id: UuidString
    command: str = Field(strict=True, min_length=1)
    request_fingerprint: Sha256
    requested_target: AbsolutePath | None
    target: AbsolutePath
    status: Literal["reserved", "committed"]
    plan: dict[str, JsonValue]
    result: dict[str, JsonValue] | None
    integrity_hmac: Sha256


@dataclass(frozen=True)
class RequestOutcome:
    result: dict[str, JsonValue]
    replayed: bool


FaultHook = Callable[[str], None]
TargetFactory = Callable[[set[str]], Path]
PlanFactory = Callable[[Path], dict[str, JsonValue]]
Publisher = Callable[[Path, dict[str, JsonValue], bool], None]
Inspector = Callable[[Path, dict[str, JsonValue]], bool]
InputValidator = Callable[[], None]


def validate_request_id(value: str) -> str:
    try:
        return _UUID_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_request_id", "--request-id must be a canonical UUID") from exc


class RequestReceiptStore:
    def __init__(self, skill_root: Path) -> None:
        self.root = normalized_absolute_path(skill_root) / ".paper_reader_batch"
        self.receipts = self.root / "request-receipts"
        self.lock_path = self.root / "request-receipts.lock"

    def _receipt_path(self, request_id: str) -> Path:
        return self.receipts / f"{request_id}.json"

    def _transition_targets(self) -> frozenset[str]:
        final_pattern = re.compile(r"^[0-9a-f-]{36}\.json$")
        return frozenset(
            name
            for name in list_directory(self.receipts)
            if final_pattern.fullmatch(name)
        )

    def _preflight_incoming_identity(
        self,
        names: list[str],
        key: bytes,
        incoming_identity: tuple[str, str, str, str | None],
    ) -> None:
        """Reject an already-bound request before recovering any other receipt."""

        request_id, command, fingerprint, requested_target = incoming_identity
        target_name = f"{request_id}.json"
        temporary_pattern = re.compile(
            rf"^\.{re.escape(target_name)}\.(?P<digest>[0-9a-f]{{64}})\.tmp$"
        )
        writing_pattern = re.compile(
            rf"^\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.writing$"
        )
        candidate_names = [
            name
            for name in names
            if name == target_name
            or temporary_pattern.fullmatch(name) is not None
            or writing_pattern.fullmatch(name) is not None
        ]
        if len(candidate_names) > 66:
            raise BatchRuntimeError(
                "resource_limit",
                "too many receipt identity candidates for one request id",
            )

        validated: list[tuple[RequestReceipt, bytes]] = []
        aggregate_bytes = 0
        for name in candidate_names:
            remaining = v2_json.MAX_JSON_ARTIFACT_BYTES - aggregate_bytes
            raw = read_bytes(
                self.receipts / name,
                code="resource_limit",
                max_bytes=remaining,
            )
            aggregate_bytes += len(raw)
            temporary_match = temporary_pattern.fullmatch(name)
            is_writing = writing_pattern.fullmatch(name) is not None
            if temporary_match is not None and sha256_bytes(raw) != temporary_match.group("digest"):
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "pending receipt filename digest mismatch",
                )
            try:
                payload = json.loads(
                    raw,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                if is_writing:
                    continue
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "published or staged receipt is invalid JSON",
                ) from exc
            try:
                RequestReceipt.model_validate(payload)
            except ValidationError as exc:
                if is_writing:
                    continue
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "published or staged receipt fails strict validation",
                ) from exc
            receipt = self._validate_payload(self.receipts / name, raw, payload, key)
            if receipt.request_id != request_id:
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "receipt candidate request id differs from its filename",
                )
            if (
                receipt.command != command
                or receipt.request_fingerprint != fingerprint
                or receipt.requested_target != requested_target
            ):
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "request id is already bound to a different operation or input",
                )
            validated.append((receipt, raw))

        if len(validated) > 2:
            raise BatchRuntimeError(
                "receipt_corrupt",
                "too many complete receipt candidates exist for one request",
            )
        if len(validated) == 2:
            by_status = {receipt.status: (receipt, raw) for receipt, raw in validated}
            if len(by_status) != 2 or set(by_status) != {"reserved", "committed"}:
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "complete receipt candidates are ambiguous",
                )
            reserved, reserved_raw = by_status["reserved"]
            committed, committed_raw = by_status["committed"]
            if (
                reserved.target != committed.target
                or reserved.plan != committed.plan
                or committed_raw != canonical_json_bytes(self._committed_receipt(reserved, key))
                or reserved_raw != canonical_json_bytes(reserved)
            ):
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "receipt candidates are not one exact reserved-to-committed transition",
                )

    def _list_receipts(
        self,
        key: bytes | None = None,
        *,
        recover: bool = False,
        incoming_identity: tuple[str, str, str, str | None] | None = None,
    ) -> list[str]:
        names = list_directory(self.receipts)
        final_pattern = re.compile(r"^[0-9a-f-]{36}\.json$")
        replace_targets = set(self._transition_targets())
        final_targets = {name for name in replace_targets if final_pattern.fullmatch(name)}
        if incoming_identity is not None and key is not None:
            self._preflight_incoming_identity(names, key, incoming_identity)
        active_targets = active_transition_targets(
            self.receipts,
            replace_targets=replace_targets,
        )
        for target_name in final_targets:
            if target_name not in active_targets:
                continue
            pending = read_pending_swap(
                self.receipts / target_name,
                max_bytes=v2_json.MAX_JSON_ARTIFACT_BYTES,
                replace_targets=replace_targets,
            )
            if not recover or key is None:
                raise BatchRuntimeError(
                    "storage_recovery_required",
                    f"request receipt swap requires its global lock: {target_name}",
                )

            def validate(raw: bytes) -> RequestReceipt:
                try:
                    payload = json.loads(
                        raw,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                    )
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                    raise BatchRuntimeError("receipt_corrupt", "pending receipt swap is invalid JSON") from exc
                return self._validate_payload(self.receipts / target_name, raw, payload, key)

            if pending is not None:
                public_raw, slot_raw = pending
            else:
                committed = read_committed_transitions(
                    self.receipts / target_name,
                    max_bytes=v2_json.MAX_JSON_ARTIFACT_BYTES,
                    replace_targets=replace_targets,
                )
                if committed:
                    public_raw, slot_raw, _transition_name = committed[0]
                else:
                    public_raw = read_bytes(
                        self.receipts / target_name,
                        code="receipt_corrupt",
                        max_bytes=v2_json.MAX_JSON_ARTIFACT_BYTES,
                    )
                    reserved = validate(public_raw)
                    if reserved.status != "reserved":
                        raise BatchRuntimeError("receipt_corrupt", "owner-only receipt is not reserved")
                    slot_raw = canonical_json_bytes(self._committed_receipt(reserved, key))
            public = validate(public_raw)
            staged = validate(slot_raw)
            if incoming_identity is not None and public.request_id == incoming_identity[0]:
                _request_id, command, fingerprint, requested_target = incoming_identity
                if (
                    public.command != command
                    or public.request_fingerprint != fingerprint
                    or public.requested_target != requested_target
                ):
                    raise BatchRuntimeError(
                        "idempotency_conflict",
                        "request id is already bound to a different pending operation or input",
                    )
            same_identity = (
                public.request_id == staged.request_id == target_name.removesuffix(".json")
                and public.command == staged.command
                and public.request_fingerprint == staged.request_fingerprint
                and public.requested_target == staged.requested_target
                and public.target == staged.target
                and public.plan == staged.plan
            )
            if not same_identity:
                raise BatchRuntimeError("receipt_corrupt", "pending receipt swap changes request identity")
            if public.status == "reserved" and staged.status == "committed":
                if slot_raw != canonical_json_bytes(self._committed_receipt(public, key)):
                    raise BatchRuntimeError(
                        "receipt_corrupt",
                        "pending committed receipt differs from its exact reserved plan",
                    )
                replace_bytes_atomic(
                    self.receipts / target_name,
                    slot_raw,
                    expected_current=public_raw,
                    transition_id=f"receipt:{public.request_id}:commit",
                    allowed_transition_targets=replace_targets,
                )
            elif public.status == "committed" and staged.status == "reserved":
                if public_raw != canonical_json_bytes(self._committed_receipt(staged, key)):
                    raise BatchRuntimeError(
                        "receipt_corrupt",
                        "committed receipt differs from its exact reserved plan",
                    )
                replace_bytes_atomic(
                    self.receipts / target_name,
                    public_raw,
                    expected_current=slot_raw,
                    transition_id=f"receipt:{public.request_id}:commit",
                    allowed_transition_targets=replace_targets,
                )
            else:
                raise BatchRuntimeError("receipt_corrupt", "pending receipt swap is not one commit transition")
        if recover:
            names = list_directory(self.receipts)
        return list_mutable_directory(
            self.receipts,
            replace_targets=replace_targets,
        )

    @staticmethod
    def _signature(key: bytes, receipt: RequestReceipt) -> str:
        payload = receipt.model_dump(mode="json")
        payload.pop("integrity_hmac")
        return hmac.new(key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()

    def _load(self, path: Path, key: bytes) -> RequestReceipt:
        raw, payload = read_json_bytes(path, code="receipt_corrupt")
        return self._validate_payload(path, raw, payload, key)

    def _validate_payload(
        self,
        path: Path,
        raw: bytes,
        payload: object,
        key: bytes,
    ) -> RequestReceipt:
        try:
            receipt = RequestReceipt.model_validate(payload)
        except ValidationError as exc:
            raise BatchRuntimeError("receipt_corrupt", f"invalid request receipt: {path}") from exc
        if raw != canonical_json_bytes(receipt):
            raise BatchRuntimeError("receipt_corrupt", f"request receipt is not canonical: {path}")
        if not hmac.compare_digest(receipt.integrity_hmac, self._signature(key, receipt)):
            raise BatchRuntimeError("receipt_corrupt", f"request receipt integrity check failed: {path}")
        return receipt

    def _pending_receipts(
        self,
        key: bytes,
        *,
        lock_descriptor: int,
    ) -> dict[str, tuple[Path, RequestReceipt, bytes]]:
        final_pattern = re.compile(r"^(?P<request_id>[0-9a-f-]{36})\.json$")
        temp_pattern = re.compile(
            r"^\.(?P<target>[0-9a-f-]{36}\.json)\.(?P<digest>[0-9a-f]{64})\.tmp$"
        )
        writing_pattern = re.compile(
            r"^\.(?P<target>[0-9a-f-]{36}\.json)\.[0-9a-f]{32}\.writing$"
        )
        names = self._list_receipts(key, recover=True)
        zero_writing = [
            name
            for name in names
            if writing_pattern.fullmatch(name)
            and internal_zero_tombstone(self.receipts / name)
        ]
        if len(zero_writing) > 64:
            raise BatchRuntimeError("resource_limit", "too many logical receipt tombstones")
        names = [name for name in names if name not in set(zero_writing)]
        classified: list[tuple[str, re.Match[str] | None, re.Match[str] | None]] = []
        for name in names:
            if final_pattern.fullmatch(name) is not None:
                continue
            temp_match = temp_pattern.fullmatch(name)
            writing_match = writing_pattern.fullmatch(name)
            match = temp_match or writing_match
            if match is None:
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    f"request receipt directory contains an unknown entry: {name}",
                )
            classified.append((name, temp_match, writing_match))

        # Validate every durable receipt before discarding any incomplete
        # runtime staging record. Corruption must remain a read-only failure.
        for name in names:
            final_match = final_pattern.fullmatch(name)
            if final_match is None:
                continue
            request_id = validate_request_id(final_match.group("request_id"))
            receipt = self._load(self.receipts / name, key)
            if receipt.request_id != request_id:
                raise BatchRuntimeError(
                    "receipt_corrupt",
                    "published receipt filename differs from its request id",
                )

        pending: dict[str, tuple[Path, RequestReceipt, bytes]] = {}
        incomplete_writes: list[tuple[Path, bytes]] = []
        for name, temp_match, writing_match in classified:
            match = temp_match or writing_match
            assert match is not None
            target_name = match.group("target")
            request_id = validate_request_id(target_name.removesuffix(".json"))
            path = self.receipts / name
            raw = read_bytes(
                path,
                code="receipt_corrupt",
                max_bytes=v2_json.MAX_JSON_ARTIFACT_BYTES,
            )
            if writing_match is not None:
                try:
                    payload = json.loads(
                        raw,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                    )
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    incomplete_writes.append((path, raw))
                    continue
                try:
                    receipt = RequestReceipt.model_validate(payload)
                except ValidationError:
                    incomplete_writes.append((path, raw))
                    continue
                receipt = self._validate_payload(path, raw, payload, key)
            else:
                receipt = self._load(path, key)
            if receipt.request_id != request_id:
                raise BatchRuntimeError("receipt_corrupt", "pending receipt request id differs from its target")
            if temp_match is not None and sha256_bytes(raw) != temp_match.group("digest"):
                raise BatchRuntimeError("receipt_corrupt", "pending receipt filename digest mismatch")
            if request_id in pending:
                raise BatchRuntimeError("receipt_corrupt", "multiple durable pending receipts exist for one request")
            pending[request_id] = (path, receipt, raw)
        if len(incomplete_writes) > 64:
            raise BatchRuntimeError("resource_limit", "too many immutable incomplete receipt attempts")
        return pending

    def _recover_unpublished_receipt(
        self,
        request_id: str,
        key: bytes,
        *,
        command: str,
        request_fingerprint: str,
        requested_target: str | None,
        pending_receipts: dict[str, tuple[Path, RequestReceipt, bytes]],
    ) -> bool:
        target = self._receipt_path(request_id)
        candidate = pending_receipts.get(request_id)
        if candidate is None:
            return False
        path, receipt, raw = candidate
        if (
            receipt.command != command
            or receipt.request_fingerprint != request_fingerprint
            or receipt.requested_target != requested_target
        ):
            raise BatchRuntimeError(
                "idempotency_conflict",
                "request id is already bound to a different pending operation or input",
            )
        promote_bytes_no_replace(path, target, raw)
        return True

    def _reserved_targets(
        self,
        key: bytes,
        pending_receipts: dict[str, tuple[Path, RequestReceipt, bytes]],
    ) -> set[str]:
        targets: set[str] = set()
        for name in self._list_receipts(key, recover=True):
            if name.endswith(".json"):
                receipt = self._load(self.receipts / name, key)
                if name != f"{receipt.request_id}.json":
                    raise BatchRuntimeError(
                        "receipt_corrupt",
                        "published receipt filename differs from its request id",
                    )
                targets.add(receipt.target)
        targets.update(receipt.target for _path, receipt, _raw in pending_receipts.values())
        return targets

    def _committed_receipt(self, receipt: RequestReceipt, key: bytes) -> RequestReceipt:
        result = receipt.plan.get("semantic_result")
        if not isinstance(result, dict):
            raise BatchRuntimeError("receipt_corrupt", "receipt plan is missing semantic_result")
        unsigned = receipt.model_copy(
            update={"status": "committed", "result": result, "integrity_hmac": "0" * 64}
        )
        return unsigned.model_copy(update={"integrity_hmac": self._signature(key, unsigned)})

    def _commit(
        self,
        receipt: RequestReceipt,
        key: bytes,
        *,
        fault: FaultHook | None = None,
    ) -> RequestReceipt:
        committed = self._committed_receipt(receipt, key)
        replace_bytes_atomic(
            self._receipt_path(receipt.request_id),
            canonical_json_bytes(committed),
            expected_current=canonical_json_bytes(receipt),
            transition_id=f"receipt:{receipt.request_id}:commit",
            allowed_transition_targets=self._transition_targets(),
            fault=fault,
        )
        return committed

    def execute(
        self,
        *,
        request_id: str,
        command: str,
        request_fingerprint: str,
        requested_target: Path | None,
        target_factory: TargetFactory,
        plan_factory: PlanFactory,
        publish: Publisher,
        inspect: Inspector,
        validate_input: InputValidator | None = None,
        fault: FaultHook | None = None,
    ) -> RequestOutcome:
        canonical_request_id = validate_request_id(request_id)
        normalized_requested_target = (
            str(normalized_absolute_path(requested_target)) if requested_target is not None else None
        )
        with locked_file(self.lock_path, create=True) as lock_descriptor:
            receipt_evidence = (
                list_directory(self.receipts)
                if entry_exists_allow_missing_parent(self.receipts)
                else []
            )
            integrity_key = initialize_locked_secret(
                lock_descriptor,
                allow_partial_reset=not receipt_evidence,
                fault=fault,
            )
            validate_locked_path(self.lock_path, lock_descriptor)
            ensure_directory(self.receipts)
            receipt_path = self._receipt_path(canonical_request_id)
            receipt_names = self._list_receipts(
                integrity_key,
                recover=True,
                incoming_identity=(
                    canonical_request_id,
                    command,
                    request_fingerprint,
                    normalized_requested_target,
                ),
            )
            pending_receipts = self._pending_receipts(
                integrity_key,
                lock_descriptor=lock_descriptor,
            )
            committed_ids = {
                name.removesuffix(".json")
                for name in receipt_names
                if name.endswith(".json")
            }
            replacement_ids = committed_ids.intersection(pending_receipts)
            for replacement_id in replacement_ids:
                published_path = self._receipt_path(replacement_id)
                published = self._load(published_path, integrity_key)
                pending_path, pending, pending_raw = pending_receipts[replacement_id]
                expected = self._committed_receipt(published, integrity_key)
                if (
                    published.status != "reserved"
                    or pending.status != "committed"
                    or pending_raw != canonical_json_bytes(expected)
                ):
                    raise BatchRuntimeError(
                        "receipt_corrupt",
                        "published and pending receipt are not one exact commit replacement",
                    )
                if replacement_id == canonical_request_id:
                    if (
                        published.command != command
                        or published.request_fingerprint != request_fingerprint
                        or published.requested_target != normalized_requested_target
                    ):
                        raise BatchRuntimeError(
                            "idempotency_conflict",
                            "request id is already bound to a different pending commit",
                        )
                    replace_bytes_atomic(
                        published_path,
                        pending_raw,
                        expected_current=canonical_json_bytes(published),
                        transition_id=f"receipt:{replacement_id}:commit",
                        allowed_transition_targets=self._transition_targets(),
                    )
                    pending_receipts.pop(replacement_id)
                    receipt_names = self._list_receipts(integrity_key, recover=True)
            replayed = receipt_path.name in receipt_names
            if not replayed:
                replayed = self._recover_unpublished_receipt(
                    canonical_request_id,
                    integrity_key,
                    command=command,
                    request_fingerprint=request_fingerprint,
                    requested_target=normalized_requested_target,
                    pending_receipts=pending_receipts,
                )
            if replayed:
                receipt = self._load(receipt_path, integrity_key)
                if receipt.request_id != canonical_request_id:
                    raise BatchRuntimeError(
                        "receipt_corrupt",
                        "published receipt request id differs from its filename",
                    )
                if normalized_requested_target is not None and receipt.target != normalized_requested_target:
                    raise BatchRuntimeError("receipt_corrupt", "receipt target differs from explicit requested target")
                if (
                    receipt.command != command
                    or receipt.request_fingerprint != request_fingerprint
                    or receipt.requested_target != normalized_requested_target
                ):
                    raise BatchRuntimeError(
                        "idempotency_conflict",
                        "request id is already bound to a different operation or input",
                    )
                if validate_input is not None:
                    validate_input()
                if receipt.status == "committed":
                    if receipt.result is None:
                        raise BatchRuntimeError("receipt_corrupt", "committed receipt is missing result")
                    return RequestOutcome(result=receipt.result, replayed=True)
            else:
                if validate_input is not None:
                    validate_input()
                target = normalized_absolute_path(
                    target_factory(self._reserved_targets(integrity_key, pending_receipts))
                )
                plan = plan_factory(target)
                result = plan.get("semantic_result")
                if not isinstance(result, dict):
                    raise BatchRuntimeError("internal_error", "request plan is missing semantic_result")
                unsigned = RequestReceipt(
                    request_id=canonical_request_id,
                    command=command,
                    request_fingerprint=request_fingerprint,
                    requested_target=normalized_requested_target,
                    target=str(target),
                    status="reserved",
                    plan=plan,
                    result=None,
                    integrity_hmac="0" * 64,
                )
                receipt = unsigned.model_copy(update={"integrity_hmac": self._signature(integrity_key, unsigned)})
                reserved_raw = canonical_json_bytes(receipt)
                committed_raw = canonical_json_bytes(self._committed_receipt(receipt, integrity_key))
                json_limit = v2_json.MAX_JSON_ARTIFACT_BYTES
                if len(reserved_raw) > json_limit or len(committed_raw) > json_limit:
                    raise BatchRuntimeError(
                        "resource_limit",
                        "reserved or committed request receipt exceeds the JSON artifact limit",
                        details={
                            "reserved_size_bytes": len(reserved_raw),
                            "committed_size_bytes": len(committed_raw),
                            "max_bytes": json_limit,
                        },
                    )
                validate_locked_path(self.lock_path, lock_descriptor)
                publish_bytes_no_replace(receipt_path, reserved_raw, fault=fault)
                if fault is not None:
                    fault("receipt_reserved")

            target = Path(receipt.target)
            if replayed and inspect(target, receipt.plan):
                pass
            else:
                if fault is not None:
                    fault("before_publish")
                validate_locked_path(self.lock_path, lock_descriptor)
                publish(target, receipt.plan, replayed)
                if fault is not None:
                    fault("after_publish")
                if not inspect(target, receipt.plan):
                    raise BatchRuntimeError("publication_incomplete", f"publication did not verify: {target}")
            validate_locked_path(self.lock_path, lock_descriptor)
            committed = self._commit(receipt, integrity_key, fault=fault)
            if fault is not None:
                fault("receipt_committed")
            if committed.result is None:  # pragma: no cover - guarded by _commit
                raise BatchRuntimeError("receipt_corrupt", "committed receipt is missing result")
            return RequestOutcome(result=committed.result, replayed=replayed)
