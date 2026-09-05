import json
import subprocess
from types import SimpleNamespace

import pytest

from pr_agent import config_loader as config_loader  # Initialize Dynaconf before importing ut_agent.
from ut_agent.blocker_evidence import validate_blocker_record
from ut_agent.dependency_evidence import (
    DeclaredDependency,
    DependencyManifestError,
    InterfaceQuery,
    build_dependency_blocker,
    build_deps_manifest_migration_suggestion,
    dependency_evidence_snapshot,
    derive_interface_queries,
    derive_missing_package_names,
    describe_missing_interface_evidence,
    discover_unique_package_provider,
    parse_declared_dependencies,
    rank_dependency_branches,
    resolve_current_dependency_evidence,
    resolve_declared_package_provider,
    search_missing_interface_across_namespace,
    search_missing_interface_elsewhere,
    search_missing_interfaces_elsewhere,
    validate_discovered_provider_changes,
    verify_interfaces_on_branch,
)
from ut_agent.tools.resolve_dependency import RootCauseEvidenceBundle, resolve_dependency_evidence_tool
from ut_agent.tools.tool_registry import _extract_params


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_parse_declared_dependencies_accepts_deps_yml_and_gitmodules(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text(
        """
- module: lhotse
  url: git@gitlab.example.com:eabot/lhotse.git
  branch: dev
""",
        encoding="utf-8",
    )
    (tmp_path / ".gitmodules").write_text(
        """
[submodule "vendor/logan"]
    path = vendor/logan
    url = git@gitlab.example.com:eabot/logan.git
    branch = main
""",
        encoding="utf-8",
    )

    assert parse_declared_dependencies(str(tmp_path)) == [
        DeclaredDependency("lhotse", "eabot/lhotse", "dev"),
        DeclaredDependency("vendor/logan", "eabot/logan", "main"),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"module": "missing-url", "branch": "dev"},
        {"module": "missing-branch", "url": "git@gitlab.example.com:eabot/lhotse.git"},
        {"module": "bad-url", "url": "not a repository", "branch": "dev"},
    ],
)
def test_parse_declared_dependencies_skips_incomplete_entries(tmp_path, entry):
    import yaml

    (tmp_path / "deps.yml").write_text(yaml.safe_dump([entry]), encoding="utf-8")

    assert parse_declared_dependencies(str(tmp_path)) == []


def test_parse_declared_dependencies_deduplicates_projects(tmp_path):
    (tmp_path / "deps.yml").write_text(
        """
- module: first
  url: git@gitlab.example.com:eabot/lhotse.git
  branch: dev
- module: duplicate
  url: https://gitlab.example.com/eabot/lhotse.git
  branch: main
""",
        encoding="utf-8",
    )

    assert parse_declared_dependencies(str(tmp_path)) == [
        DeclaredDependency("first", "eabot/lhotse", "dev")
    ]


def test_parse_declared_dependencies_rejects_more_than_limit(tmp_path):
    entries = "\n".join(
        f"- module: dep-{index}\n  url: git@gitlab.example.com:eabot/dep-{index}.git\n  branch: dev"
        for index in range(13)
    )
    (tmp_path / "deps.yml").write_text(entries, encoding="utf-8")

    with pytest.raises(DependencyManifestError, match="12"):
        parse_declared_dependencies(str(tmp_path))


def test_derive_interface_query_from_ros_generated_request():
    queries = derive_interface_queries(
        "eabot_msgs::srv::RemoteControl_Request_<std::allocator<void>> has no member named 'node_name'"
    )

    assert InterfaceQuery("eabot_msgs", "srv", "RemoteControl", "RemoteControl.srv") in queries


def test_derive_interface_query_from_missing_generated_header():
    queries = derive_interface_queries(
        "fatal error: eabot_msgs/msg/lidar_udp_frame.hpp: No such file or directory"
    )

    assert InterfaceQuery("eabot_msgs", "msg", "LidarUdpFrame", "LidarUdpFrame.msg") in queries


def test_derive_missing_cmake_package_name():
    names = derive_missing_package_names(
        'Could not find a package configuration file provided by "eabot_cmake" with any of the following names'
    )

    assert names == ["eabot_cmake"]


def test_dependency_evidence_tool_cannot_accept_arbitrary_repository():
    parameters = _extract_params(resolve_dependency_evidence_tool)

    assert set(parameters["properties"]) == {"job_name", "root_cause_id"}
    assert "project_path" not in parameters["properties"]
    assert "branch" not in parameters["properties"]


def test_dependency_tool_collects_all_diagnostics_for_selected_root(monkeypatch, tmp_path):
    pipeline = {
        "pipeline_status": "failed",
        "root_cause_groups": [
            {
                "root_cause_id": "root-prism",
                "canonical_job_name": "build_release_arm64",
                "job_names": ["build_release_arm64"],
                "canonical_diagnostic": (
                    "fatal error: eabot_msgs/msg/drivable_mini_output.hpp: No such file or directory"
                ),
            },
            {
                "root_cause_id": "root-unrelated",
                "canonical_job_name": "clang_tidy_check",
                "job_names": ["clang_tidy_check"],
                "canonical_diagnostic": "unrelated diagnostic",
            },
        ],
        "failed_jobs": [
            {
                "name": "build_release_arm64",
                "diagnostic_candidates": [
                    {
                        "text": (
                            "fatal error: eabot_msgs/msg/drivable_mini_output.hpp: No such file or directory"
                        )
                    },
                    {
                        "text": "fatal error: eabot_msgs/msg/planning_status.hpp: No such file or directory"
                    },
                ],
                "causal_lines": [
                    "fatal error: eabot_msgs/msg/planning_status.hpp: No such file or directory",
                ],
                "log_context": "ci_deps file not found or download failed (HTTP 404)",
            },
            {
                "name": "clang_tidy_check",
                "causal_lines": ["unrelated diagnostic"],
                "log_context": "unrelated context",
            },
        ],
    }
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda _messages: SimpleNamespace(pipelines=[pipeline]),
    )
    monkeypatch.setattr("ut_agent.tools.resolve_dependency.get_repo_dir", lambda _mr_id: str(tmp_path))
    monkeypatch.setattr(
        "ut_agent.tools.resolve_dependency.get_git_provider",
        lambda: SimpleNamespace(gl=object(), id_project="eabot/prism"),
    )
    captured = {}
    monkeypatch.setattr(
        "ut_agent.tools.resolve_dependency.resolve_current_dependency_evidence",
        lambda _gl, _repo, diagnostic, **kwargs: captured.update(
            diagnostic=diagnostic,
            kwargs=kwargs,
        )
        or {"status": "not_found"},
    )

    result = json.loads(
        resolve_dependency_evidence_tool.func(
            job_name="build_release_arm64",
            root_cause_id="root-prism",
            state={"mr_id": 120, "source_branch": "end2areas", "messages": []},
        )
    )

    assert result["root_cause_id"] == "root-prism"
    assert captured["diagnostic"].count("drivable_mini_output.hpp") == 1
    assert captured["diagnostic"].count("planning_status.hpp") == 1
    assert "ci_deps file not found" in captured["diagnostic"]
    assert "unrelated" not in captured["diagnostic"]
    assert captured["kwargs"]["source_branch"] == "end2areas"


