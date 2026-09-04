"""Aerospike-backed session manager and repository.

Persists each message individually (like ``FileSessionManager`` / ``S3SessionManager``)
rather than as a single versioned snapshot, so it also covers multi-agent
orchestrators. Session/agent/multi-agent metadata and each message are stored as a
JSON blob in a ``d`` bin under a composite ``session_id:...`` key. Message listing
never scans: each agent record carries a ``n`` bin (the message count), so
``list_messages`` builds the exact key range and reads it with one batch get. The
session record carries ``aids``/``maids`` list bins (agent and multi-agent ids) so
:meth:`AerospikeSessionManager.delete_session` can remove an entire session's records
without a scan either.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aerospike
from aerospike import exception as aerospike_exception
from aerospike_helpers.operations import list_operations as list_ops
from aerospike_helpers.operations import operations as ops
from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from ._client import get_client
from ._keys import validate_identifier

if TYPE_CHECKING:
    from strands.multiagent.base import MultiAgentBase

_DATA_BIN = "d"
_MESSAGE_COUNT_BIN = "n"
_AGENT_IDS_BIN = "aids"
_MULTI_AGENT_IDS_BIN = "maids"


class AerospikeSessionManager(RepositorySessionManager, SessionRepository):
    """Session manager persisting agent sessions to Aerospike.

    Example:
        ```python
        from strands import Agent
        from strands_aerospike import AerospikeSessionManager

        session_manager = AerospikeSessionManager(session_id="my-session", namespace="test")
        agent = Agent(session_manager=session_manager)
        ```
    """

    def __init__(
        self,
        session_id: str,
        namespace: str,
        *,
        set: str = "strands_sessions",
        hosts: list[tuple[str, int]] | None = None,
        client: aerospike.Client | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Aerospike session manager.

        If no session with the given ``session_id`` exists yet, one is created.

        Args:
            session_id: ID for the session. Must not contain ``:``.
            namespace: Aerospike namespace to store records in.
            set: Aerospike set name for all session/agent/message/multi-agent records.
            hosts: Cluster seed addresses as ``(host, port)`` tuples, used only when
                ``client`` is not given. Defaults to ``[("127.0.0.1", 3000)]``.
            client: An existing, connected ``aerospike.Client`` to use. When omitted,
                a process-wide client is lazily created and shared across all
                ``strands_aerospike`` components constructed with the same ``hosts``.
            **kwargs: Additional keyword arguments for future extensibility.

        Raises:
            ValueError: If ``session_id`` is empty or contains ``:``.
        """
        validate_identifier(session_id, "session")
        self._namespace = namespace
        self._set = set
        self._client = client if client is not None else get_client(hosts)
        super().__init__(session_id=session_id, session_repository=self)

    # -- Key layout --

    def _session_key(self, session_id: str) -> tuple[str, str, str]:
        return (self._namespace, self._set, session_id)

    def _agent_key(self, session_id: str, agent_id: str) -> tuple[str, str, str]:
        return (self._namespace, self._set, f"{session_id}:agent:{agent_id}")

    def _message_key(self, session_id: str, agent_id: str, message_id: int) -> tuple[str, str, str]:
        return (self._namespace, self._set, f"{session_id}:agent:{agent_id}:msg:{message_id}")

    def _multi_agent_key(self, session_id: str, multi_agent_id: str) -> tuple[str, str, str]:
        return (self._namespace, self._set, f"{session_id}:multiagent:{multi_agent_id}")

    # -- Session --

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        """Create a new session in Aerospike."""
        data = json.dumps(session.to_dict(), ensure_ascii=False)
        try:
            self._client.put(
                self._session_key(session.session_id),
                {_DATA_BIN: data, _AGENT_IDS_BIN: [], _MULTI_AGENT_IDS_BIN: []},
                policy={"exists": aerospike.POLICY_EXISTS_CREATE},
            )
        except aerospike_exception.RecordExistsError as error:
            raise SessionException(f"Session {session.session_id} already exists") from error
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error creating session {session.session_id}: {error}") from error
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        """Read session data from Aerospike."""
        bins = self._get_bins(self._session_key(session_id), context=f"reading session {session_id}")
        if bins is None:
            return None
        return Session.from_dict(json.loads(bins[_DATA_BIN]))

    def delete_session(self, session_id: str, **kwargs: Any) -> None:
        """Delete a session and every agent/message/multi-agent record under it.

        Args:
            session_id: ID of the session to delete.
            **kwargs: Additional keyword arguments for future extensibility.

        Raises:
            SessionException: If the session does not exist, or the delete fails.
        """
        bins = self._get_bins(self._session_key(session_id), context=f"reading session {session_id}")
        if bins is None:
            raise SessionException(f"Session {session_id} does not exist")

        keys_to_remove = [self._session_key(session_id)]
        for agent_id in bins.get(_AGENT_IDS_BIN, []):
            keys_to_remove.append(self._agent_key(session_id, agent_id))
            agent_bins = self._get_bins(
                self._agent_key(session_id, agent_id), context=f"reading agent {agent_id} for delete"
            )
            message_count = agent_bins.get(_MESSAGE_COUNT_BIN, 0) if agent_bins is not None else 0
            keys_to_remove.extend(self._message_key(session_id, agent_id, index) for index in range(message_count))
        for multi_agent_id in bins.get(_MULTI_AGENT_IDS_BIN, []):
            keys_to_remove.append(self._multi_agent_key(session_id, multi_agent_id))

        try:
            self._client.batch_remove(keys_to_remove)
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error deleting session {session_id}: {error}") from error

    # -- Agent --

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        """Create a new agent in Aerospike."""
        agent_id = validate_identifier(session_agent.agent_id, "agent")
        data = json.dumps(session_agent.to_dict(), ensure_ascii=False)
        try:
            self._client.put(self._agent_key(session_id, agent_id), {_DATA_BIN: data, _MESSAGE_COUNT_BIN: 0})
            self._client.operate(self._session_key(session_id), [list_ops.list_append(_AGENT_IDS_BIN, agent_id)])
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error creating agent {agent_id}: {error}") from error

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        """Read agent data from Aerospike."""
        bins = self._get_bins(self._agent_key(session_id, agent_id), context=f"reading agent {agent_id}")
        if bins is None:
            return None
        return SessionAgent.from_dict(json.loads(bins[_DATA_BIN]))

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        """Update agent data in Aerospike, preserving the original creation timestamp."""
        agent_id = session_agent.agent_id
        previous_agent = self.read_agent(session_id=session_id, agent_id=agent_id)
        if previous_agent is None:
            raise SessionException(f"Agent {agent_id} in session {session_id} does not exist")

        session_agent.created_at = previous_agent.created_at
        data = json.dumps(session_agent.to_dict(), ensure_ascii=False)
        try:
            self._client.put(self._agent_key(session_id, agent_id), {_DATA_BIN: data})
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error updating agent {agent_id}: {error}") from error

    # -- Message --

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        """Create a new message in Aerospike and bump the agent's message count."""
        message_id = session_message.message_id
        if not isinstance(message_id, int):
            raise ValueError(f"message_id=<{message_id}> | message id must be an integer")

        data = json.dumps(session_message.to_dict(), ensure_ascii=False)
        try:
            self._client.put(self._message_key(session_id, agent_id, message_id), {_DATA_BIN: data})
            self._client.operate(self._agent_key(session_id, agent_id), [ops.write(_MESSAGE_COUNT_BIN, message_id + 1)])
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error creating message {message_id}: {error}") from error

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any) -> SessionMessage | None:
        """Read message data from Aerospike."""
        bins = self._get_bins(
            self._message_key(session_id, agent_id, message_id), context=f"reading message {message_id}"
        )
        if bins is None:
            return None
        return SessionMessage.from_dict(json.loads(bins[_DATA_BIN]))

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        """Update message data in Aerospike, preserving the original creation timestamp."""
        message_id = session_message.message_id
        previous_message = self.read_message(session_id=session_id, agent_id=agent_id, message_id=message_id)
        if previous_message is None:
            raise SessionException(f"Message {message_id} does not exist")

        session_message.created_at = previous_message.created_at
        data = json.dumps(session_message.to_dict(), ensure_ascii=False)
        try:
            self._client.put(self._message_key(session_id, agent_id, message_id), {_DATA_BIN: data})
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error updating message {message_id}: {error}") from error

    def list_messages(
        self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0, **kwargs: Any
    ) -> list[SessionMessage]:
        """List messages for an agent with pagination, via one batch read.

        The agent record's message count is read first so the exact key range can be
        built -- no scan is needed to discover which message keys exist.

        Args:
            session_id: ID of the session.
            agent_id: ID of the agent.
            limit: Optional limit on number of messages to return.
            offset: Optional offset for pagination.
            **kwargs: Additional keyword arguments.

        Returns:
            SessionMessage objects in order, sorted by message_id.

        Raises:
            SessionException: If the Aerospike read fails.
        """
        agent_bins = self._get_bins(self._agent_key(session_id, agent_id), context=f"reading agent {agent_id}")
        message_count = agent_bins.get(_MESSAGE_COUNT_BIN, 0) if agent_bins is not None else 0

        end = message_count if limit is None else min(message_count, offset + limit)
        if offset >= end:
            return []

        keys = [self._message_key(session_id, agent_id, index) for index in range(offset, end)]
        try:
            result = self._client.batch_read(keys)
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error reading messages: {error}") from error

        by_key = {item.key[2]: item for item in result.batch_records}
        messages: list[SessionMessage] = []
        for index in range(offset, end):
            item = by_key.get(self._message_key(session_id, agent_id, index)[2])
            if item is not None and item.result == 0:
                messages.append(SessionMessage.from_dict(json.loads(item.record[2][_DATA_BIN])))
        return messages

    # -- Multi-agent --

    def create_multi_agent(self, session_id: str, multi_agent: MultiAgentBase, **kwargs: Any) -> None:
        """Create a new multi-agent state in Aerospike."""
        multi_agent_id = validate_identifier(multi_agent.id, "multi-agent")
        data = json.dumps(multi_agent.serialize_state(), ensure_ascii=False)
        try:
            self._client.put(self._multi_agent_key(session_id, multi_agent_id), {_DATA_BIN: data})
            self._client.operate(
                self._session_key(session_id), [list_ops.list_append(_MULTI_AGENT_IDS_BIN, multi_agent_id)]
            )
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error creating multi-agent {multi_agent_id}: {error}") from error

    def read_multi_agent(self, session_id: str, multi_agent_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """Read multi-agent state from Aerospike."""
        bins = self._get_bins(
            self._multi_agent_key(session_id, multi_agent_id), context=f"reading multi-agent {multi_agent_id}"
        )
        if bins is None:
            return None
        return json.loads(bins[_DATA_BIN])  # type: ignore[no-any-return]

    def update_multi_agent(self, session_id: str, multi_agent: MultiAgentBase, **kwargs: Any) -> None:
        """Update multi-agent state in Aerospike."""
        previous_state = self.read_multi_agent(session_id=session_id, multi_agent_id=multi_agent.id)
        if previous_state is None:
            raise SessionException(f"MultiAgent state {multi_agent.id} in session {session_id} does not exist")

        data = json.dumps(multi_agent.serialize_state(), ensure_ascii=False)
        try:
            self._client.put(self._multi_agent_key(session_id, multi_agent.id), {_DATA_BIN: data})
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error updating multi-agent {multi_agent.id}: {error}") from error

    # -- Shared helpers --

    def _get_bins(self, key: tuple[str, str, str], *, context: str) -> dict[str, Any] | None:
        """Read a record's bins, returning None if it does not exist.

        Raises:
            SessionException: If the Aerospike read fails for a reason other than a
                missing record.
        """
        try:
            _, _, bins = self._client.get(key)
        except aerospike_exception.RecordNotFound:
            return None
        except aerospike_exception.AerospikeError as error:
            raise SessionException(f"Aerospike error {context}: {error}") from error
        return bins  # type: ignore[no-any-return]
