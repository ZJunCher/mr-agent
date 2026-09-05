from dataclasses import replace

from pr_agent.distributed.models import (
    PostRepairUTState,
    PostRepairUTStatus,
    RepairCategory,
    RepairItemStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.feishu.triage_card import build_card_id, can_transition_triage_card, parse_mr_identity, render_triage_card
from pr_agent.triage.failure_categories import (
    pipeline_repair_item,
    repair_items_for_categories,
    repair_items_for_failed_jobs,
)
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.repair_card_mode import RepairCardMode


def _binding() -> TriageCardBinding:
    identity = parse_mr_identity("https://gitlab.example.com/eabot/cook/-/merge_requests/538#note_1")
    return TriageCardBinding.new(
        card_id=build_card_id(identity.project_id, identity.mr_iid, 29415),
        task_id="task-1234567890",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url=identity.mr_url,
        project_id=identity.project_id,
        mr_iid=identity.mr_iid,
        mr_title="lidar udp",
        source_branch="main-test/lidar_udp",
        pipeline_id=29415,
        pipeline_sha="abcdef123456",
        original_markdown="- failed jobs: build_release_arm64",
    )


def test_renderer_keeps_mr_identity_and_original_failure():
    binding = _binding()

    card = render_triage_card(binding, TriageCardState.REPAIR_SUCCEEDED, "coverage: 63.04%")

    assert card["header"]["title"]["content"] == "【eabot/cook !538】修复成功"
    markdown = card["elements"][0]["content"]
    assert "build_release_arm64" in markdown
    assert "coverage: 63.04%" in markdown
    assert binding.mr_url in markdown
    assert binding.source_branch in markdown
    assert "29415" in markdown
    assert "task-1234" in markdown
    assert all(element.get("tag") != "action" for element in card["elements"])


def test_success_card_shows_isolated_post_repair_ut_action(monkeypatch):
    monkeypatch.setattr("pr_agent.feishu.triage_card.post_repair_ut_enabled", lambda: True, raising=False)
    monkeypatch.setattr("pr_agent.triage.post_repair_ut.post_repair_ut_enabled", lambda: True)
    item = replace(
        repair_items_for_categories([RepairCategory.BUILD], 29416, "f" * 40)[0],
        task_id="task-1234567890",
        status=RepairItemStatus.SUCCEEDED,
    )
    binding = replace(
        _binding(),
        state=TriageCardState.REPAIR_SUCCEEDED,
        current_pipeline_id=29416,
        current_pipeline_sha="f" * 40,
        repair_items=(item,),
        post_repair_ut=PostRepairUTState(coverage_before=63.04),
    )

    card = render_triage_card(binding, binding.state, "修复成功")

    actions = [
        action
        for element in card["elements"]
        if element.get("tag") == "action"
        for action in element.get("actions", ())
        if action.get("value", {}).get("command") == "supplement-unit-tests"
    ]
    assert len(actions) == 1
    assert actions[0]["text"]["content"] == "补充单元测试"
    assert actions[0]["value"]["repair_task_id"] == binding.task_id


def test_failure_card_never_shows_post_repair_ut_action(monkeypatch):
    monkeypatch.setattr("pr_agent.triage.post_repair_ut.post_repair_ut_enabled", lambda: True)
    card = render_triage_card(_binding(), TriageCardState.PIPELINE_FAILED, "")
    assert "补充单元测试" not in str(card)


def test_running_post_repair_ut_preserves_repair_success_and_shows_cancel():
    binding = replace(
        _binding(),
        state=TriageCardState.REPAIR_SUCCEEDED,
        active_task_id="ut-1",
        post_repair_ut=PostRepairUTState(
            status=PostRepairUTStatus.RUNNING,
            task_id="ut-1",
            status_markdown="正在生成测试",
        ),
    )

    card = render_triage_card(binding, binding.state, "修复成功")

    assert card["header"]["title"]["content"].endswith("修复成功")
    assert "单元测试补充" in card["elements"][0]["content"]
    assert "正在生成测试" in card["elements"][0]["content"]
    assert "取消补测" in str(card)


def test_auto_failure_rollback_final_card_remains_a_repair_failure():
    binding = replace(_binding(), rollback_trigger="auto_failure")

    succeeded = render_triage_card(
        binding,
        TriageCardState.ROLLBACK_SUCCEEDED,
        "修复失败，本次自动修改已撤回。",
    )
    failed = render_triage_card(
        binding,
        TriageCardState.ROLLBACK_FAILED,
        "修复失败，自动撤回未完成。",
    )

    assert succeeded["header"] == {
        "title": {"tag": "plain_text", "content": "【eabot/cook !538】修复失败，修改已撤回"},
        "template": "red",
    }
    assert failed["header"] == {
        "title": {"tag": "plain_text", "content": "【eabot/cook !538】修复失败，自动撤回未完成"},
        "template": "red",
    }


def test_renderer_uses_orange_partial_success_header():
    card = render_triage_card(
        _binding(),
        TriageCardState.REPAIR_PARTIAL,
        "Format：修复成功\nClang：修复失败",
    )

    assert card["header"]["title"]["content"] == "【eabot/cook !538】部分修复成功"
    assert card["header"]["template"] == "orange"
    assert can_transition_triage_card(TriageCardState.WAITING_PIPELINE, TriageCardState.REPAIR_PARTIAL)


def test_blocked_card_is_terminal_and_owner_facing(monkeypatch):
    item = replace(
        repair_items_for_categories([RepairCategory.BUILD], 29415, "abcdef123456")[0],
        status=RepairItemStatus.BLOCKED,
        status_markdown="当前依赖分支缺少接口",
    )
    binding = replace(
        _binding(),
        repair_items=(item,),
        rollback_repair_task_id="repair-task",
        rollback_commit_count=1,
    )
    monkeypatch.setattr(
        "pr_agent.feishu.triage_card.build_repair_details_url",
        lambda task_id: f"https://agent.example/repair-results/{task_id}?sig=safe" if task_id else "",
    )

    card = render_triage_card(
        binding,
        TriageCardState.REPAIR_BLOCKED,
        "当前依赖分支缺少接口，请维护者确认候选分支。",
        detail_task_id="repair-task",
    )

    assert can_transition_triage_card(TriageCardState.REPAIR_RUNNING, TriageCardState.REPAIR_BLOCKED)
    assert not can_transition_triage_card(TriageCardState.REPAIR_BLOCKED, TriageCardState.REPAIR_BLOCKED)
    assert not can_transition_triage_card(TriageCardState.REPAIR_BLOCKED, TriageCardState.ROLLBACK_QUEUED)
    assert card["header"]["title"]["content"] == "【eabot/cook !538】外部依赖阻塞"
    assert card["header"]["template"] == "orange"
    assert "修复失败" not in card["elements"][0]["content"]
    assert "**Build**：外部依赖阻塞" in card["elements"][0]["content"]
    assert "pipeline_repair_selection" not in str(card)
    assert "撤回修复" not in str(card)


def test_model_unavailable_card_is_retryable_and_uses_safe_copy():
    item = replace(
        repair_items_for_categories([RepairCategory.BUILD], 29415, "abcdef123456")[0],
        task_id="task-1234567890",
        status=RepairItemStatus.FAILED,
        status_markdown="模型服务不可用，建议稍后重试。",
        failed_job_names=("build_release_arm64",),
    )
    binding = replace(
        _binding(),
        repair_items=(item,),
        repair_card_mode=RepairCardMode.MULTI_SELECT.value,
        active_task_id="",
    )

    card = render_triage_card(
        binding,
        TriageCardState.REPAIR_MODEL_UNAVAILABLE,
        "模型服务不可用，建议稍后重试。",
    )

    assert card["header"]["title"]["content"] == "【eabot/cook !538】模型服务不可用"
    assert card["header"]["template"] == "orange"
    assert "模型服务不可用，建议稍后重试。" in card["elements"][0]["content"]
    assert "**Build**：模型服务不可用，可重试" in card["elements"][0]["content"]
    assert "pipeline_repair_selection" in str(card)
    assert can_transition_triage_card(
        TriageCardState.REPAIR_MODEL_UNAVAILABLE,
        TriageCardState.REPAIR_QUEUED,
    )
    assert "撤回修复" not in str(card)


def test_detail_button_is_the_only_additive_card_change(monkeypatch):
    binding = replace(
        _binding(),
        repair_items=(pipeline_repair_item(29415, "abcdef123456"),),
        active_task_id="task-running",
        task_id="task-running",
    )
    monkeypatch.setattr(
        "pr_agent.feishu.triage_card.build_repair_details_url",
        lambda task_id: f"https://agent.example/repair-results/{task_id}?sig=safe" if task_id else "",
    )

    before = render_triage_card(binding, TriageCardState.REPAIR_RUNNING, "正在诊断")
    after = render_triage_card(
        binding,
        TriageCardState.REPAIR_RUNNING,
        "正在诊断",
        detail_task_id="task-running",
    )

    assert after["header"] == before["header"]
    assert after["elements"][:-1] == before["elements"]
    action = after["elements"][-1]
    assert action == {
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看修复详情"},
            "type": "default",
            "url": "https://agent.example/repair-results/task-running?sig=safe",
        }],
    }


