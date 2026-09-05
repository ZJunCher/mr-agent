# Native Repair Hybrid Agent

## Purpose

Native Repair Hybrid Agent is the production control path for automatically repairing a failed MR Pipeline when
`repair.backend = "native"`. It combines a deterministic lifecycle workflow, a versioned Planner, a Work Item-constrained ReAct
executor and an independent semantic Verifier.

The architecture is available for `trigger_type=pipeline_failed`. Hermes repair, MR-created test generation and manual triage
keep their existing behavior.

## Responsibilities

The hybrid path is responsible for:

- binding every repair to the project, MR, failed Pipeline and baseline SHA;
- creating and checkpointing a strict RepairPlan before repository tools can run;
- selecting exactly one active Work Item in stable order;
- limiting patch paths to evidence-backed `allowed_paths`;
- requiring complete paginated Diff inspection and project validation;
- requiring a second model route to accept the causal relationship between the failure and the complete Diff;
- unlocking commit/push only when plan, baseline, Diff and verification identities agree;
- restoring plan and verification events after a Worker restart;
- exposing bounded audit fields in the final result.

It does not grant unrestricted shell or filesystem access, weaken remote-SHA/lease/Fencing Token checks, merge the MR, or treat
a local model verdict as final CI success. A new Pipeline for the exact pushed SHA remains the final success proof.

## Runtime Flow

```text
failed Pipeline evidence
        |
        v
Planner -> RepairPlan v1
        |
        v
active Work Item
        |
        v
ReAct: search/read -> optional evidence-backed replan -> patch
        |
        v
complete Diff inspection -> mandatory local validation
        |
        v
independent Verifier
   | pass          | replan             | block
   v               v                    v
next Work Item   RepairPlan vN+1   discard workspace + fail
   |
   v
all Work Items covered -> plan-aware commit gate -> push
        |
        v
exact pushed-SHA Pipeline -> success or next failed snapshot
```

LangGraph contains `agent`, `tools`, `planner` and `verifier` nodes. Planner and Verifier are read-only. Repository reads and
mutations still go through registered tools, so reasoning cannot bypass the policy used by mandatory workflow actions.

## RepairPlan

`ut_agent/repair_plan.py` defines the source-of-truth strict Pydantic Schema. A plan records:

| Field | Meaning |
|---|---|
| `plan_id` | Hash identity of this exact plan version and Work Item content. |
| `lineage_id` | Stable hash for project, MR, baseline, Pipeline and failure digest. |
| `version` | Monotonically increasing version, from 1 through 50. |
| `baseline_sha` | Repository version that every patch and verification must match. |
| `source_pipeline_id` | Failed Pipeline used to create the plan. |
| `source_commit_sha` | Commit identity reported by that Pipeline. |
| `source_failure_digest` | Stable digest of normalized root-cause groups. |
| `evidence_cursor` | Last tool-message sequence consumed by this plan version. |
| `planning_mode` | `model` or `deterministic_fallback`. |
| `planner_error_code` | Bounded reason for a model-to-fallback transition. |
| `work_items` | Ordered, bounded repair units. |

The plan contains at most 20 Work Items. Each item contains at most 30 allowed paths, 10 failure evidence strings and 10 required
checks. Paths must be canonical repository-relative POSIX paths: absolute paths, traversal, empty components and `.git` are
rejected.

The Planner model may propose only a hypothesis for an existing deterministic Work Item ID. It cannot create arbitrary jobs,
change failure evidence, reorder priority, grant paths, alter required checks or change the baseline. Missing, invalid or
incomplete model output produces a conservative `deterministic_fallback` plan from CI root-cause groups.

## Work Item Scheduling

Work Items use stable priority: build, coverage, format, merge check and other. The scheduler chooses the first pending item not
already covered by an accepted verification event. The model cannot select another item in free text.

These Native calls must carry the exact active `work_item_id`:

- `search_repo_tool`;
- `read_repo_file_tool`;
- `request_repair_replan_tool`;
- `apply_format_report_tool`;
- `apply_repo_patch_tool`;
- `inspect_repo_diff_tool`;
- `run_repo_validation_tool`.

A missing or different ID is rejected before execution. Outside Native Pipeline repair, the field remains optional for backward
compatibility.

## Path Discovery and Replanning

An initial diagnostic such as `src/parser.py:10: error` creates `allowed_paths=["src/parser.py"]`. If no safe repository path is
present, `allowed_paths` is empty. The executor may search and read, but cannot patch until a replan adds a path.

`request_repair_replan_tool` accepts the current plan ID, expected version, active Work Item, reason, optional hypothesis,
proposed paths and evidence sequence IDs. It accepts the request only when:

1. plan ID and expected version match the latest plan;
2. the Work Item is still active;
3. every evidence sequence is newer than the plan cursor;
4. every sequence is a successful search/read for the same Work Item;
5. every proposed path appears in those referenced results;
6. every proposed path passes canonical path validation.

A valid request creates version `N+1` in the same lineage, unions evidence-backed paths and moves the cursor past the request.
Old patch/validation evidence therefore cannot satisfy the new plan. Invalid requests append no plan event.

A Verifier `replan` also creates `N+1`, records its reason as the revised hypothesis and moves the evidence boundary past the
current tool facts. A new patch and fresh validation are required.

## Patch Scope

Before `apply_repo_patch_tool` executes, policy parses `---` and `+++` headers from the unified Diff. Every canonical path must
exactly match the active Work Item's `allowed_paths`. Otherwise the executor must collect repository evidence and replan.

This semantic scope is additional to the patch tool's filesystem controls:

