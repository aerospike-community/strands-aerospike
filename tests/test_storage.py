"""Integration tests for AerospikeStorage against a real Aerospike server."""

from __future__ import annotations

import aerospike
import pytest

from strands_aerospike import AerospikeStorage


@pytest.fixture
def storage(client: aerospike.Client, namespace: str) -> AerospikeStorage:
    return AerospikeStorage(namespace, client=client)


async def test_write_read_roundtrip(storage: AerospikeStorage) -> None:
    await storage.write("greeting", b"hello world")
    assert await storage.read("greeting") == b"hello world"


async def test_read_missing_key_returns_none(storage: AerospikeStorage) -> None:
    assert await storage.read("does-not-exist") is None


async def test_write_overwrites_existing_value(storage: AerospikeStorage) -> None:
    await storage.write("key", b"first")
    await storage.write("key", b"second")
    assert await storage.read("key") == b"second"


async def test_delete_removes_value(storage: AerospikeStorage) -> None:
    await storage.write("key", b"value")
    await storage.delete("key")
    assert await storage.read("key") is None


async def test_delete_missing_key_is_noop(storage: AerospikeStorage) -> None:
    await storage.delete("never-existed")


async def test_list_matches_prefix(storage: AerospikeStorage) -> None:
    await storage.write("session/a/state.json", b"1")
    await storage.write("session/b/state.json", b"2")
    await storage.write("other/c/state.json", b"3")

    keys = await storage.list("session/")

    assert keys == ["session/a/state.json", "session/b/state.json"]


async def test_list_empty_prefix_matches_everything(storage: AerospikeStorage) -> None:
    await storage.write("a", b"1")
    await storage.write("b", b"2")

    assert await storage.list("") == ["a", "b"]


async def test_chunked_write_and_read_roundtrip(client: aerospike.Client, namespace: str) -> None:
    storage = AerospikeStorage(namespace, client=client, max_value_size=10)
    large_value = b"0123456789" * 5  # 50 bytes, well over the 10-byte threshold

    await storage.write("large", large_value)

    assert await storage.read("large") == large_value


async def test_overwriting_chunked_value_with_small_value_clears_chunks(
    client: aerospike.Client, namespace: str
) -> None:
    storage = AerospikeStorage(namespace, client=client, max_value_size=10)
    await storage.write("key", b"0123456789" * 5)

    await storage.write("key", b"small")

    assert await storage.read("key") == b"small"
    # The stale chunk records must not resurrect on a later read.
    chunk_key = ("test", "strands_storage_chunks", "key#0")
    with pytest.raises(aerospike.exception.RecordNotFound):
        client.get(chunk_key)


async def test_search_ranks_by_token_overlap(storage: AerospikeStorage) -> None:
    await storage.write("a", b"the quick brown dog")  # matches "quick" only
    await storage.write("b", b"the quick brown fox jumps")  # matches "quick" and "fox"
    await storage.write("c", b"completely unrelated content")  # matches neither

    results = await storage.search("quick fox")

    assert [result.key for result in results] == ["b", "a"]
    assert results[0].score > results[1].score
