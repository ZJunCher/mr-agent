from pr_agent.tools.doc_drift_report import (
    _parse_location,
    _render_doc_block,
    build_drift_report,
    filter_and_sort_results,
    make_link_builder,
)


def _result(path, stale, severity="high", suggestion="fix it"):
    return {
        "doc_path": path,
        "is_stale": stale,
        "severity": severity,
        "conflicts": [{"doc_excerpt": "top_k=5", "diff_reason": "changed to 8",
                       "code_location": "src/foo.cpp:157"}],
        "suggestion": suggestion,
    }


class TestFilterAndSort:
    def test_drops_non_stale(self):
        results = [_result("a.md", False), _result("b.md", True)]
        kept = filter_and_sort_results(results, "medium")
        assert [r["doc_path"] for r in kept] == ["b.md"]

    def test_severity_threshold(self):
        results = [
            _result("low.md", True, "low"),
            _result("med.md", True, "medium"),
            _result("high.md", True, "high"),
        ]
        kept = filter_and_sort_results(results, "medium")
        paths = [r["doc_path"] for r in kept]
        assert "low.md" not in paths
        assert paths == ["high.md", "med.md"]  # sorted desc

    def test_ignores_malformed_entries(self):
        results = [None, "nope", _result("ok.md", True)]
        kept = filter_and_sort_results(results, "medium")
        assert [r["doc_path"] for r in kept] == ["ok.md"]


class TestBuildReport:
    def test_none_when_no_drift(self):
        assert build_drift_report([_result("a.md", False)], "medium") is None

    def test_collapsed_structure(self):
        report = build_drift_report([_result("rag/README.md", True, "high")], "medium", is_zh=True)
        assert report is not None
        # outer fold + one per-doc fold = 2 <details>
        assert report.count("<details>") == 2
        assert report.count("</details>") == 2
        assert "🔴 high" in report
        assert "rag/README.md" in report
        assert "点击展开" in report
        # title must NOT contain "最高"
        assert "最高" not in report

    def test_non_collapsed_still_has_per_doc_details(self):
        report = build_drift_report(
            [_result("a.md", True, "high")], "medium", is_zh=False, collapsed=False
        )
        assert report.startswith("### ")
        # no outer fold, but per-doc fold still present
        assert report.count("<details>") == 1

    def test_english_labels(self):
        report = build_drift_report([_result("a.md", True, "high")], "medium", is_zh=False)
        assert "Doc drift" in report
        assert "Suggestion" in report
        assert "highest" not in report  # title no longer shows highest severity

    def test_multiple_docs_sorted(self):
        results = [_result("m.md", True, "medium"), _result("h.md", True, "high")]
        report = build_drift_report(results, "medium", is_zh=False)
        assert report.index("h.md") < report.index("m.md")

    def test_single_blank_line_between_docs(self):
        results = [_result("h.md", True, "high"), _result("m.md", True, "medium")]
        report = build_drift_report(results, "medium", is_zh=False)
        # never two consecutive blank lines in the output
        assert "\n\n\n" not in report

    def test_doc_blocks_are_wrapped_in_compact_list_items(self):
        results = [_result("h.md", True, "high"), _result("m.md", True, "medium")]
        report = build_drift_report(results, "medium", is_zh=True)
        assert '<ul type="none">' in report
        assert report.count("<li><details>") == 2
        assert "</details>\n<details>" not in report


class TestSanitizeExcerpt:
    def test_strips_code_fence(self):
        from pr_agent.tools.doc_drift_report import _sanitize_excerpt
        raw = "```python\nfoo = 1\n```"
        result = _sanitize_excerpt(raw)
        assert "```" not in result

    def test_strips_table_separator(self):
        from pr_agent.tools.doc_drift_report import _sanitize_excerpt
        raw = "| --- | --- |\n| val | val2 |"
        result = _sanitize_excerpt(raw)
        assert "|" not in result   # all pipes removed
        assert "val" in result     # data still present

    def test_strips_heading_section_number(self):
        from pr_agent.tools.doc_drift_report import _sanitize_excerpt
        result = _sanitize_excerpt("#### 4. Output timeout")
        assert "####" not in result
        assert "4." not in result
        assert "Output timeout" in result

    def test_strips_table_row_index(self):
        from pr_agent.tools.doc_drift_report import _sanitize_excerpt
        raw = "| 3 | `kOutputTimeout` | 输出超时 | **Error** | — | 距上次定位 | 1.0 s |"
        result = _sanitize_excerpt(raw)
        assert "|" not in result
        assert "3" not in result    # leading row index stripped
        assert "kOutputTimeout" in result
        assert "Error" in result

    def test_no_truncation_for_long_text(self):
        from pr_agent.tools.doc_drift_report import _sanitize_excerpt
        long = "x" * 200
        result = _sanitize_excerpt(long)
        # No longer truncates with ellipsis — full text returned
        assert "…" not in result
        assert result == long

    def test_no_triple_backtick_in_output(self):
        from pr_agent.tools.doc_drift_report import _sanitize_excerpt
        raw = "Some text\n```\ncommand --args\n```\n-- more text"
        assert "```" not in _sanitize_excerpt(raw)


class TestParseLocation:
    def test_path_only(self):
        assert _parse_location("src/foo.cpp") == ("src/foo.cpp", None, None)

    def test_path_with_line(self):
        assert _parse_location("src/foo.cpp:157") == ("src/foo.cpp", 157, None)

    def test_path_with_range(self):
        assert _parse_location("src/foo.cpp:120-134") == ("src/foo.cpp", 120, 134)

    def test_empty(self):
        assert _parse_location("") == ("", None, None)


class TestRenderWithLinks:
    def test_doc_path_is_clickable_link(self):
        def lb(path, start=None, end=None):
            return f"https://host/blob/{path}#L{start}"
        block = _render_doc_block(_result("docs/x.md", True, "high"), is_zh=True, link_builder=lb)
        # doc header rendered as a markdown link (blue)
        assert "[**🔴 high · docs/x.md**](https://host/blob/docs/x.md#L-1)" in block

    def test_conflict_includes_code_location_link(self):
        def lb(path, start=None, end=None):
            suffix = f"#L{start}" if start else ""
            return f"https://host/blob/{path}{suffix}"
        block = _render_doc_block(_result("docs/x.md", True, "high"), is_zh=True, link_builder=lb)
        assert "代码位置" in block
        assert "[src/foo.cpp:157](https://host/blob/src/foo.cpp#L157)" in block

    def test_no_link_builder_falls_back_to_bold(self):
        block = _render_doc_block(_result("docs/x.md", True, "high"), is_zh=True, link_builder=None)
        assert "**🔴 high · docs/x.md**" in block
        assert "[**" not in block  # no markdown link
        # code location shown as plain text
        assert "src/foo.cpp:157" in block

    def test_make_link_builder_from_provider(self):
        class FakeProvider:
            def get_line_link(self, path, start, end=None):
                return f"URL:{path}:{start}:{end}"
        lb = make_link_builder(FakeProvider())
        assert lb("a.md", -1, None) == "URL:a.md:-1:None"

    def test_make_link_builder_none_when_unsupported(self):
        assert make_link_builder(object()) is None
