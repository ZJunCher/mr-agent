#!/usr/bin/env python3
"""Detect and unlock MRs stuck on the inline-suggestion attention gate.

The inline-suggestion gate marks an MR's head commit with a ``pending`` commit
status (context = ``PR_INLINE_SUGGESTION_GATE__GATE_STATUS_CONTEXT``) to block
merge until every published inline suggestion has been applied or resolved.
If the gate is later disabled, or pr-agent itself is down and can no longer
process webhooks, those already-pending MRs would stay blocked. This tool
scans open MRs, finds ones still ``pending`` on our context, and (optionally)
flips them to ``success`` to release them.

It only ever sets ``success`` (unlock). It can never block a merge.

Auth/config come from environment variables (matching the running container):
    GITLAB__URL                                       e.g. https://gitlab.example.com
    GITLAB__PERSONAL_ACCESS_TOKEN                      a token with api scope
    PR_INLINE_SUGGESTION_GATE__GATE_STATUS_CONTEXT     gate context name (default pr-agent/inline-suggestions)

Usage (inside the pr-agent container, where env vars are set):
    python scripts/inline_gate_sweep.py                 # dry-run, all member projects
    python scripts/inline_gate_sweep.py --apply         # unlock all stuck MRs
    python scripts/inline_gate_sweep.py --projects cook chogori --apply
    python scripts/inline_gate_sweep.py --all-projects --apply   # scan every visible project
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class GitLab:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.hdr = {"PRIVATE-TOKEN": token}

    def _get(self, path: str):
        url = f"{self.base}/api/v4/{path}"
        req = urllib.request.Request(url, headers=self.hdr)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), r.headers

    def get_all(self, path: str):
        """GET with pagination (page/per_page)."""
        items = []
        page = 1
        sep = "&" if "?" in path else "?"
        while True:
            data, headers = self._get(f"{path}{sep}per_page=100&page={page}")
            if not isinstance(data, list):
                return data
            items.extend(data)
            nxt = headers.get("X-Next-Page")
            if not nxt:
                break
            page = int(nxt)
        return items

    def post(self, path: str, payload: dict):
        url = f"{self.base}/api/v4/{path}"
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=data, headers=self.hdr, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())


def list_projects(gl: GitLab, all_projects: bool):
    scope = "projects?membership=true" if not all_projects else "projects?simple=true"
    projects = gl.get_all(scope)
    return [p["path_with_namespace"] for p in projects]


def stuck_mrs_for_project(gl: GitLab, project: str, ctx: str):
    enc = urllib.parse.quote(project, safe="")
    try:
        mrs = gl.get_all(f"projects/{enc}/merge_requests?state=opened")
    except Exception as e:
        print(f"  ! list MR failed for {project}: {e}", file=sys.stderr)
        return []
    out = []
    for mr in mrs:
        iid, sha = mr["iid"], mr["sha"]
        try:
            statuses = gl.get_all(f"projects/{enc}/repository/commits/{sha}/statuses")
        except Exception:
            continue
        ours = [s for s in statuses if s.get("name") == ctx or s.get("description") == ctx]
        if not ours:
            continue
        # GitLab keeps full status history per context; the effective state is the
        # most recent entry. Sort by created_at then id so we never act on a stale
        # 'pending' that was already superseded by a later 'success'.
        latest = max(ours, key=lambda s: (str(s.get("created_at", "")), int(s.get("id", 0))))
        if latest.get("status") == "pending":
            out.append((iid, sha, mr.get("title", "")[:60]))
    return out


def unlock(gl: GitLab, project: str, sha: str, ctx: str):
    enc = urllib.parse.quote(project, safe="")
    gl.post(f"projects/{enc}/statuses/{sha}", {"state": "success", "context": ctx})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Detect/unlock MRs stuck on the inline-suggestion gate.")
    ap.add_argument("--apply", action="store_true", help="actually unlock (default: dry-run)")
    ap.add_argument("--projects", nargs="*", help="explicit project paths to scan")
    ap.add_argument("--all-projects", action="store_true", help="scan every visible project")
    args = ap.parse_args(argv)

    base = _env("GITLAB__URL")
    token = _env("GITLAB__PERSONAL_ACCESS_TOKEN")
    ctx = _env("PR_INLINE_SUGGESTION_GATE__GATE_STATUS_CONTEXT", "pr-agent/inline-suggestions")
    if not base or not token:
        print("ERROR: GITLAB__URL and GITLAB__PERSONAL_ACCESS_TOKEN must be set.", file=sys.stderr)
        return 2

    gl = GitLab(base, token)
    print(f"gate context = {ctx!r}")
    print(f"mode         = {'APPLY (will unlock)' if args.apply else 'DRY-RUN (no changes)'}\n")

    if args.projects:
        projects = args.projects
    else:
        projects = list_projects(gl, args.all_projects)
    print(f"scanning {len(projects)} project(s)...\n")

    total = 0
    for p in projects:
        stuck = stuck_mrs_for_project(gl, p, ctx)
        for iid, sha, title in stuck:
            total += 1
            tag = "UNLOCKING" if args.apply else "STUCK"
            print(f"[{tag}] {p} !{iid}  {title}")
            if args.apply:
                try:
                    unlock(gl, p, sha, ctx)
                    print(f"          -> set success on {sha[:10]}")
                except Exception as e:
                    print(f"          ! failed: {e}", file=sys.stderr)

    print(f"\n{'unlocked' if args.apply else 'found'} {total} stuck MR(s).")
    if total and not args.apply:
        print("re-run with --apply to unlock them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
