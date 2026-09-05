"""Public UT-Agent package surface with lazy heavyweight imports."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ut_agent.agent import UTAgent

__all__ = ["UTAgent"]


def __getattr__(name: str) -> Any:
    if name != "UTAgent":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from ut_agent.agent import UTAgent

    return UTAgent
