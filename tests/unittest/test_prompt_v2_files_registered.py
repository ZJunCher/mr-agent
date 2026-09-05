from pr_agent.config_loader import get_settings


def test_all_five_v2_prompt_sections_are_loaded():
    settings = get_settings()
    for key in [
        "pr_review_prompt_v2",
        "pr_review_prompt_python_v2",
        "pr_code_suggestions_prompt_v2",
        "pr_code_suggestions_prompt_not_decoupled_v2",
        "pr_code_suggestions_prompt_python_v2",
    ]:
        section = settings.get(key)
        assert section is not None, f"{key} was not loaded"
        assert section.system, f"{key}.system is empty"
        assert section.user, f"{key}.user is empty"
