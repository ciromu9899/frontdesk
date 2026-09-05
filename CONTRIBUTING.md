# Contributing

## Before you start

The tests must pass, and new behaviour needs a test that would fail without it.

```bash
python -m unittest discover -s tests
```

There are no runtime dependencies. If a change needs a third-party package for the
core to work, that is a design discussion, not a pull request - open an issue first.

## What this project is careful about

Frontdesk's value is not that an agent can act. It is that it stops before acting.
Changes that widen what an agent may do without approval will be read closely.

- **Anything irreversible needs `dangerous=True`.** The test is whether it can be
  undone, not whether it seems risky. Payments, refunds, deletions, outbound
  messages, external notifications.
- **A missing secret must never make a check pass.** Signature verification with an
  unset secret returns false, not true.
- **Trust tiers are ceilings, not defaults.** A public channel cannot be configured
  upward. If you add a channel, state what it actually verifies about the sender,
  not what you wish it verified.
- **Text returned by a tool is data.** It never becomes instruction.

## Style

- Match the surrounding code. It is plain Python with no framework.
- Comments explain why, not what. If the code needs a comment to say what it does,
  the code is the problem.
- Anything the product prints - prompts, tool descriptions, errors, personas - is
  English. It is part of the prompt the model sees; mixing languages destabilises
  the model's replies.

## Adding a tool

See [docs/guide.md](docs/guide.md), section 6. The one-line `summarize` is what an
approver reads before allowing an irreversible action. Make the amount and the
target legible in it.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
