from reliability_lab.cache import ResponseCache


def test_semantic_cache_should_not_false_hit_different_intent() -> None:
    """Improved similarity + false-hit guardrails should prevent matching
    queries with different 4-digit numbers (years, IDs).

    Previously xfail — now passes with character 3-gram cosine similarity
    and _looks_like_false_hit() guardrail.
    """
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None


def test_privacy_query_not_cached_in_memory() -> None:
    """Privacy-sensitive queries should not be stored or retrieved from cache."""
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("account balance for user 123", "Balance: $500")
    cached, _ = cache.get("account balance for user 123")
    assert cached is None
