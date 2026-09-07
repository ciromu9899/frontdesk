"""Authenticated localhost administration dashboard for Frontdesk."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import uuid
from pathlib import Path
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import audit
import auth
import channels
import handoffs
import rag
import state
import tools

MAX_KNOWLEDGE_UPLOAD = 12 * 1024 * 1024

SALON_FIELDS = (
    ("name", "Salon name", 160),
    ("hours", "Opening hours", 2000),
    ("services", "Services and prices", 6000),
    ("booking_url", "Booking website (optional)", 2000),
)


def salon_profile(tenant_id: str) -> dict:
    directory, _ = rag.tenant_paths(tenant_id)
    path = directory / ".salon-profile.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_salon_profile(tenant_id: str, values: dict) -> None:
    profile = {}
    for key, label, limit in SALON_FIELDS:
        value = values.get(key, "").strip()
        if (not value and key != "booking_url") or len(value) > limit:
            raise ValueError(f"Please enter {label.lower()} (up to {limit} characters).")
        profile[key] = value
    url = profile["booking_url"]
    parsed = urlparse(url)
    if url and (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or any(c.isspace() for c in url)):
        raise ValueError("Please use a complete https:// booking website address.")
    # Never use the optional shared-knowledge fallback for a tenant's setup.
    if tenant_id != "default" and not rag.multi_tenant_knowledge():
        raise ValueError("Enable tenant-isolated knowledge before saving salon details.")
    directory, index = rag.tenant_paths(tenant_id)
    directory.mkdir(parents=True, exist_ok=True)
    profile_path = directory / ".salon-profile.json"
    document = directory / "frontdesk-salon-profile.txt"
    text = "\n\n".join(f"{label}:\n{profile[key]}" for key, label, _ in SALON_FIELDS)
    text += "\n\nBooking policy: Direct customers to the booking website, if provided. "
    text += "This information does not provide live availability or confirm appointments. "
    text += "Ask staff about information that is not listed.\n"
    previous = {path: path.read_bytes() if path.exists() else None
                for path in (profile_path, document, index)}
    try:
        document.write_text(text, encoding="utf-8")
        rag.build_index(tenant_id=tenant_id)
        temporary = profile_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        temporary.replace(profile_path)
    except Exception:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise


def salon_setup_form(profile: dict, csrf: str, error: str = "") -> str:
    fields = []
    for key, label, limit in SALON_FIELDS:
        value = html.escape(profile.get(key, ""), quote=True)
        required = "required" if key != "booking_url" else ""
        if key in {"hours", "services"}:
            control = f'<textarea id="{key}" name="{key}" rows="4" maxlength="{limit}" {required}>{value}</textarea>'
        else:
            kind = "url" if key == "booking_url" else "text"
            control = f'<input id="{key}" name="{key}" type="{kind}" value="{value}" maxlength="{limit}" {required}>'
        fields.append(f'<label for="{key}">{label}</label>{control}')
    message = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    return ('<section class="card setup-card" style="max-width:720px;margin:auto;font-size:18px">'
            '<p class="eyebrow">YOUR FRONT DESK · SALON SETUP</p>'
            '<h1>Your salon, ready to answer</h1><p>Enter your details in any language. '
            'Include the currency with prices. Saving makes these details available to the chatbot.</p>'
            + message + '<form method="post" action="/setup" class="stack">'
            + f'<input type="hidden" name="csrf" value="{csrf}">'
            + "".join(fields) + '<button>Save salon details</button></form>'
            '<p>You can edit these details anytime. Your booking website stays in charge of appointments.</p></section>')


def _salon_enabled() -> bool:
    return bool({item.strip().lower() for item in
                 os.environ.get("FRONTDESK_INDUSTRY", "").split(",") if item.strip()}
                .intersection({"salon", "wellness"}))


def _csrf(token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), ("csrf:" + token).encode("utf-8"), hashlib.sha256).hexdigest()


def _page(title: str, body: str) -> bytes:
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Frontdesk</title>
<style>
:root{{--bg:#faf7f2;--panel:#fffefa;--ink:#302c33;--muted:#68606b;--brand:#68455f;--line:#ded6db;--ok:#28614e;--bad:#a03236}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 system-ui,sans-serif}}
header{{background:#493747;color:#fff;padding:20px 5vw}}header strong{{font-size:22px;letter-spacing:.03em}}main{{max-width:1180px;margin:32px auto;padding:0 20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:18px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:24px;box-shadow:0 6px 24px #49374708}}
.metric{{font-size:28px;font-weight:700}}.muted{{color:var(--muted)}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}code{{word-break:break-word}}
input,button{{font:inherit;min-height:48px;padding:12px 16px;border-radius:12px;border:1px solid #988994}}input{{width:100%;background:#fff}}button{{background:var(--brand);color:#fff;border:0;cursor:pointer;font-weight:650}}form.inline{{display:inline}}nav{{float:right}}nav a{{color:#fff;display:inline-flex;align-items:center;min-height:44px}}
.stack{{display:grid;gap:10px}}.badge{{display:inline-block;border-radius:999px;padding:4px 12px;background:#e8efea}}textarea,select{{font:inherit;width:100%;padding:12px;border:1px solid #988994;border-radius:12px;background:#fff;color:var(--ink)}}
a{{color:var(--brand);text-underline-offset:4px}}h1{{line-height:1.2;letter-spacing:-.025em;font-size:clamp(28px,4vw,40px)}}h2{{font-size:22px;line-height:1.3}}.eyebrow{{font-size:12px;font-weight:750;letter-spacing:.16em;color:var(--brand)}}.setup-card{{border-top:5px solid var(--brand)}}.setup-card label{{font-weight:650;margin-top:12px}}.setup-card button{{margin-top:20px}}:focus-visible{{outline:3px solid #28614e;outline-offset:4px}}button:hover{{background:#503349}}button:disabled{{opacity:.6;cursor:wait}}input,textarea{{scroll-margin:24px}}[role=alert]{{border-left:4px solid var(--bad);padding:12px;background:#fbeeee}}@media(max-width:600px){{main{{padding:0 12px;margin:20px auto}}.card{{padding:20px 16px}}header{{padding:12px 16px}}.grid{{grid-template-columns:1fr}}.setup-card button{{width:100%}}}}
</style></head><body><header><strong>FrontDesk</strong><nav><a href="/logout">Sign out</a></nav></header><main>{body}</main></body></html>"""
    return document.encode("utf-8")


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "FrontdeskAdmin/1.0"

    @property
    def secret(self) -> str:
        return self.server.auth_secret  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _secure(self) -> str:
        forced = os.environ.get("FRONTDESK_SECURE_COOKIES", "").strip().lower()
        if forced in {"1", "true", "yes", "on"}:
            return "; Secure"
        host = self.headers.get("Host", "").split(":", 1)[0]
        return "" if host in {"localhost", "127.0.0.1", "::1"} else "; Secure"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8", headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 32_768:
            raise ValueError("Request body is too large.")
        values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {key: items[-1] for key, items in values.items()}

    def _multipart(self) -> tuple[dict[str, str], str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
        if not match:
            raise ValueError("Missing multipart boundary.")
        boundary = (match.group(1) or match.group(2)).encode("ascii", "strict")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_KNOWLEDGE_UPLOAD:
            raise ValueError("Upload is too large.")
        fields: dict[str, str] = {}; filename = ""; document = b""
        for part in self.rfile.read(length).split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            headers, content = part.split(b"\r\n\r\n", 1)
            if content.endswith(b"\r\n"):
                content = content[:-2]
            disposition = headers.decode("latin-1", "replace")
            name_match = re.search(r'name="([^"]+)"', disposition)
            if not name_match:
                continue
            name = name_match.group(1)
            file_match = re.search(r'filename="([^"]*)"', disposition)
            if name == "document" and file_match:
                filename = Path(file_match.group(1).replace("\\", "/")).name
                document = content
            elif len(content) <= 4096:
                fields[name] = content.decode("utf-8", "strict")
        if not filename or not document:
            raise ValueError("A document is required.")
        return fields, filename, document

    def _token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return authorization[7:]
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie.get("frontdesk_access").value if cookie.get("frontdesk_access") else ""

    def _principal(self, permission: str = "*") -> tuple[auth.Principal, str] | None:
        token = self._token()
        try:
            principal = auth.authenticate_token(token, self.secret)
            auth.require(principal, permission)
            return principal, token
        except auth.AuthError:
            return None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/login":
            body = """<section class="card" style="max-width:480px;margin:70px auto"><h1>Administrator sign in</h1>
<p class="muted">Paste a signed Frontdesk access token with the admin role.</p>
<form method="post" action="/login"><label>Access token<input name="token" type="password" autocomplete="off" required></label><p><button>Sign in</button></p></form></section>"""
            self._send(HTTPStatus.OK, _page("Sign in", body))
            return
        if path == "/logout":
            self._send(HTTPStatus.SEE_OTHER, b"", headers={
                "Location": "/login",
                "Set-Cookie": (
                    "frontdesk_access=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    + self._secure()
                ),
            })
            return

        authenticated = self._principal("*")
        if not authenticated:
            self._send(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/login"})
            return
        principal, token = authenticated

        if path == "/setup":
            if not _salon_enabled():
                self._send(HTTPStatus.NOT_FOUND, b"Not found")
                return
            self._send(HTTPStatus.OK, _page("Salon setup", salon_setup_form(
                salon_profile(principal.tenant_id), _csrf(token, self.secret))))
            return

        if path == "/api/status":
            valid, count, chain = audit.verify()
            payload = {
                "principal": principal.subject,
                "rag": rag.index_status(tenant_id=principal.tenant_id),
                "audit": {"valid": valid, "events": count, "head": chain},
                "handoffs_open": len(handoffs.list_tickets(
                    tenant_id=principal.tenant_id, limit=1_000)),
                "backend_configured": bool(os.environ.get("FRONTDESK_BACKEND_URL")),
            }
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(HTTPStatus.OK, content, "application/json; charset=utf-8")
            return
        if path == "/api/inbox":
            payload = {"threads": state.list_threads(principal.tenant_id),
                       "handoffs": handoffs.list_tickets(status=None,
                                                          tenant_id=principal.tenant_id)}
            self._send(HTTPStatus.OK, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/analytics":
            self._send(HTTPStatus.OK,
                       json.dumps(state.analytics(principal.tenant_id), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/appointments":
            if not _salon_enabled():
                self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<h1>Not found</h1>"))
                return
            reservations = tools.load_store(principal.tenant_id).get("reservations", {})
            payload = {"appointments": [
                {"reservation_id": reservation_id, **reservation}
                for reservation_id, reservation in reservations.items()
                if reservation.get("service_id")
            ]}
            self._send(HTTPStatus.OK, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/messages":
            key = parse_qs(urlparse(self.path).query).get("thread", [""])[-1]
            payload = {"messages": state.list_messages(principal.tenant_id, key)}
            self._send(HTTPStatus.OK, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path != "/":
            self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<h1>Not found</h1>"))
            return

        status = rag.index_status(tenant_id=principal.tenant_id)
        valid, event_count, chain = audit.verify()
        events = list(reversed(audit.read_events(50)))
        csrf_value = _csrf(token, self.secret)
        handoff_count = len(handoffs.list_tickets(
            tenant_id=principal.tenant_id, limit=1_000))
        open_handoffs = handoffs.list_tickets(
            tenant_id=principal.tenant_id, limit=50)
        analytics = state.analytics(principal.tenant_id)
        threads = state.list_threads(principal.tenant_id, limit=100)
        rows = "".join(
            "<tr>" +
            f"<td>{html.escape(str(event.get('timestamp', '')))}</td>" +
            f"<td>{html.escape(str(event.get('event_type', '')))}</td>" +
            f"<td>{html.escape(str(event.get('actor', '')))}</td>" +
            f"<td><code>{html.escape(json.dumps(event.get('details', {}), ensure_ascii=False))}</code></td></tr>"
            for event in events
        ) or "<tr><td colspan=" + '"4"' + ">No events yet.</td></tr>"
        handoff_rows = "".join(
            "<tr>" +
            f"<td>{html.escape(str(ticket.get('timestamp', '')))}</td>" +
            f"<td><code>{html.escape(str(ticket.get('id', '')))}</code></td>" +
            f"<td>{html.escape(str(ticket.get('channel', '') or 'local'))}</td>" +
            f"<td>{html.escape(str(ticket.get('summary', '')))}</td>" +
            f"<td>{html.escape(str(ticket.get('assignee', '') or 'Unassigned'))}</td>" +
            "<td><div class=\"stack\"><form method=\"post\" action=\"/handoffs/update\">" +
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_value}\">" +
            f"<input type=\"hidden\" name=\"id\" value=\"{html.escape(str(ticket.get('id', '')))}\">" +
            "<input type=\"hidden\" name=\"action\" value=\"assigned\"><input name=\"assignee\" aria-label=\"Assign to\" placeholder=\"agent@example.com\" required><button>Assign</button></form>" +
            "<form method=\"post\" action=\"/handoffs/update\">" +
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_value}\">" +
            f"<input type=\"hidden\" name=\"id\" value=\"{html.escape(str(ticket.get('id', '')))}\">" +
            "<input type=\"hidden\" name=\"action\" value=\"started\"><button>Take over</button></form>" +
            "<form method=\"post\" action=\"/handoffs/update\">" +
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_value}\">" +
            f"<input type=\"hidden\" name=\"id\" value=\"{html.escape(str(ticket.get('id', '')))}\">" +
            "<input type=\"hidden\" name=\"action\" value=\"note\"><input name=\"note\" aria-label=\"Internal note\" placeholder=\"Internal note\"><button>Add note</button></form>" +
            "<form method=\"post\" action=\"/handoffs/resolve\">" +
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_value}\">" +
            f"<input type=\"hidden\" name=\"id\" value=\"{html.escape(str(ticket.get('id', '')))}\">" +
            "<input name=\"note\" aria-label=\"Resolution note\" placeholder=\"Resolution note\">" +
            "<button>Resolve</button></form></div></td></tr>"
            for ticket in open_handoffs
        ) or '<tr><td colspan="6">No open handoffs.</td></tr>'
        thread_rows = "".join(
            "<tr>" + f"<td>{html.escape(str(thread.get('channel', '')))}</td>" +
            f"<td><code>{html.escape(str(thread.get('conversation_key', '')))}</code></td>" +
            f"<td><span class=\"badge\">{html.escape(str(thread.get('status', '')))}</span></td>" +
            f"<td>{html.escape(str(thread.get('assignee', '') or 'Unassigned'))}</td>" +
            f"<td>{html.escape(str(thread.get('subject', '')))}</td>" +
            f"<td><div class=\"stack\"><a href=\"/api/messages?thread={quote(str(thread.get('conversation_key', '')))}\">JSON transcript</a>" +
            "<form method=\"post\" action=\"/inbox/reply\">" +
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_value}\">" +
            f"<input type=\"hidden\" name=\"thread\" value=\"{html.escape(str(thread.get('conversation_key', '')))}\">" +
            "<textarea name=\"reply\" maxlength=\"4000\" aria-label=\"Reply to customer\" required></textarea><button>Reply as human</button></form></div></td></tr>"
            for thread in threads
        ) or '<tr><td colspan="6">No conversations yet.</td></tr>'
        salon_section = ""
        if _salon_enabled():
            appointments = [
                {"reservation_id": reservation_id, **reservation}
                for reservation_id, reservation in
                tools.load_store(principal.tenant_id).get("reservations", {}).items()
                if reservation.get("service_id") and reservation.get("status") != "cancelled"
            ]
            appointments.sort(key=lambda item: (str(item.get("date", "")),
                                                str(item.get("time", ""))))
            appointment_rows = "".join(
                "<tr>" +
                f"<td>{html.escape(str(item.get('date', '')))} {html.escape(str(item.get('time', '')))}</td>" +
                f"<td>{html.escape(str(item.get('customer', '')))}</td>" +
                f"<td>{html.escape(str(item.get('service', item.get('service_id', ''))))}</td>" +
                f"<td>{html.escape(str(item.get('staff', item.get('staff_id', ''))))}</td>" +
                f"<td>{html.escape(str(item.get('location', item.get('location_id', ''))))}</td>" +
                f"<td><span class=\"badge\">{html.escape(str(item.get('reminder_status', 'disabled')))}</span></td>" +
                "</tr>"
                for item in appointments[:100]
            ) or '<tr><td colspan="6">No upcoming salon appointments.</td></tr>'
            salon_section = (
                '<section class="card" style="margin-top:18px"><h2>Upcoming salon appointments</h2>'
                '<p class="muted">Responsive staff view. Times are shown in each appointment location timezone.</p>'
                '<div style="overflow:auto"><table><thead><tr><th>Date and time</th><th>Customer</th>'
                '<th>Service</th><th>Stylist</th><th>Location</th><th>Reminder</th></tr></thead>'
                f'<tbody>{appointment_rows}</tbody></table></div></section>'
            )
        valid_class = "ok" if valid else "bad"
        body = f"""
<h1>Operations overview</h1><p class="muted">Signed in as {html.escape(principal.subject)} · tenant {html.escape(principal.tenant_id)}</p>
{'<section class="card"><h2>Salon setup</h2><p><a href="/setup">Set up or edit your salon in one form</a></p></section>' if _salon_enabled() else ''}
<section class="grid">
  <article class="card"><div class="muted">Knowledge files</div><div class="metric">{status['files']}</div><div>{status['chunks']} indexed chunks</div></article>
  <article class="card"><div class="muted">Audit chain</div><div class="metric {valid_class}">{'Valid' if valid else 'Invalid'}</div><div>{event_count} events · head {html.escape(chain[:12])}</div></article>
  <article class="card"><div class="muted">Open handoffs</div><div class="metric">{handoff_count}</div><div>Awaiting a human teammate</div></article>
  <article class="card"><div class="muted">CSAT</div><div class="metric">{analytics['csat_average'] or '—'}</div><div>{analytics['csat_responses']} responses</div></article>
  <article class="card"><div class="muted">Average AI response</div><div class="metric">{analytics['average_assistant_seconds'] or '—'}</div><div>seconds across completed replies</div></article>
  <article class="card"><div class="muted">Resolution rate</div><div class="metric">{round(analytics['resolution_rate'] * 100, 1)}%</div><div>{analytics['human_replies']} human replies</div></article>
  <article class="card"><div class="muted">Live backend</div><div class="metric">{'Configured' if os.environ.get('FRONTDESK_BACKEND_URL') else 'Local demo'}</div><div>No credential values are displayed</div></article>
</section>
<section class="card" style="margin-top:18px"><h2>Knowledge management</h2><p>Upload and index tenant-isolated PDF, Word, PowerPoint, Excel, Markdown, text, and HTML documents.</p>
<form method="post" action="/knowledge/upload" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{csrf_value}"><label>Knowledge document<input type="file" name="document" accept=".pdf,.docx,.pptx,.xlsx,.md,.txt,.html,.htm" required></label><p><button>Upload and index</button></p></form>
<form method="post" action="/knowledge/reindex" class="inline"><input type="hidden" name="csrf" value="{csrf_value}"><button>Rebuild index</button></form></section>
{salon_section}
<section class="card" style="margin-top:18px"><h2>Shared inbox</h2><div style="overflow:auto"><table><thead><tr><th>Channel</th><th>Conversation</th><th>Status</th><th>Assignee</th><th>Subject</th><th>Transcript</th></tr></thead><tbody>{thread_rows}</tbody></table></div></section>
<section class="card" style="margin-top:18px"><h2>Human handoffs</h2><div style="overflow:auto"><table><thead><tr><th>UTC time</th><th>ID</th><th>Channel</th><th>Summary</th><th>Assignee</th><th>Action</th></tr></thead><tbody>{handoff_rows}</tbody></table></div></section>
<section class="card" style="margin-top:18px"><h2>Recent audit events</h2><div style="overflow:auto"><table><thead><tr><th>UTC time</th><th>Event</th><th>Actor</th><th>Details</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
        self._send(HTTPStatus.OK, _page("Dashboard", body))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/knowledge/upload":
            authenticated = self._principal("*")
            if not authenticated:
                self._send(HTTPStatus.UNAUTHORIZED, _page("Unauthorized", "<h1>Unauthorized</h1>")); return
            principal, token = authenticated
            try:
                fields, filename, document = self._multipart()
            except (ValueError, UnicodeDecodeError):
                self._send(HTTPStatus.BAD_REQUEST, _page("Bad request", "<h1>Invalid upload</h1>")); return
            if not hmac.compare_digest(fields.get("csrf", ""), _csrf(token, self.secret)):
                self._send(HTTPStatus.FORBIDDEN, _page("Forbidden", "<h1>CSRF validation failed</h1>")); return
            safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")
            suffix = Path(safe_name).suffix.lower()
            if not safe_name or suffix not in rag.ALLOWED_SUFFIXES:
                self._send(HTTPStatus.BAD_REQUEST, _page("Bad request", "<h1>Unsupported document type</h1>")); return
            directory, _ = rag.tenant_paths(principal.tenant_id)
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / safe_name
            if destination.exists():
                destination = directory / f"{destination.stem}-{uuid.uuid4().hex[:8]}{suffix}"
            temporary = directory / f".upload-{uuid.uuid4().hex}{suffix}"
            try:
                temporary.write_bytes(document)
                rag._plain_text(temporary)
                temporary.replace(destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                self._send(HTTPStatus.BAD_REQUEST, _page(
                    "Bad request", "<h1>The document could not be safely extracted</h1>")); return
            result = rag.build_index(tenant_id=principal.tenant_id)
            audit.record("knowledge.uploaded", actor=principal.subject,
                         tenant_id=principal.tenant_id,
                         details={"filename": destination.name, **result})
            self._send(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/"}); return
        try:
            form = self._form()
        except (ValueError, UnicodeDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, _page("Bad request", "<h1>Bad request</h1>"))
            return
        if path == "/login":
            token = form.get("token", "")
            try:
                principal = auth.authenticate_token(token, self.secret)
                auth.require(principal, "*")
            except auth.AuthError as exc:
                audit.record("admin.login_failed", actor="anonymous", details={"reason": str(exc)})
                self._send(HTTPStatus.UNAUTHORIZED, _page("Sign in failed", "<h1>Sign in failed</h1><p>The token is invalid, expired, or lacks the admin role.</p><p><a href=\"/login\">Try again</a></p>"))
                return
            audit.record("admin.login_succeeded", actor=principal.subject, tenant_id=principal.tenant_id)
            self._send(HTTPStatus.SEE_OTHER, b"", headers={
                "Location": "/setup" if _salon_enabled() and not salon_profile(principal.tenant_id) else "/",
                "Set-Cookie": (
                    f"frontdesk_access={token}; Path=/; HttpOnly; SameSite=Strict"
                    + self._secure()
                ),
            })
            return

        authenticated = self._principal("*")
        if not authenticated:
            self._send(HTTPStatus.UNAUTHORIZED, _page("Unauthorized", "<h1>Unauthorized</h1>"))
            return
        principal, token = authenticated
        if not hmac.compare_digest(form.get("csrf", ""), _csrf(token, self.secret)):
            self._send(HTTPStatus.FORBIDDEN, _page("Forbidden", "<h1>CSRF validation failed</h1>"))
            return
        if path == "/setup" and _salon_enabled():
            try:
                save_salon_profile(principal.tenant_id, form)
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, _page("Check salon details", salon_setup_form(
                    form, _csrf(token, self.secret), str(exc))))
                return
            except OSError:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, _page("Save failed", salon_setup_form(
                    form, _csrf(token, self.secret), "Could not save. Please try again; your previous details were kept.")))
                return
            self._send(HTTPStatus.OK, _page("Salon details saved", '<section class="card"><h1>Salon details saved</h1>'
                '<p>Your hours, services and booking information are ready for the chatbot. '
                'Try asking about your opening hours and prices in customer chat before sharing it.</p>'
                '<p><a href="/setup">Edit details</a> · <a href="/">Open shared inbox</a></p></section>'))
            return
        if path == "/knowledge/reindex":
            result = rag.build_index(tenant_id=principal.tenant_id)
            audit.record("knowledge.reindexed", actor=principal.subject, tenant_id=principal.tenant_id, details=result)
            self._send(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/"})
            return
        if path == "/handoffs/resolve":
            resolved = handoffs.resolve(
                form.get("id", ""), resolved_by=principal.subject,
                tenant_id=principal.tenant_id, note=form.get("note", ""),
            )
            if not resolved:
                self._send(HTTPStatus.NOT_FOUND, _page(
                    "Not found", "<h1>Open handoff not found</h1>"))
                return
            self._send(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/"})
            return
        if path == "/handoffs/update":
            try:
                updated = handoffs.update(
                    form.get("id", ""), form.get("action", ""), actor=principal.subject,
                    tenant_id=principal.tenant_id, assignee=form.get("assignee", ""),
                    note=form.get("note", ""))
            except handoffs.HandoffError as exc:
                self._send(HTTPStatus.BAD_REQUEST, _page("Bad request", f"<h1>{html.escape(str(exc))}</h1>")); return
            if not updated:
                self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<h1>Handoff not found</h1>")); return
            self._send(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/"})
            return
        if path == "/inbox/reply":
            conversation_key = form.get("thread", "")
            reply = form.get("reply", "").strip()[:4000]
            thread = state.get_thread(principal.tenant_id, conversation_key)
            if thread is None or not reply:
                self._send(HTTPStatus.BAD_REQUEST, _page("Bad request", "<h1>Conversation and reply are required</h1>")); return
            channel_name = str(thread.get("channel") or "web")
            if channel_name != "web":
                channel = channels.available().get(channel_name)
                if channel is None or not channel.configured():
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, _page("Unavailable", "<h1>The channel is not configured</h1>")); return
                external_key = conversation_key[len(channel_name) + 1:] if conversation_key.startswith(channel_name + ":") else conversation_key
                try:
                    channel.send(external_key, reply)
                except channels.ChannelError as exc:
                    self._send(HTTPStatus.BAD_GATEWAY, _page("Delivery failed", f"<h1>{html.escape(str(exc))}</h1>")); return
            state.append_message(principal.tenant_id, conversation_key, "agent", reply,
                                 sender_id=principal.subject, channel=channel_name)
            state.upsert_thread(principal.tenant_id, conversation_key,
                                status_value="in_progress", assignee=principal.subject)
            state.record_metric(principal.tenant_id, "human_reply", conversation_key=conversation_key,
                                dimensions={"channel": channel_name})
            audit.record("inbox.human_reply", actor=principal.subject,
                         tenant_id=principal.tenant_id,
                         details={"conversation_key": conversation_key, "channel": channel_name})
            self._send(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/"})
            return
        self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<h1>Not found</h1>"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Frontdesk administration dashboard")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        secret = auth.signing_secret()
    except auth.AuthError as exc:
        print(f"Cannot start admin server: {exc}")
        return 1
    server = ThreadingHTTPServer((args.host, args.port), AdminHandler)
    server.auth_secret = secret  # type: ignore[attr-defined]
    print(f"Frontdesk Admin: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
