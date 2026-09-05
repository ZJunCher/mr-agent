import json
from dataclasses import replace

import pytest

from pr_agent.distributed.models import (
    TERMINAL_TASK_STATUSES,
    TERMINAL_TRIAGE_CARD_STATES,
    DeliveryKind,
    MrKey,
    NotificationEnvelope,
    PipelineEvent,
    PostRepairUTState,
    PostRepairUTStatus,
    RepairCategory,
    RepairItem,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.triage.failure_explanations import FailureExplanation


def test_canceled_is_terminal_task_status():
    assert TaskStatus.CANCELED in TERMINAL_TASK_STATUSES


def test_auto_pause_states_round_trip():
    assert TaskStatus("paused_by_triage") is TaskStatus.PAUSED_BY_TRIAGE
    assert DeliveryKind("resume_auto") is DeliveryKind.RESUME_AUTO
    assert TaskKind("repair_rollback") is TaskKind.REPAIR_ROLLBACK
    assert TaskKind("repair_report") is TaskKind.REPAIR_REPORT
    assert TaskKind("post_repair_ut") is TaskKind.POST_REPAIR_UT
    assert TriageCardState("rollback_running") is TriageCardState.ROLLBACK_RUNNING


def test_dependency_blocker_states_are_serializable_and_terminal():
    assert TriageCardState("repair_blocked") is TriageCardState.REPAIR_BLOCKED
    assert RepairItemStatus("blocked") is RepairItemStatus.BLOCKED
    assert TriageCardState.REPAIR_BLOCKED in TERMINAL_TRIAGE_CARD_STATES


def test_model_unavailable_card_state_is_serializable_and_terminal():
    state = TriageCardState("repair_model_unavailable")

    assert state is TriageCardState.REPAIR_MODEL_UNAVAILABLE
    assert state in TERMINAL_TRIAGE_CARD_STATES


def test_task_envelope_round_trip_preserves_mr_identity():
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey(project_id="eabot/cook", iid=536),
        pr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/536",
        command="/triage",
        payload={"sender_id": "u1"},
        idempotency_key="note:1:536:99:create",
    )

    assert TaskEnvelope.from_json(task.to_json()) == task


def test_task_envelope_rejects_unknown_schema_version():
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=None,
        pr_url="",
        command="/review",
        payload={},
        idempotency_key="note:1",
    )

    with pytest.raises(ValueError, match="schema_version"):
        TaskEnvelope.from_json(replace(task, schema_version=2).to_json())


def test_mr_key_escapes_project_id_for_redis_keys():
    assert MrKey(project_id="group/project with spaces", iid=7).redis_id == "group%2Fproject%20with%20spaces:7"


def test_pipeline_event_round_trip():
    event = PipelineEvent.new(
        project_id="eabot/cook",
        pipeline_id=29415,
        sha="abc123",
        status="success",
        ref="refs/merge-requests/536/head",
    )

    assert PipelineEvent.from_json(event.to_json()) == event
    assert event.terminal is True


def test_notification_requires_recipient_identity():
    with pytest.raises(ValueError, match="recipient identity"):
        NotificationEnvelope.new(
            task_id="task-1",
            receive_id="",
            recipient_email="",
            recipient_username="",
            kind="triage_result",
            content="done",
            title="Triage",
            header_template="green",
            mr_url="https://gitlab.example/mr/1",
        )


def test_notification_without_card_fields_remains_readable():
    notification = NotificationEnvelope.new(
        task_id="task-1",
        receive_id="ou_1",
        recipient_email="",
        recipient_username="",
        kind="markdown",
        content="done",
        title="Triage",
        header_template="green",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
    )
    payload = notification.to_dict()
    payload.pop("card_id", None)
    payload.pop("message_id", None)
    payload.pop("fallback_content", None)
    payload.pop("card_state", None)

    restored = NotificationEnvelope.from_dict(payload)

    assert restored.card_id == ""
    assert restored.message_id == ""
    assert restored.fallback_content == ""
    assert restored.card_state == ""


def test_notification_round_trip_preserves_card_state():
    notification = NotificationEnvelope.new(
        task_id="task-1",
        receive_id="ou_1",
        recipient_email="",
        recipient_username="",
        kind="card_update",
        content="{}",
        title="Triage",
        header_template="green",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        card_state=TriageCardState.REPAIR_SUCCEEDED.value,
    )

    restored = NotificationEnvelope.from_json(notification.to_json())

    assert restored.card_state == TriageCardState.REPAIR_SUCCEEDED.value


