from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    # Route now includes provider name, e.g. "primary:primary", "fallback:backup", "cache_hit:0.95"
    assert any(result.route.startswith(prefix) for prefix in ("primary", "fallback", "cache_hit", "static_fallback"))


def test_gateway_fallback_when_primary_fails() -> None:
    """Verify fallback chain: when primary circuit opens, backup serves requests."""
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.006)
    breaker_p = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=5)
    breaker_b = CircuitBreaker("backup", failure_threshold=5, reset_timeout_seconds=5)
    gateway = ReliabilityGateway(
        [primary, backup],
        {"primary": breaker_p, "backup": breaker_b},
    )
    # First requests: primary fails, backup serves
    for _ in range(5):
        result = gateway.complete("test query")
        assert result.text
        assert result.route.startswith("fallback") or result.route == "static_fallback"

    # Circuit should have opened
    assert breaker_p.transition_log, "Circuit should have transition log entries"
    open_transitions = [t for t in breaker_p.transition_log if t["to"] == "open"]
    assert len(open_transitions) > 0, "Primary circuit should have opened"


def test_gateway_static_fallback_when_all_fail() -> None:
    """Verify static fallback when ALL providers fail."""
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.006)
    breaker_p = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=60)
    breaker_b = CircuitBreaker("backup", failure_threshold=1, reset_timeout_seconds=60)
    gateway = ReliabilityGateway(
        [primary, backup],
        {"primary": breaker_p, "backup": breaker_b},
    )
    # After circuits open, should get static fallback
    for _ in range(5):
        result = gateway.complete("test query")
    assert result.route == "static_fallback"
    assert "temporarily degraded" in result.text.lower() or "try again" in result.text.lower()
