from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from typer.main import get_command

from paper_reader_batch.v2_cli import app
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_next_actions import ACTION_PRIORITY, derive_next_actions
from paper_reader_batch.v2_run import (
    derived_reconciliation_request_id,
    initialize_run,
    recover_run,
)
from paper_reader_batch.v2_worker import claim_worker, worker_prompt


RUN_ID = "11111111-1111-4111-8111-111111111111"
CLAIM_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"


def _item(
    item_id: str,
    *,
    input_type: str = "zotero_item",
    worker: str = "queued",
    local_prepare: str = "not_applicable",
    write: str = "awaiting_candidate",
    local_failure_code: str | None = None,
    local_coordination_request_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        item_id=item_id,
        input_type=input_type,
        worker_status=worker,
        local_prepare_status=local_prepare,
        local_prepare_failure_code=local_failure_code,
        local_prepare_coordination_request_id=local_coordination_request_id,
        write_status=write,
    )


def _view(
    items: list[SimpleNamespace],
    *,
    manifest_order: list[str] | None = None,
    write_policy: str = "zotero_write",
    default_concurrency: int = 3,
    snapshot_status: str = "current",
    pending_command: str | None = None,
    pending_request_id: str | None = None,
    pending_item_id: str | None = None,
    incomplete_event_writes: tuple[str, ...] = (),
    state_pending_write: str | None = None,
    incomplete_state_writes: tuple[str, ...] = (),
    committed_events: tuple[SimpleNamespace, ...] = (),
) -> SimpleNamespace:
    by_id = {item.item_id: item for item in items}
    order = manifest_order or [item.item_id for item in items]
    manifest_items = [
        SimpleNamespace(item_id=item_id, input_type=by_id[item_id].input_type)
        for item_id in order
    ]
    pending = None
    if pending_command is not None:
        pending = SimpleNamespace(
            event=SimpleNamespace(
                command=pending_command,
                sequence=7,
                request_id=pending_request_id,
                data=SimpleNamespace(item_id=pending_item_id)
                if pending_item_id is not None
                else SimpleNamespace(),
            )
        )
    return SimpleNamespace(
        manifest=SimpleNamespace(
            items=manifest_items,
            write_policy=write_policy,
            default_concurrency=default_concurrency,
        ),
        state=SimpleNamespace(items=items),
        snapshot_status=snapshot_status,
        events=list(committed_events),
        pending_event=pending,
        incomplete_event_writes=incomplete_event_writes,
        state_pending_write=state_pending_write,
        incomplete_state_writes=incomplete_state_writes,
    )


def _action(
    item_id: str | None,
    lane: str,
    action: str,
    reason_code: str,
    required_inputs: list[str],
    *,
    requires_user_intent: bool = False,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "lane": lane,
        "action": action,
        "reason_code": reason_code,
        "requires_user_intent": requires_user_intent,
        "required_inputs": required_inputs,
    }


def test_queued_pdf_advertises_both_legal_lanes_as_alternatives() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare="queued",
                write="not_applicable",
            )
        ]
    )

    assert derive_next_actions(view) == [
        _action("001", "worker", "worker.claim", "worker_queued", ["worker_id", "request_id"]),
        _action(
            "001",
            "local_prepare",
            "local-prepare.claim",
            "local_prepare_queued",
            ["worker_id", "request_id"],
        ),
    ]


@pytest.mark.parametrize("worker_status", ["failed", "blocked"])
def test_inactive_worker_failure_advertises_only_explicit_retry(
    worker_status: str,
) -> None:
    view = _view([_item("001", worker=worker_status)])

    assert derive_next_actions(view) == [
        _action(
            "001",
            "worker",
            "worker.retry",
            f"worker_{worker_status}",
            ["request_id"],
            requires_user_intent=True,
        )
    ]