def test_detail_button_is_absent_when_feature_link_is_unavailable(monkeypatch):
    binding = _binding()
    monkeypatch.setattr("pr_agent.feishu.triage_card.build_repair_details_url", lambda task_id: "")

    before = render_triage_card(binding, TriageCardState.REPAIR_SUCCEEDED, "done")
    after = render_triage_card(
        binding,
        TriageCardState.REPAIR_SUCCEEDED,
        "done",
        detail_task_id="task-running",
    )

    assert after == before


def test_terminal_card_shows_one_confirmed_rollback_action(monkeypatch):
    monkeypatch.setenv("PR_AGENT_REPAIR_ROLLBACK_ENABLED", "true")
    binding = replace(
        _binding(),
        state=TriageCardState.REPAIR_SUCCEEDED,
        rollback_repair_task_id="1" * 32,
        rollback_commit_count=2,
    )

    card = render_triage_card(binding, binding.state, "修复成功")
    actions = [
        action
        for element in card["elements"]
        if element.get("tag") == "action"
        for action in element.get("actions", ())
        if action.get("value", {}).get("command") == "rollback-repair"
    ]

    assert len(actions) == 1
    assert actions[0]["type"] == "danger"
    assert "完整撤回本次自动修复产生的 2 个提交" in actions[0]["confirm"]["text"]["content"]
    assert "commit_shas" not in actions[0]["value"]