def test_triage_card_binding_round_trip_preserves_state():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
        repair_items=(
            RepairItem(
                category=RepairCategory.BUILD,
                command="/triage",
                label="修复编译错误",
                display_name="Build",
                button_type="danger",
                status=RepairItemStatus.PENDING,
                pipeline_id=29415,
                pipeline_sha="abc123",
                failed_job_names=("build_release_arm64", "x86_64_ut_coverage_check"),
                failure_explanations=(
                    FailureExplanation(
                        job_name="build_release_arm64",
                        confirmed_reason="fatal error: missing.hpp: No such file",
                        trace_line=27,
                        confidence="confirmed",
                    ),
                ),
            ),
        ),
    )

    restored = TriageCardBinding.from_json(binding.to_json())

    assert restored == binding
    assert restored.state is TriageCardState.PIPELINE_FAILED
    assert restored.status_markdown == ""
    assert restored.fallback_sent is False
    assert restored.repair_items[0].category is RepairCategory.BUILD
    assert restored.repair_items[0].failed_job_names == (
        "build_release_arm64",
        "x86_64_ut_coverage_check",
    )
    assert restored.repair_items[0].failure_explanations[0].confirmed_reason == (
        "fatal error: missing.hpp: No such file"
    )
    assert restored.repair_items[0].failure_explanations[0].trace_line == 27
    assert restored.current_pipeline_id == 29415

    legacy_value = json.loads(binding.to_json())
    legacy_value["repair_items"][0]["failure_explanations"][0].pop("trace_line")
    legacy = TriageCardBinding.from_json(json.dumps(legacy_value))

    assert legacy.repair_items[0].failure_explanations[0].trace_line == 0


def test_triage_card_binding_round_trips_gitlab_author():
    binding = TriageCardBinding.new(
        card_id="card-8",
        task_id="",
        open_message_id="",
        receive_id="",
        mr_url="https://gitlab/eabot/control/-/merge_requests/8",
        project_id="eabot/control",
        mr_iid=8,
        mr_title="control",
        mr_author_username="xiaoyu.li",
        source_branch="jack/dev/common_rec",
        pipeline_id=31089,
        pipeline_sha="750bb8c0",
        original_markdown="failed",
    )

    restored = TriageCardBinding.from_json(binding.to_json())

    assert restored.mr_author_username == "xiaoyu.li"


def test_triage_card_binding_round_trips_post_repair_ut_state():
    binding = TriageCardBinding.new(
        card_id="card-ut",
        task_id="repair-1",
        open_message_id="om-1",
        receive_id="ou-1",
        mr_url="https://gitlab/eabot/cook/-/merge_requests/1",
        project_id="eabot/cook",
        mr_iid=1,
        mr_title="ut",
        source_branch="feature/ut",
        pipeline_id=10,
        pipeline_sha="a" * 40,
        original_markdown="failed",
    )
    binding = replace(
        binding,
        post_repair_ut=PostRepairUTState(
            status=PostRepairUTStatus.RUNNING,
            task_id="ut-1",
            baseline_sha="b" * 40,
            coverage_before=63.04,
        ),
    )

    restored = TriageCardBinding.from_json(binding.to_json())

    assert restored.post_repair_ut == binding.post_repair_ut


def test_legacy_triage_card_binding_uses_compatible_defaults():
    binding = TriageCardBinding.new(
        card_id="legacy",
        task_id="",
        open_message_id="",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="legacy",
        source_branch="feature/legacy",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )
    payload = binding.to_dict()
    for field in (
        "repair_items",
        "active_task_id",
        "active_category",
        "revision",
        "current_pipeline_id",
        "current_pipeline_sha",
        "mr_author_username",
    ):
        payload.pop(field)

    restored = TriageCardBinding.from_dict(payload)

    assert restored.repair_items == ()
    assert restored.active_task_id == ""
    assert restored.current_pipeline_id == 29415
    assert restored.mr_author_username == ""
    assert restored.rollback_repair_task_id == ""
    assert restored.rollback_commit_count == 0
