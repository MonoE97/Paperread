from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from paper_reader_batch.v2_journal import RunView


Lane = Literal["run", "worker", "local_prepare", "write", "user"]


class NextActionJson(TypedDict):
    item_id: str | None
    lane: Lane
    action: str
    reason_code: str
    requires_user_intent: bool
    required_inputs: list[str]


@dataclass(frozen=True)
class _NextAction:
    item_id: str | None
    lane: Lane
    action: str
    reason_code: str
    requires_user_intent: bool = False
    required_inputs: tuple[str, ...] = ()

    def as_json(self) -> NextActionJson:
        return {
            "item_id": self.item_id,
            "lane": self.lane,
            "action": self.action,
            "reason_code": self.reason_code,
            "requires_user_intent": self.requires_user_intent,
            "required_inputs": list(self.required_inputs),
        }


# This tuple is the sole action-order authority.  Never sort on localized
# explanations, identities, clocks, or filesystem state.
ACTION_PRIORITY: tuple[str, ...] = (
    "run.recover",
    "worker.claim",
    "worker.prompt",
    "worker.renew",
    "worker.finish",
    "worker.release",
    "worker.retry",
    "local-prepare.claim",
    "local-prepare.run",
    "local-prepare.renew",
    "local-prepare.finish",
    "local-prepare.release",
    "write.claim",
    "write.preview",
    "write.renew",
    "write.release",
    "write.commit",
    "write.mark-uncertain",
    "write.reconcile",
    "write.retry",
    "user.confirm-zotero-write-intent",
    "user.verify-zotero-write-read-only",
    "user.resolve-write-blocker",
    "user.resolve-run-storage-attention",
)

_ACTION_RANK = {action: index for index, action in enumerate(ACTION_PRIORITY)}
_LANE_RANK: dict[Lane, int] = {
    "run": -1,
    "worker": 0,
    "local_prepare": 1,
    "write": 2,
    "user": 3,
}

_WORKER_IDENTITY = ("worker_id", "claim_id", "lease_token", "attempt_id")
_WRITE_IDENTITY = ("writer_id", "claim_id", "lease_token", "write_attempt_id")
_SAFE_WHILE_PENDING = {
    "worker.prompt",
    "write.preview",
    "user.verify-zotero-write-read-only",
}

_PENDING_PUBLIC_ACTION: dict[str, str] = {
    "local-prepare.run.reserve": "local-prepare.run",
}
_PENDING_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    # Pending proposals must be replayed with the exact originating request
    # fingerprint.  Include optional public inputs that participate in that
    # fingerprint so a non-default original value is never silently replaced
    # by the CLI default during recovery.
    "run.recover": (
        "request_id",
        "paper_reader_root",
        "reconciliation_timeout_seconds",
    ),
    "worker.claim": ("worker_id", "request_id", "limit", "lease_seconds"),
    "worker.renew": (*_WORKER_IDENTITY, "request_id", "lease_seconds"),
    "worker.finish": (*_WORKER_IDENTITY, "result", "request_id"),
    "worker.release": (
        *_WORKER_IDENTITY,
        "acknowledge_no_side_effects",
        "request_id",
    ),
    "worker.retry": ("request_id",),
    "local-prepare.claim": (
        "worker_id",
        "request_id",
        "limit",
        "lease_seconds",
    ),
    "local-prepare.renew": (*_WORKER_IDENTITY, "request_id", "lease_seconds"),
    "local-prepare.finish": (*_WORKER_IDENTITY, "result", "request_id"),
    "local-prepare.release": (
        *_WORKER_IDENTITY,
        "acknowledge_no_side_effects",
        "request_id",
    ),
    "local-prepare.run": (
        *_WORKER_IDENTITY,
        "paper_reader_root",
        "request_id",
        "timeout_seconds",
    ),
    "write.claim": ("writer_id", "request_id", "lease_seconds"),
    "write.renew": (*_WRITE_IDENTITY, "request_id", "lease_seconds"),
    "write.release": (*_WRITE_IDENTITY, "request_id"),
    "write.begin": (*_WRITE_IDENTITY, "authorization", "request_id"),
    "write.commit": (*_WRITE_IDENTITY, "result", "request_id"),
    "write.mark-uncertain": (*_WRITE_IDENTITY, "reason", "request_id"),
    "write.reconcile": ("readback", "request_id"),
    "write.retry": ("acknowledge_no_match", "request_id"),
}


