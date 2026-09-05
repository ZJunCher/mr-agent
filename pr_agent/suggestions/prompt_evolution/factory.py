"""Production dependency assembly for the weekly Prompt evolution service."""
from __future__ import annotations

import os
import re
import socket
from datetime import datetime

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.eval.store import find_replayable_runs, find_replayable_runs_by_review_ids
from pr_agent.feedback.store import DEFAULT_DB_PATH
from pr_agent.suggestions.prompt_evolution.agent import PromptEvolutionAgent
from pr_agent.suggestions.prompt_evolution.aggregator import select_eligible_candidates
from pr_agent.suggestions.prompt_evolution.clusterer import cluster_evidence_async
from pr_agent.suggestions.prompt_evolution.evidence_loader import SqliteEvidenceLoader
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import GitLabPromptPublisher
from pr_agent.suggestions.prompt_evolution.high_fidelity_evaluator import ProjectSkillHighFidelityEvaluator
from pr_agent.suggestions.prompt_evolution.lease import PromptEvolutionLeaseManager
from pr_agent.suggestions.prompt_evolution.model_client import ToolCallingModelClient
from pr_agent.suggestions.prompt_evolution.models import CandidateScope
from pr_agent.suggestions.prompt_evolution.prompt_high_fidelity_evaluator import (
    GlobalPromptHighFidelityEvaluator,
)
from pr_agent.suggestions.prompt_evolution.project_skill_runner import (
    ProjectSkillEvolutionRunner,
    PromptEvolutionCoordinator,
)
from pr_agent.suggestions.prompt_evolution.project_skill_evaluator import ProjectSkillReplayEvaluator
from pr_agent.suggestions.prompt_evolution.runner import _CN, PromptEvolutionRunner, PromptEvolutionUnavailable
from pr_agent.suggestions.prompt_evolution.store import PromptEvolutionStore
from pr_agent.suggestions.prompt_evolution.validator import validate_proposal
from ut_agent.model_failover import build_model_health_store

def _required_text(settings, key: str) -> str:
    value = str(settings.get(key, "") or "").strip()
    if not value:
        raise PromptEvolutionUnavailable(f"missing required setting: {key}")
    return value