def test_worker_claimed_uses_existing_attempt_and_suppresses_same_pdf_local_claim() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="claimed",
                local_prepare="queued",
                write="not_applicable",
            )
        ]
    )

    identity = ["worker_id", "claim_id", "lease_token", "attempt_id"]
    assert derive_next_actions(view) == [
        _action(
            "001",
            "run",
            "run.recover",
            "worker_lease_expiry_recovery_if_expired",
            ["request_id"],
        ),
        _action("001", "worker", "worker.prompt", "worker_claimed", identity),
        _action("001", "worker", "worker.renew", "worker_claimed", [*identity, "request_id"]),
        _action(
            "001",
            "worker",
            "worker.finish",
            "worker_claimed",
            [*identity, "result", "request_id"],
        ),
        _action(
            "001",
            "worker",
            "worker.release",
            "worker_claimed_no_side_effects_only",
            [*identity, "acknowledge_no_side_effects", "request_id"],
            requires_user_intent=True,
        ),
    ]


@pytest.mark.parametrize(
    ("local_status", "failure_code", "expected_actions"),
    [
        (
            "claimed",
            None,
            [
                ("local-prepare.run", "local_prepare_claimed", False),
                ("local-prepare.renew", "local_prepare_claimed", False),
                ("local-prepare.finish", "local_prepare_claimed", False),
                (
                    "local-prepare.release",
                    "local_prepare_claimed_no_side_effects_only",
                    True,
                ),
            ],
        ),
        ("failed", "child_execution_failed", [("local-prepare.claim", "local_prepare_failed", False)]),
        ("blocked", "manual_block", [("local-prepare.claim", "local_prepare_blocked", False)]),
        ("prepared", None, []),
    ],
)
def test_local_prepare_states_match_public_entry_points(
    local_status: str,
    failure_code: str | None,
    expected_actions: list[tuple[str, str, bool]],
) -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare=local_status,
                local_failure_code=failure_code,
                write="not_applicable",
            )
        ]
    )

    actions = [
        entry
        for entry in derive_next_actions(view)
        if entry["lane"] == "local_prepare"
    ]
    assert [(entry["action"], entry["reason_code"], entry["requires_user_intent"]) for entry in actions] == expected_actions
    if local_status == "claimed":
        assert actions[0]["required_inputs"] == [
            "worker_id",
            "claim_id",
            "lease_token",
            "attempt_id",
            "paper_reader_root",
            "request_id",
        ]
        assert actions[-1]["required_inputs"][-2:] == [
            "acknowledge_no_side_effects",
            "request_id",
        ]


def test_coordination_uncertain_requires_same_attempt_recovery_not_new_local_claim() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare="blocked",
                local_failure_code="coordination_uncertain",
                write="not_applicable",
            )
        ]
    )

    assert derive_next_actions(view) == [
        _action(
            "001",
            "run",
            "run.recover",
            "local_prepare_coordination_uncertain_recovery_if_expired",
            ["request_id"],
        )
    ]


@pytest.mark.parametrize(
    ("write_status", "expected"),
    [
        (
            "claimed",
            [
                ("run", "run.recover", "write_claim_lease_expiry_recovery_if_expired", False),
                ("write", "write.preview", "write_claimed_preview_required", False),
                ("write", "write.renew", "write_claimed", False),
                ("write", "write.release", "write_claimed_before_begin_only", False),
                ("user", "user.confirm-zotero-write-intent", "explicit_real_write_intent_required", True),
            ],
        ),
        (
            "started",
            [
                ("run", "run.recover", "write_started_lease_expiry_uncertain_if_expired", False),
                ("write", "write.renew", "write_started_never_resend", False),
                ("write", "write.commit", "exact_external_write_result_available", False),
                ("write", "write.mark-uncertain", "external_write_result_unknown", False),
                ("user", "user.verify-zotero-write-read-only", "write_started_never_resend", False),
            ],
        ),
        (
            "retry_confirmation_required",
            [
                ("write", "write.retry", "write_retry_confirmation_required", True),
            ],
        ),
        (
            "blocked",
            [
                ("user", "user.resolve-write-blocker", "write_blocked", True),
            ],
        ),
        ("prepared_only", []),
        ("written", []),
        ("not_applicable", []),
        ("awaiting_candidate", []),
    ],
)
def test_write_states_never_suggest_unsafe_resend(
    write_status: str,
    expected: list[tuple[str, str, str, bool]],
) -> None:
    view = _view([_item("001", worker="succeeded", write=write_status)])

    actions = derive_next_actions(view)
    assert [
        (
            entry["lane"],
            entry["action"],
            entry["reason_code"],
            entry["requires_user_intent"],
        )
        for entry in actions
    ] == expected
    assert all(entry["action"] != "write.begin" for entry in actions)
    assert all("authorization" not in entry for entry in actions)
    if write_status == "started":
        verify = next(
            entry
            for entry in actions
            if entry["action"] == "user.verify-zotero-write-read-only"
        )
        assert verify["required_inputs"] == ["authorization", "note_key"]


