"""Feedback analysis report CLI.

Usage as a module::

    python -m pr_agent.feedback.report <subcommand> [options]

Subcommands::

    sync        Pull the remote feedback DB to the local machine via scp.
    summary     Overall dashboard: totals, distribution, weekly trend.
    projects    Per-project breakdown with count / avg / min / max scores.
    low-scores  List low-score reviews with optional comment preview.
    comments    List feedback records that include a user-written comment.
    export      Dump all feedback rows as CSV to stdout or a file.
    html        Generate a polished static HTML dashboard.

Examples::

    # Sync DB from the server, then show the summary
    python -m pr_agent.feedback.report sync
    python -m pr_agent.feedback.report summary

    # One-liner: sync and show summary
    python -m pr_agent.feedback.report sync && python -m pr_agent.feedback.report summary

    # Filter by time range and project
    python -m pr_agent.feedback.report summary --days 30
    python -m pr_agent.feedback.report projects --sort-by avg

    # Export to CSV for Excel / charts
    python -m pr_agent.feedback.report export -o report.csv

    # Generate a polished HTML dashboard
    python -m pr_agent.feedback.report html -o feedback-report.html
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from html import escape
from typing import Optional, Sequence

from pr_agent.feedback.timez import now_cn, to_cn_display
from pr_agent.suggestions.store import migrate_schema as _migrate_inline_schema

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

DEFAULT_LOCAL_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "feedback", "review_feedback.db",
)
DEFAULT_REMOTE_DB = "/srv/mr-agent/data/feedback/review_feedback.db"
DEFAULT_REMOTE_HOST = "mr-agent@example.invalid"
DEFAULT_GITLAB_URL = "https://gitlab.example.com"


def resolve_db_path(args: argparse.Namespace) -> str:
    """Return the effective DB path from CLI args, env, or default."""
    if getattr(args, "db", None):
        return args.db
    env = os.environ.get("PR_FEEDBACK_DB_PATH", "")
    if env:
        return env
    return DEFAULT_LOCAL_DB


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f"❌ Feedback database not found: {db_path}", file=sys.stderr)
        print("   Run `python -m pr_agent.feedback.report sync` to pull it from the server.",
              file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Subcommand: sync
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    host = args.host or os.environ.get("PR_FEEDBACK_REMOTE_HOST", DEFAULT_REMOTE_HOST)
    remote_path = args.remote_path or os.environ.get("PR_FEEDBACK_REMOTE_PATH", DEFAULT_REMOTE_DB)
    local_path = resolve_db_path(args)
    _ensure_parent_dir(local_path)

    print(f"⬇  Syncing feedback DB from {host} ...")
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
    # Quick sanity check
    if os.path.exists(local_path):
        conn = sqlite3.connect(local_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM review_feedback").fetchone()[0]
            print(f"   {count} feedback records available.")
        except Exception:
            pass
        finally:
            conn.close()
    return 0


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


# ---------------------------------------------------------------------------
# Shared query helpers
# ---------------------------------------------------------------------------

def _time_filter(days: Optional[int]) -> tuple:
    """Return (where_clause, params) for an optional days-ago filter."""
    if not days:
        return "", ()
    cutoff = (now_cn() - timedelta(days=days)).isoformat()
    return "WHERE created_at >= ?", (cutoff,)


def _project_filter(project: Optional[str]) -> tuple:
    if not project:
        return "", ()
    return "AND project = ?" if "WHERE" in _time_filter(1)[0] else "WHERE project = ?", (project,)


def _build_where(days: Optional[int] = None, project: Optional[str] = None,
                 min_score: Optional[int] = None, max_score: Optional[int] = None,
                 has_comment: bool = False) -> tuple:
    clauses = []
    params = []
    if days:
        cutoff = (now_cn() - timedelta(days=days)).isoformat()
        clauses.append("created_at >= ?")
        params.append(cutoff)
    if project:
        clauses.append("project = ?")
        params.append(project)
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if max_score is not None:
        clauses.append("score <= ?")
        params.append(max_score)
    if has_comment:
        clauses.append("comment IS NOT NULL AND comment != ''")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, tuple(params)


# ---------------------------------------------------------------------------
# Subcommand: summary
# ---------------------------------------------------------------------------

def cmd_summary(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args))
    try:
        where, params = _build_where(days=args.days, project=args.project)

        # Totals & averages
        row = conn.execute(
            f"SELECT COUNT(*) AS c, AVG(score) AS a FROM review_feedback {where}", params
        ).fetchone()
        total = row["c"]
        if not total:
            print("No feedback recorded yet.")
            return 0

        # Median
        median_row = conn.execute(
            f"""SELECT score FROM review_feedback {where}
                ORDER BY score LIMIT 1 OFFSET ?""",
            (*params, total // 2),
        ).fetchone()
        median = median_row["score"] if median_row else "N/A"

        avg = row["a"]

        print(f"\n📊 Review Feedback Summary", end="")
        if args.days:
            print(f" (last {args.days} days)", end="")
        if args.project:
            print(f" — {args.project}", end="")
        print()
        print("━" * 52)
        print(f"  Total: {total:>4}    Avg: {avg:.2f}    Median: {median}")

        # Score distribution
        print(f"\n  Score Distribution:")
        dist = conn.execute(
            f"SELECT score, COUNT(*) AS c FROM review_feedback {where} GROUP BY score ORDER BY score",
            params,
        ).fetchall()
        stars = {1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★"}
        max_count = max(row["c"] for row in dist) if dist else 1
        for row_d in dist:
            s = row_d["score"]
            c = row_d["c"]
            pct = c / total * 100
            bar_len = int(c / max_count * 20)
            bar = "█" * bar_len
            print(f"  {s} {stars.get(s, '?')}  {bar:<20} {c:>3} ({pct:5.1f}%)")

        # Weekly trend
        print(f"\n  Weekly Trend (avg):")
        week_rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', created_at) AS week, AVG(score) AS a, COUNT(*) AS c
                FROM review_feedback {where}
                GROUP BY week ORDER BY week DESC LIMIT 8""",
            params,
        ).fetchall()
        if week_rows:
            max_avg = max(row_w["a"] for row_w in week_rows)
            for row_w in reversed(week_rows):
                bar_len = max(1, int(row_w["a"] / max_avg * 20)) if max_avg else 1
                bar = "█" * bar_len
                print(f"  {row_w['week']}  {row_w['a']:.1f}  {bar} ({row_w['c']} feedbacks)")
        else:
            print("  (not enough data)")

        print()
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: projects
# ---------------------------------------------------------------------------

def cmd_projects(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args))
    try:
        where, params = _build_where(days=args.days)
        rows = conn.execute(
            f"""SELECT project, COUNT(*) AS c, ROUND(AVG(score), 1) AS a,
                       MIN(score) AS mn, MAX(score) AS mx
                FROM review_feedback {where}
                GROUP BY project ORDER BY {
                    'a' if args.sort_by == 'avg' else 'c'} {
                    'ASC' if args.sort_by == 'avg' else 'DESC'}""",
            params,
        ).fetchall()

        if not rows:
            print("No project data available.")
            return 0

        print(f"\n📁 Feedback by Project", end="")
        if args.days:
            print(f" (last {args.days} days)", end="")
        print()
        print("━" * 60)
        print(f"  {'Project':<30} {'Count':>6}  {'Avg':>5}  {'Min':>4}  {'Max':>4}")
        print(f"  {'─' * 30} {'─' * 6}  {'─' * 5}  {'─' * 4}  {'─' * 4}")

        all_avg = sum(r["a"] for r in rows) / len(rows) if rows else 0
        for r in rows:
            name = (r["project"] or "(unknown)")[:30]
            flag = " ⚠️" if r["a"] < all_avg - 0.5 else ""
            print(f"  {name:<30} {r['c']:>6}  {r['a']:>5.1f}  {r['mn']:>4}  {r['mx']:>4}{flag}")
        print()
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: low-scores
# ---------------------------------------------------------------------------

