"""Cross-conversation memory using LangGraph Store (PostgreSQL-backed).

Provides long-term user memory that persists across conversations,
enabling the agent to remember user preferences and past interactions.

Uses ``AsyncPostgresStore`` over the shared pool owned by ``_pool_utils``.
Falls back to ``InMemoryStore`` only when ``ENVIRONMENT == "testing"`` or
the Postgres connection fails while durable agent state is not required
(``settings.require_durable_agent_state`` — local/CI throwaway envs or
the ``ALLOW_MEMORY_FALLBACK`` override); every other environment raises
on init failure instead of degrading silently.

Pool lifecycle lives in ``_pool_utils``; this module never closes it.
"""

import asyncio
import logging
import re

from src.core.config import get_settings
from src.services.agent._pii_redact import redact_pii
from src.services.agent._pool_utils import (
    get_shared_langgraph_pool,
    require_durable_or_fallback,
)
from src.services.agent.checkpointer import get_db_uri

logger = logging.getLogger(__name__)

_store = None
_store_lock = asyncio.Lock()


def _build_memory_index_config() -> dict | None:
    """Return the LangGraph store index config when Cohere is available.

    AsyncPostgresStore returns ranked, scored search results only when an
    ``index=`` arg with an embedder is supplied at construction. Without
    it, ``asearch`` falls back to recency/exact-key match and every
    recalled item carries ``score=None`` (trace evidence: 019e05ad,
    019e040b on rag-agent-dev).

    Returns ``None`` when Cohere is not configured so callers can fall
    back to the un-indexed store rather than crashing on startup.
    """
    try:
        from src.services.embedding.cohere_embed_service import cohere_embed_service
    except Exception as exc:  # noqa: BLE001 - import failure must not crash
        logger.warning("Cohere import failed; memory index disabled: %s", exc)
        return None

    if not cohere_embed_service.is_enabled:
        logger.info("Cohere embedding disabled (no API key); memory index disabled")
        return None

    async def _embed(texts):
        # AsyncPostgresStore feeds the same callable for both index time
        # and query time. Use "search_document" since most calls are
        # writes; query-time mismatch is a small quality dip Cohere
        # tolerates fine.
        return await cohere_embed_service.embed_texts(
            list(texts), input_type="search_document"
        )

    return {
        "embed": _embed,
        "dims": cohere_embed_service.dimensions,
        "fields": ["query"],
    }


async def get_memory_store():
    """Return the long-term memory store singleton."""
    global _store

    if _store is not None:
        return _store

    async with _store_lock:
        if _store is not None:
            return _store

        settings = get_settings()
        if settings.ENVIRONMENT == "testing":
            from langgraph.store.memory import InMemoryStore

            _store = InMemoryStore()
            logger.info("Memory store initialised (InMemoryStore — testing env)")
            return _store

        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore

            pool = await get_shared_langgraph_pool(get_db_uri())
            index_config = _build_memory_index_config()
            if index_config is not None:
                store = AsyncPostgresStore(pool, index=index_config)
            else:
                store = AsyncPostgresStore(pool)
            # Assign the singleton only after setup() succeeds — assigning
            # earlier would leave a half-initialised store (tables missing)
            # in the global when setup() raises, and every later call would
            # short-circuit on ``is not None`` and hand it out.
            await store.setup()
            _store = store
            # Diagnostic: confirm backend class + whether semantic index is
            # wired. Trace evidence shows asearch returns score=None for
            # every recalled entry when index= is unset. Log makes the
            # runtime state obvious.
            indexed = bool(
                getattr(store, "_index", None)
                or getattr(store, "index_config", None)
                or index_config is not None
            )
            logger.info(
                "Memory store initialised (%s, indexed=%s, dims=%s)",
                type(store).__name__,
                indexed,
                (index_config or {}).get("dims") if index_config else None,
            )
            return _store
        except Exception as e:
            # Raises when durable state is required; the singleton stays
            # None so the next call retries with a clean slate.
            require_durable_or_fallback("Postgres memory store", e)

            logger.error(
                "Postgres memory store UNAVAILABLE — falling back to "
                "InMemoryStore (permitted: throwaway env or "
                "ALLOW_MEMORY_FALLBACK). Long-term memory will NOT survive "
                "restarts. Cause: %s",
                e,
            )
            from langgraph.store.memory import InMemoryStore

            _store = InMemoryStore()
            return _store


async def reset_memory_store() -> None:
    """Clear the singleton so the next call reconnects.

    Does NOT close the shared pool — owned by ``_pool_utils``.
    """
    global _store

    async with _store_lock:
        _store = None
    logger.info("Memory store singleton reset — will reconnect on next use")


async def close_memory_store() -> None:
    """Drop the singleton at app shutdown. Pool closed separately."""
    await reset_memory_store()


def _memory_namespace(user_id: str, organization_id: str | None = None) -> tuple:
    """Return the store namespace for one user's memories.

    Org-scoped when the caller knows the tenant, so two users that happen to
    share a ``user_id`` string across tenants can never read each other's
    memories. Falls back to the legacy ``("user", user_id)`` namespace when
    no org is known — older entries written before this change stay under the
    legacy namespace and simply become unreachable for org-scoped callers
    (memory is best-effort; no migration).
    """
    org = str(organization_id or "").strip()
    return (org, "user", user_id) if org else ("user", user_id)