def test_failure_card_has_correlated_triage_action():
    binding = _binding()

    card = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "")

    action = card["elements"][-1]["actions"][0]["value"]
    assert action == {
        "command": "triage",
        "mr_url": binding.mr_url,
        "card_id": binding.card_id,
        "pipeline_id": 29415,
    }


def test_unified_failure_card_has_one_repair_pipeline_action():
    binding = replace(
        _binding(),
        repair_items=(pipeline_repair_item(29415, "abcdef123456"),),
        repair_card_mode=RepairCardMode.UNIFIED.value,
    )

    card = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "")

    action_groups = [element for element in card["elements"] if element.get("tag") == "action"]
    assert len(action_groups) == 1
    assert len(action_groups[0]["actions"]) == 1
    action = action_groups[0]["actions"][0]
    assert action["text"]["content"] == "修复流水线"
    assert action["value"]["command"] == "repair-pipeline"
    assert action["value"]["category"] == "pipeline"
    assert action["value"]["pipeline_id"] == 29415
    assert action["value"]["pipeline_sha"] == "abcdef123456"


def test_multi_select_card_lists_jobs_before_required_form():
    binding = replace(
        _binding(),
        repair_items=repair_items_for_failed_jobs(
            [{"name": "code_format_check"}, {"name": "build_release_arm64"}],
            29415,
            "abcdef123456",
        ),
        repair_card_mode=RepairCardMode.MULTI_SELECT.value,
    )

    card = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "")

    markdown = card["elements"][0]["content"]
    form = card["elements"][-1]
    assert markdown.index("Format Jobs") < markdown.index("请选择需要自动修复的问题")
    assert "**Format Jobs**：code_format_check" in markdown
    assert "**Build Jobs**：build_release_arm64" in markdown
    assert form["tag"] == "form"
    assert form["elements"][0]["tag"] == "multi_select_static"
    assert form["elements"][0]["name"] == "selected_categories"
    assert form["elements"][0]["required"] is True
    assert all(option["selected"] is False for option in form["elements"][0]["options"])
    assert form["elements"][-1]["action_type"] == "form_submit"
    assert form["elements"][-1]["text"]["content"] == "修复所选问题"


