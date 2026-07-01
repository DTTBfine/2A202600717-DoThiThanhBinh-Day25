from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from reliability_lab import logger

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory semantic cache with n-gram cosine similarity and privacy/false-hit guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            logger.emit("cache.privacy_block", backend="memory", query=query[:60])
            return None, 0.0

        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        best_score = 0.0
        best_entry: CacheEntry | None = None
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_score >= self.similarity_threshold and best_entry is not None:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_entry.key,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    }
                )
                logger.emit(
                    "cache.false_hit",
                    backend="memory",
                    query=query[:60],
                    cached_key=best_entry.key[:60],
                    score=round(best_score, 4),
                )
                return None, best_score
            logger.emit("cache.hit", backend="memory", query=query[:60], score=round(best_score, 4))
            return best_entry.value, best_score

        logger.emit("cache.miss", backend="memory", query=query[:60], best_score=round(best_score, 4))
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            logger.emit("cache.set_blocked", backend="memory", reason="privacy", query=query[:60])
            return
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {},
            )
        )
        logger.emit("cache.set", backend="memory", query=query[:60])

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Cosine similarity over word tokens + character 3-grams."""
        if a == b:
            return 1.0

        def tokenize(text: str) -> list[str]:
            words = text.lower().split()
            trigrams = [word[i : i + 3] for word in words for i in range(len(word) - 2)]
            return words + trigrams

        vec_a: Counter[str] = Counter(tokenize(a))
        vec_b: Counter[str] = Counter(tokenize(b))

        dot = sum(vec_a[k] * vec_b[k] for k in vec_a if k in vec_b)
        mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
        mag_b = math.sqrt(sum(v * v for v in vec_b.values()))

        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model:
        Key   = "{prefix}{md5_hash_of_query[:12]}"
        Value = Redis Hash with fields: "query", "response"
        TTL   = set via EXPIRE — no manual eviction needed
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            logger.emit("cache.privacy_block", backend="redis", query=query[:60])
            return None, 0.0

        # Exact-match fast path via hash key
        exact_key = f"{self.prefix}{self._query_hash(query)}"
        response: str | None = self._redis.hget(exact_key, "response")
        if response is not None:
            logger.emit("cache.hit", backend="redis", query=query[:60], score=1.0, match="exact")
            return response, 1.0

        # Similarity scan over all cached entries
        best_score = 0.0
        best_response: str | None = None
        best_cached_query: str | None = None

        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query: str | None = self._redis.hget(key, "query")
            if cached_query is None:
                continue
            score = ResponseCache.similarity(query, cached_query)
            if score > best_score:
                best_score = score
                best_response = self._redis.hget(key, "response")
                best_cached_query = cached_query

        if best_score >= self.similarity_threshold and best_response is not None:
            if _looks_like_false_hit(query, best_cached_query or ""):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_cached_query,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    }
                )
                logger.emit(
                    "cache.false_hit",
                    backend="redis",
                    query=query[:60],
                    cached_key=(best_cached_query or "")[:60],
                    score=round(best_score, 4),
                )
                return None, best_score
            logger.emit(
                "cache.hit",
                backend="redis",
                query=query[:60],
                score=round(best_score, 4),
                match="similarity",
            )
            return best_response, best_score

        logger.emit("cache.miss", backend="redis", query=query[:60], best_score=round(best_score, 4))
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            logger.emit("cache.set_blocked", backend="redis", reason="privacy", query=query[:60])
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)
        logger.emit("cache.set", backend="redis", query=query[:60], key=key)

    def flush(self) -> None:
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
