# External API performance and resilience

FrontDesk separates two questions that cannot be measured reliably in one test:

1. **FrontDesk capacity** is tested against a local mock. No SNS post, payment,
   email, API charge, or real customer record is created.
2. **End-to-end connectivity** is tested at low volume against a provider's
   documented sandbox or test account, subject to that provider's policy.

## Runtime safeguards

- Transient `408`, `425`, `429`, `500`, `502`, `503`, and `504` responses use a
  bounded exponential backoff with positive jitter.
- `Retry-After` is treated as the minimum requested delay, capped by the local
  maximum delay.
- Automatic retries are allowed only for reads, explicitly safe searches, or
  writes that preserve the same idempotency key.
- Repeated transient failures open a per-client circuit. Calls fail quickly
  during the cooldown instead of consuming all worker and connection capacity.
- HTTP timeouts remain bounded. A provider response is limited to 2 MB.

These controls do not make an unsafe social post or SMTP submission idempotent.
Those sends continue to fail visibly after one uncertain attempt so an operator
can decide whether a retry would create a duplicate.

## Local simulation

```powershell
python external_api_load_test.py --requests 100 --workers 10 --provider-capacity 10
```

The JSON report includes accepted/completed requests, queue rejection, provider
call count, transient failures, duplicate effects, maximum provider concurrency,
throughput, p95 request latency, and p95 queue delay.

To demonstrate backpressure, use a queue smaller than the burst:

```powershell
python external_api_load_test.py --requests 500 --workers 10 --queue-capacity 100
```

Queue rejection in that scenario is intentional and must be surfaced to the
operator; silently accepting work that cannot be retained would lose messages.

## Controlled sandbox validation

Before any live or sandbox run, record the provider, test account, published rate
limit, permitted test volume, expected cost, and whether the action has a side
effect. Start with one request and remain below the documented threshold. Never
run the capacity command against a real provider URL.

For payment, delivery, identity, SNS, and email providers, verify separately:

- timeout and authentication handling;
- one low-volume successful request;
- a documented or sandbox-generated `429` response where permitted;
- preservation of an idempotency key across an operator-approved retry;
- recovery after the provider becomes healthy;
- audit evidence with credentials and personal data redacted.
