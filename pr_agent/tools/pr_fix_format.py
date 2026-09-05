"""
/fix-format 命令 - 手动触发，修复 MR 代码格式问题（仅 GitLab）。

在 MR 评论区输入 /fix-format 手动触发。流程：
1. 定位 MR 源分支流水线中失败的 format 检查 job（默认名 code_format_check，子串匹配）
2. 从 job 日志的“详情见报告:”行解析出格式报告的下载链接
3. 带鉴权直接 HTTP 下载报告（unified diff 文本，'+' 为正确格式，'-' 为原格式）
4. 解析 diff，将每个文件应用修复（用 '+' 行替换 '-' 行）
5. 通过 GitLab Commits API 将修复后的所有文件一次性提交到 MR 源分支

若 format 检查通过或不存在，则直接跳过，不做任何改动。
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Optional

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.log import get_logger
from pr_agent.triage.format_job_preflight import (
    FORMAT_CI_JOB_CONFIGURATION,
    FormatJobDisposition,
    classify_format_job_trace,
)

# 提交信息追加的标记，用于标识 fix-format bot 的自动提交
FORMAT_FIX_MARKER = "[format-bot]"

# 优先：从 job 日志的“详情见报告:”行提取报告链接，例如：
#   详情见报告: https://gitlab.xxx/group/proj/-/jobs/33956/artifacts/raw/code-format-report.txt
# 兼容全角/半角冒号、冒号前后空格。
_REPORT_LINE_RE = re.compile(r"详情见报告\s*[:：]\s*(https?://\S+)", re.IGNORECASE)

# 回退：从日志里任意 job artifact raw 链接提取 (job_id, artifact_path)。
_REPORT_URL_RE = re.compile(r"https?://\S+?/-/jobs/(\d+)/artifacts/raw/(\S+?\.txt)", re.IGNORECASE)

# unified diff hunk 头：@@ -a,b +c,d @@
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class FixFormatResult:
    pushed_sha: str = ""
    pushed_parent_sha: str = ""
    pushed_tree_sha: str = ""
    fixed_files: tuple[str, ...] = ()
    skipped_files: tuple[tuple[str, str], ...] = ()
    status_markdown: str = ""
    failure_kind: str = ""
    failure_summary: str = ""
    suggested_action: str = ""
    job_url: str = ""
    report_fingerprint: str = ""
    exact_report_applied: bool = False


class PRFixFormat:
    """流水线完成后，根据 code_format_check 报告自动修复代码格式并推送到源分支。"""

    def __init__(
        self,
        pr_url: str,
        ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler,
        args: list = None,
        *,
        seen_report_fingerprints: tuple[str, ...] = (),
    ):
        self.pr_url = pr_url
        self.git_provider = get_git_provider_with_context(pr_url)
        self.ai_handler_cls = ai_handler
        self.args = args or []

        cfg = get_settings().get("pr_fix_format", {}) or {}
        job_names = cfg.get("format_check_job_names", ["code_format_check"]) or ["code_format_check"]
        self.job_name_keywords = [str(s).lower() for s in job_names]
        self.report_artifact_fallback = cfg.get("report_artifact_name", "code-format-report.txt")
        self.enable_auto_push = bool(cfg.get("enable_auto_push", True))
        self.commit_message = cfg.get("commit_message", "style: 自动修复代码格式")
        pipeline_id = cfg.get("pipeline_id", "")
        self.pipeline_id = str(pipeline_id).strip() if pipeline_id not in (None, "") else ""
        self.seen_report_fingerprints = frozenset(str(value) for value in seen_report_fingerprints if str(value))

    async def run(self, *, publish_result: bool = True) -> FixFormatResult:
        try:
            provider = getattr(self.git_provider, "original_provider", self.git_provider)
            if not isinstance(provider, GitLabProvider):
                get_logger().info("fix_format 仅支持 GitLab，跳过")
                return FixFormatResult(status_markdown="格式自动修复仅支持 GitLab。")

            project = self.git_provider.gl.projects.get(self.git_provider.id_project)
            source_branch = self.git_provider.get_pr_branch()

            pipeline = self._resolve_pipeline(project, source_branch)
            if pipeline is None:
                get_logger().info("fix_format: 未找到关联流水线，跳过")
                return FixFormatResult(status_markdown="未找到关联流水线，未执行格式修复。")

            all_pipelines = self._collect_all_pipelines(project, pipeline)
            failed_jobs = self._find_failed_format_jobs(all_pipelines)
            if not failed_jobs:
                get_logger().info("fix_format: code_format_check 未失败，无需修复")
                return FixFormatResult(status_markdown="最新流水线中格式检查已不再失败，无需修复。")

            # 合并所有失败 format job 的报告文本
            report_text = ""
            traces: dict[int, str] = {}
            dispositions: list[FormatJobDisposition] = []
            for job in failed_jobs:
                trace = self._get_job_trace(project, job)
                traces[int(job.id)] = trace
                disposition = classify_format_job_trace(trace, job_url=self._job_url(job))
                if disposition.kind == FORMAT_CI_JOB_CONFIGURATION:
                    dispositions.append(disposition)
                text = self._get_report_text(project, job, trace=trace)
                if text:
                    report_text += ("\n" if report_text else "") + text

            if not report_text.strip():
                get_logger().warning("fix_format: 未能获取格式报告内容，跳过")
                if dispositions:
                    disposition = dispositions[0]
                    status_markdown = self._build_disposition_comment(disposition)
                    if publish_result:
                        self._safe_publish(status_markdown)
                    return FixFormatResult(
                        status_markdown=status_markdown,
                        failure_kind=disposition.kind,
                        failure_summary=disposition.summary,
                        suggested_action=disposition.suggested_action,
                        job_url=disposition.job_url,
                    )
                hints = [
                    hint
                    for job in failed_jobs
                    if (hint := self._job_failure_hint(project, job, trace=traces.get(int(job.id), "")))
                ]
                status_markdown = self._build_comment([], [], report_missing=True, detail="; ".join(hints))
                if publish_result:
                    self._safe_publish(status_markdown)
                return FixFormatResult(status_markdown=status_markdown)

            report_fingerprint = hashlib.sha256(report_text.encode("utf-8")).hexdigest()[:32]
            if report_fingerprint in self.seen_report_fingerprints:
                status_markdown = "格式报告与上一轮完全相同，已停止重复修复，避免产生循环提交。"
                return FixFormatResult(
                    status_markdown=status_markdown,
                    failure_kind="repeated_report",
                    failure_summary=status_markdown,
                    report_fingerprint=report_fingerprint,
                )

            file_hunks = self._parse_unified_diff(report_text)
            if not file_hunks:
                preview = report_text[:800].replace("\n", "\\n")
                get_logger().warning(
                    f"fix_format: 报告解析未得到可修复的文件，跳过 (报告长度={len(report_text)}, 前800字符: {preview})"
                )
                status_markdown = self._build_comment([], [], report_missing=True)
                if publish_result:
                    self._safe_publish(status_markdown)
                return FixFormatResult(status_markdown=status_markdown)

            # 应用 diff，收集每个文件的修复后内容
            changes: dict[str, str] = {}
            skipped: list[tuple[str, str]] = []
            for file_path, hunks in file_hunks.items():
                original = self.git_provider.get_pr_file_content(file_path, source_branch)
                if original == "":
                    skipped.append((file_path, "源分支中文件不存在或为空"))
                    continue
                new_content = self._apply_hunks(original, hunks)
                if new_content is None:
                    skipped.append((file_path, "diff 与源文件不匹配（可能已被新提交修改）"))
                    continue
                if new_content != original:
                    changes[file_path] = new_content

            fixed: list[str] = []
            commit_result = FixFormatResult()
            if changes and self.enable_auto_push:
                try:
                    from pr_agent.distributed.runtime import get_execution_runtime

                    runtime = get_execution_runtime()
                    if runtime is not None:
                        runtime.raise_if_canceled()
                    commit_result = self._commit_changes(project, source_branch, changes)
                    fixed = list(changes.keys())
                except Exception as e:
                    from pr_agent.distributed.runtime import TaskCanceled

                    if isinstance(e, TaskCanceled):
                        raise
                    get_logger().exception(f"fix_format: 提交修复失败: {e}")
                    for fp in changes:
                        skipped.append((fp, f"提交失败: {e}"))
            elif changes:
                # 仅计算不推送（enable_auto_push=false）
                fixed = list(changes.keys())

            status_markdown = self._build_comment(fixed, skipped)
            if publish_result:
                self._safe_publish(status_markdown)
            get_logger().info(f"fix_format 完成: 修复 {len(fixed)} 个文件, 跳过 {len(skipped)} 个")
            return FixFormatResult(
                pushed_sha=commit_result.pushed_sha,
                pushed_parent_sha=commit_result.pushed_parent_sha,
                pushed_tree_sha=commit_result.pushed_tree_sha,
                fixed_files=tuple(fixed),
                skipped_files=tuple(skipped),
                status_markdown=status_markdown,
                report_fingerprint=report_fingerprint,
                exact_report_applied=bool(fixed) and not skipped,
            )
        except Exception as e:
            from pr_agent.distributed.runtime import TaskCanceled

            if isinstance(e, TaskCanceled):
                raise
            get_logger().exception(f"fix_format 失败: {e}")
            return FixFormatResult(status_markdown=f"格式自动修复执行异常：{e}")

    # ------------------------------------------------------------------ #
    # 流水线与 job 定位
    # ------------------------------------------------------------------ #
    def _resolve_pipeline(self, project, source_branch: str):
        """优先按配置的 pipeline_id 获取，兜底取源分支最新流水线。"""
        if self.pipeline_id:
            try:
                return project.pipelines.get(int(self.pipeline_id))
            except Exception as e:
                get_logger().warning(f"fix_format: 按 pipeline_id={self.pipeline_id} 获取流水线失败: {e}")
        try:
            pls = project.pipelines.list(ref=source_branch, order_by="id", sort="desc", per_page=1)
            if pls:
                return project.pipelines.get(pls[0].id)
        except Exception as e:
            get_logger().warning(f"fix_format: 按分支 {source_branch} 获取流水线失败: {e}")
        return None

    def _collect_all_pipelines(self, project, pipeline, depth: int = 0, visited: Optional[set] = None) -> list:
        """递归展开父子（parent-child）流水线，真正的 job 可能在 downstream pipeline 里。"""
        if visited is None:
            visited = set()
        if pipeline.id in visited or depth > 3:
            return []
        visited.add(pipeline.id)
        result = [pipeline]
        try:
            bridges = pipeline.bridges.list(get_all=True, per_page=100)
        except Exception:
            return result
        for b in bridges:
            ds = getattr(b, "downstream_pipeline", None)
            if not ds:
                continue
            ds_id = ds.get("id") if isinstance(ds, dict) else getattr(ds, "id", None)
            if not ds_id or ds_id in visited:
                continue
            try:
                ds_pipeline = project.pipelines.get(ds_id)
                result.extend(self._collect_all_pipelines(project, ds_pipeline, depth + 1, visited))
            except Exception as e:
                get_logger().warning(f"fix_format: 获取 downstream pipeline #{ds_id} 失败: {e}")
        return result

    def _find_failed_format_jobs(self, pipelines: list) -> list:
        """在所有相关流水线里找出失败的 format 检查 job。"""
        result = []
        for p in pipelines:
            try:
                jobs = p.jobs.list(per_page=100, get_all=True)
            except Exception as e:
                get_logger().warning(f"fix_format: 获取 pipeline #{p.id} 的 job 失败: {e}")
                continue
            for job in jobs:
                name_lower = str(job.name).lower()
                if any(kw in name_lower for kw in self.job_name_keywords) and job.status == "failed":
                    result.append(job)
        return result

    def _get_report_text(self, project, job, *, trace: Optional[str] = None) -> str:
        """获取格式报告文本：优先取 job 日志“详情见报告:”行的链接直接下载，回退通用 URL 正则 / artifact API。"""
        report_url = ""
        artifact_job_id = job.id
        artifact_path = self.report_artifact_fallback
        if trace is None:
            trace = self._get_job_trace(project, job)
        try:
            # 1) 优先：搜“详情见报告:”行，取后面的链接
            m_line = _REPORT_LINE_RE.search(trace)
            if m_line:
                report_url = m_line.group(1).strip().rstrip(".,;)】）")
                get_logger().info(f"fix_format: 从“详情见报告”行解析到报告链接: {report_url}")
            # 2) 回退：从任意 jobs/artifacts/raw 链接提取 (job_id, path)
            m_url = _REPORT_URL_RE.search(trace)
            if m_url:
                if not report_url:
                    report_url = m_url.group(0)
                    get_logger().info(f"fix_format: 从通用 URL 正则解析到报告链接: {report_url}")
                artifact_job_id = int(m_url.group(1))
                artifact_path = m_url.group(2)
            if not report_url:
                get_logger().warning(f"fix_format: job {job.id} 日志中未找到报告链接")
        except Exception as e:
            get_logger().warning(f"fix_format: 解析 job {job.id} 日志失败: {e}")

        # 优先用报告链接直接带鉴权 HTTP 下载
        if report_url:
            try:
                text = self._download_url(report_url)
                if self._looks_like_html(text):
                    get_logger().warning(
                        f"fix_format: 报告链接返回的是 HTML（可能是登录页/鉴权失败），回退 artifact API: {report_url}"
                    )
                elif text.strip():
                    return text
                else:
                    get_logger().warning(f"fix_format: 报告链接下载内容为空: {report_url}")
            except Exception as e:
                get_logger().warning(f"fix_format: 直接下载报告链接失败 ({report_url}): {e}")

        # 回退：python-gitlab artifact API
        try:
            art_job = project.jobs.get(artifact_job_id)
            raw = art_job.artifact(artifact_path)
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw)
        except Exception as e:
            get_logger().warning(
                f"fix_format: 下载报告 artifact 失败 (job={artifact_job_id}, path={artifact_path}): {e}"
            )
            return ""

    @staticmethod
    def _job_url(job) -> str:
        return str(
            getattr(job, "web_url", "")
            or (getattr(job, "attributes", {}) or {}).get("web_url")
            or ""
        )

    @staticmethod
    def _get_job_trace(project, job) -> str:
        try:
            trace = project.jobs.get(job.id).trace()
            if isinstance(trace, bytes):
                return trace.decode("utf-8", errors="replace")
            return str(trace or "")
        except Exception as error:
            get_logger().warning(f"fix_format: 读取 job {job.id} 日志失败: {error}")
            return ""

    def _download_url(self, url: str) -> str:
        """带鉴权直接 HTTP GET 下载报告链接内容（复用 gl.session，尊重 GITLAB.SSL_VERIFY）。"""
        token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", None)
        auth_method = get_settings().get("GITLAB.AUTH_TYPE", "oauth_token")
        ssl_verify = get_settings().get("GITLAB.SSL_VERIFY", True)
        headers = {}
        if token:
            if auth_method == "oauth_token":
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers["PRIVATE-TOKEN"] = token
        resp = self.git_provider.gl.session.get(url, headers=headers, verify=ssl_verify, timeout=30)
        resp.raise_for_status()
        if not resp.encoding:
            resp.encoding = "utf-8"
        return resp.text

    @staticmethod
    def _looks_like_html(text: str) -> bool:
        """判断下载内容是否是 HTML 页面（鉴权失败时 GitLab 会返回 200 + 登录页）。"""
        head = (text or "").lstrip()[:200].lower()
        return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head

    # ------------------------------------------------------------------ #
    # unified diff 解析与应用
    # ------------------------------------------------------------------ #
    def _parse_unified_diff(self, report_text: str) -> dict:
        """解析（可能多文件的）unified diff，返回 {file_path: [hunk, ...]}。

        hunk = {"old_start": int, "lines": [(tag, text), ...]}，tag ∈ {' ', '-', '+'}。
        用 hunk 头声明的行数精确界定 hunk 边界，兼容 clang-format / black / git diff 输出。
        """
        files: dict[str, list] = {}
        current_hunks: Optional[list] = None
        lines = report_text.splitlines()
        i, n = 0, len(lines)

        while i < n:
            line = lines[i]

            # 文件头：--- <old> 紧跟 +++ <new>
            # 报告"问题明细"里 diff 的 --- 行可能带列表前缀（如 "- --- a/src/x.cpp"），需去掉再匹配
            header = line[2:] if line.startswith("- --- ") else line
            if header.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
                path = self._extract_path(lines[i + 1][4:])
                if path and path != "/dev/null":
                    current_hunks = files.setdefault(path, [])
                else:
                    current_hunks = None
                i += 2
                continue

            m = _HUNK_HEADER_RE.match(line)
            if m and current_hunks is not None:
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                hunk = {
                    "old_start": old_start,
                    "lines": [],
                    "old_no_newline": False,
                    "new_no_newline": False,
                }
                i += 1
                old_seen, new_seen = 0, 0
                last_tag = ""
                while i < n:
                    hl = lines[i]
                    if hl.startswith("\\"):  # "\ No newline at end of file"
                        if last_tag in {"-", " "}:
                            hunk["old_no_newline"] = True
                        if last_tag in {"+", " "}:
                            hunk["new_no_newline"] = True
                        i += 1
                        continue
                    if old_seen >= old_count and new_seen >= new_count:
                        break
                    tag = hl[0] if hl else " "
                    if tag == " ":
                        hunk["lines"].append((" ", hl[1:] if hl else ""))
                        old_seen += 1
                        new_seen += 1
                    elif tag == "-":
                        hunk["lines"].append(("-", hl[1:]))
                        old_seen += 1
                    elif tag == "+":
                        hunk["lines"].append(("+", hl[1:]))
                        new_seen += 1
                    else:
                        break
                    last_tag = tag
                    i += 1
                if hunk["lines"]:
                    current_hunks.append(hunk)
                continue

            i += 1

        # 去掉没有任何 hunk 的空文件条目
        return {k: v for k, v in files.items() if v}

    # GitLab CI 检出目录（CI_PROJECT_DIR=/builds/<group>/<project>），diff 报告里
    # 可能带这个绝对前缀，需归一化为仓库相对路径才能在源分支中找到文件。
    _CI_PROJECT_DIR_RE = re.compile(r"^(?:/?builds/[^/]+/[^/]+|/builds/[^/]+/[^/]+)/(.*)$")

    @staticmethod
    def _extract_path(raw: str) -> str:
        """从 diff 文件头行提取干净的文件路径（去掉 a/ b/ 前缀、tab/括号注释、时间戳、CI 绝对路径前缀）。"""
        raw = raw.split("\t")[0]
        raw = re.sub(r"\s+\([^)]*\)\s*$", "", raw)  # 去掉尾部 " (reformatted)" 等注释
        raw = raw.strip()
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        m = PRFixFormat._CI_PROJECT_DIR_RE.match(raw)
        if m and m.group(1):
            raw = m.group(1)
        return raw

    def _apply_hunks(self, original: str, hunks: list) -> Optional[str]:
        """把 hunks 应用到 original 文本，返回新内容；若 diff 与原文不匹配则返回 None。"""
        original_lines = original.splitlines()
        result: list[str] = []
        cursor = 0  # 0-based，指向下一个待处理的原文行

        for hunk in hunks:
            target_idx = hunk["old_start"] - 1
            if target_idx < cursor or target_idx > len(original_lines):
                get_logger().warning(f"fix_format: hunk 起点 {hunk['old_start']} 越界/乱序，放弃该文件")
                return None
            result.extend(original_lines[cursor:target_idx])
            cursor = target_idx
            for tag, text in hunk["lines"]:
                if tag == " ":
                    if cursor < len(original_lines):
                        result.append(original_lines[cursor])
                    else:
                        result.append(text)
                    cursor += 1
                elif tag == "-":
                    if cursor >= len(original_lines) or original_lines[cursor].rstrip() != text.rstrip():
                        get_logger().warning("fix_format: 删除行与源文件不一致，放弃该文件")
                        return None
                    cursor += 1
                elif tag == "+":
                    result.append(text)

        result.extend(original_lines[cursor:])
        new_content = "\n".join(result)
        old_no_newline = any(bool(hunk.get("old_no_newline")) for hunk in hunks)
        new_no_newline = any(bool(hunk.get("new_no_newline")) for hunk in hunks)
        if new_no_newline:
            return new_content
        if original.endswith("\n") or old_no_newline:
            new_content += "\n"
        return new_content

    # ------------------------------------------------------------------ #
    # 提交与评论
    # ------------------------------------------------------------------ #
    def _commit_changes(self, project, source_branch: str, changes: dict) -> FixFormatResult:
        """用 GitLab Commits API 一次性提交所有文件（单 commit，只触发一次流水线）。"""
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        branch = project.branches.get(source_branch)
        base_sha = str((getattr(branch, "commit", {}) or {}).get("id") or "")
        if len(base_sha) != 40:
            raise RuntimeError("无法确认格式修复前的源分支 SHA")
        parent_tree_sha = self._commit_tree_sha(project, base_sha)
        digest_input = json.dumps(
            {"branch": source_branch, "base_sha": base_sha, "changes": changes},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:20]
        marker = f"[pr-agent-task:{runtime.task_id}:format:{digest}]" if runtime is not None else ""
        actions = [
            {
                "action": "update",
                "file_path": file_path,
                "content": content,
                "last_commit_id": base_sha,
            }
            for file_path, content in changes.items()
        ]
        commit_message = f"{self.commit_message} {FORMAT_FIX_MARKER}{f' {marker}' if marker else ''}"
        payload = {"branch": source_branch, "commit_message": commit_message, "actions": actions}
        effect = None
        effect_name = f"format-commit:{base_sha}:{digest}"
        if runtime is not None and runtime.mode == "queue":
            from pr_agent.distributed.effects import SyncEffectGuard

            effect = SyncEffectGuard(runtime)
            claim = effect.claim(effect_name, {
                "base_sha": base_sha,
                "base_tree_sha": parent_tree_sha,
                "source_branch": source_branch,
                "files": sorted(changes),
                "task_marker": marker,
            })
            if claim.status == "completed" and isinstance(claim.result, dict):
                return FixFormatResult(
                    pushed_sha=str(claim.result.get("pushed_sha") or ""),
                    pushed_parent_sha=str(claim.result.get("pushed_parent_sha") or ""),
                    pushed_tree_sha=str(claim.result.get("pushed_tree_sha") or ""),
                )
            runtime.assert_fence_sync()
        try:
            commit = project.commits.create(payload)
            pushed_sha = str(getattr(commit, "id", "") or getattr(commit, "sha", ""))
        except Exception:
            pushed_sha = self._reconcile_format_commit(project, source_branch, base_sha, marker, set(changes))
            if not pushed_sha:
                raise
        parent_sha, tree_sha, message = self._commit_facts(project, pushed_sha)
        if parent_sha != base_sha or marker and marker not in message:
            raise RuntimeError("格式修复提交身份校验失败")
        result = FixFormatResult(
            pushed_sha=pushed_sha,
            pushed_parent_sha=parent_sha,
            pushed_tree_sha=tree_sha,
        )
        if runtime is not None and runtime.mode == "queue":
            from pr_agent.triage.repair_rollback import RepairCommitEntry

            sequence = runtime.next_repair_commit_sequence_sync()
            pushed_at = datetime.now(timezone.utc).isoformat()
            effect.record_metadata(effect_name, {
                "base_sha": base_sha,
                "base_tree_sha": parent_tree_sha,
                "source_branch": source_branch,
                "files": sorted(changes),
                "task_marker": marker,
                "commit_sha": pushed_sha,
                "parent_sha": parent_sha,
                "tree_sha": tree_sha,
                "attempt_sequence": sequence,
                "pushed_at": pushed_at,
            })
            entry = RepairCommitEntry(
                sequence=sequence,
                commit_sha=pushed_sha,
                parent_sha=parent_sha,
                tree_sha=tree_sha,
                effect_id=effect_name,
                task_marker=marker,
                pushed_at=pushed_at,
            )
            authoritative_parent_tree_sha = self._commit_tree_sha(project, parent_sha)
            branch_head_sha = str(
                (getattr(project.branches.get(source_branch), "commit", {}) or {}).get("id") or ""
            )
            try:
                runtime.record_reconciled_repair_commit_sync(
                    entry,
                    expected_parent_sha=base_sha,
                    authoritative_parent_tree_sha=authoritative_parent_tree_sha,
                    branch_head_sha=branch_head_sha,
                    source_branch=source_branch,
                )
            except Exception:
                reconciled_sha = self._reconcile_format_commit(
                    project,
                    source_branch,
                    base_sha,
                    marker,
                    set(changes),
                )
                if reconciled_sha != pushed_sha:
                    raise
                get_logger().warning(
                    "fix_format: 远端提交已确认，首次账本登记失败，使用同一提交身份重试"
                )
                runtime.record_reconciled_repair_commit_sync(
                    entry,
                    expected_parent_sha=base_sha,
                    authoritative_parent_tree_sha=authoritative_parent_tree_sha,
                    branch_head_sha=branch_head_sha,
                    source_branch=source_branch,
                )
            effect.complete(effect_name, {
                "pushed_sha": pushed_sha,
                "pushed_parent_sha": parent_sha,
                "pushed_tree_sha": tree_sha,
            })
        get_logger().info(f"fix_format: 已提交 {len(actions)} 个文件到 {source_branch}")
        return result

    @classmethod
    def _commit_tree_sha(cls, project, commit_sha: str) -> str:
        commit = project.commits.get(commit_sha)
        tree_sha = str(
            getattr(commit, "tree_id", "")
            or (getattr(commit, "attributes", {}) or {}).get("tree_id")
            or ""
        )
        if len(tree_sha) == 40:
            return tree_sha
        entries = project.repository_tree(ref=commit_sha, path="", get_all=True)
        body = bytearray()
        for entry in sorted(entries, key=cls._git_tree_sort_key):
            mode = str(entry["mode"])
            if str(entry.get("type") or "") == "tree":
                mode = mode.lstrip("0") or "0"
            body.extend(f"{mode} {entry['name']}\0".encode("utf-8"))
            body.extend(bytes.fromhex(str(entry["id"])))
        header = f"tree {len(body)}\0".encode("ascii")
        return hashlib.sha1(header + body).hexdigest()

    @staticmethod
    def _git_tree_sort_key(entry: dict) -> bytes:
        """Return Git's base-name ordering key; directories compare with a trailing slash."""
        name = str(entry["name"]).encode("utf-8")
        return name + (b"/" if str(entry.get("type") or "") == "tree" else b"\0")

    @classmethod
    def _commit_facts(cls, project, commit_sha: str) -> tuple[str, str, str]:
        commit = project.commits.get(commit_sha)
        parents = list(
            getattr(commit, "parent_ids", None)
            or (getattr(commit, "attributes", {}) or {}).get("parent_ids")
            or ()
        )
        if len(parents) != 1:
            raise RuntimeError("格式修复提交不是单父提交")
        message = str(getattr(commit, "message", "") or (getattr(commit, "attributes", {}) or {}).get("message") or "")
        return str(parents[0]), cls._commit_tree_sha(project, commit_sha), message

    @staticmethod
    def _reconcile_format_commit(project, source_branch: str, base_sha: str, marker: str, files: set[str]) -> str:
        branch = project.branches.get(source_branch)
        head_sha = str((getattr(branch, "commit", {}) or {}).get("id") or "")
        if not head_sha or head_sha == base_sha:
            return ""
        commit = project.commits.get(head_sha)
        parents = list(getattr(commit, "parent_ids", None) or ())
        message = str(getattr(commit, "message", "") or "")
        changed = {str(item.get("new_path") or item.get("old_path") or "") for item in commit.diff()}
        return head_sha if parents == [base_sha] and marker in message and changed == files else ""

    def _job_failure_hint(self, project, job, *, trace: Optional[str] = None) -> str:
        """从 format job 日志中提取第一条真实失败原因行（如 git bad object）。"""
        if trace is None:
            trace = self._get_job_trace(project, job)
        for line in (trace or "").splitlines():
            stripped = line.strip()
            if stripped and re.search(r"fatal:|error[:\s]|failed", stripped, re.IGNORECASE):
                return stripped[:200]
        return ""

    @staticmethod
    def _build_disposition_comment(disposition: FormatJobDisposition) -> str:
        job_link = f"[查看 Job 日志]({disposition.job_url})" if disposition.job_url else ""
        suffix = f" {job_link}" if job_link else ""
        return (
            "## 🎨 代码格式自动修复\n\n"
            f"Format Job 自身执行失败：{disposition.summary}\n"
            f"{disposition.suggested_action}{suffix}"
        )

    def _build_comment(self, fixed: list, skipped: list, report_missing: bool = False, detail: str = "") -> str:
        lines = ["## 🎨 代码格式自动修复"]
        if report_missing:
            lines.append("")
            if detail:
                lines.append(
                    f"检测到 `code_format_check` 失败，但格式检查 job 自身执行失败、未生成报告（根因: {detail}），"
                    "未做改动。这不是代码格式问题，请先解决 job 执行错误。"
                )
            else:
                lines.append("检测到 `code_format_check` 失败，但未能获取或解析格式报告，未做改动，请手动检查。")
            return "\n".join(lines)

        if fixed:
            action = "已修复并推送到源分支" if self.enable_auto_push else "已计算出修复（未推送）"
            lines.append("")
            lines.append(f"根据流水线 `code_format_check` 报告，{action}以下文件：")
            lines.append("")
            lines.extend(f"- `{fp}`" for fp in fixed)
        else:
            lines.append("")
            lines.append("未产生可自动修复的改动。")

        if skipped:
            lines.append("")
            lines.append("以下文件未能自动修复，请手动处理：")
            lines.append("")
            lines.extend(f"- `{fp}`：{reason}" for fp, reason in skipped)

        if fixed and self.enable_auto_push:
            lines.append("")
            lines.append(f"> 提交信息含 `{FORMAT_FIX_MARKER}` 标记，用于标识自动格式修复提交。")
        return "\n".join(lines)

    def _safe_publish(self, body: str) -> None:
        if not body:
            return
        try:
            if get_settings().config.publish_output:
                self.git_provider.publish_comment(body)
        except Exception as e:
            get_logger().warning(f"fix_format: 发布评论失败: {e}")
