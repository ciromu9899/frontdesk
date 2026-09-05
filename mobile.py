"""The approval screen, built for a phone.

Mounted by webhooks.py on the same process, because a parked approval has a
thread waiting on it and a thread cannot be resumed from somewhere else.

    GET  /m               the screen
    GET  /m/pair?t=...    one-time sign-in from a link printed on the console
    GET  /m/api/pending   what is waiting
    POST /m/api/decide    approve or decline one action
    POST /m/signout       drop the cookie

## Getting signed in on a phone

An access token is 180 characters. Nobody types that on a phone, and nobody
should be pasting it into a browser either. So the operator runs

    python webhooks.py --pair --subject you@example.com --roles operator,finance

which prints a link, valid for ten minutes and usable once. Opening it on the
phone exchanges the link for a session cookie holding a freshly issued token.
The link itself never contains the token.

The grant is signed with FRONTDESK_AUTH_SECRET, carries an expiry and a unique
id, and that id is burned when it is used - a link captured from shoulder-surfing
or a screenshot is worth nothing once the phone has opened it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie

import approvals
import audit
import auth

COOKIE = "frontdesk_mobile"

# Long enough to walk to the phone, short enough that a stale link is useless.
PAIR_TTL_SECONDS = 10 * 60

# Domain separation, so a pairing grant cannot be replayed as anything else.
PAIR_CONTEXT = b"frontdesk-mobile-pairing-v1"

_USED_PAIRINGS: set[str] = set()
_USED_LOCK = threading.Lock()


class MobileError(RuntimeError):
    """The pairing link is not usable."""


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64encode(decoded) != value:
        raise MobileError("The link is malformed.")
    return decoded


def issue_pairing(subject: str, roles: tuple[str, ...], secret: str,
                  hours: int = 12, now: int | None = None) -> str:
    """A one-time grant. It names who you will be, not a token."""
    if not subject or not roles:
        raise MobileError("A pairing link needs a subject and at least one role.")
    unknown = [role for role in roles if role not in auth.ROLE_PERMISSIONS]
    if unknown:
        raise MobileError(f"Unknown role(s): {', '.join(unknown)}")
    issued = int(time.time() if now is None else now)
    claims = {
        "exp": issued + PAIR_TTL_SECONDS,
        "hours": max(1, min(int(hours), 24 * 7)),
        "jti": uuid.uuid4().hex,
        "roles": list(roles),
        "sub": subject,
        "v": 1,
    }
    payload = _b64encode(json.dumps(
        claims, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    signature = _b64encode(hmac.new(
        secret.encode("utf-8"), PAIR_CONTEXT + payload.encode("ascii"),
        hashlib.sha256).digest())
    return f"{payload}.{signature}"


def redeem_pairing(grant: str, secret: str, now: int | None = None) -> str:
    """Verify a grant, burn it, and return a freshly issued access token."""
    if not isinstance(grant, str) or len(grant) > 4096 or grant.count(".") != 1:
        raise MobileError("The link is malformed.")
    payload, supplied = grant.split(".", 1)
    try:
        signed = payload.encode("ascii")
    except UnicodeEncodeError:
        raise MobileError("The link is malformed.") from None
    expected = _b64encode(hmac.new(
        secret.encode("utf-8"), PAIR_CONTEXT + signed, hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        raise MobileError("That link was not issued by this system.")

    try:
        claims = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise MobileError("The link is malformed.") from None
    if not isinstance(claims, dict) or claims.get("v") != 1:
        raise MobileError("The link is malformed.")
    current = int(time.time() if now is None else now)
    if not isinstance(claims.get("exp"), int) or claims["exp"] < current:
        raise MobileError("That link has expired. Generate a new one.")

    identifier = str(claims.get("jti", ""))
    with _USED_LOCK:
        if not identifier or identifier in _USED_PAIRINGS:
            raise MobileError("That link has already been used.")
        _USED_PAIRINGS.add(identifier)

    roles = tuple(str(role) for role in claims.get("roles", []))
    principal = auth.Principal(subject=str(claims.get("sub", "")), roles=roles)
    audit.record("mobile.paired", actor=principal.subject,
                 details={"roles": list(roles)})
    return auth.issue_token(principal, secret,
                            expires_in=int(claims.get("hours", 12)) * 3600)


def reset_pairings() -> None:
    """For tests and restarts."""
    with _USED_LOCK:
        _USED_PAIRINGS.clear()


def _csrf(token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), b"mobile-csrf:" + token.encode("utf-8"),
                    hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#0f2b28">
<title>Frontdesk approvals</title>
<style>
  :root {
    --paper:#f6f4f0; --card:#ffffff; --ink:#12211e; --muted:#5f6f6a;
    --edge:#dcd6cb; --brass:#8a6a2f; --brass-ink:#ffffff;
    --teal:#0f4c47; --danger:#8c3a2f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper:#0d1917; --card:#152522; --ink:#eaf0ee; --muted:#9aaba6;
      --edge:#263a36; --brass:#c9a45c; --brass-ink:#1a1206;
      --teal:#7fd8cd; --danger:#e08376;
    }
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    margin:0; background:var(--paper); color:var(--ink);
    font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding:env(safe-area-inset-top) env(safe-area-inset-right)
            env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  header {
    position:sticky; top:0; z-index:2; background:var(--paper);
    border-bottom:1px solid var(--edge);
    padding:.9rem 1rem; display:flex; align-items:baseline; gap:.6rem;
  }
  h1 { font-size:1.05rem; margin:0; letter-spacing:.01em; }
  .count { color:var(--muted); font-size:.85rem; margin-left:auto; }
  main { padding:1rem; max-width:34rem; margin:0 auto; }
  .card {
    background:var(--card); border:1px solid var(--edge); border-radius:14px;
    padding:1rem; margin-bottom:.9rem;
  }
  .what { font-size:1.12rem; font-weight:600; line-height:1.35; }
  .meta { color:var(--muted); font-size:.83rem; margin-top:.5rem; }
  .meta b { color:var(--ink); font-weight:600; }
  .row { display:flex; gap:.6rem; margin-top:1rem; }
  button {
    flex:1; min-height:52px; border-radius:11px; font-size:1rem; font-weight:600;
    border:1px solid var(--edge); background:transparent; color:var(--ink);
    font-family:inherit; cursor:pointer;
  }
  button.go { background:var(--brass); color:var(--brass-ink); border-color:var(--brass); }
  button:disabled { opacity:.45; }
  .empty { text-align:center; color:var(--muted); padding:3rem 1rem; }
  .empty .big { font-size:1.05rem; color:var(--ink); margin-bottom:.35rem; }
  .note { font-size:.85rem; color:var(--muted); margin-top:.4rem; }
  .note.bad { color:var(--danger); }
  footer { padding:1.5rem 1rem 2.5rem; text-align:center; }
  footer button { max-width:12rem; min-height:40px; font-weight:400; font-size:.9rem; }
  .gone { opacity:.5; }
</style></head>
<body>
<header><h1>Approvals</h1><span class="count" id="count"></span></header>
<main id="list"></main>
<footer><button id="out">Sign out</button></footer>
<script>
const CSRF = "__CSRF__";
const list = document.getElementById("list");
const count = document.getElementById("count");
let busy = new Set();
let shown = "";

function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

function render(items) {
  // Rebuilding the list on every poll would move a button under a finger
  // mid-tap, on actions that cannot be undone. So the DOM is only rewritten
  // when what it should show has actually changed. The countdown is coarse for
  // the same reason: a value that ticks every second would rebuild every second.
  var signature = items.map(function (item) {
    return item.id + ":" + Math.round(item.expires_in / 60);
  }).join("|");
  if (signature === shown) return;
  shown = signature;

  count.textContent = items.length ? plural(items.length, "waiting", "waiting") : "";
  if (!items.length) {
    list.innerHTML = '<div class="empty"><div class="big">Nothing waiting</div>' +
      'Actions that cannot be undone will appear here.</div>';
    return;
  }
  list.innerHTML = items.map(function (item) {
    var mins = Math.max(1, Math.round(item.expires_in / 60));
    return '<div class="card" data-id="' + item.id + '">' +
      '<div class="what"></div>' +
      '<div class="meta">asked by <b class="who"></b>' +
      (item.channel ? ' via <b class="ch"></b>' : '') +
      ' &middot; expires in ' + mins + 'm</div>' +
      '<div class="row">' +
      '<button data-act="no">Decline</button>' +
      '<button data-act="yes" class="go">Approve</button>' +
      '</div><div class="note"></div></div>';
  }).join("");
  // Text is set as text, never as markup: the summary contains customer data.
  items.forEach(function (item) {
    var card = list.querySelector('[data-id="' + item.id + '"]');
    card.querySelector(".what").textContent = item.summary;
    card.querySelector(".who").textContent = item.requested_by;
    var ch = card.querySelector(".ch");
    if (ch) ch.textContent = item.channel;
  });
}

async function poll() {
  if (busy.size) return;
  try {
    const response = await fetch("/m/api/pending", { credentials: "same-origin" });
    if (response.status === 401) { location.href = "/m"; return; }
    render(await response.json());
  } catch (error) { /* offline; the next tick tries again */ }
}

list.addEventListener("click", async function (event) {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const card = button.closest(".card");
  const id = card.dataset.id;
  const approve = button.dataset.act === "yes";
  const note = card.querySelector(".note");
  busy.add(id);
  card.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
  note.className = "note";
  note.textContent = approve ? "Approving..." : "Declining...";
  try {
    const response = await fetch("/m/api/decide", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF": CSRF },
      body: JSON.stringify({ id: id, approve: approve })
    });
    const result = await response.json();
    if (result.ok) {
      note.textContent = approve ? "Approved." : "Declined. Nothing ran.";
      card.classList.add("gone");
      setTimeout(function () { shown = ""; busy.delete(id); poll(); }, 1200);
      return;
    }
    note.className = "note bad";
    note.textContent = result.error || "That did not work.";
  } catch (error) {
    note.className = "note bad";
    note.textContent = "No connection. Nothing was sent.";
  }
  card.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
  busy.delete(id);
});

document.getElementById("out").addEventListener("click", async function () {
  await fetch("/m/signout", { method: "POST", credentials: "same-origin",
                              headers: { "X-CSRF": CSRF } });
  location.href = "/m";
});

poll();
setInterval(poll, 3000);
</script>
</body></html>
"""

