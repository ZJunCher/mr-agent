"""End-to-end acceptance tests for the weekly Prompt evolution flow.

Fully mocked: no real GitLab, Redis, or LiteLLM. Exercises the runner state
machine from lease acquisition through Draft MR publication.
"""
import asyncio
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from pr_agent.suggestions.project_prompt_rules import parse_project_rules, project_rules_hash
from pr_agent.suggestions.prompt_evolution.agent import PromptEvolutionAgent
from pr_agent.suggestions.prompt_evolution.aggregator import select_eligible_candidates
from pr_agent.suggestions.prompt_evolution.clusterer import (
    ClusterAssignment,
    ClusterEnvelope,
    cluster_evidence_async,
)
from pr_agent.suggestions.prompt_evolution.evidence_loader import SqliteEvidenceLoader
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import GitLabPromptPublisher
from pr_agent.suggestions.prompt_evolution.lease import EvolutionLease
from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    EvolutionRunStatus,
    Outcome,
    PromptChangeKind,
    PromptFileChange,
    PromptProposal,
    SourceSnapshot,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.runner import PromptEvolutionRunner
from pr_agent.suggestions.prompt_evolution.store import PromptEvolutionStore
from pr_agent.suggestions.prompt_evolution.validator import validate_proposal
from pr_agent.suggestions.prompt_provenance import compute_global_prompt_set_hash