def test_dependency_tool_downgrades_invalid_blocker_to_error(monkeypatch, tmp_path):
    invalid = _missing_declared_interface_result()
    invalid["primary_diagnostic"] = ""
    monkeypatch.setattr(
        "ut_agent.tools.resolve_dependency._root_cause_evidence_from_state",
        lambda *_args: RootCauseEvidenceBundle("root-prism", "fatal error: interface missing"),
    )
    monkeypatch.setattr("ut_agent.tools.resolve_dependency.get_repo_dir", lambda _mr_id: str(tmp_path))
    monkeypatch.setattr(
        "ut_agent.tools.resolve_dependency.get_git_provider",
        lambda: SimpleNamespace(gl=object(), id_project="eabot/prism"),
    )
    monkeypatch.setattr(
        "ut_agent.tools.resolve_dependency.resolve_current_dependency_evidence",
        lambda *_args, **_kwargs: invalid,
    )

    result = json.loads(
        resolve_dependency_evidence_tool.func(
            job_name="build_release_arm64",
            root_cause_id="root-prism",
            state={"mr_id": 120, "messages": []},
        )
    )

    assert result["status"] == "error"
    assert result["validation_error"]
    assert "blocker" not in result


class _ReadOnlyFile:
    def __init__(self, content: bytes):
        self._content = content

    def decode(self):
        return self._content


class _ReadOnlyFiles:
    def __init__(self, content: bytes):
        self._content = content
        self.calls = []

    def get(self, *, file_path, ref):
        self.calls.append((file_path, ref))
        if file_path == "eabot_msgs/package.xml":
            return _ReadOnlyFile(b"<package><name>eabot_msgs</name></package>")
        if file_path == "src/eabot_msgs/package.xml":
            raise RuntimeError("404")
        return _ReadOnlyFile(self._content)


class _ReadOnlyBranches:
    def __init__(self):
        self.calls = []

    def get(self, branch):
        self.calls.append(branch)
        return type("Branch", (), {"commit": {"id": "lhotse-current-sha"}})()


class _ReadOnlyProject:
    def __init__(self, content: bytes):
        self.branches = _ReadOnlyBranches()
        self.files = _ReadOnlyFiles(content)
        self.tree_calls = []

    def repository_tree(self, *, ref, recursive, iterator, path=None):
        self.tree_calls.append((ref, path, recursive, iterator))
        return iter([
            {"type": "tree", "path": "eabot_msgs/srv"},
            {"type": "blob", "path": "eabot_msgs/srv/RemoteControl.srv"},
        ])

    def __getattr__(self, name):
        raise AssertionError(f"unexpected GitLab project API access: {name}")


class _ReadOnlyProjects:
    def __init__(self, project):
        self.project = project
        self.calls = []

    def get(self, project_path):
        self.calls.append(project_path)
        return self.project


class _ReadOnlyGitLab:
    def __init__(self, project):
        self.projects = _ReadOnlyProjects(project)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected GitLab API access: {name}")


class _DeclaredProviderFiles:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, *, file_path, ref):
        self.calls.append((file_path, ref))
        value = self.values.get((file_path, ref))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError("404")
        return _ReadOnlyFile(value)


class _DeclaredProviderProject:
    def __init__(self, branch_shas, files):
        self.branches = _UpstreamBranches(branch_shas)
        self.files = _DeclaredProviderFiles(files)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected GitLab project API access: {name}")


def _declared_provider_gitlab(projects):
    return SimpleNamespace(projects=_DiscoveryProjects(projects))


def test_lhotse_is_verified_as_eabot_msgs_provider_by_package_xml():
    dependencies = [DeclaredDependency("lhotse", "eabot/lhotse", "dev")]
    project = _DeclaredProviderProject(
        {"dev": "lhotse-dev-sha"},
        {
            (
                "eabot_msgs/package.xml",
                "lhotse-dev-sha",
            ): b"<package><name>eabot_msgs</name></package>",
        },
    )

    result = resolve_declared_package_provider(
        _declared_provider_gitlab({"eabot/lhotse": project}),
        dependencies,
        "eabot_msgs",
    )

    assert result["status"] == "resolved"
    assert result["project_path"] == "eabot/lhotse"
    assert result["package_path"] == "eabot_msgs"
    assert result["resolved_sha"] == "lhotse-dev-sha"
    assert project.files.calls == [
        ("eabot_msgs/package.xml", "lhotse-dev-sha"),
        ("src/eabot_msgs/package.xml", "lhotse-dev-sha"),
    ]


def test_declared_provider_accepts_src_package_path():
    project = _DeclaredProviderProject(
        {"main": "provider-sha"},
        {
            (
                "src/eabot_msgs/package.xml",
                "provider-sha",
            ): b"<package><name>eabot_msgs</name></package>",
        },
    )

    result = resolve_declared_package_provider(
        _declared_provider_gitlab({"eabot/provider": project}),
        [DeclaredDependency("provider", "eabot/provider", "main")],
        "eabot_msgs",
    )

    assert result["status"] == "resolved"
    assert result["package_path"] == "src/eabot_msgs"


def test_declared_provider_is_ambiguous_when_two_manifests_match():
    package = b"<package><name>eabot_msgs</name></package>"
    first = _DeclaredProviderProject({"dev": "first-sha"}, {("eabot_msgs/package.xml", "first-sha"): package})
    second = _DeclaredProviderProject({"main": "second-sha"}, {("eabot_msgs/package.xml", "second-sha"): package})

    result = resolve_declared_package_provider(
        _declared_provider_gitlab({"eabot/first": first, "eabot/second": second}),
        [
            DeclaredDependency("first", "eabot/first", "dev"),
            DeclaredDependency("second", "eabot/second", "main"),
        ],
        "eabot_msgs",
    )

    assert result["status"] == "ambiguous"
    assert {item["project_path"] for item in result["matches"]} == {"eabot/first", "eabot/second"}


