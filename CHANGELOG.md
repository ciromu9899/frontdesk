# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## 1.7.0 - 2026-09-05

- Added a GitHub Issues and Discussions support channel with verified webhook
  signatures, per-installation tenant isolation, durable delivery deduplication,
  and bot-loop prevention.
- Added real Issue replies through GitHub REST and Discussion replies through
  GitHub GraphQL, using a short-lived Actions or installation token.
- Added an OS-neutral GitHub Action entry point, least-privilege workflow guide,
  GitHub-specific public-support persona, and twelve connector simulations.
- Preserved the web, shared inbox, human handoff, and existing channel features.

## 1.6.0 - 2026-09-05

- Added an OS-neutral headless server that runs customer chat and the shared
  inbox together on Windows, macOS and Linux.
- Added OCI/Docker and Compose delivery with persistent customer data, public
  health checks, a safe Echo default and an optional containerized Ollama model.
- Added configurable host or network Ollama discovery and secure-cookie support
  for HTTPS reverse-proxy deployments.
- Added a cross-platform operations guide and automated listener/health tests.
- Kept the Windows portable executable as an optional compatibility edition.

## 1.5.0 - 2026-08-29

- Added a portable Windows application that includes the Python runtime and a
  pinned `llama.cpp` server, so customers do not install Python or Ollama.
- Added consent-based first-run download of the pinned Qwen3-8B Q4_K_M model,
  with atomic installation and SHA-256 verification.
- Added an OpenAI-compatible local `llamacpp` provider and preflight detection
  for web, admin, and local-AI port conflicts.
- Moved packaged-app conversations, audit history, handoffs, identity records,
  webhook deduplication, and uploaded knowledge to durable Local AppData.
- Kept the Python plus Ollama launcher as an advanced compatibility path.
- Passed 264 automated checks with zero failures after the packaging changes.

## 1.4.3 - 2026-08-29

- Added a guided salon launcher that generates temporary authentication,
  selects US/UK and language settings, starts customer chat and the shared
  inbox together, and opens both pages without writing credentials to disk.
- Added a Windows double-click launcher and Ollama readiness check with an Echo
  demonstration fallback.
- Replaced obsolete single-package sales copy with the current Solo, Shop, and
  Multi PayPal plans.
- Passed 259 automated checks with zero failures.

## 1.4.2 - 2026-08-29

- Added bounded exponential backoff with jitter, `Retry-After` support, and
  per-client circuit breakers for third-party REST integrations.
- Automatic retries are restricted to reads, explicitly safe searches, and
  writes preserving the same idempotency key.
- Added a local no-side-effect external API capacity simulator covering queue
  pressure, latency, `429`/`503`, retry bounds, and duplicate-effect detection.

## [Unreleased]

## [1.4.1] - 2026-08-29

### Added

- Restored ten customer-industry personas from the verified 1.3.0 release,
  bringing the selectable set to seventeen industry personas plus the default.
- Added a purchaser-controlled IMAP/SMTP shared-mailbox runner alongside the
  existing signed email relay, with loop prevention, reply threading, durable
  deduplication, and retry after model or SMTP failure.
- Added expiring HMAC-signed CSAT links and accessible English and US-Spanish
  feedback pages for email and other non-widget channels.

### Changed

- Repeated feedback for one tenant conversation now updates its existing CSAT
  response instead of inflating the response count.
- Customer web chat now honours a valid `FRONTDESK_WEB_PERSONA` and safely falls
  back to the language-specific ecommerce persona for an invalid value.

## [1.4.0] - 2026-08-28

### Added

- Added an opt-in salon and wellness industry pack with service, location, and
  stylist catalogues, availability search, approved appointment creation, and
  overlapping-booking protection.
- Added retry-safe email appointment reminders and a responsive upcoming
  appointments view for authenticated administrators.
- Added deterministic human handoff for salon chemical-treatment, allergy,
  reaction, pregnancy, and patch-test safety questions without collecting
  medical details.
