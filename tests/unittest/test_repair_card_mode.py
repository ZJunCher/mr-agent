from pr_agent.triage import repair_card_mode as mode_module
from pr_agent.triage.repair_card_mode import RepairCardMode, repair_card_mode


class Settings:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_explicit_multi_select_mode_wins_over_legacy_booleans(monkeypatch):
    monkeypatch.setattr(
        mode_module,
        "get_settings",
        lambda: Settings(
            {
                "FEISHU.REPAIR_CARD_MODE": "multi_select",
                "FEISHU.UNIFIED_PIPELINE_REPAIR": True,
                "FEISHU.MULTI_ACTION_REPAIR_CARDS": True,
            }
        ),
    )

    assert repair_card_mode() is RepairCardMode.MULTI_SELECT


def test_mode_falls_back_to_legacy_booleans(monkeypatch):
    monkeypatch.setattr(
        mode_module,
        "get_settings",
        lambda: Settings(
            {
                "FEISHU.UNIFIED_PIPELINE_REPAIR": False,
                "FEISHU.MULTI_ACTION_REPAIR_CARDS": True,
            }
        ),
    )

    assert repair_card_mode() is RepairCardMode.LEGACY_ACTIONS