SIGNED_OUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Frontdesk approvals</title>
<style>
  :root { --paper:#f6f4f0; --ink:#12211e; --muted:#5f6f6a; --edge:#dcd6cb; }
  @media (prefers-color-scheme: dark) {
    :root { --paper:#0d1917; --ink:#eaf0ee; --muted:#9aaba6; --edge:#263a36; }
  }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--paper); color:var(--ink);
         font:16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:26rem; margin:1.5rem; padding:2rem 1.5rem;
         border:1px solid var(--edge); border-radius:14px; }
  h1 { font-size:1.15rem; margin:0 0 .75rem; }
  p { margin:0 0 .6rem; }
  code { font-size:.82rem; word-break:break-all; color:var(--muted); }
</style></head>
<body><main>
<h1>__TITLE__</h1>
<p>__BODY__</p>
<p><code>python webhooks.py --pair --subject you@example.com --roles operator</code></p>
</main></body></html>
"""


def _signed_out_page(title: str, body: str) -> bytes:
    import html
    return (SIGNED_OUT
            .replace("__TITLE__", html.escape(title))
            .replace("__BODY__", html.escape(body))).encode("utf-8")


class MobileRoutes:
    """The /m routes, mixed into the webhook handler.

    Kept apart from webhooks.py because these are the only routes here that a
    person looks at, and the only ones that need a cookie, a CSRF token and a
    same-origin check.
    """

    def _secret(self) -> str:
        return os.environ.get("FRONTDESK_AUTH_SECRET", "")

    def _cookie_token(self) -> str:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        entry = jar.get(COOKIE)
        return entry.value if entry else ""

    def _approver(self) -> auth.Principal | None:
        secret = self._secret()
        token = self._cookie_token()
        if not secret or not token:
            return None
        try:
            return auth.authenticate_token(token, secret)
        except auth.AuthError:
            return None

    def _secure_cookie(self) -> str:
        """Secure is set unless this is plainly a local test over HTTP."""
        host = str(self.headers.get("Host", "")).split(":")[0]
        local = host in {"127.0.0.1", "localhost", "::1"}
        return "" if local else "; Secure"

    def handle_mobile_get(self, path: str, query: str) -> bool:
        """Returns True when the path belonged to this screen."""
        if path == "/m":
            self._screen()
            return True
        if path == "/m/pair":
            self._pair(query)
            return True
        if path == "/m/api/pending":
            self._pending()
            return True
        return False

    def handle_mobile_post(self, path: str, body: bytes) -> bool:
        if path == "/m/api/decide":
            self._decide(body)
            return True
        if path == "/m/signout":
            self._signout()
            return True
        return False

    # -- screens -----------------------------------------------------------

    def _screen(self) -> None:
        principal = self._approver()
        if principal is None:
            self._html(HTTPStatus.OK, _signed_out_page(
                "Not signed in",
                "Open a pairing link on this phone. Generate one on the machine "
                "running Frontdesk:"))
            return
        page = PAGE.replace("__CSRF__", _csrf(self._cookie_token(), self._secret()))
        self._html(HTTPStatus.OK, page.encode("utf-8"))

    def _pair(self, query: str) -> None:
        parsed = {key: values[0] for key, values
                  in urllib.parse.parse_qs(query).items() if values}
        try:
            token = redeem_pairing(parsed.get("t", ""), self._secret())
        except (MobileError, auth.AuthError) as exc:
            audit.record("mobile.pair_failed", actor="mobile",
                         details={"reason": str(exc)})
            self._html(HTTPStatus.BAD_REQUEST,
                       _signed_out_page("That link did not work", str(exc)))
            return
        # 303 so a refresh does not replay a link that is now spent.
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/m")
        self.send_header(
            "Set-Cookie",
            f"{COOKIE}={token}; Path=/m; HttpOnly; SameSite=Strict"
            f"{self._secure_cookie()}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _signout(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie",
                         f"{COOKIE}=; Path=/m; HttpOnly; SameSite=Strict; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- api ---------------------------------------------------------------

    def _pending(self) -> None:
        principal = self._approver()
        if principal is None:
            self._json_status(HTTPStatus.UNAUTHORIZED, {"error": "not signed in"})
            return
        self._json_status(HTTPStatus.OK, approvals.pending())

    def _decide(self, body: bytes) -> None:
        principal = self._approver()
        if principal is None:
            self._json_status(HTTPStatus.UNAUTHORIZED, {"error": "not signed in"})
            return
        supplied = str(self.headers.get("X-CSRF", ""))
        if not hmac.compare_digest(
                supplied, _csrf(self._cookie_token(), self._secret())):
            self._json_status(HTTPStatus.FORBIDDEN, {"error": "stale page; reload"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json_status(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            return
        if not isinstance(payload, dict):
            self._json_status(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            return

        took, why = approvals.decide(
            str(payload.get("id", "")), bool(payload.get("approve")), principal)
        self._json_status(HTTPStatus.OK,
                          {"ok": took} if took else {"ok": False, "error": why})

    # -- plumbing ----------------------------------------------------------

    def _html(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json_status(self, status: HTTPStatus, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
