"""Aerospike-backed implementation of the ``strands.memory.types.MemoryStore`` protocol.

Search is lexical, not vector-similarity: content is tokenized into a ``tok`` list
bin with a list-element secondary index, and a query looks up matching records per
token via that index rather than fetching every record and scoring client-side (the
default ``KeywordSearchStrategy`` behavior other ``Storage``-backed stores fall back
to). Aerospike's vector-search story (Aerospike Vector Search) is being deprecated
with no settled replacement yet, so this store does not offer semantic/embedding
search -- pair it with a dedicated vector store for that access pattern if needed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

import aerospike
from aerospike import exception as aerospike_exception
from aerospike import predicates
from aerospike_helpers import expressions as exp
from strands.memory.extraction.model_extractor import ModelExtractor
from strands.memory.extraction.types import ExtractionConfig, ExtractionResult, Extractor, ExtractorContext
from strands.memory.types import MemoryEntry, Metadata, SearchOptions
from strands.types.exceptions import StorageError

from ._client import get_client

if TYPE_CHECKING:
    from strands.types.content import Message

_DEFAULT_MAX_SEARCH_RESULTS = 10
_MAX_SLUG_LENGTH = 50
_TXT_BIN = "txt"
_TOK_BIN = "tok"
_STORE_BIN = "store"
_MD_BIN = "md"

_DEFAULT_EXTRACTION_FRAMING = (
    "You extract durable facts worth remembering across future conversations from a transcript."
)

_OUTPUT_CONTRACT = (
    'Return ONLY a JSON array of objects: {"content": string}.\n\n'
    "Group related facts into a single entry. The first line is a markdown heading"
    ' (e.g. "# User preferences", "# Project setup", "# Team conventions").'
    " Put each fact on its own line below the heading.\n\n"
    "If there is nothing worth remembering, return []."
)


def _tokenize(text: str) -> set[str]:
    """Lowercase and split text into a set of word tokens, dropping empties."""
    return {token for token in re.split(r"\W+", text.lower()) if token}


def _slugify(text: str) -> str:
    """Convert text to a URL-safe kebab-case slug, truncated to 50 characters."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = slug.strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = slug[:_MAX_SLUG_LENGTH]
    return slug.rstrip("-")


def _index_name(set_name: str) -> str:
    return f"{set_name}_{_TOK_BIN}_idx"