@pytest.mark.parametrize(
    "manifest",
    [
        b"<package>",
        b"<package><name>different_msgs</name></package>",
    ],
)
def test_declared_provider_rejects_invalid_or_wrong_manifest(manifest):
    project = _DeclaredProviderProject({"dev": "provider-sha"}, {("eabot_msgs/package.xml", "provider-sha"): manifest})

    result = resolve_declared_package_provider(
        _declared_provider_gitlab({"eabot/provider": project}),
        [DeclaredDependency("provider", "eabot/provider", "dev")],
        "eabot_msgs",
    )

    assert result["status"] == "not_found"


def test_declared_provider_bounds_read_error_details():
    project = _DeclaredProviderProject(
        {"dev": "provider-sha"},
        {("eabot_msgs/package.xml", "provider-sha"): RuntimeError("secret upstream detail")},
    )

    result = resolve_declared_package_provider(
        _declared_provider_gitlab({"eabot/provider": project}),
        [DeclaredDependency("provider", "eabot/provider", "dev")],
        "eabot_msgs",
    )

    assert result["status"] == "not_found"
    assert result["errors"] == [{"project_path": "eabot/provider", "category": "RuntimeError"}]
    assert "secret upstream detail" not in json.dumps(result)


def test_branch_ranking_finds_two_end_areas_beyond_first_twenty():
    names = [f"archive/{index:03d}" for index in range(40)] + ["TwoEndAreas/phase1/0820"]

    ranked = rank_dependency_branches(
        names,
        source_branch="end2areas",
        queries=[InterfaceQuery("eabot_msgs", "msg", "DrivableMiniOutput", "DrivableMiniOutput.msg")],
    )

    assert ranked[0] == "TwoEndAreas/phase1/0820"


class _ScopedTreeProject:
    def __init__(self, branch_shas, entries_by_scope):
        self.branches = _UpstreamBranches(branch_shas)
        self.entries_by_scope = entries_by_scope
        self.tree_calls = []

    def repository_tree(self, *, ref, path, recursive, iterator):
        self.tree_calls.append((ref, path, recursive, iterator))
        return iter(self.entries_by_scope.get((ref, path), ()))


def _drivable_query():
    return InterfaceQuery("eabot_msgs", "msg", "DrivableMiniOutput", "DrivableMiniOutput.msg")


def test_branch_verification_scans_only_package_path():
    project = _ScopedTreeProject(
        {"TwoEndAreas/phase1/0820": "candidate-sha"},
        {
            ("candidate-sha", "eabot_msgs"): [
                {"type": "blob", "path": "eabot_msgs/perception/msg/DrivableMiniOutput.msg"},
            ],
        },
    )

    result = verify_interfaces_on_branch(
        project,
        "TwoEndAreas/phase1/0820",
        "eabot_msgs",
        [_drivable_query()],
    )

    assert result["verification_complete"] is True
    assert result["matched_queries"] == ["DrivableMiniOutput.msg"]
    assert result["file_paths"] == {
        "DrivableMiniOutput.msg": "eabot_msgs/perception/msg/DrivableMiniOutput.msg",
    }
    assert project.tree_calls == [("candidate-sha", "eabot_msgs", True, True)]


def test_branch_verification_does_not_claim_absence_after_scope_limit():
    project = _ScopedTreeProject(
        {"huge": "huge-sha"},
        {
            ("huge-sha", "eabot_msgs"): [
                {"type": "blob", "path": f"eabot_msgs/msg/Generated{index}.msg"}
                for index in range(5_001)
            ],
        },
    )

    result = verify_interfaces_on_branch(project, "huge", "eabot_msgs", [_drivable_query()])

    assert result["verification_complete"] is False
    assert result["error_category"] == "scope_limit_exceeded"
    assert result["missing_queries"] == []


def test_branch_verification_reports_lookup_failure_without_raw_error():
    project = _ScopedTreeProject({}, {})

    result = verify_interfaces_on_branch(project, "missing", "eabot_msgs", [_drivable_query()])

    assert result["verification_complete"] is False
    assert result["error_category"] == "RuntimeError"
    assert "404" not in json.dumps(result)


def test_candidate_search_classifies_unique_full_branch_after_unrelated_catalog_entries():
    branch_shas = {f"archive/{index:03d}": f"archive-{index}" for index in range(40)}
    branch_shas.update({"dev": "dev-sha", "TwoEndAreas/phase1/0820": "candidate-sha"})
    project = _ScopedTreeProject(
        branch_shas,
        {
            ("candidate-sha", "eabot_msgs"): [
                {"type": "blob", "path": "eabot_msgs/msg/DrivableMiniOutput.msg"},
                {"type": "blob", "path": "eabot_msgs/msg/PlanningStatus.msg"},
            ],
        },
    )
    gl = SimpleNamespace(projects=_DiscoveryProjects({"eabot/lhotse": project}))
    queries = [
        _drivable_query(),
        InterfaceQuery("eabot_msgs", "msg", "PlanningStatus", "PlanningStatus.msg"),
    ]

    result = search_missing_interfaces_elsewhere(
        gl,
        "eabot/lhotse",
        queries,
        "dev",
        "eabot_msgs",
        source_branch="end2areas",
    )

    assert result["candidate_kind"] == "unique_verified_candidate"
    assert result["verified_candidates"][0]["branch"] == "TwoEndAreas/phase1/0820"
    assert result["verified_candidates"][0]["matched_queries"] == [
        "DrivableMiniOutput.msg",
        "PlanningStatus.msg",
    ]


def test_candidate_search_lists_multiple_full_branches_without_choosing_one():
    project = _ScopedTreeProject(
        {"dev": "dev-sha", "feature/a": "a-sha", "feature/b": "b-sha"},
        {
            ("a-sha", "eabot_msgs"): [
                {"type": "blob", "path": "eabot_msgs/msg/DrivableMiniOutput.msg"},
            ],
            ("b-sha", "eabot_msgs"): [
                {"type": "blob", "path": "eabot_msgs/msg/DrivableMiniOutput.msg"},
            ],
        },
    )

    result = search_missing_interfaces_elsewhere(
        SimpleNamespace(projects=_DiscoveryProjects({"eabot/lhotse": project})),
        "eabot/lhotse",
        [_drivable_query()],
        "dev",
        "eabot_msgs",
        source_branch="feature",
    )

    assert result["candidate_kind"] == "multiple_verified_candidates"
    assert {item["branch"] for item in result["verified_candidates"]} == {"feature/a", "feature/b"}


