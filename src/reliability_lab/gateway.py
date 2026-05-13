from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache, _is_uncacheable
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers.

    Request flow:
    1. Check cache — return immediately on HIT (0ms latency, $0 cost).
    2. Try each provider in order (primary → backup → ...).
    3. For each provider, check circuit breaker — skip if OPEN (fail fast).
    4. On provider success, cache the response and return.
    5. If ALL providers fail, return static fallback message.

    Route reasons are specific and include provider name:
    - "cache_hit:0.95" — served from cache with similarity score
    - "primary:gpt4" — served by primary provider
    - "fallback:backup" — served by fallback provider
    - "static_fallback" — all providers failed
    """

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cumulative_cost: float = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Includes full timing (routing overhead + provider latency) and
        specific route reasons with provider names.
        """
        start = time.perf_counter()

        # --- Cache check ---
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                elapsed = (time.perf_counter() - start) * 1000
                return GatewayResponse(
                    cached, f"cache_hit:{score:.2f}", None, True, elapsed, 0.0
                )

        # --- Provider chain with circuit breakers ---
        last_error: str | None = None
        for idx, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)

                # Cache the successful response (skip privacy-sensitive)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})

                # Track cumulative cost
                self.cumulative_cost += response.estimated_cost

                # Determine route with provider name
                role = "primary" if idx == 0 else "fallback"
                elapsed = (time.perf_counter() - start) * 1000

                return GatewayResponse(
                    text=response.text,
                    route=f"{role}:{provider.name}",
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=elapsed,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        # --- Static fallback — all providers failed ---
        elapsed = (time.perf_counter() - start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=elapsed,
            estimated_cost=0.0,
            error=last_error,
        )
