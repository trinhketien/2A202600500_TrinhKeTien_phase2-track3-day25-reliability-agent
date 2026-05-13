from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

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


def _char_ngrams(text: str, n: int = 3) -> Counter[str]:
    """Generate character n-gram frequency counter for a text."""
    text = text.lower().strip()
    grams: Counter[str] = Counter()
    for i in range(len(text) - n + 1):
        grams[text[i : i + n]] += 1
    return grams


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory cache with improved similarity, TTL, and false-hit guardrails.

    Improvements over baseline:
    - Character 3-gram similarity (cosine) instead of naive Jaccard token overlap.
    - Exact-match fast-path returns score 1.0 immediately.
    - Privacy-sensitive queries are never cached or returned from cache.
    - False-hit detection blocks matches where 4-digit numbers (years/IDs) differ.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        # Privacy guard — never serve cached results for sensitive queries
        if _is_uncacheable(query):
            return None, 0.0

        best_value: str | None = None
        best_score = 0.0
        best_key: str = ""
        now = time.time()
        # Evict expired entries
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key

        if best_score >= self.similarity_threshold:
            # False-hit guard — block matches where years/IDs differ
            if _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        # Privacy guard — never cache sensitive queries
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Improved similarity using character 3-gram cosine similarity.

        This approach is better than Jaccard token overlap because:
        - Captures sub-word patterns (e.g., "2024" vs "2026" produce different n-grams).
        - More robust to word-order variation.
        - Exact match returns 1.0 via fast-path.
        """
        a_clean = a.lower().strip()
        b_clean = b.lower().strip()
        # Fast path: exact match
        if a_clean == b_clean:
            return 1.0
        if not a_clean or not b_clean:
            return 0.0

        vec_a = _char_ngrams(a_clean, 3)
        vec_b = _char_ngrams(b_clean, 3)

        # Cosine similarity
        common_keys = set(vec_a.keys()) & set(vec_b.keys())
        dot = sum(vec_a[k] * vec_b[k] for k in common_keys)
        mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
        mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model:
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    Features:
    - Exact-match lookup via deterministic hash key.
    - Similarity scan over all cached entries using character 3-gram cosine.
    - Privacy guardrails: sensitive queries are never cached or returned.
    - False-hit detection: queries with different 4-digit numbers are blocked.
    - Graceful degradation: all Redis errors are caught — returns (None, 0.0) instead of crashing.
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
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Steps:
        1. Return (None, 0.0) if query is privacy-sensitive.
        2. Try exact-match via hash key.
        3. Similarity scan over all cached entries.
        4. Apply false-hit detection before returning.
        """
        # 1. Privacy guard
        if _is_uncacheable(query):
            return None, 0.0

        try:
            # 2. Exact-match lookup
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if exact_response is not None:
                return exact_response, 1.0

            # 3. Similarity scan
            best_score = 0.0
            best_response: str | None = None
            best_cached_query: str = ""

            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_response = self._redis.hget(key, "response")
                    best_cached_query = cached_query

            if best_score >= self.similarity_threshold and best_response is not None:
                # 4. False-hit detection
                if _looks_like_false_hit(query, best_cached_query):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_query": best_cached_query,
                            "score": best_score,
                            "reason": "different_year_or_id",
                        }
                    )
                    return None, best_score
                return best_response, best_score

            return None, best_score

        except Exception:
            # Graceful degradation — Redis down, don't crash
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        Steps:
        1. Skip if query is privacy-sensitive.
        2. Build key from query hash.
        3. Store as Redis Hash with "query" and "response" fields.
        4. Set TTL via EXPIRE.
        """
        # 1. Privacy guard
        if _is_uncacheable(query):
            return

        try:
            # 2. Build key
            key = f"{self.prefix}{self._query_hash(query)}"
            # 3. Store as Redis Hash
            mapping: dict[str, str] = {"query": query, "response": value}
            if metadata:
                for mk, mv in metadata.items():
                    mapping[f"meta:{mk}"] = mv
            self._redis.hset(key, mapping=mapping)
            # 4. Set TTL
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            # Graceful degradation — Redis down, don't crash
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