- absolute and traversal paths are rejected;
- `.git` and `.gitignore` targets are rejected;
- resolved paths must remain inside the isolated repository;
- `git apply --check` must pass before mutation;
- failed application must leave the pre-call Diff digest unchanged;
- successful application must return a new base SHA, Diff digest and changed-file list.

For a format Work Item, `apply_format_report_tool` downloads the deterministic CI artifact, then routes its unified Diff through
the same Work Item and `apply_repo_patch_tool` policy. It does not directly bypass Native path scope or patch evidence.

## Diff and Local Validation

After a patch, workflow forces `inspect_repo_diff_tool` from line 1. Each page records one base SHA, Diff digest, total-line count,
interval and Work Item. The reducer merges intervals and finds gaps. Commit remains locked until pages cover every line.

`run_repo_validation_tool` always runs `diff_check`, adds Python compilation for changed Python files, and requires the configured
unit-test profile for test/coverage repair. Commands are fixed argv values, run with `shell=False`, inside the repository or a
configured subdirectory, with timeout, bounded output, cancellation and fencing checks. Validation is rejected when it modifies
the worktree, omits a required check, fails, uses another Work Item, or references another base/Diff identity.

Projects needing tests configure `[repair.validation.profiles."<project-id>"]` with non-empty `unit_test_argv`, a
repository-relative `working_directory` and bounded `timeout_seconds`. A missing required profile fails closed.

## Independent Verifier

The Verifier runs only after `evaluate_native_commit()` confirms complete Diff review and all mandatory checks. It reconstructs
the complete Diff only from matching pages and rejects missing, inconsistent or over-budget material.

The model must differ from `state.active_model`. No independent route produces `independent_model_unavailable` and blocks
publication. Strict output contains `verdict`, `causal_alignment`, `scope_compliant`, `evidence_sufficient`,
`covered_work_item_ids`, bounded `reason` and `risks`.

A claimed `pass` is downgraded to `replan` unless all three booleans are true and required coverage is present. Intermediate
verification covers the active item. Final verification covers every executable item against the complete accumulated Diff.

## Commit Gate

`commit_and_push_tool` is accepted only when:

1. the latest plan matches project, MR, failed Pipeline, source commit, failure digest and baseline;
2. no Work Item is pending or blocked;
3. latest Verifier is `pass` for the exact plan ID/version;
4. that event covers every executable Work Item;
5. its baseline and Diff digest match the current validated snapshot;
6. successful patch evidence is newer than the plan cursor;
7. complete Diff and all required checks pass;
8. existing remote SHA, source branch, workspace, lease, Fencing Token, idempotency and round limits pass.

If any Work Item is blocked, deterministic scheduling discards accumulated uncommitted changes and finishes unsuccessfully. It
never publishes a partial plan.

## Persistence and Recovery

`AgentState` contains append-only `repair_plans` and `repair_verifications`. Redis LangGraph Checkpoint stores them with messages.
Messages remain the source of tool facts; plan events represent intent, and verification events represent semantic acceptance.

On restart:

- failed evidence without a current plan routes to Planner and creates v1;
- a current Verifier `replan` routes to Planner and creates the next version;
- otherwise execution returns to ReAct/mandatory scheduling;
- old checkpoints without new channels are accepted because initialization and reducers tolerate missing keys.

Queue mode uses synchronous checkpoint durability. Waiting for an external Pipeline remains the existing interrupt/Redis flow.

## Audit Result

The final result includes bounded `repair_plan` and `repair_verification` fields: plan/lineage/version, Pipeline ID, Work Item
counts, active ID, replan count, Planner status, Verifier verdict/model/Diff identity/coverage and error code. Full prompts and
unrestricted Diff text are not copied into the result.

## Failure Codes

| Code | Meaning / action |
|---|---|
| `repair_plan_missing_or_stale` | Recreate a plan from latest failed evidence. |
| `repair_plan_version_stale` | Retry against the latest version. |
| `repair_replan_evidence_stale` | Collect new search/read facts after the cursor. |
| `repair_replan_path_unproven` | Reference a result containing the path. |
| `repair_baseline_mismatch` | Stop; worktree and plan baseline differ. |
| `native_diff_review_incomplete` | Continue at `next_start_line`. |
| `native_validation_profile_missing` | Add a project validation profile. |
| `repair_verification_rejected` | Follow Verifier reason and replan or stop. |
| `independent_model_unavailable` | Configure another model; publication stays blocked. |
| `verifier_diff_over_budget` | Reduce/split the change; truncated Diff is never accepted. |

## Operations Checklist

Before enabling Native repair:

1. configure at least two distinct model candidates;
2. configure each project's exact unit-test profile when tests may be required;
3. verify isolated workspace, GitLab access, Redis Checkpoint and distributed runtime;
4. run focused Native tests and inspect final audit fields;
5. keep exact-SHA Pipeline callbacks and source-branch protection enabled.

For diagnosis, inspect plan/version, active item, Verifier error, Diff digest and validation before free-form logs.

## Source of Truth

- `ut_agent/repair_plan.py`: contracts, scheduling and commit reducer;
- `ut_agent/repair_planner.py`: initial planning and versioned replanning;
- `ut_agent/repair_verifier.py`: independent semantic acceptance;
- `ut_agent/agent.py`: graph and routes;
- `ut_agent/execution_policy.py`: tool and commit policy;
- `ut_agent/native_repair_state.py`: patch/Diff/validation hard evidence;
- `ut_agent/pipeline_actions.py`: mandatory actions and safe convergence;
- `pr_agent/distributed/checkpoint.py`: durable graph state.
