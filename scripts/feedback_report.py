#!/usr/bin/env python3
"""Quick summary report — delegates to pr_agent.feedback.report.

Usage:
    python scripts/feedback_report.py [path-to-sqlite-db]
    python scripts/feedback_report.py summary --days 30
    python scripts/feedback_report.py projects --sort-by avg
    python scripts/feedback_report.py html -o feedback-report.html
"""

import os
import sys

# Ensure the repo root is on the path so imports work.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pr_agent.feedback.report import main

if __name__ == "__main__":
    # If the first argument looks like a file path (not a subcommand), treat as --db
    args = list(sys.argv[1:])
    if args and not args[0].startswith("-") and args[0] not in (
        "sync", "summary", "projects", "low-scores", "comments", "export", "html", "help",
    ):
        # Bare path -> translate to "summary --db <path> --days 0"
        db_path = args.pop(0)
        args = ["summary", "--db", db_path, "--days", "0"] + args
    sys.exit(main(args))