async def search_memories(
    store,
    user_id: str,
    query: str,
    limit: int = 5,
    organization_id: str | None = None,
) -> list:
    """Search for relevant memories for a user."""
    if store is None:
        return []

    try:
        namespace = _memory_namespace(user_id, organization_id)
        results = await store.asearch(namespace, query=query, limit=limit)
        return [
            {
                "key": item.key,
                "value": item.value,
                "score": getattr(item, "score", 0.0),
            }
            for item in results
        ]
    except Exception as e:
        logger.warning("Memory search failed: %s", e)
        return []


async def save_memory(
    store,
    user_id: str,
    key: str,
    value: dict,
    organization_id: str | None = None,
) -> bool:
    """Save a memory for a user."""
    if store is None:
        return False

    try:
        namespace = _memory_namespace(user_id, organization_id)
        await store.aput(namespace, key, value)
        logger.debug("Saved memory for user %s: %s", user_id, key)
        return True
    except Exception as e:
        logger.warning("Memory save failed: %s", e)
        return False


# Minimum similarity score before forget_memory will actually delete.
# Trace 019e040b style false-recalls (score ~0.06) would otherwise let
# a stray "forget" request wipe an unrelated memory. 0.6 is empirical —
# tune if FN/FP rate is wrong after deploy.
_FORGET_SCORE_THRESHOLD: float = 0.6
_FORGET_BOILERPLATE = {
    "a",
    "about",
    "all",
    "an",
    "anything",
    "data",
    "delete",
    "each",
    "every",
    "everything",
    "for",
    "forget",
    "going",
    "i",
    "information",
    "is",
    "it",
    "me",
    "memories",
    "memory",
    "my",
    "nothing",
    "of",
    "one",
    "ones",
    "please",
    "remove",
    "remember",
    "single",
    "something",
    "stored",
    "stuff",
    "that",
    "the",
    "thing",
    "things",
    "this",
    "to",
    "what",
    "you",
}


async def delete_memory_by_query(
    store,
    user_id: str,
    query: str,
    limit: int = 3,
    organization_id: str | None = None,
) -> dict:
    """Find the top semantic matches for *query* under the user's namespace,
    delete those above the safety threshold, and return a report.

    Returns ``{"deleted": int, "matches": [{key, score, query}]}``.
    """
    if store is None or not query:
        return {"deleted": 0, "matches": []}

    namespace = _memory_namespace(user_id, organization_id)
    try:
        results = await store.asearch(namespace, query=query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        # B8-S1: never report a failed search as "nothing matched" — the
        # caller turns that into {"status": "completed", "deleted": 0} for a
        # HITL-approved destructive action that never ran.
        logger.warning("forget_memory: asearch failed: %s", exc)
        raise

    matches = [
        {
            "key": item.key,
            "score": float(getattr(item, "score", 0.0) or 0.0),
            "query": (getattr(item, "value", None) or {}).get("query", ""),
        }
        for item in results
    ]

    # Detect the degraded "no semantic index" state. When the store was built
    # without an embedder (the default config — Cohere unset), asearch cannot
    # rank and returns score=None for every item (coerced to 0.0 above), so the
    # 0.6 threshold rejects everything and a user-confirmed forget silently
    # deletes nothing. In that state, fall back to an exact case-insensitive
    # substring match against each memory's stored text so the confirmed forget
    # actually takes effect — without the recency-deletion footgun of blindly
    # bypassing the threshold (un-indexed asearch returns recents, not query
    # matches), so we only delete recents that genuinely mention a query with
    # at least three meaningful non-PII topic tokens.
    unranked = bool(results) and all(
        getattr(item, "score", None) is None for item in results
    )
    needle = query.strip().casefold()
    redacted_query = re.sub(
        r"<[^>]+>|\[redacted_[^]]+\]", " ", redact_pii(query), flags=re.IGNORECASE
    )
    query_topics = {
        token
        for token in re.findall(r"\w+", redacted_query.casefold())
        if token not in _FORGET_BOILERPLATE
    }

    deleted = 0
    for m, item in zip(matches, results):
        should_delete = m["score"] >= _FORGET_SCORE_THRESHOLD
        if not should_delete and unranked and needle and len(query_topics) >= 3:
            value = getattr(item, "value", None) or {}
            haystack = " ".join(
                str(v) for v in value.values() if isinstance(v, str)
            ).casefold()
            should_delete = needle in haystack
            if not should_delete:
                redacted_haystack = re.sub(
                    r"<[^>]+>|\[redacted_[^]]+\]",
                    " ",
                    redact_pii(haystack),
                    flags=re.IGNORECASE,
                )
                stored_topics = {
                    token
                    for token in re.findall(r"\w+", redacted_haystack.casefold())
                    if token not in _FORGET_BOILERPLATE
                }
                should_delete = query_topics <= stored_topics
        if not should_delete:
            continue
        try:
            await store.adelete(namespace, m["key"])
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("forget_memory: adelete failed for %s: %s", m["key"], exc)

    if unranked:
        logger.warning(
            "forget_memory: store has no semantic index; used text "
            "fallback for query=%r (deleted=%d of %d candidates)",
            query,
            deleted,
            len(matches),
        )

    return {"deleted": deleted, "matches": matches}
