from pathlib import Path

from pr_agent.suggestions.project_prompt_rules import (
    EffectiveProjectSkill,
    ProjectRuleSet,
    PromptRule,
    filter_project_rules,
)
from pr_agent.suggestions.prompt_provenance import build_prompt_provenance, compute_global_prompt_set_hash


def test_global_hash_changes_with_prompt_content(tmp_path: Path):
    prompt = tmp_path / "a.toml"
    prompt.write_text("x = 1\n", encoding="utf-8")
    first = compute_global_prompt_set_hash(tmp_path, prompt_paths=("a.toml",))
    prompt.write_text("x = 2\n", encoding="utf-8")
    second = compute_global_prompt_set_hash(tmp_path, prompt_paths=("a.toml",))
    assert first != second


def test_bundle_hash_separates_project_rules(tmp_path: Path):
    prompt = tmp_path / "a.toml"
    prompt.write_text("x = 1\n", encoding="utf-8")
    empty = ProjectRuleSet("eabot/cook")
    ruled = ProjectRuleSet("eabot/cook", (PromptRule("r1", ("generation",), "Be strict."),))
    templates = {"generation:0:system": "sys", "generation:0:user": "user"}

    first = build_prompt_provenance(empty, templates, settings_root=tmp_path, prompt_paths=("a.toml",))
    second = build_prompt_provenance(ruled, templates, settings_root=tmp_path, prompt_paths=("a.toml",))

    assert first.global_prompt_set_hash == second.global_prompt_set_hash
    assert first.project_rules_hash != second.project_rules_hash
    assert first.prompt_bundle_hash != second.prompt_bundle_hash
    assert first.as_record()["prompt_bundle_hash"] == first.prompt_bundle_hash
    assert first.global_prompt_set_hash
    assert first.project_rules_hash
    assert first.prompt_bundle_hash


def test_bundle_hash_uses_only_effective_language_rules(tmp_path: Path):
    prompt = tmp_path / "a.toml"
    prompt.write_text("x = 1\n", encoding="utf-8")
    all_rules = ProjectRuleSet("eabot/cook", (
        PromptRule("py", ("generation",), "Python rule.", ("python",)),
        PromptRule("cpp", ("generation",), "C++ rule.", ("cpp",)),
    ))
    templates = {"generation:0:system": "sys", "generation:0:user": "user"}

    python_rules = filter_project_rules(all_rules, {"python"})
    cpp_rules = filter_project_rules(all_rules, {"cpp"})
    python = build_prompt_provenance(python_rules, templates, settings_root=tmp_path, prompt_paths=("a.toml",))
    cpp = build_prompt_provenance(cpp_rules, templates, settings_root=tmp_path, prompt_paths=("a.toml",))

    assert python.project_rules_hash != cpp.project_rules_hash
    assert python.prompt_bundle_hash != cpp.prompt_bundle_hash


def test_provenance_freezes_target_sha_rules_files_and_references(tmp_path: Path):
    prompt = tmp_path / "a.toml"
    prompt.write_text("x = 1\n", encoding="utf-8")
    rule = PromptRule("api", ("improve",), "Check API.", ("cpp",))
    effective = EffectiveProjectSkill(
        project="eabot/cook",
        target="improve",
        target_branch="main",
        target_sha="sha-123",
        status="loaded",
        manifest_hash="manifest-hash",
        skill_hash="skill-hash",
        rules=(rule,),
        matched_files=(("api", ("src/api.cc",)),),
        references=(),
    )

    provenance = build_prompt_provenance(
        ProjectRuleSet("eabot/cook", (rule,)),
        {"generation:user": "user"},
        settings_root=tmp_path,
        prompt_paths=("a.toml",),
        effective_skill=effective,
    )
    record = provenance.as_record()

    assert record["project_rules_hash"] == "skill-hash"
    assert record["project_skill_target_sha"] == "sha-123"
    assert record["project_skill_rule_ids_json"] == '["api"]'
    assert '"src/api.cc"' in record["project_skill_matched_files_json"]
