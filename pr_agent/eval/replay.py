"""Replay benchmark CLI for the offline evaluation set.

Subcommands
-----------
- ``sync``    : pull the review_runs database from the server (scp), like the
  feedback report tool.
- ``list``    : show captured baseline ``review_runs``.
- ``run``     : replay review_runs against the frozen diff (GitLab Compare API)
  under an experiment ``--tag`` and store the new outputs. ``--dry-run`` only
  fetches the frozen diff (no LLM calls) to validate the plumbing.
- ``compare`` : compare two tags' outputs side by side, joined with the human
  feedback scores, to see whether a prompt/model change improved things.

Run it from the repo root with the project venv, e.g.::

    PYTHONPATH=. ./.venv/bin/python -m pr_agent.eval.replay list
    PYTHONPATH=. ./.venv/bin/python -m pr_agent.eval.replay run --tag baseline
    PYTHONPATH=. ./.venv/bin/python -m pr_agent.eval.replay compare baseline exp1

``run`` (non dry-run) needs the same GitLab token + model credentials the live
server uses, since it re-executes a real review.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DEFAULT_LOCAL_DB = os.path.join(REPO_ROOT, "data", "feedback", "review_feedback.db")
DEFAULT_REMOTE_DB = "/srv/mr-agent/data/feedback/review_feedback.db"
DEFAULT_REMOTE_HOST = "mr-agent@example.invalid"
# A local, git-ignored file holding GitLab/model credentials and overrides so a
# replay does not need `export VAR=...` every time. See _load_local_env().
DEFAULT_ENV_FILE = os.path.join(REPO_ROOT, ".env.eval.local")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _load_local_env(path: str) -> None:
    """Load ``KEY=VALUE`` lines from a local env file into ``os.environ``.

    Lets the replay read GitLab/model credentials and config overrides from a
    git-ignored file instead of requiring ``export`` before every run. Rules:

    - comments (``#``) and blank lines are ignored;
    - surrounding quotes are stripped;
    - unfilled placeholders (``<...>``) and empty values are skipped, so an
      un-edited template never injects a bogus token;
    - a value already present in the real environment is NOT overridden, so an
      explicit ``export`` still wins.

    Must run before any ``pr_agent.config_loader`` import so Dynaconf's env
    loader (``SECTION__KEY``) and native vars (e.g. ``ANTHROPIC_BASE_URL``) are
    picked up. Only key NAMES are printed, never values.
    """
    if not path or not os.path.exists(path):
        return
    loaded = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key or not value:
                    continue
                if value.startswith("<") and value.endswith(">"):
                    continue  # unfilled placeholder
                if os.environ.get(key):
                    continue  # an exported value takes precedence
                os.environ[key] = value
                loaded.append(key)
    except Exception as e:
        print(f"⚠  Could not read env file {path}: {e}", file=sys.stderr)
        return
    if loaded:
        print(f"🔧 Loaded {len(loaded)} setting(s) from {path}: {', '.join(loaded)}")


def _inject_response_language() -> None:
    """Mirror ``PRAgent.handle_request``'s response-language injection.

    The live server appends a "respond in <locale>" instruction to every tool's
    ``extra_instructions`` at its entrypoint. The replay calls ``PRReviewer``
    directly and bypasses that layer, which is why replayed output came back in
    English. Re-apply the exact same logic here so a replay matches production.
    Idempotent (dedup guard) and best-effort.
    """
    from pr_agent.config_loader import get_settings
    from pr_agent.log import get_logger
    try:
        response_language = get_settings().config.get("response_language", "en-us")
        if str(response_language).lower() == "en-us":
            return
        lang_instruction_text = (
            f"Your response MUST be written in the language corresponding to "
            f"locale code: '{response_language}'. This is crucial.")
        separator_text = "\n======\n\nIn addition, "
        for key in get_settings():
            setting = get_settings().get(key)
            if str(type(setting)) == "<class 'dynaconf.utils.boxing.DynaBox'>":
                if hasattr(setting, "extra_instructions"):
                    current = setting.extra_instructions
                    if lang_instruction_text not in str(current):
                        if current:
                            setting.extra_instructions = str(current) + separator_text + lang_instruction_text
                        else:
                            setting.extra_instructions = lang_instruction_text
    except Exception as e:
        get_logger().warning(f"Failed to inject response language for replay: {e}")


def _resolve_runs_db(args: argparse.Namespace) -> str:
    if getattr(args, "db", None):
        return args.db
    return os.environ.get("PR_FEEDBACK_DB_PATH", "") or DEFAULT_LOCAL_DB


def _human_scores(runs_db: str) -> dict:
    """Map review_id -> latest human score from review_feedback (best-effort)."""
    scores = {}
    if not os.path.exists(runs_db):
        return scores
    conn = sqlite3.connect(runs_db)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT review_id, score, comment FROM review_feedback "
                "WHERE review_id IS NOT NULL ORDER BY created_at"
            ).fetchall()
        except Exception:
            return scores
        for row in rows:
            scores[row["review_id"]] = {"score": row["score"], "comment": row["comment"]}
    finally:
        conn.close()
    return scores


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    host = args.host or os.environ.get("PR_FEEDBACK_REMOTE_HOST", DEFAULT_REMOTE_HOST)
    remote_path = args.remote_path or os.environ.get("PR_FEEDBACK_REMOTE_PATH", DEFAULT_REMOTE_DB)
    local_path = _resolve_runs_db(args)
    _ensure_parent_dir(local_path)

    print(f"⬇  Syncing review_runs DB from {host} ...")
    cmd = ["scp", f"{host}:{remote_path}", local_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"❌ scp failed: {result.stderr.strip()}", file=sys.stderr)
            return 1
    except FileNotFoundError:
        print("❌ scp not found. Please install openssh-client.", file=sys.stderr)
        return 1

    print(f"✅ Synced to {local_path}")
    from pr_agent.eval.store import list_review_runs
    runs = list_review_runs(path=local_path)
    replayable = [r for r in runs if r.get("base_sha") and r.get("head_sha")]
    print(f"   {len(runs)} review_runs ({len(replayable)} replayable with frozen shas).")
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    from pr_agent.eval.store import list_review_runs
    runs_db = _resolve_runs_db(args)
    runs = list_review_runs(path=runs_db, limit=args.limit, only_replayable=args.replayable)
    if not runs:
        print("No review_runs found. Enable [eval] enable_capture, collect some "
              "/feedback ratings, then `sync`.")
        return 0

    scores = _human_scores(runs_db)
    print(f"\n🧪 Captured review_runs ({len(runs)} shown)")
    print("─" * 92)
    print(f"  {'review_id':<14}{'score':>5}  {'project':<26}{'mr':>5}  {'base→head':<20}{'model':<14}")
    print("─" * 92)
    for r in runs:
        rid = (r.get("review_id") or "")[:12]
        score = scores.get(r.get("review_id"), {}).get("score")
        score_s = str(score) if score is not None else "-"
        project = (str(r.get("project") or ""))[:24]
        mr = str(r.get("mr_iid") or "")
        base = (r.get("base_sha") or "")[:7]
        head = (r.get("head_sha") or "")[:7]
        refs = f"{base}→{head}" if base and head else "(no shas)"
        model = (str(r.get("model") or ""))[:12]
        print(f"  {rid:<14}{score_s:>5}  {project:<26}{mr:>5}  {refs:<20}{model:<14}")
    print("─" * 92)
    return 0


# ---------------------------------------------------------------------------
# run (replay)
# ---------------------------------------------------------------------------

async def _replay_one(run: dict, tag: str, dry_run: bool, model: Optional[str],
                      use_frozen_input: bool = True) -> dict:
    from pr_agent.config_loader import get_settings
    from pr_agent.git_providers import _GIT_PROVIDERS
    from pr_agent.eval.benchmark_provider import BenchmarkGitProvider

    review_id = run.get("review_id")
    pr_url = run.get("pr_url")
    base_sha = run.get("base_sha")
    head_sha = run.get("head_sha")
    started = time.time()

    result = {
        "tag": tag,
        "review_id": review_id,
        "pr_url": pr_url,
        "project": run.get("project"),
        "mr_iid": run.get("mr_iid"),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "model": model or run.get("model"),
    }

    if not pr_url or not base_sha or not head_sha:
        result.update(status="skipped", error="missing pr_url/base_sha/head_sha")
        return result

    # register benchmark provider + point the config at it (process-local)
    _GIT_PROVIDERS["benchmark"] = BenchmarkGitProvider
    get_settings().set("config.git_provider", "benchmark")
    get_settings().set("config.publish_output", False)
    get_settings().set("config.publish_output_progress", False)
    get_settings().set("eval._replay_base_sha", base_sha)
    get_settings().set("eval._replay_head_sha", head_sha)
    get_settings().set("eval.enable_capture", False)  # never stamp during replay

    # strict replay: feed the exact non-code inputs frozen at review time
    frozen_input = None
    if use_frozen_input:
        raw_input = run.get("input_json")
        if raw_input:
            try:
                frozen_input = raw_input if isinstance(raw_input, dict) else json.loads(raw_input)
            except Exception:
                frozen_input = None
    get_settings().set("eval._replay_input_json", frozen_input or {})
    result["input_frozen"] = bool(frozen_input)
    if model:
        get_settings().set("config.model", model)
    get_settings().set("data", {})

    try:
        if dry_run:
            provider = BenchmarkGitProvider(pr_url, base_sha=base_sha, head_sha=head_sha,
                                            input_snapshot=frozen_input)
            files = provider.get_diff_files()
            result.update(status="dry_run",
                          review_output=f"[dry-run] fetched {len(files)} changed files",
                          duration_ms=int((time.time() - started) * 1000))
            return result

        _inject_response_language()
        from pr_agent.tools.pr_reviewer import PRReviewer
        reviewer = PRReviewer(pr_url)
        await reviewer.run()
        artifact = (get_settings().get("data", {}) or {}).get("artifact")
        output = artifact if isinstance(artifact, str) else (str(artifact) if artifact else "")
        result.update(status="ok" if output else "empty",
                      review_output=output,
                      duration_ms=int((time.time() - started) * 1000))
        return result
    except Exception as e:
        result.update(status="error", error=str(e),
                      duration_ms=int((time.time() - started) * 1000))
        return result


def cmd_run(args: argparse.Namespace) -> int:
    from pr_agent.eval.store import (list_review_runs, save_replay_result,
                                     get_benchmark_db_path)

    runs_db = _resolve_runs_db(args)
    runs = list_review_runs(path=runs_db, only_replayable=True)
    if args.review_id:
        runs = [r for r in runs if r.get("review_id") == args.review_id]
    if args.limit:
        runs = runs[: args.limit]

    if not runs:
        print("No replayable review_runs (need frozen base/head shas). "
              "Collect feedback with [eval] enable_capture on, then sync.")
        return 0

    bench_db = args.out or get_benchmark_db_path()
    print(f"▶  Replaying {len(runs)} review_runs under tag '{args.tag}'"
          f"{' (dry-run)' if args.dry_run else ''} → {bench_db}")

    ok = 0
    for i, run in enumerate(runs, 1):
        rid = (run.get("review_id") or "")[:12]
        print(f"  [{i}/{len(runs)}] {rid} ...", end=" ", flush=True)
        result = asyncio.run(_replay_one(run, args.tag, args.dry_run, args.model,
                                         use_frozen_input=not args.no_frozen_input))
        save_replay_result(result, path=bench_db)
        status = result.get("status")
        print(status + (f" ({result.get('error')})" if result.get("error") else ""))
        if status in ("ok", "dry_run"):
            ok += 1
    print(f"✅ Done: {ok}/{len(runs)} succeeded. Results stored under tag '{args.tag}'.")
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def cmd_compare(args: argparse.Namespace) -> int:
    from pr_agent.eval.store import list_replay_results, get_benchmark_db_path

    bench_db = args.out or get_benchmark_db_path()
    runs_db = _resolve_runs_db(args)
    left = {r["review_id"]: r for r in list_replay_results(args.tag_a, path=bench_db)}
    right = {r["review_id"]: r for r in list_replay_results(args.tag_b, path=bench_db)}
    scores = _human_scores(runs_db)

    common = sorted(set(left) & set(right))
    if not common:
        print(f"No overlapping review_ids between '{args.tag_a}' and '{args.tag_b}'. "
              f"Run both tags first.")
        return 0

    print(f"\n📊 Compare  '{args.tag_a}'  vs  '{args.tag_b}'   ({len(common)} samples)")
    print("─" * 88)
    print(f"  {'review_id':<14}{'human':>6}  {'len_a':>8}{'len_b':>8}{'Δlen':>8}  status")
    print("─" * 88)
    len_a_total = len_b_total = 0
    for rid in common:
        a, b = left[rid], right[rid]
        la = len(a.get("review_output") or "")
        lb = len(b.get("review_output") or "")
        len_a_total += la
        len_b_total += lb
        human = scores.get(rid, {}).get("score")
        human_s = str(human) if human is not None else "-"
        st = f"{a.get('status')}/{b.get('status')}"
        print(f"  {rid[:12]:<14}{human_s:>6}  {la:>8}{lb:>8}{lb-la:>+8}  {st}")
    print("─" * 88)
    n = len(common)
    print(f"  avg length: {args.tag_a}={len_a_total // n}  {args.tag_b}={len_b_total // n}  "
          f"Δ={(len_b_total - len_a_total) // n:+d}")
    print("\nNote: length is only a rough proxy. Read individual outputs with "
          "`--show <review_id>` for qualitative comparison.")
    if args.show:
        _show_pair(left.get(args.show), right.get(args.show), args.tag_a, args.tag_b)
    return 0


def _show_pair(a, b, tag_a, tag_b):
    print("\n" + "=" * 88)
    print(f"--- {tag_a} ---\n")
    print((a or {}).get("review_output") or "(none)")
    print("\n" + "=" * 88)
    print(f"--- {tag_b} ---\n")
    print((b or {}).get("review_output") or "(none)")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr_agent.eval.replay",
        description="Offline evaluation / replay benchmark for PR-Agent reviews.")
    parser.add_argument("--db", help="path to the review_runs (feedback) SQLite DB")
    parser.add_argument("--env-file", dest="env_file", default=DEFAULT_ENV_FILE,
                        help="local KEY=VALUE file with GitLab/model credentials "
                             "(default: .env.eval.local in the repo root)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="pull the review_runs DB from the server")
    p_sync.add_argument("--host", help="ssh host, e.g. root@1.2.3.4")
    p_sync.add_argument("--remote-path", dest="remote_path", help="remote DB path")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="list captured review_runs")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--replayable", action="store_true",
                        help="only rows with frozen base/head shas")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="replay review_runs under an experiment tag")
    p_run.add_argument("--tag", required=True, help="experiment label, e.g. baseline / exp1")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--review-id", dest="review_id", default=None,
                       help="replay only this review_id")
    p_run.add_argument("--model", default=None, help="override config.model for this run")
    p_run.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="only fetch the frozen diff (no LLM calls)")
    p_run.add_argument("--no-frozen-input", dest="no_frozen_input", action="store_true",
                       help="re-fetch live MR inputs instead of the frozen review-time snapshot")
    p_run.add_argument("--out", default=None, help="benchmark results DB path")
    p_run.set_defaults(func=cmd_run)

    p_cmp = sub.add_parser("compare", help="compare two experiment tags")
    p_cmp.add_argument("tag_a")
    p_cmp.add_argument("tag_b")
    p_cmp.add_argument("--out", default=None, help="benchmark results DB path")
    p_cmp.add_argument("--show", default=None, help="print full outputs for this review_id")
    p_cmp.set_defaults(func=cmd_compare)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Load local credentials/overrides BEFORE any pr_agent.config_loader import
    # so Dynaconf picks them up.
    _load_local_env(getattr(args, "env_file", DEFAULT_ENV_FILE))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
