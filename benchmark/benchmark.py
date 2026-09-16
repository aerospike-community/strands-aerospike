#!/usr/bin/env python3
"""Benchmark strands-aerospike against every other Storage/SessionRepository/MemoryStore backend.

Benchmarks against every other backend that already ships in strands-agents, across four
data-volume scales per interface -- the largest a configurable multi-gigabyte "bulk" tier
(10GB by default for the Storage interface; see --bulk-target-gb) sized to this host's
available disk/RAM.

Usage:
    .venv/bin/python benchmark/benchmark.py                     # full run (10GB bulk tier)
    .venv/bin/python benchmark/benchmark.py --bulk-target-gb 100 # bigger bulk tier, needs more disk
    .venv/bin/python benchmark/benchmark.py --quick              # tiny scales/reps, for a fast smoke test

Requires a running Aerospike CE container (``./scripts/start_aerospike_ce.sh``).
Everything else (InMemoryStorage, LocalFileStorage, FileSessionManager,
FileMemoryStore, and a local moto S3 server for S3Storage/S3SessionManager) is
started/created by this script itself.

Regenerates ``reports/histograms/*.png``, ``reports/REPORT.md``,
``reports/REPORT.html``, and ``reports/REPORT.pdf`` from a clean run every time
-- see ``reports/REPORT.md``'s own Reproduction section for the one-line command.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aerospike  # noqa: E402
from backends import (  # noqa: E402
    connect_aerospike,
    memory_backends,
    moto_s3_server,
    session_backend_factories,
    storage_backends,
)
from harness import Samples  # noqa: E402
from report import plot_histograms, render_html, render_pdf, write_report  # noqa: E402
from scenarios import run_memory_scenario, run_session_scenario, run_storage_scenario  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _host_total_ram_gb() -> float | None:
    """Best-effort total system RAM in GB, for stating the bulk-tier host-resource ceiling honestly."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kib = int(line.split()[1])
                    return kib / (1024 * 1024)
    except OSError:
        pass
    return None


