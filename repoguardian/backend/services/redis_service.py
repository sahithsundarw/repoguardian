"""
Redis service — event queue (Streams) and ephemeral state (key/value).

Redis Streams are used for durable, at-least-once event delivery:
  - Producer (webhook handler) → publishes WebhookEvent to the stream
  - Consumer (worker) → reads from stream in consumer group

Redis key/value is used for:
  - HITL workflow state (finding_id → current status)
  - Per-repo daily rate limit counters
  - Agent result caching
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from backend.config import get_settings
from backend.models.schemas import WebhookEvent

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Singleton connection ───────────────────────────────────────────────────────

_redis_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return (and lazily create) the global Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


# ── Stream operations ──────────────────────────────────────────────────────────


class EventQueueProducer:
    """Publishes WebhookEvents onto the Redis Stream."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client
        self._stream = settings.redis_event_stream
        self._max_len = settings.redis_stream_max_len

    async def publish(self, event: WebhookEvent) -> str:
        """
        Publish an event and return the stream entry ID.
        Uses MAXLEN to automatically trim old entries.
        """
        payload = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "data": event.model_dump_json(),
        }
        entry_id = await self._client.xadd(
            self._stream,
            payload,
            maxlen=self._max_len,
            approximate=True,
        )
        logger.info("Published event %s → stream entry %s", event.event_id, entry_id)
        return entry_id


class EventQueueConsumer:
    """
    Reads WebhookEvents from the Redis Stream via a consumer group.
    Supports at-least-once delivery with explicit ACK.
    """

    def __init__(self, client: aioredis.Redis, consumer_name: str) -> None:
        self._client = client
        self._stream = settings.redis_event_stream
        self._group = settings.redis_consumer_group
        self._consumer = consumer_name

    async def ensure_group(self) -> None:
        """Create the consumer group if it doesn't exist."""
        try:
            await self._client.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            logger.info("Created consumer group '%s'", self._group)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def read_next(
        self,
        count: int = 1,
        block_ms: int | None = None,
    ) -> list[tuple[str, WebhookEvent]]:
        """
        Read up to `count` unprocessed entries from the stream.

        Returns a list of (entry_id, WebhookEvent) tuples.
        If block_ms is set, blocks until a message arrives.
        """
        block = block_ms if block_ms else None
        raw = await self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},  # ">" means only new messages
            count=count,
            block=block,
        )

        results: list[tuple[str, WebhookEvent]] = []
        if not raw:
            return results

        for _stream_name, entries in raw:
            for entry_id, fields in entries:
                try:
                    event = WebhookEvent.model_validate_json(fields["data"])
                    results.append((entry_id, event))
                except Exception as e:
                    logger.error("Failed to deserialise stream entry %s: %s", entry_id, e)
                    await self.ack(entry_id)  # Discard malformed entries
        return results

    async def ack(self, entry_id: str) -> None:
        """Acknowledge successful processing of a stream entry."""
        await self._client.xack(self._stream, self._group, entry_id)
        logger.debug("ACKed stream entry %s", entry_id)

    async def nack_retry(self, entry_id: str) -> None:
        """
        Mark a message for reprocessing by claiming it back into the
        pending entries list without ACKing. The worker will retry.
        """
        logger.warning("NACKing entry %s for retry", entry_id)
        # In Redis Streams, not ACKing leaves it in the PEL (Pending Entry List)
        # The worker's periodic PEL check will reclaim stale entries.


# ── Key/Value state operations ─────────────────────────────────────────────────


class StateStore:
    """
    Thin wrapper around Redis for HITL and rate-limit state management.
    All keys are namespaced under settings.redis_state_prefix.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client
        self._prefix = settings.redis_state_prefix

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    async def set(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        serialised = json.dumps(value) if not isinstance(value, str) else value
        ex = int(ttl.total_seconds()) if ttl else None
        await self._client.set(self._key(key), serialised, ex=ex)

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(self._key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    async def delete(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def increment(self, key: str, ttl: timedelta | None = None) -> int:
        """Atomic increment — used for rate limiting."""
        k = self._key(key)
        count = await self._client.incr(k)
        if count == 1 and ttl:  # set TTL only on first increment
            await self._client.expire(k, int(ttl.total_seconds()))
        return count

    # ── HITL-specific helpers ──────────────────────────────────────────────────

    async def set_finding_status(self, finding_id: str, status: str) -> None:
        await self.set(
            f"hitl:{finding_id}",
            {"status": status},
            ttl=timedelta(days=settings.hitl_timeout_days + 1),
        )

    async def get_finding_status(self, finding_id: str) -> str | None:
        data = await self.get(f"hitl:{finding_id}")
        return data["status"] if data else None

    # ── Rate-limit helpers ─────────────────────────────────────────────────────

    async def check_rate_limit(self, repo_full_name: str) -> bool:
        """
        Returns True if the repo is within its daily review limit.
        Increments the counter atomically.
        """
        key = f"rate:daily:{repo_full_name}"
        count = await self.increment(key, ttl=timedelta(hours=24))
        return count <= settings.max_reviews_per_repo_daily

    # ── Result cache ───────────────────────────────────────────────────────────

    async def cache_result(self, cache_key: str, result: dict, ttl_hours: int = 24) -> None:
        await self.set(f"cache:{cache_key}", result, ttl=timedelta(hours=ttl_hours))

    async def get_cached_result(self, cache_key: str) -> dict | None:
        return await self.get(f"cache:{cache_key}")
