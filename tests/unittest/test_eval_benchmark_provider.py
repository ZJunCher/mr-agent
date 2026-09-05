from pr_agent.eval.benchmark_provider import BenchmarkGitProvider


class _FakeProject:
    def __init__(self, diffs):
        self._diffs = diffs

    def repository_compare(self, base, head):
        assert base == "BASE" and head == "HEAD"
        return {"diffs": self._diffs}


class _FakeProjects:
    def __init__(self, project):
        self._project = project

    def get(self, _id):
        return self._project


class _FakeGL:
    def __init__(self, project):
        self.projects = _FakeProjects(project)


def _make_provider(diffs):
    """Build a BenchmarkGitProvider without touching the network."""
    provider = object.__new__(BenchmarkGitProvider)
    provider.base_sha = "BASE"
    provider.head_sha = "HEAD"
    provider.diff_files = None
    provider.git_files = None
    provider.id_project = "group/repo"
    provider.id_mr = 7
    provider.gl = _FakeGL(_FakeProject(diffs))
    # avoid real file fetches
    provider.get_pr_file_content = lambda path, sha: f"content-of-{path}@{sha}"
    return provider


class TestBenchmarkProviderDiff:
    def test_get_diff_files_builds_filepatchinfo(self):
        diffs = [
            {"old_path": "a.py", "new_path": "a.py", "new_file": False,
             "deleted_file": False, "renamed_file": False,
             "diff": "@@ -1 +1,2 @@\n line\n+added\n"},
        ]
        provider = _make_provider(diffs)
        files = provider.get_diff_files()
        assert len(files) == 1
        f = files[0]
        assert f.filename == "a.py"
        assert "added" in f.patch
        assert f.num_plus_lines == 1

    def test_get_diff_files_filters_invalid_extensions(self):
        diffs = [
            {"old_path": "img.png", "new_path": "img.png", "new_file": True,
             "deleted_file": False, "renamed_file": False, "diff": ""},
            {"old_path": "b.py", "new_path": "b.py", "new_file": False,
             "deleted_file": False, "renamed_file": False,
             "diff": "@@ -1 +1 @@\n-old\n+new\n"},
        ]
        provider = _make_provider(diffs)
        files = provider.get_diff_files()
        names = [f.filename for f in files]
        assert "b.py" in names
        assert "img.png" not in names

    def test_get_diff_files_is_cached(self):
        diffs = [
            {"old_path": "a.py", "new_path": "a.py", "new_file": False,
             "deleted_file": False, "renamed_file": False,
             "diff": "@@ -1 +1 @@\n-x\n+y\n"},
        ]
        provider = _make_provider(diffs)
        first = provider.get_diff_files()
        second = provider.get_diff_files()
        assert first is second

    def test_get_files_returns_changed_paths(self):
        diffs = [
            {"old_path": "a.py", "new_path": "a.py", "new_file": False,
             "deleted_file": False, "renamed_file": False, "diff": "x"},
            {"old_path": "c.py", "new_path": "c.py", "new_file": True,
             "deleted_file": False, "renamed_file": False, "diff": "y"},
        ]
        provider = _make_provider(diffs)
        assert provider.get_files() == ["a.py", "c.py"]

    def test_get_diff_refs_returns_frozen_shas(self):
        provider = _make_provider([])
        refs = provider.get_diff_refs()
        assert refs == {"base_sha": "BASE", "head_sha": "HEAD", "start_sha": "BASE"}

    def test_publishing_is_noop(self):
        provider = _make_provider([])
        assert provider.publish_comment("x") is None
        assert provider.publish_persistent_comment("x", "h") is None
        assert provider.publish_code_suggestions([]) is True
