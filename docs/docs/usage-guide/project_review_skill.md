# Project Review Skill

## Summary

Project Review Skill lets each GitLab project own declarative review rules for `/review` and `/improve` without copying or replacing PR-Agent's global prompts. A project opts in by committing one validated manifest to its target branch; PR-Agent pins that manifest and its referenced Markdown files to the exact target-branch SHA used by the command.

The Skill is project context, not executable code. It cannot add tools, run commands, change output schemas, weaken global safety rules, or grant the model additional repository permissions.

## Source of truth

| Item | Path |
|---|---|
| Runtime schema, selection, hashing | `pr_agent/suggestions/project_prompt_rules.py` |
| GitLab target-SHA reader | `pr_agent/git_providers/gitlab_provider.py` |
| `/review` integration | `pr_agent/tools/pr_reviewer.py` |
| `/improve` integration | `pr_agent/tools/pr_code_suggestions.py` |
| Usage and feedback storage | `pr_agent/feedback/store.py`, `pr_agent/suggestions/store.py` |
| Cross-project evolution | `pr_agent/suggestions/prompt_evolution/project_skill_runner.py` |
| Static evolution guard | `pr_agent/suggestions/prompt_evolution/validator.py` |
| SkillOpt batching and score gate | `pr_agent/suggestions/prompt_evolution/project_skill_optimizer.py` |
| Pinned-model paired replay | `pr_agent/suggestions/prompt_evolution/project_skill_evaluator.py` |
| Optimization and rejected-edit storage | `pr_agent/suggestions/prompt_evolution/store.py` |
| Draft MR publisher | `pr_agent/suggestions/prompt_evolution/gitlab_publisher.py` |
| Runtime configuration | `pr_agent/settings/configuration.toml` |
| Example manifest | `pr_agent/settings/project_review_skill.example.toml` |
| Unit tests | `tests/unittest/test_project_prompt_rules.py`, `tests/unittest/test_project_skill_prompt_integration.py` |

## Responsibility

Project Review Skill is responsible for:

- expressing project-specific review expectations;
- selecting rules by command, language, changed path, and exclusion path;
- loading explicitly referenced project facts under a fixed character budget;
- providing immutable target SHA, Skill hash, selected rule IDs, matched files, and Reference hashes;
- connecting real suggestion outcomes and review feedback to the Skill version that was actually used;
- proposing evidence-backed Skill changes through project-owned Draft MRs.

It is not responsible for:

- replacing global prompts or their output schemas;
- executing repository scripts, tests, tools, or shell commands;
- creating a project's initial Skill;
- changing `references/*.md` automatically;
- approving or merging an evolution MR;
- defining CODEOWNERS or project permissions.

## Opt-in layout

The project owner creates this fixed structure in the project's default or reviewed target branch:

```text
.pr_agent/skills/review/
├── skill.toml
└── references/
    ├── architecture.md
    └── api-compatibility.md
```

Only `skill.toml` is required. The absence of that file means the project has not opted in; PR-Agent keeps its existing global behavior.

PR-Agent never creates the initial file. This makes repository ownership explicit and prevents feedback from an arbitrary project from granting the bot a new writable policy surface.

## Manifest schema

```toml
schema_version = 1
name = "example-review"
project = "example-group/example-project"
description = "Cook project review rules"

[[rules]]
id = "api-compatibility"
targets = ["review", "improve"]
languages = ["cpp"]
paths = ["src/**"]
exclude_paths = ["src/generated/**"]
instruction = "Public API changes must preserve backward compatibility."
references = ["references/api-compatibility.md"]

[[rules]]
id = "realtime-no-blocking-io"
targets = ["review", "improve"]
paths = ["src/realtime/**"]
instruction = "Realtime threads must not perform blocking IO."
```

### Top-level fields

| Field | Required | Contract |
|---|---:|---|
| `schema_version` | yes | Must be integer `1`. |
| `project` | yes | Exact GitLab `path_with_namespace`, for example `example-group/example-project`. |
| `name` | no | Human-readable name, at most 200 characters. |
| `description` | no | Purpose/ownership summary, at most 2,000 characters. |
| `rules` | no | Array of at most 50 rule objects. |

Unknown top-level fields are rejected. This prevents a misspelled or future-looking field from silently appearing to work.

### Rule fields

