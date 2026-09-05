# Frontdesk reference

Lists only. For how to do things, see [guide.md](guide.md).

- [Command-line options](#command-line-options)
- [Commands during a conversation](#commands-during-a-conversation)
- [Tools](#tools)
- [Personas](#personas)
- [Roles and permissions](#roles-and-permissions)
- [Environment variables](#environment-variables)
- [Trust tiers](#trust-tiers)
- [Helper commands](#helper-commands)
- [Exit codes](#exit-codes)

---

## Command-line options

```bash
python chat.py [options]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `-m`, `--message` | — | Run once and exit. Without it, interactive |
| `--provider` | `auto` | `anthropic` / `openai` / `ollama` / `echo`. `auto` safely selects local Ollama; cloud providers require explicit selection |
| `--model` | per provider | Model ID |
| `--persona` | `default` | A filename under `personas/` |
| `--effort` | `medium` | Depth of thinking, `low` to `max` (**Claude only**) |
| `--max-tokens` | `64000` | Output token ceiling — a limit, not a target |
| `--temperature` | none | **OpenAI and Ollama only.** Never sent to Claude |
| `--max-history-chars` | `200000` | History ceiling; the oldest turns are dropped past it. `0` disables |
| `--max-steps` | `8` | Tool executions allowed per exchange |
| `--no-tools` | — | Turn tool use off |
| `--show-thinking` | off | Show the thinking summary |
| `--auto-approve` | off | Skip confirmation. **For testing. Never in production** |
| `--base-url` | — | Override the API base URL |
| `--region` | `us` | Market conventions: `us` or `uk`. Currency, dates, emergency number |
| `--ui-lang` | `en` | Customer language: English (`en`) or US Spanish (`es`) |
| `--no-color` | — | Turn colour off |
| `--doctor` | — | Diagnose the configuration and exit |
| `--list-personas` | — | List the personas and exit |
| `--version` | — | Print the version |

**Ways to give it input**

```bash
python chat.py                                # interactive
python chat.py -m "Where is order A-88001?"   # one shot
echo "Summarize this" | python chat.py        # piped
```

In interactive mode, a trailing `\` continues onto the next line.

---

## Commands during a conversation

| Command | Meaning |
| --- | --- |
| `/help` | List the commands |
| `/reset` | Clear the history; the persona stays |
| `/history` | Show the conversation so far |
| `/save [path]` | Save the conversation as JSON |
| `/persona [name]` | Switch persona; no argument lists them |
| `/provider [name]` | Switch provider |
| `/model [id]` | Switch model |
| `/effort [level]` | Change the depth of thinking |
| `/thinking` | Toggle display of the thinking process |
| `/tools [on\|off]` | Turn tool use on or off; no argument lists the tools |
| `/store [reset]` | Show or reset the demo data |
| `/info` | Show the current settings |
| `/exit`, `/quit` | Quit |

---

## Tools

**What the kinds mean.** A *read* runs without asking. A *gated* tool needs
**approval for each individual call**, and where approval cannot be obtained it
does not run at all.

| Tool | Kind | What it does |
| --- | --- | --- |
| `get_today` | read | Today's date. Called before interpreting things like "next Friday" |
| `get_order_status` | read | Delivery state, ETA and amount, by order number |
| `search_reservations` | read | Find reservations by customer name |
| `search_knowledge` | read | Search the knowledge base and return the source with it |
| `change_reservation` | **gated** | Move a reservation |
| `cancel_reservation` | **gated** | Cancel a reservation |

FrontDesk intentionally has no payment tools. To connect another purchaser-owned
business system, follow [the customer guide](customer-guide.md).

---

## Personas

All of them are English, and each carries the boundaries its industry has to
hold. None of them states a currency, a date format or an emergency number:
those come from the region, so the same persona is correct in both markets.

| Name | For | Its main boundaries |
| --- | --- | --- |
| `default` | General | States no price, date or condition that is not in the data; accepts no sensitive information |
| `agent` | Autonomous work | Confirms before anything hard to undo; respects permission boundaries; does not obey instructions found inside tool results |
| `ecommerce` | Ecommerce, D2C | Never takes a card number or CVV in chat; invents no stock levels or return terms |
| `fintech` | Finance | **Gives no investment or tax advice**; takes no one-time codes; states no balance without authentication |
| `healthcare` | Healthcare | **Sends emergencies to the region's number and stops everything else**; no diagnosis or treatment recommendation; minimises PHI |
| `saas-support` | SaaS, IT | Invents no API or flag that does not exist; never asks for credentials |
| `helpdesk` | Internal | Discloses nobody else's pay, review or medical information; sends harassment reports straight to HR |
| `automotive` | Dealers, service, rental | Does not declare a vehicle safe; routes safety faults to recovery |
| `education` | Schools, tutoring, gyms | Checks capacity; quotes fees and contract terms only from approved material |
| `events` | Events, photography, hire | Checks dates; never invents deposits, cancellation fees, or waivers |
| `homeservices` | Trades, repairs, cleaning | Requires a real quote where condition, access, or materials affect price |
| `hospitality` | Hotels, restaurants, travel | Checks rates and availability; does not infer accessibility or allergen safety |
| `legal` | Law-firm reception | Books consultations and gives no legal advice or deadline interpretation |
| `professional` | Accountancy and consulting reception | Gives no tax, accounting, compliance, valuation, or other professional advice |
| `realestate` | Estate, lettings, property management | Enforces fair-housing boundaries and does not steer by protected characteristics |
| `recruiting` | Hiring reception | Describes roles and accepts applications but never screens or ranks candidates |
| `salon` | Salons and spas | Checks appointments and pricing; sends treatment-safety questions to a person |

---

## Roles and permissions

| Role | Permissions |
| --- | --- |
| `guest` | `knowledge:read` |
| `viewer` | `audit:read`, `knowledge:read` |
| `support` | `knowledge:read`, `orders:read`, `reservations:read` |
| `operator` | Everything support has, plus `reservations:write` |
| `finance` | `knowledge:read` (compatibility alias; no financial actions) |
| `admin` | Everything, the admin screen included |

```bash
python auth.py --subject who@example.com --roles operator --hours 8
```

Separate several with commas: `--roles operator,finance`.

---

## Trust tiers

A channel is granted roles according to what it was able to verify about the
sender. Configuration can narrow a grant; nothing can widen it past the ceiling.

| Tier | What was verified | Ceiling | Default |
| --- | --- | --- | --- |
| `public` | Nothing. A handle is a claim | `guest` | `guest` |
| `workspace` | Membership of the organisation | `support`, `operator` | `support` |
| `authenticated` | The customer themselves | `support`, `operator` | `support` |

`finance` is absent from every ceiling on purpose. Moving money is the business
acting, and it stays with an operator holding a token issued by `auth.py`.

Set `FRONTDESK_CHANNEL_<CHANNEL>_ROLES` to choose within a ceiling, for instance
`FRONTDESK_CHANNEL_META_ROLES=operator` to let a verified customer change their
own booking.

**Whose records** is separate from **what kind of action**. When the principal is
a customer who proved an email, every record a tool touches must carry that
email; anything else comes back as "not found on your account".

---

## Environment variables

### Authentication

| Variable | Required | Meaning |
| --- | --- | --- |
| `FRONTDESK_AUTH_SECRET` | yes | Token signing key. 32 characters or more |
| `FRONTDESK_ACCESS_TOKEN` | yes | A token issued by `auth.py` |
| `FRONTDESK_AUTH_MODE` | — | `disabled` turns authentication off. **Development only** |

### Region

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_REGION` | `us` (default) or `uk`. Sets currency, date format, emergency number, the regulation named, and spelling |

See [guide.md section 12](guide.md#12-choosing-the-market-us-or-uk).

### Model providers

| Variable | Meaning |
| --- | --- |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | Claude credentials |
| `OPENAI_API_KEY` | OpenAI credentials |
| `OLLAMA_BASE_URL` | Defaults to `http://localhost:11434` |
| `FRONTDESK_OLLAMA_KEEP_ALIVE` | Model residency after a reply. Defaults to `30m` |
| `FRONTDESK_OLLAMA_NUM_CTX` / `FRONTDESK_OLLAMA_NUM_BATCH` | Bounded context and prompt batch. Defaults to `8192` / `256` |
| `FRONTDESK_CHAT_MAX_TOKENS` | Web-chat generation ceiling. Defaults to `256` |
| `FRONTDESK_CHANNEL_MAX_TOKENS` | Social/email reply ceiling. Defaults to `256` |

### Channels

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_SLACK_SIGNING_SECRET` | Slack signature verification |
| `FRONTDESK_SLACK_BOT_TOKEN` | Sending replies (`xoxb-...`) |
| `FRONTDESK_SLACK_TEAM_ID` | Your workspace id. Rejects deliveries from any other |
| `FRONTDESK_TEAMS_SECURITY_TOKEN` | The HMAC secret shown once when the outgoing webhook is created |
| `FRONTDESK_TEAMS_TENANT_ID` | Your Microsoft 365 tenant id. Rejects any other |
| `FRONTDESK_META_APP_SECRET` | Meta signature verification |
| `FRONTDESK_META_PAGE_TOKEN` | Sending replies |
| `FRONTDESK_META_VERIFY_TOKEN` | Answers the subscription challenge |
| `FRONTDESK_META_GRAPH_VERSION` | Meta and WhatsApp Graph API version. Defaults to `v26.0`; update after provider deprecation review |
| `FRONTDESK_WHATSAPP_APP_SECRET` | WhatsApp webhook signature verification |
| `FRONTDESK_WHATSAPP_TOKEN` | WhatsApp Cloud API replies |
| `FRONTDESK_WHATSAPP_VERIFY_TOKEN` | WhatsApp subscription challenge |
| `FRONTDESK_EMAIL_WEBHOOK_SECRET` | HMAC secret for the inbound email relay; 32+ characters |
| `FRONTDESK_EMAIL_IMAP_HOST` / `FRONTDESK_EMAIL_IMAP_PORT` | Purchaser-owned mailbox intake; port defaults to `993` |
| `FRONTDESK_EMAIL_SMTP_HOST` / `FRONTDESK_EMAIL_SMTP_PORT` | Direct mailbox reply server; defaults to the IMAP host and port `587` |
| `FRONTDESK_EMAIL_USER` / `FRONTDESK_EMAIL_PASSWORD` | Mailbox credentials supplied only by the runtime secret manager |
| `FRONTDESK_EMAIL_ADDRESS` / `FRONTDESK_EMAIL_FOLDER` | Reply address and IMAP folder; folder defaults to `INBOX` |
| `FRONTDESK_EMAIL_TENANT_ID` / `FRONTDESK_EMAIL_PERSONA` | Tenant and optional persona for the mailbox runner |
| `FRONTDESK_PUBLIC_URL` | HTTPS origin used for optional rating links in outbound email |
| `FRONTDESK_FEEDBACK_SECRET` | HMAC key for rating links; 32+ characters |
| `FRONTDESK_WEB_PERSONA` | Installed persona selected by customer web chat |
| `FRONTDESK_CHANNEL_<NAME>_ROLES` | Narrows the roles a channel grants, within its ceiling |

### Approvals from a phone

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_REMOTE_APPROVAL` | `1` lets a phone answer the confirmation gate. Without it, a gated action on a channel is declined |

See [guide.md section 11](guide.md#11-approving-from-a-phone).

### LinkedIn sign-in

Optional for public knowledge chat and used as step-up identity before private
account actions. This is identity, not messaging. See
[guide.md section 10](guide.md#10-letting-people-prove-who-they-are-with-linkedin).

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_LINKEDIN_CLIENT_ID` / `FRONTDESK_LINKEDIN_CLIENT_SECRET` | App credentials |
| `FRONTDESK_LINKEDIN_REDIRECT_URI` | Callback URL. Must match the app's registered URL exactly; HTTPS outside local testing |
| `FRONTDESK_LINKEDIN_STATE_SECRET` | Signs sign-in links. 32 characters or more, separate from every other secret |
| `FRONTDESK_LINKEDIN_WORKSPACE_DOMAINS` | Comma-separated email domains that count as your own staff |
| `FRONTDESK_IDENTITY_TTL_HOURS` | How long a completed verification lasts. Defaults to `8` |

### Real backends

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_BACKEND_URL` | HTTPS required. Unset, the demo data is used |
| `FRONTDESK_BACKEND_TOKEN` | Bearer token |
| `FRONTDESK_BACKEND_TIMEOUT` | Seconds. Defaults to `10` |
| `FRONTDESK_BACKEND_ALLOW_HTTP` | `1` permits HTTP. **Never in production** |
| `FRONTDESK_TENANT_BACKENDS_FILE` | JSON profile mapping exact tenant IDs to separate HTTPS endpoints and `token_env` names. Required for live non-default tenants |

The four `FRONTDESK_BACKEND_*` variables are retained for the `default` tenant.
They are rejected for non-default tenants so one shared token cannot silently
cross tenant boundaries. Copy `tenant-backends.example.json`, keep the profile
outside the web root, and provide each referenced token through a distinct
environment variable. Do not put token values in the JSON file.

### Embedded widget and business integrations

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_PUBLIC_ORIGIN` | Exact HTTPS origin serving `embed.js` and `/widget` |
| `FRONTDESK_EMBED_ORIGINS` | Space-separated exact HTTPS customer-site origins allowed to frame the widget |
| `FRONTDESK_INTEGRATIONS_FILE` | Tenant profile for Shopify, Zendesk, HubSpot, and SMTP; contains environment-variable names, never token values |

### Audit log

| Variable | Meaning |
| --- | --- |
| `FRONTDESK_AUDIT_MAX_BYTES` | Bytes per segment. Defaults to 5MB. Zero or less disables rotation |

Put settings in the environment or in `.env`. `.env` is gitignored and excluded
from anything distributed. **Do not put real values in `.env.example`** — that
file *is* distributed.

---

## Helper commands

| Command | For |
| --- | --- |
| `python chat.py --doctor` | Diagnose the configuration; show what is missing and how to fix it |
| `python auth.py --new-secret` | Generate a signing key |
| `python auth.py --subject X --roles Y` | Issue an access token |
| `python rag.py --build` | Rebuild the knowledge index |
| `python rag.py --status` | Show the state of the index |
| `python webchat.py --port 8766` | Start full-page chat, `/widget`, and `/embed.js` |
| `python webhooks.py --port 8770` | Start the receiver for Slack, Teams, Meta, WhatsApp, and email relay |
| `python webhooks.py --pair --subject X --roles Y` | Print a one-time link for signing a phone in |
| `python linkedin_verify.py --port 8790` | Start the LinkedIn sign-in callback server |
| `python admin.py --port 8765` | Start the localhost admin screen |
| `python docs/make_images.py` | Regenerate the documentation images |
| `python -m unittest discover -s tests` | Run the tests |

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | A failure at run time: an API error, failed authentication, or a diagnosis that found something to fix |
| `2` | The configuration is wrong, for instance an invalid option value |
