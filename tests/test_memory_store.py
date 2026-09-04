"""Integration tests for AerospikeMemoryStore against a real Aerospike server."""

from __future__ import annotations

import aerospike
import pytest

from strands_aerospike import AerospikeMemoryStore


@pytest.fixture
async def store(client: aerospike.Client, namespace: str) -> AerospikeMemoryStore:
    memory_store = AerospikeMemoryStore(name="test-memory", namespace=namespace, client=client)
    await memory_store.initialize()
    return memory_store


async def test_add_then_search_finds_entry(store: AerospikeMemoryStore) -> None:
    await store.add("# User preference\nPrefers dark mode in every app.")

    results = await store.search("dark mode preference")

    assert len(results) == 1
    assert "dark mode" in results[0].content


async def test_add_with_same_heading_appends_facts(store: AerospikeMemoryStore) -> None:
    await store.add("# Project setup\nUses Python 3.12.")
    await store.add("# Project setup\nUses uv for dependency management.")

    results = await store.search("python uv dependency")

    assert len(results) == 1
    assert "Python 3.12" in results[0].content
    assert "uv for dependency management" in results[0].content


async def test_search_scopes_to_store_name(client: aerospike.Client, namespace: str) -> None:
    store_a = AerospikeMemoryStore(name="store-a", namespace=namespace, client=client)
    store_b = AerospikeMemoryStore(name="store-b", namespace=namespace, client=client)
    await store_a.initialize()
    await store_b.initialize()

    await store_a.add("# Shared topic\nfact only in store a")
    await store_b.add("# Shared topic\nfact only in store b")

    results_a = await store_a.search("shared topic fact")

    assert len(results_a) == 1
    assert "store a" in results_a[0].content
    assert "store b" not in results_a[0].content


async def test_search_with_no_matches_returns_empty(store: AerospikeMemoryStore) -> None:
    await store.add("# Unrelated\nnothing relevant here")

    assert await store.search("completely different query terms") == []


async def test_search_rejects_invalid_max_results(store: AerospikeMemoryStore) -> None:
    with pytest.raises(ValueError, match="max_search_results"):
        await store.search("anything", {"max_search_results": 0})


async def test_add_stores_metadata(store: AerospikeMemoryStore) -> None:
    await store.add("# Tagged fact\nsome durable fact", metadata={"source": "conversation-1"})

    results = await store.search("tagged fact durable")

    assert results[0].metadata == {"source": "conversation-1"}