| Field | Required | Contract |
|---|---:|---|
| `id` | yes | Stable unique identifier using letters, numbers, `.`, `_`, or `-`; at most 128 characters. |
| `targets` | yes | Non-empty unique list containing only `review` and/or `improve`. |
| `instruction` | yes | Independent review requirement, at most 2,000 characters. |
| `languages` | no | Unique list containing `python` and/or `cpp`; omitted means all languages. |
| `paths` | no | Include globs; omitted means the whole project. |
| `exclude_paths` | no | Exclusion globs evaluated after includes. |
| `references` | no | Markdown paths below `references/`, relative to the Skill directory. |

Unknown rule fields are rejected. In particular, `script`, `command`, `validation`, `tool`, and similar executable fields are not part of the schema.

### Glob semantics

- `*` matches characters inside one path segment: `src/*` matches `src/a.py`, but not `src/pkg/a.py`.
- `**` crosses directory boundaries: `src/**` matches both `src/a.py` and `src/pkg/a.py`.
- `?` matches one non-separator character.
- Paths are repository-relative POSIX paths. Absolute paths, empty segments, `.` and `..` are rejected.
- A file must match at least one `paths` pattern when `paths` is present.
- A matching `exclude_paths` pattern always wins.

Example: a rule with `paths = ["src/realtime/**"]` and `exclude_paths = ["src/realtime/generated/**"]` applies to `src/realtime/loop.cc`, but not `src/realtime/generated/messages.cc`.

### Reference contract

- A Reference must use `references/<name>.md`.
- Absolute paths, traversal, non-Markdown files, and files outside the fixed directory are rejected.
- References are loaded only when a rule that cites them is selected.
- Manifest and References are read at the same target SHA.
- At most 10 distinct References are selected.
- Loaded Reference text is clipped to a deterministic total budget of 20,000 characters; provenance records clipping and hashes the original content.
- Reference text is labeled as controlled user context. It cannot override system instructions, schemas, safety rules, or tool permissions.

## Runtime call flow

1. PR-Agent identifies the GitLab project, MR target branch, current target-branch head SHA, changed files, and language route.
2. It reads `.pr_agent/skills/review/skill.toml` at that exact SHA, not from the MR source branch.
3. It validates TOML, project identity, schema, sizes, paths, references, IDs, targets, and languages.
4. It selects rules for `review` or `improve`, then filters by language and file path.
5. It loads only References used by selected rules, at the same SHA.
6. It computes an immutable Effective Skill and appends it to the user prompt below an explicit trust-boundary notice.
7. It reuses that Skill session through every internal `/improve` stage: generation, reflection, scenario validation, inline self-check, de-conflict, and Tier-1 repair.
8. It records the target SHA, Skill/Manifest hash, selected rules, matched files, Reference hashes, status, and final prompt bundle.

For a mixed Python/C++ MR, PR-Agent still performs its existing language routing. Each model leg receives only the language-compatible subset of the same target-SHA Skill session.

## Why source-branch edits cannot affect the current MR

Assume the target branch `main` points to SHA `A`, while an MR source branch adds a malicious Skill at SHA `B`.

PR-Agent resolves `main -> A`, then calls the repository Files API with `ref=A`. It never asks for `ref=B` or the source branch name. The malicious Skill can be reviewed as ordinary Diff content, but it affects reviews only after maintainers merge it and it becomes part of a later target-branch SHA.

## Rollout modes

Configure `project_review_skill.rollout_mode` in PR-Agent:

| Mode | Load and validate | Record provenance | Inject `/review` | Inject `/improve` |
|---|---:|---:|---:|---:|
| `disabled` | no | disabled status only | no | no |
| `shadow` | yes | yes | no | no |
| `review_only` | yes | yes | yes | no |
| `review_and_improve` | yes | yes | yes | yes |

The repository default is `review_and_improve`. This remains opt-in per project because a valid target-branch `skill.toml` is still required.

An unknown rollout value is treated as `disabled`.

## Failure behavior

| Status | Meaning | Runtime behavior | Evolution evidence |
|---|---|---|---|
| `loaded` | Manifest and selected References are valid. | Selected context may be injected according to rollout mode. | Eligible when a rule was selected. |
| `missing` | Fixed manifest does not exist. | Continue with global prompts. | Not eligible. |
| `invalid` | Schema, path, size, project identity, or Reference validation failed. | Ignore the entire Skill and continue globally. | Not eligible. |
| `unavailable` | GitLab/API read failed after the command's normal error handling. | Continue with global prompts and log the failure. | Not eligible. |
| `disabled` | Rollout mode is disabled. | Do not load or inject. | Not eligible. |

