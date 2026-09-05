"""Tier-2 heavy repair channel for /improve suggestions (Pipeline v2).

The last-resort tier: for RepairTasks that Tier-0 (deterministic_fix.py) and
Tier-1 (tier1_repair.py) could not resolve, clone the MR's source branch and
hand the FULL repository to a local Copilot CLI session (subprocess), which
can see and edit files the diff-scoped earlier tiers cannot reach (e.g. a
companion header outside this PR's diff). Never pushes or commits; reads the
resulting uncommitted `git diff` and converts it back into per-file
suggestions, split by whether the touched file is in the original PR diff
(one-click appliable) or not (copy-patch comment -- GitLab's inline-suggestion
API rejects files outside the diff; see gitlab_provider.py's
`publish_inline_suggestions` `file_not_in_diff` check).

Runs asynchronously (fire-and-forget from the caller, see a later task) and
never raises out of its public entry points.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

# config_loader must be imported before pr_agent.log -- see the identical
# comment in deterministic_fix.py for why.
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def _cfg(key: str, default=None):
    return get_settings().get(f"pr_code_suggestions.{key}", default)


# --------------------------------------------------------------------------- #
# prompt builder
# --------------------------------------------------------------------------- #
def build_heavy_repair_prompt(tasks: List[dict], task_ids: List[str]) -> str:
    """Build one combined Copilot CLI prompt covering every still-unresolved
    RepairTask in this batch. Pure function."""
    blocks = []
    for task_id, task in zip(task_ids, tasks):
        members_text = "\n".join(
            f"  - {m.get('suggestion_content', '')} "
            f"(file: {m.get('relevant_file', task.get('relevant_file', ''))}, "
            f"around lines {m.get('relevant_lines_start')}-{m.get('relevant_lines_end')})"
            for m in task.get("members", [])
        )
        companion_note = ""
        if task.get("companion_head_file") and task.get("members"):
            companion_note = f"\n  Known companion file: {task['members'][0].get('companion_file', '')}"
        blocks.append(
            f"Task {task_id} (file: {task.get('relevant_file', '')}, "
            f"issue type: {task.get('structural_issue', '')}):\n"
            f"{members_text}{companion_note}\n"
            f"  Why earlier automated repair could not fix it: {task.get('fix_note', '')}"
        )
    tasks_text = "\n\n".join(blocks)

    return (
        "You are repairing a set of code review suggestions directly in this repository checkout.\n\n"
        "Each task below describes a code issue that automated tooling could not repair on its own "
        "(it needed full-repository context: a companion file outside the diff, a truly cross-cutting "
        "change, etc.). Make the SMALLEST possible edit(s) in the checkout that resolve each task. Do "
        "not run git commit or git push. Do not modify build/CI configuration files unless a task "
        "explicitly asks for it.\n\n"
        f"{tasks_text}\n\n"
        "When you are done, write a file named `manifest.json` at the root of this checkout with this "
        "exact shape:\n"
        '{\n  "<task_id>": {"status": "done", "files": ["path/to/file"], "note": "one short sentence"},\n'
        "  ...\n}\n"
        "Every task id above MUST appear in manifest.json. Use \"status\": \"failed\" (with a \"note\" "
        "explaining why) for any task you could not resolve -- never omit a task silently."
    )


# --------------------------------------------------------------------------- #
# manifest reading
# --------------------------------------------------------------------------- #
def read_manifest(repo_dir: str) -> Dict[str, dict]:
    """Read manifest.json from the Tier-2 workdir. Returns {} when missing or
    unparsable (never raises); the caller treats every task as failed."""
    path = os.path.join(repo_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        get_logger().warning(f"Tier-2 manifest.json not found at {path}")
        return {}
    except Exception as e:
        get_logger().warning(f"Tier-2 manifest.json unreadable: {e}")
        return {}


# --------------------------------------------------------------------------- #
# unified diff parsing
# --------------------------------------------------------------------------- #
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def line_range_in_diff_hunks(patch: str, line_start: int, line_end: int) -> bool:
    """Return True if [line_start, line_end] (1-based, inclusive, in the PR's
    head-file line numbering) overlaps any hunk's new-side ("+") range in
    `patch` -- i.e. whether GitLab would accept an inline suggestion
    anchored there, since its API only allows positions inside an actual
    diff hunk. A file being present in the PR's diff is NOT sufficient on
    its own: a specific line/range can still fall outside every hunk in that
    file's own patch (e.g. Tier-2 repaired a part of the file this PR never
    touched), which is exactly the scenario this function exists to catch
    before attempting a one-click inline suggestion that GitLab would
    otherwise reject with a 500/400 error.
    """
    for line in (patch or "").splitlines():
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            continue
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        new_end = new_start + max(new_count, 1) - 1 if new_count > 0 else new_start
        if line_start <= new_end and line_end >= new_start:
            return True
    return False


def parse_unified_diff(diff_text: str) -> Dict[str, List[dict]]:
    """Parse `git diff` (unstaged, unified format) output into
    {file_path: [hunk, ...]}, where each hunk is:
        {"old_start": int, "old_end": int,
         "existing_code": str,   # old-side content (context + removed lines)
         "improved_code": str}   # new-side content (context + added lines)

    `old_start`/`old_end` are 1-based inclusive line numbers in the ORIGINAL
    (pre-Tier-2) file -- i.e. exactly what `relevant_lines_start/end` means
    everywhere else in this codebase, since Tier-2 clones the current PR head
    and edits on top of it: the diff's "-" side IS the PR's current head_file.
    """
    files: Dict[str, List[dict]] = {}
    current_file = None
    current_hunk = None

    def flush_hunk():
        nonlocal current_hunk
        if current_hunk is not None and current_file is not None:
            files.setdefault(current_file, []).append({
                "old_start": current_hunk["old_start"],
                "old_end": current_hunk["old_start"] + max(current_hunk["old_count"], 1) - 1
                if current_hunk["old_count"] > 0 else current_hunk["old_start"],
                "existing_code": "\n".join(current_hunk["old_lines"]),
                "improved_code": "\n".join(current_hunk["new_lines"]),
            })
        current_hunk = None

    for line in diff_text.splitlines():
        m_file = _DIFF_GIT_RE.match(line)
        if m_file:
            flush_hunk()
            current_file = m_file.group(2)
            continue
        m_hunk = _HUNK_HEADER_RE.match(line)
        if m_hunk:
            flush_hunk()
            old_start = int(m_hunk.group(1))
            old_count = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
            current_hunk = {"old_start": old_start, "old_count": old_count, "old_lines": [], "new_lines": []}
            continue
        if current_hunk is None:
            continue  # file metadata lines (index/---/+++) before the first hunk
        if line.startswith("-"):
            current_hunk["old_lines"].append(line[1:])
        elif line.startswith("+"):
            current_hunk["new_lines"].append(line[1:])
        elif line.startswith(" "):
            current_hunk["old_lines"].append(line[1:])
            current_hunk["new_lines"].append(line[1:])
        # a line like '\ No newline at end of file' is ignored
    flush_hunk()
    return files


def read_working_tree_diff(repo_dir: str) -> str:
    """Return the (uncommitted) `git diff` output for repo_dir. Never raises."""
    try:
        result = subprocess.run(["git", "diff"], cwd=repo_dir, capture_output=True, text=True, timeout=30)
        return result.stdout or ""
    except Exception as e:
        get_logger().warning(f"Tier-2 git diff read failed: {e}")
        return ""

# --------------------------------------------------------------------------- #
# repo clone (deliberately duplicated from ut_agent/tools/clone_branch.py's
# clone_source_branch rather than imported: that module's PACKAGE (ut_agent/
# __init__.py -> ut_agent/agent.py) pulls in langgraph/langchain_core at
# import time for its LangGraph state machine, which is an unrelated, heavy,
# optional dependency this core /improve pipeline should never be coupled
# to -- a missing/incompatible langgraph install must never be able to break
# code suggestions. The clone logic itself has no such dependency; only the
# sibling @tool-decorated wrapper in that file does.)
# --------------------------------------------------------------------------- #
def _clone_source_branch(git_provider, output_dir: str, mr_id, source_branch: str) -> str:
    """Shallow-clone the MR source branch. Returns the clone directory path on
    success, or a string starting with "ERROR:" on failure. Never raises."""
    repo_dir = os.path.join(output_dir, f"mr_{mr_id}", "repo")
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        return repo_dir
    os.makedirs(repo_dir, exist_ok=True)

    try:
        repo_url = git_provider.get_git_repo_url(git_provider.pr_url)
    except Exception as e:
        return f"ERROR: get_git_repo_url raised: {e}"
    if not repo_url:
        return "ERROR: could not resolve repository URL"

    try:
        clone_url = git_provider._prepare_clone_url_with_token(repo_url)
    except Exception as e:
        return f"ERROR: _prepare_clone_url_with_token raised: {e}"
    if not clone_url:
        return f"ERROR: could not build an authenticated clone URL (repo: {repo_url})"

    clone_depth = int(_cfg("tier2_clone_depth", 1))
    cmd = [
        "git", "clone", "--branch", source_branch, "--depth", str(clone_depth),
        "--single-branch", "--recurse-submodules", "--shallow-submodules",
        clone_url, repo_dir,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return f"ERROR: git clone failed (exit={result.returncode}): {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "ERROR: git clone timed out (300s)"
    except Exception as e:
        return f"ERROR: git clone raised: {e}"

    if os.path.isfile(os.path.join(repo_dir, ".gitmodules")):
        try:
            subprocess.run(
                ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
                cwd=repo_dir, capture_output=True, text=True, timeout=300,
            )
        except Exception:
            pass  # non-fatal: submodules may already be present from clone

    return repo_dir


# --------------------------------------------------------------------------- #
# Copilot CLI subprocess runner (injectable for testing)
# --------------------------------------------------------------------------- #
# (cmd, cwd, timeout_seconds) -> (returncode, stdout, stderr); -1 returncode means timeout
CopilotRunner = Callable[[List[str], str, int], Tuple[int, str, str]]


def _default_copilot_runner(cmd: List[str], cwd: str, timeout_seconds: int) -> Tuple[int, str, str]:
    """Real subprocess invocation, following ut_agent's proven pattern: Popen +
    a readline polling loop with a wall-clock timeout, rather than
    subprocess.run's blocking wait (which cannot be interrupted early)."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_lines: List[str] = []
    start = time.time()
    while True:
        if time.time() - start > timeout_seconds:
            proc.kill()
            return -1, "\n".join(stdout_lines), "timeout"
        line = proc.stdout.readline()
        if line:
            stdout_lines.append(line.rstrip())
        elif proc.poll() is not None:
            remaining = proc.stdout.read()
            if remaining:
                stdout_lines.extend(remaining.rstrip().split("\n"))
            break
    stderr = proc.stderr.read() or ""
    return proc.returncode, "\n".join(stdout_lines), stderr


