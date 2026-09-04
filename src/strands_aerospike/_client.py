"""Process-wide Aerospike client factory.

The Aerospike client is thread-safe and holds its own connection pool and cluster
state, so a process should hold exactly one per unique cluster config rather than
opening a new connection per component or per call. Callers may also construct and
inject their own ``aerospike.Client`` (see each component's ``client`` argument) when
they want to own the lifecycle themselves.
"""

from __future__ import annotations

import threading
from typing import Any

import aerospike

_clients: dict[tuple[Any, ...], aerospike.Client] = {}
_lock = threading.Lock()


def _config_key(hosts: list[tuple[str, int]], policies: dict[str, Any] | None) -> tuple[Any, ...]:
    """Build a hashable cache key from client configuration."""
    policies_key = tuple(sorted(policies.items())) if policies else ()
    return (tuple(hosts), policies_key)


def get_client(
    hosts: list[tuple[str, int]] | None = None,
    *,
    policies: dict[str, Any] | None = None,
) -> aerospike.Client:
    """Return a process-wide Aerospike client for the given cluster config.

    Lazily creates and memoizes one connected client per unique ``(hosts, policies)``
    pair, so repeated calls with the same config reuse a single connection pool instead
    of opening a new one. Callers who need distinct lifecycles (e.g. tests that want to
    close a client independently) should construct their own ``aerospike.client(...)``
    and inject it via each component's ``client`` argument instead of using this factory.

    Args:
        hosts: Cluster seed addresses as ``(host, port)`` tuples. Defaults to
            ``[("127.0.0.1", 3000)]``.
        policies: Optional client-level default policies (e.g. ``{"read": {...}}``).

    Returns:
        A connected ``aerospike.Client``.
    """
    resolved_hosts = hosts if hosts is not None else [("127.0.0.1", 3000)]
    key = _config_key(resolved_hosts, policies)

    with _lock:
        client = _clients.get(key)
        if client is not None:
            return client

        config: dict[str, Any] = {"hosts": resolved_hosts}
        if policies is not None:
            config["policies"] = policies

        client = aerospike.client(config).connect()
        _clients[key] = client
        return client
