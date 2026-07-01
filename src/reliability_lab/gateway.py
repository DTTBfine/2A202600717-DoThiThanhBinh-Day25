from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab import logger
from reliability_lab.cache import ResponseCache, SharedRedisCache
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
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Cache → circuit-breaker-guarded provider chain → static fallback."""
        start = time.perf_counter()

        # 1. Cache check
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                elapsed_ms = (time.perf_counter() - start) * 1000
                route = f"cache_hit:{score:.2f}"
                logger.emit(
                    "gateway.complete",
                    route=route,
                    provider=None,
                    cache_hit=True,
                    latency_ms=round(elapsed_ms, 2),
                    prompt=prompt[:60],
                )
                return GatewayResponse(
                    text=cached_text,
                    route=route,
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        # 2. Provider fallback chain
        last_error: str | None = None
        for i, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                route = "primary" if i == 0 else "fallback"
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.emit(
                    "gateway.complete",
                    route=route,
                    provider=response.provider,
                    cache_hit=False,
                    latency_ms=round(response.latency_ms, 2),
                    cost=round(response.estimated_cost, 6),
                    prompt=prompt[:60],
                )
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=response.provider,
                    cache_hit=False,
                    latency_ms=response.latency_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                logger.emit(
                    "gateway.provider_error",
                    provider=provider.name,
                    error=last_error,
                    prompt=prompt[:60],
                )

        # 3. Static fallback — all providers failed
        logger.emit("gateway.static_fallback", last_error=last_error, prompt=prompt[:60])
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error,
        )