- Added tenant backend contracts for services, availability, and idempotent
  appointment creation.

### Changed

- Added the narrow `appointments:create` permission for verified customers and
  support users without granting reservation change or cancellation rights.
- Industry-specific tools are hidden and rejected unless the tenant explicitly
  enables `FRONTDESK_INDUSTRY=salon` or `wellness`.

## [1.3.2] - 2026-08-27

### Added

- Added pre-model rejection of passwords, one-time codes, card security codes,
  Social Security numbers, API credentials, and Luhn-valid payment card numbers.
- Added focused conversational regression coverage for sensitive-input handling,
  low-relevance knowledge, bounded web settings, and durable session restoration.

### Changed

- Discarded low-relevance retrieval hits before they can reach the model.
- Made invalid or excessive web-chat tuning values fall back or clamp safely.
- Kept seller-only PayPal, order, entitlement, and licence operations outside the
  customer release archive.

## [1.3.1] - 2026-08-24

### Added

- Added US/UK sales-readiness knowledge and market/language quality gates.
- Added deterministic emergency-number and requested-human-handoff paths.

### Changed

- Added citations only when a generated answer materially overlaps server-retrieved
  evidence, preventing both missing and invented source identifiers.
- Improved Spanish evaluation and expanded approved Spanish warranty knowledge.
- Corrected the reference guide's obsolete English-only statement.

## [1.3.0] - 2026-08-24

### Added

- Added an embeddable English and US-Spanish web-chat widget, durable shared
  inbox, assignment, human takeover, internal notes, CSAT, and analytics.
- Added tenant-scoped knowledge upload and reindexing for PDF and Office files.
- Added Shopify, Zendesk, HubSpot, WhatsApp Cloud API, and signed email-relay
  integrations with secrets referenced only through environment-variable names.
- Expanded the real-provider quality suite from 41 to 60 cases.

### Changed

- Changed LinkedIn OpenID Connect from a startup requirement to optional step-up
  authentication for private account actions; guest knowledge chat remains open.
- Tuned Ollama for a resident model, bounded context, and shorter support answers.
- Corrected product copy so the delivered application does not claim payment,
  charge, capture, or refund capabilities.

## [1.2.0] - 2026-08-23

### Changed

- Removed payment creation, capture, status and refund tools from the FrontDesk
  runtime and installer.
- Separated ShellieSoftwareTools' external PayPal product sale from the delivered
  customer application.
- Made the customer release web-chat and Slack/Meta focused, with purchaser-owned
  connector credentials and tenant-scoped data.

## [1.1.0] - 2026-08-23

### Added

- Customer web chat in English and US Spanish, local Ollama as the fixed
  automatic provider, durable sessions and webhook deduplication, tenant-scoped
  state and connectors, and PDF/Office hybrid retrieval.
- Isolated Shellie product sales ledger using `SHELLIE_PAYPAL_*`, PayPal webhook
  verification, tax holds, buyer claims, receipts, refunds, disputes and signed
  download entitlements.
- Customer legal-pack drafts, retention schedule, subprocessor list and updated
  legal risk register.
- Deterministic release builder with CycloneDX SBOM, SHA-256 manifest, guarded
  installation, backup, rollback and Windows catalog-signing workflow.
- The installer now refuses an unsigned catalog and verifies that the signed
  catalog covers the release directory before changing an installation.

### Security

- PayPal TLS verification remains enabled in compatibility mode.
- Product-sale events are durably deduplicated and price, currency and optional
  merchant identity are checked before fulfilment.

### Release gates

- Qualified legal/tax review, real-user accessibility testing, production
  recovery evidence and certificate-backed release signing remain mandatory
  before a general-availability or compliance claim.

## [1.0.0] - 2026-08-20

First public release.

### Added

- Conversation loop with provider switching across Anthropic, OpenAI, Ollama, and
  a dry-run `echo` provider that needs no credentials.
- A confirmation gate on every irreversible tool call. One approval covers one
  action; where no approval can be obtained, the action is declined rather than
  assumed.
