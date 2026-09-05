import asyncio
import os
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from pr_agent.distributed.checkpoint import CoreRedisCheckpointSaver

pytestmark = pytest.mark.skipif(not os.getenv("PR_AGENT_TEST_REDIS_URL"), reason="PR_AGENT_TEST_REDIS_URL is not set")


class GraphState(TypedDict, total=False):
    value: str
    pipeline_status: str


def build_interrupt_graph(saver):
    async def wait_pipeline(state: GraphState):
        event = interrupt({"wait": "pipeline"})
        return {"pipeline_status": event["status"]}

    builder = StateGraph(GraphState)
    builder.add_node("wait_pipeline", wait_pipeline)
    builder.add_edge(START, "wait_pipeline")
    builder.add_edge("wait_pipeline", END)
    return builder.compile(checkpointer=saver)


def test_checkpoint_survives_new_saver_instance():
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        saver1 = CoreRedisCheckpointSaver.from_url(redis_url)
        await saver1.async_client.flushdb()
        config = {"configurable": {"thread_id": "task-536", "checkpoint_ns": ""}}
        graph1 = build_interrupt_graph(saver1)

        first = await graph1.ainvoke({"value": "before"}, config=config, durability="sync")

        assert first["__interrupt__"][0].value == {"wait": "pipeline"}
        await saver1.aclose()

        saver2 = CoreRedisCheckpointSaver.from_url(redis_url)
        graph2 = build_interrupt_graph(saver2)
        resumed = await graph2.ainvoke(Command(resume={"status": "success"}), config=config, durability="sync")

        assert resumed["pipeline_status"] == "success"
        await saver2.async_client.flushdb()
        await saver2.aclose()

    asyncio.run(run_test())