def _validate_prompt_evolution_config(cfg) -> tuple[str, ...]:
    if not str(cfg.target_project or "").strip():
        raise PromptEvolutionUnavailable("prompt_evolution.target_project must be configured")
    if not str(cfg.target_branch or "").strip():
        raise PromptEvolutionUnavailable("prompt_evolution.target_branch must be configured")
    models = tuple(str(model).strip() for model in (cfg.models or ()) if str(model).strip())
    if not models:
        raise PromptEvolutionUnavailable("prompt_evolution.models must contain at least one model")
    if int(cfg.model_max_attempts) <= 0:
        raise PromptEvolutionUnavailable("prompt_evolution.model_max_attempts must be positive")
    if not bool(cfg.project_skill_optimizer_enabled):
        raise PromptEvolutionUnavailable("project Skill optimizer must be enabled in production")
    gate_mode = str(cfg.project_skill_optimizer_gate_mode or "").lower()
    if gate_mode not in {"shadow", "enforce"}:
        raise PromptEvolutionUnavailable("project_skill_optimizer_gate_mode must be shadow or enforce")
    edit_budget = int(cfg.project_skill_optimizer_edit_budget)
    max_edit_budget = int(cfg.project_skill_optimizer_max_edit_budget)
    if edit_budget <= 0 or max_edit_budget <= 0 or edit_budget > max_edit_budget:
        raise PromptEvolutionUnavailable("invalid project Skill optimizer edit budget")
    selection_ratio = float(cfg.project_skill_optimizer_selection_ratio)
    if not 0 < selection_ratio < 1:
        raise PromptEvolutionUnavailable("project Skill optimizer selection ratio must be between zero and one")
    if int(cfg.project_skill_optimizer_min_train_mrs) < 1:
        raise PromptEvolutionUnavailable("project Skill optimizer requires training MRs")
    if int(cfg.project_skill_optimizer_min_selection_mrs) < 1:
        raise PromptEvolutionUnavailable("project Skill optimizer requires selection MRs")
    if int(cfg.project_skill_optimizer_min_control_cases) < 1:
        raise PromptEvolutionUnavailable("project Skill optimizer requires accepted controls")
    if int(cfg.project_skill_optimizer_max_selection_cases) < 2:
        raise PromptEvolutionUnavailable("project Skill optimizer selection case limit is too small")
    minimum_delta = float(cfg.project_skill_optimizer_minimum_score_delta)
    if not 0 <= minimum_delta <= 1:
        raise PromptEvolutionUnavailable("project Skill optimizer minimum score delta is invalid")
    if int(cfg.project_skill_optimizer_rejected_buffer_size) < 1:
        raise PromptEvolutionUnavailable("project Skill optimizer rejected buffer must be positive")
    if not bool(getattr(cfg, "project_skill_high_fidelity_enabled", False)):
        raise PromptEvolutionUnavailable("project Skill high-fidelity evaluation must be enabled in production")
    high_fidelity_mode = str(getattr(cfg, "project_skill_high_fidelity_gate_mode", "") or "").lower()
    if high_fidelity_mode not in {"shadow", "enforce"}:
        raise PromptEvolutionUnavailable("project_skill_high_fidelity_gate_mode must be shadow or enforce")
    min_mrs = int(getattr(cfg, "project_skill_high_fidelity_min_mrs", 0))
    max_mrs = int(getattr(cfg, "project_skill_high_fidelity_max_mrs", 0))
    if min_mrs < 1 or max_mrs < min_mrs:
        raise PromptEvolutionUnavailable("invalid project Skill high-fidelity MR limits")
    canary_percent = int(getattr(cfg, "project_skill_canary_percent", 0))
    if canary_percent < 0 or canary_percent > 100:
        raise PromptEvolutionUnavailable("project Skill canary percent must be between zero and one hundred")
    if bool(getattr(cfg, "project_skill_canary_enabled", False)):
        approved_ref = str(getattr(cfg, "project_skill_canary_approved_ref", "") or "")
        if canary_percent <= 0 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", approved_ref):
            raise PromptEvolutionUnavailable("enabled project Skill canary requires an immutable approved ref")
    if not bool(getattr(cfg, "global_prompt_high_fidelity_enabled", False)):
        raise PromptEvolutionUnavailable("global Prompt high-fidelity evaluation must be enabled in production")
    global_gate_mode = str(getattr(cfg, "global_prompt_high_fidelity_gate_mode", "") or "").lower()
    if global_gate_mode != "enforce":
        raise PromptEvolutionUnavailable("global_prompt_high_fidelity_gate_mode must be enforce")
    global_min_mrs = int(getattr(cfg, "global_prompt_high_fidelity_min_mrs", 0))
    global_max_mrs = int(getattr(cfg, "global_prompt_high_fidelity_max_mrs", 0))
    if global_min_mrs < 1 or global_max_mrs < global_min_mrs:
        raise PromptEvolutionUnavailable("invalid global Prompt high-fidelity MR limits")
    global_minimum_delta = float(getattr(cfg, "global_prompt_high_fidelity_minimum_score_delta", -1))
    if not 0 <= global_minimum_delta <= 1:
        raise PromptEvolutionUnavailable("global Prompt high-fidelity minimum score delta is invalid")
    return models


def _feedback_db_path(settings) -> str:
    return str(
        settings.get("pr_code_suggestions.inline_suggestions_storage_path", "")
        or settings.get("pr_feedback.storage_path", "")
        or DEFAULT_DB_PATH
    )


def _build_completion(handler):
    async def _completion(**kwargs):
        from litellm import acompletion

        call_kwargs = dict(kwargs)
        api_base, api_key = handler._resolve_endpoint(call_kwargs.get("model", ""))
        if api_base:
            call_kwargs["api_base"] = api_base
        if api_key:
            call_kwargs["api_key"] = api_key
        return await acompletion(**call_kwargs)

    return _completion


class PromptEvolutionClusterer:
    def __init__(self, client, model: str, system_prefix: str, user_template: str):
        self.client = client
        self.model = model
        self.system_prefix = system_prefix
        self.user_template = user_template

    async def cluster(self, *, evidence, system_prefix="", user_template=""):
        return await cluster_evidence_async(
            self.client,
            self.model,
            evidence,
            self.system_prefix,
            self.user_template,
        )


class PromptEvolutionAggregator:
    def select(self, clusters, thresholds, global_hash):
        return select_eligible_candidates(clusters, thresholds, global_hash)


class PromptEvolutionValidator:
    def validate(self, proposal, candidates, workspace, **limits):
        return validate_proposal(proposal, candidates, workspace, **limits)


