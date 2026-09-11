"""Backend construction and lifecycle management for the benchmark suite.

Only backends that actually exist in the host project (``strands-agents``) are
benchmarked alongside ``strands-aerospike``'s own implementations, per each
interface identified in ``docs/DESIGN.md``:

- ``Storage``: ``InMemoryStorage``, ``LocalFileStorage``, ``S3Storage`` (against a
  local moto server -- see the module docstring on ``moto_s3_server`` for why).
- ``SessionRepository`` (via ``RepositorySessionManager``): ``FileSessionManager``,
  ``S3SessionManager`` (moto).
- ``MemoryStore``: ``FileMemoryStore``. ``BedrockKnowledgeBase`` is the only other
  in-repo ``MemoryStore`` and requires a live AWS Bedrock Knowledge Base -- it
  cannot be exercised locally, so it is excluded rather than faked.

Redis/Valkey, PostgreSQL, and MongoDB implementations of ``Storage``,
``SessionRepository``, and ``MemoryStore`` were explicitly checked for in
``strands-agents`` and do not exist -- see ``reports/REPORT.md``'s environment
section for that check. This module's comparison set is therefore everything
``strands-agents`` ships for these interfaces, not a partial selection.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import aerospike
import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from strands.session.file_session_manager import FileSessionManager  # noqa: E402
from strands.session.s3_session_manager import S3SessionManager  # noqa: E402
from strands.storage.in_memory_storage import InMemoryStorage  # noqa: E402
from strands.storage.local_file_storage import LocalFileStorage  # noqa: E402
from strands.storage.s3_storage import S3Storage  # noqa: E402
from strands.vended_memory_stores.file_memory_store.store import FileMemoryStore  # noqa: E402

from strands_aerospike import AerospikeMemoryStore, AerospikeSessionManager, AerospikeStorage  # noqa: E402

AEROSPIKE_HOSTS = [("127.0.0.1", 3000)]
AEROSPIKE_NAMESPACE = "test"


def connect_aerospike() -> aerospike.Client:
    """Connect to the local Aerospike CE container used by ``scripts/start_aerospike_ce.sh``."""
    return aerospike.client({"hosts": AEROSPIKE_HOSTS}).connect()


def truncate_aerospike_set(client: aerospike.Client, set_name: str) -> None:
    """Truncate a benchmark set between runs.

    Truncating a set that has never held any records is a normal no-op on the
    server, so a failure here almost always means something else went wrong
    (e.g. a transient overload after a preceding bulk-tier run). Swallowing
    that silently lets the next scenario seed on top of leftover keys from a
    prior run, corrupting its scale/key count with no warning -- so print
    instead of ignoring, even though seeding proceeds either way.
    """
    try:
        client.truncate(AEROSPIKE_NAMESPACE, set_name, 0)
    except aerospike.exception.AerospikeError as error:
        print(f"Warning: failed to truncate set '{set_name}': {error}", flush=True)


class MotoS3(NamedTuple):
    """A running local S3-compatible server and the endpoint it listens on."""

    endpoint: str


@contextmanager
def moto_s3_server() -> Generator[MotoS3]:
    """Start a real local S3-API HTTP server (moto) for the ``S3Storage``/``S3SessionManager`` benchmarks.

    This is not real AWS S3 -- see ``reports/REPORT.md``'s environment section for
    the caveat that follows from that. It is deliberately *not* moto's
    ``mock_aws`` decorator, which patches botocore in-process and never sends an
    HTTP request at all: that would make ``S3Storage`` look as fast as
    ``InMemoryStorage``, which measures nothing real. ``ThreadedMotoServer`` is a
    real WSGI server bound to a real local port, so every S3 call in this
    benchmark pays real HTTP + serialization overhead, the same way it would
    against any other S3-compatible endpoint (AWS S3 itself, MinIO, LocalStack)
    -- just without real network latency, which is exactly the caveat to state.

    ``S3Storage`` has no ``endpoint_url`` constructor argument (unlike
    ``S3SessionManager``, which takes one directly), so this uses the
    botocore-supported ``AWS_ENDPOINT_URL_S3`` environment variable instead --
    a supported botocore mechanism, not a patch to the host's ``S3Storage`` class.
    """
    from moto.server import ThreadedMotoServer

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    _, port = server._server.server_address
    endpoint = f"http://127.0.0.1:{port}"

    previous_env = {
        key: os.environ.get(key)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL_S3")
    }
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_ENDPOINT_URL_S3"] = endpoint
    try:
        yield MotoS3(endpoint=endpoint)
    finally:
        server.stop()
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_bucket(name: str) -> None:
    """Create a fresh S3 bucket against the moto endpoint currently in the environment."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=name)


def storage_backends(tmp_root: Path, aero_client: aerospike.Client, run_id: str) -> dict[str, object]:
    """Construct one fresh ``Storage``-interface backend instance per candidate, for one run."""
    truncate_aerospike_set(aero_client, "bench_storage")
    truncate_aerospike_set(aero_client, "bench_storage_chunks")

    file_dir = tmp_root / f"storage_file_{run_id}"
    file_dir.mkdir(parents=True, exist_ok=True)

    bucket = f"bench-storage-{run_id}"
    make_bucket(bucket)

    return {
        "AerospikeStorage": AerospikeStorage(namespace=AEROSPIKE_NAMESPACE, set="bench_storage", client=aero_client),
        "InMemoryStorage": InMemoryStorage(),
        "LocalFileStorage": LocalFileStorage(base_dir=str(file_dir)),
        "S3Storage": S3Storage(bucket=bucket, region_name="us-east-1"),
    }


def session_backend_factories(
    tmp_root: Path, aero_client: aerospike.Client, run_id: str
) -> dict[str, Callable[[str], object]]:
    """Return one ``session_id -> fresh SessionRepository manager`` factory per candidate, for one run.

    A factory (rather than one ready instance) is needed because the delete-session
    benchmark must construct a brand-new session per timed repetition (deleting is
    destructive), while every other op benchmarks against one shared, pre-seeded
    session -- both cases just call the same factory with a different session id.
    """
    truncate_aerospike_set(aero_client, "bench_sessions")

    file_dir = tmp_root / f"session_file_{run_id}"
    file_dir.mkdir(parents=True, exist_ok=True)

    bucket = f"bench-sessions-{run_id}"
    make_bucket(bucket)

    return {
        "AerospikeSessionManager": lambda session_id: AerospikeSessionManager(
            session_id=session_id, namespace=AEROSPIKE_NAMESPACE, set="bench_sessions", client=aero_client
        ),
        "FileSessionManager": lambda session_id: FileSessionManager(session_id=session_id, storage_dir=str(file_dir)),
        "S3SessionManager": lambda session_id: S3SessionManager(
            session_id=session_id, bucket=bucket, region_name="us-east-1"
        ),
    }


def memory_backends(tmp_root: Path, aero_client: aerospike.Client, run_id: str) -> dict[str, object]:
    """Construct one fresh ``MemoryStore``-interface instance per candidate, for one run."""
    truncate_aerospike_set(aero_client, "bench_memory")

    file_dir = tmp_root / f"memory_file_{run_id}"
    file_dir.mkdir(parents=True, exist_ok=True)

    return {
        "AerospikeMemoryStore": AerospikeMemoryStore(
            name="bench", namespace=AEROSPIKE_NAMESPACE, set="bench_memory", client=aero_client
        ),
        "FileMemoryStore": FileMemoryStore(name="bench", storage=LocalFileStorage(base_dir=str(file_dir))),
    }
