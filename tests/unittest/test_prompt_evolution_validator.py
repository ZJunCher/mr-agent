import hashlib
from pathlib import Path

from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.models import (
    MISSING_FILE_HASH,
    CandidateScope,
    EligibleCandidate,
    Evidence,
    Outcome,
    PromptChangeKind,
    PromptFileChange,
    PromptProposal,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.prompt_surface import GENERATION_ALL
from pr_agent.suggestions.prompt_evolution.validator import validate_proposal


def _hash_content(content: str | None) -> str:
    if content is None:
        return MISSING_FILE_HASH
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _workspace_for(paths: tuple[str, ...]) -> PromptWorkspace:
    files = {path: Path(path).read_text(encoding="utf-8") if Path(path).is_file() else None for path in paths}
    return PromptWorkspace("group/pr-agent", "main", "a" * 40, files)


def _validator_candidate(scope: CandidateScope, project: str | None = None) -> EligibleCandidate:
    evidence = Evidence(
        suggestion_id="s1",
        project=project or "eabot/cook",
        mr_iid="1",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/1",
        created_at="2026-08-01T00:00:00+08:00",
        file_path="src/a.py",
        label="bug",
        summary="summary",
        suggestion_content="content",
        outcome=Outcome.REJECTED,
        weight=1.0,
        global_prompt_set_hash="g1",
        prompt_bundle_hash="b1",
    )
    cluster = WeightedCluster("cluster", (evidence,), 0.0, 1.0, 1.0)
    return EligibleCandidate("c1", scope, project, "b1" if project else "g1", cluster)


def _proposal(path: str, family: str = "tier1_repair", content: str = "x = 1\n"):
    workspace = _workspace_for((path,))
    change = PromptFileChange(path, family, _hash_content(workspace.files[path]), content, ("s1",))
    scope = CandidateScope.PROJECT if family == "project_rule" else CandidateScope.GLOBAL
    project = "eabot/cook" if scope is CandidateScope.PROJECT else None
    candidates = (_validator_candidate(scope, project),)
    proposal = PromptProposal("test", PromptChangeKind.CONSERVATIVE_TIGHTENING, ("s1",), (change,))
    return proposal, workspace, candidates


def test_rejects_python_file_even_when_model_proposes_it():
    proposal, workspace, candidates = _proposal(path="pr_agent/tools/pr_code_suggestions.py")
    report = validate_proposal(proposal, candidates, workspace)
    assert not report.passed
    assert "path_not_allowed" in report.errors


def test_generation_all_requires_every_mirror():
    path = sorted(GENERATION_ALL)[0]
    workspace = _workspace_for(tuple(sorted(GENERATION_ALL)))
    content = str(workspace.files[path]) + "\n# tighten repeated false positives\n"
    proposal = PromptProposal("test", PromptChangeKind.CONSERVATIVE_TIGHTENING, ("s1",), (
        PromptFileChange(path, "generation_all", _hash_content(workspace.files[path]), content, ("s1",)),
    ))
    candidates = (_validator_candidate(CandidateScope.GLOBAL),)
    report = validate_proposal(proposal, candidates, workspace)
    assert not report.passed
    assert "mirror_family_incomplete:generation_all" in report.errors


def test_project_rule_path_must_match_project_field():
    path = ".pr_agent/skills/review/skill.toml"
    base_content = 'schema_version = 1\nproject = "eabot/cook"\n'
    workspace = PromptWorkspace("eabot/cook", "main", "a" * 40, {path: base_content})
    candidates = (_validator_candidate(CandidateScope.PROJECT, "eabot/cook"),)
    proposal = PromptProposal("test", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(
            path,
            "project_rule",
            _hash_content(base_content),
            'schema_version = 1\nproject = "eabot/other"\n',
            ("s1",),
        ),
    ))
    report = validate_proposal(proposal, candidates, workspace)
    assert "project_rule_path_mismatch" in report.errors


def test_valid_prompt_only_change_passes():
    path = "pr_agent/settings/pr_tier1_repair_prompts.toml"
    workspace = _workspace_for((path,))
    content = str(workspace.files[path]) + "\n# reviewed conservative tightening\n"
    proposal = PromptProposal("test", PromptChangeKind.CONSERVATIVE_TIGHTENING, ("s1",), (
        PromptFileChange(path, "tier1_repair", _hash_content(workspace.files[path]), content, ("s1",)),
    ))
    candidates = (_validator_candidate(CandidateScope.GLOBAL),)
    report = validate_proposal(proposal, candidates, workspace)
    assert report.passed


