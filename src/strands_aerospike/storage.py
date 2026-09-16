"""Aerospike-backed implementation of the unified ``strands.storage.Storage`` protocol."""

from __future__ import annotations

import asyncio
import builtins
import re

import aerospike
from aerospike import exception as aerospike_exception
from aerospike_helpers import expressions as exp
from aerospike_helpers.batch import records as batch_records
from aerospike_helpers.operations import operations as ops
from strands.storage.storage import StorageSearchResult
from strands.types.exceptions import StorageError

from ._client import get_client

# Conservative default chunk threshold, comfortably under Aerospike's common
# 1 MiB write-block-size default (leaving headroom for the "k"/"n" bins and
# per-record overhead). Override via ``max_value_size`` for clusters configured
# with a larger write-block-size.
_DEFAULT_MAX_VALUE_SIZE = 900_000

_KEY_BIN = "k"
_VALUE_BIN = "v"
_CHUNK_COUNT_BIN = "n"


def _chunk_set(set_name: str) -> str:
    """Return the companion set name used for overflow chunk records.

    Kept as a distinct set (rather than a key suffix in the same set) so chunk
    records never collide with, or get returned by, a prefix scan of the primary
    set -- no key-escaping scheme is needed to tell the two apart.
    """
    return f"{set_name}_chunks"


def _chunk_key(key: str, index: int) -> str:
    """Return the chunk-set key for the given logical key and chunk index."""
    return f"{key}#{index}"


