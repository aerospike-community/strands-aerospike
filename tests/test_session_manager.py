"""Integration tests for AerospikeSessionManager against a real Aerospike server."""

from __future__ import annotations

from typing import Any

import aerospike
import pytest
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

from strands_aerospike import AerospikeSessionManager


class _FakeMultiAgent:
    """Minimal stand-in for MultiAgentBase, avoiding a full Graph/Swarm construction."""

    def __init__(self, multi_agent_id: str, state: dict[str, Any]) -> None:
        self.id = multi_agent_id
        self._state = state

    def serialize_state(self) -> dict[str, Any]:
        return self._state

    def deserialize_state(self, state: dict[str, Any]) -> None:
        self._state = state


def _message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


@pytest.fixture
def repository(client: aerospike.Client, namespace: str) -> AerospikeSessionManager:
    return AerospikeSessionManager(session_id="test-session", namespace=namespace, client=client)


def test_create_session_creates_and_reads_back(repository: AerospikeSessionManager) -> None:
    assert repository.session.session_id == "test-session"
    read_back = repository.read_session("test-session")
    assert read_back is not None
    assert read_back.session_id == "test-session"


def test_create_session_twice_raises(repository: AerospikeSessionManager) -> None:
    with pytest.raises(SessionException, match="already exists"):
        repository.create_session(Session(session_id="test-session", session_type=SessionType.AGENT))


def test_agent_create_read_update(repository: AerospikeSessionManager) -> None:
    agent = SessionAgent(agent_id="agent-1", state={"key": "value"}, conversation_manager_state={})
    repository.create_agent("test-session", agent)

    read_back = repository.read_agent("test-session", "agent-1")
    assert read_back is not None
    assert read_back.state == {"key": "value"}

    read_back.state = {"key": "updated"}
    repository.update_agent("test-session", read_back)

    updated = repository.read_agent("test-session", "agent-1")
    assert updated is not None
    assert updated.state == {"key": "updated"}
    assert updated.created_at == read_back.created_at


def test_update_agent_missing_raises(repository: AerospikeSessionManager) -> None:
    agent = SessionAgent(agent_id="ghost", state={}, conversation_manager_state={})
    with pytest.raises(SessionException, match="does not exist"):
        repository.update_agent("test-session", agent)


def test_message_create_read_update_and_list(repository: AerospikeSessionManager) -> None:
    agent = SessionAgent(agent_id="agent-1", state={}, conversation_manager_state={})
    repository.create_agent("test-session", agent)

    for index in range(5):
        message = SessionMessage.from_message(_message(f"msg-{index}"), index)
        repository.create_message("test-session", "agent-1", message)

    all_messages = repository.list_messages("test-session", "agent-1")
    assert [message.message_id for message in all_messages] == [0, 1, 2, 3, 4]

    paged = repository.list_messages("test-session", "agent-1", limit=2, offset=1)
    assert [message.message_id for message in paged] == [1, 2]

    third = repository.read_message("test-session", "agent-1", 2)
    assert third is not None
    third.redact_message = _message("[redacted]")
    repository.update_message("test-session", "agent-1", third)

    reread = repository.read_message("test-session", "agent-1", 2)
    assert reread is not None
    assert reread.to_message() == _message("[redacted]")


def test_list_messages_beyond_range_returns_empty(repository: AerospikeSessionManager) -> None:
    agent = SessionAgent(agent_id="agent-1", state={}, conversation_manager_state={})
    repository.create_agent("test-session", agent)
    repository.create_message("test-session", "agent-1", SessionMessage.from_message(_message("only"), 0))

    assert repository.list_messages("test-session", "agent-1", offset=10) == []


def test_multi_agent_create_read_update(repository: AerospikeSessionManager) -> None:
    multi_agent = _FakeMultiAgent("graph-1", {"nodes": ["a", "b"]})
    repository.create_multi_agent("test-session", multi_agent)  # type: ignore[arg-type]

    state = repository.read_multi_agent("test-session", "graph-1")
    assert state == {"nodes": ["a", "b"]}

    multi_agent2 = _FakeMultiAgent("graph-1", {"nodes": ["a", "b", "c"]})
    repository.update_multi_agent("test-session", multi_agent2)  # type: ignore[arg-type]

    updated_state = repository.read_multi_agent("test-session", "graph-1")
    assert updated_state == {"nodes": ["a", "b", "c"]}


def test_delete_session_removes_everything(
    repository: AerospikeSessionManager, client: aerospike.Client, namespace: str
) -> None:
    agent = SessionAgent(agent_id="agent-1", state={}, conversation_manager_state={})
    repository.create_agent("test-session", agent)
    repository.create_message("test-session", "agent-1", SessionMessage.from_message(_message("hi"), 0))

    repository.delete_session("test-session")

    assert repository.read_session("test-session") is None
    assert repository.read_agent("test-session", "agent-1") is None
    with pytest.raises(aerospike.exception.RecordNotFound):
        client.get((namespace, "strands_sessions", "test-session:agent:agent-1:msg:0"))


def test_delete_session_missing_raises(repository: AerospikeSessionManager) -> None:
    with pytest.raises(SessionException, match="does not exist"):
        repository.delete_session("no-such-session")


def test_session_id_with_colon_rejected(client: aerospike.Client, namespace: str) -> None:
    with pytest.raises(ValueError, match=":"):
        AerospikeSessionManager(session_id="bad:id", namespace=namespace, client=client)
