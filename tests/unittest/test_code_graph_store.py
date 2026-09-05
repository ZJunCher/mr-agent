import os
import tempfile

import pytest

from pr_agent.algo.code_graph.graph_store import GraphStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield GraphStore(os.path.join(tmp, "sub", "graph.db"))


def test_creates_db_file_and_parent_dirs(store):
    assert os.path.isfile(store.db_path)


def test_creates_db_with_bare_filename_in_current_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    store = GraphStore("graph.db")
    assert os.path.isfile(store.db_path)


def test_forward_direct_dependency(store):
    store.replace_file_edges("a.py", ["b.py"])
    assert store.get_forward("a.py", max_hops=1) == {"b.py": 1}


def test_forward_multi_hop(store):
    store.replace_file_edges("a.py", ["b.py"])
    store.replace_file_edges("b.py", ["c.py"])
    assert store.get_forward("a.py", max_hops=2) == {"b.py": 1, "c.py": 2}


def test_forward_respects_max_hops(store):
    store.replace_file_edges("a.py", ["b.py"])
    store.replace_file_edges("b.py", ["c.py"])
    assert store.get_forward("a.py", max_hops=1) == {"b.py": 1}


def test_reverse_is_symmetric_to_forward(store):
    store.replace_file_edges("a.py", ["b.py"])
    store.replace_file_edges("b.py", ["c.py"])
    assert store.get_reverse("c.py", max_hops=2) == {"b.py": 1, "a.py": 2}


def test_replace_file_edges_overwrites_not_accumulates(store):
    store.replace_file_edges("a.py", ["b.py"])
    store.replace_file_edges("a.py", ["c.py"])
    assert store.get_forward("a.py", max_hops=1) == {"c.py": 1}


def test_remove_file_deletes_its_outgoing_edges(store):
    store.replace_file_edges("a.py", ["b.py"])
    store.remove_file("a.py")
    assert store.get_forward("a.py", max_hops=1) == {}


def test_get_forward_on_unknown_file_returns_empty(store):
    assert store.get_forward("does_not_exist.py", max_hops=2) == {}


def test_no_cycle_infinite_loop(store):
    store.replace_file_edges("a.py", ["b.py"])
    store.replace_file_edges("b.py", ["a.py"])
    # a <-> b cycle: forward from a should not include a itself, and must terminate.
    assert store.get_forward("a.py", max_hops=3) == {"b.py": 1}