NOW = datetime(2026, 8, 14, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET_PROJECT = "group/pr-agent"
TARGET_BRANCH = "main"
BASE_SHA = "a" * 40
GLOBAL_HASH = "global-v1"
BASE_SKILL = 'schema_version = 1\nname = "cook"\nproject = "eabot/cook"\n'
BASE_SKILL_HASH = hashlib.sha256(BASE_SKILL.encode()).hexdigest()
BASE_RULES_HASH = project_rules_hash(parse_project_rules(BASE_SKILL, "eabot/cook"))


class StaticToolClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call(self, model, system, user, tool_name, result_model):
        self.calls.append((model, system, user, tool_name))
        return self.result


class FakeGitLabProject:
    """Minimal python-gitlab-like project recording created MRs/commits."""

    def __init__(self, base_sha=BASE_SHA):
        self.branches = SimpleNamespace(
            items={"main": SimpleNamespace(commit={"id": base_sha})},
            created=[],
            get=self._get_branch,
            create=self._create_branch,
        )
        self.commits = SimpleNamespace(
            items=[SimpleNamespace(id="c" * 40, message="chore(prompt): weekly improve evolution\n\nPrompt-Evolution-Batch: 2026-w33-test")],
            created=[],
            get=self._get_commit,
            create=self._create_commit,
            list=self._list_commits,
        )
        mr = SimpleNamespace(iid="1", web_url="https://gitlab.example/group/pr-agent/-/merge_requests/1",
                              state="opened", source_branch="codex/prompt-evolution/2026-w33-test", merge=Mock())
        self.mergerequests = SimpleNamespace(
            items=[mr],
            created=[],
            get=self._get_mr,
            create=self._create_mr,
            list=self._list_mrs,
        )
        self.files = SimpleNamespace(get=self._get_file)
        self.created_mrs = 0
        self.last_mr = None
        self.last_commit = None

    def _get_branch(self, name):
        if name not in self.branches.items:
            err = RuntimeError("not found")
            err.response_code = 404
            raise err
        return self.branches.items[name]

    def _create_branch(self, payload):
        self.branches.created.append(payload)
        self.branches.items[payload["branch"]] = SimpleNamespace(commit={"id": payload["ref"]})

    def _get_commit(self, sha):
        for c in self.commits.items:
            if c.id == sha:
                return c
        return self.commits.items[0]

    def _create_commit(self, payload):
        self.commits.created.append(payload)
        self.last_commit = payload
        return SimpleNamespace(id="c" * 40, message=payload["commit_message"])

    def _list_commits(self, **kwargs):
        return list(self.commits.items)

    def _get_mr(self, iid):
        for mr in self.mergerequests.items:
            if mr.iid == iid:
                return mr
        return self.mergerequests.items[0]

    def _create_mr(self, payload):
        self.mergerequests.created.append(payload)
        self.created_mrs += 1
        mr = SimpleNamespace(iid="1", web_url="https://gitlab.example/group/pr-agent/-/merge_requests/1",
                             state="opened", source_branch=payload.get("source_branch", ""),
                             merge=Mock())
        self.last_mr = payload
        return mr

    def _list_mrs(self, **kwargs):
        return list(self.mergerequests.items)

    def _get_file(self, path, ref=None):
        if path == ".pr_agent/skills/review/skill.toml":
            return type("File", (), {"decode": lambda self: BASE_SKILL})()
        content = Path(path).read_text(encoding="utf-8")
        return type("File", (), {"decode": lambda self: content})()


def _evidence(suggestion_id, project="eabot/cook", mr_iid="1", outcome=Outcome.REJECTED,
              feedback=(), file_path="src/a.py"):
    return Evidence(
        suggestion_id=suggestion_id, project=project, mr_iid=mr_iid,
        mr_url=f"https://gl/{project}/-/merge_requests/{mr_iid}",
        created_at="2026-08-01T00:00:00+08:00", file_path=file_path,
        label="bug", summary="summary", suggestion_content="content",
        outcome=outcome, weight=1.0 if outcome in (Outcome.ACCEPTED, Outcome.REJECTED) else 0.25,
        global_prompt_set_hash=GLOBAL_HASH, prompt_bundle_hash=f"bundle:{project}:v1",
        project_rules_hash=BASE_RULES_HASH,
        feedback=feedback,
    )


def _settings(enabled=True):
    return SimpleNamespace(
        prompt_evolution=SimpleNamespace(
            enabled=enabled, target_project=TARGET_PROJECT, target_branch=TARGET_BRANCH,
            branch_prefix="codex/prompt-evolution", window_days=90, unhandled_after_days=14,
            accepted_weight=1.0, rejected_weight=1.0, unhandled_weight=0.25,
            project_min_negative_weight=3.0, project_min_negative_ratio=0.70,
            project_min_mrs=2, unhandled_only_min_count=12, unhandled_only_min_mrs=3,
            global_min_negative_weight=5.0, global_min_negative_ratio=0.70,
            global_min_projects=2, global_min_mrs=3,
            closed_candidate_cooldown_days=30, max_candidates_per_run=20,
            max_files_per_mr=20, max_diff_lines=600, max_prompt_file_chars=200000,
            lease_seconds=300, model_max_retries=2, model="test-model",
        ),
    )


def _project_rule_proposal(evidence_ids, change_kind="specific_rule"):
    path = ".pr_agent/skills/review/skill.toml"
    content = (BASE_SKILL + '[[rules]]\nid = "r1"\n'
               'targets = ["review", "improve"]\nlanguages = ["python"]\ninstruction = "Be strict."\n')
    return PromptProposal(
        rationale="Reduce speculative suggestions",
        change_kind=PromptChangeKind(change_kind),
        evidence_ids=tuple(evidence_ids),
        changes=(PromptFileChange(path=path, family="project_rule",
                                  expected_base_sha256=BASE_SKILL_HASH,
                                  content=content, evidence_ids=tuple(evidence_ids)),),
    )


def _make_runner(gitlab_project, tool_client, evidence, *, enabled=True):
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store = PromptEvolutionStore(tmp.name)
    leases = Mock()
    leases.acquire = AsyncMock(return_value=EvolutionLease(TARGET_PROJECT, "worker", 1))
    leases.renew = AsyncMock(return_value=True)
    leases.assert_current = AsyncMock()
    leases.release = AsyncMock(return_value=True)

    publisher = GitLabPromptPublisher(gitlab_project)

    evidence_loader = Mock()
    evidence_loader.load = Mock(return_value=SourceSnapshot(evidence, "2026-08-01T00:00:00+08:00", True))

    # Clusterer returns one cluster containing all evidence.
    cluster_key = "speculative"
    cluster = WeightedCluster(cluster_key, evidence, 0.0,
                              sum(e.weight for e in evidence), 1.0)

    clusterer = Mock()
    clusterer.cluster = Mock(return_value=((cluster,), ()))

    aggregator = Mock()
    candidate = EligibleCandidate(
        candidate_id="c1", scope=CandidateScope.PROJECT, project="eabot/cook",
        source_prompt_hash="bundle:eabot/cook:v1", cluster=cluster,
    )
    aggregator.select = Mock(return_value=(candidate,))

    agent = PromptEvolutionAgent(tool_client, model="test-model")

    validator = Mock()
    validator.validate = Mock(
        side_effect=lambda proposal, candidates, workspace, **limits: validate_proposal(
            proposal, candidates, workspace, **limits
        )
    )

    return PromptEvolutionRunner(
        settings=_settings(enabled=enabled), store=store, leases=leases,
        publisher=publisher, evidence_loader=evidence_loader,
        clusterer=clusterer, aggregator=aggregator, agent=agent,
        validator=validator, owner="worker", now=NOW,
    ), leases


def test_full_weekly_flow_publishes_draft_mr():
    evidence = (
        _evidence("s1", mr_iid="1", feedback=("too speculative",)),
        _evidence("s2", mr_iid="2", feedback=()),
        _evidence("s3", mr_iid="3", feedback=()),
    )
    proposal = _project_rule_proposal(["s1", "s2", "s3"], change_kind="specific_rule")
    tool_client = StaticToolClient(proposal)
    gitlab = FakeGitLabProject()
    runner, leases = _make_runner(gitlab, tool_client, evidence, enabled=True)

    result = asyncio.run(runner.run(dry_run=False))

    assert result.status is EvolutionRunStatus.MR_OPEN
    assert gitlab.created_mrs == 1
    assert gitlab.last_mr["title"].startswith("Draft:")
    assert all(action["file_path"].endswith(".toml") for action in gitlab.last_commit["actions"])
    assert gitlab.last_commit["actions"][0]["file_path"] == ".pr_agent/skills/review/skill.toml"
    assert "Offline behavioural evaluation: NOT RUN" not in gitlab.last_mr["description"]
    assert "disabled for this runner" in gitlab.last_mr["description"]
    leases.release.assert_awaited()


def test_twelve_unhandled_accepts_conservative_tightening():
    evidence = tuple(
        _evidence(f"s{i}", mr_iid=str(i // 4), outcome=Outcome.UNHANDLED, feedback=())
        for i in range(12)
    )
    # Compute the expected base hash from the fake GitLab file content.
    base_content = Path("pr_agent/settings/pr_tier1_repair_prompts.toml").read_text(encoding="utf-8")
    expected_base_sha = hashlib.sha256(base_content.encode("utf-8")).hexdigest()
    proposal = PromptProposal(
        rationale="tighten trigger",
        change_kind=PromptChangeKind.CONSERVATIVE_TIGHTENING,
        evidence_ids=tuple(e.suggestion_id for e in evidence),
        changes=(PromptFileChange(
            path="pr_agent/settings/pr_tier1_repair_prompts.toml",
            family="tier1_repair", expected_base_sha256=expected_base_sha,
            content=base_content + "\n# tighten repeated false positives\n",
            evidence_ids=tuple(e.suggestion_id for e in evidence),
        ),),
    )
    tool_client = StaticToolClient(proposal)
    gitlab = FakeGitLabProject()
    runner, leases = _make_runner(gitlab, tool_client, evidence, enabled=True)
    runner.aggregator.select = Mock(return_value=(EligibleCandidate(
        candidate_id="c1", scope=CandidateScope.GLOBAL, project=None,
        source_prompt_hash=GLOBAL_HASH,
        cluster=WeightedCluster("ck", evidence, 0.0, sum(e.weight for e in evidence), 1.0),
    ),))

    result = asyncio.run(runner.run(dry_run=False))
    assert result.status is EvolutionRunStatus.MR_OPEN
    leases.release.assert_awaited()


def test_all_pending_makes_no_model_or_gitlab_calls():
    evidence = (_evidence("s1", outcome=Outcome.PENDING, feedback=()),)
    gitlab = FakeGitLabProject()
    tool_client = StaticToolClient(None)
    runner, leases = _make_runner(gitlab, tool_client, evidence, enabled=True)
    # Override evidence loader to report no new signal for pending evidence.
    runner.evidence_loader.load = Mock(return_value=SourceSnapshot((), "2026-08-01T00:00:00+08:00", False))

    result = asyncio.run(runner.run(dry_run=False))
    assert result.status is EvolutionRunStatus.COMPLETED_NO_CHANGE
    assert tool_client.calls == []
    assert gitlab.created_mrs == 0
    leases.release.assert_awaited()


def _seed_real_evidence(path, global_hash):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE published_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, updated_at TEXT, suggestion_id TEXT,
            project TEXT, mr_iid TEXT, mr_url TEXT, file_path TEXT,
            label TEXT, one_sentence_summary TEXT, suggestion_content TEXT,
            applied_at TEXT, resolved_at TEXT, global_prompt_set_hash TEXT,
            project_rules_hash TEXT, prompt_bundle_hash TEXT
        );
        CREATE TABLE inline_suggestion_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, project TEXT, mr_iid TEXT,
            suggestion_id TEXT, comment TEXT
        );
        CREATE TABLE mr_inventory (
            project_path TEXT, mr_iid TEXT, state TEXT, updated_at TEXT
        );
    """)
    for index in range(1, 4):
        conn.execute(
            "INSERT INTO published_suggestions "
            "(created_at, updated_at, suggestion_id, project, mr_iid, mr_url, file_path, label, "
            "one_sentence_summary, suggestion_content, resolved_at, global_prompt_set_hash, "
            "project_rules_hash, prompt_bundle_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-08-01T00:00:00+08:00", "2026-08-10T00:00:00+08:00", f"s{index}",
                "eabot/cook", str(index), f"https://gl/eabot/cook/-/merge_requests/{index}",
                f"src/a{index}.py", "bug", "speculative", "avoid speculation",
                "2026-08-10T00:00:00+08:00", global_hash, BASE_RULES_HASH, "bundle:eabot/cook:v1",
            ),
        )
        conn.execute(
            "INSERT INTO mr_inventory VALUES (?, ?, ?, ?)",
            ("eabot/cook", str(index), "opened", "2026-08-10T00:00:00+08:00"),
        )
    conn.execute(
        "INSERT INTO inline_suggestion_feedback "
        "(created_at, project, mr_iid, suggestion_id, comment) VALUES (?, ?, ?, ?, ?)",
        ("2026-08-10T00:00:00+08:00", "eabot/cook", "1", "s1", "too speculative"),
    )
    conn.commit()
    conn.close()


def test_production_shape_uses_real_sqlite_aggregation_and_reuses_one_draft_mr(tmp_path):
    global_hash = compute_global_prompt_set_hash()
    db_path = tmp_path / "feedback.db"
    store = PromptEvolutionStore(str(db_path))
    _seed_real_evidence(db_path, global_hash)
    evidence_ids = ("s1", "s2", "s3")
    proposal = _project_rule_proposal(evidence_ids, change_kind="specific_rule")

    class RoutingToolClient:
        async def call(self, model, system, user, tool_name, result_model):
            if tool_name == "submit_feedback_clusters":
                return ClusterEnvelope(clusters=[
                    ClusterAssignment(cluster_key="speculative", evidence_ids=["E1", "E2", "E3"])
                ])
            return proposal

    client = RoutingToolClient()

    class Clusterer:
        async def cluster(self, *, evidence, system_prefix, user_template):
            return await cluster_evidence_async(client, "test-model", evidence, "cluster", "evidence")

    class Aggregator:
        def select(self, clusters, thresholds, current_global_hash):
            return select_eligible_candidates(clusters, thresholds, current_global_hash)

    class Validator:
        def validate(self, proposal, candidates, workspace, **limits):
            return validate_proposal(proposal, candidates, workspace, **limits)

    leases = Mock()
    leases.acquire = AsyncMock(return_value=EvolutionLease(TARGET_PROJECT, "worker", 1))
    leases.renew = AsyncMock(return_value=True)
    leases.assert_current = AsyncMock()
    leases.release = AsyncMock(return_value=True)
    gitlab = FakeGitLabProject()
    runner = PromptEvolutionRunner(
        settings=_settings(enabled=True),
        store=store,
        leases=leases,
        publisher=GitLabPromptPublisher(gitlab),
        evidence_loader=SqliteEvidenceLoader(str(db_path)),
        clusterer=Clusterer(),
        aggregator=Aggregator(),
        agent=PromptEvolutionAgent(client, model="test-model"),
        validator=Validator(),
        owner="worker",
        now=NOW,
    )

    first = asyncio.run(runner.run(dry_run=False))
    second = asyncio.run(runner.run(dry_run=False))

    assert first.status is EvolutionRunStatus.MR_OPEN
    assert second.mr_url == first.mr_url
    assert len(gitlab.branches.created) == 1
    assert len(gitlab.commits.created) == 1
    assert gitlab.created_mrs == 1
    assert gitlab.last_mr["target_branch"] == TARGET_BRANCH
