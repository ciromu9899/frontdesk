"""The endpoint the platforms actually post to.

    python webhooks.py --port 8770

Until this existed, the adapters in channels/ were complete and unreachable: the
setup instructions told you to register `https://<host>/slack` and nothing served
that path. This is that server.

    POST /github  GitHub Issues and Discussions
    POST /slack   Slack Events API
    POST /meta    Instagram DM and Messenger
    GET  /meta    Meta's subscription challenge
    POST /teams   Microsoft Teams outgoing webhook
    GET  /health  a liveness probe that reveals nothing
    /m/...        the approval screen for a phone, see mobile.py

Run it behind an HTTPS reverse proxy. It binds localhost by default.

## The two things that decide whether this is safe

**Verify before anything else.** A request whose signature does not check out is
answered 401 and never parsed. Not logged as an event, not handed to a channel,
not looked at. Every path through here that reaches the agent has passed
`channel.verify()` first.

**Answer quickly, then work.** Slack gives about three seconds before it treats
the delivery as failed and retries; a retry that arrives while the first is still
thinking gets the work done twice, and this agent cancels reservations. So Slack
and Meta are acknowledged immediately and processed on a worker thread, and every
delivery is checked against the ids already seen.

Teams is the exception in both respects: its outgoing webhook is synchronous, the
reply *is* the response body, so it is handled inline. That is a slower response,
and it is the only shape Teams offers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import approvals
import audit
import channels
import config as cfg
import mobile
import state
from channels.dispatch import Dispatcher

# A body larger than this is refused unread. Real events are a few kilobytes.
MAX_BODY_BYTES = 256_000

# How long a delivery id is remembered, and how many. A platform retries for
# minutes, not hours.
DEDUP_SECONDS = 10 * 60
DEDUP_MAX = 4096

_SEEN: "OrderedDict[str, float]" = OrderedDict()
_SEEN_LOCK = threading.Lock()


def already_handled(key: str, now: float | None = None,
                    tenant_id: str = "default") -> bool:
    """True when this delivery has been seen before, recording it if not.

    Slack and Meta both retry on anything other than a prompt 200, and a retry
    carries the same id. Without this, a slow first attempt turns one "cancel my
    booking" into two.
    """
    return state.already_seen(tenant_id, key, ttl=DEDUP_SECONDS, now=now)


def delivery_tenant(name: str, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "default"
    if name == "slack":
        return f"slack:{payload.get('team_id') or 'default'}"
    if name == "teams":
        tenant = ((payload.get("channelData") or {}).get("tenant") or {}).get("id")
        return f"teams:{tenant or 'default'}"
    if name == "meta":
        pages = sorted(str(entry.get("id")) for entry in payload.get("entry", [])
                       if entry.get("id"))
        return f"meta:{','.join(pages) or 'default'}"
    if name == "whatsapp":
        phone_ids = sorted(str(((change.get("value") or {}).get("metadata") or {}).get("phone_number_id"))
                           for entry in payload.get("entry", []) for change in entry.get("changes", [])
                           if ((change.get("value") or {}).get("metadata") or {}).get("phone_number_id"))
        return f"whatsapp:{','.join(phone_ids) or 'default'}"
    if name == "email":
        return str(payload.get("tenant_id", "default"))
    if name == "github":
        installation = payload.get("installation") or {}
        repository = payload.get("repository") or {}
        key = installation.get("id") or repository.get("id") or repository.get("full_name")
        return f"github:{key or 'default'}"
    return "default"


def reset_seen() -> None:
    """For tests and restarts."""
    with _SEEN_LOCK:
        _SEEN.clear()
    state.reset_deliveries()


def delivery_key(name: str, headers: dict, body: bytes) -> str:
    """A stable id for this delivery, so a retry can be recognised."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if name == "slack":
        # event_id is stable across Slack's retries; the retry number is not.
        return f"slack:{payload.get('event_id', '')}"
    if name == "teams":
        return f"teams:{payload.get('id', '')}"
    if name == "meta":
        # Meta has no delivery id, but each message carries one.
        ids = [
            str((event.get("message") or {}).get("mid", ""))
            for entry in payload.get("entry", [])
            for event in entry.get("messaging", [])
        ]
        joined = ",".join(sorted(identifier for identifier in ids if identifier))
        return f"meta:{joined}" if joined else ""
    if name == "whatsapp":
        ids = [str(message.get("id", "")) for entry in payload.get("entry", [])
               for change in entry.get("changes", [])
               for message in (change.get("value") or {}).get("messages", [])]
        joined = ",".join(sorted(identifier for identifier in ids if identifier))
        return f"whatsapp:{joined}" if joined else ""
    if name == "email":
        message_id = str(payload.get("message_id", ""))
        return f"email:{message_id}" if message_id else ""
    if name == "github":
        delivery = str(lowered.get("x-github-delivery", ""))
        return f"github:{delivery}" if delivery else ""
    return f"{name}:{lowered.get('x-request-id', '')}"


