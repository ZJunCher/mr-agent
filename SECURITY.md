# Security Policy

## Supported code

Security fixes target the latest commit on the default branch. This repository does not operate a hosted MR-Agent service.
Deployment operators are responsible for updating their own instances and rotating provider credentials.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository when it is available. If private reporting is not
enabled, open a minimal issue asking the maintainer for a private contact channel. Do not include exploit details, credentials,
private repository content, CI logs, or personal data in a public issue.

A useful report includes the affected revision, deployment mode, impact, reproduction conditions, and a proposed mitigation when
known. Remove tokens, internal URLs, repository names, and user identifiers from logs before attaching them.

## Credential handling

- Pass Git provider, model provider, Redis, and Feishu credentials through environment variables or a secret manager.
- Never bake credentials into container images or commit `.env`, `.secrets.toml`, private keys, logs, or database files.
- Use separate bot accounts with the minimum API scopes required by the enabled commands.
- Protect default branches and require CI validation for Agent-generated changes.
- Revoke and rotate a credential immediately if it appears in Git history, logs, screenshots, or an image layer.

The repository includes `scripts/audit_public_release.py` for a focused source and custom-history check. It supplements provider
secret scanning and manual review; it is not a complete security audit.
