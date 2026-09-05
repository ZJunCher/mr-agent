from pr_agent.algo.language_router import (
    get_improve_prompt_pairs,
    get_review_prompt_pairs,
    improve_prompt_pair_languages,
    language_scopes_for_mode,
)
from pr_agent.config_loader import get_settings


def test_get_review_prompt_pairs_defaults_to_v1_when_use_v2_omitted():
    pairs = get_review_prompt_pairs("cpp")
    assert pairs == [(get_settings().pr_review_prompt.system, get_settings().pr_review_prompt.user)]


def test_get_review_prompt_pairs_selects_v2_cpp():
    # 2026-07: use_v2=True 现在路由到 v3 提示词（曾经是 v2，现在生产环境已切换到 v3）
    pairs = get_review_prompt_pairs("cpp", use_v2=True)
    assert pairs == [(get_settings().pr_review_prompt_v3.system, get_settings().pr_review_prompt_v3.user)]


def test_get_review_prompt_pairs_selects_v2_python():
    pairs = get_review_prompt_pairs("python", use_v2=True)
    assert pairs == [(get_settings().pr_review_prompt_python_v3.system, get_settings().pr_review_prompt_python_v3.user)]


def test_get_review_prompt_pairs_selects_v2_mixed():
    pairs = get_review_prompt_pairs("mixed", use_v2=True)
    assert pairs == [
        (get_settings().pr_review_prompt_v3.system, get_settings().pr_review_prompt_v3.user),
        (get_settings().pr_review_prompt_python_v3.system, get_settings().pr_review_prompt_python_v3.user),
    ]


def test_get_improve_prompt_pairs_defaults_to_v1_when_use_v2_omitted():
    base_sys, base_usr = "BASE_SYS", "BASE_USR"
    pairs = get_improve_prompt_pairs("cpp", base_sys, base_usr)
    assert pairs == [(base_sys, base_usr)]


def test_get_improve_prompt_pairs_selects_v2_python():
    base_sys, base_usr = "BASE_SYS", "BASE_USR"
    pairs = get_improve_prompt_pairs("python", base_sys, base_usr, use_v2=True)
    assert pairs == [(
        get_settings().pr_code_suggestions_prompt_python_v3.system,
        get_settings().pr_code_suggestions_prompt_python_v3.user,
    )]


def test_get_improve_prompt_pairs_selects_v2_mixed():
    base_sys, base_usr = "BASE_SYS", "BASE_USR"
    pairs = get_improve_prompt_pairs("mixed", base_sys, base_usr, use_v2=True)
    assert pairs == [
        (base_sys, base_usr),
        (
            get_settings().pr_code_suggestions_prompt_python_v3.system,
            get_settings().pr_code_suggestions_prompt_python_v3.user,
        ),
    ]


def test_improve_prompt_pair_languages_match_prompt_pair_count():
    base_sys, base_usr = "BASE_SYS", "BASE_USR"
    for mode in ("python", "cpp", "mixed", "other"):
        assert len(improve_prompt_pair_languages(mode)) == len(
            get_improve_prompt_pairs(mode, base_sys, base_usr)
        )

    assert improve_prompt_pair_languages("mixed") == (
        frozenset({"cpp"}),
        frozenset({"python"}),
    )
    assert language_scopes_for_mode("other") == frozenset()