class WebhookHandler(mobile.MobileRoutes, BaseHTTPRequestHandler):
    server_version = "FrontdeskWebhooks/1"

    # Set by serve(); the tests build a handler class with these bound.
    dispatcher: Dispatcher | None = None
    registry: dict | None = None

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default logging; the audit log is the record that counts."""

    # -- plumbing ----------------------------------------------------------

    def _channels(self) -> dict:
        if self.registry is not None:
            return self.registry
        return channels.available()

    def _dispatcher(self) -> Dispatcher:
        if self.dispatcher is None:
            type(self).dispatcher = Dispatcher()
        return self.dispatcher

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            # There is no draining a body this size, so say so and hang up
            # rather than leaving the client writing into a closed pipe.
            self.close_connection = True
            self._text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large")
            return None
        return self.rfile.read(length)

    def _text(self, status: HTTPStatus, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if self.handle_mobile_get(parsed.path.rstrip("/") or "/", parsed.query):
            return
        if parsed.path == "/health":
            self._text(HTTPStatus.OK, "ok")
            return
        if parsed.path in {"/meta", "/whatsapp"}:
            self._meta_challenge(parsed.query, parsed.path.strip("/"))
            return
        self._text(HTTPStatus.NOT_FOUND, "not found")

    def _meta_challenge(self, query: str, name: str = "meta") -> None:
        """Meta proves it is talking to the right endpoint before subscribing."""
        channel = self._channels().get(name)
        parsed = {key: values[0] for key, values
                  in urllib.parse.parse_qs(query).items() if values}
        answer = channel.challenge(parsed) if channel else None
        if answer is None:
            audit.record("channel.challenge_rejected", actor=name,
                         details={"reason": "verify token did not match"})
            self._text(HTTPStatus.FORBIDDEN, "forbidden")
            return
        self._text(HTTPStatus.OK, answer)

    def do_POST(self) -> None:
        # Read the body before deciding anything. Answering a POST without
        # draining what the client is still sending makes the operating system
        # reset the connection, so the caller sees a transport error instead of
        # the 404 or 413 that would have told them what was wrong.
        body = self._body()
        if body is None:
            return

        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if self.handle_mobile_post(path, body):
            return

        name = path.strip("/").lower()
        channel = self._channels().get(name)
        if channel is None:
            self._text(HTTPStatus.NOT_FOUND, "not found")
            return

        # Nothing below this line runs on an unverified request.
        if not channel.verify(dict(self.headers), body):
            audit.record("channel.rejected", actor=name,
                         details={"reason": "signature did not verify",
                                  "bytes": len(body)})
            self._text(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return

        if name == "slack":
            handshake = _slack_handshake(body)
            if handshake is not None:
                self._json(HTTPStatus.OK, {"challenge": handshake})
                return

        if already_handled(delivery_key(name, dict(self.headers), body),
                           tenant_id=delivery_tenant(name, body)):
            audit.record("channel.duplicate", actor=name,
                         details={"note": "retry of a delivery already handled"})
            self._text(HTTPStatus.OK, "ok")
            return

        try:
            parser = getattr(channel, "parse_request", None)
            messages = parser(dict(self.headers), body) if parser else channel.parse(body)
        except channels.ChannelError as exc:
            audit.record("channel.unparsable", actor=name,
                         details={"reason": str(exc)})
            self._text(HTTPStatus.BAD_REQUEST, "bad request")
            return

        if name == "teams":
            self._teams_inline(channel, messages)
            return

        # Acknowledge first: the platform is counting seconds, and a retry would
        # run the work twice.
        self._text(HTTPStatus.OK, "ok")
        for message in messages:
            threading.Thread(target=self._answer, args=(channel, message),
                             daemon=True).start()

    def _teams_inline(self, channel, messages: list) -> None:
        """Teams expects the reply as the response body, so it is answered here."""
        if not messages:
            self._json(HTTPStatus.OK, {"type": "message", "text": ""})
            return
        try:
            reply = self._dispatcher().handle(messages[0])
        except Exception as exc:
            audit.record("channel.failed", actor="teams", details={"error": str(exc)})
            reply = "Something went wrong on our side. A teammate will follow up."
        self._json(HTTPStatus.OK, channel.reply_payload(reply))

    def _answer(self, channel, message) -> None:
        """Run the agent and send the reply. Already off the request thread."""
        try:
            reply = self._dispatcher().handle(message)
        except Exception as exc:
            audit.record("channel.failed", actor=message.channel,
                         details={"error": str(exc)})
            return
        if not reply:
            return
        try:
            sender = getattr(channel, "send_message", None)
            if sender:
                sender(message, reply)
            else:
                channel.send(message.thread_key, reply)
        except channels.ChannelError as exc:
            audit.record("channel.send_failed", actor=message.channel,
                         details={"error": str(exc)})


def _slack_handshake(body: bytes) -> str | None:
    """Slack's one-off url_verification, answered before anything else happens.

    It is signed like any other event, so it arrives here already verified.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        return str(challenge) if challenge else None
    return None