def _check_aerospike() -> aerospike.Client:
    try:
        return connect_aerospike()
    except aerospike.exception.ClientError as error:
        print(
            f"ERROR: Aerospike server not reachable at 127.0.0.1:3000 ({error}).\n"
            "Run ./scripts/start_aerospike_ce.sh first.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


def main() -> None:
    """Run the full benchmark suite and regenerate ``reports/``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Tiny scales/reps, for a fast smoke test.")
    parser.add_argument(
        "--bulk-target-gb",
        type=float,
        default=10.0,
        help=(
            "Target data volume (GB) for the Storage interface's largest 'bulk' scale tier. "
            "Default 10GB fits this host's disk/RAM; raise it (e.g. --bulk-target-gb 100) once a "
            "host with more free disk is available -- see reports/REPORT.md's environment section "
            "for the current host's constraints."
        ),
    )
    args = parser.parse_args()

    # Force line buffering so progress prints (the whole point of them) show up immediately
    # even when stdout isn't a tty -- e.g. piped through `uv run`, `tee`, or a log file.
    sys.stdout.reconfigure(line_buffering=True)

    if args.quick:
        storage_scales = [10, 30, 80]
        session_scales = [5, 20, 40]
        memory_scales = [10, 30, 60]
        reps, warmup = 10, 3
        storage_search_reps = 5
        session_delete_reps = 5
        storage_bulk_value_bytes = 64 * 1024
        storage_bulk_list_reps = 3
        session_bulk_extra_bytes = 500
        memory_bulk_extra_bytes = 200
    else:
        storage_bulk_value_bytes = 512 * 1024  # 512KiB "large blob" -- see Architecture in the plan
        bulk_storage_scale = round(args.bulk_target_gb * (1024**3) / storage_bulk_value_bytes)
        storage_scales = [100, 500, 2000, bulk_storage_scale]
        session_scales = [20, 100, 500, 2000]
        memory_scales = [50, 250, 1000, 5000]
        reps, warmup = 100, 15
        storage_search_reps = 20
        session_delete_reps = 20
        storage_bulk_list_reps = 5
        session_bulk_extra_bytes = 8000
        memory_bulk_extra_bytes = 4000

    aero_client = _check_aerospike()

    run_start = time.perf_counter()
    all_samples: list[Samples] = []
    tmp_root = Path(tempfile.mkdtemp(prefix="strands_aerospike_bench_", dir="/var/tmp"))
    print(f"Scratch directory: {tmp_root}")

    try:
        with moto_s3_server() as moto:
            print(f"moto S3 server: {moto.endpoint}")

            print("\n=== Storage ===")
            for scale in storage_scales:
                scale_start = time.perf_counter()
                print(f"-- scale={scale} --")
                is_bulk_tier = scale == storage_scales[-1]
                backends = storage_backends(tmp_root, aero_client, run_id=f"storage-{scale}")
                if is_bulk_tier:
                    excluded = [name for name in ("InMemoryStorage", "S3Storage") if name in backends]
                    for name in excluded:
                        backends.pop(name)
                    if excluded:
                        print(
                            f"  bulk tier: excluding {excluded} -- both would hold the whole bulk-tier "
                            "dataset in this benchmark process's own RAM (InMemoryStorage: a plain dict; "
                            "S3Storage: moto buffers each object under ~5MB in memory before rolling to "
                            "disk); see reports/REPORT.md's environment section."
                        )
                search_reps = storage_search_reps if scale <= storage_scales[min(1, len(storage_scales) - 1)] else None
                value_size_bytes = storage_bulk_value_bytes if is_bulk_tier else None
                list_reps = storage_bulk_list_reps if is_bulk_tier else None
                samples = run_storage_scenario(
                    backends,
                    scale=scale,
                    reps=reps,
                    warmup=warmup,
                    search_reps=search_reps,
                    value_size_bytes=value_size_bytes,
                    list_reps=list_reps,
                )
                all_samples.extend(samples)
                print(f"-- scale={scale} done in {time.perf_counter() - scale_start:.1f}s --")

            print("\n=== SessionRepository ===")
            for scale in session_scales:
                scale_start = time.perf_counter()
                print(f"-- scale={scale} --")
                is_bulk_tier = scale == session_scales[-1]
                factories = session_backend_factories(tmp_root, aero_client, run_id=f"session-{scale}")
                delete_reps = session_delete_reps if scale == session_scales[0] else None
                samples = run_session_scenario(
                    factories,
                    scale=scale,
                    reps=reps,
                    warmup=warmup,
                    delete_reps=delete_reps,
                    message_body_extra_bytes=session_bulk_extra_bytes if is_bulk_tier else 0,
                )
                all_samples.extend(samples)
                print(f"-- scale={scale} done in {time.perf_counter() - scale_start:.1f}s --")

            print("\n=== MemoryStore ===")
            for scale in memory_scales:
                scale_start = time.perf_counter()
                print(f"-- scale={scale} --")
                is_bulk_tier = scale == memory_scales[-1]
                backends = memory_backends(tmp_root, aero_client, run_id=f"memory-{scale}")
                samples = run_memory_scenario(
                    backends,
                    scale=scale,
                    reps=reps,
                    warmup=warmup,
                    content_extra_bytes=memory_bulk_extra_bytes if is_bulk_tier else 0,
                )
                all_samples.extend(samples)
                print(f"-- scale={scale} done in {time.perf_counter() - scale_start:.1f}s --")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\nAll scenarios done in {time.perf_counter() - run_start:.1f}s total.")

    print("\n=== Generating reports ===")

    reports_dir = REPO_ROOT / "reports"
    if reports_dir.exists():
        shutil.rmtree(reports_dir)
    reports_dir.mkdir(parents=True)

    print("Plotting histograms...", end="", flush=True)
    hist_start = time.perf_counter()
    histogram_paths = plot_histograms(all_samples, reports_dir / "histograms")
    print(f" done in {time.perf_counter() - hist_start:.1f}s ({len(histogram_paths)} file(s))")

    (reports_dir / "raw_samples.json").write_text(
        json.dumps([asdict(sample) for sample in all_samples], indent=2), encoding="utf-8"
    )

    print("Querying Aerospike server version...", end="", flush=True)
    try:
        aerospike_version = subprocess.run(
            ["docker", "exec", "strands-aerospike-ce", "asinfo", "-v", "build"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        print(f" {aerospike_version}")
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        aerospike_version = "unknown (docker exec asinfo failed)"
        print(" unknown (docker exec asinfo failed)")
    host_ram_gb = _host_total_ram_gb()
    host_ram_note = f"~{host_ram_gb:.1f}GB" if host_ram_gb is not None else "an undetermined amount"
    bulk_actual_gb = (storage_scales[-1] * storage_bulk_value_bytes) / (1024**3)
    environment_md = f"""**Environment:**

- Host: {platform.platform()}, Python {platform.python_version()}
- Aerospike: Community Edition, single-node Docker container started by
  `./scripts/start_aerospike_ce.sh` -- `docker exec strands-aerospike-ce asinfo -v build`
  reports build info per node: `{aerospike_version}`.
- S3Storage / S3SessionManager: benchmarked against a local `moto[server]`
  `ThreadedMotoServer` (real local HTTP server implementing the S3 API), **not**
  real AWS S3. This measures real HTTP + (de)serialization overhead but *not*
  real network latency to an AWS region -- treat the S3 numbers here as a
  floor, not a prediction of production S3 latency.
- FileSessionManager / LocalFileStorage / FileMemoryStore: local filesystem
  (the scratch directory this run created and deleted afterward), not a
  network filesystem.
- **These are single-node, single-machine, local-Docker numbers.** They are
  not production-cluster numbers for any backend; treat them as relative
  comparisons under identical local conditions, not absolute latency
  predictions for a real deployment.
- BedrockKnowledgeBase (the only other in-repo `MemoryStore`) requires a live
  AWS Bedrock Knowledge Base and cannot be exercised locally, so it is
  excluded rather than faked.
- **Redis/Valkey, PostgreSQL, and MongoDB were explicitly checked for** as
  candidate comparison backends for the `Storage`, `SessionRepository`, and
  `MemoryStore` interfaces (`strands-agents`'s own dependency metadata and
  source tree were searched for a Redis/Valkey-backed, SQL/Postgres-backed,
  or Mongo-backed implementation of any of the three). None exist in
  `strands-agents` today -- its only shipped backends for these interfaces
  are the in-memory, local-filesystem, S3, and (for `MemoryStore`) Bedrock
  Knowledge Base implementations benchmarked here. This comparison set is
  therefore exhaustive against what `strands-agents` currently ships, not a
  partial selection.
- **Bulk-scale tier**: the Storage interface's largest scale targets `--bulk-target-gb`
  ({args.bulk_target_gb:g} GB requested, {bulk_actual_gb:.2f}GB actual at
  {storage_bulk_value_bytes // 1024}KiB/value) of total value bytes. `InMemoryStorage` and
  `S3Storage` are excluded from that tier: `InMemoryStorage` is a plain in-process dict, and
  `S3Storage`'s moto backend buffers each object under ~5MB in a `SpooledTemporaryFile` that
  never rolls over to disk at this value size -- both would need to hold the entire bulk
  dataset in this benchmark process's own RAM. This host has {host_ram_note} of RAM total, not
  enough headroom to do that safely, so those two are compared only at the smaller scales.
  `AerospikeStorage`'s "test" namespace storage capacity (`STORAGE_GB`) and primary-index
  memory (`MEM_GB`) were both raised in `scripts/start_aerospike_ce.sh` to give headroom above
  this target. **This is a deliberate ceiling driven by this host's available disk and RAM, not
  a hard design limit** -- raise `--bulk-target-gb` (e.g. to 100) once a host with more free
  disk (and, if `InMemoryStorage`/`S3Storage` should stay included at that scale, more free
  RAM) is available.
"""

    methodology_md = f"""- **Storage** (`AerospikeStorage`, `InMemoryStorage`, `LocalFileStorage`,
  `S3Storage`): scales are pre-existing key counts (100 / 500 / 2,000),
  bracketing a realistic range for snapshot/plugin-blob storage. Values are
  ~1.2KB of pseudo-text. `write`/`read`/`delete`/`list` are timed with 100
  samples (15 discarded warmup) at every scale. `search` (fetch-every-key
  token-overlap scoring on every non-Aerospike backend, and on
  `AerospikeStorage` itself -- it intentionally doesn't index-optimize this
  path, see `docs/DESIGN.md`) is inherently O(n) per query; it is timed with a
  reduced 20 samples, and only at the two smaller scales, to keep total
  runtime reasonable without skipping the operation. A fourth "bulk" tier is
  also run, sized so total value bytes hit `--bulk-target-gb` (10GB by
  default) using {storage_bulk_value_bytes // 1024}KiB values instead of the
  ~1.2KB values used at the smaller scales -- reaching that target via
  ~1.2KB values would need millions of keys, impractical to seed against
  file- and HTTP-backed backends, so the bulk tier grows the payload size
  instead of the key count. `list`'s reps drop to {storage_bulk_list_reps}
  at the bulk tier only, since a full-set scan reads every record's bins off
  the storage device before filtering, and 100 reps over a multi-gigabyte
  dataset would mean scanning proportionally more.
- **SessionRepository** (`AerospikeSessionManager`, `FileSessionManager`,
  `S3SessionManager`, via each backend's own manager instance): scales are
  pre-existing message counts per agent (20 / 100 / 500), bracketing a long
  but realistic single-agent conversation. `create_message`/`read_message`/
  `list_messages` are timed with 100 samples (15 discarded warmup) at every
  scale. `delete_session` is destructive -- each rep must recreate a whole
  fresh session first -- so it is timed with a reduced 20 samples at the
  smallest scale only, measuring just the `delete_session` call itself
  (the per-rep recreate is unmeasured setup). A fourth "bulk" tier
  ({session_scales[-1]} messages, each padded to
  ~{session_bulk_extra_bytes // 1000}KB) is also run -- large enough to
  meaningfully exceed the smaller tiers, but a single agent conversation
  reaching the Storage interface's literal multi-gigabyte target is not a
  realistic shape for this subsystem, so this tier brackets
  SessionRepository's own realistic ceiling instead (see the Storage bullet
  above for where that larger target actually applies).
- **MemoryStore** (`AerospikeMemoryStore`, `FileMemoryStore`): scales are
  pre-existing entry counts (50 / 250 / 1,000), bracketing a substantial
  agent-memory store. `add`, `search` (a common query token matching every
  seeded entry -- worst case for a full-scan backend, but *also* worst case
  for an index-backed lookup, since the index can't narrow a query every
  record matches), and `search_selective` (a store-unique rare token matching
  exactly one seeded entry regardless of scale -- the case an index-backed
  lookup is actually built for) are all timed with 100 samples (15 discarded
  warmup) at every scale. A fourth "bulk" tier ({memory_scales[-1]} entries,
  each padded with ~{memory_bulk_extra_bytes // 1000}KB of extra body text)
  is also run, sized to a large but realistic single memory/knowledge
  namespace rather than the Storage interface's literal multi-gigabyte
  target (see the Storage bullet above).
- All backends within one scenario/scale run back-to-back in the same script
  invocation, each against its own freshly-seeded, freshly-created storage
  (fresh Aerospike set truncation, fresh temp directory, fresh S3 bucket) --
  no shared state or warm-cache advantage carries from one backend to the
  next.
"""

    interpretation_md = _interpretation(all_samples)

    reproduction_md = """```bash
./scripts/start_aerospike_ce.sh
uv pip install --python .venv/bin/python -e ".[dev,benchmark]" -e ../harness-sdk/strands-py
.venv/bin/python benchmark/benchmark.py                       # default 10GB bulk tier
.venv/bin/python benchmark/benchmark.py --bulk-target-gb 100  # once more host disk is available
```
"""

    print("Writing REPORT.md and REPORT.html...", end="", flush=True)
    report_md = write_report(
        out_path=reports_dir / "REPORT.md",
        samples=all_samples,
        histogram_paths=histogram_paths,
        environment_md=environment_md,
        methodology_md=methodology_md,
        interpretation_md=interpretation_md,
        reproduction_md=reproduction_md,
    )
    render_html(report_md, reports_dir / "REPORT.html", title="strands-aerospike benchmark report")
    print(" done")

    print("Rendering REPORT.pdf (this can take a minute)...", end="", flush=True)
    pdf_start = time.perf_counter()
    pdf_ok = render_pdf(report_md, reports_dir / "REPORT.pdf", reports_dir=reports_dir)
    print(f" done in {time.perf_counter() - pdf_start:.1f}s")
    if not pdf_ok:
        print("WARNING: REPORT.pdf was written but xhtml2pdf reported rendering errors.", file=sys.stderr)

    print(
        f"\nWrote {reports_dir / 'REPORT.md'}, {reports_dir / 'REPORT.html'}, {reports_dir / 'REPORT.pdf'}, "
        f"and {len(histogram_paths)} histogram(s)."
    )


def _rank_at_scale(samples: list[Samples], interface: str, operation: str, scale: int) -> list[tuple[str, float]]:
    """Backends ranked fastest-to-slowest by mean latency, for one (interface, operation, scale) cell."""
    rows = [
        (s.backend, s.stats["mean_ms"])
        for s in samples
        if s.interface == interface and s.operation == operation and s.scale == scale and s.stats["n"] > 0
    ]
    return sorted(rows, key=lambda row: row[1])


def _growth(
    samples: list[Samples], interface: str, operation: str, backend: str
) -> tuple[int, float, int, float] | None:
    """(smallest_scale, its mean_ms, largest_scale, its mean_ms) for one backend across all tested scales."""
    points = sorted(
        (s.scale, s.stats["mean_ms"])
        for s in samples
        if s.interface == interface and s.operation == operation and s.backend == backend and s.stats["n"] > 0
    )
    if len(points) < 2:
        return None
    (scale_lo, mean_lo), (scale_hi, mean_hi) = points[0], points[-1]
    return (scale_lo, mean_lo, scale_hi, mean_hi)


def _rank_sentence(ranked: list[tuple[str, float]]) -> str:
    return "; ".join(f"{backend} {mean_ms:.3f}ms" for backend, mean_ms in ranked)


def _growth_clause(samples: list[Samples], interface: str, operation: str, backend: str) -> str:
    growth = _growth(samples, interface, operation, backend)
    if growth is None:
        return f"{backend}: not enough scales sampled to characterize growth"
    scale_lo, mean_lo, scale_hi, mean_hi = growth
    ratio = mean_hi / mean_lo if mean_lo else float("inf")
    if ratio < 2:
        shape = "roughly flat"
    elif ratio < (scale_hi / scale_lo):
        shape = "grows sub-linearly"
    else:
        shape = "grows roughly linearly"
    return f"{backend} {mean_lo:.3f}ms@{scale_lo} -> {mean_hi:.3f}ms@{scale_hi} ({shape}, {ratio:.1f}x)"


def _interpretation(samples: list[Samples]) -> str:
    """Build the plain-language interpretation section from the actual collected numbers.

    Reports rankings and growth trends as observed, rather than asserting a fixed
    "Aerospike wins" narrative -- a single-node local-Docker comparison can and does
    favor a zero-network backend on some ops (see the environment caveat), and the
    report needs to say so plainly when that's what happened, not paper over it.
    """
    largest_storage_scale = max(s.scale for s in samples if s.interface == "Storage")
    smallest_storage_scale = min(s.scale for s in samples if s.interface == "Storage")
    largest_session_scale = max(s.scale for s in samples if s.interface == "SessionRepository")
    largest_memory_scale = max(s.scale for s in samples if s.interface == "MemoryStore")

    lines = []

    list_rank = _rank_at_scale(samples, "Storage", "list", largest_storage_scale)
    list_growth = "; ".join(_growth_clause(samples, "Storage", "list", backend) for backend, _ in list_rank)
    aerospike_list_rank = next((i for i, (b, _) in enumerate(list_rank) if b == "AerospikeStorage"), None)
    list_note = (
        "AerospikeStorage's `list` filters server-side with a compiled regex expression during a "
        "scan (see docs/DESIGN.md), so only matching records are ever serialized back to the "
        "client -- the advantage that buys is in *bytes shipped*, not in avoiding the server-side "
        "scan itself, and a `scan()` call carries fixed per-call job-setup overhead that a direct "
        "directory walk or in-process dict iteration does not pay. On a single local node with "
        "small local values, that fixed overhead can dominate small-to-medium key counts; it "
        "matters most exactly when scanning-and-shipping unfiltered data would be expensive, i.e. "
        "large key counts and/or a real multi-node cluster where network egress (not local disk) "
        "is the bottleneck a naive `list()` avoids."
        if aerospike_list_rank is not None and aerospike_list_rank > 0
        else "AerospikeStorage's `list` filters server-side with a compiled regex expression during a "
        "scan (see docs/DESIGN.md), so only matching records are ever serialized back to the client, "
        "unlike a full directory walk or a paginated `ListObjectsV2` call that still enumerates every key."
    )
    lines.append(
        f"- **`Storage.list`, ranked fastest to slowest at scale={largest_storage_scale}**: "
        f"{_rank_sentence(list_rank)}. "
        f"Growth from scale={smallest_storage_scale} to scale={largest_storage_scale}: {list_growth}. {list_note}"
    )

    lm_rank = _rank_at_scale(samples, "SessionRepository", "list_messages", largest_session_scale)
    lines.append(
        f"- **`list_messages`, ranked fastest to slowest at scale={largest_session_scale} messages**: "
        f"{_rank_sentence(lm_rank)}. AerospikeSessionManager never scans to build this result: the agent "
        "record's message-count bin gives the exact key range, fetched with one `batch_read` (see "
        "docs/DESIGN.md) -- one round trip regardless of how the range is fetched, versus "
        "FileSessionManager's directory listing plus one file open per message, and "
        "S3SessionManager's per-object or paginated-list calls, both of which scale with message count "
        "and, for S3, with real per-request HTTP overhead against the local moto server."
    )

    search_rank = _rank_at_scale(samples, "MemoryStore", "search", largest_memory_scale)
    search_growth = "; ".join(_growth_clause(samples, "MemoryStore", "search", backend) for backend, _ in search_rank)
    lines.append(
        f"- **`MemoryStore.search`, ranked fastest to slowest at scale={largest_memory_scale} entries**: "
        f"{_rank_sentence(search_rank)}. Growth from smallest to largest scale tested: {search_growth}. "
        "AerospikeMemoryStore looks up the query token via a list secondary index "
        "(`predicates.contains`, see docs/DESIGN.md) -- cost driven by the number of *matching* "
        "entries, not store size. FileMemoryStore's default `KeywordSearchStrategy` fetches every "
        "entry's content and scores it client-side, so its cost grows with total store size "
        "regardless of how many entries actually match; the growth figures above are the direct "
        "evidence for that difference. Because every entry matches this particular query, though, "
        "the index can't actually narrow anything here either -- see `search_selective` below for "
        "the case an index-backed lookup is built for."
    )

    selective_rank = _rank_at_scale(samples, "MemoryStore", "search_selective", largest_memory_scale)
    selective_growth = "; ".join(
        _growth_clause(samples, "MemoryStore", "search_selective", backend) for backend, _ in selective_rank
    )
    lines.append(
        f"- **`MemoryStore.search_selective`, ranked fastest to slowest at scale={largest_memory_scale} entries**: "
        f"{_rank_sentence(selective_rank)}. Growth from smallest to largest scale tested: {selective_growth}. "
        "This query matches exactly one store-unique entry regardless of scale -- the case the "
        "`tok` list secondary index (see docs/DESIGN.md) exists for: AerospikeMemoryStore's cost "
        "should track the (constant) number of matches, not store size, while FileMemoryStore's "
        "`KeywordSearchStrategy` still fetches and scores every entry no matter how selective the "
        "query is. Compare this growth against `search` above, where every entry matched: that's "
        "the direct evidence for whether the index is actually paying for itself here."
    )

    write_rank = _rank_at_scale(samples, "Storage", "write", largest_storage_scale)
    read_rank = _rank_at_scale(samples, "Storage", "read", largest_storage_scale)
    lines.append(
        f"- **Single-key `write`, ranked fastest to slowest at scale={largest_storage_scale}**: "
        f"{_rank_sentence(write_rank)}. **Single-key `read`**: {_rank_sentence(read_rank)}. Every backend "
        "maps these to one underlying key lookup (AerospikeStorage: one record put/get; "
        "AerospikeSessionManager's equivalent create_message/read_message paths are the 'pure key "
        "lookups' design choice documented in docs/DESIGN.md). InMemoryStorage has no I/O at all, so "
        "it is the floor every persistent backend is compared against, not a production competitor "
        "(nothing survives a process restart); the gap between it and every other backend here is "
        "the actual cost of durability, not an inefficiency in any of them."
    )

    return "\n".join(lines)


if __name__ == "__main__":
    main()
