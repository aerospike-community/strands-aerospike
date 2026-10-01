"""End-to-end smoke test: a real strands.Agent wired to all three Aerospike backends.

Unlike the other test modules (which exercise AerospikeStorage / AerospikeMemoryStore /
AerospikeSessionManager directly), this drives them the way a Strands user actually
would -- through strands.Agent -- to catch interface mismatches the isolated tests
cannot (wrong hook wiring, a signature the Agent calls differently than expected, etc).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import aerospike
from strands import Agent
from strands.memory import MemoryManager
from strands.models import Model
from strands.session import SnapshotSessionManager
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from strands_aerospike import AerospikeMemoryStore, AerospikeSessionManager, AerospikeStorage


class _EchoModel(Model):
    """Minimal Model that replies with a single fixed assistant text turn."""

    def __init__(self, text: str) -> None:
        self._text = text

    def format_chunk(self, event: Any) -> StreamEvent:
        return event  # type: ignore[return-value]

    def format_request(
        self, messages: Messages, tool_specs: list[ToolSpec] | None = None, system_prompt: str | None = None
    ) -> Any:
        return None

    def get_config(self) -> Any:
        return {}

    def update_config(self, **model_config: Any) -> None:
        pass

    async def structured_output(
        self, output_model: Any, prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        tool_choice: Any | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": self._text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


async def test_agent_persists_and_restores_session(client: aerospike.Client, namespace: str) -> None:
    session_manager = AerospikeSessionManager(session_id="agent-smoke-session", namespace=namespace, client=client)

    agent = Agent(model=_EchoModel("hello there"), session_manager=session_manager, agent_id="agent-1")
    result = await agent.invoke_async("hi")
    assert "hello there" in str(result)
    assert len(agent.messages) == 2  # user + assistant

    # A fresh Agent bound to the same session should restore the prior conversation.
    restored_session_manager = AerospikeSessionManager(
        session_id="agent-smoke-session", namespace=namespace, client=client
    )
    restored_agent = Agent(model=_EchoModel("unused"), session_manager=restored_session_manager, agent_id="agent-1")
    assert len(restored_agent.messages) == 2
    assert restored_agent.messages[0]["content"][0]["text"] == "hi"


async def test_agent_persists_via_storage(client: aerospike.Client, namespace: str) -> None:
    """A plain ``storage=`` kwarg is just a slot other components resolve from -- it
    persists nothing on its own. Drive it through SnapshotSessionManager, the real
    consumer, to exercise AerospikeStorage the way it is actually used.
    """
    storage = AerospikeStorage(namespace, client=client)
    session_manager = SnapshotSessionManager("agent-storage-session", storage=storage)

    agent = Agent(model=_EchoModel("stored"), session_manager=session_manager, agent_id="agent-storage")
    await agent.invoke_async("remember this")

    keys = await storage.list("")
    assert len(keys) > 0


async def test_agent_with_memory_manager_search_and_add(client: aerospike.Client, namespace: str) -> None:
    memory_store = AerospikeMemoryStore(name="agent-memory", namespace=namespace, client=client)
    await memory_store.initialize()

    memory_manager = MemoryManager(stores=[memory_store], add_tool_config=True, injection=False)
    agent = Agent(model=_EchoModel("noted"), memory_manager=memory_manager, agent_id="agent-memory-user")

    await memory_manager.add("# Preference\nUser prefers concise answers.")
    results = await memory_manager.search("preference concise")

    assert len(results) == 1
    assert "concise answers" in results[0].content

    # The manager's search_memory tool must have been registered on the agent.
    assert any(tool.tool_name == "search_memory" for tool in agent.tool_registry.registry.values())
