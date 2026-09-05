from datetime import datetime, timezone

from pr_agent.feedback.timez import (BEIJING_TZ, now_cn, now_cn_iso, to_cn,
                                     to_cn_display)


class TestBeijingTime:
    def test_offset_is_plus_eight(self):
        assert now_cn().utcoffset().total_seconds() == 8 * 3600
        assert BEIJING_TZ.utcoffset(None).total_seconds() == 8 * 3600

    def test_now_iso_carries_beijing_offset(self):
        assert now_cn_iso().endswith("+08:00")

    def test_legacy_utc_string_converts_to_beijing(self):
        # 10:59 UTC == 18:59 Beijing
        assert to_cn_display("2026-06-24T10:59:47.819621+00:00") == "2026-06-24 18:59"

    def test_already_beijing_string_unchanged(self):
        assert to_cn_display("2026-06-24T18:59:47.819621+08:00") == "2026-06-24 18:59"

    def test_z_suffix_is_treated_as_utc(self):
        assert to_cn_display("2026-06-24T10:00:00Z") == "2026-06-24 18:00"

    def test_naive_string_assumed_utc(self):
        assert to_cn_display("2026-06-24T10:00:00") == "2026-06-24 18:00"

    def test_datetime_input_is_converted(self):
        dt = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)
        assert to_cn(dt).hour == 18
        assert to_cn_display(dt) == "2026-06-24 18:00"

    def test_empty_and_unparsable_fall_back(self):
        assert to_cn(None) is None
        assert to_cn("") is None
        assert to_cn_display("") == ""
        # unparsable values are returned trimmed rather than raising
        assert to_cn_display("not-a-date") == "not-a-date"

    def test_custom_format(self):
        assert to_cn_display("2026-06-24T10:00:00Z", fmt="%Y/%m/%d %H:%M:%S") == "2026/06/24 18:00:00"
