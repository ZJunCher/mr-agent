"""Tests for the /improve summary table's multi-location grouping
(_group_multi_location_suggestions) and its rendering in
generate_summarized_suggestions: suggestions that Tier-2 resolved at
multiple code locations for the SAME original issue (sharing a
source_task_id) must collapse into a single table row / <details> block,
not one row per location."""
from unittest.mock import MagicMock

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def _sugg(**kw):
    base = {
        "relevant_file": "src/foo.cpp", "relevant_lines_start": 1, "relevant_lines_end": 1,
        "existing_code": "old", "improved_code": "new", "one_sentence_summary": "issue summary",
        "suggestion_content": "why...\nfix...", "label": "correctness", "score": 8,
    }
    base.update(kw)
    return base


def _tool():
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_line_link = MagicMock(return_value="http://link")
    return tool


class TestGroupMultiLocationSuggestions:
    def test_suggestions_without_source_task_id_pass_through_unchanged(self):
        tool = _tool()
        suggestions = [_sugg(relevant_file="a.cpp"), _sugg(relevant_file="b.cpp")]
        result = tool._group_multi_location_suggestions(suggestions)
        assert result == suggestions
        assert all("_multi_location_members" not in s for s in result)

    def test_single_member_group_is_not_wrapped(self):
        tool = _tool()
        suggestions = [_sugg(source_task_id="SUG-001")]
        result = tool._group_multi_location_suggestions(suggestions)
        assert len(result) == 1
        assert "_multi_location_members" not in result[0]

    def test_multi_member_group_merges_into_one_item(self):
        tool = _tool()
        suggestions = [
            _sugg(relevant_file="a.cpp", relevant_lines_start=1, relevant_lines_end=1, source_task_id="SUG-001"),
            _sugg(relevant_file="a.cpp", relevant_lines_start=5, relevant_lines_end=5, source_task_id="SUG-001"),
            _sugg(relevant_file="b.cpp", relevant_lines_start=9, relevant_lines_end=9, source_task_id="SUG-001"),
        ]
        result = tool._group_multi_location_suggestions(suggestions)
        assert len(result) == 1
        assert len(result[0]["_multi_location_members"]) == 3

    def test_different_source_task_ids_stay_separate(self):
        tool = _tool()
        suggestions = [
            _sugg(source_task_id="SUG-001"), _sugg(source_task_id="SUG-001"),
            _sugg(source_task_id="SUG-002"), _sugg(source_task_id="SUG-002"),
        ]
        result = tool._group_multi_location_suggestions(suggestions)
        assert len(result) == 2
        assert all(len(item["_multi_location_members"]) == 2 for item in result)

    def test_mixed_grouped_and_singleton_suggestions(self):
        tool = _tool()
        suggestions = [
            _sugg(relevant_file="a.cpp", source_task_id="SUG-001"),
            _sugg(relevant_file="b.cpp", source_task_id="SUG-001"),
            _sugg(relevant_file="c.cpp"),  # no source_task_id -- normal suggestion
        ]
        result = tool._group_multi_location_suggestions(suggestions)
        assert len(result) == 2
        wrapped = [s for s in result if "_multi_location_members" in s]
        singleton = [s for s in result if "_multi_location_members" not in s]
        assert len(wrapped) == 1 and len(wrapped[0]["_multi_location_members"]) == 2
        assert len(singleton) == 1 and singleton[0]["relevant_file"] == "c.cpp"

    def test_does_not_mutate_input_list_or_dicts(self):
        tool = _tool()
        original = [_sugg(source_task_id="SUG-001"), _sugg(source_task_id="SUG-001")]
        snapshot = [dict(s) for s in original]
        tool._group_multi_location_suggestions(original)
        assert original == snapshot


