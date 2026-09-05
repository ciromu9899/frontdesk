"""Email as a channel: IMAP in, SMTP out, on the standard library alone.

## Why a poller rather than a webhook

The other channels are pushed to `webhooks.py` by a platform that signs its
deliveries. Email has no such platform. A provider's inbound-parse webhook would
mean a third-party account in the path of every customer message, which is
exactly what a self-hosted deployment is avoiding. So this fetches from the
mailbox the business already owns.

## What email cannot prove

A `From` header is a claim. Nothing here verifies it, so email is a **public**
channel: the sender gets `guest`, and an order lookup or a booking change is
refused at the permission layer like any other unverified sender. Treating a
matching email address as identity would make impersonation trivial.

## Not replying to the wrong thing

Two bots replying to each other never stops, so a reply is only sent to a human
message: automatic replies, list traffic, bounces and no-reply addresses are
read and dropped. Replies carry `Auto-Submitted: auto-replied` so the other side
extends the same courtesy.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import html
import imaplib
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage

import audit
import state
from channels.base import PUBLIC, InboundMessage

NAME = "email"
MAX_BODY_CHARS = 16_000
DEDUP_SECONDS = 7 * 24 * 3600
FETCH_LIMIT = 25

_TAG = re.compile(r"<[^>]+>")
_QUOTED = re.compile(
    r"^\s*(?:>.*|On .+ wrote:|-{2,}\s*Original Message\s*-{2,}.*|_{5,}.*)$",
    re.IGNORECASE)
_NOREPLY = re.compile(r"(^|[.\-_])(no-?reply|do-?not-?reply|mailer-daemon|postmaster|"
                      r"bounce|notification)s?($|[.\-_@])", re.IGNORECASE)


class EmailConfigError(RuntimeError):
    """The mailbox is not configured well enough to run."""


@dataclass(frozen=True)
class MailboxMessage:
    uid: bytes
    message: email.message.Message


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def configured() -> bool:
    return bool(_env("FRONTDESK_EMAIL_IMAP_HOST") and _env("FRONTDESK_EMAIL_ADDRESS"))


def _require() -> dict:
    missing = [n for n in ("FRONTDESK_EMAIL_IMAP_HOST", "FRONTDESK_EMAIL_USER",
                           "FRONTDESK_EMAIL_PASSWORD", "FRONTDESK_EMAIL_ADDRESS")
               if not _env(n)]
    if missing:
        raise EmailConfigError("Set " + ", ".join(missing))
    return {
        "imap_host": _env("FRONTDESK_EMAIL_IMAP_HOST"),
        "imap_port": int(_env("FRONTDESK_EMAIL_IMAP_PORT", "993")),
        "smtp_host": _env("FRONTDESK_EMAIL_SMTP_HOST") or _env("FRONTDESK_EMAIL_IMAP_HOST"),
        "smtp_port": int(_env("FRONTDESK_EMAIL_SMTP_PORT", "587")),
        "user": _env("FRONTDESK_EMAIL_USER"),
        "password": _env("FRONTDESK_EMAIL_PASSWORD"),
        "address": _env("FRONTDESK_EMAIL_ADDRESS"),
        "folder": _env("FRONTDESK_EMAIL_FOLDER", "INBOX"),
        "tenant_id": _env("FRONTDESK_EMAIL_TENANT_ID", "default"),
        "starttls": _env("FRONTDESK_EMAIL_SMTP_STARTTLS", "1") not in {"0", "false", "no"},
    }


def _plain_text(message: email.message.Message) -> str:
    """The readable body, preferring text/plain and never executing anything."""
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_filename():          # an attachment is not a question
                continue
            if part.get_content_type() == "text/plain":
                body = part.get_content()
                break
            if part.get_content_type() == "text/html" and not body:
                body = _TAG.sub(" ", part.get_content())
    else:
        body = message.get_content()
        if message.get_content_type() == "text/html":
            body = _TAG.sub(" ", body)
    body = html.unescape(body or "")
    kept = [line for line in body.splitlines() if not _QUOTED.match(line)]
    return "\n".join(kept).strip()[:MAX_BODY_CHARS]


def is_automated(message: email.message.Message, our_address: str) -> bool:
    """Would replying to this start a loop, or answer a machine?"""
    auto = (message.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    precedence = (message.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "list", "junk", "auto_reply"}:
        return True
    if any(message.get(header) for header in
           ("List-Id", "List-Unsubscribe", "List-Post", "X-Autoreply",
            "X-Autorespond", "X-Failed-Recipients")):
        return True
    sender = email.utils.parseaddr(message.get("From", ""))[1].lower()
    if not sender or _NOREPLY.search(sender):
        return True
    return sender == our_address.strip().lower()


def thread_key_for(message: email.message.Message) -> str:
    """The conversation this message belongs to.

    The first id in References is the thread's root, so a long exchange keeps
    one session instead of starting a new one on every reply.
    """
    references = (message.get("References") or "").split()
    if references:
        return references[0].strip("<>")
    parent = (message.get("In-Reply-To") or "").strip()
    if parent:
        return parent.strip("<>")
    return (message.get("Message-ID") or "").strip("<>")


def to_inbound(message: email.message.Message, tenant_id: str = "default") -> InboundMessage:
    sender = email.utils.parseaddr(message.get("From", ""))[1]
    subject = (message.get("Subject") or "").strip()
    body = _plain_text(message)
    text = f"{subject}\n\n{body}".strip() if subject else body
    return InboundMessage(
        channel=NAME,
        external_user_id=sender.lower(),
        thread_key=thread_key_for(message),
        text=text,
        trust=PUBLIC,
        display_name=email.utils.parseaddr(message.get("From", ""))[0],
        raw={"message_id": (message.get("Message-ID") or "").strip("<>"),
             "subject": subject,
             "references": (message.get("References") or "").strip()},
        tenant_id=tenant_id,
    )


def send_reply(settings: dict, inbound: email.message.Message, text: str) -> None:
    """Reply to the sender, in the same thread, marked as automatic."""
    to_address = email.utils.parseaddr(inbound.get("From", ""))[1]
    if not to_address:
        raise EmailConfigError("Inbound message has no usable From address.")
    reply = EmailMessage()
    reply["From"] = settings["address"]
    # Deliberately the From address, never Reply-To: honouring an attacker's
    # Reply-To would turn this mailbox into a way to send mail to a stranger.
    reply["To"] = to_address
    subject = (inbound.get("Subject") or "").strip()
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    parent = (inbound.get("Message-ID") or "").strip()
    if parent:
        reply["In-Reply-To"] = parent
        references = (inbound.get("References") or "").strip()
        reply["References"] = f"{references} {parent}".strip()
    reply["Auto-Submitted"] = "auto-replied"
    reply["X-Auto-Response-Suppress"] = "All"
    reply.set_content(text)

    if settings["smtp_port"] == 465:
        server = smtplib.SMTP_SSL(settings["smtp_host"], settings["smtp_port"], timeout=30)
    else:
        server = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"], timeout=30)
    with server:
        if settings["smtp_port"] != 465 and settings["starttls"]:
            server.starttls()
        server.login(settings["user"], settings["password"])
        server.send_message(reply)


def fetch_unseen(settings: dict, limit: int = FETCH_LIMIT) -> list[MailboxMessage]:
    connection = imaplib.IMAP4_SSL(settings["imap_host"], settings["imap_port"])
    try:
        connection.login(settings["user"], settings["password"])
        connection.select(settings["folder"])
        status, data = connection.uid("search", None, "UNSEEN")
        if status != "OK":
            return []
        identifiers = data[0].split()[:limit]
        messages = []
        for identifier in identifiers:
            status, payload = connection.uid("fetch", identifier, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            messages.append(MailboxMessage(
                identifier,
                email.message_from_bytes(payload[0][1], policy=email.policy.default)))
        return messages
    finally:
        try:
            connection.logout()
        except OSError:
            pass


def mark_seen(settings: dict, uid: bytes) -> None:
    """Acknowledge one IMAP message only after it no longer needs a retry."""
    if not uid:
        return
    connection = imaplib.IMAP4_SSL(settings["imap_host"], settings["imap_port"])
    try:
        connection.login(settings["user"], settings["password"])
        connection.select(settings["folder"])
        status, _ = connection.uid("store", uid, "+FLAGS", "(\\Seen)")
        if status != "OK":
            raise EmailConfigError("The mailbox did not acknowledge the message.")
    finally:
        try:
            connection.logout()
        except OSError:
            pass


def poll_once(dispatcher, settings: dict | None = None,
              base_url: str = "", send: bool = True) -> dict:
    """Fetch, answer and reply once. Returns what happened, for the runner."""
    settings = settings or _require()
    counts = {"fetched": 0, "answered": 0, "dry_run": 0, "held_for_human": 0,
              "skipped_automated": 0, "skipped_duplicate": 0, "failed": 0}
    for fetched in fetch_unseen(settings):
        uid = fetched.uid if isinstance(fetched, MailboxMessage) else b""
        message = fetched.message if isinstance(fetched, MailboxMessage) else fetched
        counts["fetched"] += 1
        if is_automated(message, settings["address"]):
            counts["skipped_automated"] += 1
            audit.record("channel.rejected", actor="email",
                         tenant_id=settings["tenant_id"],
                         details={"channel": NAME, "reason": "automated"})
            mark_seen(settings, uid)
            continue
        message_id = (message.get("Message-ID") or "").strip("<>")
        delivery_key = f"email:{message_id}" if message_id else ""
        if delivery_key and state.delivery_seen(
                settings["tenant_id"], delivery_key, ttl=DEDUP_SECONDS):
            counts["skipped_duplicate"] += 1
            audit.record("channel.duplicate", actor="email",
                         tenant_id=settings["tenant_id"],
                         details={"channel": NAME})
            mark_seen(settings, uid)
            continue
        inbound = to_inbound(message, settings["tenant_id"])
        if not inbound.text.strip():
            counts["skipped_automated"] += 1
            mark_seen(settings, uid)
            continue
        try:
            reply = dispatcher.handle(inbound)
            if not reply:
                counts["held_for_human"] += 1
                if delivery_key:
                    state.mark_delivery(settings["tenant_id"], delivery_key)
                mark_seen(settings, uid)
                continue
            if base_url:
                import feedback
                token = feedback.issue(inbound.tenant_id,
                                       f"{NAME}:{inbound.thread_key}",
                                       channel=NAME)
                reply += feedback.invitation(base_url, token)
            if not send:
                counts["dry_run"] += 1
                continue
            send_reply(settings, message, reply)
            if delivery_key:
                state.mark_delivery(settings["tenant_id"], delivery_key)
            mark_seen(settings, uid)
            counts["answered"] += 1
        except Exception as exc:                     # noqa: BLE001 - one bad mail must not stop the poll
            counts["failed"] += 1
            audit.record("channel.failed", actor="email",
                         tenant_id=settings["tenant_id"],
                         details={"channel": NAME, "error": type(exc).__name__})
    return counts


def _main() -> int:
    parser = argparse.ArgumentParser(description="Answer email from the shared mailbox")
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls")
    parser.add_argument("--persona", help="override the persona for email")
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--base-url", default=os.environ.get("FRONTDESK_PUBLIC_URL", ""),
                        help="where the rating link points; omit to send no rating link")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and answer, but send nothing")
    arguments = parser.parse_args()

    from channels.dispatch import Dispatcher
    try:
        settings = _require()
    except EmailConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    dispatcher = Dispatcher(persona=arguments.persona or _env("FRONTDESK_EMAIL_PERSONA")
                            or None, provider=arguments.provider)
    print(f"Email channel on {settings['address']} "
          f"({settings['folder']}), tenant {settings['tenant_id']}"
          + (" - dry run" if arguments.dry_run else ""))
    while True:
        try:
            counts = poll_once(dispatcher, settings, base_url=arguments.base_url,
                               send=not arguments.dry_run)
            if counts["fetched"]:
                print(" ".join(f"{key}={value}" for key, value in counts.items()))
        except (imaplib.IMAP4.error, OSError) as exc:
            print(f"mailbox unavailable: {type(exc).__name__}", file=sys.stderr)
        if arguments.once:
            return 0
        time.sleep(max(10, arguments.interval))


if __name__ == "__main__":
    raise SystemExit(_main())