def test_python_project_evidence_requires_python_scoped_new_rule():
    path = ".pr_agent/skills/review/skill.toml"
    base_content = 'schema_version = 1\nproject = "eabot/cook"\n'
    workspace = PromptWorkspace("eabot/cook", "main", "a" * 40, {path: base_content})
    valid_content = (
        'schema_version = 1\nproject = "eabot/cook"\n'
        '[[rules]]\nid = "python-rule"\ntargets = ["improve"]\n'
        'languages = ["python"]\ninstruction = "Require direct Python evidence."\n'
    )
    invalid_content = valid_content.replace('languages = ["python"]\n', "")
    candidates = (_validator_candidate(CandidateScope.PROJECT, "eabot/cook"),)

    valid = PromptProposal("test", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(path, "project_rule", _hash_content(base_content), valid_content, ("s1",)),
    ))
    invalid = PromptProposal("test", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(path, "project_rule", _hash_content(base_content), invalid_content, ("s1",)),
    ))

    assert validate_proposal(valid, candidates, workspace).passed
    assert "project_rule_language_mismatch" in validate_proposal(invalid, candidates, workspace).errors


def test_rejects_proposal_over_configured_diff_line_limit():
    path = "pr_agent/settings/pr_tier1_repair_prompts.toml"
    workspace = _workspace_for((path,))
    content = str(workspace.files[path]) + "\n" + "\n".join(f"# added {index}" for index in range(601))
    proposal = PromptProposal("test", PromptChangeKind.CONSERVATIVE_TIGHTENING, ("s1",), (
        PromptFileChange(path, "tier1_repair", _hash_content(workspace.files[path]), content, ("s1",)),
    ))

    report = validate_proposal(
        proposal,
        (_validator_candidate(CandidateScope.GLOBAL),),
        workspace,
        max_diff_lines=600,
    )

    assert not report.passed
    assert "diff_too_large" in report.errors


def test_project_skill_evolution_rejects_rule_deletion_and_reference_change():
    path = ".pr_agent/skills/review/skill.toml"
    base = (
        'schema_version = 1\nname = "cook"\nproject = "eabot/cook"\n'
        '[[rules]]\nid = "api"\ntargets = ["improve"]\nlanguages = ["python"]\n'
        'instruction = "Check API compatibility."\n'
    )
    workspace = PromptWorkspace("eabot/cook", "main", "a" * 40, {path: base})
    candidates = (_validator_candidate(CandidateScope.PROJECT, "eabot/cook"),)

    deletion = PromptProposal("delete", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(
            path,
            "project_rule",
            _hash_content(base),
            'schema_version = 1\nname = "cook"\nproject = "eabot/cook"\n',
            ("s1",),
        ),
    ))
    reference_change = PromptProposal("reference", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(
            path,
            "project_rule",
            _hash_content(base),
            base.replace(
                'instruction = "Check API compatibility."\n',
                'instruction = "Check API compatibility with direct evidence."\n'
                'references = ["references/api.md"]\n',
            ),
            ("s1",),
        ),
    ))

    assert "project_rule_deletion" in validate_proposal(deletion, candidates, workspace).errors
    assert "project_rule_reference_change" in validate_proposal(reference_change, candidates, workspace).errors


def test_project_skill_evolution_requires_existing_opt_in_manifest():
    path = ".pr_agent/skills/review/skill.toml"
    workspace = PromptWorkspace("eabot/cook", "main", "a" * 40, {path: None})
    candidates = (_validator_candidate(CandidateScope.PROJECT, "eabot/cook"),)
    content = (
        'schema_version = 1\nproject = "eabot/cook"\n'
        '[[rules]]\nid = "api"\ntargets = ["improve"]\nlanguages = ["python"]\n'
        'instruction = "Check API compatibility."\n'
    )
    proposal = PromptProposal("create", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(path, "project_rule", MISSING_FILE_HASH, content, ("s1",)),
    ))

    assert "project_skill_not_opted_in" in validate_proposal(proposal, candidates, workspace).errors


def test_project_skill_evolution_enforces_semantic_edit_budget():
    path = ".pr_agent/skills/review/skill.toml"
    base = (
        'schema_version = 1\nname = "cook"\nproject = "eabot/cook"\n'
        '[[rules]]\nid = "api"\ntargets = ["improve"]\nlanguages = ["python"]\n'
        'instruction = "Check API compatibility."\n'
    )
    content = base.replace(
        'instruction = "Check API compatibility."',
        'instruction = "Check API compatibility with direct evidence."',
    ) + (
        '[[rules]]\nid = "tests"\ntargets = ["improve"]\nlanguages = ["python"]\n'
        'instruction = "Require tests for behavior changes."\n'
    )
    workspace = PromptWorkspace("eabot/cook", "main", "a" * 40, {path: base})
    candidates = (_validator_candidate(CandidateScope.PROJECT, "eabot/cook"),)
    proposal = PromptProposal("two edits", PromptChangeKind.SPECIFIC_RULE, ("s1",), (
        PromptFileChange(path, "project_rule", _hash_content(base), content, ("s1",)),
    ))

    report = validate_proposal(proposal, candidates, workspace, max_project_rule_edits=1)

    assert not report.passed
    assert "textual_learning_rate_exceeded" in report.errors
