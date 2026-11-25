"""Async Redis adapter (caching, session memory, HITL confirmation TTL tokens) and Observability adapter."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

from app.domain.models import ChatMessage, PendingConfirmation

logger = logging.getLogger(__name__)


class AsyncRedisAdapter:
    """Implements CachePort & ConfirmationStorePort with automatic in-memory fallback."""

    def __init__(
        self,
        redis_url: str,
        confirmation_ttl_seconds: int = 600,
        chat_history_ttl_seconds: int = 86400,
    ) -> None:
        self._confirmation_ttl = confirmation_ttl_seconds
        self._chat_history_ttl = chat_history_ttl_seconds
        self._mem_kv: dict[str, tuple[str, float | None]] = {}
        self._mem_lists: dict[str, list[str]] = defaultdict(list)
        self._redis = None
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url, decode_responses=True)
        except Exception as exc:
            logger.warning("Redis client init failed (%s); using in-memory store.", exc)

    def _mem_set(self, key: str, raw: str, ttl_seconds: int) -> None:
        deadline = time.monotonic() + ttl_seconds if ttl_seconds > 0 else time.monotonic()
        self._mem_kv[key] = (raw, deadline)

    def _mem_get(self, key: str) -> str | None:
        entry = self._mem_kv.get(key)
        if entry is None:
            return None
        raw, deadline = entry
        if deadline is not None and time.monotonic() >= deadline:
            self._mem_kv.pop(key, None)
            return None
        return raw

    def _mem_pop(self, key: str) -> str | None:
        entry = self._mem_kv.pop(key, None)
        if entry is None:
            return None
        raw, deadline = entry
        if deadline is not None and time.monotonic() >= deadline:
            return None
        return raw

    async def get_json(self, key: str) -> Any | None:
        try:
            if self._redis is not None:
                raw = await self._redis.get(key)
                return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("Redis GET fallback for key=%s: %s", key, exc)
        raw = self._mem_get(key)
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        encoded = json.dumps(value, default=str)
        try:
            if self._redis is not None:
                if ttl_seconds > 0:
                    await self._redis.set(key, encoded, ex=ttl_seconds)
                else:
                    await self._redis.delete(key)
                return
        except Exception as exc:
            logger.warning("Redis SET fallback for key=%s: %s", key, exc)
        self._mem_set(key, encoded, ttl_seconds)

    async def append_chat_message(self, session_id: str, msg: ChatMessage) -> None:
        key = f"chat_history:{session_id}"
        encoded = msg.model_dump_json()
        try:
            if self._redis is not None:
                await self._redis.rpush(key, encoded)
                await self._redis.ltrim(key, -20, -1)
                await self._redis.expire(key, self._chat_history_ttl)
                return
        except Exception as exc:
            logger.warning("Redis RPUSH fallback: %s", exc)
        self._mem_lists[key].append(encoded)
        self._mem_lists[key] = self._mem_lists[key][-20:]

    async def get_chat_history(self, session_id: str, limit: int = 12) -> list[ChatMessage]:
        key = f"chat_history:{session_id}"
        raw_items: list[str] = []
        try:
            if self._redis is not None:
                redis_items = await self._redis.lrange(key, -limit, -1)
                raw_items = [
                    item if isinstance(item, str) else item.decode("utf-8") for item in redis_items
                ]
            else:
                raw_items = self._mem_lists[key][-limit:]
        except Exception as exc:
            logger.warning("Redis LRANGE fallback: %s", exc)
            raw_items = self._mem_lists[key][-limit:]
        return [ChatMessage.model_validate_json(item) for item in raw_items]

    async def save_pending(self, confirmation: PendingConfirmation) -> None:
        key = f"hitl_confirm:{confirmation.user_id}:{confirmation.token}"
        await self.set_json(
            key, confirmation.model_dump(mode="json"), ttl_seconds=self._confirmation_ttl
        )

    async def pop_pending(self, token: str, user_id: str) -> PendingConfirmation | None:
        key = f"hitl_confirm:{user_id}:{token}"
        try:
            if self._redis is not None:
                pipe = self._redis.pipeline()
                pipe.get(key)
                pipe.delete(key)
                raw, _ = await pipe.execute()
                return PendingConfirmation.model_validate_json(raw) if raw else None
        except Exception as exc:
            logger.warning("Redis POP fallback: %s", exc)
        raw = self._mem_pop(key)
        return PendingConfirmation.model_validate_json(raw) if raw else None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


class StructuredObservabilityAdapter:
    """Implements ObservabilityPort with structured logs, in-memory metrics, and optional Langfuse tracing."""

    def __init__(
        self,
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_host: str = "https://cloud.langfuse.com",
    ) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._recent_spans: list[dict[str, Any]] = []
        self._langfuse = None
        if langfuse_public_key and langfuse_secret_key:
            try:
                from langfuse import Langfuse

                self._langfuse = Langfuse(
                    public_key=langfuse_public_key,
                    secret_key=langfuse_secret_key,
                    host=langfuse_host,
                )
            except Exception as exc:
                logger.warning("Langfuse init skipped: %s", exc)

    def start_trace(self, trace_id: str, user_id: str, session_id: str, input_text: str) -> Any:
        logger.info(
            "TRACE_START trace_id=%s user_id=%s session_id=%s input=%r",
            trace_id,
            user_id,
            session_id,
            input_text[:120],
        )
        if self._langfuse is not None:
            try:
                trace_fn = getattr(self._langfuse, "trace", None)
                if callable(trace_fn):
                    return trace_fn(
                        id=trace_id,
                        user_id=user_id,
                        session_id=session_id,
                        input=input_text,
                    )
            except Exception:
                return None
        return None

    def record_span(
        self,
        trace_id: str,
        name: str,
        duration_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "trace_id": trace_id,
            "span": name,
            "duration_ms": round(duration_ms, 2),
            "metadata": metadata or {},
        }
        self._recent_spans.append(entry)
        self._recent_spans = self._recent_spans[-100:]
        logger.info("SPAN %s", json.dumps(entry))

    def increment_metric(self, metric_name: str, value: int = 1) -> None:
        self._counters[metric_name] += value

    def get_metrics_snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self._counters),
            "recent_spans": self._recent_spans[-20:],
        }