def test_candidate_search_classifies_partial_candidate():
    project = _ScopedTreeProject(
        {"dev": "dev-sha", "end2areas/partial": "partial-sha"},
        {
            ("partial-sha", "eabot_msgs"): [
                {"type": "blob", "path": "eabot_msgs/msg/DrivableMiniOutput.msg"},
            ],
        },
    )
    result = search_missing_interfaces_elsewhere(
        SimpleNamespace(projects=_DiscoveryProjects({"eabot/lhotse": project})),
        "eabot/lhotse",
        [
            _drivable_query(),
            InterfaceQuery("eabot_msgs", "msg", "PlanningStatus", "PlanningStatus.msg"),
        ],
        "dev",
        "eabot_msgs",
        source_branch="end2areas",
    )

    assert result["candidate_kind"] == "partial_candidate"
    assert result["partial_candidates"][0]["branch"] == "end2areas/partial"
    assert result["verified_candidates"] == []


def test_candidate_search_caps_branch_catalog_and_reports_no_verified_candidate():
    branch_shas = {f"archive/{index:03d}": f"archive-{index}" for index in range(301)}
    branch_shas["dev"] = "dev-sha"
    project = _ScopedTreeProject(branch_shas, {})

    result = search_missing_interfaces_elsewhere(
        SimpleNamespace(projects=_DiscoveryProjects({"eabot/lhotse": project})),
        "eabot/lhotse",
        [
            _drivable_query(),
            InterfaceQuery("eabot_msgs", "msg", "PlanningStatus", "PlanningStatus.msg"),
        ],
        "dev",
        "eabot_msgs",
        source_branch="end2areas",
    )

    assert result["catalog_truncated"] is True
    assert result["checked_branch_count"] == 20
    assert result["candidate_kind"] == "no_verified_candidate"
    assert result["verified_candidates"] == []


class _RejectingWriteApi:
    def __init__(self, label, attempts):
        self.label = label
        self.attempts = attempts

    def __getattr__(self, name):
        operation = f"{self.label}.{name}"
        self.attempts.append(operation)
        raise AssertionError(f"unexpected GitLab write API access: {operation}")


class _PrismDependencyFiles:
    def __init__(self, write_attempts):
        self.calls = []
        self.write_attempts = write_attempts

    def get(self, *, file_path, ref):
        self.calls.append((file_path, ref))
        if (file_path, ref) == ("eabot_msgs/package.xml", "lhotse-dev-sha"):
            return _ReadOnlyFile(b"<package><name>eabot_msgs</name></package>")
        raise RuntimeError("404")

    def create(self, *_args, **_kwargs):
        self.write_attempts.append("files.create")
        raise AssertionError("unexpected GitLab write API access: files.create")

    def update(self, *_args, **_kwargs):
        self.write_attempts.append("files.update")
        raise AssertionError("unexpected GitLab write API access: files.update")

    def delete(self, *_args, **_kwargs):
        self.write_attempts.append("files.delete")
        raise AssertionError("unexpected GitLab write API access: files.delete")


class _PrismDependencyProject:
    def __init__(self):
        branch_shas = {f"archive/{index:03d}": f"archive-{index}-sha" for index in range(40)}
        branch_shas.update({
            "dev": "lhotse-dev-sha",
            "TwoEndAreas/phase1/0820": "two-end-areas-sha",
        })
        self.write_attempts = []
        self.branches = _UpstreamBranches(branch_shas)
        self.files = _PrismDependencyFiles(self.write_attempts)
        self.commits = _RejectingWriteApi("commits", self.write_attempts)
        self.merge_requests = _RejectingWriteApi("merge_requests", self.write_attempts)
        self.tree_calls = []
        self._trees = {
            ("lhotse-dev-sha", "eabot_msgs"): [],
            ("two-end-areas-sha", "eabot_msgs"): [
                {"type": "blob", "path": "eabot_msgs/msg/DrivableMiniOutput.msg"},
                {"type": "blob", "path": "eabot_msgs/msg/GateleverDetectionOutput.msg"},
                {"type": "blob", "path": "eabot_msgs/msg/OrderInfo.msg"},
                {"type": "blob", "path": "eabot_msgs/msg/PlanningStatus.msg"},
            ],
        }

    def repository_tree(self, *, ref, path, recursive, iterator):
        self.tree_calls.append((ref, path, recursive, iterator))
        return iter(self._trees.get((ref, path), ()))

    def push(self, *_args, **_kwargs):
        self.write_attempts.append("push")
        raise AssertionError("unexpected GitLab write API access: push")

    @property
    def repository_commits(self):
        self.write_attempts.append("repository_commits")
        raise AssertionError("unexpected GitLab write API access: repository_commits")

    def __getattr__(self, name):
        raise AssertionError(f"unexpected GitLab project API access: {name}")