def test_multi_select_card_without_actionable_items_has_no_repair_fallback():
    binding = replace(
        _binding(),
        repair_items=(),
        repair_card_mode=RepairCardMode.MULTI_SELECT.value,
        original_markdown=(
            "Format Job 自身执行失败，格式检查尚未开始。 "
            "[查看 Job 日志](https://gitlab.example/jobs/108606)"
        ),
    )

    card = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "")

    assert len(card["elements"]) == 1
    assert card["elements"][0]["tag"] == "markdown"
    assert "Format Job 自身执行失败" in card["elements"][0]["content"]
    assert "查看 Job 日志" in card["elements"][0]["content"]
    assert "修复编译错误" not in str(card)


def test_parse_mr_identity_supports_nested_groups_and_strips_fragment():
    identity = parse_mr_identity("https://gitlab.example/group/sub/repo/-/merge_requests/42?view=parallel#note_1")

    assert identity.project_id == "group/sub/repo"
    assert identity.mr_iid == 42
    assert identity.mr_url == "https://gitlab.example/group/sub/repo/-/merge_requests/42"


def test_terminal_card_state_cannot_regress():
    assert can_transition_triage_card(
        TriageCardState.REPAIR_RUNNING, TriageCardState.WAITING_PIPELINE
    )
    assert not can_transition_triage_card(
        TriageCardState.REPAIR_SUCCEEDED, TriageCardState.REPAIR_RUNNING
    )
    assert not can_transition_triage_card(
        TriageCardState.REPAIR_FAILED, TriageCardState.REPAIR_FAILED
    )


def test_active_format_keeps_build_visible_but_not_clickable():
    items = repair_items_for_categories([RepairCategory.FORMAT, RepairCategory.BUILD], 29415, "old-sha")
    items = (
        replace(items[0], status=RepairItemStatus.RUNNING, task_id="task-format"),
        items[1],
    )
    binding = replace(
        _binding(),
        repair_items=items,
        active_task_id="task-format",
        active_category=RepairCategory.FORMAT.value,
    )

    card = render_triage_card(binding, TriageCardState.REPAIR_RUNNING, "正在修复格式")
    markdown = card["elements"][0]["content"]

    assert "**Format**：正在修复" in markdown
    assert "**Build**：等待当前修复完成" in markdown
    actions = [element for element in card["elements"] if element.get("tag") == "action"]
    assert len(actions) == 1
    assert [action["value"]["command"] for action in actions[0]["actions"]] == ["cancel-repair"]


def test_partial_result_reopens_build_on_latest_pipeline():
    items = repair_items_for_categories([RepairCategory.FORMAT, RepairCategory.BUILD], 30041, "old-sha")
    items = (
        replace(items[0], status=RepairItemStatus.SUCCEEDED),
        replace(items[1], status=RepairItemStatus.PENDING),
    )
    binding = replace(
        _binding(),
        repair_items=items,
        task_id="",
        active_task_id="",
        active_category="",
        revision=3,
        current_pipeline_id=30100,
        current_pipeline_sha="new-sha",
        repair_card_mode=RepairCardMode.LEGACY_ACTIONS.value,
    )

    card = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "格式已修复")
    action = card["elements"][-1]["actions"][0]["value"]

    assert action["category"] == RepairCategory.BUILD.value
    assert action["pipeline_id"] == 30100
    assert action["pipeline_sha"] == "new-sha"
    assert action["revision"] == 3