def test_uncertain_write_offers_only_read_only_reconciliation_paths() -> None:
    view = _view([_item("001", worker="succeeded", write="uncertain")])

    assert derive_next_actions(view) == [
        _action(
            "001",
            "run",
            "run.recover",
            "write_uncertain_read_only_recovery",
            ["paper_reader_root", "request_id"],
        ),
        _action(
            "001",
            "write",
            "write.reconcile",
            "write_uncertain_read_only_reconciliation",
            ["readback", "request_id"],
        ),
    ]


def test_serial_write_lane_only_advertises_first_claimable_manifest_item() -> None:
    view = _view(
        [
            _item("003", worker="succeeded", write="queued"),
            _item("001", worker="succeeded", write="queued"),
            _item("002", worker="succeeded", write="written"),
        ],
        manifest_order=["001", "002", "003"],
    )

    assert derive_next_actions(view) == [
        _action("001", "write", "write.claim", "write_queued", ["writer_id", "request_id"])
    ]


@pytest.mark.parametrize("active", ["claimed", "started", "uncertain"])
def test_serial_write_lane_suppresses_other_queued_claims(active: str) -> None:
    view = _view(
        [
            _item("001", worker="succeeded", write=active),
            _item("002", worker="succeeded", write="queued"),
        ]
    )

    assert all(
        not (entry["item_id"] == "002" and entry["action"] == "write.claim")
        for entry in derive_next_actions(view)
    )


def test_attention_state_does_not_block_another_item_from_using_free_write_lane() -> None:
    view = _view(
        [
            _item("001", worker="succeeded", write="blocked"),
            _item("002", worker="succeeded", write="queued"),
        ]
    )

    actions = derive_next_actions(view)
    assert ("002", "write.claim") in [
        (entry["item_id"], entry["action"]) for entry in actions
    ]


def test_shared_worker_local_concurrency_suppresses_new_claims_at_capacity() -> None:
    view = _view(
        [
            _item("001", worker="claimed"),
            _item(
                "002",
                input_type="pdf_path",
                worker="queued",
                local_prepare="queued",
                write="not_applicable",
            ),
        ],
        default_concurrency=1,
    )

    assert all(
        not (
            entry["item_id"] == "002"
            and entry["action"] in {"worker.claim", "local-prepare.claim"}
        )
        for entry in derive_next_actions(view)
    )


def test_global_claim_commands_only_identify_their_first_actual_assignment() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare="queued",
                write="not_applicable",
            ),
            _item(
                "002",
                input_type="pdf_path",
                worker="queued",
                local_prepare="queued",
                write="not_applicable",
            ),
        ],
        default_concurrency=1,
    )

    claim_actions = [
        entry
        for entry in derive_next_actions(view)
        if entry["action"] in {"worker.claim", "local-prepare.claim"}
    ]

    assert [(entry["item_id"], entry["action"]) for entry in claim_actions] == [
        ("001", "worker.claim"),
        ("001", "local-prepare.claim"),
    ]


def test_mixed_actions_use_manifest_then_lane_then_fixed_action_priority() -> None:
    view = _view(
        [
            _item("003", worker="succeeded", write="queued"),
            _item(
                "002",
                input_type="pdf_path",
                worker="queued",
                local_prepare="queued",
                write="not_applicable",
            ),
            _item("001", worker="queued"),
        ],
        manifest_order=["001", "002", "003"],
    )

    actions = derive_next_actions(view)
    assert [(entry["item_id"], entry["lane"], entry["action"]) for entry in actions] == [
        ("001", "worker", "worker.claim"),
        ("002", "local_prepare", "local-prepare.claim"),
        ("003", "write", "write.claim"),
    ]
    assert isinstance(ACTION_PRIORITY, tuple)
    assert len(ACTION_PRIORITY) == len(set(ACTION_PRIORITY))


