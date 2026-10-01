"""Shared fixtures for the integration test suite.

Every test in this suite requires a real Aerospike server (see
``scripts/start_aerospike_ce.sh``). The session-scoped ``client`` fixture connects
once; if the connection fails, the whole session is skipped so contributors without
the container running still get a clean (skipped) run rather than a hard failure.
"""

from __future__ import annotations

from collections.abc import Iterator

import aerospike
import pytest

_TEST_HOSTS = [("127.0.0.1", 3000)]
_TEST_NAMESPACE = "test"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every collected test as an integration test."""
    marker = pytest.mark.integration
    for item in items:
        item.add_marker(marker)


@pytest.fixture(scope="session")
def client() -> Iterator[aerospike.Client]:
    """A connected Aerospike client, or a session-wide skip if unreachable."""
    try:
        connected = aerospike.client({"hosts": _TEST_HOSTS}).connect()
    except aerospike.exception.ClientError as error:
        pytest.skip(f"Aerospike server not reachable at {_TEST_HOSTS}: {error}")
        return
    yield connected
    connected.close()


@pytest.fixture
def namespace() -> str:
    """The Aerospike namespace configured in the test server (see the docker script)."""
    return _TEST_NAMESPACE


@pytest.fixture(autouse=True)
def _truncate_sets(client: aerospike.Client) -> Iterator[None]:
    """Truncate every set this suite writes to before each test, for isolation."""
    yield
    for set_name in (
        "strands_storage",
        "strands_storage_chunks",
        "strands_memory",
        "strands_sessions",
    ):
        try:
            client.truncate(_TEST_NAMESPACE, set_name, 0)
        except aerospike.exception.AerospikeError:
            pass
