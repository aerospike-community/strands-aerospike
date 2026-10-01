"""Aerospike backends for the Strands Agents SDK.

Provides Aerospike-backed implementations of Strands' three pluggable persistence
interfaces: :class:`~strands.storage.storage.Storage`,
:class:`~strands.session.session_manager.SessionManager` (via
:class:`~strands.session.session_repository.SessionRepository`), and
:class:`~strands.memory.types.MemoryStore`.

Example:
    ```python
    from strands import Agent
    from strands_aerospike import AerospikeSessionManager, AerospikeStorage

    agent = Agent(
        storage=AerospikeStorage(namespace="test"),
        session_manager=AerospikeSessionManager(session_id="my-session", namespace="test"),
    )
    ```
"""

from .memory_store import AerospikeMemoryStore
from .session_manager import AerospikeSessionManager
from .storage import AerospikeStorage

__all__ = [
    "AerospikeMemoryStore",
    "AerospikeSessionManager",
    "AerospikeStorage",
]