def run_copilot_cli(repo_dir: str, prompt: str, timeout_seconds: int,
                     runner: Optional[CopilotRunner] = None) -> Tuple[bool, str]:
    """Invoke the Copilot CLI in repo_dir with the given prompt. Returns
    (succeeded, message). `runner` is injectable for tests (defaults to a
    real subprocess.Popen-based implementation). Never raises."""
    runner = runner or _default_copilot_runner
    cmd = [
        "copilot", "-p", prompt, "--allow-all-tools",
        "--deny-tool=shell(git push)", "--deny-tool=shell(git commit)", "--deny-tool=shell(rm)",
    ]
    try:
        returncode, stdout, stderr = runner(cmd, repo_dir, timeout_seconds)
    except Exception as e:
        get_logger().warning(f"Tier-2 Copilot CLI invocation raised: {e}")
        return False, f"invocation error: {e}"
    if returncode == -1:
        return False, "Copilot CLI timed out"
    if returncode != 0:
        get_logger().warning(f"Tier-2 Copilot CLI exited {returncode}: {(stderr or '')[-500:]}")
        return False, f"Copilot CLI exited {returncode}"
    return True, "ok"

# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def classify_heavy_repair_results(
    manifest: Dict[str, dict],
    file_hunks: Dict[str, List[dict]],
    diff_file_patches: Dict[str, str],
    task_by_id: Dict[str, dict],
) -> Dict[str, list]:
    """Turn Tier-2's raw output (manifest + parsed diff hunks) into renderable
    results, split by delivery method.

    `diff_file_patches` maps this PR's own diff filenames to their raw patch
    text (from FilePatchInfo.patch), used to check not just whether a file is
    in the PR's diff but whether the SPECIFIC line range Tier-2 repaired
    falls inside one of that file's own diff hunks -- see
    line_range_in_diff_hunks. A file being in the diff is not sufficient on
    its own: Tier-2 can (and does) repair an untouched part of an otherwise-
    changed file, which GitLab's inline-suggestion API rejects (500/
    no_id_returned) because the position doesn't correspond to any hunk.

    Returns:
        {
          "one_click": [suggestion_dict, ...],       # file+line range WAS in original diff
          "copy_patch": [copy_patch_dict, ...],       # file not in diff, or line range outside its hunks
          "failed": [(task_id, reason), ...],         # manifest said failed / no diff produced
        }
    """
    one_click: List[dict] = []
    copy_patch: List[dict] = []
    failed: List[tuple] = []

    for task_id, entry in manifest.items():
        task = task_by_id.get(task_id)
        base_relevant_file = task.get("relevant_file") if task else None
        # RepairTask dicts (see deterministic_fix.py's _new_task) carry no
        # label/score/summary of their own -- those live on the ORIGINAL
        # suggestion(s) that produced this task, in task["members"]. Reading
        # task.get("label", ...) directly always misses and silently falls
        # back to the placeholder default; read from members[0] instead so
        # Tier-2-resolved suggestions keep their real label/score/summary.
        primary_member = (task.get("members") or [{}])[0] if task else {}
        status = str(entry.get("status", "failed")).lower()
        note = str(entry.get("note", "") or "")
        files = entry.get("files") or ([base_relevant_file] if base_relevant_file else [])

        if status != "done":
            failed.append((task_id, note or f"Tier-2 reported status={status}"))
            continue

        produced_any = False
        for file_path in files:
            hunks = file_hunks.get(file_path)
            if not hunks:
                continue  # manifest claimed this file but no actual diff hunk was found
            patch = diff_file_patches.get(file_path)
            for hunk in hunks:
                produced_any = True
                in_diff = bool(patch) and line_range_in_diff_hunks(patch, hunk["old_start"], hunk["old_end"])
                if in_diff:
                    one_click.append({
                        "relevant_file": file_path,
                        "existing_code": hunk["existing_code"],
                        "improved_code": hunk["improved_code"],
                        "relevant_lines_start": hunk["old_start"],
                        "relevant_lines_end": hunk["old_end"],
                        "one_sentence_summary": primary_member.get("one_sentence_summary", ""),
                        # Prefer the ORIGINAL suggestion_content (it carries the
                        # "Severity: High" line _extract_impact_level/_impact_label
                        # parse to render the table/inline header's impact level).
                        # Tier-2's manifest `note` is just a short one-line recap
                        # from the Copilot CLI session and never has that marker --
                        # using it as the primary source silently produced
                        # "Unspecified" impact on every Tier-2-resolved suggestion.
                        # Fall back to `note` only when the original has nothing.
                        "suggestion_content": primary_member.get("suggestion_content", "") or note,
                        "label": primary_member.get("label", "possible issue") or "possible issue",
                        "score": primary_member.get("score", 7),
                        # Carried through in addition to the embedded "Severity:"
                        # line above: _impact_label/_extract_impact_level check
                        # this direct field FIRST, before falling back to parsing
                        # suggestion_content, so this is belt-and-suspenders.
                        "severity": primary_member.get("severity", ""),
                        "resolved_by_stage": "tier2_heavy",
                        "source_task_id": task_id,
                    })
                else:
                    copy_patch.append({
                        "relevant_file": file_path,
                        "existing_code": hunk["existing_code"],
                        "improved_code": hunk["improved_code"],
                        "relevant_lines_start": hunk["old_start"],
                        "relevant_lines_end": hunk["old_end"],
                        "note": note,
                        # Same rendering fields as the one_click branch above --
                        # copy_patch results now feed the /improve summary
                        # table too (as a "not one-click appliable" row rather
                        # than a standalone comment), and
                        # generate_summarized_suggestions silently drops any
                        # suggestion missing label/one_sentence_summary/
                        # suggestion_content, so these must be populated the
                        # same way one_click's are.
                        "one_sentence_summary": primary_member.get("one_sentence_summary", ""),
                        "suggestion_content": primary_member.get("suggestion_content", "") or note,
                        "label": primary_member.get("label", "possible issue") or "possible issue",
                        "score": primary_member.get("score", 7),
                        "severity": primary_member.get("severity", ""),
                        "resolved_by_stage": "tier2_copy_patch",
                        "source_task_id": task_id,
                    })
        if not produced_any:
            failed.append((task_id, "manifest said done but no matching diff hunk was found"))

    return {"one_click": one_click, "copy_patch": copy_patch, "failed": failed}


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
async def run_heavy_repair(git_provider, tasks: List[dict]) -> Dict[str, list]:
    """Top-level Tier-2 orchestrator: clone -> prompt -> Copilot CLI -> diff ->
    manifest -> classify. Returns
    {"one_click": [...], "copy_patch": [...], "failed": [(task_id, reason), ...]}.
    Never raises.
    """
    if not tasks:
        return {"one_click": [], "copy_patch": [], "failed": []}

    task_ids = [f"SUG-{i + 1:03d}" for i in range(len(tasks))]
    task_by_id = {tid: t for tid, t in zip(task_ids, tasks)}

    with tempfile.TemporaryDirectory(prefix="pr_agent_tier2_") as tmp_root:
        try:
            source_branch = git_provider.get_pr_branch()
            mr_id = getattr(git_provider, "id_mr", 0) or 0
            repo_dir = _clone_source_branch(git_provider, tmp_root, mr_id, source_branch)
        except Exception as e:
            get_logger().warning(f"Tier-2 clone raised: {e}")
            return {"one_click": [], "copy_patch": [],
                    "failed": [(tid, f"clone error: {e}") for tid in task_ids]}
        if repo_dir.startswith("ERROR"):
            get_logger().warning(f"Tier-2 clone failed: {repo_dir}")
            return {"one_click": [], "copy_patch": [],
                    "failed": [(tid, repo_dir) for tid in task_ids]}

        prompt = build_heavy_repair_prompt(tasks, task_ids)
        timeout_seconds = int(_cfg("tier2_timeout_seconds", 480))
        session_start = time.monotonic()
        ok, message = run_copilot_cli(repo_dir, prompt, timeout_seconds)
        duration_ms = int((time.monotonic() - session_start) * 1000)
        if not ok:
            return {"one_click": [], "copy_patch": [], "failed": [(tid, message) for tid in task_ids]}

        diff_text = read_working_tree_diff(repo_dir)
        file_hunks = parse_unified_diff(diff_text)
        manifest = read_manifest(repo_dir)

        try:
            diff_file_patches = {f.filename: (f.patch or "") for f in git_provider.get_diff_files()}
        except Exception:
            diff_file_patches = {}

        classified = classify_heavy_repair_results(manifest, file_hunks, diff_file_patches, task_by_id)
        # Every renderable result (whether one-click or copy-patch) is tagged
        # with how long the Tier-2 session took, so store.py's
        # tier2_duration_ms telemetry column (a later task) can be populated
        # by whoever publishes these results, without needing to plumb the
        # timer through a second layer.
        for item in classified["one_click"] + classified["copy_patch"]:
            item["tier2_duration_ms"] = duration_ms
        return classified