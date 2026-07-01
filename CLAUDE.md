# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a Python lab project ("Day 10 — Reliability Engineering for Production Agents") that teaches building a production-style reliability layer for an LLM agent gateway. The codebase is a **skeleton with intentional TODOs**: students implement circuit breaking, semantic caching, gateway routing, chaos testing, and Redis-backed shared cache. No real LLM API keys are needed — `FakeLLMProvider` simulates everything.

## Commands

```bash
# Install (use conda ai-lab env or a venv)
pip install -e ".[dev]"

# Run all tests
make test                          # pytest -q

# Run a single test file
pytest tests/test_circuit_breaker.py -v

# Run a single test by name
pytest tests/test_cache.py::test_exact_match -v

# Lint / type-check
make lint       # ruff check src tests scripts
make typecheck  # mypy src (strict mode)

# Start Redis for shared cache tests
make docker-up   # docker compose up -d
make docker-down

# Run chaos simulation (writes reports/metrics.json)
make run-chaos

# Generate final report from metrics
make report

# Clean generated artifacts
make clean
```

## Architecture

```
ReliabilityGateway.complete(prompt)
  │
  ├─ ResponseCache / SharedRedisCache   ← check first (semantic similarity)
  │
  ├─ [for each provider in order]
  │    CircuitBreaker.call(provider.complete, prompt)
  │        └─ FakeLLMProvider.complete(prompt)  → ProviderResponse
  │
  └─ static fallback string              ← all providers failed
```

### Source modules (`src/reliability_lab/`)

| File | Role | Status |
|---|---|---|
| `circuit_breaker.py` | 3-state machine (CLOSED/OPEN/HALF_OPEN) | **TODO: 4 methods** |
| `cache.py` | In-memory semantic cache + Redis shared cache | **TODO: 5 methods** |
| `gateway.py` | Routes: cache → breakers → fallback chain | **TODO: 1 method** |
| `chaos.py` | Runs named failure scenarios, collects metrics | **TODO: 2 methods** |
| `metrics.py` | Aggregates P50/P95/P99, availability, cost; CSV export | **TODO: write_csv()** |
| `providers.py` | `FakeLLMProvider` — simulates latency/failures — **do not modify** | done |
| `config.py` | Pydantic config loader from YAML — **do not modify** | done |

### Key design constraints

- `record_failure()` in `CircuitBreaker` must use `if/elif`, not `or`: HALF_OPEN probe failure and threshold breach have **different reasons** logged to `transition_log`.
- `ResponseCache.similarity()` must use **character n-gram cosine** (not Jaccard/token overlap). Tokenize as words + char trigrams, build `Counter` vectors, compute `dot / (|a| × |b|)`.
- `ResponseCache.get()` needs `self.false_hit_log` list in `__init__` — add it there.
- `SharedRedisCache` reuses `ResponseCache.similarity()` statically for scan-based lookup.
- `gateway.complete()` must check `self.cache` is not None before calling cache methods; route label is `"primary"` for index 0 provider, `"fallback"` for others.

### Config (`configs/default.yaml`)

All thresholds and chaos scenarios live here. Key settings:
- `circuit_breaker.failure_threshold: 3`, `reset_timeout_seconds: 2`, `success_threshold: 1`
- `cache.similarity_threshold: 0.92`, `ttl_seconds: 300`, `backend: memory|redis`
- `scenarios:` — named chaos runs with `provider_overrides` (fail_rate per provider)

### Tests

```
tests/test_circuit_breaker.py    — 11 tests (start here)
tests/test_cache.py              — 9 tests
tests/test_gateway_contract.py   — 4 tests
tests/test_todo_requirements.py  — 7 xfail markers (become xpass when all TODOs done)
tests/test_redis_cache.py        — 6 tests (skipped unless Redis running via docker-up)
tests/test_config.py             — 2 tests (already passing)
tests/test_metrics.py            — 2 tests (already passing)
```

Recommended implementation order: circuit breaker → cache → gateway → chaos/metrics → Redis.
