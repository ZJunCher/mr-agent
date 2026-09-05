# Notices

## Upstream project

MR-Agent is based on [PR-Agent](https://github.com/qodo-ai/pr-agent), an open-source project distributed under the
GNU Affero General Public License v3.0.

The initial commit imports the complete source tree from upstream commit
`e9e7b22a9162c4c7f1ae43c4e0248d85c9279611`. This repository uses a compact history; the original commits and author records remain
available in the [PR-Agent upstream repository](https://github.com/qodo-ai/pr-agent).

Changes after the import commit were reconstructed from a sanitized code snapshot and are maintained independently.

## Main extensions

This repository adds or substantially extends:

- GitLab MR review governance, evidence checks, suggestion feedback, and project-level review Skills;
- Redis-backed distributed execution with leases, fencing, idempotency, retries, and recovery;
- Feishu notifications and interactive repair workflows;
- CI failure diagnosis, controlled code repair, exact-SHA pipeline verification, and rollback safeguards;
- verified Repair Memory with retrieval and audit records.

MR-Agent is an independent community fork. It is not an official Qodo product, and Qodo does not maintain or endorse the
extensions listed above. PR-Agent, Qodo, GitHub, GitLab, Feishu, and other names belong to their respective owners.

## License

The repository is distributed under the [GNU Affero General Public License v3.0](LICENSE). Retain the license and notices when
redistributing modified versions. Third-party dependencies remain subject to their own licenses.
