<!-- GSD:project-start source:PROJECT.md -->

## Project

**strands-aerospike**

A Python package that implements the Strands Agents SDK's three pluggable persistence interfaces (`Storage`, `SessionManager`, `MemoryStore`) on top of Aerospike: `AerospikeStorage` (chunked byte KV store), `AerospikeSessionManager` (per-message session/agent/multi-agent persistence), and `AerospikeMemoryStore` (lexical search over memory entries via a list secondary index). It lets Strands-based agents use Aerospike as their backing store instead of file/S3-based alternatives.

**Core Value:** Correct, concurrency-safe persistence: multiple Strands agent processes/clients reading and writing the same Aerospike-backed session, storage, or memory records must never corrupt or silently serve stale data — even without any coordination between processes.

### Constraints

- **Correctness**: Fixes must preserve existing public API signatures (constructors, method signatures) — this is a review-feedback pass, not a breaking redesign
- **Concurrency model**: Prefer Aerospike server-side atomicity (single-record `operate()`, batch operations, generation checks) over client-side locking, per reviewer guidance
- **Testing**: Changes must be verifiable against the existing pytest integration suite running against a local Aerospike CE container (`scripts/start_aerospike_ce.sh`)

<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->

## Technology Stack

## Languages

- Python 3.10+ - All source code, tests, and benchmarks
- Supports Python 3.10, 3.11, 3.12, 3.13 (see `pyproject.toml`)

## Runtime

- Python 3.10 or higher (enforced via `requires-python = ">=3.10"` in `pyproject.toml`)
- uv (preferred, fast dependency resolver)
- pip (supported via standard Python packaging)
- Lockfile: `uv.lock` (version 1, ~1MB pinned dependency tree)

## Frameworks

- strands-agents >= 1.0.0 - The Strands Agents SDK; implements three pluggable persistence interfaces (`Storage`, `SessionManager`, `MemoryStore`)
- aerospike >= 15.0.0 - Python client for Aerospike NoSQL database; core backend for all three persistence layers
- pytest >= 8.0.0, < 10.0.0 - Test runner
- pytest-asyncio >= 0.24.0, < 1.5.0 - Async test fixture support (`asyncio_mode = "auto"` in `pyproject.toml`)
- ruff >= 0.13.0, < 0.17.0 - Fast linter and formatter (Python code style enforcer)
- mypy >= 1.15.0, < 3.0.0 - Static type checker (strict mode enabled: `disallow_untyped_defs = true`)
- matplotlib >= 3.9.0, < 4.0.0 - Histogram plotting for performance reports
- moto[server] >= 5.1.0, < 6.0.0 - Mock S3 server for S3Storage/S3SessionManager benchmarks
- boto3 >= 1.34.0, < 2.0.0 - AWS SDK (used with moto for local S3 operations)
- markdown >= 3.6, < 4.0.0 - Markdown report generation
- xhtml2pdf >= 0.2.14, < 0.3.0 - PDF rendering from HTML (generates REPORT.pdf from REPORT.html)

## Key Dependencies

- aerospike 19.2.2 - Licensed separately; handles all read/write/search operations against Aerospike cluster
- aerospike-helpers (pinned transitively) - Expression builders, batch operations, list operations (see `storage.py`, `session_manager.py`, `memory_store.py`)
- strands-agents >= 1.0.0 - Defines the protocol interfaces this package implements (`strands.storage.Storage`, `strands.session.session_manager.SessionManager`, `strands.memory.types.MemoryStore`)
- aiohttp - Async HTTP client (transitive from boto3/moto stack for S3)
- typing-extensions - Type hints backports (Python < 3.13 compatibility)

## Configuration

- Default Aerospike hosts: `127.0.0.1:3000` (overrideable via `hosts=` argument to constructors)
- Default namespace: `test` (must exist on the Aerospike server; set via `namespace=` argument)
- No environment variables required for core functionality
- AWS credentials (only used in benchmarks): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `AWS_ENDPOINT_URL_S3` (auto-set by moto context manager in `benchmark/backends.py`)
- `pyproject.toml` - Project metadata, dependency groups (dev, benchmark), build config (hatchling backend)
- `ruff.toml` config in `pyproject.toml`:
- `mypy` config in `pyproject.toml`:

## Platform Requirements

- Python 3.10+ with pip or uv
- Docker (for running Aerospike CE locally via `scripts/start_aerospike_ce.sh`)
- 15000 open file descriptors recommended (set via `ulimit nofile=15000:15000` in container startup)
- Running Aerospike CE container (see `scripts/start_aerospike_ce.sh`):
- Local Aerospike CE container must be running
- Available disk space for 10GB bulk-tier data (default; tunable via `--bulk-target-gb` flag)
- Generates: `reports/REPORT.md`, `reports/REPORT.html`, `reports/REPORT.pdf`, `reports/histograms/*.png`

<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

## Naming Patterns