def test_prepare_only_and_all_local_runs_never_offer_zotero_actions() -> None:
    prepare_only = _view(
        [_item("001", worker="succeeded", write="prepared_only")],
        write_policy="prepare_only",
    )
    all_local = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="succeeded",
                local_prepare="prepared",
                write="not_applicable",
            )
        ]
    )

    for view in (prepare_only, all_local):
        assert all(
            entry["lane"] not in {"write", "user"}
            for entry in derive_next_actions(view)
        )


def test_public_action_required_inputs_cover_exact_cli_signature() -> None:
    views = [
        _view([_item("001", worker="queued")]),
        _view([_item("001", worker="claimed")]),
        _view(
            [
                _item(
                    "001",
                    input_type="pdf_path",
                    worker="queued",
                    local_prepare="claimed",
                    write="not_applicable",
                )
            ]
        ),
        _view([_item("001", worker="succeeded", write="queued")]),
        _view([_item("001", worker="succeeded", write="claimed")]),
        _view([_item("001", worker="succeeded", write="started")]),
        _view([_item("001", worker="succeeded", write="uncertain")]),
        _view(
            [_item("001", worker="succeeded", write="retry_confirmation_required")]
        ),
        _view(
            [_item("001", worker="succeeded", write="claimed")],
            pending_command="write.begin",
            pending_item_id="001",
        ),
    ]
    root = get_command(app)
    allowed_contextual_inputs = {
        "run.recover": {"paper_reader_root", "reconciliation_timeout_seconds"},
        "worker.claim": {"limit", "lease_seconds"},
        "worker.renew": {"lease_seconds"},
        "worker.release": {"acknowledge_no_side_effects"},
        "local-prepare.claim": {"limit", "lease_seconds"},
        "local-prepare.renew": {"lease_seconds"},
        "local-prepare.run": {"timeout_seconds"},
        "local-prepare.release": {"acknowledge_no_side_effects"},
        "write.claim": {"lease_seconds"},
        "write.renew": {"lease_seconds"},
        "write.retry": {"acknowledge_no_match"},
    }

    for view in views:
        for candidate in derive_next_actions(view):
            if candidate["lane"] == "user":
                continue
            group_name, command_name = candidate["action"].split(".", 1)
            group = root.commands[group_name]
            command = group.commands[command_name]
            cli_required = {
                parameter.name
                for parameter in command.params
                if parameter.required and parameter.name not in {"run_dir", "item_id"}
            }
            listed = set(candidate["required_inputs"])
            assert cli_required <= listed, candidate
            assert listed - cli_required <= allowed_contextual_inputs.get(
                candidate["action"],
                set(),
            ), candidate


def test_pending_event_suppresses_unrelated_mutations_and_replays_exact_public_origin() -> None:
    view = _view(
        [_item("001", worker="queued")],
        pending_command="worker.claim",
    )

    assert derive_next_actions(view) == [
        _action(
            None,
            "worker",
            "worker.claim",
            "pending_event_originating_request_replay_required",
            ["worker_id", "request_id", "limit", "lease_seconds"],
        )
    ]


@pytest.mark.parametrize("pending_command", ["worker.release", "worker.finish"])
def test_pending_same_item_worker_exit_suppresses_prompt(
    pending_command: str,
) -> None:
    view = _view(
        [_item("001", worker="claimed")],
        pending_command=pending_command,
        pending_request_id=RUN_ID,
        pending_item_id="001",
    )

    actions = derive_next_actions(view)

    assert [entry["action"] for entry in actions] == [pending_command]
    assert actions[0]["item_id"] == "001"
    assert actions[0]["reason_code"] == "pending_event_originating_request_replay_required"