Fail-open here means review availability is preserved by falling back to the trusted global prompt. It does not mean invalid project rules are partially accepted.

## Provenance and feedback

Published `/improve` suggestions persist these fields in `published_suggestions`:

- `global_prompt_set_hash`, legacy-compatible `project_rules_hash`, and final `prompt_bundle_hash`;
- explicit `project_skill_hash`, `project_skill_manifest_hash`, and `project_skill_target_sha`;
- load status, selected rule IDs, matched files, and Reference hashes.

`/review` stores the same Skill identity in `project_skill_usages` under the hidden `review_id`. A later `/feedback` rating joins to that immutable usage record. Ratings 1-2 become negative project evidence, ratings 4-5 become positive control evidence, and rating 3 remains neutral/pending. Runs with no selected rule or a non-loaded Skill are excluded from Skill evolution.

Evidence from different Skill/Prompt bundles is not mixed. A target Manifest change produces a new hash and supersedes proposals derived from the old version.

## Self-evolution lifecycle

1. The weekly scheduler freezes suggestion outcomes and joined review feedback at one watermark.
2. It groups semantically equivalent evidence and applies deterministic thresholds.
3. Global candidates stay in MR-Agent; project candidates are grouped by owning GitLab project.
4. For each project candidate, PR-Agent opens that project's default branch, resolves its head SHA, and verifies that the fixed Manifest already exists.
5. The recorded evidence Manifest hash must equal the current target Manifest hash. A mismatch becomes `SUPERSEDED` and no model or write is allowed.
6. PR-Agent deterministically splits candidate evidence by MR: at least two training MRs and one hidden selection MR. A single MR can never appear on both sides.
7. It adds current-version accepted cases from other MRs as hidden controls. Selection cases never enter the generator Prompt or rejected-edit summaries.
8. The generator sees only training evidence, the current `skill.toml`, and bounded summaries of previously rejected edits for the same base Skill hash.
9. The generator may propose only a complete replacement for the existing `skill.toml` linked to exact training evidence IDs.
10. The deterministic validator enforces schema, base hash, one-file whitelist, no deletion, no Reference change, no metadata/target/language change, no path broadening, no exclusion removal, size budgets, secret checks, evidence-language scope, and a semantic edit budget.
11. One fixed replay model evaluates the current and candidate Skill against exactly the same hidden cases. If either half fails, both halves are discarded and restarted on one fallback model.
12. The replay model returns only `emit`, `suppress`, or `revise` per case. It cannot return a score or publish decision.
13. Deterministic code computes weighted overall, accepted-control, and rejected-target scores. The candidate must strictly improve by the configured minimum without regressing either subgroup.
14. A rejected candidate is persisted with its edit signature, split, scores, model and errors. An equivalent edit is rejected on a later run for the same base Skill without repeating the replay.
15. Only a candidate that passes the SkillOpt gate can proceed to the current lease/Fencing assertion and target SHA recheck.
16. PR-Agent creates or reuses `codex/review-skill-evolution/<batch-id>`, one atomic commit, and one Draft MR.
17. The Draft MR is never approved or merged by PR-Agent. Project CODEOWNERS review it.
18. GitLab state reconciliation records `MERGED` or `CLOSED`; a merged target-branch `skill.toml` becomes the new stable best Skill and starts a hash-isolated observation period.

Default project-candidate discovery thresholds are negative weight at least 3, negative ratio at least 70%, and at least two MRs. Publishing additionally requires enough data for two training MRs, one explicit-rejection selection MR, and one accepted control case. An unhandled-only cluster can trigger discovery but cannot pass the hidden validation gate until explicit accepted/rejected evidence exists. Explicit review ratings have higher meaning than unhandled signals.

## SkillOpt integration

