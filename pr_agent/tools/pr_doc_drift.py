"""``/doc_drift`` tool: detect documentation that this MR may have made stale.

Flow (single-call "A" strategy):
  1. Read the MR diff (changed files + patches).
  2. Clone the repo and select candidate docs = global docs ∪ neighbour docs.
  3. Send (all candidate docs + diff) to a lightweight Claude model in ONE call.
  4. Parse the YAML result, filter by severity, and publish a single collapsed
     MR comment.

Everything degrades safely: any failure logs and returns without publishing,
never blocking the MR.
"""
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.utils import load_yaml
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger
from pr_agent.tools.doc_drift_report import build_drift_report, make_link_builder as _make_link_builder
from pr_agent.tools.doc_drift_selector import gather_candidate_docs

try:
    from jinja2 import Environment, StrictUndefined
except Exception:  # pragma: no cover - jinja2 is a hard dependency
    Environment = None
    StrictUndefined = None


def _cfg(key: str, default=None):
    return get_settings().get(f"doc_drift.{key}", default)


def _is_zh() -> bool:
    try:
        return str(get_settings().get("config.response_language", "en-US")).lower().startswith("zh")
    except Exception:
        return True


def _resolve_models() -> list[str]:
    """Main doc-drift model + fallbacks, deduped, with sane final fallback."""
    model = _cfg("model", "") or get_settings().get("config.model_weak", "") \
        or get_settings().config.model
    fallbacks = _cfg("fallback_models", []) or []
    if isinstance(fallbacks, str):
        fallbacks = [m.strip() for m in fallbacks.split(",") if m.strip()]
    models: list[str] = []
    for m in [model, *fallbacks]:
        if m and m not in models:
            models.append(m)
    return models


def _build_diff_str(diff_files) -> tuple[str, list[str]]:
    """Return (diff text for the prompt, list of changed filenames)."""
    parts = []
    changed = []
    for f in diff_files or []:
        filename = getattr(f, "filename", None)
        patch = getattr(f, "patch", None)
        if not filename:
            continue
        changed.append(filename)
        if patch:
            parts.append(f"## file: {filename}\n{patch}")
    return "\n\n".join(parts), changed


def _aggregate_docs(doc_map: dict[str, str]) -> str:
    out = []
    for path, content in doc_map.items():
        out.append(f"\n==doc path==\n\n{path}\n\n==doc content==\n\n{content}\n=========\n")
    return "".join(out)


class PRDocDrift:
    def __init__(self, ctx_url, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler, args: tuple = None):
        self.ctx_url = ctx_url
        self.args = args
        self.ai_handler = ai_handler()
        self.git_provider = None
        self.repo_url = ""
        try:
            self.git_provider = get_git_provider_with_context(ctx_url)
            if not self.git_provider:
                raise Exception(f"No git provider found at {ctx_url}")
            self.repo_url = get_settings().get("DOC_DRIFT.REPO_URL", "") or \
                self.git_provider.get_git_repo_url(self.ctx_url)
            if not self.repo_url:
                raise Exception(f"Unable to deduce repo url from context url: {self.ctx_url}")
        except Exception:
            get_logger().exception("doc-drift: init failed; run() will do nothing.")
            self.git_provider = None

    async def run(self):
        if not self.git_provider:
            return None
        if not _cfg("enabled", True):
            get_logger().info("doc-drift: disabled by config.")
            return None
        try:
            with get_logger().contextualize(command="doc_drift", pr_url=self.ctx_url):
                return await self._run()
        except Exception:
            get_logger().exception("doc-drift: run failed; no comment published.")
            return None

    async def detect(self) -> list[dict] | None:
        """Run drift detection and return filtered stale-doc list (may be empty).

        Returns:
            list[dict]  – filtered results (may be empty = no drift found).
            None        – model / parse error; caller should silently skip.
        """
        if not self.git_provider:
            return None
        try:
            diff_files = self.git_provider.get_diff_files()
        except Exception:
            get_logger().exception("doc-drift: failed to read diff files.")
            return None
        diff_str, changed_files = _build_diff_str(diff_files)
        if not changed_files:
            get_logger().info("doc-drift: no changed files; nothing to check.")
            return []

        doc_map = gather_candidate_docs(
            git_provider=self.git_provider,
            repo_url=self.repo_url,
            changed_files=changed_files,
            global_globs=_cfg("global_docs", ["AGENTS.md", "README.md", "docs/**/*.md"]),
            ancestor_globs=_cfg("ancestor_doc_globs", ["*.md", "README*"]),
            doc_exts=_cfg("doc_exts", [".md", ".mdx", ".rst"]),
            max_docs=int(_cfg("max_docs_per_mr", 30)),
            max_doc_chars=int(_cfg("max_doc_chars", 20000)),
        )
        if not doc_map:
            get_logger().info("doc-drift: no candidate documents to evaluate.")
            return []

        response = await self._invoke_model(diff_str, _aggregate_docs(doc_map))
        if not response:
            return None

        parsed = load_yaml(response)
        if not parsed or not isinstance(parsed, dict):
            get_logger().error("doc-drift: failed to parse model YAML.", artifacts={"response": response})
            return None
        results = parsed.get("results")
        if not isinstance(results, list):
            get_logger().warning("doc-drift: model returned no 'results' list.", artifacts={"parsed": parsed})
            return []

        from pr_agent.tools.doc_drift_report import filter_and_sort_results
        return filter_and_sort_results(results, str(_cfg("severity_threshold", "medium")))

    async def _run(self):
        kept = await self.detect()
        if kept is None:
            return None

        report = build_drift_report(
            kept,
            severity_threshold=str(_cfg("severity_threshold", "medium")),
            is_zh=_is_zh(),
            collapsed=bool(_cfg("report_collapsed", True)),
            link_builder=_make_link_builder(self.git_provider),
        )
        if not report:
            get_logger().info("doc-drift: no drift at or above threshold; nothing to publish.")
            return None

        if get_settings().config.publish_output:
            self.git_provider.publish_comment(report)
        else:
            get_logger().info("doc-drift report (not published):", artifacts={"report": report})
        return report

    async def _invoke_model(self, diff_str: str, docs_str: str) -> str | None:
        variables = {
            "diff": diff_str,
            "docs": docs_str,
            "language": "Chinese" if _is_zh() else "English",
            "is_zh": _is_zh(),
        }
        try:
            environment = Environment(undefined=StrictUndefined)
            system = environment.from_string(get_settings().pr_doc_drift_prompt.system).render(variables)
            user = environment.from_string(get_settings().pr_doc_drift_prompt.user).render(variables)
        except Exception:
            get_logger().exception("doc-drift: failed to render prompts.")
            return None

        models = _resolve_models()
        temperature = get_settings().get("config.temperature", 0.2)
        for i, model in enumerate(models):
            try:
                response, _ = await self.ai_handler.chat_completion(
                    model=model, system=system, user=user, temperature=temperature
                )
                if response:
                    return response
                get_logger().warning(f"doc-drift: empty response from {model}.")
            except Exception as e:
                get_logger().warning(f"doc-drift: model {model} failed: {e}")
            if i == len(models) - 1:
                get_logger().error("doc-drift: all models failed; degrading (no comment).")
        return None
