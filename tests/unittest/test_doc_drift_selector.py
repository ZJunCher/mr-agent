from pr_agent.tools.doc_drift_selector import (
    ancestor_dirs,
    match_glob,
    select_candidates,
)


class TestMatchGlob:
    def test_exact(self):
        assert match_glob("AGENTS.md", ["AGENTS.md"])
        assert not match_glob("src/AGENTS.md", ["AGENTS.md"])

    def test_single_star_within_segment(self):
        assert match_glob("README.md", ["*.md"])
        assert not match_glob("docs/README.md", ["*.md"])  # * does not cross '/'

    def test_double_star_any_depth(self):
        assert match_glob("docs/a.md", ["docs/**/*.md"])
        assert match_glob("docs/sub/dir/a.md", ["docs/**/*.md"])
        # docs/**/*.md requires at least the docs/ prefix and a .md file
        assert not match_glob("other/a.md", ["docs/**/*.md"])

    def test_leading_slash_normalized(self):
        assert match_glob("/AGENTS.md", ["AGENTS.md"])


class TestAncestorDirs:
    def test_includes_root_and_all_ancestors(self):
        dirs = ancestor_dirs(["a/b/c/file.py"])
        assert "" in dirs
        assert "a" in dirs
        assert "a/b" in dirs
        assert "a/b/c" in dirs
        assert "a/b/c/file.py" not in dirs  # filename dropped

    def test_root_level_file(self):
        assert ancestor_dirs(["file.py"]) == {""}

    def test_empty(self):
        assert ancestor_dirs([]) == set()


class TestSelectCandidates:
    def _repo(self):
        return [
            "AGENTS.md",
            "README.md",
            "docs/guide.md",
            "docs/deep/nested.md",
            "rag/README.md",
            "rag/config.md",
            "unrelated/notes.md",
        ]

    def test_global_docs_always_selected(self):
        selected = select_candidates(
            self._repo(),
            changed_files=["src/x.py"],  # no neighbours among docs
            global_globs=["AGENTS.md", "README.md", "docs/**/*.md"],
            ancestor_globs=["*.md", "README*"],
            max_docs=30,
        )
        assert "AGENTS.md" in selected
        assert "README.md" in selected
        assert "docs/guide.md" in selected
        assert "docs/deep/nested.md" in selected
        # not a global doc and not a neighbour of src/x.py
        assert "rag/config.md" not in selected

    def test_neighbour_docs_by_same_dir(self):
        selected = select_candidates(
            self._repo(),
            changed_files=["rag/engine.py"],
            global_globs=["AGENTS.md"],
            ancestor_globs=["*.md", "README*"],
            max_docs=30,
        )
        assert "rag/README.md" in selected
        assert "rag/config.md" in selected
        assert "unrelated/notes.md" not in selected

    def test_dedup_global_and_neighbour(self):
        # rag/README.md is both a neighbour and would match a global README glob
        selected = select_candidates(
            self._repo(),
            changed_files=["rag/engine.py"],
            global_globs=["**/README.md"],
            ancestor_globs=["*.md"],
            max_docs=30,
        )
        assert selected.count("rag/README.md") == 1

    def test_global_prioritised_under_cap(self):
        selected = select_candidates(
            self._repo(),
            changed_files=["rag/engine.py"],
            global_globs=["AGENTS.md", "README.md"],
            ancestor_globs=["*.md"],
            max_docs=2,
        )
        assert selected == ["AGENTS.md", "README.md"]

    def test_empty_when_no_docs(self):
        assert select_candidates([], ["src/x.py"], ["AGENTS.md"], ["*.md"], 30) == []
