"""Safe local capacity probe for a third-party API integration.

The provider is simulated in-process: no SNS post, payment, email, API charge,
or external account is touched. The scenario exercises queue pressure, latency,
transient 429/503 responses, retry bounds, and idempotency reuse.
"""

from __future__ import annotations

import argparse
import io
import json
import queue
import statistics
import threading
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import resilience


@dataclass(frozen=True)
class Scenario:
    requests: int = 100
    workers: int = 10
    queue_capacity: int = 100
    provider_capacity: int = 10
    latency_ms: float = 20.0
    fail_first_every: int = 10

    def validate(self) -> None:
        if not 1 <= self.requests <= 100_000:
            raise ValueError("requests must be between 1 and 100000")
        if not 1 <= self.workers <= 500:
            raise ValueError("workers must be between 1 and 500")
        if not 1 <= self.queue_capacity <= 100_000:
            raise ValueError("queue_capacity must be between 1 and 100000")
        if not 1 <= self.provider_capacity <= 500:
            raise ValueError("provider_capacity must be between 1 and 500")
        if not 0 <= self.latency_ms <= 60_000:
            raise ValueError("latency_ms must be between 0 and 60000")
        if not 0 <= self.fail_first_every <= 100_000:
            raise ValueError("fail_first_every must be between 0 and 100000")


class SimulatedProvider:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.calls = 0
        self.transient_failures = 0
        self._attempts: dict[str, int] = {}
        self._completed: set[str] = set()
        self.duplicate_effects = 0

    @staticmethod
    def _http_error(code: int, retry_after: str = "0") -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://mock.invalid/api", code, "simulated", {"Retry-After": retry_after},
            io.BytesIO(b'{"error":"simulated"}'))

    def call(self, request_id: str) -> dict:
        with self._lock:
            self.calls += 1
            attempt = self._attempts.get(request_id, 0) + 1
            self._attempts[request_id] = attempt
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            overloaded = self._active > self.scenario.provider_capacity
        try:
            if self.scenario.latency_ms:
                time.sleep(self.scenario.latency_ms / 1000)
            numeric_id = int(request_id.rsplit("-", 1)[-1])
            scheduled = (self.scenario.fail_first_every > 0 and attempt == 1 and
                         numeric_id % self.scenario.fail_first_every == 0)
            if overloaded or scheduled:
                with self._lock:
                    self.transient_failures += 1
                code = 429 if overloaded else 503
                raise self._http_error(code, "0.01" if code == 429 else "0")
            with self._lock:
                if request_id in self._completed:
                    self.duplicate_effects += 1
                self._completed.add(request_id)
            return {"ok": True, "request_id": request_id}
        finally:
            with self._lock:
                self._active -= 1


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))]


def run(scenario: Scenario | None = None) -> dict:
    selected = scenario or Scenario()
    selected.validate()
    provider = SimulatedProvider(selected)
    work: queue.Queue[tuple[int, float] | None] = queue.Queue(selected.queue_capacity)
    lock = threading.Lock()
    latencies: list[float] = []
    queue_delays: list[float] = []
    errors: list[str] = []
    rejected = 0
    breaker = resilience.CircuitBreaker(failure_threshold=20, recovery_timeout=1)
    policy = resilience.RetryPolicy(max_retries=2, base_delay=0.005, max_delay=0.05)

    def worker() -> None:
        while True:
            item = work.get()
            if item is None:
                work.task_done()
                return
            index, enqueued = item
            started = time.perf_counter()
            try:
                resilience.execute(
                    lambda: provider.call(f"load-{index}"), retry_safe=True,
                    policy=policy, breaker=breaker)
            except Exception as exc:
                with lock:
                    errors.append(type(exc).__name__)
            else:
                with lock:
                    latencies.append(time.perf_counter() - started)
                    queue_delays.append(started - enqueued)
            finally:
                work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(selected.workers)]
    for thread in threads:
        thread.start()
    overall = time.perf_counter()
    for index in range(selected.requests):
        try:
            work.put_nowait((index, time.perf_counter()))
        except queue.Full:
            rejected += 1
    work.join()
    for _ in threads:
        work.put(None)
    work.join()
    elapsed = time.perf_counter() - overall
    return {
        "mode": "local-mock-no-external-side-effects",
        "requests": selected.requests,
        "accepted": selected.requests - rejected,
        "completed": len(latencies),
        "failures": len(errors),
        "queue_rejected": rejected,
        "provider_calls": provider.calls,
        "transient_failures": provider.transient_failures,
        "duplicate_effects": provider.duplicate_effects,
        "max_provider_inflight": provider.max_active,
        "circuit_state": breaker.state,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_rps": round(len(latencies) / elapsed, 2) if elapsed else None,
        "latency_average_ms": round(statistics.mean(latencies) * 1000, 2) if latencies else None,
        "latency_p95_ms": round((_percentile(latencies, .95) or 0) * 1000, 2)
        if latencies else None,
        "queue_delay_p95_ms": round((_percentile(queue_delays, .95) or 0) * 1000, 2)
        if queue_delays else None,
        "errors": errors[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the safe external API load simulation.")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--queue-capacity", type=int, default=100)
    parser.add_argument("--provider-capacity", type=int, default=10)
    parser.add_argument("--latency-ms", type=float, default=20)
    parser.add_argument("--fail-first-every", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(Scenario(
        requests=args.requests, workers=args.workers, queue_capacity=args.queue_capacity,
        provider_capacity=args.provider_capacity, latency_ms=args.latency_ms,
        fail_first_every=args.fail_first_every))
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["failures"] == 0 and report["duplicate_effects"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