def _is_derived_run_reconciliation(view: RunView, *, item_id: str | None) -> bool:
    pending = view.pending_event
    if (
        pending is None
        or pending.event.command != "write.reconcile"
        or item_id is None
    ):
        return False
    from paper_reader_batch.v2_run import derived_reconciliation_request_id

    for outer in getattr(view, "events", ()):
        if outer.command != "run.recover":
            continue
        data = outer.data
        target_item_id = None
        if getattr(data, "kind", None) == "write.lease_expired_uncertain":
            target_item_id = getattr(data, "item_id", None)
        elif getattr(data, "kind", None) == "run.recovered":
            target_item_id = getattr(
                getattr(data, "reconciliation_write", None),
                "item_id",
                None,
            )
        if (
            target_item_id == item_id
            and derived_reconciliation_request_id(outer.request_id)
            == pending.event.request_id
        ):
            return True
    return False


def _pending_action(view: RunView) -> _NextAction | None:
    pending = view.pending_event
    if pending is None:
        return None
    internal_command = pending.event.command
    item_id = getattr(getattr(pending.event, "data", None), "item_id", None)
    if _is_derived_run_reconciliation(view, item_id=item_id):
        # The public outer run.recover request cannot adopt a pending event
        # owned by its derived write.reconcile request id, while status cannot
        # reconstruct the exact private readback input needed to resume that
        # inner request.  Do not advertise either command as executable.
        return _NextAction(
            item_id=item_id,
            lane="user",
            action="user.resolve-run-storage-attention",
            reason_code="pending_run_recovery_reconciliation_manual_attention",
            requires_user_intent=True,
            required_inputs=("maintainer_decision",),
        )
    action = _PENDING_PUBLIC_ACTION.get(internal_command, internal_command)
    if internal_command == "local-prepare.finish" and item_id is not None:
        state_item = next(
            (item for item in view.state.items if item.item_id == item_id),
            None,
        )
        if (
            state_item is not None
            and getattr(state_item, "local_prepare_coordination_request_id", None)
            == getattr(pending.event, "request_id", None)
        ):
            action = "local-prepare.run"
    required_inputs = _PENDING_REQUIRED_INPUTS.get(action)
    if required_inputs is None:
        return _NextAction(
            item_id=None,
            lane="user",
            action="user.resolve-run-storage-attention",
            reason_code="pending_internal_request_manual_attention",
            requires_user_intent=True,
            required_inputs=("maintainer_decision",),
        )
    if internal_command == "local-prepare.run.reserve" and item_id is None:
        claimed = [
            item.item_id
            for item in view.state.items
            if item.local_prepare_status == "claimed"
        ]
        item_id = claimed[0] if len(claimed) == 1 else None
    public_group = action.partition(".")[0]
    lane_by_group: dict[str, Lane] = {
        "run": "run",
        "worker": "worker",
        "local-prepare": "local_prepare",
        "write": "write",
    }
    lane = lane_by_group.get(public_group)
    if lane is None:  # pragma: no cover - required-input map is closed above
        return _NextAction(
            item_id=None,
            lane="user",
            action="user.resolve-run-storage-attention",
            reason_code="pending_internal_request_manual_attention",
            requires_user_intent=True,
            required_inputs=("maintainer_decision",),
        )
    return _NextAction(
        item_id=item_id,
        lane=lane,
        action=action,
        reason_code="pending_event_originating_request_replay_required",
        requires_user_intent=action
        in {
            "worker.release",
            "worker.retry",
            "local-prepare.release",
            "write.begin",
            "write.retry",
        },
        required_inputs=required_inputs,
    )


