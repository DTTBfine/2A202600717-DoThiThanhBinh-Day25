from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

from reliability_lab import logger

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """3-state circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.HALF_OPEN:
            return True
        # OPEN — check if timeout elapsed
        if self.opened_at is not None:
            elapsed = time.monotonic() - self.opened_at
            if elapsed >= self.reset_timeout_seconds:
                self._transition(CircuitState.HALF_OPEN, "timeout_elapsed")
                logger.emit("breaker.probe", name=self.name, state=self.state.value)
                return True
        logger.emit("breaker.denied", name=self.name, state=self.state.value)
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        if not self.allow_request():
            logger.emit("breaker.circuit_open", name=self.name)
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN — request rejected")
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def record_success(self) -> None:
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0
            logger.emit("breaker.closed", name=self.name, reason="probe_success")

    def record_failure(self) -> None:
        self.failure_count += 1
        self.success_count = 0
        # HALF_OPEN probe failure — re-open immediately with distinct reason
        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
            logger.emit("breaker.opened", name=self.name, reason="probe_failure", failure_count=self.failure_count)
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self.opened_at = time.monotonic()
            logger.emit("breaker.opened", name=self.name, reason="failure_threshold_reached", failure_count=self.failure_count)

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        logger.emit(
            "breaker.transition",
            name=self.name,
            from_state=self.state.value,
            to_state=new_state.value,
            reason=reason,
        )
        self.state = new_state
