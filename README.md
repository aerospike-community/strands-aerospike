# strands-aerospike

Aerospike-backed storage, session, and memory backends for the
[Strands Agents](https://github.com/strands-agents/harness-sdk) Python SDK.

This package implements Strands' existing pluggable persistence interfaces against
Aerospike — it does not fork or patch the SDK itself. See [docs/DESIGN.md](docs/DESIGN.md)
for the data model and the reasoning behind it.

- `AerospikeStorage` — the unified `strands.storage.Storage` interface (used for
  snapshots, plugin state, and anything else that needs a generic byte KV store).
- `AerospikeSessionManager` — a message-log `SessionManager`/`SessionRepository`,
  covering single agents and multi-agent orchestrators.
- `AerospikeMemoryStore` — a `MemoryStore` for `strands.memory.MemoryManager`,
  with server-side lexical search via an Aerospike list secondary index.

> **No vector/embedding search.** Aerospike's vector-search story (AVS) is
> being deprecated with no settled replacement yet, so `AerospikeMemoryStore` does
> lexical (keyword) search only. Pair it with a dedicated vector store if you need
> semantic similarity search.

## Install

```bash
pip install strands-aerospike
```

## Run Aerospike locally

```bash
./scripts/start_aerospike_ce.sh
```

This starts a single-node Aerospike Community Edition container on `127.0.0.1:3000`
with a `test` namespace, the same one the test suite and the examples below use.
Equivalent one-liner, if you'd rather not use the script:

```bash
docker run -d --name aerospike-ce --ulimit nofile=15000:15000 \
  -p 3000-3002:3000-3002 aerospike/aerospike-server:latest
```

## Quickstart

```python
from strands import Agent
from strands.memory import MemoryManager
from strands_aerospike import AerospikeMemoryStore, AerospikeSessionManager, AerospikeStorage

session_manager = AerospikeSessionManager(session_id="my-session", namespace="test")
memory_store = AerospikeMemoryStore(name="agent-memory", namespace="test")

agent = Agent(
    session_manager=session_manager,
    memory_manager=MemoryManager(stores=[memory_store]),
)

agent("Remember that I prefer concise answers.")
```

All three components share one process-wide Aerospike client by default (see
[docs/DESIGN.md](docs/DESIGN.md#client-lifecycle)); pass `hosts=[("host", 3000)]` to
point at a real cluster, or `client=your_aerospike_client` to supply your own.

### AerospikeStorage on its own

```python
from strands_aerospike import AerospikeStorage

storage = AerospikeStorage(namespace="test")
await storage.write("session/abc/state.json", b"...")
data = await storage.read("session/abc/state.json")
```

## Testing

```bash
./scripts/start_aerospike_ce.sh
pip install -e ".[dev]"
pytest
```

Tests are integration tests against a real server (see
[docs/DESIGN.md](docs/DESIGN.md#testing)); without the container running, the suite
skips cleanly instead of failing.

## Benchmarks

[reports/REPORT.md](reports/REPORT.md) quantifies how `AerospikeStorage`,
`AerospikeSessionManager`, and `AerospikeMemoryStore` perform against every other
`Storage`/`SessionRepository`/`MemoryStore` backend `strands-agents` ships
(`InMemoryStorage`, `LocalFileStorage`, `S3Storage`, `FileSessionManager`,
`S3SessionManager`, `FileMemoryStore`), across four data-volume scales per subsystem --
the largest a configurable multi-gigabyte "bulk" tier (10GB by default for the Storage
interface; see `reports/REPORT.md`'s environment section for how each subsystem's bulk
tier is sized) -- with latency histograms under `reports/histograms/`.

The same content also renders as `reports/REPORT.html` (open it directly in a
browser, or serve `reports/` locally, for a far more legible read than the raw
Markdown) and `reports/REPORT.pdf` (landscape, for sharing or printing) — all
three are generated from one Markdown string, so there's nothing to keep in
sync between them.

Regenerate it from a clean run:

```bash
./scripts/start_aerospike_ce.sh
uv pip install --python .venv/bin/python -e ".[dev,benchmark]" -e ../harness-sdk/strands-py
.venv/bin/python benchmark/benchmark.py
```