def cmd_low_scores(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args))
    try:
        where, params = _build_where(
            days=args.days, project=args.project, max_score=args.max_score,
        )
        rows = conn.execute(
            f"""SELECT created_at, pr_url, project, mr_iid, reviewer_user, score, comment
                FROM review_feedback {where}
                ORDER BY score ASC, created_at DESC LIMIT ?""",
            (*params, args.limit),
        ).fetchall()

        if not rows:
            print(f"No reviews with score ≤ {args.max_score} found.")
            return 0

        print(f"\n🔍 Low-Score Reviews (≤ {args.max_score})", end="")
        if args.days:
            print(f", last {args.days} days", end="")
        print()
        print("━" * 70)

        for r in rows:
            ts = to_cn_display(r["created_at"])
            pr = r["pr_url"] or f"{r['project']}!{r['mr_iid']}"
            user = r["reviewer_user"] or "?"
            comment = (r["comment"] or "").replace("\n", " ").strip()
            print(f"\n  [{ts}] {r['score']}/5 by {user}  {pr}")
            if comment:
                print(f"      💬 {comment[:120]}")
        print()
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: comments
# ---------------------------------------------------------------------------

def cmd_comments(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args))
    try:
        where, params = _build_where(
            days=args.days, project=args.project, min_score=args.min_score,
            max_score=args.max_score, has_comment=True,
        )
        rows = conn.execute(
            f"""SELECT created_at, reviewer_user, score, comment, pr_url, project, mr_iid
                FROM review_feedback {where}
                ORDER BY created_at DESC LIMIT ?""",
            (*params, args.limit),
        ).fetchall()

        if not rows:
            print("No feedback with comments found.")
            return 0

        print(f"\n💬 Feedback with Comments", end="")
        if args.days:
            print(f" (last {args.days} days)", end="")
        print()
        print("━" * 60)

        for r in rows:
            user = (r["reviewer_user"] or "?")[:15]
            comment = (r["comment"] or "").replace("\n", " ").strip()
            ts = to_cn_display(r["created_at"])
            print(f"\n  [{r['score']}/5] {user}  {ts}")
            print(f"      💬 {comment}")
            print(f"      🔗 {r['pr_url']}")
        print()
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args))
    try:
        where, params = _build_where(days=args.days, project=args.project)
        rows = conn.execute(
            f"""SELECT id, created_at, pr_url, project, mr_iid, mr_author,
                       reviewer_user, score, comment, review_id, commit_sha,
                       model, source
                FROM review_feedback {where} ORDER BY created_at DESC""",
            params,
        ).fetchall()

        if args.output:
            with open(args.output, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(rows[0].keys() if rows else [])
                for r in rows:
                    writer.writerow(r)
            print(f"✅ Exported {len(rows)} records to {args.output}")
        else:
            writer = csv.writer(sys.stdout)
            if rows:
                writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow(r)
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: html
# ---------------------------------------------------------------------------

def cmd_html(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args))
    try:
        where, params = _build_where(days=args.days, project=args.project)

        overview = conn.execute(
            f"SELECT COUNT(*) AS c, AVG(score) AS a FROM review_feedback {where}", params
        ).fetchone()
        total = overview["c"]
        if not total:
            print("No feedback recorded yet.")
            return 0

        median_row = conn.execute(
            f"""SELECT score FROM review_feedback {where}
                ORDER BY score LIMIT 1 OFFSET ?""",
            (*params, total // 2),
        ).fetchone()
        median = median_row["score"] if median_row else "N/A"
        avg = overview["a"] or 0

        dist_rows = conn.execute(
            f"SELECT score, COUNT(*) AS c FROM review_feedback {where} GROUP BY score ORDER BY score",
            params,
        ).fetchall()
        dist_map = {row["score"]: row["c"] for row in dist_rows}
        dist_labels = ["1", "2", "3", "4", "5"]
        dist_values = [dist_map.get(score, 0) for score in range(1, 6)]

        week_rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', created_at) AS week, AVG(score) AS a, COUNT(*) AS c
                FROM review_feedback {where}
                GROUP BY week ORDER BY week DESC LIMIT 8""",
            params,
        ).fetchall()
        week_rows = list(reversed(week_rows))

        project_rows = conn.execute(
            f"""SELECT project, COUNT(*) AS c, ROUND(AVG(score), 2) AS a,
                       MIN(score) AS mn, MAX(score) AS mx
                FROM review_feedback {where}
                GROUP BY project ORDER BY c DESC, a DESC LIMIT 8""",
            params,
        ).fetchall()

        all_rows = conn.execute(
            f"""SELECT id, created_at, reviewer_user, score, comment, pr_url, project,
                       mr_iid, mr_author, review_id, commit_sha, model, source
                FROM review_feedback {where}
                ORDER BY created_at DESC""",
            params,
        ).fetchall()

        html = _render_html_dashboard(
            total=total,
            avg=avg,
            median=median,
            days=args.days,
            project=args.project,
            dist_labels=dist_labels,
            dist_values=dist_values,
            week_rows=week_rows,
            project_rows=project_rows,
            all_rows=all_rows,
        )
        output = args.output or "feedback-report.html"
        with open(output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✅ Generated HTML report: {output}")
    finally:
        conn.close()
    return 0


def _render_html_dashboard(
    *,
    total: int,
    avg: float,
    median,
    days: int,
    project: Optional[str],
    dist_labels: list[str],
    dist_values: list[int],
    week_rows,
    project_rows,
    all_rows,
) -> str:
    now = now_cn().strftime("%Y-%m-%d %H:%M")
    week_labels = [row["week"] for row in week_rows]
    week_values = [round(row["a"], 2) for row in week_rows]
    all_feedback_rows = [
        {
            "id": row["id"],
            "created_at": to_cn_display(row["created_at"]) if row["created_at"] else "",
            "reviewer_user": row["reviewer_user"] or "",
            "score": row["score"],
            "comment": (row["comment"] or "").replace("\n", " ").strip(),
            "pr_url": row["pr_url"] or "",
            "project": row["project"] or "",
            "mr_iid": row["mr_iid"] or "",
            "mr_author": row["mr_author"] or "",
            "review_id": row["review_id"] or "",
            "commit_sha": row["commit_sha"] or "",
            "model": row["model"] or "",
            "source": row["source"] or "",
        }
        for row in all_rows
    ]
    all_feedback_json = json.dumps(all_feedback_rows, ensure_ascii=False).replace("</", "<\\/")

    project_table_rows = "".join(
        f"""
        <tr>
          <td>{escape((row['project'] or '(unknown)'))}</td>
          <td>{row['c']}</td>
          <td>{float(row['a'] or 0):.2f}</td>
          <td>{row['mn']}</td>
          <td>{row['mx']}</td>
        </tr>
        """
        for row in project_rows
    ) or '<tr><td colspan="5" class="muted">No project data</td></tr>'

    title_suffix = f" · {escape(project)}" if project else ""
    subtitle = f"Last {days} days" if days else "All data"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PR-Agent Feedback Report{title_suffix}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: rgba(17, 24, 39, 0.82);
      --panel-2: rgba(30, 41, 59, 0.88);
      --text: #e5eefb;
      --muted: #94a3b8;
      --line: rgba(148, 163, 184, 0.18);
      --accent: #60a5fa;
      --accent-2: #a78bfa;
      --green: #34d399;
      --yellow: #fbbf24;
      --red: #f87171;
      --shadow: 0 20px 40px rgba(0, 0, 0, 0.35);
      --radius: 22px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(96,165,250,.24), transparent 28%),
        radial-gradient(circle at top right, rgba(167,139,250,.18), transparent 24%),
        linear-gradient(180deg, #0b1020 0%, #111827 100%);
    }}
    a {{ color: inherit; text-decoration: none; }}
    .container {{ max-width: 1280px; margin: 0 auto; padding: 32px 24px 48px; }}
    .hero {{
      display: flex; justify-content: space-between; gap: 24px; align-items: flex-start;
      padding: 28px 30px; border: 1px solid var(--line); border-radius: 28px;
      background: linear-gradient(135deg, rgba(15,23,42,.88), rgba(30,41,59,.72));
      box-shadow: var(--shadow); backdrop-filter: blur(16px);
    }}
    .hero h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .hero p {{ margin: 0; color: var(--muted); }}
    .stamp {{ text-align: right; color: var(--muted); font-size: 14px; }}
    .metrics {{
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 18px;
      margin-top: 24px;
    }}
    .card {{
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
      box-shadow: var(--shadow); padding: 22px; backdrop-filter: blur(14px);
    }}
    .metric-value {{ font-size: 34px; font-weight: 700; margin-top: 10px; }}
    .metric-label, .section-subtitle, .muted {{ color: var(--muted); }}
    .grid-2 {{
      display: grid; grid-template-columns: 1.15fr 1fr; gap: 18px; margin-top: 18px;
    }}
    .section-title {{
      margin: 0 0 6px; font-size: 18px; font-weight: 700;
    }}
    .chart-wrap {{ height: 320px; margin-top: 18px; }}
    table {{
      width: 100%; border-collapse: collapse; margin-top: 16px; overflow: hidden;
      border-radius: 14px;
    }}
    th, td {{
      text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--line);
      font-size: 14px;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    tr:last-child td {{ border-bottom: none; }}
    .score-badge {{
      min-width: 58px; text-align: center; padding: 4px 10px; border-radius: 999px;
      font-size: 12px; font-weight: 700; color: white;
    }}
    .score-1 {{ background: linear-gradient(135deg, #ef4444, #f87171); }}
    .score-2 {{ background: linear-gradient(135deg, #f97316, #fb923c); }}
    .score-3 {{ background: linear-gradient(135deg, #f59e0b, #fbbf24); color: #1f2937; }}
    .score-4 {{ background: linear-gradient(135deg, #10b981, #34d399); }}
    .score-5 {{ background: linear-gradient(135deg, #3b82f6, #60a5fa); }}
    .empty-state {{
      color: var(--muted); padding: 18px; border: 1px dashed var(--line); border-radius: 16px;
      margin-top: 16px;
    }}
    .filters {{
      display: grid; grid-template-columns: 1.2fr repeat(4, minmax(0, 180px)); gap: 12px;
      margin-top: 16px;
    }}
    .field {{ display: flex; flex-direction: column; gap: 8px; }}
    .field label {{ color: var(--muted); font-size: 13px; }}
    .field input, .field select {{
      width: 100%; border: 1px solid var(--line); background: rgba(15, 23, 42, 0.85);
      color: var(--text); padding: 12px 14px; border-radius: 12px; outline: none;
    }}
    .field input::placeholder {{ color: #64748b; }}
    .toolbar {{
      display: flex; justify-content: space-between; align-items: center; gap: 12px;
      margin-top: 16px; color: var(--muted); font-size: 14px;
    }}
    .table-wrap {{
      margin-top: 14px; border: 1px solid var(--line); border-radius: 16px; overflow: hidden;
      background: rgba(2, 6, 23, 0.28);
    }}
    .table-wrap table {{ margin-top: 0; }}
    .table-wrap td {{ vertical-align: top; }}
    .mini-badge {{
      display: inline-flex; align-items: center; justify-content: center; min-width: 42px;
      padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700;
      color: white;
    }}
    .comment-cell {{
      max-width: 420px; white-space: normal; word-break: break-word; line-height: 1.55;
    }}
    .link-cell a {{ color: #93c5fd; }}
    .pager {{
      display: flex; justify-content: space-between; align-items: center; gap: 12px;
      margin-top: 16px; flex-wrap: wrap;
    }}
    .pager-actions {{ display: flex; gap: 10px; align-items: center; }}
    .btn {{
      border: 1px solid var(--line); background: rgba(15, 23, 42, 0.9); color: var(--text);
      padding: 10px 14px; border-radius: 12px; cursor: pointer;
    }}
    .btn:disabled {{ opacity: .45; cursor: not-allowed; }}
    @media (max-width: 980px) {{
      .metrics, .grid-2 {{ grid-template-columns: 1fr; }}
      .hero {{ flex-direction: column; }}
      .stamp {{ text-align: left; }}
      .filters {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 720px) {{
      .filters {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <section class="hero">
      <div>
        <h1>PR-Agent Feedback Report{title_suffix}</h1>
        <p>{escape(subtitle)} · A polished static dashboard generated from SQLite feedback data.</p>
      </div>
      <div class="stamp">
        <div>Generated at</div>
        <strong>{escape(now)}</strong>
      </div>
    </section>

    <section class="metrics">
      <div class="card">
        <div class="metric-label">Total feedback</div>
        <div class="metric-value">{total}</div>
      </div>
      <div class="card">
        <div class="metric-label">Average score</div>
        <div class="metric-value">{avg:.2f}</div>
      </div>
      <div class="card">
        <div class="metric-label">Median score</div>
        <div class="metric-value">{median}</div>
      </div>
      <div class="card">
        <div class="metric-label">Positive rate (4-5)</div>
        <div class="metric-value">{((dist_values[3] + dist_values[4]) / total * 100):.0f}%</div>
      </div>
    </section>

    <section class="grid-2">
      <div class="card">
        <h2 class="section-title">Score distribution</h2>
        <div class="section-subtitle">Overall rating mix across the selected range.</div>
        <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
      </div>
      <div class="card">
        <h2 class="section-title">Weekly trend</h2>
        <div class="section-subtitle">Average score changes over time.</div>
        <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
      </div>
    </section>

    <section class="card" style="margin-top: 18px;">
      <h2 class="section-title">Project summary table</h2>
      <div class="section-subtitle">Counts and score range by project.</div>
      <table>
        <thead>
          <tr>
            <th>Project</th>
            <th>Count</th>
            <th>Avg</th>
            <th>Min</th>
            <th>Max</th>
          </tr>
        </thead>
        <tbody>
          {project_table_rows}
        </tbody>
      </table>
    </section>

    <section class="card" style="margin-top: 18px;">
      <h2 class="section-title">All feedback explorer</h2>
      <div class="section-subtitle">Filter, search, and page through all feedback records in this report.</div>

      <div class="filters">
        <div class="field">
          <label for="searchInput">Search</label>
          <input id="searchInput" type="text" placeholder="Search user / project / comment / link" />
        </div>
        <div class="field">
          <label for="scoreFilter">Score</label>
          <select id="scoreFilter">
            <option value="">All scores</option>
            <option value="1">1/5</option>
            <option value="2">2/5</option>
            <option value="3">3/5</option>
            <option value="4">4/5</option>
            <option value="5">5/5</option>
          </select>
        </div>
        <div class="field">
          <label for="commentFilter">Comments</label>
          <select id="commentFilter">
            <option value="all">All</option>
            <option value="with">With comment</option>
            <option value="without">Without comment</option>
          </select>
        </div>
        <div class="field">
          <label for="projectFilter">Project</label>
          <select id="projectFilter">
            <option value="">All projects</option>
          </select>
        </div>
        <div class="field">
          <label for="sortFilter">Sort</label>
          <select id="sortFilter">
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="score-desc">Highest score</option>
            <option value="score-asc">Lowest score</option>
          </select>
        </div>
      </div>

      <div class="toolbar">
        <div id="resultSummary">Loading feedback rows...</div>
        <div class="field" style="width: 120px;">
          <label for="pageSize">Per page</label>
          <select id="pageSize">
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Score</th>
              <th>User</th>
              <th>Project</th>
              <th>Comment</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody id="feedbackTableBody">
            <tr><td colspan="6" class="muted">Loading...</td></tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <div id="pageInfo" class="muted">Page 1</div>
        <div class="pager-actions">
          <button id="prevPage" class="btn" type="button">Previous</button>
          <button id="nextPage" class="btn" type="button">Next</button>
        </div>
      </div>
    </section>
  </div>

  <script>
    const distLabels = {json.dumps(dist_labels, ensure_ascii=False)};
    const distValues = {json.dumps(dist_values)};
    const weekLabels = {json.dumps(week_labels, ensure_ascii=False)};
    const weekValues = {json.dumps(week_values)};
    const feedbackRows = {all_feedback_json};

    const commonLegend = {{
      labels: {{ color: '#cbd5e1', boxWidth: 12, usePointStyle: true }}
    }};
    const commonTicks = {{
      color: '#94a3b8',
      grid: {{ color: 'rgba(148, 163, 184, 0.12)' }}
    }};

    new Chart(document.getElementById('scoreChart'), {{
      type: 'doughnut',
      data: {{
        labels: distLabels,
        datasets: [{{
          data: distValues,
          backgroundColor: ['#f87171', '#fb923c', '#fbbf24', '#34d399', '#60a5fa'],
          borderColor: '#0f172a',
          borderWidth: 4,
          hoverOffset: 8
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: commonLegend,
        }}
      }}
    }});

    new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{
        labels: weekLabels,
        datasets: [{{
          label: 'Average score',
          data: weekValues,
          borderColor: '#a78bfa',
          backgroundColor: 'rgba(167, 139, 250, 0.18)',
          fill: true,
          tension: 0.35,
          pointRadius: 4,
          pointHoverRadius: 5
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: commonLegend }},
        scales: {{
          x: commonTicks,
          y: {{ ...commonTicks, min: 0, max: 5 }}
        }}
      }}
    }});

    const scoreFilter = document.getElementById('scoreFilter');
    const commentFilter = document.getElementById('commentFilter');
    const projectFilter = document.getElementById('projectFilter');
    const sortFilter = document.getElementById('sortFilter');
    const searchInput = document.getElementById('searchInput');
    const pageSizeSelect = document.getElementById('pageSize');
    const tableBody = document.getElementById('feedbackTableBody');
    const resultSummary = document.getElementById('resultSummary');
    const pageInfo = document.getElementById('pageInfo');
    const prevPageBtn = document.getElementById('prevPage');
    const nextPageBtn = document.getElementById('nextPage');

    let currentPage = 1;

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    function scoreBadge(score) {{
      return `<span class="mini-badge score-${{score}}">${{score}}/5</span>`;
    }}

    function renderProjectOptions() {{
      const projects = [...new Set(feedbackRows.map((row) => row.project || '(unknown)'))].sort();
      for (const project of projects) {{
        const option = document.createElement('option');
        option.value = project;
        option.textContent = project;
        projectFilter.appendChild(option);
      }}
    }}

    function getFilteredRows() {{
      const score = scoreFilter.value;
      const commentMode = commentFilter.value;
      const project = projectFilter.value;
      const search = searchInput.value.trim().toLowerCase();
      const sort = sortFilter.value;

      let rows = feedbackRows.filter((row) => {{
        if (score && String(row.score) !== score) return false;
        const hasComment = Boolean((row.comment || '').trim());
        if (commentMode === 'with' && !hasComment) return false;
        if (commentMode === 'without' && hasComment) return false;
        if (project) {{
          const rowProject = row.project || '(unknown)';
          if (rowProject !== project) return false;
        }}
        if (search) {{
          const haystack = [
            row.reviewer_user, row.project, row.comment, row.pr_url, row.mr_author, row.review_id
          ].join(' ').toLowerCase();
          if (!haystack.includes(search)) return false;
        }}
        return true;
      }});

      rows.sort((a, b) => {{
        if (sort === 'oldest') return a.created_at.localeCompare(b.created_at);
        if (sort === 'score-desc') return b.score - a.score || b.created_at.localeCompare(a.created_at);
        if (sort === 'score-asc') return a.score - b.score || b.created_at.localeCompare(a.created_at);
        return b.created_at.localeCompare(a.created_at);
      }});

      return rows;
    }}

    function renderTable() {{
      const rows = getFilteredRows();
      const pageSize = Number(pageSizeSelect.value);
      const totalRows = rows.length;
      const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
      currentPage = Math.min(currentPage, totalPages);
      const start = (currentPage - 1) * pageSize;
      const pageRows = rows.slice(start, start + pageSize);

      if (!pageRows.length) {{
        tableBody.innerHTML = '<tr><td colspan="6" class="muted">No matching feedback.</td></tr>';
      }} else {{
        tableBody.innerHTML = pageRows.map((row) => {{
          const ts = escapeHtml((row.created_at || '').slice(0, 16).replace('T', ' '));
          const projectName = escapeHtml(row.project || '(unknown)');
          const user = escapeHtml(row.reviewer_user || '?');
          const comment = escapeHtml(row.comment || '').trim() || '<span class="muted">—</span>';
          const prUrl = row.pr_url || '#';
          const prLabel = escapeHtml(prUrl.replace(/^https?:\\/\\//, ''));
          return `
            <tr>
              <td>${{ts}}</td>
              <td>${{scoreBadge(row.score)}}</td>
              <td>${{user}}</td>
              <td>${{projectName}}</td>
              <td class="comment-cell">${{comment}}</td>
              <td class="link-cell"><a href="${{escapeHtml(prUrl)}}" target="_blank" rel="noreferrer">${{prLabel}}</a></td>
            </tr>
          `;
        }}).join('');
      }}

      const startDisplay = totalRows ? start + 1 : 0;
      const endDisplay = Math.min(start + pageSize, totalRows);
      resultSummary.textContent = `Showing ${{startDisplay}}-${{endDisplay}} of ${{totalRows}} feedback rows`;
      pageInfo.textContent = `Page ${{currentPage}} / ${{totalPages}}`;
      prevPageBtn.disabled = currentPage <= 1;
      nextPageBtn.disabled = currentPage >= totalPages;
    }}

    function resetToFirstPage() {{
      currentPage = 1;
      renderTable();
    }}

    [scoreFilter, commentFilter, projectFilter, sortFilter].forEach((el) => {{
      el.addEventListener('change', resetToFirstPage);
    }});
    searchInput.addEventListener('input', resetToFirstPage);
    pageSizeSelect.addEventListener('change', resetToFirstPage);
    prevPageBtn.addEventListener('click', () => {{
      if (currentPage > 1) {{
        currentPage -= 1;
        renderTable();
      }}
    }});
    nextPageBtn.addEventListener('click', () => {{
      currentPage += 1;
      renderTable();
    }});

    renderProjectOptions();
    renderTable();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PR-Agent Feedback Analysis Report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", title="subcommands")

    # sync
    p_sync = sub.add_parser("sync", help="Pull feedback DB from remote server")
    p_sync.add_argument("--host", help=f"SSH host (default: {DEFAULT_REMOTE_HOST})")
    p_sync.add_argument("--remote-path", help=f"Remote DB path (default: {DEFAULT_REMOTE_DB})")
    _add_db_arg(p_sync)

    # summary
    p_sum = sub.add_parser("summary", help="Overall dashboard")
    _add_common_args(p_sum)

    # projects
    p_proj = sub.add_parser("projects", help="Per-project breakdown")
    _add_common_args(p_proj)
    p_proj.add_argument("--sort-by", choices=["count", "avg"], default="count",
                        help="Sort by count or average score (default: count)")

    # low-scores
    p_low = sub.add_parser("low-scores", help="List low-score reviews")
    _add_common_args(p_low)
    p_low.add_argument("--max-score", type=int, default=2,
                        help="Max score to include (default: 2)")
    p_low.add_argument("--limit", type=int, default=20,
                       help="Max results (default: 20)")

    # comments
    p_comm = sub.add_parser("comments", help="Feedback with user comments")
    _add_common_args(p_comm)
    p_comm.add_argument("--min-score", type=int, default=None,
                        help="Min score filter")
    p_comm.add_argument("--max-score", type=int, default=None,
                        help="Max score filter")
    p_comm.add_argument("--limit", type=int, default=50,
                       help="Max results (default: 50)")

    # export
    p_exp = sub.add_parser("export", help="Export feedback as CSV")
    _add_common_args(p_exp)
    p_exp.add_argument("-o", "--output", help="Output CSV file (default: stdout)")

    # html
    p_html = sub.add_parser("html", help="Generate a polished static HTML dashboard")
    _add_common_args(p_html)
    p_html.add_argument("-o", "--output", help="Output HTML file (default: feedback-report.html)")
    p_html.add_argument("--limit", type=int, default=12, help="Max comments / low-score items (default: 12)")

    # inline-summary
    p_is = sub.add_parser("inline-summary", help="Inline suggestion adoption rate summary")
    _add_common_args(p_is)

    # inline-html
    p_ih = sub.add_parser("inline-html", help="Generate inline suggestion HTML dashboard")
    _add_common_args(p_ih)
    p_ih.add_argument("-o", "--output", help="Output HTML file (default: inline-report.html)")
    p_ih.add_argument("--gitlab-url", help=f"GitLab base URL for MR links (default: {DEFAULT_GITLAB_URL})")

    return parser


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help=f"Local SQLite path (default: {DEFAULT_LOCAL_DB})")


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    _add_db_arg(parser)
    parser.add_argument("--days", type=int, default=30,
                       help="Limit to last N days (default: 30)")
    parser.add_argument("--project", help="Filter by project name")


# ---------------------------------------------------------------------------
# __main__ support
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "sync": cmd_sync,
        "summary": cmd_summary,
        "projects": cmd_projects,
        "low-scores": cmd_low_scores,
        "comments": cmd_comments,
        "export": cmd_export,
        "html": cmd_html,
        "inline-summary": cmd_inline_summary,
        "inline-html": cmd_inline_html,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 1



# ---------------------------------------------------------------------------
# Subcommands: inline-summary / inline-html
# ---------------------------------------------------------------------------

def _inline_connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f"❌ Database not found: {db_path}", file=sys.stderr)
        print("   Run sync first or check --db path.", file=sys.stderr)
        sys.exit(1)
    # Self-heal: add any columns/tables introduced after this DB was created
    # (e.g. mr_url) so reports work against older synced copies too.
    _migrate_inline_schema(path=db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _pct(applied: int, published: int) -> str:
    if published == 0:
        return "N/A"
    return f"{applied / published * 100:.1f}%"


def cmd_inline_summary(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    conn = _inline_connect(db_path)
    days = getattr(args, "days", 30)
    project = getattr(args, "project", None)

    clauses: list = []
    params: list = []
    if days:
        cutoff = (now_cn() - timedelta(days=days)).isoformat()
        clauses.append("created_at >= ?")
        params.append(cutoff)
    if project:
        clauses.append("project = ?")
        params.append(project)

    pub_where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    published_total = conn.execute(
        f"SELECT COUNT(*) FROM published_suggestions {pub_where}", params
    ).fetchone()[0]

    app_clauses = clauses + ["applied_at IS NOT NULL"]
    app_where = "WHERE " + " AND ".join(app_clauses)
    applied_total = conn.execute(
        f"SELECT COUNT(*) FROM published_suggestions {app_where}", params
    ).fetchone()[0]

    print("\n=== Inline Suggestion Analytics ===")
    print(f"总发布建议: {published_total}  已采纳: {applied_total}  整体采纳率: {_pct(applied_total, published_total)}")

    # Per-project
    proj_where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    print("\n按项目:")
    rows = conn.execute(
        f"SELECT project,"
        f" COUNT(*) AS pub,"
        f" SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app"
        f" FROM published_suggestions {proj_where}"
        f" GROUP BY project ORDER BY pub DESC",
        params,
    ).fetchall()
    for r in rows:
        print(f"  {r['project']:<35} 发布 {r['pub']:>4}  采纳 {r['app']:>4}  {_pct(r['app'], r['pub'])}")

    # Recent MRs
    print("\n近期 MR 采纳情况（最近10条）:")
    mr_rows = conn.execute(
        f"SELECT project, mr_iid,"
        f" COUNT(*) AS pub,"
        f" SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app"
        f" FROM published_suggestions {proj_where}"
        f" GROUP BY project, mr_iid"
        f" ORDER BY MAX(created_at) DESC LIMIT 10",
        params,
    ).fetchall()
    for r in mr_rows:
        print(f"  {r['project']}!{r['mr_iid']:<6} {r['pub']}条建议  {r['app']}/{r['pub']} 采纳  {_pct(r['app'], r['pub'])}")

    # Recent feedback
    try:
        fb_rows = conn.execute(
            "SELECT feedback_user, project, mr_iid, comment, created_at"
            " FROM inline_suggestion_feedback ORDER BY id DESC LIMIT 10"
        ).fetchall()
        if fb_rows:
            print("\n用户反馈（最近10条）:")
            for r in fb_rows:
                ts = to_cn_display(r["created_at"])
                print(f"  [{ts}] @{r['feedback_user']} on {r['project']}!{r['mr_iid']}: {r['comment'][:80]}")
    except Exception:
        pass

    conn.close()
    return 0


def cmd_inline_html(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    out = getattr(args, "output", None) or "inline-report.html"
    gitlab_url = (
        getattr(args, "gitlab_url", None)
        or os.environ.get("PR_FEEDBACK_GITLAB_URL")
        or DEFAULT_GITLAB_URL
    ).rstrip("/")
    conn = _inline_connect(db_path)

    pub_total = conn.execute(
        "SELECT COUNT(*) FROM published_suggestions"
    ).fetchone()[0]
    app_total = conn.execute(
        "SELECT COUNT(*) FROM published_suggestions WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    proj_rows = conn.execute(
        "SELECT project,"
        " COUNT(*) AS pub,"
        " SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app"
        " FROM published_suggestions GROUP BY project ORDER BY pub DESC"
    ).fetchall()

    week_rows = conn.execute(
        "SELECT strftime('%Y-W%W', created_at) AS week,"
        " COUNT(*) AS pub,"
        " SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app"
        " FROM published_suggestions GROUP BY week ORDER BY week DESC LIMIT 8"
    ).fetchall()
    week_rows = list(reversed(week_rows))

    # Owner (MR author) is captured directly on published_suggestions.mr_author
    # going forward (see inline_publisher._mr_author). For historical rows
    # saved before that column existed, fall back to review_feedback (populated
    # when the MR author/reviewer runs the review command) keyed by
    # project + mr_iid. Best-effort only — falls back further to a query
    # without the owner column if neither source is available.
    try:
        mr_rows = conn.execute(
            "SELECT ps.project AS project, ps.mr_iid AS mr_iid,"
            " COUNT(*) AS pub,"
            " SUM(CASE WHEN ps.applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app,"
            " MAX(ps.created_at) AS last_at,"
            " MAX(ps.mr_url) AS mr_url,"
            " COALESCE("
            "   (SELECT ps2.mr_author FROM published_suggestions ps2"
            "    WHERE ps2.project = ps.project AND ps2.mr_iid = ps.mr_iid"
            "    AND ps2.mr_author IS NOT NULL AND ps2.mr_author != '' LIMIT 1),"
            "   (SELECT rf.mr_author FROM review_feedback rf"
            "    WHERE rf.project = ps.project AND rf.mr_iid = ps.mr_iid"
            "    AND rf.mr_author IS NOT NULL AND rf.mr_author != ''"
            "    ORDER BY rf.id DESC LIMIT 1)"
            " ) AS owner"
            " FROM published_suggestions ps GROUP BY ps.project, ps.mr_iid"
            " ORDER BY last_at DESC LIMIT 1000"
        ).fetchall()
    except Exception:
        mr_rows = conn.execute(
            "SELECT project, mr_iid,"
            " COUNT(*) AS pub,"
            " SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app,"
            " MAX(created_at) AS last_at,"
            " MAX(mr_url) AS mr_url,"
            " NULL AS owner"
            " FROM published_suggestions GROUP BY project, mr_iid"
            " ORDER BY last_at DESC LIMIT 1000"
        ).fetchall()

    try:
        fb_rows = conn.execute(
            "SELECT feedback_user, project, mr_iid, comment, created_at"
            " FROM inline_suggestion_feedback ORDER BY id DESC LIMIT 50"
        ).fetchall()
    except Exception:
        fb_rows = []

    conn.close()

    html = _render_inline_html_dashboard(
        pub_total=pub_total,
        app_total=app_total,
        proj_rows=proj_rows,
        week_rows=week_rows,
        mr_rows=mr_rows,
        fb_rows=fb_rows,
        gitlab_url=gitlab_url,
    )

    _ensure_parent_dir(out)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\u2705 Inline report saved to {out}")
    return 0


def _render_inline_html_dashboard(
    *,
    pub_total: int,
    app_total: int,
    proj_rows,
    week_rows,
    mr_rows,
    fb_rows,
    gitlab_url: str,
) -> str:
    now = now_cn().strftime("%Y-%m-%d %H:%M")
    overall_pct = (app_total / pub_total * 100) if pub_total else 0.0

    week_labels = [row["week"] for row in week_rows]
    week_values = [
        round((row["app"] / row["pub"] * 100) if row["pub"] else 0, 1) for row in week_rows
    ]

    project_data = [
        {
            "project": str(row["project"] or "(unknown)"),
            "pub": row["pub"],
            "app": row["app"],
            "pct": round((row["app"] / row["pub"] * 100) if row["pub"] else 0, 1),
        }
        for row in proj_rows
    ]
    project_data_json = json.dumps(project_data, ensure_ascii=False).replace("</", "<\\/")

    top_projects = sorted(proj_rows, key=lambda r: r["pub"], reverse=True)[:8]
    dist_labels = [str(r["project"] or "(unknown)") for r in top_projects]
    dist_values = [r["pub"] for r in top_projects]

    def _mr_data():
        data = []
        for r in mr_rows:
            pct = round((r["app"] / r["pub"] * 100) if r["pub"] else 0, 1)
            project = str(r["project"])
            mr_iid = str(r["mr_iid"])
            # Prefer the real MR URL stored at publish time; fall back to a
            # constructed link for older rows saved before mr_url existed.
            stored_url = (r["mr_url"] or "").strip() if "mr_url" in r.keys() else ""
            link = stored_url or (f"{gitlab_url}/{project}/-/merge_requests/{mr_iid}" if project and mr_iid else "")
            owner = (r["owner"] if "owner" in r.keys() else None) or ""
            data.append({
                "mr": f"{project}!{mr_iid}",
                "project": project,
                "pub": r["pub"],
                "app": r["app"],
                "pct": pct,
                "ts": to_cn_display(r["last_at"]) if r["last_at"] else "",
                "owner": owner,
                "link": link,
            })
        return data

    mr_data_json = json.dumps(_mr_data(), ensure_ascii=False).replace("</", "<\\/")

    fb_data = [
        {
            "ts": to_cn_display(row["created_at"]) if row["created_at"] else "",
            "user": str(row["feedback_user"] or ""),
            "mr": f"{row['project'] or ''}!{row['mr_iid'] or ''}",
            "comment": str(row["comment"] or "")[:200],
        }
        for row in fb_rows
    ]
    fb_data_json = json.dumps(fb_data, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Inline Suggestion Analytics</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: rgba(17, 24, 39, 0.82);
      --panel-2: rgba(30, 41, 59, 0.88);
      --text: #e5eefb;
      --muted: #94a3b8;
      --line: rgba(148, 163, 184, 0.18);
      --accent: #60a5fa;
      --accent-2: #a78bfa;
      --green: #34d399;
      --yellow: #fbbf24;
      --red: #f87171;
      --shadow: 0 20px 40px rgba(0, 0, 0, 0.35);
      --radius: 22px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(96,165,250,.24), transparent 28%),
        radial-gradient(circle at top right, rgba(167,139,250,.18), transparent 24%),
        linear-gradient(180deg, #0b1020 0%, #111827 100%);
    }}
    a {{ color: inherit; text-decoration: none; }}
    .container {{ max-width: 1280px; margin: 0 auto; padding: 32px 24px 48px; }}
    .hero {{
      display: flex; justify-content: space-between; gap: 24px; align-items: flex-start;
      padding: 28px 30px; border: 1px solid var(--line); border-radius: 28px;
      background: linear-gradient(135deg, rgba(15,23,42,.88), rgba(30,41,59,.72));
      box-shadow: var(--shadow); backdrop-filter: blur(16px);
    }}
    .hero h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .hero p {{ margin: 0; color: var(--muted); }}
    .stamp {{ text-align: right; color: var(--muted); font-size: 14px; }}
    .metrics {{
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 18px;
      margin-top: 24px;
    }}
    .card {{
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
      box-shadow: var(--shadow); padding: 22px; backdrop-filter: blur(14px);
    }}
    .metric-value {{ font-size: 34px; font-weight: 700; margin-top: 10px; }}
    .metric-label, .section-subtitle, .muted {{ color: var(--muted); }}
    .grid-2 {{
      display: grid; grid-template-columns: 1.15fr 1fr; gap: 18px; margin-top: 18px;
    }}
    .section-title {{
      margin: 0 0 6px; font-size: 18px; font-weight: 700;
    }}
    .chart-wrap {{ height: 320px; margin-top: 18px; }}
    table {{
      width: 100%; border-collapse: collapse; margin-top: 16px; overflow: hidden;
      border-radius: 14px;
    }}
    th, td {{
      text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--line);
      font-size: 14px;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    tr:last-child td {{ border-bottom: none; }}
    .mini-badge {{
      display: inline-flex; align-items: center; justify-content: center; min-width: 56px;
      padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700;
      color: white;
    }}
    .pct-low {{ background: linear-gradient(135deg, #ef4444, #f87171); }}
    .pct-mid {{ background: linear-gradient(135deg, #f59e0b, #fbbf24); color: #1f2937; }}
    .pct-high {{ background: linear-gradient(135deg, #10b981, #34d399); }}
    .comment-cell {{
      max-width: 420px; white-space: normal; word-break: break-word; line-height: 1.55;
    }}
    .link-cell a {{ color: #93c5fd; }}
    .filters {{
      display: grid; grid-template-columns: 1.2fr repeat(3, minmax(0, 180px)); gap: 12px;
      margin-top: 16px;
    }}
    .field {{ display: flex; flex-direction: column; gap: 8px; }}
    .field label {{ color: var(--muted); font-size: 13px; }}
    .field input, .field select {{
      width: 100%; border: 1px solid var(--line); background: rgba(15, 23, 42, 0.85);
      color: var(--text); padding: 12px 14px; border-radius: 12px; outline: none;
    }}
    .field input::placeholder {{ color: #64748b; }}
    .toolbar {{
      display: flex; justify-content: space-between; align-items: center; gap: 12px;
      margin-top: 16px; color: var(--muted); font-size: 14px;
    }}
    .table-wrap {{
      margin-top: 14px; border: 1px solid var(--line); border-radius: 16px; overflow: hidden;
      background: rgba(2, 6, 23, 0.28);
    }}
    .table-wrap table {{ margin-top: 0; }}
    .table-wrap td {{ vertical-align: top; }}
    .pager {{
      display: flex; justify-content: space-between; align-items: center; gap: 12px;
      margin-top: 16px; flex-wrap: wrap;
    }}
    .pager-actions {{ display: flex; gap: 10px; align-items: center; }}
    .btn {{
      border: 1px solid var(--line); background: rgba(15, 23, 42, 0.9); color: var(--text);
      padding: 10px 14px; border-radius: 12px; cursor: pointer;
    }}
    .btn:disabled {{ opacity: .45; cursor: not-allowed; }}
    @media (max-width: 980px) {{
      .metrics, .grid-2 {{ grid-template-columns: 1fr; }}
      .hero {{ flex-direction: column; }}
      .stamp {{ text-align: left; }}
      .filters {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 720px) {{
      .filters {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <section class="hero">
      <div>
        <h1>Inline Suggestion Analytics</h1>
        <p>A polished static dashboard generated from SQLite feedback data.</p>
      </div>
      <div class="stamp">
        <div>Generated at</div>
        <strong>{escape(now)}</strong>
      </div>
    </section>

    <section class="metrics">
      <div class="card">
        <div class="metric-label">Published suggestions</div>
        <div class="metric-value">{pub_total}</div>
      </div>
      <div class="card">
        <div class="metric-label">Applied</div>
        <div class="metric-value">{app_total}</div>
      </div>
      <div class="card">
        <div class="metric-label">Overall adoption rate</div>
        <div class="metric-value">{overall_pct:.1f}%</div>
      </div>
      <div class="card">
        <div class="metric-label">User feedback</div>
        <div class="metric-value">{len(fb_rows)}</div>
      </div>
    </section>

    <section class="grid-2">
      <div class="card">
        <h2 class="section-title">Top projects by suggestion count</h2>
        <div class="section-subtitle">Published suggestions per project (top 8).</div>
        <div class="chart-wrap"><canvas id="projectChart"></canvas></div>
      </div>
      <div class="card">
        <h2 class="section-title">Weekly adoption trend</h2>
        <div class="section-subtitle">Adoption rate changes over time.</div>
        <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
      </div>
    </section>

    <section class="card" style="margin-top: 18px;">
      <h2 class="section-title">Project summary table</h2>
      <div class="section-subtitle">Published / applied counts and adoption rate by project.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Project</th>
              <th>Published</th>
              <th>Applied</th>
              <th>Adoption rate</th>
            </tr>
          </thead>
          <tbody id="projectTableBody">
            <tr><td colspan="4" class="muted">Loading...</td></tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <div id="projectPageInfo" class="muted">Page 1</div>
        <div class="pager-actions">
          <button id="projectPrevPage" class="btn" type="button">Previous</button>
          <button id="projectNextPage" class="btn" type="button">Next</button>
        </div>
      </div>
    </section>

    <section class="card" style="margin-top: 18px;">
      <h2 class="section-title">MR explorer</h2>
      <div class="section-subtitle">Filter, search, and page through per-MR adoption details.</div>

      <div class="filters">
        <div class="field">
          <label for="searchInput">Search</label>
          <input id="searchInput" type="text" placeholder="Search MR / project" />
        </div>
        <div class="field">
          <label for="projectFilter">Project</label>
          <select id="projectFilter">
            <option value="">All projects</option>
          </select>
        </div>
        <div class="field">
          <label for="sortFilter">Sort</label>
          <select id="sortFilter">
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="pct-desc">Highest adoption</option>
            <option value="pct-asc">Lowest adoption</option>
          </select>
        </div>
      </div>

      <div class="toolbar">
        <div id="resultSummary">Loading MR rows...</div>
        <div class="field" style="width: 120px;">
          <label for="pageSize">Per page</label>
          <select id="pageSize">
            <option value="10" selected>10</option>
            <option value="15">15</option>
            <option value="30">30</option>
            <option value="50">50</option>
          </select>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>MR</th>
              <th>Published</th>
              <th>Applied</th>
              <th>Adoption rate</th>
              <th>Time</th>
              <th>Owner</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody id="mrTableBody">
            <tr><td colspan="7" class="muted">Loading...</td></tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <div id="pageInfo" class="muted">Page 1</div>
        <div class="pager-actions">
          <button id="prevPage" class="btn" type="button">Previous</button>
          <button id="nextPage" class="btn" type="button">Next</button>
        </div>
      </div>
    </section>

    <section class="card" style="margin-top: 18px;">
      <h2 class="section-title">User feedback</h2>
      <div class="section-subtitle">Latest replies left by users on suggestion threads (up to 50).</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>User</th>
              <th>MR</th>
              <th>Comment</th>
            </tr>
          </thead>
          <tbody id="feedbackTableBody">
            <tr><td colspan="4" class="muted">Loading...</td></tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <div id="feedbackPageInfo" class="muted">Page 1</div>
        <div class="pager-actions">
          <button id="feedbackPrevPage" class="btn" type="button">Previous</button>
          <button id="feedbackNextPage" class="btn" type="button">Next</button>
        </div>
      </div>
    </section>
  </div>

  <script>
    const distLabels = {json.dumps(dist_labels, ensure_ascii=False)};
    const distValues = {json.dumps(dist_values)};
    const weekLabels = {json.dumps(week_labels, ensure_ascii=False)};
    const weekValues = {json.dumps(week_values)};
    const mrRows = {mr_data_json};
    const projectRows = {project_data_json};
    const feedbackRows = {fb_data_json};

    const commonLegend = {{
      labels: {{ color: '#cbd5e1', boxWidth: 12, usePointStyle: true }}
    }};
    const commonTicks = {{
      color: '#94a3b8',
      grid: {{ color: 'rgba(148, 163, 184, 0.12)' }}
    }};

    new Chart(document.getElementById('projectChart'), {{
      type: 'bar',
      data: {{
        labels: distLabels,
        datasets: [{{
          label: 'Published suggestions',
          data: distValues,
          backgroundColor: '#60a5fa',
          borderRadius: 6
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          x: commonTicks,
          y: commonTicks
        }}
      }}
    }});

    new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{
        labels: weekLabels,
        datasets: [{{
          label: 'Adoption rate %',
          data: weekValues,
          borderColor: '#a78bfa',
          backgroundColor: 'rgba(167, 139, 250, 0.18)',
          fill: true,
          tension: 0.35,
          pointRadius: 4,
          pointHoverRadius: 5
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: commonLegend }},
        scales: {{
          x: commonTicks,
          y: {{ ...commonTicks, min: 0, max: 100 }}
        }}
      }}
    }});

    const projectFilter = document.getElementById('projectFilter');
    const sortFilter = document.getElementById('sortFilter');
    const searchInput = document.getElementById('searchInput');
    const pageSizeSelect = document.getElementById('pageSize');
    const tableBody = document.getElementById('mrTableBody');
    const resultSummary = document.getElementById('resultSummary');
    const pageInfo = document.getElementById('pageInfo');
    const prevPageBtn = document.getElementById('prevPage');
    const nextPageBtn = document.getElementById('nextPage');

    let currentPage = 1;

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    function pctBadgeClass(pct) {{
      if (pct < 20) return 'pct-low';
      if (pct < 50) return 'pct-mid';
      return 'pct-high';
    }}

    function pctBadge(pct) {{
      return `<span class="mini-badge ${{pctBadgeClass(pct)}}">${{pct}}%</span>`;
    }}

    function renderProjectOptions() {{
      const projects = [...new Set(mrRows.map((row) => row.project || '(unknown)'))].sort();
      for (const project of projects) {{
        const option = document.createElement('option');
        option.value = project;
        option.textContent = project;
        projectFilter.appendChild(option);
      }}
    }}

    function getFilteredRows() {{
      const project = projectFilter.value;
      const search = searchInput.value.trim().toLowerCase();
      const sort = sortFilter.value;

      let rows = mrRows.filter((row) => {{
        if (project) {{
          const rowProject = row.project || '(unknown)';
          if (rowProject !== project) return false;
        }}
        if (search) {{
          const haystack = [row.mr, row.project].join(' ').toLowerCase();
          if (!haystack.includes(search)) return false;
        }}
        return true;
      }});

      rows.sort((a, b) => {{
        if (sort === 'oldest') return a.ts.localeCompare(b.ts);
        if (sort === 'pct-desc') return b.pct - a.pct;
        if (sort === 'pct-asc') return a.pct - b.pct;
        return b.ts.localeCompare(a.ts);
      }});

      return rows;
    }}

    function renderTable() {{
      const rows = getFilteredRows();
      const pageSize = Number(pageSizeSelect.value);
      const totalRows = rows.length;
      const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
      currentPage = Math.min(currentPage, totalPages);
      const start = (currentPage - 1) * pageSize;
      const pageRows = rows.slice(start, start + pageSize);

      if (!pageRows.length) {{
        tableBody.innerHTML = '<tr><td colspan="7" class="muted">No matching MR.</td></tr>';
      }} else {{
        tableBody.innerHTML = pageRows.map((row) => {{
          const mr = escapeHtml(row.mr);
          const ts = escapeHtml(row.ts);
          const owner = row.owner ? escapeHtml(row.owner) : '<span class="muted">—</span>';
          const link = row.link
            ? `<a href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">查看</a>`
            : '<span class="muted">—</span>';
          return `
            <tr>
              <td>${{mr}}</td>
              <td>${{row.pub}}</td>
              <td>${{row.app}}</td>
              <td>${{pctBadge(row.pct)}}</td>
              <td>${{ts}}</td>
              <td>${{owner}}</td>
              <td class="link-cell">${{link}}</td>
            </tr>
          `;
        }}).join('');
      }}

      const startDisplay = totalRows ? start + 1 : 0;
      const endDisplay = Math.min(start + pageSize, totalRows);
      resultSummary.textContent = `Showing ${{startDisplay}}-${{endDisplay}} of ${{totalRows}} MR rows`;
      pageInfo.textContent = `Page ${{currentPage}} / ${{totalPages}}`;
      prevPageBtn.disabled = currentPage <= 1;
      nextPageBtn.disabled = currentPage >= totalPages;
    }}

    function resetToFirstPage() {{
      currentPage = 1;
      renderTable();
    }}

    [projectFilter, sortFilter].forEach((el) => {{
      el.addEventListener('change', resetToFirstPage);
    }});
    searchInput.addEventListener('input', resetToFirstPage);
    pageSizeSelect.addEventListener('change', resetToFirstPage);
    prevPageBtn.addEventListener('click', () => {{
      if (currentPage > 1) {{
        currentPage -= 1;
        renderTable();
      }}
    }});
    nextPageBtn.addEventListener('click', () => {{
      currentPage += 1;
      renderTable();
    }});

    renderProjectOptions();
    renderTable();

    function createPager({{ rows, tbodyId, pageInfoId, prevBtnId, nextBtnId, pageSize, emptyColspan, renderRow, emptyText }}) {{
      let page = 1;
      const tbody = document.getElementById(tbodyId);
      const pageInfoEl = document.getElementById(pageInfoId);
      const prevBtn = document.getElementById(prevBtnId);
      const nextBtn = document.getElementById(nextBtnId);

      function render() {{
        const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
        page = Math.min(page, totalPages);
        const start = (page - 1) * pageSize;
        const pageRows = rows.slice(start, start + pageSize);
        tbody.innerHTML = pageRows.length
          ? pageRows.map(renderRow).join('')
          : `<tr><td colspan="${{emptyColspan}}" class="muted">${{emptyText}}</td></tr>`;
        pageInfoEl.textContent = `Page ${{page}} / ${{totalPages}} (${{rows.length}} rows)`;
        prevBtn.disabled = page <= 1;
        nextBtn.disabled = page >= totalPages;
      }}

      prevBtn.addEventListener('click', () => {{
        if (page > 1) {{ page -= 1; render(); }}
      }});
      nextBtn.addEventListener('click', () => {{
        page += 1; render();
      }});
      render();
    }}

    createPager({{
      rows: projectRows,
      tbodyId: 'projectTableBody',
      pageInfoId: 'projectPageInfo',
      prevBtnId: 'projectPrevPage',
      nextBtnId: 'projectNextPage',
      pageSize: 10,
      emptyColspan: 4,
      emptyText: 'No project data',
      renderRow: (row) => `
        <tr>
          <td>${{escapeHtml(row.project)}}</td>
          <td>${{row.pub}}</td>
          <td>${{row.app}}</td>
          <td>${{pctBadge(row.pct)}}</td>
        </tr>
      `,
    }});

    createPager({{
      rows: feedbackRows,
      tbodyId: 'feedbackTableBody',
      pageInfoId: 'feedbackPageInfo',
      prevBtnId: 'feedbackPrevPage',
      nextBtnId: 'feedbackNextPage',
      pageSize: 10,
      emptyColspan: 4,
      emptyText: 'No feedback yet',
      renderRow: (row) => `
        <tr>
          <td>${{escapeHtml(row.ts)}}</td>
          <td>@${{escapeHtml(row.user)}}</td>
          <td>${{escapeHtml(row.mr)}}</td>
          <td class="comment-cell">${{escapeHtml(row.comment) || '<span class="muted">—</span>'}}</td>
        </tr>
      `,
    }});
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
