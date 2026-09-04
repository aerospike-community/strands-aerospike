"""Per-interface seeding and operation timing.

Each ``run_*_scenario`` function seeds one backend with exactly ``scale``
pre-existing records (fixture cost, excluded from all timing per the
methodology), then times each op the interface actually exposes. Fairness
requires identical data shapes and call patterns across backends within a
scenario -- only the backend instance changes.
"""

from __future__ import annotations

import asyncio
import random
import string
import time
from collections.abc import Callable
from typing import Any

from harness import Samples, _ProgressBar, time_async, time_sync
from strands.types.exceptions import StorageError

_WORDS = (
    "agent session memory storage benchmark aerospike latency throughput "
    "cluster record bin index namespace batch query token search vector"
).split()


def _lorem(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(n_words))


def _random_suffix(rng: random.Random, length: int = 8) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(length))


async def _write_with_retry(storage: Any, key: str, value: bytes, *, max_attempts: int = 6) -> None:
    """Retry a seeding write with exponential backoff on transient device-overload errors.

    Aerospike's device storage-engine has a write-cache queue that a tight sequential loop of
    large (bulk-tier) writes can outrun, raising StorageError. Seeding isn't timed, so backing
    off here doesn't affect any measured latency -- it only makes bulk-tier seeding reliable.
    """
    delay = 0.05
    for attempt in range(max_attempts):
        try:
            await storage.write(key, value)
            return
        except StorageError:
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2


class _RateLimiter:
    """Caps cumulative throughput during bulk-tier seeding so Aerospike's device write-flush queue doesn't overflow.

    Without this, a tight sequential loop of large writes can build up backlog in the device's
    write-flush queue that hasn't fully drained by the time the timed measurement phase starts
    right after seeding, causing that phase to hit the same overload error. Measured ~149MB/s raw
    sustained device throughput on this host; capping seeding well below that (not just retrying
    overflow after the fact) keeps the queue drained by the time seeding finishes, instead of
    merely tolerating transient overflow during seeding itself.
    """

    def __init__(self, max_bytes_per_sec: float) -> None:
        self._max_bytes_per_sec = max_bytes_per_sec
        self._start = time.monotonic()
        self._bytes_sent = 0

    async def throttle(self, n_bytes: int) -> None:
        self._bytes_sent += n_bytes
        target_elapsed = self._bytes_sent / self._max_bytes_per_sec
        actual_elapsed = time.monotonic() - self._start
        if actual_elapsed < target_elapsed:
            await asyncio.sleep(target_elapsed - actual_elapsed)


def _run_async_op(
    *,
    interface: str,
    name: str,
    op_name: str,
    scale: int,
    fn: Callable[[int], Any],
    reps: int,
    warmup: int,
    label_width: int = 10,
) -> Samples:
    """Time one async op with a live progress bar, then print its final mean/elapsed."""
    bar = _ProgressBar(total=warmup + reps, prefix=f"  [{name}] {op_name}")

    async def with_progress(i: int) -> None:
        await fn(i)
        bar.update()

    op_start = time.perf_counter()
    samples = asyncio.run(time_async(with_progress, reps=reps, warmup=warmup))
    bar.clear()
    result = Samples(interface=interface, operation=op_name, backend=name, scale=scale, latencies_s=samples)
    print(
        f"  [{name}] {op_name:{label_width}s} mean={result.stats['mean_ms']:.3f}ms "
        f"({time.perf_counter() - op_start:.1f}s)",
        flush=True,
    )
    return result


def _run_sync_op(
    *,
    interface: str,
    name: str,
    op_name: str,
    scale: int,
    fn: Callable[[int], None],
    reps: int,
    warmup: int,
    label_width: int = 16,
) -> Samples:
    """Time one sync op with a live progress bar, then print its final mean/elapsed."""
    bar = _ProgressBar(total=warmup + reps, prefix=f"  [{name}] {op_name}")

    def with_progress(i: int) -> None:
        fn(i)
        bar.update()

    op_start = time.perf_counter()
    samples = time_sync(with_progress, reps=reps, warmup=warmup)
    bar.clear()
    result = Samples(interface=interface, operation=op_name, backend=name, scale=scale, latencies_s=samples)
    print(
        f"  [{name}] {op_name:{label_width}s} mean={result.stats['mean_ms']:.3f}ms "
        f"({time.perf_counter() - op_start:.1f}s)",
        flush=True,
    )
    return result


# -- Storage ------------------------------------------------------------------