MR-Agent implements the controlled optimization semantics described by [Microsoft SkillOpt](https://github.com/microsoft/SkillOpt) and its paper, [SkillOpt: Executive Strategy for Self-Evolving Agent Skills](https://arxiv.org/abs/2605.23904): bounded text edits, a hidden validation gate, rejected-edit feedback, and best-version selection.

The upstream `skillopt` Python package is not installed. It is an MIT-licensed standalone training framework with its own Benchmark adapters, model backends, artifact directories, and optional UI. MR-Agent reuses the algorithmic structure inside its existing LiteLLM, SQLite, Redis, GitLab, and Project Skill governance boundaries.

In MR-Agent:

- the repository target branch is the source of the current stable best Skill;
- the semantic rule-edit budget is the textual learning rate;
- stored paired replay results are the validation history;
- rejected optimization rows are the rejected-edit buffer and slow memory;
- a passing candidate is still only eligible for a Draft MR, not automatic deployment;
- owner approval and merge select the next production best Skill.

### Optimizer configuration

All settings live below `[prompt_evolution]`:

| Setting | Default | Meaning |
|---|---:|---|
| `project_skill_optimizer_enabled` | `true` | Required in production. Disabled optimization cannot publish project Skill changes. |
| `project_skill_optimizer_gate_mode` | `enforce` | `shadow` records the complete gate but publishes no project Skill MR; `enforce` requires a pass. |
| `project_skill_optimizer_edit_budget` | `1` | Maximum changed rule objects in one candidate. |
| `project_skill_optimizer_max_edit_budget` | `3` | Hard configuration ceiling for the edit budget. |
| `project_skill_optimizer_selection_ratio` | `0.25` | Target fraction of candidate MR groups reserved for hidden selection. |
| `project_skill_optimizer_min_train_mrs` | `2` | Minimum distinct training MRs after selection removal. |
| `project_skill_optimizer_min_selection_mrs` | `1` | Minimum explicit-rejection MR groups hidden from generation. |
| `project_skill_optimizer_min_control_cases` | `1` | Minimum current-version accepted cases used as regression controls. |
| `project_skill_optimizer_max_selection_cases` | `20` | Maximum cases sent to each half of paired replay. |
| `project_skill_optimizer_minimum_score_delta` | `0.05` | Required weighted improvement in addition to strict greater-than. |
| `project_skill_optimizer_rejected_buffer_size` | `10` | Maximum recent rejected summaries shown to the generator. |

## 高保真回放门禁

片段级 `emit / suppress / revise` 回放现在只是低成本第一关。候选通过后，系统从真实 `/improve` 运行中保存的 project、MR、base/head SHA 和冻结非代码输入恢复完整 Diff，并让 baseline Skill 与 candidate Skill 分别走同一个生产生成入口。两边使用相同的 Diff chunk 计划、全局 Prompt、模型配置、相关上下文、输出 Schema、解析与评分逻辑；唯一允许变化的是 Project Skill 内容和哈希。

程序生成 `EvaluationConditionManifest`，覆盖 SHA、Diff hash、chunk plan hash、Prompt hash、模型、配置、上下文和 Schema。任一必要条件不同、任一边 Diff coverage 不是 complete、输出匹配有歧义或候选没有严格提分，都会失败关闭。这里不要求固定 seed、多次重复或交替 A/B 顺序。

```toml
[prompt_evolution]
project_skill_high_fidelity_enabled = true
project_skill_high_fidelity_gate_mode = "enforce" # shadow 或 enforce
project_skill_high_fidelity_min_mrs = 1
project_skill_high_fidelity_max_mrs = 10
```

`shadow` 会执行并保存 paired replay，但不创建演进 Draft MR；`enforce` 只有片段门禁和高保真门禁都通过，才进入现有 Draft MR 路径。无论哪种模式都不会自动批准或合并。

## 安全灰度

自动生成的新 Skill 仍然只能进入 Draft MR。Canary 只接受负责人明确配置的、40 或 64 位小写十六进制不可变 commit SHA；不能填写 `main`、分支名或 tag。流量按 `project + mr_iid + head_sha` 的稳定哈希选择，同一个 MR 不会在 stable/canary 之间漂移。

```toml
[prompt_evolution]
project_skill_canary_enabled = false
project_skill_canary_percent = 0
project_skill_canary_approved_ref = ""
```

命中 canary 时只从该不可变 ref 读取同一路径的 `skill.toml`。Manifest 缺失、Schema 非法、ref 非法或读取失败会立即回退目标分支 stable Skill；运行记录中的 `target_sha` 会显示实际使用的版本。关闭 `project_skill_canary_enabled` 即停止新 canary 流量。

高保真审计保存在 `project_skill_optimization_steps.high_fidelity_json`，包括条件哈希、覆盖状态、匹配动作和分项分数；不把完整私有 Diff 写入 Draft MR 描述。

### Optimizer terminal states

| State | Meaning | Publication |
|---|---|---:|
| `INSUFFICIENT_VALIDATION` | MR-isolated training/selection data or accepted controls are insufficient. | no |
| `OPTIMIZATION_REJECTED` | Candidate was statically legal but failed strict score, subgroup, edit-budget, completeness, or rejected-signature gates. | no |
| `DRY_RUN_VALIDATED` | Candidate passed but the run is dry-run or optimizer mode is `shadow`. | no |
| `MR_OPEN` | Candidate passed every optimization and existing Git safety gate and a Draft MR exists. | Draft MR only |

The `project_skill_optimization_steps` table records train/selection/control IDs, split hash, base and candidate hashes, model, scores, edit signature, action and errors. It does not duplicate full hidden code snippets.

## Evolution write boundary

An automatic project evolution MR may change exactly:

```text
.pr_agent/skills/review/skill.toml
```

It may:

- add an evidence-backed rule without References;
- clarify an instruction without shortening it;
- narrow include paths;
- add exclusion paths.

It may not:

- create the initial Skill;
- delete a rule;
- change project metadata, targets, language scope, or Reference lists;
- modify `references/*.md`;
- broaden include paths or remove exclusions;
- add scripts, commands, tools, CI files, or business code;
- push directly to the target branch;
- approve or merge the Draft MR.

The generator, deterministic validator, independent evaluator, and publisher each enforce a separate part of this boundary.

## Concurrency, recovery, and idempotency

- Leases and monotonically increasing Fencing Tokens are scoped by business project.
- A Worker that loses its lease stops before GitLab writes.
- The publisher checks the target branch SHA immediately before creating a commit.
- Batch identity includes ISO week, project, target branch, and dry-run mode.
- Candidate fingerprints include scope, project, semantic cluster, and source bundle hash.
- Existing owned branches, batch commit trailers, and open source-branch MRs are discovered before creation.
- Commit/MR timeouts use read-after-write discovery instead of blind retries.
- MR reconciliation is scoped by both project and MR IID, because different projects can all have an MR `!1`.
- The global evidence watermark advances only after project evolution has no retryable failure and the global run reaches its own terminal state.

## Validation commands

From the MR-Agent repository root:

```bash
PYTHONPATH=. ./.venv/bin/pytest \
  tests/unittest/test_project_prompt_rules.py \
  tests/unittest/test_project_skill_prompt_integration.py \
  tests/unittest/test_project_skill_evolution_runner.py \
  tests/unittest/test_project_skill_evolution_evaluator.py \
  tests/unittest/test_project_skill_high_fidelity_evaluator.py \
  tests/unittest/test_project_skill_rollout.py \
  tests/unittest/test_project_skill_optimizer.py -q
```

Run the wider evolution regression:

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_prompt_evolution_*.py -q
```

## Agent guidance

When modifying this feature:

1. Read the runtime schema and this document before changing prompt templates or provider code.
2. Search for `PROJECT_SKILL_MANIFEST_PATH`, `project_skill_hash`, and `project_rule` to find all trust and persistence boundaries.
3. Keep target-SHA reads, selection, and Reference loading in one `ProjectSkillSession`.
4. Do not move project content into system prompts or add executable Manifest fields.
5. Update SQLite migrations and evidence loading together when provenance changes.
6. Preserve separate publishers and MR reconciliation per target project.
7. Run focused runtime, storage, evaluator, publisher, runner, and full unit tests.
8. Update this guide, the example Manifest, and the design document when the contract changes.

## Related documents

- `docs/superpowers/specs/2026-08-26-project-review-skill-evolution-design.md`
- `docs/superpowers/plans/2026-08-26-project-review-skill-evolution.md`
- `docs/superpowers/specs/2026-08-27-project-skill-skillopt-design.md`
- `docs/superpowers/plans/2026-08-27-project-skill-skillopt.md`
- [Review tool](../tools/review.md)
- [Improve tool](../tools/improve.md)