- Lowercase with underscores for module files (`storage.py`, `session_manager.py`, `memory_store.py`)
- Underscore prefix for private/internal modules (`_client.py`, `_keys.py`)
- snake_case for all function names
- Underscore prefix for private functions (`_freeze`, `_config_key`, `_chunk_set`, `_write_sync`)
- Public functions without prefix (`get_client`, `validate_identifier`)
- snake_case for local and instance variables (`chunk_count`, `message_key`, `query_tokens`)
- Underscore prefix for private instance variables (`_namespace`, `_client`, `_write_lock`)
- PascalCase for classes (`AerospikeStorage`, `AerospikeSessionManager`, `AerospikeMemoryStore`)
- UPPER_CASE for module-level constants (`_DEFAULT_MAX_VALUE_SIZE`, `_KEY_BIN`, `_VALUE_BIN`, `_CHUNK_COUNT_BIN`)

## Code Style

- Tool: ruff (configured in `pyproject.toml`)
- Line length: 120 characters
- `from __future__ import annotations` at top of every module for forward compatibility
- Tool: ruff with rules: B, D, E, F, G, I, LOG, UP
- Docstring convention: Google style
- No docstring required for test files (D exemption in `tests/**/*.py`)
- Tool: mypy with strict settings
- `python_version = 3.10`
- `disallow_untyped_defs = true`
- `no_implicit_optional = true`
- Full type hints required on all functions
- External packages with untyped stubs (aerospike) use `ignore_missing_imports`

## Import Organization

- Use `TYPE_CHECKING` blocks to import types needed only for annotations:
- See `session_manager.py` and `memory_store.py` for examples.

## Error Handling

- Catch specific exception types first, broad types later
- Wrap exceptions with `from error` to preserve tracebacks:
- Custom exception types from `strands.types.exceptions` (`StorageError`, `SessionException`)
- Handle expected errors gracefully (e.g., `RecordNotFound` → return `None`)
- Destructuring tuple returns to check success: `_, _, bins = client.get(...)`

## Logging

- Use exception messages for context (e.g., `StorageError(f"Failed to read '{key}'")`)
- No INFO/DEBUG logging observed; errors are the primary signal

## Comments

- Explain non-obvious algorithm choices or trade-offs
- Document complex data structure layouts (e.g., Aerospike bin and key schemes)
- Clarify security/correctness implications (e.g., chunked write order in `storage.py:122-128`)
- Avoid commenting obvious code
- Not used (Python docstrings instead)
- Module-level docstrings explain module purpose and provide usage examples
- Function/method docstrings use Google style with Args, Returns, Raises sections

## Function Design

- Use keyword-only arguments (after `*`) for clarity on large parameter lists
- Provide defaults for optional parameters
- Example: `def __init__(self, namespace: str, *, set: str = "strands_storage", hosts=None, ...)`
- Explicit return types in all functions (mypy requirement)
- Use `| None` for optional returns
- Collective returns use `list[T]` or `dict[K, V]` syntax
- Return tuples explicitly for composite keys: `tuple[str, str, str]`

## Module Design

- Define `__all__` in public modules to list public API
- Example in `__init__.py`: `__all__ = ["AerospikeMemoryStore", "AerospikeSessionManager", "AerospikeStorage"]`
- Main `__init__.py` imports and re-exports the three public backends
- No star imports (`from module import *`) used internally

## Async Patterns

- Public methods are async (`async def write`, `async def read`)
- Blocking Aerospike SDK calls are wrapped with `asyncio.to_thread`:
- Private `_sync` methods contain blocking logic and are called via `asyncio.to_thread`
- Used for coordination (e.g., `_write_lock` in `memory_store.py:145`)

## Client Management

- `_client.py` provides `get_client()` to return a single, memoized `aerospike.Client` per configuration
- Callers can inject their own client via an optional `client=` parameter
- Thread-safe with `threading.Lock()` to guard the cache

<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

## System Overview

```text

```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| `AerospikeStorage` | KV store for snapshots, plugin state; chunking for large values | `src/strands_aerospike/storage.py` |
| `AerospikeSessionManager` | Message-log persistence for single/multi-agent sessions | `src/strands_aerospike/session_manager.py` |
| `AerospikeMemoryStore` | Named memory entries with server-side lexical search via secondary indexes | `src/strands_aerospike/memory_store.py` |
| `get_client()` | Process-wide Aerospike client factory with memoization | `src/strands_aerospike/_client.py` |
| `validate_identifier()` | Validation for composite session/agent keys (rejects `:`) | `src/strands_aerospike/_keys.py` |

## Pattern Overview

- **No direct Strands SDK coupling** - Each component implements a protocol (e.g., `strands.storage.Storage`), not a base class; allows independent release cycles
- **Server-side filtering when possible** - `list()` uses regex expressions, `search()` and `AerospikeMemoryStore.search()` use secondary indexes or expression filters to avoid shipping unneeded records to the client
- **Batch operations over scans** - `SessionManager.list_messages()` computes exact keys instead of scanning; `delete_session()` builds a key range from metadata bins
- **JSON serialization for flexibility** - Complex objects (Session, SessionAgent, SessionMessage) stored as single `d` bin JSON blob to absorb schema changes
- **Chunking for unbounded values** - `Storage` splits large values across companion records to respect Aerospike's write-block-size limits

