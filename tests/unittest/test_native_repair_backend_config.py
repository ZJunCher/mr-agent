"""Task 1: native repair backend 配置解析测试。

约束：
- 默认 backend 为 hermes，保持线上行为不变。
- native 作为新 backend，先实现配置开关再实现能力。
- 非法值必须显式报错，不能静默回退到默认（避免配置笔误导致行为漂移）。
- 测试不读取真实密钥，只验证 parse_repair_backend 的纯函数行为。
"""
import pytest

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing the eager ut_agent package.
from ut_agent.config import parse_repair_backend, REPAIR_BACKEND


class TestParseRepairBackend:
    """parse_repair_backend 是配置加载的纯函数入口。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "hermes"),
            ("", "hermes"),
            ("hermes", "hermes"),
            ("native", "native"),
            ("HERMES", "hermes"),
            ("Native", "native"),
        ],
    )
    def test_parse_repair_backend_accepts_known_values(self, value, expected):
        assert parse_repair_backend(value) == expected

    @pytest.mark.parametrize(
        "value",
        ["copilot", "claude", "auto", "true", "1", "0"],
    )
    def test_parse_repair_backend_rejects_unknown_value(self, value):
        with pytest.raises(ValueError, match="repair backend"):
            parse_repair_backend(value)

    def test_parse_repair_backend_strips_whitespace(self):
        assert parse_repair_backend("  native  ") == "native"
        assert parse_repair_backend("\thermes\n") == "hermes"


class TestRepairBackendConfig:
    """REPAIR_BACKEND 是模块加载时从 settings.toml 读取的常量。"""

    def test_repair_backend_is_known_value(self):
        assert REPAIR_BACKEND in {"hermes", "native"}

    def test_repair_backend_defaults_to_native(self):
        # 默认 backend 已切换为 native（Hermes 路径保留为回退选项）
        assert REPAIR_BACKEND == "native"