class AerospikeMemoryStore:
    """A :class:`~strands.memory.types.MemoryStore` backed by Aerospike.

    Entries are keyed ``<name>:<slug>``, slugified from the first line of content
    the same way :class:`~strands.vended_memory_stores.file_memory_store.FileMemoryStore`
    does, so calling :meth:`add` twice with facts sharing a heading appends to the
    same entry rather than creating a duplicate. All entries for a given store share
    one Aerospike set; a ``store`` bin plus a server-side filter expression scope
    every query to just this store's entries.

    Example:
        ```python
        from strands import Agent
        from strands.memory import MemoryManager
        from strands_aerospike import AerospikeMemoryStore

        memory_store = AerospikeMemoryStore(name="agent-memory", namespace="test")
        agent = Agent(model=model, memory_manager=MemoryManager(stores=[memory_store]))
        ```
    """

    def __init__(
        self,
        *,
        name: str,
        namespace: str,
        set: str = "strands_memory",
        hosts: list[tuple[str, int]] | None = None,
        client: aerospike.Client | None = None,
        writable: bool = True,
        description: str | None = None,
        max_search_results: int | None = None,
        extraction: ExtractionConfig | bool | None = None,
    ) -> None:
        """Initialize the Aerospike memory store.

        Args:
            name: Unique identifier for this store, used to target it in tools and
                to scope its entries within the shared Aerospike set.
            namespace: Aerospike namespace to store records in.
            set: Aerospike set name. Shared across every ``AerospikeMemoryStore``
                instance pointed at the same cluster; entries are scoped by ``name``.
            hosts: Cluster seed addresses as ``(host, port)`` tuples, used only when
                ``client`` is not given. Defaults to ``[("127.0.0.1", 3000)]``.
            client: An existing, connected ``aerospike.Client`` to use. When omitted,
                a process-wide client is lazily created and shared across all
                ``strands_aerospike`` components constructed with the same ``hosts``.
            writable: Whether this store accepts writes via :meth:`add`.
            description: Human-readable description; included in tool descriptions.
            max_search_results: Default maximum results per search.
            extraction: Automatic-extraction configuration. ``True`` enables it with
                a key-aware extractor that reuses existing topic headings; an
                :class:`ExtractionConfig` overrides the extractor/trigger/filter.
        """
        self.name = name
        self.writable = writable
        self.description = description
        self.max_search_results = max_search_results

        self._namespace = namespace
        self._set = set
        self._index_name = _index_name(set)
        self._client = client if client is not None else get_client(hosts)
        self._write_lock = asyncio.Lock()

        self.extraction: ExtractionConfig | bool | None = self._resolve_extraction(extraction)

    def _resolve_extraction(self, extraction: ExtractionConfig | bool | None) -> ExtractionConfig | bool | None:
        if extraction is None or extraction is False:
            return extraction
        if extraction is True:
            return ExtractionConfig(extractor=self._create_key_aware_extractor())
        if "extractor" not in extraction or extraction.get("extractor") is None:
            result = ExtractionConfig(extractor=self._create_key_aware_extractor())
            if "trigger" in extraction:
                result["trigger"] = extraction["trigger"]
            if "filter" in extraction:
                result["filter"] = extraction["filter"]
            return result
        return extraction

    def _create_key_aware_extractor(self) -> Extractor:
        """Create an extractor that injects existing topic headings so the model reuses them."""
        store = self

        class _KeyAwareExtractor:
            async def extract(
                self, messages: list[Message], context: ExtractorContext | None = None
            ) -> list[ExtractionResult]:
                headings = await asyncio.to_thread(store._list_headings)

                prompt = f"{_DEFAULT_EXTRACTION_FRAMING}\n\n{_OUTPUT_CONTRACT}"
                if headings:
                    prompt += (
                        f"\n\nExisting topics: {', '.join(headings)}. "
                        "Reuse an existing topic heading when new facts belong to it."
                    )

                return await ModelExtractor(system_prompt=prompt).extract(messages, context)

        return _KeyAwareExtractor()

    async def initialize(self) -> None:
        """Idempotently create the list secondary index backing :meth:`search`."""
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        try:
            self._client.index_list_create(
                self._namespace, self._set, _TOK_BIN, aerospike.INDEX_STRING, self._index_name
            )
        except aerospike_exception.IndexFoundError:
            pass

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        """Search entries by list-index-backed lexical token matching.

        Args:
            query: Natural-language search query.
            options: Optional search configuration (e.g. max_search_results).

        Returns:
            Top matches ranked by token-overlap count, best first.

        Raises:
            ValueError: If ``options["max_search_results"]`` is less than 1.
        """
        option_max = options.get("max_search_results") if options else None
        if option_max is not None and option_max < 1:
            raise ValueError("max_search_results must be >= 1")
        max_results = (
            option_max
            if option_max is not None
            else self.max_search_results
            if self.max_search_results is not None
            else _DEFAULT_MAX_SEARCH_RESULTS
        )

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scored = await asyncio.to_thread(self._search_sync, query_tokens)
        scored.sort(key=lambda item: item[0], reverse=True)

        entries: list[MemoryEntry] = []
        for _, content, metadata in scored[:max_results]:
            entries.append(MemoryEntry(content=content, metadata=metadata))
        return entries

    def _search_sync(self, query_tokens: set[str]) -> list[tuple[int, str, Metadata | None]]:
        scope_filter = exp.Eq(exp.StrBin(_STORE_BIN), self.name).compile()
        hits: dict[str, tuple[int, str, Metadata | None]] = {}

        try:
            for token in query_tokens:
                query = self._client.query(self._namespace, self._set)
                query.where(predicates.contains(_TOK_BIN, aerospike.INDEX_TYPE_LIST, token))
                for record_key, _, bins in query.results(policy={"expressions": scope_filter}):
                    key = record_key[2]
                    count, content, metadata = hits.get(key, (0, bins.get(_TXT_BIN, ""), _decode_metadata(bins)))
                    hits[key] = (count + 1, content, metadata)
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Aerospike memory search failed for store '{self.name}'") from error

        return list(hits.values())

    async def add(self, content: str, metadata: Metadata | None = None) -> str:
        """Add a knowledge entry to the store.

        The key is derived from the first line of content (slugified, truncated to
        50 chars). If an entry with the same slug already exists, new facts (lines
        after the heading) are appended rather than overwriting.

        Args:
            content: The knowledge content to store.
            metadata: Optional metadata to store and return alongside the entry.

        Returns:
            The record key the entry was written under.
        """
        lines = content.split("\n")
        first_line = re.sub(r"^#+\s*", "", lines[0])
        slug = _slugify(first_line) or f"entry-{int(time.time() * 1000)}"
        record_key = f"{self.name}:{slug}"

        async with self._write_lock:
            await asyncio.to_thread(self._add_sync, record_key, content, lines, metadata)

        return record_key

    def _add_sync(self, record_key: str, content: str, lines: list[str], metadata: Metadata | None) -> None:
        try:
            _, _, existing_bins = self._client.get((self._namespace, self._set, record_key))
            existing_content = existing_bins.get(_TXT_BIN, "")
            new_facts = "\n".join(lines[1:]).strip()
            merged = f"{existing_content.rstrip()}\n{new_facts}" if new_facts else existing_content
        except aerospike_exception.RecordNotFound:
            merged = content

        bins: dict[str, Any] = {
            _TXT_BIN: merged,
            _TOK_BIN: sorted(_tokenize(merged)),
            _STORE_BIN: self.name,
        }
        if metadata is not None:
            bins[_MD_BIN] = json.dumps(metadata, ensure_ascii=False)

        try:
            self._client.put((self._namespace, self._set, record_key), bins)
        except aerospike_exception.AerospikeError as error:
            raise StorageError(f"Failed to write memory entry '{record_key}'") from error

    def _list_headings(self) -> list[str]:
        """List existing entry slugs for this store, for the key-aware extractor."""
        scope_filter = exp.Eq(exp.StrBin(_STORE_BIN), self.name).compile()
        prefix = f"{self.name}:"
        headings: list[str] = []
        scan = self._client.scan(self._namespace, self._set)
        scan.foreach(
            lambda record: headings.append(record[0][2].removeprefix(prefix).replace("-", " ")),
            {"expressions": scope_filter},
        )
        return headings


def _decode_metadata(bins: dict[str, Any]) -> Metadata | None:
    """Decode the JSON-encoded metadata bin, if present."""
    raw = bins.get(_MD_BIN)
    if raw is None:
        return None
    return json.loads(raw)  # type: ignore[no-any-return]