def test_real_pending_claim_uses_public_lane_and_keeps_safe_existing_prompt(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdfs = []
    for index in range(2):
        pdf = tmp_path / f"paper-{index}.pdf"
        pdf.write_bytes(f"%PDF-1.7\npending {index}\n".encode())
        pdfs.append(pdf)
    paths = tmp_path / "paths.txt"
    paths.write_text("\n".join(str(pdf) for pdf in pdfs), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="pending next actions",
        output=manifest,
        request_id=RUN_ID,
        skill_root=skill_root,
        default_concurrency=2,
        created_at="2026-07-10T00:00:00Z",
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id=CLAIM_ID,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    active = claim_worker(
        run_dir,
        worker_id="reader-1",
        request_id="44444444-4444-4444-8444-444444444444",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]

    def crash_after_proposal_fsync(stage: str) -> None:
        if stage == "after_writing_fsync":
            raise RuntimeError("simulated crash after pending proposal fsync")

    with pytest.raises(RuntimeError, match="simulated crash"):
        claim_worker(
            run_dir,
            worker_id="reader-2",
            request_id="55555555-5555-4555-8555-555555555555",
            limit=1,
            now="2026-07-10T00:00:02Z",
            fault=crash_after_proposal_fsync,
        )

    view = load_run_view(run_dir)
    assert view.pending_event is not None
    assert worker_prompt(
        run_dir,
        active["item_id"],
        worker_id=active["worker_id"],
        claim_id=active["claim_id"],
        lease_token=active["lease_token"],
        attempt_id=active["attempt_id"],
        now="2026-07-10T00:00:03Z",
    )["item_id"] == "001"

    actions = derive_next_actions(view)
    assert [(item["lane"], item["action"], item["item_id"]) for item in actions] == [
        ("worker", "worker.claim", None),
        ("worker", "worker.prompt", "001"),
    ]


def test_pending_recovery_suppresses_only_expired_worker_prompts(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdfs = []
    for index in range(2):
        pdf = tmp_path / f"paper-{index}.pdf"
        pdf.write_bytes(f"%PDF-1.7\npending recovery {index}\n".encode())
        pdfs.append(pdf)
    paths = tmp_path / "paths.txt"
    paths.write_text("\n".join(str(pdf) for pdf in pdfs), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="pending recovery next actions",
        output=manifest,
        request_id=RUN_ID,
        skill_root=skill_root,
        default_concurrency=2,
        created_at="2026-07-10T00:00:00Z",
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id=CLAIM_ID,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    expired = claim_worker(
        run_dir,
        worker_id="reader-1",
        request_id="44444444-4444-4444-8444-444444444444",
        limit=1,
        lease_seconds=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    active = claim_worker(
        run_dir,
        worker_id="reader-2",
        request_id="55555555-5555-4555-8555-555555555555",
        limit=1,
        lease_seconds=300,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]

    def crash_after_proposal_fsync(stage: str) -> None:
        if stage == "after_writing_fsync":
            raise RuntimeError("simulated recovery crash after pending proposal fsync")

    with pytest.raises(RuntimeError, match="simulated recovery crash"):
        recover_run(
            run_dir,
            request_id="66666666-6666-4666-8666-666666666666",
            now="2026-07-10T00:00:03Z",
            fault=crash_after_proposal_fsync,
        )

    view = load_run_view(run_dir)
    assert view.pending_event is not None
    assert view.pending_event.event.command == "run.recover"
    assert [
        lease.item_id
        for lease in view.pending_event.event.data.expired_worker_leases
    ] == [expired["item_id"]]

    actions = derive_next_actions(view)
    assert actions[0]["action"] == "run.recover"
    assert [
        entry["item_id"]
        for entry in actions
        if entry["action"] == "worker.prompt"
    ] == [active["item_id"]]


def test_pending_recovery_suppresses_expired_write_preview() -> None:
    view = _view(
        [_item("001", worker="succeeded", write="claimed")],
        pending_command="run.recover",
        pending_request_id=RUN_ID,
        pending_item_id="001",
    )
    view.pending_event.event.data.kind = "write.lease_expired"

    assert [entry["action"] for entry in derive_next_actions(view)] == [
        "run.recover"
    ]


def test_pending_scalar_worker_expiry_suppresses_prompt() -> None:
    view = _view(
        [_item("001", worker="claimed")],
        pending_command="run.recover",
        pending_request_id=RUN_ID,
        pending_item_id="001",
    )
    view.pending_event.event.data.kind = "worker.lease_expired"

    assert [entry["action"] for entry in derive_next_actions(view)] == [
        "run.recover"
    ]


def test_pending_internal_local_reservation_maps_to_public_originating_command() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare="claimed",
                write="not_applicable",
            )
        ],
        pending_command="local-prepare.run.reserve",
    )

    assert derive_next_actions(view) == [
        _action(
            "001",
            "local_prepare",
            "local-prepare.run",
            "pending_event_originating_request_replay_required",
            [
                "worker_id",
                "claim_id",
                "lease_token",
                "attempt_id",
                "paper_reader_root",
                "request_id",
                "timeout_seconds",
            ],
        )
    ]


def test_pending_coordinated_finish_maps_back_to_public_local_run() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare="claimed",
                write="not_applicable",
                local_coordination_request_id=RUN_ID,
            )
        ],
        pending_command="local-prepare.finish",
        pending_request_id=RUN_ID,
        pending_item_id="001",
    )

    assert derive_next_actions(view) == [
        _action(
            "001",
            "local_prepare",
            "local-prepare.run",
            "pending_event_originating_request_replay_required",
            [
                "worker_id",
                "claim_id",
                "lease_token",
                "attempt_id",
                "paper_reader_root",
                "request_id",
                "timeout_seconds",
            ],
        )
    ]


