"""
ut_agent 模型配置加载器 - 从 ut_agent/settings.toml 读取配置。
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.toml")

with open(_SETTINGS_PATH, "rb") as f:
    _cfg = tomllib.load(f)

_llm = _cfg.get("llm", {})
_agent = _cfg.get("agent", {})

# 模型配置
MODEL = _llm.get("model", "anthropic/claude-sonnet-4-5-20250929")
FALLBACK_MODEL = _llm.get("fallback_model", "anthropic/claude-haiku-4-5-20251001")
_fallback_models = _llm.get("fallback_models")
if _fallback_models is None:
    _fallback_models = [FALLBACK_MODEL] if FALLBACK_MODEL else []
elif isinstance(_fallback_models, str):
    _fallback_models = [_fallback_models]

MODEL_CANDIDATES = tuple(dict.fromkeys(
    str(model).strip() for model in [MODEL, *_fallback_models] if str(model).strip()
))
MODEL_FAILURE_COOLDOWN_SECONDS = int(_llm.get("model_failure_cooldown_seconds", 300))
MODEL_PROBE_LEASE_SECONDS = int(_llm.get("model_probe_lease_seconds", 30))
if MODEL_FAILURE_COOLDOWN_SECONDS <= 0:
    raise ValueError("llm.model_failure_cooldown_seconds must be positive")
if MODEL_PROBE_LEASE_SECONDS <= 0:
    raise ValueError("llm.model_probe_lease_seconds must be positive")
API_KEY = (
    os.environ.get("UT_AGENT_API_KEY")
    or os.environ.get("OPENAI__KEY")
    or os.environ.get("OPENAI_KEY")
    or _llm.get("api_key", "")
)
BASE_URL = (
    os.environ.get("UT_AGENT_API_BASE")
    or os.environ.get("OPENAI__API_BASE")
    or os.environ.get("OPENAI_API_BASE")
    or _llm.get("base_url", "")
)
DEFAULT_TEMPERATURE = _llm.get("temperature", 0.2)
HERMES_API_MODE = _llm.get("hermes_api_mode", "anthropic_messages")

# Agent 配置
TEST_MODE = _agent.get("test_mode", False)

# ── Repair backend 配置 ──
# backend 决定 pipeline_failed 场景如何产生工作区 diff：
#   - "hermes": 委托 Hermes CLI 子进程（当前线上默认）
#   - "native": 由 LangGraph Agent 直接调用受控编码工具
# 迁移期间保持默认 hermes，native 经测试验证后再切换。
_repair = _cfg.get("repair", {})
_REPAIR_BACKEND_KNOWN = ("hermes", "native")


def parse_repair_backend(value: object) -> str:
    """将配置值归一化为已知 repair backend 名称。

    - None / 空字符串回退到 hermes（保持线上默认行为）。
    - 已知值大小写不敏感，去除首尾空白后匹配。
    - 未知值显式抛 ValueError，避免配置笔误静默漂移到默认 backend。
    """
    if value is None:
        return "hermes"
    text = str(value).strip()
    if not text:
        return "hermes"
    lowered = text.lower()
    if lowered in _REPAIR_BACKEND_KNOWN:
        return lowered
    raise ValueError(
        f"Unknown repair backend: {value!r}. Expected one of: {', '.join(_REPAIR_BACKEND_KNOWN)}"
    )


REPAIR_BACKEND = parse_repair_backend(_repair.get("backend", "hermes"))

# 日志与 diff 读取上限（native backend 使用）
CI_LOG_READ_MAX_LINES = int(_repair.get("ci_log_read_max_lines", 200))
CI_LOG_READ_MAX_BYTES = int(_repair.get("ci_log_read_max_bytes", 32768))
REPO_SEARCH_MAX_RESULTS = int(_repair.get("repo_search_max_results", 50))
DIFF_VIEW_MAX_LINES = int(_repair.get("diff_view_max_lines", 600))


@dataclass(frozen=True)
class ValidationProfile:
    unit_test_argv: tuple[str, ...] = ()
    working_directory: str = "."
    timeout_seconds: int = 600
    lint_argv: tuple[str, ...] = ()
    build_argv: tuple[str, ...] = ()
    test_argv: tuple[str, ...] = ()

    @property
    def effective_test_argv(self) -> tuple[str, ...]:
        return self.test_argv or self.unit_test_argv

    @property
    def configured_checks(self) -> tuple[str, ...]:
        checks = []
        if self.lint_argv:
            checks.append("lint_check")
        if self.build_argv:
            checks.append("build_check")
        if self.test_argv:
            checks.append("test_check")
        elif self.unit_test_argv:
            checks.append("unit_test_check")
        return tuple(checks)


_validation = _repair.get("validation", {})
VALIDATION_DEFAULT_TIMEOUT_SECONDS = int(_validation.get("default_timeout_seconds", 600))
VALIDATION_MAX_TIMEOUT_SECONDS = int(_validation.get("max_timeout_seconds", 1200))
VALIDATION_MAX_OUTPUT_CHARS = int(_validation.get("max_output_chars", 20000))

if VALIDATION_DEFAULT_TIMEOUT_SECONDS <= 0:
    raise ValueError("repair.validation.default_timeout_seconds must be positive")
if VALIDATION_MAX_TIMEOUT_SECONDS <= 0:
    raise ValueError("repair.validation.max_timeout_seconds must be positive")
if VALIDATION_DEFAULT_TIMEOUT_SECONDS > VALIDATION_MAX_TIMEOUT_SECONDS:
    raise ValueError("repair.validation.default_timeout_seconds must not exceed max_timeout_seconds")
if VALIDATION_MAX_OUTPUT_CHARS <= 0:
    raise ValueError("repair.validation.max_output_chars must be positive")


def _parse_validation_profile(project_id: str, value: object) -> ValidationProfile:
    if not isinstance(value, dict):
        raise ValueError(f"repair.validation.profiles.{project_id} must be a table")
    def argv(name: str) -> tuple[str, ...]:
        raw = value.get(name)
        if raw is None:
            return ()
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item for item in raw):
            raise ValueError(f"repair.validation.profiles.{project_id}.{name} must be a non-empty string array")
        return tuple(raw)

    legacy_test_argv = argv("unit_test_argv")
    test_argv = argv("test_argv")
    if legacy_test_argv and test_argv:
        raise ValueError(
            f"repair.validation.profiles.{project_id} cannot define both unit_test_argv and test_argv"
        )
    lint_argv = argv("lint_argv")
    build_argv = argv("build_argv")
    if not any((legacy_test_argv, test_argv, lint_argv, build_argv)):
        raise ValueError(f"repair.validation.profiles.{project_id} must configure lint_argv, build_argv, or test_argv")
    working_directory = str(value.get("working_directory", ".")).strip() or "."
    work_path = PurePosixPath(working_directory)
    if work_path.is_absolute() or ".." in work_path.parts:
        raise ValueError(f"repair.validation.profiles.{project_id}.working_directory must stay inside the repository")
    timeout_seconds = int(value.get("timeout_seconds", VALIDATION_DEFAULT_TIMEOUT_SECONDS))
    if timeout_seconds <= 0 or timeout_seconds > VALIDATION_MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"repair.validation.profiles.{project_id}.timeout_seconds must be between 1 and "
            f"{VALIDATION_MAX_TIMEOUT_SECONDS}"
        )
    return ValidationProfile(
        unit_test_argv=legacy_test_argv,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
        lint_argv=lint_argv,
        build_argv=build_argv,
        test_argv=test_argv,
    )


_validation_profiles = {
    str(project_id): _parse_validation_profile(str(project_id), value)
    for project_id, value in (_validation.get("profiles", {}) or {}).items()
}
VALIDATION_PROFILES = MappingProxyType(_validation_profiles)


def get_validation_profile(project_id: str) -> ValidationProfile | None:
    """Return the exact configured validation profile for a GitLab project path."""
    return VALIDATION_PROFILES.get(str(project_id).strip())
