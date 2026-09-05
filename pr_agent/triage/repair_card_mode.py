from enum import StrEnum

from pr_agent.config_loader import get_settings


class RepairCardMode(StrEnum):
    MULTI_SELECT = "multi_select"
    UNIFIED = "unified"
    LEGACY_ACTIONS = "legacy_actions"


def _enabled(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def repair_card_mode() -> RepairCardMode:
    settings = get_settings()
    explicit = settings.get("FEISHU.REPAIR_CARD_MODE")
    if explicit is not None and str(explicit).strip():
        try:
            return RepairCardMode(str(explicit).strip().lower())
        except ValueError as exc:
            supported = ", ".join(mode.value for mode in RepairCardMode)
            raise ValueError(f"unsupported feishu.repair_card_mode; expected one of: {supported}") from exc
    if _enabled(settings.get("FEISHU.UNIFIED_PIPELINE_REPAIR", True)):
        return RepairCardMode.UNIFIED
    if _enabled(settings.get("FEISHU.MULTI_ACTION_REPAIR_CARDS", True)):
        return RepairCardMode.LEGACY_ACTIONS
    return RepairCardMode.UNIFIED