- PayPal Orders API v2: order creation, status, capture, and refund, with
  idempotency keys so a retry cannot charge twice. Card details never reach the
  agent.
- Seven personas carrying the boundaries their industries require.
- Signed access tokens and role-based permissions.
- Tamper-evident audit log with a SHA-256 hash chain that survives size-based
  rotation across files.
- Local document retrieval with citations. Documents are never sent to an external
  embedding service.
- Slack and Meta (Instagram DM / Messenger) channels, where the trust a channel can
  actually establish determines the permissions it receives. A public channel is
  capped at `guest` and cannot be configured upward.
- A guarded REST connector for order and reservation systems, enforcing HTTPS,
  bearer auth, timeouts, response size limits, and idempotency keys.
- `--doctor` for diagnosing configuration, and a localhost administration console.
- A bounded conversation history that never orphans a tool result or leaves the
  transcript starting on a non-user turn.
- Access tokens must be spelled canonically. Base64 leaves the trailing bits of
  the final character unused, so one signature had sixteen valid encodings;
  requiring the round trip keeps a token's text and its meaning in step.
- `tools_check_secrets.py`, `tools_check_links.py` and `tools_check_language.py`,
  run in CI: no credential-shaped string is committed, every documentation link
  resolves, and the surface the model and the customer read stays English.
- `docs/design-notes.md`, recording the decisions that constrain the code so that
  changing one is deliberate rather than accidental.
- Sign In with LinkedIn, as identity rather than messaging: somebody on a public
  channel can prove a verified email and reach their own records, where before
  the refusal was a dead end. LinkedIn exposes no inbound-DM webhook to third
  parties, so there is no message channel to build.
- `webhooks.py`, the receiver the platforms actually post to. The Slack and Meta
  adapters were complete and unreachable before it: the setup instructions named
  an endpoint that nothing served.
- Microsoft Teams, via an outgoing webhook rather than the Bot Framework -
  HMAC-SHA256 over the body, no Azure registration, and a reply carried in the
  HTTP response.
- Delivery de-duplication. A platform retry no longer runs the work a second
  time, which for this agent means no longer cancelling a reservation twice.
- An approval screen built for a phone, and the third path through the
  confirmation gate that makes it useful: an irreversible action arriving on a
  channel can now be parked and answered with a tap instead of being declined
  because nobody was at a terminal. The approver must hold the permission the
  tool needs, each request is decided once, and an unanswered one expires.
- `webhooks.py --pair`, a one-time link that signs a phone in without a token
  ever appearing in a URL.
- UK support alongside the US, as one setting. Currency, date format, spelling,
  the regulation a persona names, and the emergency number all follow
  `FRONTDESK_REGION`. The personas no longer state conventions of their own, so a
  new one cannot forget them and the two markets cannot drift apart.
- Record-level ownership. A principal carrying a verified email is scoped to
  records bearing that email; a stranger's record reports as not found, which is
  also all a stranger should learn about it.

### Changed

- A session's running commentary now goes to a stream chosen by the caller. The
  CLI is unaffected; a channel server discards it, so customer messages are not
  copied to a server's stdout.
- `finance` is no longer in any channel trust ceiling. Proving you are the
  customer is not authorisation to move money; refunds stay with an operator
  holding a token issued by `auth.py`.

### Known limitations

- History is trimmed, not summarised. Dropped context is gone.
- Two markets are supported, US and UK. A third is a dictionary entry in
  `regions.py`, but nothing beyond those two has been reviewed by anyone who
  works in that market.
- The knowledge index handles `.md`, `.txt`, and `.html`. PDF and Office documents
  are not ingested.
- Tenant isolation reaches authentication and the audit log, but not the knowledge
  index or the backend connector. Running two customers in one deployment will
  cross their data.
- Slack and Meta have not been exercised against live workspaces. Signature
  verification, normalisation, and permission handling are covered by tests
  against recorded payload shapes.

[1.0.0]: https://github.com/shellie-software-tools/frontdesk/releases/tag/v1.0.0