def serve(host: str = "127.0.0.1", port: int = 8770,
          persona: str | None = None, provider: str = "auto") -> int:
    registry = channels.available()
    live = {name: channel for name, channel in registry.items()
            if channel.configured()}
    if not live:
        print("No channel is configured, so this server would receive nothing.")
        print("Set up at least one - see docs/customer-guide.md#slack-and-meta:")
        for name in registry:
            print(f"  {name}")
        return 2

    WebhookHandler.registry = registry
    WebhookHandler.dispatcher = Dispatcher(persona=persona, provider=provider)

    print(f"listening : http://{host}:{port}")
    for name in sorted(live):
        print(f"  ready   : POST /{name}")
    for name in sorted(set(registry) - set(live)):
        print(f"  skipped : /{name} (not configured)")
    if approvals.enabled():
        print(f"  ready   : GET  /m   (approval screen)")
        print("            pair a phone with:  python webhooks.py --pair "
              "--subject you@example.com --roles operator")
    else:
        print("  skipped : /m   (set FRONTDESK_REMOTE_APPROVAL=1 to approve from a phone)")
    print("Put an HTTPS reverse proxy in front of this and register the public URLs.")

    server = ThreadingHTTPServer((host, port), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Frontdesk webhook receiver for GitHub and support channels")
    parser.add_argument("--port", type=int, default=8770, help="port to listen on")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind; keep this behind a reverse proxy")
    parser.add_argument("--persona", help="override the per-channel default persona")
    parser.add_argument("--provider", default="auto",
                        help="auto / anthropic / openai / ollama / echo")
    parser.add_argument("--pair", action="store_true",
                        help="print a one-time link for signing a phone in, then exit")
    parser.add_argument("--subject", help="who the phone will be signed in as")
    parser.add_argument("--roles", default="operator",
                        help="comma-separated roles for the paired phone")
    parser.add_argument("--base-url",
                        help="public origin of this server, for the pairing link")
    parser.add_argument("--hours", type=int, default=12,
                        help="how long the paired phone stays signed in")
    arguments = parser.parse_args()
    cfg.load_dotenv()

    if arguments.pair:
        return _pair(arguments)

    return serve(arguments.host, arguments.port,
                 persona=arguments.persona, provider=arguments.provider)


def _pair(arguments) -> int:
    """Print the link the operator opens on their phone."""
    secret = os.environ.get("FRONTDESK_AUTH_SECRET", "")
    if len(secret) < 32:
        print("FRONTDESK_AUTH_SECRET must be set (32+ characters) to pair a phone.")
        print("  python auth.py --new-secret")
        return 2
    if not arguments.subject:
        print("--pair needs --subject, so the audit log records who approved what.")
        return 2

    roles = tuple(part.strip() for part in arguments.roles.split(",") if part.strip())
    try:
        grant = mobile.issue_pairing(arguments.subject, roles, secret,
                                     hours=arguments.hours)
    except mobile.MobileError as exc:
        print(str(exc))
        return 2

    base = (arguments.base_url or f"http://{arguments.host}:{arguments.port}").rstrip("/")
    print(f"Open this on the phone, within {mobile.PAIR_TTL_SECONDS // 60} minutes:")
    print()
    print(f"  {base}/m/pair?t={grant}")
    print()
    print(f"It works once, and signs in as {arguments.subject} "
          f"[{', '.join(roles)}] for {arguments.hours}h.")
    if base.startswith("http://") and "127.0.0.1" not in base and "localhost" not in base:
        print()
        print("WARNING: that is a plain HTTP address. Serve this over HTTPS -")
        print("the link and the session cookie are both worth stealing.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