def test_prism_120_resolves_four_interfaces_to_verified_candidate_without_repository_changes(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text(
        "- module: lhotse\n"
        "  url: git@gitlab.example.com:eabot/lhotse.git\n"
        "  branch: dev\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init")
    _git(tmp_path, "add", "dev_kit/deps.yml")
    _git(tmp_path, "-c", "user.name=Codex", "-c", "user.email=codex@example.com", "commit", "-m", "fixture")
    before = _git(tmp_path, "status", "--porcelain")
    project = _PrismDependencyProject()
    gl = SimpleNamespace(projects=_DiscoveryProjects({"eabot/lhotse": project}))
    diagnostic = "\n".join([
        "fatal error: eabot_msgs/msg/drivable_mini_output.hpp: No such file or directory",
        "fatal error: eabot_msgs/msg/gatelever_detection_output.hpp: No such file or directory",
        "fatal error: eabot_msgs/msg/order_info.hpp: No such file or directory",
        "fatal error: eabot_msgs/msg/planning_status.hpp: No such file or directory",
        "ci_deps: branch end2areas returned HTTP 404; falling back to the declared dependency branch",
    ])

    result = resolve_current_dependency_evidence(
        gl,
        str(tmp_path),
        diagnostic,
        current_project_id="eabot/prism",
        source_branch="end2areas",
    )
    snapshot = dependency_evidence_snapshot(result)

    assert result["status"] == "not_found"
    assert snapshot["candidate_kind"] == "unique_verified_candidate"
    assert [query["filename"] for query in snapshot["queries"]] == [
        "DrivableMiniOutput.msg",
        "GateleverDetectionOutput.msg",
        "OrderInfo.msg",
        "PlanningStatus.msg",
    ]
    assert snapshot["current_branch"]["missing_queries"] == [
        "DrivableMiniOutput.msg",
        "GateleverDetectionOutput.msg",
        "OrderInfo.msg",
        "PlanningStatus.msg",
    ]
    assert snapshot["verified_candidates"] == [{
        "branch": "TwoEndAreas/phase1/0820",
        "resolved_sha": "two-end-areas-sha",
        "verification_complete": True,
        "matched_queries": [
            "DrivableMiniOutput.msg",
            "GateleverDetectionOutput.msg",
            "OrderInfo.msg",
            "PlanningStatus.msg",
        ],
        "missing_queries": [],
        "file_paths": {
            "DrivableMiniOutput.msg": "eabot_msgs/msg/DrivableMiniOutput.msg",
            "GateleverDetectionOutput.msg": "eabot_msgs/msg/GateleverDetectionOutput.msg",
            "OrderInfo.msg": "eabot_msgs/msg/OrderInfo.msg",
            "PlanningStatus.msg": "eabot_msgs/msg/PlanningStatus.msg",
        },
    }]
    blocker = build_dependency_blocker(result, "build_release_arm64")
    assert validate_blocker_record(blocker, "build_release_arm64") is None
    assert project.write_attempts == []
    assert _git(tmp_path, "status", "--porcelain") == before


def _missing_declared_interface_result(candidate_kind="unique_verified_candidate"):
    candidates = [
        {
            "branch": "TwoEndAreas/phase1/0820",
            "resolved_sha": "candidate-sha",
            "verification_complete": True,
            "matched_queries": ["DrivableMiniOutput.msg", "PlanningStatus.msg"],
            "missing_queries": [],
            "file_paths": {
                "DrivableMiniOutput.msg": "eabot_msgs/msg/DrivableMiniOutput.msg",
                "PlanningStatus.msg": "eabot_msgs/msg/PlanningStatus.msg",
            },
        },
    ]
    if candidate_kind == "multiple_verified_candidates":
        candidates.append({**candidates[0], "branch": "feature/other", "resolved_sha": "other-sha"})
    if candidate_kind in {"partial_candidate", "no_verified_candidate"}:
        candidates = []
    return {
        "status": "not_found",
        "evidence_kind": "declared_interface_missing",
        "project_path": "eabot/lhotse",
        "declared_branch": "dev",
        "declared_sha": "dev-sha",
        "package_path": "eabot_msgs",
        "queries": [
            {
                "package": "eabot_msgs",
                "kind": "msg",
                "interface": "DrivableMiniOutput",
                "filename": "DrivableMiniOutput.msg",
            },
            {"package": "eabot_msgs", "kind": "msg", "interface": "PlanningStatus", "filename": "PlanningStatus.msg"},
        ],
        "current_branch": {
            "verification_complete": True,
            "matched_queries": [],
            "missing_queries": ["DrivableMiniOutput.msg", "PlanningStatus.msg"],
            "file_paths": {},
        },
        "candidate_kind": candidate_kind,
        "verified_candidates": candidates,
        "partial_candidates": [],
        "checked_branch_count": 20,
        "catalog_truncated": False,
        "primary_diagnostic": "fatal error: eabot_msgs/msg/drivable_mini_output.hpp: No such file or directory",
        "owner_facing_analysis": "当前声明分支缺少已观察接口。",
    }


def test_unique_verified_candidate_builds_valid_external_dependency_blocker():
    blocker = build_dependency_blocker(_missing_declared_interface_result(), "build_release_arm64")

    assert validate_blocker_record(blocker, "build_release_arm64") is None
    assert blocker["blocker_type"] == "external_dependency"
    assert "TwoEndAreas/phase1/0820" in blocker["suggested_action"]
    assert "维护者确认" in blocker["suggested_action"]


def test_multiple_candidates_do_not_choose_one_in_suggested_action():
    blocker = build_dependency_blocker(
        _missing_declared_interface_result("multiple_verified_candidates"),
        "build_release_arm64",
    )

    assert validate_blocker_record(blocker, "build_release_arm64") is None
    assert "请选择并确认" in blocker["suggested_action"]
    assert "建议切换到" not in blocker["suggested_action"]


@pytest.mark.parametrize("candidate_kind", ["partial_candidate", "no_verified_candidate"])
def test_no_full_candidate_still_builds_human_action_without_guessing(candidate_kind):
    blocker = build_dependency_blocker(
        _missing_declared_interface_result(candidate_kind),
        "build_release_arm64",
    )

    assert validate_blocker_record(blocker, "build_release_arm64") is None
    assert "建议切换到" not in blocker["suggested_action"]


def test_blocker_is_invalid_without_exact_ci_or_branch_evidence():
    result = _missing_declared_interface_result()
    result["primary_diagnostic"] = ""
    result["declared_sha"] = ""

    blocker = build_dependency_blocker(result, "build_release_arm64")

    assert validate_blocker_record(blocker, "build_release_arm64") is not None


class _UpstreamBranches:
    def __init__(self, branch_shas):
        self.branch_shas = branch_shas

    def list(self, **_kwargs):
        return [SimpleNamespace(name=name) for name in self.branch_shas]

    def get(self, name):
        sha = self.branch_shas.get(name)
        if sha is None:
            raise RuntimeError("404")
        return SimpleNamespace(commit={"id": sha})


class _UpstreamCommits:
    def __init__(self, records):
        self.records = records

    def list(self, *, ref_name, path, per_page, get_all):
        return [
            SimpleNamespace(
                id=record["id"],
                author_name=record["author_name"],
                committed_date=record["date"],
                title=record["title"],
            )
            for record in self.records
            if record["ref_name"] == ref_name and record["path"] == path
        ][:per_page]


class _UpstreamProject:
    def __init__(self, branch_shas, trees, commit_records):
        self.branches = _UpstreamBranches(branch_shas)
        self.commits = _UpstreamCommits(commit_records)
        self.files = _DiscoveryFiles({
            "eabot_msgs/package.xml": b"<package><name>eabot_msgs</name></package>",
        })
        self._trees = trees

    def repository_tree(self, *, ref, recursive, iterator, path=None):
        return iter(self._trees.get(ref, []))


class _UpstreamProjects:
    def __init__(self, project):
        self.project = project

    def get(self, project_path):
        return self.project


def _upstream_gitlab(branch_shas, trees, commit_records):
    project = _UpstreamProject(branch_shas, trees, commit_records)
    return SimpleNamespace(projects=_UpstreamProjects(project))


def test_search_missing_interface_elsewhere_finds_file_on_other_branch():
    query = InterfaceQuery("eabot_msgs", "msg", "LidarUdpFrame", "LidarUdpFrame.msg")
    gl = _upstream_gitlab(
        branch_shas={"dev": "dev-sha", "feature/lidar-v2": "feature-sha"},
        trees={
            "dev": [{"type": "tree", "path": "eabot_msgs/msg"}],
            "feature-sha": [{"type": "blob", "path": "eabot_msgs/msg/LidarUdpFrame.msg"}],
        },
        commit_records=[
            {
                "ref_name": "dev",
                "path": "LidarUdpFrame.msg",
                "id": "abc123def456",
                "author_name": "alice",
                "date": "2026-08-01T00:00:00Z",
                "title": "remove unused lidar udp frame message",
            }
        ],
    )

    result = search_missing_interface_elsewhere(gl, "eabot/eabot_msgs", query, "dev")

    assert result["status"] == "searched"
    assert result["present_on_branches"] == [
        {"branch": "feature/lidar-v2", "file_path": "eabot_msgs/msg/LidarUdpFrame.msg"}
    ]
    assert result["removal_commit"]["commit_sha"] == "abc123def456"[:12]
    assert result["removal_commit"]["author"] == "alice"


def test_describe_missing_interface_evidence_renders_owner_facing_text():
    evidence = {
        "status": "searched",
        "project_path": "eabot/eabot_msgs",
        "declared_branch": "dev",
        "query": {"filename": "LidarUdpFrame.msg"},
        "present_on_branches": [{"branch": "feature/lidar-v2", "file_path": "eabot_msgs/msg/LidarUdpFrame.msg"}],
        "removal_commit": {
            "commit_sha": "abc123def456",
            "author": "alice",
            "committed_date": "2026-08-01",
            "title": "remove unused lidar udp frame message",
        },
    }

    text = describe_missing_interface_evidence(evidence)

    assert "eabot/eabot_msgs" in text
    assert "LidarUdpFrame.msg" in text
    assert "abc123def456" in text
    assert "feature/lidar-v2" in text


def test_describe_missing_interface_evidence_reports_no_other_branch_found():
    evidence = {
        "status": "searched",
        "project_path": "eabot/eabot_msgs",
        "declared_branch": "dev",
        "query": {"filename": "LidarUdpFrame.msg"},
        "present_on_branches": [],
        "removal_commit": None,
    }

    text = describe_missing_interface_evidence(evidence)

    assert "均未找到该文件" in text


def test_describe_missing_interface_evidence_ignores_non_searched_status():
    assert describe_missing_interface_evidence({"status": "not_applicable"}) == ""


class _DiscoveryFiles:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, *, file_path, ref):
        self.calls.append((file_path, ref))
        if file_path not in self.values:
            raise RuntimeError("404")
        return _ReadOnlyFile(self.values[file_path])


