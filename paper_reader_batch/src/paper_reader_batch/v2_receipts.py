from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Callable, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from paper_reader_batch.v2_contracts import AbsolutePath, Sha256, StrictModel, UuidString
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    canonical_json_bytes,
    ensure_directory,
    entry_exists_allow_missing_parent,
    initialize_locked_secret,
    list_directory,
    locked_file,
    normalized_absolute_path,
    publish_bytes_no_replace,
    promote_bytes_no_replace,
    read_bytes,
    read_json_bytes,
    replace_bytes_atomic,
    sha256_bytes,
    unlink_regular_exact,
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
        names = list_directory(self.receipts)
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
            raw = read_bytes(path, code="receipt_corrupt")
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
        if incomplete_writes:
            validate_locked_path(self.lock_path, lock_descriptor)
            for path, raw in incomplete_writes:
                unlink_regular_exact(path, raw)
            validate_locked_path(self.lock_path, lock_descriptor)
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
        for name in list_directory(self.receipts):
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
            receipt_names = list_directory(self.receipts)
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
                    )
                    pending_receipts.pop(replacement_id)
                    receipt_names = list_directory(self.receipts)
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
                if receipt.status == "committed":
                    if receipt.result is None:
                        raise BatchRuntimeError("receipt_corrupt", "committed receipt is missing result")
                    return RequestOutcome(result=receipt.result, replayed=True)
            else:
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
                validate_locked_path(self.lock_path, lock_descriptor)
                publish_bytes_no_replace(receipt_path, canonical_json_bytes(receipt), fault=fault)
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
