from __future__ import annotations

import io
import urllib.error
from unittest import TestCase

import external_api_load_test
import resilience


def http_error(code: int, retry_after: str = "0") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://provider.example/api", code, "simulated", {"Retry-After": retry_after},
        io.BytesIO(b"{}"))


class RetryPolicyTests(TestCase):
    def test_retry_after_and_idempotent_retry_are_bounded(self) -> None:
        effects = [http_error(429, "2"), {"ok": True}]
        delays: list[float] = []

        def operation():
            result = effects.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        result = resilience.execute(
            operation, retry_safe=True,
            policy=resilience.RetryPolicy(max_retries=2, base_delay=.1, max_delay=5),
            sleeper=delays.append, random_value=lambda: 0)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(delays, [2.0])

    def test_non_idempotent_operation_is_never_retried(self) -> None:
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise http_error(503)

        with self.assertRaises(urllib.error.HTTPError):
            resilience.execute(
                operation, retry_safe=False,
                policy=resilience.RetryPolicy(max_retries=5), sleeper=lambda _delay: None)
        self.assertEqual(calls, 1)

    def test_circuit_opens_and_recovers_after_cooldown(self) -> None:
        now = [100.0]
        breaker = resilience.CircuitBreaker(2, 10, clock=lambda: now[0])
        policy = resilience.RetryPolicy(max_retries=0)
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError):
                resilience.execute(lambda: (_ for _ in ()).throw(http_error(503)),
                                   retry_safe=True, policy=policy, breaker=breaker)
        self.assertEqual(breaker.state, "open")
        with self.assertRaises(resilience.CircuitOpenError):
            resilience.execute(lambda: {"ok": True}, retry_safe=True,
                               policy=policy, breaker=breaker)
        now[0] += 11
        self.assertEqual(resilience.execute(lambda: {"ok": True}, retry_safe=True,
                                            policy=policy, breaker=breaker), {"ok": True})
        self.assertEqual(breaker.state, "closed")


class LocalExternalApiSimulationTests(TestCase):
    def test_mock_load_exercises_retry_without_duplicate_effects(self) -> None:
        report = external_api_load_test.run(external_api_load_test.Scenario(
            requests=20, workers=4, queue_capacity=20, provider_capacity=4,
            latency_ms=1, fail_first_every=5))
        self.assertEqual(report["mode"], "local-mock-no-external-side-effects")
        self.assertEqual(report["completed"], 20)
        self.assertEqual(report["failures"], 0)
        self.assertGreater(report["provider_calls"], 20)
        self.assertGreater(report["transient_failures"], 0)
        self.assertEqual(report["duplicate_effects"], 0)