def test_running_unified_card_has_cancel_action():
    item = replace(
        pipeline_repair_item(29415, "abcdef123456"),
        status=RepairItemStatus.RUNNING,
        task_id="task-running",
    )
    binding = replace(
        _binding(),
        repair_items=(item,),
        task_id="task-running",
        active_task_id="task-running",
        active_category=RepairCategory.PIPELINE.value,
        revision=2,
        repair_card_mode=RepairCardMode.UNIFIED.value,
    )

    card = render_triage_card(binding, TriageCardState.REPAIR_RUNNING, "正在诊断并修复")

    action = card["elements"][-1]["actions"][0]
    assert action["text"]["content"] == "取消修复"
    assert action["value"] == {
        "command": "cancel-repair",
        "task_id": "task-running",
        "mr_url": binding.mr_url,
        "card_id": binding.card_id,
        "pipeline_id": 29415,
        "pipeline_sha": "abcdef123456",
        "revision": 2,
    }


def test_canceled_current_card_can_retry():
    item = replace(
        pipeline_repair_item(29415, "abcdef123456"),
        status=RepairItemStatus.FAILED,
    )
    binding = replace(
        _binding(),
        repair_items=(item,),
        task_id="",
        active_task_id="",
        active_category="",
        revision=3,
        repair_card_mode=RepairCardMode.UNIFIED.value,
    )

    card = render_triage_card(binding, TriageCardState.CANCELED, "修复已取消")

    action = card["elements"][-1]["actions"][0]
    assert action["text"]["content"] == "修复流水线"
    assert action["value"]["command"] == "repair-pipeline"
    assert action["value"]["revision"] == 3


def test_failed_item_renders_bounded_failure_explanations():
    item = replace(
        repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30103, "final-sha")[0],
        status=RepairItemStatus.FAILED,
        failure_explanations=(
            FailureExplanation(
                job_name="build_release_arm64",
                job_url="https://gitlab.example/eabot/cook/-/jobs/88",
                confirmed_reason="fatal error: missing.hpp: No such file",
                possible_reason="依赖没有安装",
                suggested_action="补充构建依赖后重试",
                confidence="confirmed",
            ),
        ),
    )
    binding = replace(_binding(), repair_items=(item,), repair_card_mode=RepairCardMode.MULTI_SELECT.value)

    markdown = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "修复失败")["elements"][0]["content"]

    assert "**失败说明**" in markdown
    assert "**原因分析**：依赖没有安装" in markdown
    assert "**建议处理**：补充构建依赖后重试" in markdown
    assert "fatal error: missing.hpp" not in markdown
    assert "[查看 Job 日志](https://gitlab.example/eabot/cook/-/jobs/88)" in markdown


def test_failed_item_without_analysis_falls_back_to_confirmed_reason():
    item = replace(
        repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30103, "final-sha")[0],
        status=RepairItemStatus.FAILED,
        failure_explanations=(
            FailureExplanation(
                job_name="build_release_arm64",
                confirmed_reason="fatal error: missing.hpp: No such file",
                confidence="confirmed",
            ),
        ),
    )
    binding = replace(_binding(), repair_items=(item,), repair_card_mode=RepairCardMode.MULTI_SELECT.value)

    markdown = render_triage_card(binding, TriageCardState.PIPELINE_FAILED, "修复失败")["elements"][0]["content"]

    assert "**已确认原因**：fatal error: missing.hpp: No such file" in markdown


def test_failed_item_without_reason_has_unknown_fallback():
    item = replace(
        repair_items_for_failed_jobs([{"name": "clang_tidy_check"}], 30103, "final-sha")[0],
        status=RepairItemStatus.FAILED,
        failure_explanations=(FailureExplanation(job_name="clang_tidy_check"),),
    )
    binding = replace(_binding(), repair_items=(item,))

    markdown = render_triage_card(binding, TriageCardState.REPAIR_FAILED, "修复失败")["elements"][0]["content"]

    assert "暂未提取到具体根因，请查看 Job 日志" in markdown


def test_failure_explanations_can_be_disabled(monkeypatch):
    item = replace(
        repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30103, "final-sha")[0],
        status=RepairItemStatus.FAILED,
        failure_explanations=(FailureExplanation(job_name="build_release_arm64", confirmed_reason="compile error"),),
    )
    binding = replace(_binding(), repair_items=(item,))
    monkeypatch.setattr("pr_agent.feishu.triage_card.triage_failure_explanations_enabled", lambda: False)

    markdown = render_triage_card(binding, TriageCardState.REPAIR_FAILED, "修复失败")["elements"][0]["content"]

    assert "**失败说明**" not in markdown
    assert "compile error" not in markdown