class AerospikeStorage:
    """A :class:`~strands.storage.storage.Storage` backend persisting bytes in Aerospike.

    Each key maps to one record in ``set`` with a ``k`` bin (the opaque string key,
    stored explicitly so prefix listing can filter on it) and either a ``v`` bin
    (the raw bytes, when they fit under ``max_value_size``) or an ``n`` bin (a chunk
    count) when the value was split across companion records in a ``<set>_chunks``
    set. Listing scans ``set`` with a server-side regex filter expression on ``k``
    rather than fetching every record and filtering in the client.

    Example:
        ```python
        from strands_aerospike import AerospikeStorage

        storage = AerospikeStorage(namespace="test")
        await storage.write("session/abc/state.json", data)
        ```
    """

    def __init__(
        self,
        namespace: str,
        *,
        set: str = "strands_storage",
        hosts: list[tuple[str, int]] | None = None,
        client: aerospike.Client | None = None,
        max_value_size: int = _DEFAULT_MAX_VALUE_SIZE,
        ttl: int | None = None,
    ) -> None:
        """Initialize Aerospike-backed storage.

        Args:
            namespace: Aerospike namespace to store records in.
            set: Aerospike set name for primary records. Chunked values also use
                a companion set named ``f"{set}_chunks"``.
            hosts: Cluster seed addresses as ``(host, port)`` tuples, used only when
                ``client`` is not given. Defaults to ``[("127.0.0.1", 3000)]``.
            client: An existing, connected ``aerospike.Client`` to use. When omitted,
                a process-wide client is lazily created and shared across all
                ``strands_aerospike`` components constructed with the same ``hosts``.
            max_value_size: Values at or under this many bytes are stored inline in
                a single record; larger values are split across chunk records. Keep
                this under the cluster's configured write-block-size.
            ttl: Optional record time-to-live in seconds, applied to every write.
                When omitted, the namespace's configured default-ttl applies.
        """
        self._namespace = namespace
        self._set = set
        self._chunk_set = _chunk_set(set)
        self._client = client if client is not None else get_client(hosts)
        self._max_value_size = max_value_size
        self._ttl = ttl

    def _meta(self) -> dict[str, int]:
        return {"ttl": self._ttl} if self._ttl is not None else {}

    def _primary_key(self, key: str) -> tuple[str, str, str]:
        return (self._namespace, self._set, key)

    def _chunk_record_key(self, key: str, index: int) -> tuple[str, str, str]:
        return (self._namespace, self._chunk_set, _chunk_key(key, index))

    async def write(self, key: str, data: bytes) -> None:
        """Store data under key, overwriting any existing value (and stale chunks).

        Args:
            key: Opaque string key identifying the value.
            data: Raw bytes to persist.

        Raises:
            StorageError: If the write fails.
        """
        if not key:
            raise StorageError("Storage key must not be empty")
        await asyncio.to_thread(self._write_sync, key, data)

    def _write_sync(self, key: str, data: bytes) -> None:
        try:
            # Look up any stale chunks from a previous chunked write, but don't remove
            # them yet: deleting first and writing second leaves a window where a
            # failed/partial new write has already destroyed the old value's chunks,
            # turning a would-be write failure into permanent data loss on read.
            # Removing them only after the new write succeeds keeps the old value
            # intact (and readable) until the new one is durably in place.
            previous_chunk_count = self._get_previous_chunk_count(key)
            if len(data) <= self._max_value_size:
                self._client.put(
                    self._primary_key(key),
                    {_KEY_BIN: key, _VALUE_BIN: bytes(data), _CHUNK_COUNT_BIN: aerospike.null()},
                    meta=self._meta(),
                )
            else:
                self._write_chunked(key, data, meta=self._meta())
            if previous_chunk_count:
                self._remove_chunks(key, previous_chunk_count)
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Failed to write '{key}'") from error

    def _get_previous_chunk_count(self, key: str) -> int | None:
        """Return key's existing chunk count, if any, without removing anything."""
        try:
            _, _, bins = self._client.get(self._primary_key(key))
        except aerospike_exception.RecordNotFound:
            return None
        return bins.get(_CHUNK_COUNT_BIN)  # type: ignore[no-any-return]

    def _remove_chunks(self, key: str, chunk_count: int) -> None:
        chunk_keys = [self._chunk_record_key(key, index) for index in range(chunk_count)]
        self._client.batch_remove(chunk_keys)

    def _clear_existing_chunks(self, key: str) -> None:
        """Remove any chunk records left over from a previous chunked write under key."""
        previous_chunk_count = self._get_previous_chunk_count(key)
        if previous_chunk_count:
            self._remove_chunks(key, previous_chunk_count)

    def _write_chunked(self, key: str, data: bytes, *, meta: dict[str, int]) -> None:
        chunks = [data[i : i + self._max_value_size] for i in range(0, len(data), self._max_value_size)]
        writes = [
            batch_records.Write(
                self._chunk_record_key(key, index), [ops.write(_VALUE_BIN, bytes(chunk))], meta=meta
            )
            for index, chunk in enumerate(chunks)
        ]
        writes.append(
            batch_records.Write(
                self._primary_key(key),
                [
                    ops.write(_KEY_BIN, key),
                    ops.write(_CHUNK_COUNT_BIN, len(chunks)),
                    ops.write(_VALUE_BIN, aerospike.null()),
                ],
                meta=meta,
            )
        )
        result = self._client.batch_write(batch_records.BatchRecords(writes))
        _raise_on_batch_failure(result, context=f"writing chunks for '{key}'")

    async def read(self, key: str) -> bytes | None:
        """Retrieve the bytes previously stored under key.

        Args:
            key: The key to read.

        Returns:
            The stored bytes, or None if no value exists for key.

        Raises:
            StorageError: If the read fails for a reason other than a missing key.
        """
        return await asyncio.to_thread(self._read_sync, key)

    def _read_sync(self, key: str) -> bytes | None:
        try:
            _, _, bins = self._client.get(self._primary_key(key))
        except aerospike_exception.RecordNotFound:
            return None
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Failed to read '{key}'") from error

        if _VALUE_BIN in bins:
            return bytes(bins[_VALUE_BIN])

        chunk_count = bins.get(_CHUNK_COUNT_BIN)
        if chunk_count is None:
            raise StorageError(f"Record for '{key}' has neither a value nor a chunk count")

        try:
            chunk_keys = [self._chunk_record_key(key, index) for index in range(chunk_count)]
            result = self._client.batch_read(chunk_keys)
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Failed to read chunks for '{key}'") from error

        _raise_on_batch_failure(result, context=f"reading chunks for '{key}'")
        # Reassemble by explicit chunk index rather than sorting the batch result by key
        # string -- lexicographic order would place "key#10" before "key#2".
        by_chunk_key = {item.key[2]: item for item in result.batch_records}
        return b"".join(
            bytes(by_chunk_key[_chunk_key(key, index)].record[2][_VALUE_BIN]) for index in range(chunk_count)
        )

    async def delete(self, key: str) -> None:
        """Delete the value stored under key, including any chunk records. A no-op if absent.

        Args:
            key: The key to delete.

        Raises:
            StorageError: If the delete fails.
        """
        await asyncio.to_thread(self._delete_sync, key)

    def _delete_sync(self, key: str) -> None:
        try:
            previous_chunk_count = self._get_previous_chunk_count(key)
            keys = [self._primary_key(key)]
            if previous_chunk_count:
                keys.extend(self._chunk_record_key(key, index) for index in range(previous_chunk_count))
            self._client.batch_remove(keys)
        except aerospike_exception.RecordNotFound:
            pass
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Failed to delete '{key}'") from error

    async def list(self, query: str) -> builtins.list[str]:
        """List keys matching the given prefix query.

        Filters server-side with a regex expression on the ``k`` bin during a scan
        of the primary set, so unmatched records are never shipped to the client.

        Args:
            query: A string prefix to match. An empty string matches every key.

        Returns:
            The matching keys, sorted ascending.

        Raises:
            StorageError: If the listing fails.
        """
        return await asyncio.to_thread(self._list_sync, query)

    def _list_sync(self, query: str) -> builtins.list[str]:
        pattern = f"^{re.escape(query)}"
        try:
            expression = exp.CmpRegex(aerospike.REGEX_NONE, pattern, exp.StrBin(_KEY_BIN)).compile()
            matched: builtins.list[str] = []
            scan = self._client.scan(self._namespace, self._set)
            scan.foreach(lambda record: matched.append(record[2][_KEY_BIN]), {"expressions": expression})
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Failed to list keys with prefix '{query}'") from error
        return sorted(matched)

    async def search(self, query: str) -> builtins.list[StorageSearchResult]:
        """Search stored content by keyword token-overlap scoring.

        Fetches every key's content and scores it against the query, matching the
        semantics of the ``Storage`` protocol's own default strategy. Backends with
        large datasets should scope searches with a namespace, or a future release
        may add an index-backed strategy; see :class:`AerospikeMemoryStore` for a
        secondary-index-backed alternative for the memory-store use case.

        Args:
            query: A natural-language query.

        Returns:
            Matched keys with relevance scores, ranked best-first.
        """
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        keys = await self.list("")
        results: builtins.list[StorageSearchResult] = []
        for key in keys:
            data = await self.read(key)
            if data is None:
                continue
            content = data.decode("utf-8", errors="replace")
            overlap = len(query_tokens & _tokenize(content))
            if overlap > 0:
                results.append(StorageSearchResult(key=key, score=float(overlap), data=data))

        results.sort(key=lambda result: result.score, reverse=True)
        return results


def _tokenize(text: str) -> set[str]:
    """Lowercase and split text into word tokens for keyword-overlap scoring."""
    return {token for token in re.split(r"\W+", text.lower()) if token}


def _raise_on_batch_failure(result: batch_records.BatchRecords, *, context: str) -> None:
    """Raise StorageError if any entry in a batch result failed.

    The client's batch APIs return success at the batch level even when
    individual keys failed -- each entry's own result code must be checked.
    """
    failed = [item for item in result.batch_records if item.result != 0]
    if failed:
        codes = {item.result for item in failed}
        raise StorageError(f"Batch operation failed while {context}: result codes {sorted(codes)}")
