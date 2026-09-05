import os
import sqlite3
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def connect_sqlite(path: str) -> sqlite3.Connection:
    ensure_parent_dir(path)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn
    except Exception:
        conn.close()
        raise


def run_write_transaction(
    path: str,
    operation: Callable[[sqlite3.Connection], T],
    attempts: int = 4,
    *,
    connect: Callable[[str], sqlite3.Connection] = connect_sqlite,
) -> T:
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            conn = connect(path)
            try:
                with conn:
                    return operation(conn)
            finally:
                conn.close()
        except sqlite3.OperationalError as error:
            locked = "locked" in str(error).lower() or "busy" in str(error).lower()
            if not locked or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (2**attempt))
    raise RuntimeError("unreachable SQLite retry state")