@pytest.mark.parametrize(
    ("pending_command", "fingerprint_bound_inputs"),
    [
        ("run.recover", {"paper_reader_root", "reconciliation_timeout_seconds"}),
        ("worker.claim", {"limit", "lease_seconds"}),
        ("worker.renew", {"lease_seconds"}),
        ("local-prepare.claim", {"limit", "lease_seconds"}),
        ("local-prepare.renew", {"lease_seconds"}),
        ("write.claim", {"lease_seconds"}),
        ("write.renew", {"lease_seconds"}),
    ],
)
def test_pending_replay_lists_optional_public_inputs_bound_by_request_fingerprint(
    pending_command: str,
    fingerprint_bound_inputs: set[str],
) -> None:
    view = _view([_item("001", worker="queued")], pending_command=pending_command)

    action = derive_next_actions(view)[0]

    assert action["reason_code"] == "pending_event_originating_request_replay_required"
    assert fingerprint_bound_inputs <= set(action["required_inputs"])


def test_pending_derived_reconciliation_uses_manual_attention_not_false_replay() -> None:
    derived_request_id = derived_reconciliation_request_id(RUN_ID)
    view = _view(
        [_item("001", worker="succeeded", write="uncertain")],
        pending_command="write.reconcile",
        pending_request_id=derived_request_id,
        pending_item_id="001",
        committed_events=(
            SimpleNamespace(
                command="run.recover",
                request_id=RUN_ID,
                data=SimpleNamespace(
                    kind="write.lease_expired_uncertain",
                    item_id="001",
                ),
            ),
        ),
    )

    assert derive_next_actions(view) == [
        _action(
            "001",
            "user",
            "user.resolve-run-storage-attention",
            "pending_run_recovery_reconciliation_manual_attention",
            ["maintainer_decision"],
            requires_user_intent=True,
        )
    ]


def test_pending_direct_write_reconcile_remains_public_write_reconcile() -> None:
    view = _view(
        [_item("001", worker="succeeded", write="uncertain")],
        pending_command="write.reconcile",
        pending_request_id=RUN_ID,
        pending_item_id="001",
    )

    assert derive_next_actions(view) == [
        _action(
            "001",
            "write",
            "write.reconcile",
            "pending_event_originating_request_replay_required",
            ["readback", "request_id"],
        )
    ]


def test_unknown_pending_internal_command_uses_generic_run_attention() -> None:
    view = _view(
        [_item("001", worker="queued")],
        pending_command="internal.unknown",
    )

    assert derive_next_actions(view) == [
        _action(
            None,
            "user",
            "user.resolve-run-storage-attention",
            "pending_internal_request_manual_attention",
            ["maintainer_decision"],
            requires_user_intent=True,
        )
    ]


