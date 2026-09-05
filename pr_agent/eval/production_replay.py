"""Side-effect-free replay through the production review/improve generation paths."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from pr_agent.algo.utils import load_yaml
from pr_agent.config_loader import get_settings, task_settings_override
from pr_agent.eval.benchmark_provider import BenchmarkGitProvider
from pr_agent.eval.conditions import EvaluationConditionManifest, build_condition_manifest
from pr_agent.suggestions.project_prompt_rules import ProjectSkillSession


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProductionReplayRequest:
    project: str
    mr_iid: str
    pr_url: str
    base_sha: str
    head_sha: str
    target_sha: str
    input_snapshot: dict
    skill_content: str
    command: str
    model: str
    captured_at: str


@dataclass(frozen=True)
class NormalizedReviewItem:
    file_path: str
    line_start: int
    line_end: int
    label: str
    summary: str
    content: str
    fingerprint: str


@dataclass(frozen=True)
class ProductionReplayResult:
    status: str
    command: str
    output: object = None
    normalized_items: tuple[NormalizedReviewItem, ...] = ()
    coverage_status: str = "failed"
    condition: EvaluationConditionManifest | None = None
    output_hash: str = ""
    error_code: str = ""
    error: str = ""


@contextmanager
def _read_only_settings(settings):
    missing = object()
    updates = {
        "config.publish_output": False,
        "config.publish_output_progress": False,
        "eval.enable_capture": False,
    }
    previous = {key: settings.get(key, missing) for key in updates}
    try:
        for key, value in updates.items():
            settings.set(key, value)
        yield
    finally:
        for key, value in previous.items():
            if value is missing and hasattr(settings, "unset"):
                settings.unset(key)
            elif value is not missing:
                settings.set(key, value)


def _line_number(value: object) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


def _normalized_item(raw: dict) -> NormalizedReviewItem:
    file_path = str(raw.get("relevant_file") or raw.get("file_path") or "").strip()
    start = _line_number(raw.get("relevant_lines_start", raw.get("relevant_line", raw.get("line_start"))))
    end = _line_number(raw.get("relevant_lines_end", raw.get("relevant_line", raw.get("line_end")))) or start
    label = str(raw.get("label") or raw.get("severity") or "").strip()
    summary = str(raw.get("one_sentence_summary") or raw.get("summary") or "").strip()
    content = str(raw.get("suggestion_content") or raw.get("suggestion") or "").strip()
    fingerprint = _digest({
        "file": file_path.casefold(),
        "start": start,
        "end": end,
        "label": label.casefold(),
        "summary": " ".join(summary.casefold().split()),
        "content": " ".join(content.casefold().split()),
    })
    return NormalizedReviewItem(file_path, start, end, label, summary, content, fingerprint)


def _normalize_output(command: str, tool, output: object) -> tuple[NormalizedReviewItem, ...]:
    if command == "improve":
        raw_items = output.get("code_suggestions", []) if isinstance(output, dict) else []
    else:
        prediction = str(getattr(tool, "prediction", "") or "")
        parsed = load_yaml(prediction, first_key="review", last_key="security_concerns") or {}
        review = parsed.get("review", {}) if isinstance(parsed, dict) else {}
        raw_items = review.get("key_issues_to_review", []) if isinstance(review, dict) else []
    return tuple(_normalized_item(item) for item in raw_items if isinstance(item, dict))


def _diff_hash(provider) -> str:
    return _digest([
        {
            "filename": str(getattr(file, "filename", "") or ""),
            "patch": str(getattr(file, "patch", "") or ""),
        }
        for file in provider.get_diff_files()
    ])


def _prompt_templates(tool, command: str, settings) -> dict:
    if command == "improve":
        pairs = getattr(tool, "_improve_prompt_pairs", ())
        return {f"{index}:{side}": value for index, pair in enumerate(pairs)
                for side, value in zip(("system", "user"), pair)}
    use_v3 = bool(settings.get("pr_reviewer.code_graph.enabled", False))
    section = settings.get("pr_review_prompt_v3" if use_v3 else "pr_review_prompt", None)
    if section is not None and getattr(section, "system", None) is not None:
        return {"system": str(section.system), "user": str(section.user)}
    return {"tool": tool.__class__.__name__, "vars": getattr(tool, "vars", {})}


def _config_snapshot(settings) -> dict:
    keys = (
        "config.temperature",
        "config.max_model_tokens",
        "pr_reviewer.code_graph.enabled",
        "large_mr_review.enabled",
        "large_mr_review.max_chunks",
        "large_mr_review.max_concurrency",
        "large_mr_review.output_buffer_tokens",
        "large_mr_review.chunk_metadata_tokens",
        "large_mr_review.fail_closed",
    )
    return {key: settings.get(key, None) for key in keys}


async def run_production_replay(
    request: ProductionReplayRequest,
    *,
    settings=None,
    provider_factory: Callable = BenchmarkGitProvider,
    reviewer_factory: Callable | None = None,
    improve_factory: Callable | None = None,
) -> ProductionReplayResult:
    """Run a frozen snapshot through production generation without external writes."""
    settings = settings or get_settings()
    if request.command not in {"review", "improve"}:
        return ProductionReplayResult("error", request.command, error_code="invalid_command",
                                      error="command must be review or improve")
    try:
        provider = provider_factory(
            request.pr_url,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            input_snapshot=request.input_snapshot,
        )
        session = ProjectSkillSession.from_content(
            provider, request.project, request.skill_content, request.target_sha,
        )
    except Exception as exc:
        return ProductionReplayResult("error", request.command, error_code="invalid_project_skill", error=str(exc))

    try:
        with task_settings_override(settings), _read_only_settings(settings):
            if request.command == "review":
                if reviewer_factory is None:
                    from pr_agent.tools.pr_reviewer import PRReviewer

                    reviewer_factory = PRReviewer
                tool = reviewer_factory(
                    request.pr_url, git_provider=provider, project_skill_session=session,
                )
                await tool._prepare_prediction(request.model)
                output = tool._prepare_pr_review() if getattr(tool, "prediction", None) else ""
                schema = "PRReview:v1"
                parser = "review-yaml:v1"
            else:
                if improve_factory is None:
                    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

                    improve_factory = PRCodeSuggestions
                tool = improve_factory(
                    request.pr_url, git_provider=provider, project_skill_session=session,
                )
                output = await tool.generate_suggestions_data()
                schema = "PRCodeSuggestions:v1"
                parser = "improve-yaml:v1"

            coverage_status = str(getattr(getattr(tool, "review_coverage", None), "status", "failed"))
            plan_hash = str(getattr(getattr(tool, "review_chunk_plan", None), "plan_hash", ""))
            effective = getattr(tool, "project_skill_effective", None)
            skill_hash = str(getattr(effective, "skill_hash", "") or session.rule_set.manifest_hash)
            templates = _prompt_templates(tool, request.command, settings)
            prompt_hash = _digest(templates)
            context_hash = _digest(str(getattr(tool, "related_files_context", "") or ""))
            condition = build_condition_manifest(
                project=request.project,
                mr_iid=request.mr_iid,
                command=request.command,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                target_sha=request.target_sha,
                model=request.model,
                temperature=float(settings.get("config.temperature", 0.0) or 0.0),
                max_model_tokens=int(settings.get("config.max_model_tokens", 0) or 0),
                global_prompt_set_hash=prompt_hash,
                prompt_bundle_hash=_digest({"command": request.command, "templates": templates}),
                config=_config_snapshot(settings),
                diff_hash=_diff_hash(provider),
                chunk_plan_hash=plan_hash,
                context_hash=context_hash,
                output_schema=schema,
                parser_version=parser,
                skill_hash=skill_hash,
                captured_at=request.captured_at,
            )
            normalized = _normalize_output(request.command, tool, output)
            return ProductionReplayResult(
                "ok",
                request.command,
                output=output,
                normalized_items=normalized,
                coverage_status=coverage_status,
                condition=condition,
                output_hash=_digest(output),
            )
    except Exception as exc:
        return ProductionReplayResult(
            "error", request.command, error_code="replay_execution_failed", error=str(exc),
        )
