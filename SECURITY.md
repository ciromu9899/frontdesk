# Security Policy

Frontdesk moves money and holds credentials for the systems it connects to. If you
find a way to make it act without approval, leak a secret, or accept a forged
request, we want to hear about it before anyone else does.

## Reporting a vulnerability

Email **security@shelliecom.com**. Please do not open a public issue for anything
that could be exploited.

Include what you did, what happened, and what you expected. A proof of concept
helps. We will acknowledge within three business days and tell you what we intend
to do about it.

This is a small team. We will not pretend to a response time we cannot meet, and
there is no bounty program.

## What we consider a vulnerability

- An irreversible action (charge, refund, cancellation) running without an
  explicit approval
- A forged webhook passing signature verification, or a signature check that
  passes when the shared secret is unset
- A public-trust channel obtaining a permission above `guest`
- A secret appearing in the audit log, the transcript, a distributed file, or an
  error message
- Tampering with the audit log that `audit.verify` fails to detect
- Retrieved documents or tool output overriding the system prompt, the permission
  check, or the confirmation gate

## What we do not consider a vulnerability

- Behaviour under `--auto-approve` or `FRONTDESK_AUTH_MODE=disabled`. Both are
  documented as development-only and both say so at the point of use.
- The bundled demo data being writable. It is demo data.
- Model output being wrong. Frontdesk constrains what an agent may *do*; it does
  not guarantee what a model will *say*.

## Supported versions

Only the latest release. This project does not backport fixes.