class _DiscoveryProject:
    def __init__(self, project_path, files):
        self.path_with_namespace = project_path
        self.default_branch = "dev"
        self.ssh_url_to_repo = f"git@gitlab.example.com:{project_path}.git"
        self.files = _DiscoveryFiles(files)
        self.branches = _ReadOnlyBranches()


class _DiscoveryProjects:
    def __init__(self, projects):
        self.projects = projects

    def get(self, project_path):
        return self.projects[project_path]


class _DiscoveryGroupProjects:
    def __init__(self, summaries):
        self.summaries = summaries

    def list(self, **_kwargs):
        return self.summaries


class _DiscoveryGroups:
    def __init__(self, summaries):
        self.group = SimpleNamespace(projects=_DiscoveryGroupProjects(summaries))

    def get(self, namespace):
        assert namespace == "eabot"
        return self.group


def _discovery_gitlab(projects):
    summaries = [
        SimpleNamespace(
            path_with_namespace=path,
            default_branch=project.default_branch,
            archived=False,
            ssh_url_to_repo=project.ssh_url_to_repo,
        )
        for path, project in projects.items()
    ]
    return SimpleNamespace(groups=_DiscoveryGroups(summaries), projects=_DiscoveryProjects(projects))


class _MigrationProject:
    def __init__(self, project_path, default_branch, branch_shas, trees):
        self.path_with_namespace = project_path
        self.default_branch = default_branch
        self.ssh_url_to_repo = f"git@gitlab.example.com:{project_path}.git"
        self.branches = _UpstreamBranches(branch_shas)
        self.files = _DiscoveryFiles({
            "eabot_msgs/package.xml": b"<package><name>eabot_msgs</name></package>",
        })
        self._trees = trees

    def repository_tree(self, *, ref, recursive, iterator, path=None):
        return iter(self._trees.get(ref, []))


def _migration_gitlab(projects, *, current_project_path, current_branch_shas, current_trees):
    """A GitLab double where `current_project_path` is missing the file but sibling `projects` may have it."""
    all_projects = dict(projects)
    all_projects[current_project_path] = _MigrationProject(
        current_project_path, "dev", current_branch_shas, current_trees
    )
    summaries = [
        SimpleNamespace(
            path_with_namespace=path,
            default_branch=project.default_branch,
            archived=False,
            ssh_url_to_repo=project.ssh_url_to_repo,
        )
        for path, project in projects.items()
    ]
    return SimpleNamespace(groups=_DiscoveryGroups(summaries), projects=_DiscoveryProjects(all_projects))


def test_search_missing_interface_across_namespace_finds_single_migration_target():
    query = InterfaceQuery("eabot_msgs", "msg", "LidarUdpFrame", "LidarUdpFrame.msg")
    sibling = _MigrationProject(
        "eabot/eabot_msgs_v2",
        "main",
        {"main": "sibling-sha"},
        {"sibling-sha": [{"type": "blob", "path": "eabot_msgs/msg/LidarUdpFrame.msg"}]},
    )
    gl = _migration_gitlab(
        {"eabot/eabot_msgs_v2": sibling},
        current_project_path="eabot/eabot_msgs",
        current_branch_shas={"dev": "dev-sha"},
        current_trees={"dev-sha": [{"type": "tree", "path": "eabot_msgs/msg"}]},
    )

    matches = search_missing_interface_across_namespace(gl, "eabot/eabot_msgs", query)

    assert matches == [
        {"project_path": "eabot/eabot_msgs_v2", "branch": "main", "file_path": "eabot_msgs/msg/LidarUdpFrame.msg"}
    ]


