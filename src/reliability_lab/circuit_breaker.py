from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Production-safe circuit breaker state machine.

    State transitions:
    - CLOSED: calls pass through; count failures. When failure_count >= failure_threshold → OPEN.
    - OPEN: fail fast (raise CircuitOpenError) until reset_timeout_seconds elapses → HALF_OPEN.
    - HALF_OPEN: allow one probe request. On success → CLOSED. On failure → re-OPEN immediately.

    All transitions are recorded in transition_log for observability and recovery time calculation.
    """

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
        """Return whether a request should be attempted.

        Returns False when OPEN and timeout has not elapsed.
        When timeout elapses, transitions to HALF_OPEN and allows one probe.
        """
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and time.monotonic() - self.opened_at >= self.reset_timeout_seconds:
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                return True
            return False
        return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker."""
        if not self.allow_request():
            raise CircuitOpenError(f"circuit {self.name} is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Record success and close from HALF_OPEN if enough probes pass.

        Resets failure counter. In HALF_OPEN, transitions to CLOSED when
        success_count reaches success_threshold.
        """
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0

    def record_failure(self) -> None:
        """Record failure and open when threshold is reached.

        In HALF_OPEN: immediately re-open (probe failed).
        In CLOSED: open when failure_count reaches failure_threshold.
        Resets success counter on any failure.
        """
        self.failure_count += 1
        self.success_count = 0
        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "half_open_probe_failure")
            self.opened_at = time.monotonic()
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self.opened_at = time.monotonic()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