def build_runner_from_settings(
    *,
    now: datetime | None = None,
    settings=None,
    redis_factory_cls=RedisClientFactory,
    gitlab_factory=None,
    llm_handler_factory=LiteLLMAIHandler,
    model_health_store=None,
) -> PromptEvolutionRunner | PromptEvolutionCoordinator:
    """Build the production runner without performing any GitLab write or model call."""
    settings = settings or get_settings()
    cfg = settings.prompt_evolution
    models = _validate_prompt_evolution_config(cfg)

    redis_url = str(os.getenv("PR_AGENT_REDIS_URL", "") or "").strip()
    if not redis_url:
        raise PromptEvolutionUnavailable("missing required environment variable: PR_AGENT_REDIS_URL")
    redis_client = redis_factory_cls(redis_url).create_async()

    gitlab_url = _required_text(settings, "GITLAB.URL")
    token = _required_text(settings, "GITLAB.PERSONAL_ACCESS_TOKEN")
    auth_type = str(settings.get("GITLAB.AUTH_TYPE", "oauth_token") or "oauth_token").lower()
    if auth_type not in {"oauth_token", "private_token"}:
        raise PromptEvolutionUnavailable("GITLAB.AUTH_TYPE must be oauth_token or private_token")
    if gitlab_factory is None:
        import gitlab

        gitlab_factory = gitlab.Gitlab
    auth_kwargs = {auth_type: token}
    gl = gitlab_factory(
        url=gitlab_url,
        ssl_verify=bool(settings.get("GITLAB.SSL_VERIFY", True)),
        **auth_kwargs,
    )
    project = gl.projects.get(cfg.target_project)
    actual_project = str(getattr(project, "path_with_namespace", "") or "")
    if actual_project and actual_project != str(cfg.target_project):
        raise PromptEvolutionUnavailable("GitLab returned an unexpected target project")

    db_path = _feedback_db_path(settings)
    store = PromptEvolutionStore(db_path)
    evidence_loader = SqliteEvidenceLoader(db_path)
    leases = PromptEvolutionLeaseManager(redis_client)
    publisher = GitLabPromptPublisher(project)

    def project_publisher_factory(project_path: str) -> GitLabPromptPublisher:
        business_project = gl.projects.get(project_path)
        actual = str(getattr(business_project, "path_with_namespace", "") or project_path)
        if actual != project_path:
            raise PromptEvolutionUnavailable("GitLab returned an unexpected Project Skill repository")
        return GitLabPromptPublisher(business_project)

    handler = llm_handler_factory()
    owner = f"{os.getenv('HOSTNAME', '') or socket.gethostname()}:{os.getpid()}:prompt-evolution"
    health_store = model_health_store or build_model_health_store()
    client = ToolCallingModelClient(
        completion=_build_completion(handler),
        models=models,
        attempts_per_model=int(cfg.model_max_attempts),
        health_store=health_store,
        owner=owner,
    )
    agent = PromptEvolutionAgent(client, model=models[0])
    evaluator_client = ToolCallingModelClient(
        completion=_build_completion(handler),
        models=models[1:],
        attempts_per_model=int(cfg.model_max_attempts),
        health_store=health_store,
        owner=f"{owner}:project-skill-evaluator",
    )
    evaluator = ProjectSkillReplayEvaluator(evaluator_client, model=models[1])
    high_fidelity_evaluator = ProjectSkillHighFidelityEvaluator(
        lambda project_path, mr_iids: find_replayable_runs(project_path, mr_iids, path=db_path),
        min_mrs=int(cfg.project_skill_high_fidelity_min_mrs),
        max_mrs=int(cfg.project_skill_high_fidelity_max_mrs),
        command="improve",
    )
    global_high_fidelity_evaluator = GlobalPromptHighFidelityEvaluator(
        lambda project_path, mr_iids: find_replayable_runs(project_path, mr_iids, path=db_path),
        review_record_loader=lambda review_ids: find_replayable_runs_by_review_ids(review_ids, path=db_path),
        min_mrs=int(cfg.global_prompt_high_fidelity_min_mrs),
        max_mrs=int(cfg.global_prompt_high_fidelity_max_mrs),
        command="improve",
    )
    cluster_prompt = settings.prompt_evolution_cluster_prompt
    clusterer = PromptEvolutionClusterer(
        client,
        models[0],
        str(cluster_prompt.system),
        str(cluster_prompt.user),
    )

    common = {
        "settings": settings,
        "store": store,
        "leases": leases,
        "evidence_loader": evidence_loader,
        "clusterer": clusterer,
        "aggregator": PromptEvolutionAggregator(),
        "agent": agent,
        "validator": PromptEvolutionValidator(),
        "owner": owner,
        "now": now or datetime.now(_CN),
    }
    global_runner = PromptEvolutionRunner(
        **common,
        publisher=publisher,
        candidate_scopes=frozenset({CandidateScope.GLOBAL}),
        high_fidelity_evaluator=global_high_fidelity_evaluator,
        behavioral_model=models[0],
    )
    project_runner = ProjectSkillEvolutionRunner(
        **common,
        publisher_factory=project_publisher_factory,
        evaluator=evaluator,
        high_fidelity_evaluator=high_fidelity_evaluator,
    )
    return PromptEvolutionCoordinator(project_runner, global_runner)