def _append_worker_actions(
    actions: list[_NextAction],
    item,
    *,
    claimable_item_id: str | None,
    coordination_uncertain: bool,
) -> None:
    status = item.worker_status
    if status == "queued":
        if (
            item.item_id == claimable_item_id
            and item.local_prepare_status != "claimed"
            and not coordination_uncertain
        ):
            actions.append(
                _NextAction(
                    item_id=item.item_id,
                    lane="worker",
                    action="worker.claim",
                    reason_code="worker_queued",
                    required_inputs=("worker_id", "request_id"),
                )
            )
    elif status == "claimed":
        actions.extend(
            (
                _NextAction(
                    item.item_id,
                    "worker",
                    "worker.prompt",
                    "worker_claimed",
                    required_inputs=_WORKER_IDENTITY,
                ),
                _NextAction(
                    item.item_id,
                    "worker",
                    "worker.renew",
                    "worker_claimed",
                    required_inputs=(*_WORKER_IDENTITY, "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "worker",
                    "worker.finish",
                    "worker_claimed",
                    required_inputs=(*_WORKER_IDENTITY, "result", "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "worker",
                    "worker.release",
                    "worker_claimed_no_side_effects_only",
                    requires_user_intent=True,
                    required_inputs=(
                        *_WORKER_IDENTITY,
                        "acknowledge_no_side_effects",
                        "request_id",
                    ),
                ),
            )
        )
    elif status in {"failed", "blocked"}:
        actions.append(
            _NextAction(
                item.item_id,
                "worker",
                "worker.retry",
                f"worker_{status}",
                requires_user_intent=True,
                required_inputs=("request_id",),
            )
        )


def _append_local_prepare_actions(
    actions: list[_NextAction],
    item,
    *,
    claimable_item_id: str | None,
    coordination_uncertain: bool,
) -> None:
    if item.input_type != "pdf_path":
        return
    status = item.local_prepare_status
    if status == "claimed":
        actions.extend(
            (
                _NextAction(
                    item.item_id,
                    "local_prepare",
                    "local-prepare.run",
                    "local_prepare_claimed",
                    required_inputs=(
                        *_WORKER_IDENTITY,
                        "paper_reader_root",
                        "request_id",
                    ),
                ),
                _NextAction(
                    item.item_id,
                    "local_prepare",
                    "local-prepare.renew",
                    "local_prepare_claimed",
                    required_inputs=(*_WORKER_IDENTITY, "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "local_prepare",
                    "local-prepare.finish",
                    "local_prepare_claimed",
                    required_inputs=(*_WORKER_IDENTITY, "result", "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "local_prepare",
                    "local-prepare.release",
                    "local_prepare_claimed_no_side_effects_only",
                    requires_user_intent=True,
                    required_inputs=(
                        *_WORKER_IDENTITY,
                        "acknowledge_no_side_effects",
                        "request_id",
                    ),
                ),
            )
        )
    elif (
        status in {"queued", "failed", "blocked"}
        and item.item_id == claimable_item_id
        and item.worker_status != "claimed"
        and not coordination_uncertain
    ):
        actions.append(
            _NextAction(
                item.item_id,
                "local_prepare",
                "local-prepare.claim",
                f"local_prepare_{status}",
                required_inputs=("worker_id", "request_id"),
            )
        )


def _append_write_actions(
    actions: list[_NextAction],
    item,
    *,
    write_policy: str,
    first_claimable_write_id: str | None,
) -> None:
    if item.input_type == "pdf_path" or write_policy != "zotero_write":
        return
    status = item.write_status
    if status == "queued" and item.item_id == first_claimable_write_id:
        actions.append(
            _NextAction(
                item.item_id,
                "write",
                "write.claim",
                "write_queued",
                required_inputs=("writer_id", "request_id"),
            )
        )
    elif status == "claimed":
        actions.extend(
            (
                _NextAction(
                    item.item_id,
                    "write",
                    "write.preview",
                    "write_claimed_preview_required",
                    required_inputs=_WRITE_IDENTITY,
                ),
                _NextAction(
                    item.item_id,
                    "write",
                    "write.renew",
                    "write_claimed",
                    required_inputs=(*_WRITE_IDENTITY, "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "write",
                    "write.release",
                    "write_claimed_before_begin_only",
                    required_inputs=(*_WRITE_IDENTITY, "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "user",
                    "user.confirm-zotero-write-intent",
                    "explicit_real_write_intent_required",
                    requires_user_intent=True,
                    required_inputs=("candidate_preview", "explicit_real_write_intent"),
                ),
            )
        )
    elif status == "started":
        actions.extend(
            (
                _NextAction(
                    item.item_id,
                    "write",
                    "write.renew",
                    "write_started_never_resend",
                    required_inputs=(*_WRITE_IDENTITY, "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "write",
                    "write.commit",
                    "exact_external_write_result_available",
                    required_inputs=(*_WRITE_IDENTITY, "result", "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "write",
                    "write.mark-uncertain",
                    "external_write_result_unknown",
                    required_inputs=(*_WRITE_IDENTITY, "reason", "request_id"),
                ),
                _NextAction(
                    item.item_id,
                    "user",
                    "user.verify-zotero-write-read-only",
                    "write_started_never_resend",
                    required_inputs=("authorization", "note_key"),
                ),
            )
        )
    elif status == "uncertain":
        actions.append(
            _NextAction(
                item.item_id,
                "write",
                "write.reconcile",
                "write_uncertain_read_only_reconciliation",
                required_inputs=("readback", "request_id"),
            )
        )
    elif status == "retry_confirmation_required":
        actions.append(
            _NextAction(
                item.item_id,
                "write",
                "write.retry",
                "write_retry_confirmation_required",
                requires_user_intent=True,
                required_inputs=("acknowledge_no_match", "request_id"),
            )
        )
    elif status == "blocked":
        actions.append(
            _NextAction(
                item.item_id,
                "user",
                "user.resolve-write-blocker",
                "write_blocked",
                requires_user_intent=True,
                required_inputs=("maintainer_decision",),
            )
        )


def derive_next_actions(view: RunView) -> list[NextActionJson]:
    """Derive read-only action candidates from one already validated run view."""

    pending = _pending_action(view)

    actions: list[_NextAction] = []
    if view.snapshot_status != "current":
        actions.append(
            _NextAction(
                None,
                "run",
                "run.recover",
                "snapshot_repair_required",
                required_inputs=("request_id",),
            )
        )

    storage_attention = (
        (
            bool(view.incomplete_event_writes),
            "incomplete_event_write_attention",
        ),
        (
            view.state_pending_write is not None
            and view.snapshot_status == "current",
            "pending_state_write_attention",
        ),
        (
            bool(view.incomplete_state_writes),
            "incomplete_state_write_attention",
        ),
    )
    for present, reason_code in storage_attention:
        if present:
            actions.append(
                _NextAction(
                    None,
                    "user",
                    "user.resolve-run-storage-attention",
                    reason_code,
                    requires_user_intent=True,
                    required_inputs=("maintainer_decision",),
                )
            )

    state_by_id = {item.item_id: item for item in view.state.items}
    manifest_index = {
        manifest_item.item_id: index
        for index, manifest_item in enumerate(view.manifest.items)
    }
    active_task_claims = sum(
        item.worker_status == "claimed" or item.local_prepare_status == "claimed"
        for item in view.state.items
    )
    claim_capacity_available = active_task_claims < view.manifest.default_concurrency
    first_runtime_worker_claim_id = None
    first_local_prepare_claim_id = None
    if claim_capacity_available:
        first_runtime_worker_claim_id = next(
            (
                manifest_item.item_id
                for manifest_item in view.manifest.items
                if state_by_id[manifest_item.item_id].worker_status == "queued"
                and state_by_id[manifest_item.item_id].local_prepare_status != "claimed"
                and not (
                    state_by_id[manifest_item.item_id].local_prepare_status
                    == "blocked"
                    and state_by_id[manifest_item.item_id].local_prepare_failure_code
                    == "coordination_uncertain"
                )
            ),
            None,
        )
        first_local_prepare_claim_id = next(
            (
                manifest_item.item_id
                for manifest_item in view.manifest.items
                if state_by_id[manifest_item.item_id].input_type == "pdf_path"
                and state_by_id[manifest_item.item_id].local_prepare_status
                in {"queued", "failed", "blocked"}
                and state_by_id[manifest_item.item_id].worker_status != "claimed"
                and not (
                    state_by_id[manifest_item.item_id].local_prepare_status == "blocked"
                    and state_by_id[manifest_item.item_id].local_prepare_failure_code
                    == "coordination_uncertain"
                )
            ),
            None,
        )
    serial_write_busy = any(
        item.write_status in {"claimed", "started", "uncertain"}
        for item in view.state.items
    )
    first_claimable_write_id = None
    if view.manifest.write_policy == "zotero_write" and not serial_write_busy:
        first_claimable_write_id = next(
            (
                manifest_item.item_id
                for manifest_item in view.manifest.items
                if state_by_id[manifest_item.item_id].write_status == "queued"
            ),
            None,
        )

    for manifest_item in view.manifest.items:
        item = state_by_id[manifest_item.item_id]
        coordination_uncertain = (
            item.input_type == "pdf_path"
            and item.local_prepare_status == "blocked"
            and item.local_prepare_failure_code == "coordination_uncertain"
        )
        if coordination_uncertain:
            actions.append(
                _NextAction(
                    item.item_id,
                    "run",
                    "run.recover",
                    "local_prepare_coordination_uncertain_recovery_if_expired",
                    required_inputs=("request_id",),
                )
            )
        if item.worker_status == "claimed":
            actions.append(
                _NextAction(
                    item.item_id,
                    "run",
                    "run.recover",
                    "worker_lease_expiry_recovery_if_expired",
                    required_inputs=("request_id",),
                )
            )
        if item.local_prepare_status == "claimed":
            actions.append(
                _NextAction(
                    item.item_id,
                    "run",
                    "run.recover",
                    "local_prepare_lease_expiry_recovery_if_expired",
                    required_inputs=("request_id",),
                )
            )
        if item.write_status == "claimed":
            actions.append(
                _NextAction(
                    item.item_id,
                    "run",
                    "run.recover",
                    "write_claim_lease_expiry_recovery_if_expired",
                    required_inputs=("request_id",),
                )
            )
        elif item.write_status == "started":
            actions.append(
                _NextAction(
                    item.item_id,
                    "run",
                    "run.recover",
                    "write_started_lease_expiry_uncertain_if_expired",
                    required_inputs=("request_id",),
                )
            )
        if item.write_status == "uncertain":
            actions.append(
                _NextAction(
                    item.item_id,
                    "run",
                    "run.recover",
                    "write_uncertain_read_only_recovery",
                    required_inputs=("paper_reader_root", "request_id"),
                )
            )
        _append_worker_actions(
            actions,
            item,
            claimable_item_id=first_runtime_worker_claim_id,
            coordination_uncertain=coordination_uncertain,
        )
        _append_local_prepare_actions(
            actions,
            item,
            claimable_item_id=first_local_prepare_claim_id,
            coordination_uncertain=coordination_uncertain,
        )
        _append_write_actions(
            actions,
            item,
            write_policy=view.manifest.write_policy,
            first_claimable_write_id=first_claimable_write_id,
        )

    actions.sort(
        key=lambda candidate: (
            0 if candidate.lane == "run" else 1,
            -1 if candidate.item_id is None else manifest_index[candidate.item_id],
            _LANE_RANK[candidate.lane],
            _ACTION_RANK[candidate.action],
        )
    )
    if pending is not None:
        safe_read_only = [
            candidate
            for candidate in actions
            if candidate.action in _SAFE_WHILE_PENDING
        ]
        return [pending.as_json(), *(candidate.as_json() for candidate in safe_read_only)]
    return [candidate.as_json() for candidate in actions]


__all__ = ["ACTION_PRIORITY", "NextActionJson", "derive_next_actions"]
