"""Async Redis adapter (caching, session memory, HITL confirmation TTL tokens) and Observability adapter."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.domain.models import ChatMessage, PendingConfirmation, TraceRecord
from app.domain.pii_masker import PIIMasker

logger = logging.getLogger(__name__)


class AsyncRedisAdapter:
    """Implements CachePort & ConfirmationStorePort with automatic in-memory fallback."""

    def __init__(
        self,
        redis_url: str,
        confirmation_ttl_seconds: int = 600,
        chat_history_ttl_seconds: int = 86400,
        failure_cooldown_seconds: float = 60.0,
    ) -> None:
        self._confirmation_ttl = confirmation_ttl_seconds
        self._chat_history_ttl = chat_history_ttl_seconds
        self._cooldown_seconds = max(0.0, failure_cooldown_seconds)
        self._redis_disabled_until: float = 0.0
        self._mem_kv: dict[str, tuple[str, float | None]] = {}
        self._mem_lists: dict[str, list[str]] = defaultdict(list)
        self._redis = None
        if redis_url.strip():
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(redis_url, decode_responses=True)
            except Exception as exc:
                logger.warning("Redis client init failed (%s); using in-memory store.", exc)

    def _redis_available(self) -> bool:
        return self._redis is not None and time.monotonic() >= self._redis_disabled_until

    def _trip_redis_cooldown(self, op: str, exc: Exception) -> None:
        if self._cooldown_seconds > 0:
            self._redis_disabled_until = time.monotonic() + self._cooldown_seconds
            logger.warning(
                "Redis %s failed (%s); pausing Redis attempts for %.0fs and using in-memory fallback.",
                op,
                exc,
                self._cooldown_seconds,
            )
        else:
            logger.warning("Redis %s fallback: %s", op, exc)

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
            if self._redis_available() and self._redis is not None:
                raw = await self._redis.get(key)
                return json.loads(raw) if raw else None
        except Exception as exc:
            self._trip_redis_cooldown(f"GET({key})", exc)
        raw = self._mem_get(key)
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        encoded = json.dumps(value, default=str)
        try:
            if self._redis_available() and self._redis is not None:
                if ttl_seconds > 0:
                    await self._redis.set(key, encoded, ex=ttl_seconds)
                else:
                    await self._redis.delete(key)
                return
        except Exception as exc:
            self._trip_redis_cooldown(f"SET({key})", exc)
        self._mem_set(key, encoded, ttl_seconds)

    async def append_chat_message(self, session_id: str, msg: ChatMessage) -> None:
        key = f"chat_history:{session_id}"
        encoded = msg.model_dump_json()
        try:
            if self._redis_available() and self._redis is not None:
                await self._redis.rpush(key, encoded)
                await self._redis.ltrim(key, -20, -1)
                await self._redis.expire(key, self._chat_history_ttl)
                return
        except Exception as exc:
            self._trip_redis_cooldown("RPUSH", exc)
        self._mem_lists[key].append(encoded)
        self._mem_lists[key] = self._mem_lists[key][-20:]

    async def get_chat_history(self, session_id: str, limit: int = 12) -> list[ChatMessage]:
        key = f"chat_history:{session_id}"
        raw_items: list[str] = []
        try:
            if self._redis_available() and self._redis is not None:
                redis_items = await self._redis.lrange(key, -limit, -1)
                raw_items = [
                    item if isinstance(item, str) else item.decode("utf-8") for item in redis_items
                ]
            else:
                raw_items = self._mem_lists[key][-limit:]
        except Exception as exc:
            self._trip_redis_cooldown("LRANGE", exc)
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
            if self._redis_available() and self._redis is not None:
                pipe = self._redis.pipeline()
                pipe.get(key)
                pipe.delete(key)
                raw, _ = await pipe.execute()
                return PendingConfirmation.model_validate_json(raw) if raw else None
        except Exception as exc:
            self._trip_redis_cooldown("POP", exc)
        raw = self._mem_pop(key)
        return PendingConfirmation.model_validate_json(raw) if raw else None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


class StructuredObservabilityAdapter:
    """Implements ObservabilityPort with structured logs, JSONL trace persistence, and metrics."""

    def __init__(
        self,
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_host: str = "https://cloud.langfuse.com",
        trace_log_path: str = "",
        pii_masker: PIIMasker | None = None,
    ) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._recent_spans: list[dict[str, Any]] = []
        self._latencies_ms: list[float] = []
        self._trace_log_path = trace_log_path
        self._recent_traces: list[TraceRecord] = (
            self._load_traces_from_disk()[-200:] if trace_log_path else []
        )
        self._pii_masker = pii_masker or PIIMasker()
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
        safe_input = self._pii_masker.mask_text(input_text)[0]
        logger.info(
            "TRACE_START trace_id=%s user_id=%s session_id=%s input=%r",
            trace_id,
            user_id,
            session_id,
            safe_input[:120],
        )
        if self._langfuse is not None:
            try:
                trace_fn = getattr(self._langfuse, "trace", None)
                if callable(trace_fn):
                    return trace_fn(
                        id=trace_id,
                        user_id=user_id,
                        session_id=session_id,
                        input=safe_input,
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

    def finish_trace(self, record: TraceRecord) -> None:
        """Scrub residual PII, update in-memory metrics, and persist JSONL trace record."""
        safe_record = record.model_copy(
            update={
                "pii_redacted_prompt": self._pii_masker.mask_text(record.pii_redacted_prompt)[0],
                "pii_redacted_response": self._pii_masker.mask_text(record.pii_redacted_response)[
                    0
                ],
                "generated_sql": (
                    self._pii_masker.mask_text(record.generated_sql)[0]
                    if record.generated_sql is not None
                    else None
                ),
                "error_type": (
                    self._pii_masker.mask_text(record.error_type)[0]
                    if record.error_type is not None
                    else None
                ),
            }
        )
        self._recent_traces.append(safe_record)
        self._recent_traces = self._recent_traces[-200:]
        self._latencies_ms.append(safe_record.latency_ms)
        self._latencies_ms = self._latencies_ms[-500:]
        if safe_record.fallback_used:
            self._counters["llm_fallback_total"] += 1
        if safe_record.error_type:
            self._counters["errors_total"] += 1

        logger.info(
            "TRACE_END trace_id=%s intent=%s ast_valid=%s bytes=%d llm_calls=%d cost=%.6f latency_ms=%.2f error=%s",
            safe_record.trace_id,
            safe_record.intent_classified,
            safe_record.ast_valid,
            safe_record.bq_bytes_billed,
            safe_record.llm_calls_count,
            safe_record.total_cost_usd,
            safe_record.latency_ms,
            safe_record.error_type,
        )
        if self._trace_log_path:
            try:
                path = Path(self._trace_log_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(safe_record.model_dump_json() + "\n")
            except OSError as exc:
                logger.warning("Could not write trace log to %s: %s", self._trace_log_path, exc)

    def _load_traces_from_disk(self) -> list[TraceRecord]:
        if not self._trace_log_path:
            return []
        path = Path(self._trace_log_path)
        if not path.is_file():
            return []
        records: list[TraceRecord] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        records.append(TraceRecord.model_validate_json(stripped))
                    except Exception:
                        continue
        except OSError as exc:
            logger.warning("Could not read trace log from %s: %s", self._trace_log_path, exc)
        return records

    def get_recent_traces(self, limit: int = 10, user_id: str | None = None) -> list[TraceRecord]:
        source = self._recent_traces if self._recent_traces else self._load_traces_from_disk()
        filtered = [t for t in source if user_id is None or t.user_id == user_id]
        return list(reversed(filtered[-max(1, limit) :]))

    def get_trace(self, trace_id: str, user_id: str | None = None) -> TraceRecord | None:
        normalized = trace_id.strip()
        if not normalized:
            return None
        source = self._recent_traces if self._recent_traces else self._load_traces_from_disk()
        scoped = [t for t in source if user_id is None or t.user_id == user_id]
        for trace in reversed(scoped):
            if trace.trace_id == normalized:
                return trace
        if len(normalized) >= 4:
            prefix_matches = [t for t in scoped if t.trace_id.startswith(normalized)]
            if len(prefix_matches) == 1:
                return prefix_matches[0]
        return None

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = max(0, math.ceil((pct / 100.0) * len(ordered)) - 1)
        return round(ordered[idx], 2)

    def get_metrics_snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self._counters),
            "latency_ms": {
                "count": len(self._latencies_ms),
                "p50": self._percentile(self._latencies_ms, 50),
                "p95": self._percentile(self._latencies_ms, 95),
                "p99": self._percentile(self._latencies_ms, 99),
            },
            "trace_log_path": self._trace_log_path,
            "recent_spans": self._recent_spans[-20:],
        }
