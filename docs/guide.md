# Frontdesk operations guide

A set of procedures, indexed by what you are trying to do. For lists of options
and tools, see [reference.md](reference.md).

- [1. Setting it up properly](#1-setting-it-up-properly)
- [2. Connecting PayPal](#2-connecting-paypal)
- [3. Connecting real systems](#3-connecting-real-systems)
- [4. Answering from your own documents](#4-answering-from-your-own-documents)
- [5. Fitting it to your industry](#5-fitting-it-to-your-industry)
- [6. Adding a tool](#6-adding-a-tool)
- [7. Reading the audit log](#7-reading-the-audit-log)
- [8. Things that commonly go wrong](#8-things-that-commonly-go-wrong)
- [9. Connecting Slack, Teams and Meta](#9-connecting-slack-teams-and-meta)
- [10. Letting people prove who they are, with LinkedIn](#10-letting-people-prove-who-they-are-with-linkedin)
- [11. Approving from a phone](#11-approving-from-a-phone)
- [12. Choosing the market: US or UK](#12-choosing-the-market-us-or-uk)

---

## 1. Setting it up properly

`FRONTDESK_AUTH_MODE=disabled` is for development. In production, authentication
is on.

**Three steps.** After each one, `python chat.py --doctor` will tell you how far
you have got and what is left.

### 1-1. Create a signing secret

```bash
python auth.py --new-secret
```

Put what it prints in `FRONTDESK_AUTH_SECRET`. It must be at least 32
characters. This is the key that signs tokens: keep it out of the repository and
treat it as a secret.

### 1-2. Issue an access token

```bash
python auth.py --subject operator@example.com --roles operator --hours 8
```

Put what it prints in `FRONTDESK_ACCESS_TOKEN`. For the roles, see the
[permissions table](reference.md#roles-and-permissions). Grant the least that
works: somebody who does not handle payments does not get `finance`.

### 1-3. Check it

```bash
python chat.py --doctor
```

When both Authentication lines read `[ok]`, you are done.

> **If you put the secret and token in `.env`**, note that `.env` is gitignored
> and excluded from anything distributed. **Never put real values in
> `.env.example`** — that file *is* distributed.

---

## 2. Connecting PayPal

### Keep the two PayPal uses separate

There are two legally and operationally different payment flows:

1. **Shellie Software Tools sells a Frontdesk licence.** Shellie's own merchant
   account and sales checkout collect that payment. They belong outside the
   customer's Frontdesk installation. Shellie keeps the buyer and transaction
   records needed for sale, tax, fraud, support and accounting purposes.
2. **A customer uses Frontdesk in its own business.** This section configures
   that customer's own merchant account. The customer owns the credentials,
   determines the purpose, and is responsible for the transaction and notices.

Never reuse Shellie's merchant credentials in a customer's deployment, reuse a
customer's credentials for licence sales, or combine the two flows' order logs.
PayPal receives payment data under its own terms; the merchant remains
responsible for the buyer and transaction data it receives from PayPal.

Shellie's current hosted sales page is:

```text
https://www.paypal.com/ncp/payment/WZMRKU9FRKW4S
```

It was verified on 2026-08-22 to display `FrontDesk`, `Chatbots for various
industries`, and `$2,999.00 USD` for quantity one. Recheck the visible item,
amount, currency and seller account after every PayPal-link change. Do not use
this link to test a customer's operational payment flow.

The distributed source currently carries Apache License 2.0. Receiving payment
does not remove the recipient's rights under that licence. Do not describe the
payment as purchasing an exclusive or non-redistributable proprietary licence
unless the licensing model and distributed licence have first been changed and
reviewed.

**The bot never touches card details.** The buyer approves on PayPal's own
hosted page, and all Frontdesk ever holds is an order ID.

### 2-1. Get credentials

1. Sign in at https://developer.paypal.com
2. Apps & Credentials → the **Sandbox** tab → Create App
3. Put the Client ID and Secret in **`.env`**

```
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
PAYPAL_ENV=sandbox
```

### 2-2. Check that it is really reachable

```bash
python verify_paypal.py
```

It gets a token, creates an order and reads it back, against the real API. No
money moves.

**Passing stub tests proves nothing about the real API.** Run this before going
anywhere near production.

### 2-3. Check the approval step too (optional)

Take the approval URL `verify_paypal.py` prints and open it in a browser as your
sandbox **Personal** account — find it under Testing Tools → Sandbox Accounts.
Approve it, then:

```bash
python verify_paypal.py --order <order id>
```

If it now reads `APPROVED`, approval works end to end.

### 2-4. Use the SDK v6 approval page (optional)

Instead of handing back PayPal's approval link directly, you can use the PayPal
JavaScript SDK v6 page that Frontdesk serves. If the buyer's browser already
holds a PayPal session, PayPal may decide to skip the password prompt. Frontdesk
does not log anyone in, and stores no password, one-time code or PayPal cookie.

Two settings. `FRONTDESK_CHECKOUT_SECRET` is a dedicated random value of at
least 32 characters; `FRONTDESK_CHECKOUT_BASE_URL` is the HTTPS URL your reverse
proxy exposes.

```
FRONTDESK_CHECKOUT_BASE_URL=https://checkout.example.com
FRONTDESK_CHECKOUT_SECRET=<a dedicated random value, 32+ characters>
```

Start the checkout server:

```bash
python paypal_checkout.py --port 8780
```

In production, do not expose `127.0.0.1:8780` directly. Put an HTTPS reverse
proxy in front of it on the same origin as `FRONTDESK_CHECKOUT_BASE_URL`. Once
configured, the `approval_url` that `create_paypal_order` returns is a signed
SDK v6 page valid for one hour. The signing token sits in the URL fragment, so
it reaches neither HTTP logs nor the Referer sent to PayPal.

On that page, the moment the buyer approves on PayPal the server re-checks the
order's ID, status, amount and currency, then captures. It will not capture an
order still in `CREATED`, nor one whose amount disagrees with the signed link.
Opening the same link twice returns the existing capture rather than charging
again. Leave those two variables unset and you get PayPal's own approval URL and
a manual capture behind the confirmation gate, exactly as before.

### The shape of a payment

| Step | Tool | Money |
| --- | --- | --- |
| 1. Create the order | `create_paypal_order` | does not move |
| 2. Buyer approves on PayPal | (outside the bot) | does not move |
| 3. Confirm approval | `get_paypal_order_status` | does not move |
| 4. Settle | `capture_paypal_order` | **moves** — gate required |
| Refund | `refund_paypal_capture` | **moves** — gate required |

Capture and refund carry idempotency keys, so a retry cannot charge or refund
twice. `paypal.py` also holds a $10,000 ceiling per transaction.

> It only goes near production when `PAYPAL_ENV=live` is set. The default is
> sandbox, and `verify_paypal.py` refuses to run against live unless told to.

---

## 3. Connecting real systems

Unset, it runs on the demo data in `data/store.json`. Two environment variables
switch it to a real API.

```
FRONTDESK_BACKEND_URL=https://api.example.com/frontdesk/v1
FRONTDESK_BACKEND_TOKEN=<bearer token>
```

The connector layer insists on: HTTPS, bearer authentication, a 10 second
timeout, a 2MB response ceiling, JSON responses, and an `Idempotency-Key` on
anything that writes.

The API contract it expects:

| Method | Path | For |
| --- | --- | --- |
| `GET` | `/orders/{order_id}` | Look up an order |
| `GET` | `/reservations?customer=...` | Search reservations |
| `PATCH` | `/reservations/{reservation_id}` | Move one |
| `DELETE` | `/reservations/{reservation_id}` | Cancel one |

**If your paths or JSON differ**, adjust the four methods in `connectors.py`.
Nothing else needs to change.

---

## 4. Answering from your own documents

Put approved documents in `knowledge/` and build the index.

```bash
python rag.py --build
```

`.md`, `.txt` and `.html` are supported. PDF and Office documents are not, today.

Answers carry a `filename#chunk-N` citation. Search happens locally and
**documents are never sent to an external embedding API**.

The admin screen can rebuild it too — see [section 7](#7-reading-the-audit-log).

> Retrieved documents are treated as untrusted data. Instructions hidden inside
> a document do not override the system prompt, permissions, or the confirmation
> gate.

---

## 5. Fitting it to your industry

```bash
python chat.py --persona fintech
```

The bundled personas, and the boundaries each one holds, are listed in
[reference.md](reference.md#personas).

**To write a new one**, drop a file at `personas/<name>.md`. The filename is
what `--persona` takes.

Write it in English. It becomes the system prompt verbatim, so writing it in
another language makes the reply language wobble. Start from an existing persona,
and spend your effort stating what must **not** happen in that industry.

---

## 6. Adding a tool

Register it in `tools.py`. Swap the `handler` for a real API call and it is
production code.

```python
_register(Tool(
    name="issue_store_credit",
    handler=_issue_store_credit,
    dangerous=True,                                   # puts it behind the gate
    summarize=lambda a, lang: f"Issue ${a['amount']} store credit to {a['customer']}",
    spec={"en": {
        "description": "Issue store credit to a customer. This cannot be undone.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer": {"type": "string", "description": "Customer email"},
                "amount": {"type": "string", "description": "Amount in USD"},
            },
            "required": ["customer", "amount"],
        },
    }},
))
```

**The test for `dangerous=True`** is whether it can be taken back. Anything that
cannot — a payment, a refund, a message sent, a deletion, an external
notification — gets it. Once marked, it will not run where approval cannot be
obtained.

`summarize` is the one line the confirmation dialog shows. Make **the amount and
the target legible in it**. That line is all the approver sees.

Write the description and parameter text in English. They are part of the prompt
the model reads.

---

## 7. Reading the audit log

Authentication results, session starts, tool requests, declined approvals,
successes and failures, dropped history and knowledge reindexing all go to
`data/audit.jsonl`. Anything that looks like a secret is redacted.

```bash
python auth.py --subject admin@example.com --roles admin --hours 8
python admin.py --port 8765
```

Open `http://127.0.0.1:8765` and sign in with the token you just issued. It
listens on localhost only. It shows the audit chain, recent events, the RAG
index, and whether real backends are configured — never the secret values
themselves.

When the log passes `FRONTDESK_AUDIT_MAX_BYTES` (5MB by default) it is moved
aside as `audit-<timestamp>.jsonl` and a new file starts. **The first event
after the switch carries the previous file's final hash, so the chain runs
unbroken across files.** Verification walks every segment.

---

## 8. Things that commonly go wrong

**`Authentication failed: An access token is required.`**
→ Work through [section 1](#1-setting-it-up-properly). While developing, you can
set `FRONTDESK_AUTH_MODE=disabled` instead.

**`can't open file '...verify_paypal.py'`**
→ You are not in the project directory. `cd frontdesk` first.

**PayPal keeps returning the same order ID**
→ The idempotency key is doing its job — that is the double-charge guard. Change
`--amount` to create a genuinely new order.

**The bot replies in the wrong language, or drifts**
→ Something other than English got into a persona or a tool definition. Both are
part of the prompt the model reads. Keep them English.

**A long conversation starts failing**
→ Once the history reaches the ceiling (200,000 characters by default), the
oldest turns are dropped automatically, and you will see it happen on screen.
Change the ceiling with `--max-history-chars`.

**A write tool never runs**
→ Where approval cannot be obtained — piped input, one-shot mode — it is refused
deliberately. Run interactively. For testing only, there is `--auto-approve`,
which must not be used in production.

**Real values ended up in `.env.example`**
→ That file is distributed, so treat those credentials as leaked: revoke them
and issue new ones. Real values belong in `.env`.

---

## 9. Connecting Slack, Teams and Meta

### Decide this first: who approves a refund demanded over DM?

In the CLI, a human at the terminal presses the key. There is no terminal on the
other end of an Instagram DM. Connect a social channel without answering this
question and **money moves on the say-so of whoever claims to be someone**.

Frontdesk answers it with what the channel was able to verify. No new mechanism:
the existing permission system closes it.

| Trust tier | What it means | Roles granted | What it can reach |
| --- | --- | --- | --- |
| `public` | A handle is not an identity | `guest`, fixed | Public information only |
| `workspace` | Verified as a member of the organisation | `support` / `operator` | Lookups and reservation changes |
| `authenticated` | Verified as the customer themselves | `support` / `operator` | Their own records |

Slack and Teams sit at `workspace`: an inbound message is checkable as coming
from inside your organisation. Instagram and Messenger sit at `public`.
`finance` is in no ceiling at all - moving money stays with an operator holding a
token from `auth.py`.

An Instagram DM is `public`. The sender is not "the customer who placed that
order"; they are "whoever is operating that account". Accounts get shared, and
accounts get taken over.

So **Instagram can neither look up an order nor issue a refund**. The permission
tier refuses, and the reply points at a route where identity can be established:
the account page, or a person. That is not a limitation bolted on — it is an
accurate reflection of what this channel can prove.

Write something like `FRONTDESK_CHANNEL_META_ROLES=finance` and the part above
the ceiling is silently dropped. No misconfiguration can put payment permissions
on a public channel.

### 9-1. Start the receiver

All three platforms deliver by HTTP POST, so something has to be listening.

```bash
python webhooks.py --port 8770
```

| Path | For |
| --- | --- |
| `POST /slack` | Slack Events API |
| `POST /teams` | Microsoft Teams outgoing webhook |
| `POST /meta` | Instagram DM and Messenger |
| `GET /meta` | Meta's subscription challenge |
| `GET /health` | A liveness probe that reveals nothing |

It binds localhost. Put an HTTPS reverse proxy in front of it and register the
public URLs with each platform. It refuses to start if no channel is configured,
because it would be listening for deliveries that can never be verified.

Two things about how it behaves are worth knowing before you debug it.

**A request that fails signature verification is answered 401 and never parsed.**
Not logged as an event, not handed to a channel. If a platform reports delivery
failures, the signing secret is the first thing to check.

**Slack and Meta are acknowledged immediately and answered afterwards.** Slack
allows about three seconds before it calls the delivery failed and retries, and a
retry arriving while the first is still thinking would do the work twice - this
agent cancels reservations. So the reply is sent through the platform's API once
the agent finishes, and every delivery id is checked against the ones already
seen. Teams is the exception: its webhook is synchronous, so the reply is the
HTTP response and the caller waits.

### 9-2. Connecting Slack, for an internal help desk

1. api.slack.com/apps → Create New App
2. Turn on **Event Subscriptions** and set the Request URL to `https://<your host>/slack`
3. Under Subscribe to bot events, add `message.im` and `app_mention`
4. Under **OAuth & Permissions**, grant `chat:write` and install to the workspace
5. Put the values in `.env`

```
FRONTDESK_SLACK_SIGNING_SECRET=...     # shown under Basic Information
FRONTDESK_SLACK_BOT_TOKEN=xoxb-...     # shown under OAuth & Permissions
FRONTDESK_SLACK_TEAM_ID=T01234567      # optional; rejects other workspaces
FRONTDESK_CHANNEL_SLACK_ROLES=support  # support by default; operator is allowed
```

### 9-3. Connecting Microsoft Teams

Frontdesk uses a Teams **outgoing webhook**, not the Bot Framework. Anyone who
can manage a team can add one; there is no Azure registration, no app package and
no review. It authenticates with HMAC-SHA256 over the request body, the same
primitive Slack and Meta use.

The Bot Framework was the alternative, and it authenticates with an RS256 JWT
validated against a rotating key set. Implementing that here would mean writing
signature verification from scratch, which this project does not do.

1. In Teams, open the team, then **Manage team** -> **Apps** -> **Create an
   outgoing webhook**
2. Give it a name - what people will @-mention - and set the callback URL to
   `https://<your host>/teams`
3. Teams shows a **security token once**. Copy it now; it is not shown again
4. Put it in `.env`

```
FRONTDESK_TEAMS_SECURITY_TOKEN=<the token Teams showed>
FRONTDESK_TEAMS_TENANT_ID=<your Microsoft 365 tenant id>   # optional, recommended
```

`FRONTDESK_TEAMS_TENANT_ID` makes the channel drop anything arriving from another
tenant. Then @-mention the webhook in a channel:

```
@Frontdesk where is order A-88001
```

The mention markup is stripped before the model sees the message, so it reads
"where is order A-88001".

**An outgoing webhook is scoped to one team.** Add it to every team that should
be able to reach the bot. It only ever sees messages that @-mention it, which is
usually what you want in a busy channel.

### 9-4. Connecting Instagram DMs and Messenger

1. Create an app at developers.facebook.com and add Messenger or Instagram
2. Connect a Facebook page and issue a page access token
3. Under Webhooks, set the Callback URL to `https://<your host>/meta` and a Verify Token
4. Subscribe to `messages`

```
FRONTDESK_META_APP_SECRET=...
FRONTDESK_META_PAGE_TOKEN=...
FRONTDESK_META_VERIFY_TOKEN=<any string you choose>
```

### 9-5. Checking it

```bash
python chat.py --doctor
```

A Channels line reading `configured - trust=..., roles=...` means the settings
took. Then, with the receiver running:

```bash
curl -s http://127.0.0.1:8770/health
```

An unsigned POST is the other check worth doing by hand - it must come back 401,
not 200.

### About signature verification

Every inbound message has its signature verified before anything else happens.
Loosen that and anyone can forge a "refund me".

- **Slack** — HMAC-SHA256 over `v0:{timestamp}:{body}`. The timestamp is inside
  the signature, so replays of old messages are rejected too; five minutes of
  drift is allowed
- **Teams** — HMAC-SHA256 over the body, in `Authorization: HMAC <base64>`. The
  secret Teams issues is itself base64 and must be decoded before use as the key;
  signing with the printable form is the classic mistake, and there is a test for
  it. No timestamp is covered, which is why the receiver deduplicates by activity
  id
- **Meta** — HMAC-SHA256 in `X-Hub-Signature-256`

Comparison uses `hmac.compare_digest`. Comparing with `==` leaks the signature a
byte at a time through how long the comparison takes.

**With no secret configured, verification always fails.** A missing setting
cannot turn it into a no-op.

### What is deliberately not here

**X, formerly Twitter, is not included.** Two reasons. API access is paid and
the terms keep shifting, and I cannot implement its signature scheme with
confidence — **signature verification done wrong is more dangerous than none at
all**. Beyond that, a public mention is the weakest identity there is, which
makes it the worst possible fit for a bot that touches money.

**LinkedIn is here, but not as an inbox.** It exposes no inbound-DM webhook to
third parties, so there is no message channel to build. What it does offer is
identity, which turns out to be the more useful half - see
[section 10](#10-letting-people-prove-who-they-are-with-linkedin).

### What has not been tested

**No platform has been exercised live.** That needs credentials, a workspace and
a public HTTPS endpoint, none of which I can provide here. What is tested is the
receiver end to end over real HTTP with real signatures: a forged signature is
refused, a valid one is answered, a retry is not answered twice, and Teams gets
its reply in the response body.

Before you put this in front of customers, confirm connectivity with a sandbox
workspace and a test page.

---

## 10. Letting people prove who they are, with LinkedIn

### What this is, and what it is not

**It is not another inbox.** LinkedIn gives third-party applications no webhook
for inbound direct messages; messaging sits behind partner programmes a
self-serve app cannot enter. Nobody can build the Slack-shaped adapter here, and
any project claiming to have done so is either in one of those programmes or
scraping.

**It is the answer to section 9's problem.** On a public channel the sender is a
handle, so they get `guest` and every request about their own order is refused.
That is correct, and it is also where the conversation dies. Signing in with
LinkedIn is how the person gets out of it:

```
Them>  cancel my table on the 19th
Bot>   I can't change a booking over social media - I have no way to confirm
       who you are from a handle alone. You can verify yourself in a few
       seconds by signing in with LinkedIn, and then I can help right here:

       https://www.linkedin.com/oauth/v2/authorization?...

Them>  [signs in]
Them>  cancel my table on the 19th
Bot>   Done - your table on Saturday the 19th is cancelled.
```

Nothing about the permission model changed to allow that. The person moved tiers
because they produced evidence.

### What a sign-in actually establishes

LinkedIn returns an email it has verified. What that is worth depends on whose
email it turns out to be:

| What was verified | Tier | What they can reach |
| --- | --- | --- |
| The email is not verified | `public` | Nothing changes |
| It is at one of your own domains | `workspace` | Staff-level lookups |
| It matches a customer record | `authenticated` | Their own records |
| It is verified but matches nothing | `public` | Nothing changes, and the page says so |

The last row matters. Somebody can sign in perfectly successfully and still be
nobody this deployment knows, and saying that plainly is better than implying
their sign-in failed.

**Configuration never sets the tier**, only evidence does. And a verified
customer cannot move money whatever you configure: refunds stay with an operator
holding a token issued by `auth.py`. Proving you are the customer is not
authorisation to refund yourself.

### 10-1. Create the LinkedIn app

1. Go to linkedin.com/developers and create an app against a company page
2. Under **Products**, add **Sign In with LinkedIn using OpenID Connect**
3. Under **Auth**, add your callback as an authorised redirect URL
4. Copy the Client ID and Client Secret

The scopes you need - `openid`, `profile`, `email` - come with that product. You
are not asking for messaging or posting permissions, which is why this needs no
review.

### 10-2. Configure it

```bash
python auth.py --new-secret
```

Use that value for `FRONTDESK_LINKEDIN_STATE_SECRET`; it signs the sign-in links
and must not be shared with any other secret in the system.

```
FRONTDESK_LINKEDIN_CLIENT_ID=...
FRONTDESK_LINKEDIN_CLIENT_SECRET=...
FRONTDESK_LINKEDIN_REDIRECT_URI=https://verify.example.com/linkedin/callback
FRONTDESK_LINKEDIN_STATE_SECRET=<32+ characters>
FRONTDESK_LINKEDIN_WORKSPACE_DOMAINS=example.com
```

`FRONTDESK_LINKEDIN_REDIRECT_URI` must match what you registered **character for
character**. It is read from configuration rather than rebuilt from the incoming
request, because a proxy rewriting the Host header would otherwise produce a URI
that silently stops matching.

### 10-3. Run the callback server

```bash
python linkedin_verify.py --port 8790
```

It binds localhost. Put an HTTPS reverse proxy in front of it on the origin in
`FRONTDESK_LINKEDIN_REDIRECT_URI`. The server sets no cookie, keeps no session,
and uses the access token once before discarding it.

### 10-4. Decide what a verified customer may do

By default a verified customer gets `support`: they can look things up. To let
them change their own booking, grant `operator` on the channel they arrived on.

```
FRONTDESK_CHANNEL_META_ROLES=operator
```

The ceiling in `channels/base.py` still applies, so this cannot reach `finance`
however it is written.

**A role says what kind of thing may be done; it does not say whose records.**
When the principal is a verified customer, every record a tool touches is checked
against their email first - a stranger's reservation comes back as "not found on
your account", which is also all a stranger should learn about it.

### 10-5. Check it

```bash
python chat.py --doctor
```

```
Channels (Slack / Meta / LinkedIn)
  [ok] linkedin sign-in           configured - a public sender can verify and reach their own records
  [ok]   workspace domains        example.com
```

### What is stored, and for how long

A completed sign-in is remembered against the person on the channel they were
already using, so the next message is not a stranger again. That record holds a
verified email address, which is personal data:

- it expires by itself after `FRONTDESK_IDENTITY_TTL_HOURS`, eight hours by
  default, and expired records are deleted on the next read rather than left to
  rot;
- `channels.identity.forget(channel, user_id)` removes one on request;
- every verification, every failure and every deletion is an audit event;
- the file lives under `data/`, which is gitignored and excluded from every
  archive.

### The parts worth knowing about the security

**The state parameter names the conversation.** A bare random state stops
cross-site request forgery and nothing else: an attacker who completes their own
sign-in and then hands the resulting callback URL to a victim would attach their
own identity to the victim's thread. Frontdesk signs the channel, the sender and
the thread into the state, so a callback can only ever apply to the conversation
that requested it. Links expire after 15 minutes.

**The ID token is not verified locally.** It arrives from the token endpoint over
TLS to a client that authenticated with its own secret, which OpenID Connect Core
section 3.1.3.7 explicitly permits, and the claims are then read from
`/v2/userinfo`. The alternative would be hand-rolling RS256 and JWKS handling in
a project with no dependencies - writing signature verification from scratch,
which is the one thing this project will not do.

**The client secret only ever goes in the POST body to the token endpoint.** A
test asserts that, because a secret in a URL ends up in logs.

---

## 11. Approving from a phone

### The problem this fixes

The confirmation gate is the product: nothing irreversible runs without a person
saying yes. Until now that person had to be at a terminal.

Which meant the gate worked perfectly for the CLI and **refused everything
arriving on a channel**. A reservation cancellation asked for in a DM at 9pm was
declined - not because it was wrong, but because nobody was at a keyboard. That
is the right failure and it is still a failure, because the owner of a small
business is not at a terminal. They are holding a phone.

```
Them>  Please cancel reservation R-2001
Bot>   I found the confirmed reservation. Give me a moment to request approval.

       [on the owner's phone]
       ┌──────────────────────────────────────┐
       │ Cancel reservation R-2001            │
       │ Scheduled for 09/12/2026             │
       │ asked by dana.whitfield@example.com  │
       │ via meta · expires in 5m             │
       │  [ Decline ]        [ Approve ]      │
       └──────────────────────────────────────┘

Bot>   Reservation R-2001 was cancelled after approval.
```

### 11-1. Turn it on

```
FRONTDESK_REMOTE_APPROVAL=1
```

Then run the receiver as usual; the screen is served by the same process:

```bash
python webhooks.py --port 8770
```

It has to be the same process. A parked approval has the agent's own thread
waiting on it, and a thread cannot be resumed from somewhere else.

### 11-2. Sign the phone in

An access token is 180 characters. Nobody types that on a phone, and pasting one
into a mobile browser is worse. So generate a link:

```bash
python webhooks.py --pair --subject you@example.com --roles operator,finance --base-url https://desk.example.com
```

Open it on the phone. It works **once**, expires in ten minutes, and exchanges
itself for a session cookie holding a freshly issued token. The link never
contains the token, so a screenshot of it is worth nothing after the phone has
used it.

Add the page to the home screen and it behaves like an app.

### 11-3. What the phone can and cannot do

**The approver needs the permission themselves.** A phone signed in as `support`
cannot authorise a refund; it says so, rather than failing quietly, so whoever is
holding it knows to fetch somebody who can. This is stricter than the terminal,
where anyone who sits down can approve anything.

**One decision each.** An answered request is closed. Tapping twice does not run
it twice.

**Silence is a no.** An unanswered request expires after five minutes and the
action does not run. Waiting forever would hold a customer on a dead
conversation; defaulting to yes would make the gate decorative.

**Everything is audited**: requested, decided, expired, refused, and by whom.

### 11-4. Check it

```bash
python chat.py --doctor
```

```
Approvals from a phone
  [ok] remote approval           on - a phone can answer the confirmation gate
```

### Before you expose it

The screen sends a session cookie on every request, so **serve it over HTTPS**.
The cookie is `HttpOnly`, `SameSite=Strict`, scoped to `/m`, and marked `Secure`
unless the host is plainly localhost. `--pair` warns when the link it prints is
plain HTTP.

The approval screen shows customer text - what is being refunded, and for whom.
It is written into the page as text, never as markup, and the page's content
security policy allows no external anything.

### What is left to a phone's own defences

If the phone is unlocked in somebody else's hands, that person can approve
things, exactly as they could at an unlocked terminal. The cookie's lifetime is
what bounds it: `--hours` sets it, twelve by default. Sign out from the screen to
end it early.

---

## 12. Choosing the market: US or UK

Both markets are served in English, which makes it tempting to treat them as one
deployment. They are not, and the differences are not decoration.

```
FRONTDESK_REGION=uk
```

or, for one run:

```bash
python chat.py --region uk --persona healthcare
```

### What changes

| | `us` | `uk` |
| --- | --- | --- |
| Currency | USD, `$` | GBP, `£` |
| Dates | MM/DD/YYYY | DD/MM/YYYY |
| Emergency number | 911 | 999 |
| Health regulation named | HIPAA | UK GDPR and the Data Protection Act 2018 |
| Urgent, not emergency | the nurse line or urgent care | NHS 111 or an urgent treatment centre |
| Times | US zones, named (ET, CT, MT, PT) | UK time, GMT or BST |
| Spelling | US | British |

### Why this is one setting and not seven files

**05/09/2026 is two different days.** A US reader sees 5 September, a UK reader
sees 9 May. A booking confirmation that gets this wrong is an appointment the
customer does not turn up to, and nothing in the exchange looks wrong at the time.

**999 is not 911.** A patient-facing assistant that tells somebody in Britain
with stroke symptoms to dial 911 has done real harm. That single fact is the
reason `regions.py` exists rather than a note in the README asking people to
remember.

**PayPal rejects the wrong currency**, so a UK deployment taking dollars is
either an error or a settlement at a rate nobody chose.

Each persona used to state its own conventions - seven copies of "dates as
MM/DD/YYYY", seven chances to disagree. They now say only what makes them
different, and the conventions are prepended from the region. A new persona
cannot forget what it never had to write.

### Writing a persona that works in both

Write the persona without conventions; they arrive automatically. Where a fact
genuinely differs, use a placeholder:

```markdown
If a patient describes any life-threatening symptom, tell them to
{region.emergency_phrase} or go to the nearest emergency department now.
```

Available: `{region.name}`, `{region.adjective}`, `{region.currency}`,
`{region.currency_symbol}`, `{region.date_format}`, `{region.emergency_number}`,
`{region.emergency_phrase}`, `{region.health_regulation}`,
`{region.privacy_regulation}`, `{region.urgent_care}`, `{region.timezone_note}`,
`{region.spelling}`.

An unrecognised placeholder is left in the text rather than deleted. Quietly
removing part of a system prompt would be worse than leaving something visibly
wrong, and a test asserts no persona ships with one unfilled.

### Adding a third market

Add an entry to `REGIONS` in `regions.py`. Nothing else needs editing: the
currency list, the doctor's report and every persona derive from it. The tests
iterate over `regions.SUPPORTED`, so a new market is checked the moment it exists.

### Check it

```bash
python chat.py --doctor
```

```
Region
  [ok] FRONTDESK_REGION           uk - the United Kingdom
  [ok]   conventions              GBP (£), dates DD/MM/YYYY, emergency 999
```
