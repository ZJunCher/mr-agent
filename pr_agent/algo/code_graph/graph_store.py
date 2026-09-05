"""SQLite-backed storage for the file-level dependency graph of one target
branch.

Each row in the `edges` table is a directed dependency: `source` (a file)
depends on `target` (another file). Two indexes make both query directions
cheap:

- forward: "which files does X depend on" -> queries by `source`
- reverse: "which files depend on X" (used to warn a PR author that other
  code relies on the file they are changing) -> queries by `target`

This intentionally stores only file-level edges (not function/symbol-level
call edges) - see the design doc's decision to start with the smaller,
faster-to-build-and-validate slice of the problem.
"""

import os
import sqlite3
import threading
from typing import Dict, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (source, target)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
"""

class GraphStore:
    """Thin wrapper around a single SQLite file holding one target branch's
    file-level dependency graph."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_lock = threading.Lock()
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent_dir, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def replace_file_edges(self, file_path: str, dependency_paths: Iterable[str]) -> None:
        """Replace all outgoing edges for `file_path` with `dependency_paths`.

        Used both for a full rebuild (called once per file) and for an
        incremental update (called only for files that changed since the
        last sync).
        """
        with self._write_lock:
            with self._connect() as conn:
                conn.execute("INSERT OR IGNORE INTO files(path) VALUES (?)", (file_path,))
                conn.execute("DELETE FROM edges WHERE source = ?", (file_path,))
                conn.executemany(
                    "INSERT OR IGNORE INTO edges(source, target) VALUES (?, ?)",
                    [(file_path, target) for target in dependency_paths],
                )
                conn.commit()

    def remove_file(self, file_path: str) -> None:
        """Remove a file's outgoing edges from the graph - used when an
        incremental update detects the file was deleted."""
        with self._write_lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
                conn.execute("DELETE FROM edges WHERE source = ?", (file_path,))
                conn.commit()

    def get_forward(self, file_path: str, max_hops: int) -> Dict[str, int]:
        """Files that `file_path` (transitively, up to `max_hops`) depends
        on, mapped to the hop distance at which they were first found."""
        return self._bfs(file_path, max_hops, column="source", other="target")

    def get_reverse(self, file_path: str, max_hops: int) -> Dict[str, int]:
        """Files that (transitively, up to `max_hops`) depend on
        `file_path` - i.e. what might break if `file_path` changes."""
        return self._bfs(file_path, max_hops, column="target", other="source")

    def _bfs(self, start_file: str, max_hops: int, column: str, other: str) -> Dict[str, int]:
        distances: Dict[str, int] = {}
        frontier = [start_file]
        with self._connect() as conn:
            for hop in range(1, max_hops + 1):
                if not frontier:
                    break
                placeholders = ",".join("?" for _ in frontier)
                rows = conn.execute(
                    f"SELECT DISTINCT {other} FROM edges WHERE {column} IN ({placeholders})",
                    frontier,
                ).fetchall()
                next_frontier = []
                for (neighbor,) in rows:
                    if neighbor == start_file or neighbor in distances:
                        continue
                    distances[neighbor] = hop
                    next_frontier.append(neighbor)
                frontier = next_frontier
        return distances
