"""The page a person lands on after signing in with LinkedIn.

    python linkedin_verify.py --port 8790

It does exactly one thing: complete the OpenID Connect exchange, work out what
that established, and remember it against the conversation the sign-in came from.
Then it tells the person to go back to where they were.

Run it behind an HTTPS reverse proxy on the origin registered as
FRONTDESK_LINKEDIN_REDIRECT_URI. LinkedIn compares that URI exactly.

Nothing here is a session: no cookie is set, no password is seen, and the access
token is used once and discarded. What survives is a record that this person on
this channel proved a verified email - see channels/identity.py for how long.
"""

from __future__ import annotations

import argparse
import html
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import audit
import config as cfg
import tools
from channels import identity, linkedin

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark;
           --ink: #10241f; --paper: #f7f5f1; --edge: #d8d2c8; --note: #5d6b66; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink: #eef2f0; --paper: #10201c; --edge: #2b3d38; --note: #9fb0aa; }}
  }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
          background: var(--paper); color: var(--ink);
          font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 30rem; padding: 2.5rem 2rem; border: 1px solid var(--edge);
          border-radius: 14px; margin: 1.5rem; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .75rem; }}
  p {{ margin: 0 0 .75rem; }}
  .note {{ color: var(--note); font-size: .875rem; }}
</style></head>
<body><main>
<h1>{heading}</h1>
{body}
</main></body></html>
"""


def _page(title: str, heading: str, paragraphs: list[str]) -> bytes:
    body = "".join(f"<p>{html.escape(text)}</p>" for text in paragraphs[:1])
    body += "".join(f'<p class="note">{html.escape(text)}</p>'
                    for text in paragraphs[1:])
    return PAGE.format(title=html.escape(title), heading=html.escape(heading),
                       body=body).encode("utf-8")


class VerifyHandler(BaseHTTPRequestHandler):
    server_version = "FrontdeskLinkedIn/1"

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default logging; the audit log is the record that counts."""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/linkedin/callback"):
            self._send(HTTPStatus.NOT_FOUND,
                       _page("Not found", "Not found", ["There is nothing here."]))
            return

        query = urllib.parse.parse_qs(parsed.query)
        first = {key: values[0] for key, values in query.items() if values}

        # LinkedIn reports a refusal by redirecting here with an error, which is
        # an ordinary outcome and not a failure to report as one.
        if first.get("error"):
            self._send(HTTPStatus.OK, _page(
                "Sign-in cancelled", "Sign-in cancelled",
                ["Nothing was shared and nothing has changed.",
                 "You can close this tab and go back to the conversation."]))
            return

        try:
            claims = linkedin.verify_state(first.get("state", ""))
            token = linkedin.exchange_code(first.get("code", ""))
            userinfo = linkedin.fetch_userinfo(token)
        except linkedin.LinkedInError as exc:
            audit.record("identity.failed", actor="linkedin:unknown",
                         details={"reason": str(exc)})
            self._send(HTTPStatus.BAD_REQUEST, _page(
                "Sign-in failed", "That didn't work",
                [str(exc),
                 "Go back to the conversation and ask for a new link."]))
            return

        tenant_id = str(claims.get("tid", "default"))
        customer_lookup = lambda email: tools.find_customer_by_email(email, tenant_id)
        principal = linkedin.principal_for(
            userinfo, tenant_id=tenant_id, customer_lookup=customer_lookup)
        trust = linkedin.trust_for(
            userinfo, customer_lookup=customer_lookup)
        name = linkedin.display_name(userinfo)

        if principal.roles == ("guest",):
            # Verified, but not as anyone this deployment knows. Saying so plainly
            # beats implying the sign-in failed, because it did not.
            audit.record("identity.unmatched", actor=principal.subject,
                         details={"channel": claims["ch"], "trust": trust})
            self._send(HTTPStatus.OK, _page(
                "Signed in", f"Thanks, {name}",
                ["LinkedIn confirmed your email, but it doesn't match an account "
                 "here, so I still can't open your order.",
                 "Go back to the conversation and a teammate will take it from "
                 "there."]))
            return

        identity.remember(
            claims["ch"], claims["uid"],
            subject=principal.subject,
            email=principal.subject.partition(":")[2],
            trust=trust, name=name,
            tenant_id=tenant_id,
        )
        self._send(HTTPStatus.OK, _page(
            "Signed in", f"Thanks, {name}",
            ["You're verified. Go back to the conversation and ask again - I can "
             "help now.",
             "This verification expires on its own. You can close this tab."]))

    def _send(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="LinkedIn sign-in callback for Frontdesk")
    parser.add_argument("--port", type=int, default=8790, help="port to listen on")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind; keep this behind a reverse proxy")
    arguments = parser.parse_args()

    cfg.load_dotenv()

    try:
        linkedin.require_configured()
    except linkedin.LinkedInError as exc:
        print(str(exc))
        return 2

    dropped = identity.purge_expired()
    if dropped:
        print(f"dropped {dropped} expired verification(s)")

    print(f"callback  : {linkedin.redirect_uri()}")
    print(f"listening : http://{arguments.host}:{arguments.port}/linkedin/callback")
    print("Put an HTTPS reverse proxy in front of this on the registered origin.")
    server = ThreadingHTTPServer((arguments.host, arguments.port), VerifyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
