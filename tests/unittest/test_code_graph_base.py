from pr_agent.algo.code_graph.base import DependencyEdge, normalize_repo_path


def test_normalize_repo_path_strips_leading_dot_slash():
    assert normalize_repo_path("./pkg/mod.py") == "pkg/mod.py"


def test_normalize_repo_path_converts_backslashes():
    assert normalize_repo_path("pkg\\sub\\mod.py") == "pkg/sub/mod.py"


def test_normalize_repo_path_passthrough_for_already_normal_path():
    assert normalize_repo_path("pkg/sub/mod.py") == "pkg/sub/mod.py"


def test_dependency_edge_is_a_simple_frozen_pair():
    edge = DependencyEdge(source="a.py", target="b.py")
    assert edge.source == "a.py"
    assert edge.target == "b.py"
