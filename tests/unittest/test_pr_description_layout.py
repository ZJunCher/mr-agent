from pr_agent.tools.pr_description import PRDescription


def test_format_description_two_columns_keeps_summary_and_outputs_vertical_sections():
    source = """
## 变更说明
核心变更内容。

## 变更类型
- [x] 新功能 (feat)

## 相关需求/问题
- 飞书需求/问题ID: m-12345
- 链接: [需求链接](https://example.com)

## 测试说明
### 测试用例
- [x] 集成测试已添加

### 测试结果
- 单元测试通过率:{}
- 集成测试通过率:{}

## 影响范围
- **影响模块**: 用户认证模块
""".strip()

    result = PRDescription._format_description_two_columns(source, "feat: add feature m-12345")

    assert result.startswith("<details open>\n<summary><h3>变更说明</h3></summary>")
    assert "<table>" not in result
    assert result.count("<details open>") == 3
    assert result.count("<details>") == 0
    assert "<summary><h3>变更类型</h3></summary>" not in result
    assert "<summary><h3>影响范围</h3></summary>" in result
    assert "<summary><h3>测试说明</h3></summary>" in result
    assert "新功能 (feat)" not in result
    assert "变更类型" not in result
    assert "需求链接：https://project.feishu.cn/eabot/issue/detail/12345" in result
    assert "相关需求/问题" not in result
    assert "实车测试" in result
    assert "仿真测试" in result
    assert "单元测试已添加" not in result
    assert "集成测试已添加" not in result
    assert "实车测试已完成" not in result
    assert "仿真测试已完成" not in result
    assert "测试报告：" in result
    assert "测试通过率：" not in result
    assert "## 测试说明" not in result
    assert "测试用例" not in result
    assert "测试结果" not in result
    assert "图示详解" not in result
    assert "文件详解" not in result

    first = result.find("<summary><h3>变更说明</h3></summary>")
    third = result.find("<summary><h3>影响范围</h3></summary>")
    req = result.find("需求链接：https://project.feishu.cn/eabot/issue/detail/12345")
    test_sec = result.find("<summary><h3>测试说明</h3></summary>")
    assert first < third < req < test_sec


def test_format_description_two_columns_noop_when_missing_sections():
    source = "## 变更说明\n仅有一个区块"

    result = PRDescription._format_description_two_columns(source)

    assert result == source


def test_format_description_two_columns_keeps_issue_link_placeholder_when_title_has_no_issue_id():
    source = """
## 变更说明
核心变更内容。

## 变更类型
- [x] 新功能 (feat)

## 相关需求/问题
- 飞书需求/问题ID: m-12345

## 测试说明
- [x] 集成测试已添加

## 影响范围
- 模块A
""".strip()

    result = PRDescription._format_description_two_columns(source, "feat: add feature without issue")

    assert "需求链接：https://project.feishu.cn/eabot/issue/detail/[在此填入问题ID]" in result
