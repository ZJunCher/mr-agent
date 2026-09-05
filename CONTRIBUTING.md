# Contributing to MR-Agent

Contributions are welcome when they keep Agent actions observable, bounded, and verifiable.

## Development setup

MR-Agent requires Python 3.12 or newer.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Use environment variables or an ignored `pr_agent/settings/.secrets.toml` for local credentials. Do not edit
`pr_agent/settings/configuration.toml` to add deployment secrets or organization-specific endpoints.

## Making a change

1. Create a focused branch such as `feature/<name>` or `fix/<issue>`.
2. Follow the existing Python style: 120-character lines, Ruff import ordering, and concise English docstrings.
3. Add tests under the closest matching directory. Unit behavior belongs in `tests/unittest/`; external integrations belong in
   `tests/integration/` or `tests/e2e_tests/`.
4. Run the smallest relevant tests while developing, then run the full unit suite before submitting.
5. Update README or user documentation when behavior, configuration, or deployment changes.

Example commands:

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_fix_json_escape_char.py -q
PYTHONPATH=. ./.venv/bin/pytest tests/unittest -q
./.venv/bin/ruff check pr_agent ut_agent tests scripts
PYTHONPATH=. ./.venv/bin/python scripts/audit_public_release.py --root .
```

External end-to-end tests require provider credentials and test repositories. Do not run them against production projects or
include their logs in a pull request.

## Pull requests

Keep each pull request small enough to review. Explain the failure mode or user need, the chosen behavior, and the evidence used
to verify it. Agent workflow changes should cover stale events, retries, duplicate delivery, worker takeover, and terminal-state
handling when those cases apply.

Use Conventional Commit-style subjects, for example `fix: reject stale pipeline results`. Never commit API keys, access tokens,
private URLs, personal data, generated databases, or runtime workspaces.