def run_storage_scenario(
    backends: dict[str, Any],
    *,
    scale: int,
    reps: int,
    warmup: int,
    search_reps: int | None,
    value_size_bytes: int | None = None,
    list_reps: int | None = None,
) -> list[Samples]:
    """Seed and time write/read/delete/list (and, if requested, search) for ``Storage`` backends.

    ``value_size_bytes``, if given, replaces the default ~1.2KB lorem-text value with a
    high-entropy blob of exactly that size (used by the bulk-scale tier to reach a target
    data volume with far fewer keys than the default value size would need).
    ``list_reps``, if given, overrides ``reps``/``warmup`` for the ``list`` op only (used by
    the bulk-scale tier, where a full-set scan reads the whole dataset off storage per call).
    """
    rng = random.Random(scale)
    if value_size_bytes is None:
        value = _lorem(rng, 200).encode("utf-8")  # ~1.2KB, representative of a small JSON snapshot/plugin blob
    else:
        value = rng.randbytes(value_size_bytes)  # bulk tier: opaque blob (e.g. a larger state snapshot)
    results: list[Samples] = []

    for name, storage in backends.items():
        keys = [f"key-{i:07d}" for i in range(scale)]

        seed_bar = _ProgressBar(total=scale + warmup + reps, prefix=f"  [{name}] seeding")

        async def seed(storage: Any = storage, keys: list[str] = keys, bar: _ProgressBar = seed_bar) -> None:
            limiter = _RateLimiter(max_bytes_per_sec=64 * 1024 * 1024) if value_size_bytes is not None else None
            for key in keys:
                await _write_with_retry(storage, key, value)
                bar.update()
                if limiter is not None:
                    await limiter.throttle(len(value))
            for j in range(warmup + reps):
                await _write_with_retry(storage, f"delete-{j:07d}", value)
                bar.update()
                if limiter is not None:
                    await limiter.throttle(len(value))

        seed_start = time.perf_counter()
        asyncio.run(seed())
        seed_bar.clear()
        print(f"  [{name}] seeded {scale} key(s) in {time.perf_counter() - seed_start:.1f}s", flush=True)

        async def do_write(i: int, storage: Any = storage) -> None:
            await storage.write(f"write-{i:07d}", value)

        async def do_read(i: int, storage: Any = storage, keys: list[str] = keys) -> None:
            await storage.read(keys[i % scale])

        async def do_delete(i: int, storage: Any = storage) -> None:
            await storage.delete(f"delete-{i:07d}")

        async def do_list(_i: int, storage: Any = storage) -> None:
            await storage.list("key-")

        for op_name, fn in (("write", do_write), ("read", do_read), ("delete", do_delete)):
            results.append(
                _run_async_op(
                    interface="Storage", name=name, op_name=op_name, scale=scale, fn=fn, reps=reps, warmup=warmup
                )
            )

        # `list` gets its own rep count: at the bulk tier, a full-set scan-with-expression-filter
        # still reads every record's bins off the storage device before evaluating the filter, so
        # repeating it 100x over a 10GB dataset would mean scanning ~1TB. list_reps lets the driver
        # shrink this specifically for that tier without affecting write/read/delete above.
        effective_list_reps = reps if list_reps is None else list_reps
        effective_list_warmup = warmup if list_reps is None else min(warmup, effective_list_reps)
        results.append(
            _run_async_op(
                interface="Storage",
                name=name,
                op_name="list",
                scale=scale,
                fn=do_list,
                reps=effective_list_reps,
                warmup=effective_list_warmup,
            )
        )

        if search_reps is not None:

            async def do_search(_i: int, storage: Any = storage) -> None:
                await storage.search("benchmark")

            results.append(
                _run_async_op(
                    interface="Storage",
                    name=name,
                    op_name="search",
                    scale=scale,
                    fn=do_search,
                    reps=search_reps,
                    warmup=min(5, search_reps),
                )
            )

    return results


# -- MemoryStore ----------------------------------------------------------------


def run_memory_scenario(
    backends: dict[str, Any], *, scale: int, reps: int, warmup: int, content_extra_bytes: int = 0
) -> list[Samples]:
    """Seed and time add/search/search_selective for ``MemoryStore`` backends.

    ``content_extra_bytes``, if nonzero, pads every seeded/added entry's content with extra
    lorem text of roughly that many bytes (used by the bulk-scale tier).
    """
    rng = random.Random(scale)
    results: list[Samples] = []

    rare_token = f"raretoken{scale}xyz"
    extra_words = content_extra_bytes // 7  # ~7 bytes/word (avg 6-char word + separator) from _WORDS

    def _padded(content: str) -> str:
        return content if extra_words == 0 else f"{content} {_lorem(rng, extra_words)}"

    for name, store in backends.items():

        seed_bar = _ProgressBar(total=scale, prefix=f"  [{name}] seeding")

        async def seed(store: Any = store, bar: _ProgressBar = seed_bar) -> None:
            await store.initialize()
            for i in range(scale):
                # Every entry mentions "benchmark" (a query for it matches 100% of the store --
                # worst case for a full-scan backend, but *also* worst case for an index-backed
                # one, since the index can't narrow a query that every record matches). Exactly
                # one entry additionally mentions a store-unique rare token, so a query for that
                # token is selective: it should matter only to an index-backed lookup, not to a
                # backend that always fetches the whole store regardless of match count.
                extra = f" {rare_token}" if i == 0 else ""
                content = _padded(
                    f"# Seed fact {i}\nDetail body mentioning benchmark payload {_lorem(rng, 10)}.{extra}"
                )
                await store.add(content)
                bar.update()

        seed_start = time.perf_counter()
        asyncio.run(seed())
        seed_bar.clear()
        entries_word = "entry" if scale == 1 else "entries"
        print(f"  [{name}] seeded {scale} {entries_word} in {time.perf_counter() - seed_start:.1f}s", flush=True)

        async def do_add(i: int, store: Any = store) -> None:
            content = _padded(f"# New fact {i}-{_random_suffix(rng)}\nDetail body benchmark payload {_lorem(rng, 10)}.")
            await store.add(content)

        async def do_search(_i: int, store: Any = store) -> None:
            await store.search("benchmark")

        async def do_search_selective(_i: int, store: Any = store, rare_token: str = rare_token) -> None:
            await store.search(rare_token)

        for op_name, fn in (
            ("add", do_add),
            ("search", do_search),
            ("search_selective", do_search_selective),
        ):
            results.append(
                _run_async_op(
                    interface="MemoryStore",
                    name=name,
                    op_name=op_name,
                    scale=scale,
                    fn=fn,
                    reps=reps,
                    warmup=warmup,
                    label_width=16,
                )
            )

    return results