def test_search_missing_interface_across_namespace_reports_ambiguous_when_multiple_match():
    query = InterfaceQuery("eabot_msgs", "msg", "LidarUdpFrame", "LidarUdpFrame.msg")
    tree = [{"type": "blob", "path": "eabot_msgs/msg/LidarUdpFrame.msg"}]
    gl = _migration_gitlab(
        {
            "eabot/eabot_msgs_v2": _MigrationProject("eabot/eabot_msgs_v2", "main", {"main": "a-sha"}, {"a-sha": tree}),
            "eabot/eabot_msgs_v3": _MigrationProject("eabot/eabot_msgs_v3", "main", {"main": "b-sha"}, {"b-sha": tree}),
        },
        current_project_path="eabot/eabot_msgs",
        current_branch_shas={"dev": "dev-sha"},
        current_trees={"dev-sha": []},
    )

    matches = search_missing_interface_across_namespace(gl, "eabot/eabot_msgs", query)

    assert {match["project_path"] for match in matches} == {"eabot/eabot_msgs_v2", "eabot/eabot_msgs_v3"}


def test_describe_missing_interface_evidence_mentions_single_migration_target():
    evidence = {
        "status": "searched",
        "project_path": "eabot/eabot_msgs",
        "declared_branch": "dev",
        "query": {"filename": "LidarUdpFrame.msg"},
        "present_on_branches": [],
        "removal_commit": None,
        "migrated_to_projects": [
            {"project_path": "eabot/eabot_msgs_v2", "branch": "main", "file_path": "eabot_msgs/msg/LidarUdpFrame.msg"}
        ],
    }

    text = describe_missing_interface_evidence(evidence)

    assert "eabot/eabot_msgs_v2" in text
    assert "疑似已迁移到该仓库" in text