## Layers

- Purpose: Implement Strands SDK interfaces (`Storage`, `SessionRepository`, `MemoryStore`)
- Location: `src/strands_aerospike/storage.py`, `session_manager.py`, `memory_store.py`
- Contains: Public classes (`AerospikeStorage`, `AerospikeSessionManager`, `AerospikeMemoryStore`)
- Depends on: Aerospike client, Strands SDK types, `_client`, `_keys`
- Used by: Strands Agent, MemoryManager, SessionManager
- Purpose: Provide a thread-safe, memoized process-wide Aerospike client
- Location: `src/strands_aerospike/_client.py`
- Contains: `get_client()` factory, client cache, config hashing
- Depends on: `aerospike` library
- Used by: All three public components
- Purpose: Enforce composite key syntax (e.g., no `:` in session/agent ids)
- Location: `src/strands_aerospike/_keys.py`
- Contains: `validate_identifier()` function
- Depends on: Nothing
- Used by: `AerospikeSessionManager`

## Data Flow

### AerospikeStorage Request Path

### AerospikeSessionManager Request Path

### AerospikeMemoryStore Request Path

- No mutable shared state in components (stateless adapters)
- Aerospike cluster is the system of record
- Async locks used only in `AerospikeMemoryStore.add()` to serialize concurrent writes within a single process (doesn't prevent cluster-wide races, hence retry loop)

## Key Abstractions

- Purpose: Map Strands' logical identifiers to Aerospike's (namespace, set, key) tuples
- Pattern: Composite string keys join segments with `:` for sessions/agents/messages
- Validation: `validate_identifier()` rejects `:` to prevent segment confusion
- Example: `storage` -> `(namespace, "strands_storage", user_key)`, `session` -> `(namespace, "strands_sessions", "session_id:agent:agent_id")`
- Purpose: Store arbitrarily large values despite write-block-size limits
- Pattern: Values larger than threshold split into fixed-size chunks in companion set
- Metadata: Primary record carries chunk count in `n` bin
- Safety: Previous chunks cleared only after new write succeeds (preserves old value on failure)
- `AerospikeStorage.search()`: Token overlap on full content (fetch-all, like SDK default)
- `AerospikeMemoryStore.search()`: Index-backed token lookup (server-side narrow, no fetch-all)
- Purpose: Store complex dataclass objects while absorbing schema changes
- Pattern: Single `d` (data) bin with full JSON blob of `to_dict()`
- Rationale: Strands' Session/SessionAgent/SessionMessage are internal; don't fragment into bins

## Entry Points

- Location: `src/strands_aerospike/storage.py:45-290`
- Triggers: Instantiated by user code or wrapped by `Agent(storage=...)`
- Responsibilities: `write()`, `read()`, `delete()`, `list()`, `search()` (async)
- Location: `src/strands_aerospike/session_manager.py:41-332`
- Triggers: Instantiated by user code, passed to `Agent(session_manager=...)` or `MemoryManager` (inherits from `RepositorySessionManager`)
- Responsibilities: Full `SessionRepository` and `RepositorySessionManager` protocol (sync factory methods, async delegation)
- Location: `src/strands_aerospike/memory_store.py:82-337`
- Triggers: Instantiated by user code, passed to `MemoryManager(stores=[...])`
- Responsibilities: `initialize()`, `add()`, `search()` (async)
- Location: `src/strands_aerospike/_client.py:42-77`
- Triggers: Called implicitly by all three components when `client=None`
- Responsibilities: Memoize and return process-wide Aerospike client

## Architectural Constraints

- **Threading:** Aerospike client is thread-safe; sync operations run in `asyncio.to_thread()` worker pool (no explicit threading used, async event loop drives concurrency)
- **Global state:** Process-wide client cache in `_client._clients` dict protected by `threading.Lock`; one shared client per unique `(hosts, policies)` pair
- **Circular imports:** None (linear dependency: components → `_client`/`_keys` → aerospike)
- **Aerospike operations atomic at record level:** No multi-record transactions; optimistic concurrency (generation checks) used in `AerospikeMemoryStore.add()` for within-record safety

## Anti-Patterns

### Scan-based operations where batch reads are possible

### Prefixing to create subsets within one set when a separate set would be clearer

### Decomposing complex objects into individual bins during write

## Error Handling

- Catch `aerospike_exception.AerospikeError`, re-raise as SDK exception with context
- Special handling: `RecordNotFound` → return None (expected for reads); `RecordExistsError` → raise SessionException (create idempotency violation)
- Batch failures: Check `result.batch_records[i].result` code; raise if any entry failed (see `storage.py:297-306`)
- Generation check failures in memory store: Retry loop (up to 5 attempts) to resolve optimistic concurrency conflicts

## Cross-Cutting Concerns

- Session/agent/multi-agent identifiers validated at construction time (`validate_identifier()`) to prevent composite key collisions
- Empty keys rejected in `AerospikeStorage.write()`

<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:

- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