@pytest.mark.parametrize(
    ("view_kwargs", "reason_code"),
    [
        (
            {"incomplete_event_writes": (".event.writing",)},
            "incomplete_event_write_attention",
        ),
        (
            {"state_pending_write": ".state.tmp"},
            "pending_state_write_attention",
        ),
        (
            {"incomplete_state_writes": (".state.writing",)},
            "incomplete_state_write_attention",
        ),
    ],
)
def test_non_authoritative_storage_residue_gets_attention_not_fake_recovery(
    view_kwargs: dict[str, object],
    reason_code: str,
) -> None:
    view = _view([_item("001", worker="queued")], **view_kwargs)

    actions = derive_next_actions(view)
    assert actions[0] == _action(
        None,
        "user",
        "user.resolve-run-storage-attention",
        reason_code,
        ["maintainer_decision"],
        requires_user_intent=True,
    )
    assert all(
        entry["action"] != "run.recover"
        for entry in actions
    )
    assert actions[1]["action"] == "worker.claim"


def test_stale_pending_state_uses_real_snapshot_recovery_before_attention() -> None:
    view = _view(
        [_item("001", worker="queued")],
        snapshot_status="stale",
        state_pending_write=".state.tmp",
    )

    actions = derive_next_actions(view)
    assert actions[0]["action"] == "run.recover"
    assert actions[0]["reason_code"] == "snapshot_repair_required"
    assert all(
        entry["reason_code"] != "pending_state_write_attention"
        for entry in actions
    )
    assert actions[1]["action"] == "worker.claim"


def test_claimed_local_attempt_exposes_only_conditional_expiry_recovery() -> None:
    view = _view(
        [
            _item(
                "001",
                input_type="pdf_path",
                worker="queued",
                local_prepare="claimed",
                write="not_applicable",
            )
        ]
    )

    assert derive_next_actions(view)[0] == _action(
        "001",
        "run",
        "run.recover",
        "local_prepare_lease_expiry_recovery_if_expired",
        ["request_id"],
    )


def test_stale_snapshot_recovery_precedes_item_actions() -> None:
    view = _view([_item("001", worker="queued")], snapshot_status="stale")

    actions = derive_next_actions(view)
    assert actions[0] == _action(
        None,
        "run",
        "run.recover",
        "snapshot_repair_required",
        ["request_id"],
    )
    assert actions[1]["action"] == "worker.claim"


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str | None]]:
    snapshot: dict[str, tuple[int, int, int, int, str | None]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        snapshot[path.relative_to(root).as_posix() or "."] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            digest,
        )
    return snapshot


def test_repeated_cli_status_is_one_json_and_zero_mutation(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nnext actions\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="next actions",
        output=manifest,
        request_id=RUN_ID,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id=CLAIM_ID,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    before = _tree_snapshot(run_dir)

    runner = CliRunner()
    payloads = []
    for _ in range(2):
        result = runner.invoke(app, ["run", "status", str(run_dir)])
        assert result.exit_code == 0, result.output
        assert len(result.stdout.splitlines()) == 1
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["command"] == "run.status"
        payloads.append(payload)

    assert payloads[0] == payloads[1]
    assert _tree_snapshot(run_dir) == before
    serialized_actions = json.dumps(payloads[0]["result"]["next_actions"], sort_keys=True)
    assert RUN_ID not in serialized_actions
    assert CLAIM_ID not in serialized_actions
    assert ATTEMPT_ID not in serialized_actions
    assert re.search(r"[0-9a-f]{8}-[0-9a-f-]{27,}", serialized_actions) is None
    assert "lease_token_sha256" not in serialized_actions


def test_repeated_status_preserves_partial_event_residue_bytes_inode_and_mtime(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\npartial status\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="partial status",
        output=manifest,
        request_id=RUN_ID,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id=CLAIM_ID,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    residue = (
        run_dir
        / "events"
        / ".00000000000000000002.json.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.writing"
    )
    residue.write_bytes(b'{"partial"')
    residue.chmod(0o600)
    before = _tree_snapshot(run_dir)

    runner = CliRunner()
    payloads = []
    for _ in range(2):
        result = runner.invoke(app, ["run", "status", str(run_dir)])
        assert result.exit_code == 0, result.output
        assert len(result.stdout.splitlines()) == 1
        payloads.append(json.loads(result.stdout))

    assert payloads[0] == payloads[1]
    actions = payloads[0]["result"]["next_actions"]
    assert actions[0]["action"] == "user.resolve-run-storage-attention"
    assert actions[0]["reason_code"] == "incomplete_event_write_attention"
    assert all(entry["action"] != "run.recover" for entry in actions)
    assert _tree_snapshot(run_dir) == before