class TestGenerateSummarizedSuggestionsWithGrouping:
    def test_multi_location_group_renders_as_a_single_row_with_all_diffs(self):
        tool = _tool()
        suggestions = [
            _sugg(relevant_file="a.cpp", relevant_lines_start=1, relevant_lines_end=1,
                  existing_code="x1", improved_code="y1", one_sentence_summary="缺少 NaN/Inf 防护",
                  label="数值稳定性", score=9, source_task_id="SUG-001"),
            _sugg(relevant_file="a.cpp", relevant_lines_start=50, relevant_lines_end=50,
                  existing_code="x2", improved_code="y2", one_sentence_summary="缺少 NaN/Inf 防护",
                  label="数值稳定性", score=9, source_task_id="SUG-001"),
            _sugg(relevant_file="b.hpp", relevant_lines_start=10, relevant_lines_end=10,
                  existing_code="x3", improved_code="y3", one_sentence_summary="缺少 NaN/Inf 防护",
                  label="数值稳定性", score=9, source_task_id="SUG-001"),
        ]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})

        # Exactly ONE row/details for this group, not three.
        assert body.count("<details><summary>缺少 NaN/Inf 防护") == 1
        # All three diffs still present inside that one details block.
        assert "x1" in body and "y1" in body
        assert "x2" in body and "y2" in body
        assert "x3" in body and "y3" in body
        assert "a.cpp" in body and "b.hpp" in body
        # Location count is surfaced in the summary line.
        assert "涉及 3 处代码位置" in body

    def test_normal_suggestions_without_grouping_render_one_row_each(self):
        tool = _tool()
        suggestions = [
            _sugg(relevant_file="a.cpp", one_sentence_summary="issue A"),
            _sugg(relevant_file="b.cpp", one_sentence_summary="issue B"),
        ]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert body.count("<details><summary>issue A") == 1
        assert body.count("<details><summary>issue B") == 1
        assert "处代码位置" not in body


class TestOneClickApplicabilityNote:
    def test_normal_suggestion_shows_can_be_applied_note(self):
        tool = _tool()
        suggestions = [_sugg(one_sentence_summary="锁内执行字符串拼接")]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert "锁内执行字符串拼接（可一键应用修改）" in body

    def test_copy_patch_suggestion_shows_file_not_in_diff_note(self):
        tool = _tool()
        suggestions = [_sugg(one_sentence_summary="缺少 NaN/Inf 防护", resolved_by_stage="tier2_copy_patch")]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert "缺少 NaN/Inf 防护（文件不在本次改动范围内，需回本地修改）" in body
        assert "可一键应用修改" not in body

    def test_tier2_heavy_one_click_suggestion_shows_can_be_applied_note(self):
        tool = _tool()
        suggestions = [_sugg(one_sentence_summary="锁竞争风险", resolved_by_stage="tier2_heavy")]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert "锁竞争风险（可一键应用修改）" in body

    def test_multi_location_group_all_one_click_shows_can_be_applied_note(self):
        tool = _tool()
        suggestions = [
            _sugg(relevant_file="a.cpp", one_sentence_summary="缺少 NaN/Inf 防护",
                  source_task_id="SUG-001", resolved_by_stage="tier2_heavy"),
            _sugg(relevant_file="b.hpp", one_sentence_summary="缺少 NaN/Inf 防护",
                  source_task_id="SUG-001", resolved_by_stage="tier2_heavy"),
        ]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert "可一键应用修改" in body
        assert "需回本地修改" not in body

    def test_multi_location_group_any_copy_patch_member_makes_whole_group_not_appliable(self):
        # Per explicit requirement: if even ONE location in the group can't
        # be one-click applied, the WHOLE group is displayed as not
        # appliable -- no partial "3 can, 2 can't" breakdown.
        tool = _tool()
        suggestions = [
            _sugg(relevant_file="a.cpp", one_sentence_summary="缺少 NaN/Inf 防护",
                  source_task_id="SUG-001", resolved_by_stage="tier2_heavy"),
            _sugg(relevant_file="b.hpp", one_sentence_summary="缺少 NaN/Inf 防护",
                  source_task_id="SUG-001", resolved_by_stage="tier2_copy_patch"),
        ]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert "需回本地修改" in body
        assert "可一键应用修改" not in body



class TestJumpLinkRendering:
    def test_jump_link_rendered_outside_details_when_inline_note_url_present(self):
        tool = _tool()
        suggestions = [_sugg(inline_note_url="https://gl/mr/1#note_42")]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        # The link must appear AFTER the closing </details> tag (sibling, not child).
        details_close = body.index("</details>")
        link_pos = body.index("https://gl/mr/1#note_42")
        assert link_pos > details_close
        assert "点击跳转至应用建议处" in body

    def test_no_jump_link_when_inline_note_url_absent(self):
        tool = _tool()
        suggestions = [_sugg()]
        body = tool.generate_summarized_suggestions({"code_suggestions": suggestions})
        assert "点击跳转至应用建议处" not in body
        assert "Click to jump to the applied suggestion" not in body
