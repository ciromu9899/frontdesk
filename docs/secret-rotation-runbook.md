# Secret rotation runbook

This runbook applies to deployment-owned Slack, Meta, PayPal and application
credentials. FrontDesk does not copy secret values into reports, logs or release
packages. Rotation must be performed by the credential owner in the provider's
administration console.

## Required sequence

1. Record the credential name, owning tenant, provider and rotation ticket. Do
   not record the secret value.
2. Create a replacement with the minimum documented permissions. Keep the old
   credential active during the overlap only when the provider supports it.
3. Put the replacement into the deployment's approved secret store. Do not place
   it in a tracked `.env` file, command history or support message.
4. Restart the affected FrontDesk process and execute its health, authentication,
   signed-webhook, reply and restart-deduplication checks.
5. Revoke the old credential at the provider. Confirm that authentication with
   the old credential fails and the new one still passes.
6. Record operator, timestamp, affected environment, test evidence and next due
   date. Immediately investigate any unexpected use of the revoked credential.

## Provider checklist

| Credential | Minimum post-rotation proof |
| --- | --- |
| Slack bot token and signing secret | `auth.test`, signed inbound event, one threaded reply, replay rejected after restart |
| Meta page token, app secret and verify token | Graph identity, challenge, signed inbound message, one reply, replay rejected after restart |
| Shellie PayPal webhook and API credentials | verified webhook, fixed-plan payment in sandbox, one entitlement, refund/dispute state transition |
| FrontDesk token-signing and admin secrets | old token rejected, new token accepted, tenant and role boundaries unchanged |

## Emergency rotation

On suspected disclosure, disable the affected integration if immediate
replacement is unavailable, preserve audit evidence, notify the incident owner,
rotate every credential derived from or stored beside the exposed value, and
follow the incident terms in `docs/legal/incident-privacy-contacts.md`.

This repository intentionally cannot perform a real rotation without an
authorised credential owner and provider access. A runbook-only result is not
rotation evidence.