def test_unique_missing_package_provider_is_authorized_read_only(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text("dependencies: []\n", encoding="utf-8")
    projects = {
        "eabot/base": _DiscoveryProject(
            "eabot/base",
            {"eabot_cmake/package.xml": b"<package><name>eabot_cmake</name></package>"},
        ),
        "eabot/logan": _DiscoveryProject("eabot/logan", {}),
    }

    result = discover_unique_package_provider(
        _discovery_gitlab(projects),
        str(tmp_path),
        "eabot/chogori",
        'Could not find a package configuration file provided by "eabot_cmake"',
    )

    assert result["status"] == "resolved"
    assert result["evidence_kind"] == "discovered_provider"
    assert result["project_path"] == "eabot/base"
    assert result["declared_branch"] == "dev"
    assert result["repository_url"] == "git@gitlab.example.com:eabot/base.git"
    assert result["dependency_manifest_path"] == "dev_kit/deps.yml"


def test_two_missing_package_providers_are_ambiguous(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text("dependencies: []\n", encoding="utf-8")
    package = {"eabot_cmake/package.xml": b"<package><name>eabot_cmake</name></package>"}
    projects = {
        "eabot/base": _DiscoveryProject("eabot/base", package),
        "eabot/platform": _DiscoveryProject("eabot/platform", package),
    }

    result = discover_unique_package_provider(
        _discovery_gitlab(projects),
        str(tmp_path),
        "eabot/chogori",
        'Could not find a package configuration file provided by "eabot_cmake"',
    )

    assert result["status"] == "ambiguous"
    assert len(result["matches"]) == 2


def test_missing_package_discovery_does_not_authorize_zero_matches(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text("dependencies: []\n", encoding="utf-8")

    result = discover_unique_package_provider(
        _discovery_gitlab({"eabot/logan": _DiscoveryProject("eabot/logan", {})}),
        str(tmp_path),
        "eabot/chogori",
        'Could not find a package configuration file provided by "eabot_cmake"',
    )

    assert result == {"status": "not_found", "package_name": "eabot_cmake"}


def test_missing_package_discovery_skips_archived_and_third_party_projects(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text("dependencies: []\n", encoding="utf-8")
    package = {"eabot_cmake/package.xml": b"<package><name>eabot_cmake</name></package>"}
    projects = {
        "eabot/archived": _DiscoveryProject("eabot/archived", package),
        "eabot/third-party/vendor": _DiscoveryProject("eabot/third-party/vendor", package),
    }
    summaries = [
        SimpleNamespace(
            path_with_namespace="eabot/archived",
            default_branch="dev",
            archived=True,
            ssh_url_to_repo=projects["eabot/archived"].ssh_url_to_repo,
        ),
        SimpleNamespace(
            path_with_namespace="eabot/third-party/vendor",
            default_branch="dev",
            archived=False,
            ssh_url_to_repo=projects["eabot/third-party/vendor"].ssh_url_to_repo,
        ),
    ]
    gl = SimpleNamespace(
        groups=_DiscoveryGroups(summaries),
        projects=_DiscoveryProjects(projects),
    )

    result = discover_unique_package_provider(
        gl,
        str(tmp_path),
        "eabot/chogori",
        'Could not find a package configuration file provided by "eabot_cmake"',
    )

    assert result["status"] == "not_found"


def test_discovered_provider_manifest_change_accepts_only_exact_repository_and_branch(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    manifest = tmp_path / "dev_kit" / "deps.yml"
    manifest.write_text(
        "- module: lhotse\n  url: git@gitlab.example.com:eabot/lhotse.git\n  branch: dev\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init")
    _git(tmp_path, "add", "dev_kit/deps.yml")
    _git(tmp_path, "-c", "user.name=Codex", "-c", "user.email=codex@example.com", "commit", "-m", "fixture")
    snapshot = {
        "evidence_kind": "discovered_provider",
        "module": "base",
        "project_path": "eabot/base",
        "repository_url": "git@gitlab.example.com:eabot/base.git",
        "declared_branch": "dev",
        "dependency_manifest_path": "dev_kit/deps.yml",
    }
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "- module: base\n  url: git@gitlab.example.com:eabot/base.git\n  branch: dev\n",
        encoding="utf-8",
    )

    assert validate_discovered_provider_changes(
        str(tmp_path), [snapshot], [str(manifest)]
    ) == (True, "")


def test_discovered_provider_manifest_change_rejects_unverified_branch(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    manifest = tmp_path / "dev_kit" / "deps.yml"
    manifest.write_text("dependencies: []\n", encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "add", "dev_kit/deps.yml")
    _git(tmp_path, "-c", "user.name=Codex", "-c", "user.email=codex@example.com", "commit", "-m", "fixture")
    snapshot = {
        "evidence_kind": "discovered_provider",
        "module": "base",
        "project_path": "eabot/base",
        "repository_url": "git@gitlab.example.com:eabot/base.git",
        "declared_branch": "dev",
        "dependency_manifest_path": "dev_kit/deps.yml",
    }
    manifest.write_text(
        "- module: base\n  url: git@gitlab.example.com:eabot/base.git\n  branch: guessed-branch\n",
        encoding="utf-8",
    )

    safe, reason = validate_discovered_provider_changes(str(tmp_path), [snapshot], [str(manifest)])

    assert safe is False
    assert "未经核验" in reason


def test_resolver_reads_only_current_declared_dependency(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text(
        """
- module: lhotse
  url: git@gitlab.example.com:eabot/lhotse.git
  branch: dev
""",
        encoding="utf-8",
    )
    _git(tmp_path, "init")
    _git(tmp_path, "add", "dev_kit/deps.yml")
    _git(tmp_path, "-c", "user.name=Codex", "-c", "user.email=codex@example.com", "commit", "-m", "fixture")
    before = _git(tmp_path, "status", "--porcelain")
    project = _ReadOnlyProject(
        b"int64 timestamp_ns\nuint32 command\nstring trace_id\nstring optional\n"
        b"---\nint64 timestamp_ns\nstring trace_id\nbool success\n"
    )
    fake_gl = _ReadOnlyGitLab(project)

    result = resolve_current_dependency_evidence(
        fake_gl,
        str(tmp_path),
        "eabot_msgs::srv::RemoteControl_Request_ has no member named 'node_name'",
    )

    assert result["status"] == "resolved"
    assert result["project_path"] == "eabot/lhotse"
    assert result["declared_branch"] == "dev"
    assert result["resolved_sha"] == "lhotse-current-sha"
    assert result["file_path"] == "eabot_msgs/srv/RemoteControl.srv"
    assert "node_name" not in result["content"]
    assert "target" not in result["content"]
    assert fake_gl.projects.calls == ["eabot/lhotse", "eabot/lhotse"]
    assert project.branches.calls == ["dev", "dev"]
    assert project.tree_calls == [("lhotse-current-sha", "eabot_msgs", True, True)]
    assert project.files.calls == [
        ("eabot_msgs/package.xml", "lhotse-current-sha"),
        ("src/eabot_msgs/package.xml", "lhotse-current-sha"),
        ("eabot_msgs/srv/RemoteControl.srv", "lhotse-current-sha"),
    ]
    assert _git(tmp_path, "status", "--porcelain") == before


def test_resolver_attaches_upstream_evidence_when_interface_is_missing(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text(
        """
- module: eabot_msgs
  url: git@gitlab.example.com:eabot/eabot_msgs.git
  branch: dev
""",
        encoding="utf-8",
    )
    fake_gl = _upstream_gitlab(
        branch_shas={"dev": "dev-sha", "feature/lidar-v2": "feature-sha"},
        trees={
            "dev-sha": [{"type": "tree", "path": "eabot_msgs/msg"}],
            "feature-sha": [{"type": "blob", "path": "eabot_msgs/msg/LidarUdpFrame.msg"}],
        },
        commit_records=[
            {
                "ref_name": "dev",
                "path": "LidarUdpFrame.msg",
                "id": "abc123def456",
                "author_name": "alice",
                "date": "2026-08-01",
                "title": "remove unused lidar udp frame message",
            }
        ],
    )

    result = resolve_current_dependency_evidence(
        fake_gl,
        str(tmp_path),
        "fatal error: eabot_msgs/msg/lidar_udp_frame.hpp: No such file or directory",
    )

    assert result["status"] == "not_found"
    assert "LidarUdpFrame.msg" in result["owner_facing_analysis"]
    assert result["upstream_evidence"]["present_on_branches"] == [
        {"branch": "feature/lidar-v2", "file_path": "eabot_msgs/msg/LidarUdpFrame.msg"}
    ]


def test_build_deps_manifest_migration_suggestion_renders_url_only_change(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text(
        "dependencies:\n"
        "  - module: eabot_msgs\n"
        "    url: git@gitlab.example.com:eabot/eabot_msgs.git\n"
        "    branch: dev\n",
        encoding="utf-8",
    )

    suggestion = build_deps_manifest_migration_suggestion(
        str(tmp_path), "eabot/eabot_msgs", "dev", "eabot/eabot_msgs_v2"
    )

    assert "dev_kit/deps.yml" in suggestion
    assert "url: git@gitlab.example.com:eabot/eabot_msgs.git" in suggestion
    assert "url: git@gitlab.example.com:eabot/eabot_msgs_v2.git" in suggestion
    assert suggestion.count("branch: dev") == 2
    assert "不会自动提交" in suggestion


def test_build_deps_manifest_migration_suggestion_returns_empty_when_block_not_found(tmp_path):
    (tmp_path / "deps.yml").write_text(
        "dependencies:\n"
        "  - module: other\n"
        "    url: git@gitlab.example.com:eabot/other.git\n"
        "    branch: dev\n",
        encoding="utf-8",
    )

    suggestion = build_deps_manifest_migration_suggestion(
        str(tmp_path), "eabot/eabot_msgs", "dev", "eabot/eabot_msgs_v2"
    )

    assert suggestion == ""


def test_resolver_includes_manifest_suggestion_for_unambiguous_migration(tmp_path):
    (tmp_path / "dev_kit").mkdir()
    (tmp_path / "dev_kit" / "deps.yml").write_text(
        "dependencies:\n"
        "  - module: eabot_msgs\n"
        "    url: git@gitlab.example.com:eabot/eabot_msgs.git\n"
        "    branch: dev\n",
        encoding="utf-8",
    )
    sibling = _MigrationProject(
        "eabot/eabot_msgs_v2",
        "main",
        {"main": "sibling-sha"},
        {"sibling-sha": [{"type": "blob", "path": "eabot_msgs/msg/LidarUdpFrame.msg"}]},
    )
    fake_gl = _migration_gitlab(
        {"eabot/eabot_msgs_v2": sibling},
        current_project_path="eabot/eabot_msgs",
        current_branch_shas={"dev": "dev-sha"},
        current_trees={"dev-sha": [{"type": "tree", "path": "eabot_msgs/msg"}]},
    )

    result = resolve_current_dependency_evidence(
        fake_gl,
        str(tmp_path),
        "fatal error: eabot_msgs/msg/lidar_udp_frame.hpp: No such file or directory",
    )

    assert result["status"] == "not_found"
    assert "eabot/eabot_msgs_v2" in result["owner_facing_analysis"]
    suggestion = result["upstream_evidence"]["suggested_manifest_change"]
    assert "url: git@gitlab.example.com:eabot/eabot_msgs_v2.git" in suggestion
    assert "不会自动提交" in suggestion
