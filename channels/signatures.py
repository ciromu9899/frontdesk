"""Webhook signature verification.

The first gate against a forged message driving real work. Weaken this and anyone
can manufacture a "please refund me" event.

The schemes:

- Slack: v0=HMAC-SHA256("v0:{timestamp}:{body}"). The timestamp is inside the
  signed material, so a captured request cannot simply be replayed.
- Meta and GitHub: X-Hub-Signature-256: sha256=HMAC-SHA256(body)
- Teams: Authorization: HMAC {base64(HMAC-SHA256(body))}, keyed with the secret
  Teams issues, which is itself base64 and has to be decoded to bytes first.

All three compare with hmac.compare_digest. Comparing with == leaks how many
leading bytes matched through timing, which is enough to recover a signature byte
by byte.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

# Replay window, matching Slack's own recommendation.
MAX_SKEW_SECONDS = 60 * 5


def _lower_headers(headers: dict) -> dict:
    """HTTP header names are case-insensitive; normalise so lookups cannot miss."""
    return {str(key).lower(): value for key, value in headers.items()}


def verify_slack(headers: dict, body: bytes, signing_secret: str,
                 now: float | None = None) -> bool:
    """Verify a Slack signature.

    The signed material is "v0:{timestamp}:{body}". Because the timestamp is
    covered by the signature, replaying an old request does not pass.
    """
    if not signing_secret:
        return False
    lowered = _lower_headers(headers)
    supplied = str(lowered.get("x-slack-signature", ""))
    timestamp = str(lowered.get("x-slack-request-timestamp", ""))
    if not supplied.startswith("v0=") or not timestamp:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - sent_at) > MAX_SKEW_SECONDS:
        return False   # replayed, or the clocks are badly out of step

    basestring = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied)


def verify_meta(headers: dict, body: bytes, app_secret: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 (Messenger and Instagram)."""
    if not app_secret:
        return False
    supplied = str(_lower_headers(headers).get("x-hub-signature-256", ""))
    if not supplied.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied)


def verify_github(headers: dict, body: bytes, webhook_secret: str) -> bool:
    """Verify GitHub's X-Hub-Signature-256 over the unchanged request body."""
    if not webhook_secret:
        return False
    supplied = str(_lower_headers(headers).get("x-hub-signature-256", ""))
    if not supplied.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied)


def verify_teams(headers: dict, body: bytes, security_token: str) -> bool:
    """Verify a Microsoft Teams outgoing webhook signature.

    Teams hands out the secret already base64 encoded, and signs with the decoded
    bytes. Using the printable form as the key produces a signature that never
    matches - the failure is total rather than subtle, which is the good case.

    The signature covers the body alone, with no timestamp, so this scheme cannot
    detect a replay by itself. webhooks.py keeps a short-lived record of activity
    ids for that reason.
    """
    if not security_token:
        return False
    try:
        key = base64.b64decode(security_token, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not key:
        return False

    supplied = str(_lower_headers(headers).get("authorization", ""))
    if not supplied.startswith("HMAC "):
        return False
    expected = "HMAC " + base64.b64encode(
        hmac.new(key, body, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, supplied)


def meta_verify_challenge(query: dict, verify_token: str) -> str | None:
    """Answer the GET challenge Meta sends when a subscription is registered.

    Returns hub.challenge only when hub.verify_token matches. On any mismatch it
    returns None and the caller must respond 403.
    """
    if not verify_token:
        return None
    mode = query.get("hub.mode")
    supplied = str(query.get("hub.verify_token", ""))
    if mode != "subscribe":
        return None
    if not hmac.compare_digest(supplied, verify_token):
        return None
    challenge = query.get("hub.challenge")
    return str(challenge) if challenge is not None else None
