# Design notes

Why Frontdesk is built the way it is. Each entry is a decision that constrains
the code, written down so that changing it is a deliberate act rather than an
accident.

- [The agent](#the-agent)
- [Doing things that cannot be undone](#doing-things-that-cannot-be-undone)
- [Money](#money)
- [Identity, and who is allowed to ask](#identity-and-who-is-allowed-to-ask)
- [Evidence](#evidence)
- [Language](#language)
- [What is deliberately absent](#what-is-deliberately-absent)
- [Things an external review caught](#things-an-external-review-caught)
- [What is not finished](#what-is-not-finished)

---

## The agent

**Conversation history is vendor neutral, and each provider translates it on the
way out.** Providers disagree sharply about how a tool call is represented:
Claude puts `tool_use` and `tool_result` blocks inside user turns, OpenAI uses a
`tool_calls` array and a separate `tool` role. History is kept as a list of
`Turn`, and `providers.py` converts at send time. Without that layer, "switch
provider" would mean rewriting the application, and provider independence would
be a claim rather than a feature.

**Claude's own blocks are replayed exactly as they arrived.** `Turn.raw` keeps
the provider-native payload and it is preferred when talking to the same
provider again, because thinking blocks have to go back unmodified. Normalising
them would be lossy in a way that only shows up several turns later.

**No sampling parameters are sent to Claude.** `claude-opus-5` rejects
`temperature`, `top_p` and `top_k` with a 400. `--temperature` therefore reaches
OpenAI and Ollama only. This is a property of the model, not a preference.

**History is bounded, and the trim has three invariants**: a `tool_use` is never
separated from its `tool_result`, the history always begins on a user turn, and
the request currently in flight is never touched. Break the first and the API
returns 400; break the second and Claude does; break the third and the agent
forgets what it was asked mid-task. The trim is visible on screen when it
happens.

---

## Doing things that cannot be undone

**The gate is the product.** Anything marked `dangerous=True` in `tools.py`
stops and asks before it runs. The test for the flag is simply whether the action
can be taken back — a payment, a refund, a message sent, a deletion, an external
notification.

**An approval can be answered from a phone, and the phone is held to more than
the terminal was.** The gate had exactly two outcomes: ask the person at the
keyboard, or decline. That made it perfect for the CLI and useless for every
channel, since a DM at 9pm has nobody at a keyboard. Now an action can be parked
and shown on a phone - and the person tapping must hold the permission the tool
needs, which is stricter than the terminal, where whoever sits down can approve
anything.

Three properties keep a tap worth what a keypress was worth: one decision per
request, an approver who could have done it themselves, and an unanswered request
that expires rather than proceeding. The last one matters most. A gate that
defaults to yes when nobody looks is not a gate.

**One approval covers one action.** There is no batch approval and no "approve
all for this session". A mechanism for approving things in bulk is a mechanism
for approving things you did not read.

**Where consent cannot be obtained, nothing runs.** Piped input and one-shot mode
have no human at the keyboard, so gated tools are declined rather than assumed.
The alternative — treating an absent human as a yes — is how an unattended cron
job issues a refund.

**Failure is reported as failure.** Declines and errors go back to the model with
`is_error` set. The agent is never told an action succeeded when it did not,
because a model that believes a refund went through will tell the customer so.

**Tool results are untrusted data.** A document retrieved from the knowledge base
can contain text that looks like an instruction. The personas say so explicitly,
and nothing in a tool result can raise permissions or bypass the gate.

---

## Money

**The bot never touches card details.** It creates an order; the buyer approves
on PayPal's own hosted page; the bot captures. Card numbers and PayPal
credentials never enter the conversation. This is PayPal's standard flow, and it
agrees with the personas' rule against accepting a card number in chat.

**Capture and refund carry idempotency keys**, so a retried request returns the
original result instead of charging twice. The key is derived per operation, not
per session — two different refunds must not collide, and the same refund
retried must.

**There is a ceiling in the code**, $10,000 per transaction. It is not a business
rule so much as a limit on how much damage one mistake can do.

**Sandbox is the default.** Live happens only when `PAYPAL_ENV=live` is set
explicitly, and `verify_paypal.py` refuses to run against live without
`--allow-live`.

**Stubs are not connectivity.** `verify_paypal.py` exists because a passing test
suite says nothing about whether the real API is reachable with the real
credentials. It goes as far as creating an order and reading it back, and moves
no money.

---

## Identity, and who is allowed to ask

**The hard question is: who approves a refund demanded over DM?** In the CLI, a
human at the terminal presses the key. There is no terminal behind an Instagram
message. Connecting a social channel without answering this puts money behind
whoever claims to be someone.

**The answer is what the channel could actually verify**, mapped onto the
permission system that already exists. No new mechanism was invented for this.

| Trust tier | What was verified | Roles |
| --- | --- | --- |
| `public` | Nothing. A handle is a claim | `guest` |
| `workspace` | Membership of the organisation | `support`, `operator` |
| `authenticated` | The customer themselves | `support`, `operator` |

**`finance` appears in no ceiling.** Proving you are the customer is not
authorisation to refund yourself. Moving money is the business acting, and it
stays with an operator holding a token this system issued.

**The ceiling cannot be configured away.** Setting
`FRONTDESK_CHANNEL_META_ROLES=finance` does not grant `finance`; the part above
the tier's ceiling is dropped. A misconfiguration should not be able to put
payment permissions on a public channel.

**Teams is integrated as an outgoing webhook, not through the Bot Framework.**
The Bot Framework authenticates with an RS256 JWT against a rotating key set;
an outgoing webhook authenticates with HMAC-SHA256 over the body, the same
primitive Slack and Meta already use. The second can be implemented correctly
here and the first cannot, and it needs no Azure registration to try. The cost is
that its reply is the HTTP response rather than an API call, so `send()` on that
channel raises instead of pretending.

**A webhook is acknowledged before it is answered.** Slack calls a delivery
failed after about three seconds and retries; a retry arriving mid-thought would
run the work twice, and the work includes cancelling reservations. So the reply
goes out through the platform's API afterwards, and delivery ids are remembered
for ten minutes. Teams cannot work this way - synchronous is the only shape it
offers - which is the honest trade for how easy it is to set up.

**The session's running commentary is discarded on a server.** In a terminal it
is the interface; in a webhook process it is a second, unredacted, unrotated copy
of every customer message, interleaved across threads. `audit.py` is the record
that is meant to exist.

**Signatures are verified with `hmac.compare_digest`.** Comparing with `==`
returns faster on an early mismatch, which leaks the signature a byte at a time.
Slack's scheme signs `v0:{timestamp}:{body}`, so replays are rejected as well as
forgeries; Meta uses `X-Hub-Signature-256`.

**A missing secret fails closed.** With nothing configured, verification always
returns false. The failure mode of "not configured yet" must not be "accepts
everything".

**A tier can be earned, not only assigned.** The tiers above were a taxonomy
with no way into the upper two from a public channel - a person stuck at `guest`
had no move to make. Sign In with LinkedIn is that move: they produce a verified
email, and where that email lands decides the tier. This is why LinkedIn is
integrated as identity and not as an inbox; it also has no inbound-DM webhook for
third parties, so the inbox was never on offer.

**A role says what may be done. It never says whose records.** For an operator
that distinction does not arise: they act for the business and see everything.
For a customer who signed in it is the whole game, since `operator` would
otherwise let them cancel a stranger's booking. So a principal carrying a
verified email is scoped to records bearing that email, and a record belonging to
someone else reports as "not found on your account" - the same words as a record
that does not exist, because confirming that R-2003 exists but is not yours is
itself a disclosure.

**Access tokens must be spelled canonically.** Base64 ignores the unused bits of
its final character, so one signature had sixteen valid encodings. Requiring the
decode to round-trip keeps a token's text and its meaning in one-to-one
correspondence.

---

## Evidence

**The audit log is hash chained, and the chain survives rotation.** When a
segment fills, the first event written to the new file carries the previous
file's final hash. Tampering stays detectable on either side of a rotation, which
is precisely where a naive implementation stops being able to prove anything.
Reads stream; the log is never loaded whole.

**Retrieval is local and cited.** Answers carry `filename#chunk-N`. Documents are
never sent to an external embedding service — for many of the industries this
targets, that is the difference between usable and not.

**The demo images are real output.** `docs/make_images.py` runs `chat.py` and
captures stdout. The frame, the prompts, the tool notices, the confirmation
dialog and the data the tools return are all genuine. Only the model's own prose
is scripted, so that the images can be regenerated without an API key — that one
substitution is stated here because a reader cannot see it from the image.

**Colours in the SVGs are attributes, not `<style>`.** Pasted into Office and
similar, a `<style>` block is dropped and every glyph turns black.

**In the HTML demo, no colour is transitioned.** A colour that comes from a CSS
custom property freezes on its old value when the theme changes, leaving the
text switched and the background not. Only `filter` and `transform` are
transitioned; colour changes land immediately.

---

## Language

**Two English-speaking markets are not one market.** The US and the UK share a
language and disagree about the things a support agent says most often: 05/09/2026
is two different days, a refund is dollars or pounds, and the number to call in an
emergency is 911 or 999. That last one is why `regions.py` exists as code rather
than as a note asking people to remember - a healthcare assistant confidently
giving a British patient the American emergency number is not a formatting bug.

Each persona used to carry its own conventions, which is seven copies of a fact
and seven chances to disagree. They now carry only what makes them different, and
the region supplies the rest. Adding a third market is one dictionary entry, and
the tests iterate over the supported regions so it is covered the moment it exists.

**English and US Spanish ship together.** Personas, user-facing tool results,
approval prompts and errors have matching locale entries so the answer language
does not drift halfway through an operational flow. Language and region remain
separate axes on purpose: the UK and the US share a language and differ in
conventions, while a Spanish-speaking customer in Texas needs US conventions in
Spanish.

**CI enforces the supported languages**, because the author writes Japanese and
the rule is easy to break by accident: `tools_check_language.py` fails the build
if Japanese product copy appears anywhere in the tree.

---

## What is deliberately absent

**X, formerly Twitter.** API access is paid, the terms keep shifting, and the
signature scheme cannot be implemented here with confidence. Signature
verification done wrong is more dangerous than none at all, because it looks like
protection. Separately, a public mention is the weakest identity available, which
makes it the worst fit for a bot that touches money.

**Batch approval.** See above.

**ROI figures.** Numbers like "$4 saved per contact" and "payback in three to six
months" circulate widely, and none of them could be traced to a source that could
be checked. Marketing claims about return need either measurements from the
customer's own deployment or a citation.

---

## Things an external review caught

An automated review pass over the first build raised seven issues worth
recording, because each one is a mistake that reads as correct:

- **PayPal amounts were parsed as floats.** They are `Decimal` now, and a value
  with excess precision is rejected rather than rounded into something
  acceptable. Rounding an invalid amount into a valid one is how you charge a
  number nobody approved.
- **Refund idempotency keys were derived per amount.** Two deliberate refunds of
  the same amount collided and the second silently returned the first. The key
  now comes from the tool call's operation ID, so retries stay safe and distinct
  refunds stay distinct.
- **Extending the audit chain read the whole log**, and non-ASCII tampering
  raised instead of reporting. It now reads only the trailing event, and a
  corrupted hash is a `False` verdict rather than an exception.
- **The index missed single-character terms and unspaced scripts.** Both are
  handled, with a version field so an old index is rebuilt rather than
  misinterpreted.
- **The Anthropic SDK floor was too low** for the streaming and adaptive-thinking
  interfaces actually used. It is pinned at 0.85.0.

## What is not finished

Stated plainly, because finding these in month two of a deployment costs more
than reading them here.

- **History is trimmed, not summarised.** What falls off the end is gone.
- **Slack and Meta have never been exercised against live workspaces.** Signature
  verification, normalisation and permission handling are tested against recorded
  payload shapes, which is not the same as having received a real webhook.
- **The name has not been cleared.** No trademark or domain search has been done,
  and "Frontdesk" is a common enough word that collisions are likely.
