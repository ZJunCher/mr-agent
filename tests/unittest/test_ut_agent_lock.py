import asyncio

from ut_agent.agent import _RUN_LOCKS, is_mr_being_fixed
from ut_agent.tools.context import workspace_key


def test_is_mr_being_fixed_false_when_no_lock():
    assert is_mr_being_fixed("proj-never-seen", 999999) is False


def test_is_mr_being_fixed_false_when_lock_exists_but_not_held():
    key = workspace_key("proj-idle", 1)
    _RUN_LOCKS.setdefault(key, asyncio.Lock())
    assert is_mr_being_fixed("proj-idle", 1) is False


def test_is_mr_being_fixed_true_when_lock_held():
    key = workspace_key("proj-busy", 2)
    lock = _RUN_LOCKS.setdefault(key, asyncio.Lock())

    async def _hold():
        await lock.acquire()
        return is_mr_being_fixed("proj-busy", 2)

    try:
        assert asyncio.run(_hold()) is True
    finally:
        if lock.locked():
            lock.release()