# -- SessionRepository ----------------------------------------------------------


def run_session_scenario(
    factories: dict[str, Callable[[str], Any]],
    *,
    scale: int,
    reps: int,
    warmup: int,
    delete_reps: int | None,
    message_body_extra_bytes: int = 0,
) -> list[Samples]:
    """Seed and time create_message/read_message/list_messages (and, if requested, delete_session).

    ``message_body_extra_bytes``, if nonzero, pads every message body with that many extra
    characters (used by the bulk-scale tier to simulate larger tool-output-bearing messages).
    """
    results: list[Samples] = []

    from strands.types.session import SessionAgent, SessionMessage

    def make_message(index: int) -> SessionMessage:
        text = f"message body {index}"
        if message_body_extra_bytes:
            text = f"{text} " + ("x" * message_body_extra_bytes)
        return SessionMessage.from_message({"role": "user", "content": [{"text": text}]}, index)

    for name, factory in factories.items():
        session_id = f"bench-{name.lower()}-{scale}"
        manager = factory(session_id)
        agent_id = "bench-agent"
        manager.create_agent(session_id, SessionAgent(agent_id=agent_id, state={}, conversation_manager_state={}))
        seed_bar = _ProgressBar(total=scale, prefix=f"  [{name}] seeding")
        seed_start = time.perf_counter()
        for i in range(scale):
            manager.create_message(session_id, agent_id, make_message(i))
            seed_bar.update()
        seed_bar.clear()
        print(f"  [{name}] seeded {scale} message(s) in {time.perf_counter() - seed_start:.1f}s", flush=True)

        def do_create_message(
            i: int, manager: Any = manager, session_id: str = session_id, agent_id: str = agent_id
        ) -> None:
            manager.create_message(session_id, agent_id, make_message(scale + i))

        def do_read_message(
            i: int, manager: Any = manager, session_id: str = session_id, agent_id: str = agent_id
        ) -> None:
            manager.read_message(session_id, agent_id, i % scale)

        def do_list_messages(
            _i: int, manager: Any = manager, session_id: str = session_id, agent_id: str = agent_id
        ) -> None:
            manager.list_messages(session_id, agent_id)

        for op_name, fn in (
            ("create_message", do_create_message),
            ("read_message", do_read_message),
            ("list_messages", do_list_messages),
        ):
            results.append(
                _run_sync_op(
                    interface="SessionRepository",
                    name=name,
                    op_name=op_name,
                    scale=scale,
                    fn=fn,
                    reps=reps,
                    warmup=warmup,
                )
            )

        if delete_reps is not None:
            # Deleting is destructive, so each rep needs its own freshly-recreated session;
            # that setup cost dwarfs the delete itself, so time_sync's warmup/reps loop (which
            # only excludes fixture cost run *before* the timed loop, not per-rep) doesn't fit --
            # time only the delete_session call itself, inline, per rep.
            delete_samples: list[float] = []
            warm = min(3, delete_reps)
            delete_bar = _ProgressBar(total=warm + delete_reps, prefix=f"  [{name}] delete_session")
            for i in range(warm + delete_reps):
                del_session_id = f"bench-del-{name.lower()}-{scale}-{i}"
                del_manager = factory(del_session_id)
                del_manager.create_agent(
                    del_session_id, SessionAgent(agent_id=agent_id, state={}, conversation_manager_state={})
                )
                for j in range(scale):
                    del_manager.create_message(del_session_id, agent_id, make_message(j))
                start = time.perf_counter()
                del_manager.delete_session(del_session_id)
                elapsed = time.perf_counter() - start
                if i >= warm:
                    delete_samples.append(elapsed)
                delete_bar.update()
            delete_bar.clear()
            result = Samples(
                interface="SessionRepository",
                operation="delete_session",
                backend=name,
                scale=scale,
                latencies_s=delete_samples,
            )
            results.append(result)
            print(f"  [{name}] {'delete_session':16s} mean={result.stats['mean_ms']:.3f}ms", flush=True)

    return results
