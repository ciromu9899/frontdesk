"""Customer satisfaction: a rating that can be trusted enough to act on.

A rating link travels in an email or sits under a web conversation, so it is
signed. Without a signature anyone could post scores for conversations they were
never part of, and the number a business uses to decide whether the bot is
working would mean nothing.

The token carries the conversation it rates and an expiry. It carries no
customer identity, so a forwarded email leaks a rating opportunity and nothing
else.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time

import auth
import state

TTL_SECONDS = 14 * 24 * 3600
SCORES = (1, 2, 3, 4, 5)


class FeedbackError(RuntimeError):
    """A rating token was missing, malformed, expired, or not ours."""


def _secret() -> bytes:
    secret = os.environ.get("FRONTDESK_FEEDBACK_SECRET", "").strip()
    if not secret:
        # Falls back to the token signing key so a deployment does not need a
        # second secret to collect ratings. A separate one is still better: it
        # keeps a leaked rating link from being useful anywhere else.
        secret = os.environ.get("FRONTDESK_AUTH_SECRET", "").strip()
    if len(secret) < 32:
        raise FeedbackError(
            "Set FRONTDESK_FEEDBACK_SECRET (or FRONTDESK_AUTH_SECRET) to at "
            "least 32 characters before issuing rating links.")
    return secret.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def issue(tenant_id: str, conversation_key: str, *, session_id: str = "",
          channel: str = "", persona: str = "", now: float | None = None) -> str:
    payload = {
        "t": tenant_id,
        "c": conversation_key,
        "s": session_id,
        "ch": channel,
        "p": persona,
        "x": int((now or time.time()) + TTL_SECONDS),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify(token: str, now: float | None = None) -> dict:
    if not token or token.count(".") != 1:
        raise FeedbackError("Malformed rating token.")
    body, signature = token.split(".")
    expected = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise FeedbackError("Rating token signature does not match.")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise FeedbackError("Rating token payload is unreadable.") from exc
    if not isinstance(payload, dict) or not all(
            isinstance(payload.get(name), str) and payload.get(name)
            for name in ("t", "c")):
        raise FeedbackError("Rating token payload is incomplete.")
    if float(payload.get("x", 0)) < (now or time.time()):
        raise FeedbackError("This rating link has expired.")
    return payload


def submit(token: str, score: int, comment: str = "", now: float | None = None) -> dict:
    """Record a rating against the conversation the token names."""
    payload = verify(token, now=now)
    if int(score) not in SCORES:
        raise FeedbackError("Score must be 1 to 5.")
    result = state.record_csat(
        payload["t"], payload["c"], int(score), comment[:1000])
    audit_details = {"channel": payload.get("ch", ""), "score": int(score),
                     "has_comment": bool(comment.strip())}
    import audit
    audit.record("feedback.recorded", actor="customer",
                 tenant_id=payload["t"], session_id=payload.get("s", ""),
                 details=audit_details)
    return {"tenant_id": payload["t"], "conversation_key": payload["c"],
            "score": int(score), "updated": result["updated"]}


def link(base_url: str, token: str, score: int | None = None) -> str:
    """Build the URL a customer follows. One click per score where asked inline."""
    url = f"{base_url.rstrip('/')}/feedback?t={token}"
    return f"{url}&score={int(score)}" if score is not None else url


def invitation(base_url: str, token: str, lang: str = "en") -> str:
    """The line appended to a reply asking for the rating."""
    if lang == "es":
        return ("\n\n---\n¿Te resultó útil esta respuesta? "
                f"Puntúala del 1 al 5: {link(base_url, token)}&lang=es")
    return ("\n\n---\nWas this helpful? Rate it 1-5: "
            f"{link(base_url, token)}")


def _main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Issue or check a rating link")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--conversation", required=False)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--verify", help="check a token instead of issuing one")
    arguments = parser.parse_args()
    try:
        if arguments.verify:
            print(json.dumps(verify(arguments.verify), indent=2))
            return 0
        if not arguments.conversation:
            parser.error("--conversation is required unless --verify is used")
        token = issue(arguments.tenant, arguments.conversation, channel="manual")
        print(link(arguments.base_url, token))
    except FeedbackError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
