# Design

`strands-aerospike` implements Aerospike-backed versions of the three pluggable
persistence interfaces the [Strands Agents Python SDK](https://github.com/strands-agents/harness-sdk)
already defines, rather than inventing a new extension point:

| Strands interface | This package |
|---|---|
| `strands.storage.storage.Storage` | `AerospikeStorage` |
| `strands.session.session_repository.SessionRepository` (used via `RepositorySessionManager`) | `AerospikeSessionManager` |
| `strands.memory.types.MemoryStore` | `AerospikeMemoryStore` |

Each is a companion, installable package (`strands-aerospike`) that depends on
`strands-agents`, not a fork or patch of it.

## Client lifecycle

The Aerospike client is thread-safe and holds its own connection pool and cluster
state, so it should be constructed once per process rather than once per component
or per call. `strands._client.get_client(hosts)` lazily creates and memoizes one
client per unique `hosts` config; all three components default to it, so an app that
constructs an `AerospikeStorage`, an `AerospikeMemoryStore`, and an
`AerospikeSessionManager` against the same cluster shares one connection pool without
any extra wiring. An app that wants explicit control (its own lifecycle, TLS config,
custom policies) can construct an `aerospike.Client` itself and pass it to every
component's `client=` argument instead.

## AerospikeStorage

`Storage` is a minimal four-operation protocol over opaque string keys and raw
bytes (`write` / `read` / `delete` / `list` / `search`). Every SDK subsystem that
needs generic persistence (snapshots, the file-backed memory store, plugin storage)
goes through it, so its record shape has to serve arbitrary, unstructured byte
values under arbitrary keys.

- **One record per key**, in a configurable set (default `strands_storage`). The key
  is stored explicitly in a `k` bin (bin name kept to a single character; Aerospike
  bin names cap at 15) because `list()` needs to filter and return the *user* key,
  and a digest-based primary key cannot be reversed back to it.
- **`list(prefix)` filters server-side.** A scan of the set applies a compiled
  `CmpRegex` filter expression on the `k` bin (`^` + the escaped prefix), so
  non-matching records are never shipped to the client. This is the closest
  Aerospike-native analog to S3's sorted-keyspace prefix listing; Aerospike has no
  ordered keyspace to exploit here, so a filtered scan is the correct trade-off for
  an operation that is fundamentally an enumeration, not a targeted lookup.
- **`search()` uses the same token-overlap scoring as the `Storage` protocol's own
  default strategy** (rather than importing it, since it lives outside the `strands
  .storage` package's declared `__all__`). This means `search` on `AerospikeStorage`
  fetches every key's content, like the default strategy does for any
  `Storage`-backed store — see `AerospikeMemoryStore` below for the case where a
  richer, server-side-narrowed alternative was worth building specifically because
  its access pattern (a named store's own entries) supports it.
- **Chunking.** A record's total size is capped by the cluster's configured
  write-block-size (often 1 MiB), and a `Storage` value can be arbitrarily large
  (a big snapshot, a big plugin blob). Values at or under `max_value_size` (default
  900,000 bytes, comfortably under the common 1 MiB default) are stored inline in a
  `v` bin. Larger values are split into fixed-size chunks written to companion
  records in a separate `<set>_chunks` set (never key-suffixed into the same set —
  a distinct set means chunk records can never collide with, or be returned by, a
  primary-set prefix scan, with no escaping scheme needed), and the primary record
  instead carries an `n` bin (chunk count). Chunks are written before the primary
  record's `n` pointer, so a partial failure leaves at worst an orphaned-but-harmless
  chunk, never a pointer to missing data. `write()` always clears any stale chunk
  records left by a previous chunked value under the same key before writing, so an
  overwrite from large to small never leaves orphans.

## AerospikeSessionManager

Sessions are modeled like `FileSessionManager` / `S3SessionManager` (one record per
message, via the `SessionRepository` CRUD contract), not like `SnapshotSessionManager`
(one blob per agent) — the message-log shape is what covers multi-agent
orchestrators, which `SnapshotSessionManager` explicitly does not.

- **Composite string keys** in one configurable set (default `strands_sessions`):
  `<session_id>`, `<session_id>:agent:<agent_id>`,
  `<session_id>:agent:<agent_id>:msg:<message_id>`, and
  `<session_id>:multiagent:<multi_agent_id>`. Session/agent ids may not contain `:`
  (validated at construction and on every write) since a colon in a segment would
  let it bleed into the next.
- **All four of `read_message` and `update_message`'s callers, and `create_message`,
  are pure key lookups** — the fastest path in Aerospike, and the first thing this
  skill's guidance checks for. `list_messages` is the one access pattern that looks
  like it needs a range scan; it doesn't, because each agent record carries an `n`
  bin (message count), atomically set to `message_id + 1` on every `create_message`.
  `list_messages` reads that one bin, then does a **single batch read** across the
  exact key range `[offset, end)` — no scan, no secondary index, ever.
- **The session record carries `aids` / `maids` list bins** (agent ids, multi-agent
  ids), appended atomically via a CDT `list_append` operation whenever
  `create_agent` / `create_multi_agent` runs. `delete_session` reads these two lists
  plus each agent's message count to build the *exact* set of keys to remove, then
  issues one `batch_remove` — again, no scan.
- Each record stores its payload as a single JSON-serialized `d` bin (matching
  `FileSessionManager` / `S3SessionManager`'s JSON-blob approach) rather than
  decomposing `Session` / `SessionAgent` / `SessionMessage` into typed bins per
  field — those dataclasses are internal to Strands and can grow fields over time;
  a JSON blob absorbs that without a schema migration on this side.

## AerospikeMemoryStore

`MemoryStore.search` has no required implementation strategy; the SDK's own
`FileMemoryStore` falls back to fetch-everything-and-score client-side (fine for a
local file tree, not for a cluster). This store instead does lexical search
server-side:

- Each entry's content is tokenized into a `tok` list bin, with an Aerospike **list
  secondary index** on it (created idempotently in `initialize()`, swallowing
  `IndexFoundError`). A query token is looked up via `predicates.contains(...,
  INDEX_TYPE_LIST, token)`, which uses the index rather than scanning.
- All of a store's entries live in one shared, configurable set (default
  `strands_memory`) alongside every other named store on the same cluster, keyed
  `<name>:<slug>`. A query additionally applies a compiled filter expression
  (`store == name`) on top of the index-narrowed candidates — confirmed, in this
  package's own development, to combine correctly in one query rather than requiring
  a second round trip — so cross-store leakage is impossible without a per-store set
  per name (which would need dynamic, user-string-derived Aerospike set names).
- **No vector/embedding search.** Aerospike Vector Search is being deprecated with
  no settled replacement, so this store does not offer semantic similarity search —
  see the `aerospike-backend-integration` skill's vector guardrail. The list-index
  lexical approach here is the documented fallback shape for exactly this situation.
  Pair `AerospikeMemoryStore` with a dedicated vector store if semantic recall is a
  hard requirement, or wait for Aerospike's vector story to resolve.
- `add()` mirrors `FileMemoryStore`'s slugify-and-merge behavior (a fact sharing a
  heading with an existing entry is appended to it, not duplicated), including the
  same key-aware extractor that lists existing headings so automatic extraction
  reuses topic names instead of fragmenting them.

## Testing

Real Aerospike CE in Docker, not a protocol mock — `scripts/start_aerospike_ce.sh`.
All tests are integration tests by design (this package has no meaningful behavior
to unit-test in isolation from the server); `tests/conftest.py`'s session-scoped
`client` fixture skips the whole run if the server is unreachable, so a contributor
without the container running gets a clean skip, not a failure. Sets are truncated
between tests rather than tearing down the namespace, preserving the idempotent
index-creation path.

`tests/test_agent_integration.py` goes one level up from testing each class in
isolation: it wires all three into a real `strands.Agent` (with a minimal
in-repo mock `Model`, since a live agent invocation is otherwise not exercised by
the per-class tests) to confirm the hook wiring and call signatures Strands actually
uses, not just the shapes this package assumed they'd be.
