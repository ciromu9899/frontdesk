# FrontDesk Customer Guide

FrontDesk is a customer-support chatbot for an accessible web chat and purchaser-
controlled web, Slack, Meta, WhatsApp, and email channels. It does not create, capture, change, or refund
payments. ShellieSoftwareTools' external product checkout is not part of this
application.

## Responsibility boundary

The purchaser deploys FrontDesk, creates the channel applications, supplies the
channel credentials at runtime, chooses the model provider, and controls the
conversation database and knowledge documents. The distributed package contains
no purchaser credentials and no Shellie merchant credentials.

Ollama keeps model inference on the purchaser's machine. Slack, Meta, and any
optional cloud provider still process data under their own terms. Configure
notices, retention, access controls, and deletion procedures for the purchaser's
actual use case. Do not invite regulated or sensitive data unless that deployment
has the required contract, controls, and review.

## Install and local model

Use Python 3.11 or newer and install the locked dependencies. Start Ollama and
make the bundled default model available:

```text
ollama pull qwen3:8b
python chat.py --provider ollama --persona ecommerce
```

FrontDesk never silently switches from Ollama to a cloud model. Select any cloud
provider explicitly and review its data terms before use.

Run the pre-flight check without printing secret values:

```text
python chat.py --doctor
```

## Customer web chat

```text
python webchat.py --port 8766
```

Open `http://127.0.0.1:8766/` for development. Use `?lang=es` for US Spanish.
Production must place the service behind HTTPS. The UI uses no third-party assets,
supports keyboard operation and live-region announcements, and acknowledges Global
Privacy Control.

Set `FRONTDESK_WEB_PERSONA` to an installed persona name such as `salon`,
`hospitality`, or `ecommerce`. An invalid value safely falls back to ecommerce.

## Slack, Meta, WhatsApp, and email

Create dedicated applications in the purchaser's own Slack workspace and Meta
business account. Grant only the scopes needed to receive and reply to support
messages. Supply credentials through the purchaser's runtime secret manager; do
not add them to the package or documentation.

Point both applications at the purchaser's HTTPS webhook endpoint and run:

```text
python webhooks.py
python verify_channels_live.py
```

FrontDesk verifies platform signatures before parsing messages and stores delivery
IDs in durable SQLite state so a retry is not processed twice after a restart.
Passing local tests is not evidence that a real workspace is connected; retain the
live verification output for the purchaser's deployment.

Email supports two purchaser-controlled deployment patterns:

- a signed inbound relay handled by `webhooks.py`, with SMTP profiles in the
  tenant integrations file; or
- direct IMAP polling and SMTP replies from the purchaser's shared mailbox:

```text
python email_mailbox.py --once --dry-run
python email_mailbox.py --interval 60 --base-url https://support.example.com
```

The mailbox runner rejects automatic replies, list traffic, bounces, no-reply
senders, and messages from its own address. It keeps replies in the original email
thread, stores successful delivery IDs in SQLite, and leaves model or SMTP failures
unacknowledged for the next poll. Configure all mailbox credentials only in the
purchaser's runtime secret manager. The public URL enables a 14-day signed CSAT
link; set a separate 32-character `FRONTDESK_FEEDBACK_SECRET`.

## Tenant isolation

Every access token, channel identity, session, knowledge index, connector request,
handoff and demo record carries a tenant ID. Give each customer a distinct tenant
and distinct connector credentials. Never reuse a shared administrator token or
knowledge directory across unrelated customers.

## Knowledge documents

The local retrieval pipeline accepts PDF, DOCX, PPTX, XLSX, Markdown, text, and
HTML. Build the tenant's index after adding approved material:

```text
python rag.py --build --tenant default
```

Retrieved text is treated as data, not as an instruction. Answers must cite the
returned source and state when the approved knowledge base does not contain an
answer.

## Optional step-up LinkedIn sign-in

LinkedIn OpenID Connect sign-in is optional for public knowledge chat and required
only before a deployment exposes account-specific tools through that identity path.
It lets a public sender prove an account identity; it is not an inbound LinkedIn
DM connector and does not grant administrator permissions. Create a LinkedIn
developer application owned by the purchaser, add the **Sign in with LinkedIn
using OpenID Connect** product, and configure an exact HTTPS callback URI.

Set all four runtime values before enabling LinkedIn sign-in:

```text
FRONTDESK_LINKEDIN_CLIENT_ID
FRONTDESK_LINKEDIN_CLIENT_SECRET
FRONTDESK_LINKEDIN_REDIRECT_URI=https://support.example.com/linkedin/callback
FRONTDESK_LINKEDIN_STATE_SECRET=<at least 32 random characters>
```

Run `python linkedin_verify.py` behind the same HTTPS reverse proxy. Guest chat and
other configured channels start independently. Store the client secret and state
secret only in the purchaser's runtime secret manager.

## Remote approval

Remote approval is optional for purchaser-defined non-financial changes such as a
reservation cancellation. Each action requires a separate approval. If remote
approval is unavailable or times out, FrontDesk refuses the action.

## Operations and evidence

Before production use, run the unit tests, a real-Ollama load test, backup/restore
exercise, automated accessibility check, and live Slack/Meta verification. A
disabled-user accessibility study, deployment-specific legal review, production
recovery exercise, secret rotation, and signed installer remain purchaser or
seller release gates; automated checks do not replace them.

See `docs/data-responsibility.md`, `docs/compliance-readiness.md`, and
`docs/legal/README.md` for the responsibility matrix and current draft documents.
