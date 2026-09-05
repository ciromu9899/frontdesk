"""LinkedIn, as an identity channel rather than a message channel.

## Why this is not a Slack-shaped adapter

LinkedIn does not give third-party applications a webhook for inbound direct
messages. Messaging access sits behind partner programmes that a self-serve app
cannot enter, so the "receive a DM, let the agent answer" shape that slack.py and
meta.py implement cannot be built here. Writing it anyway would produce code that
never receives anything.

What LinkedIn does offer any application, self-serve, is **Sign In with LinkedIn
using OpenID Connect**: `openid profile email`. That turns out to be worth more to
Frontdesk than another inbox, because it answers the question the whole channel
design is organised around.

## The problem it solves

On a public channel the sender is a handle, so they get `guest` and every
customer-specific request is refused (see base.py). That is correct, and it is
also where the conversation stops. Frontdesk had no way for the person to say
"fine - here is who I am".

Signing in with LinkedIn is that way. The person proves control of an account
with an email LinkedIn has verified, and the tier they land in is decided by what
that actually established:

    verified email at one of our own domains   -> workspace
    verified email matching a customer record  -> authenticated
    anything else                              -> public, unchanged

Note the shape of that list: **configuration never sets the tier.** The tier is a
statement about what was verified, and only evidence moves it.

## What is deliberately not done here

The ID token is not verified locally. It is read from the token endpoint's
response over TLS by a client that authenticated with its own secret, which OIDC
Core section 3.1.3.7 explicitly allows; the claims are then taken from
`/v2/userinfo`. The alternative - hand-rolling RS256 and JWKS handling in a
project with no dependencies - would mean writing signature verification from
scratch, and signature verification done wrong is more dangerous than an honest
round trip to the issuer.

Environment:
    FRONTDESK_LINKEDIN_CLIENT_ID          from the LinkedIn app
    FRONTDESK_LINKEDIN_CLIENT_SECRET      from the LinkedIn app
    FRONTDESK_LINKEDIN_REDIRECT_URI       must match the app's registered URI exactly
    FRONTDESK_LINKEDIN_STATE_SECRET       32+ characters; signs the state parameter
    FRONTDESK_LINKEDIN_WORKSPACE_DOMAINS  comma-separated email domains that count
                                          as our own organisation (optional)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import auth
from channels.base import AUTHENTICATED, PUBLIC, WORKSPACE, ChannelError, roles_for

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
# OAuth endpoint, not a credential.
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"  # nosec B105
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

SCOPES = "openid profile email"

# A sign-in link is handed to someone mid-conversation; it should outlive a short
# distraction and nothing more.
STATE_TTL_SECONDS = 15 * 60

# Domain separation, so a state token can never be replayed as a checkout token.
STATE_CONTEXT = b"frontdesk-linkedin-state-v1"

HTTP_TIMEOUT = 15
MAX_RESPONSE_BYTES = 256_000


class LinkedInError(ChannelError):
    """The LinkedIn configuration is wrong, or a sign-in failed verification."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _client_id() -> str:
    value = os.environ.get("FRONTDESK_LINKEDIN_CLIENT_ID", "").strip()
    if not value:
        raise LinkedInError("FRONTDESK_LINKEDIN_CLIENT_ID is not set.")
    return value


def _client_secret() -> str:
    value = os.environ.get("FRONTDESK_LINKEDIN_CLIENT_SECRET", "").strip()
    if not value:
        raise LinkedInError("FRONTDESK_LINKEDIN_CLIENT_SECRET is not set.")
    return value


def _state_secret() -> bytes:
    value = os.environ.get("FRONTDESK_LINKEDIN_STATE_SECRET", "")
    if len(value) < 32:
        raise LinkedInError(
            "FRONTDESK_LINKEDIN_STATE_SECRET must contain at least 32 characters.")
    return value.encode("utf-8")


def redirect_uri() -> str:
    """The callback URL, which must match the one registered with LinkedIn exactly.

    LinkedIn compares it character for character, so it is read from configuration
    rather than reconstructed from the request - a proxy rewriting the Host header
    would otherwise produce a URI that silently stops matching.
    """
    value = os.environ.get("FRONTDESK_LINKEDIN_REDIRECT_URI", "").strip()
    if not value:
        raise LinkedInError("FRONTDESK_LINKEDIN_REDIRECT_URI is not set.")
    parsed = urllib.parse.urlparse(value)
    local = parsed.hostname in {"127.0.0.1", "localhost"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise LinkedInError(
            "FRONTDESK_LINKEDIN_REDIRECT_URI must use HTTPS outside local testing.")
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise LinkedInError(
            "FRONTDESK_LINKEDIN_REDIRECT_URI must not carry a query or fragment.")
    return value


def workspace_domains() -> tuple[str, ...]:
    raw = os.environ.get("FRONTDESK_LINKEDIN_WORKSPACE_DOMAINS", "")
    return tuple(
        part.strip().lower().lstrip("@")
        for part in raw.split(",")
        if part.strip()
    )


def configured() -> bool:
    """True when a sign-in link can actually be produced."""
    return all(os.environ.get(name, "").strip() for name in (
        "FRONTDESK_LINKEDIN_CLIENT_ID",
        "FRONTDESK_LINKEDIN_CLIENT_SECRET",
        "FRONTDESK_LINKEDIN_REDIRECT_URI",
    )) and len(os.environ.get("FRONTDESK_LINKEDIN_STATE_SECRET", "")) >= 32


def require_configured() -> None:
    """Refuse a production-facing service when required LinkedIn OIDC is absent."""
    missing = [name for name in (
        "FRONTDESK_LINKEDIN_CLIENT_ID",
        "FRONTDESK_LINKEDIN_CLIENT_SECRET",
        "FRONTDESK_LINKEDIN_REDIRECT_URI",
    ) if not os.environ.get(name, "").strip()]
    if len(os.environ.get("FRONTDESK_LINKEDIN_STATE_SECRET", "")) < 32:
        missing.append("FRONTDESK_LINKEDIN_STATE_SECRET (32+ characters)")
    if missing:
        raise LinkedInError(
            "LinkedIn sign-in is required. Set " + ", ".join(missing) + ".")
    redirect_uri()


# --------------------------------------------------------------------------
# The state parameter
# --------------------------------------------------------------------------


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64encode(decoded) != value:
        raise LinkedInError("State is not in canonical form.")
    return decoded


def issue_state(channel: str, external_user_id: str, thread_key: str,
                now: int | None = None, tenant_id: str = "default") -> str:
    """Sign a state parameter that names the conversation this sign-in belongs to.

    Two attacks close here. A bare random state stops cross-site request forgery
    but nothing else: an attacker who completes their own sign-in and then feeds
    the resulting callback URL to a victim would attach their identity to the
    victim's thread. Binding the conversation into the signed state means a
    callback can only ever apply to the conversation that started it.
    """
    if not channel or not external_user_id or not thread_key:
        raise LinkedInError("A state token needs a channel, a user and a thread.")
    issued_at = int(time.time() if now is None else now)
    claims = {
        "ch": channel,
        "exp": issued_at + STATE_TTL_SECONDS,
        "thread": thread_key,
        "tid": tenant_id,
        "uid": external_user_id,
        "v": 1,
    }
    payload = _b64encode(json.dumps(
        claims, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    signature = _b64encode(hmac.new(
        _state_secret(), STATE_CONTEXT + payload.encode("ascii"), hashlib.sha256
    ).digest())
    return f"{payload}.{signature}"


def verify_state(state: str, now: int | None = None) -> dict:
    """Verify a state parameter and return its claims."""
    if not isinstance(state, str) or len(state) > 4096 or state.count(".") != 1:
        raise LinkedInError("Invalid sign-in state.")
    payload, supplied = state.split(".", 1)
    try:
        signed = payload.encode("ascii")
    except UnicodeEncodeError:
        raise LinkedInError("Invalid sign-in state encoding.") from None
    expected = _b64encode(hmac.new(
        _state_secret(), STATE_CONTEXT + signed, hashlib.sha256
    ).digest())
    if not hmac.compare_digest(supplied, expected):
        raise LinkedInError("Invalid sign-in state signature.")
    try:
        claims = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise LinkedInError("Invalid sign-in state payload.") from None
    if not isinstance(claims, dict) or claims.get("v") != 1:
        raise LinkedInError("Invalid sign-in state claims.")
    if not isinstance(claims.get("exp"), int):
        raise LinkedInError("Invalid sign-in state expiry.")
    current = int(time.time() if now is None else now)
    if claims["exp"] < current:
        raise LinkedInError("This sign-in link has expired.")
    if claims["exp"] > current + STATE_TTL_SECONDS + 60:
        raise LinkedInError("Invalid sign-in state expiry.")
    for field in ("ch", "thread", "uid"):
        if not isinstance(claims.get(field), str) or not claims[field]:
            raise LinkedInError("Invalid sign-in state claims.")
    return claims


# --------------------------------------------------------------------------
# The OpenID Connect flow
# --------------------------------------------------------------------------


def authorization_url(channel: str, external_user_id: str, thread_key: str,
                      now: int | None = None, tenant_id: str = "default") -> str:
    """The link the person follows to sign in."""
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(),
        "state": issue_state(channel, external_user_id, thread_key, now=now,
                             tenant_id=tenant_id),
        "scope": SCOPES,
    })
    return f"{AUTHORIZE_URL}?{query}"


def _post_form(url: str, fields: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Frontdesk/1.0"},
        method="POST",
    )
    return _read_json(request)


def _get_json(url: str, access_token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json",
                 "Authorization": f"Bearer {access_token}",
                 "User-Agent": "Frontdesk/1.0"},
        method="GET",
    )
    return _read_json(request)


def _read_json(request: urllib.request.Request) -> dict:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # LinkedIn puts a machine-readable reason in the body; it is useful in a
        # log and useless to an end user, so it does not travel any further.
        raise LinkedInError(f"LinkedIn returned HTTP {exc.code}.") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise LinkedInError(
            f"Could not reach LinkedIn: {getattr(exc, 'reason', exc)}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LinkedInError("LinkedIn response was unreasonably large.")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LinkedInError("LinkedIn returned invalid JSON.") from None
    if not isinstance(decoded, dict):
        raise LinkedInError("LinkedIn returned an unexpected shape.")
    return decoded


def exchange_code(code: str) -> str:
    """Trade the authorization code for an access token."""
    if not code or len(code) > 4096:
        raise LinkedInError("Invalid authorization code.")
    payload = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri(),
    })
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise LinkedInError("LinkedIn did not return an access token.")
    return token


def fetch_userinfo(access_token: str) -> dict:
    """Read the OIDC claims for the person who just signed in."""
    claims = _get_json(USERINFO_URL, access_token)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise LinkedInError("LinkedIn did not return a subject claim.")
    return claims


# --------------------------------------------------------------------------
# What the sign-in established
# --------------------------------------------------------------------------


def _email_of(claims: dict) -> str:
    """The verified email, or "" when LinkedIn did not confirm one.

    An unverified email is a typed-in string. Treating it as identity would make
    the whole flow theatre, so it is discarded rather than downgraded.
    """
    if claims.get("email_verified") not in (True, "true"):
        return ""
    email = claims.get("email")
    if not isinstance(email, str) or email.count("@") != 1:
        return ""
    return email.strip().lower()


def trust_for(claims: dict, customer_lookup=None) -> str:
    """Decide the tier from what the sign-in actually proved.

    customer_lookup, when supplied, takes an email and returns a customer record
    or None. That is the only thing that can establish "this is the customer", so
    without it a verified LinkedIn identity stops at the workspace tier at best.
    """
    email = _email_of(claims)
    if not email:
        return PUBLIC

    domain = email.rpartition("@")[2]
    if domain and domain in workspace_domains():
        return WORKSPACE

    if customer_lookup is not None:
        try:
            if customer_lookup(email):
                return AUTHENTICATED
        except Exception:
            # A backend that is down must not hand out a tier by accident.
            return PUBLIC
    return PUBLIC


def principal_for(claims: dict, tenant_id: str = "default",
                  customer_lookup=None) -> auth.Principal:
    """Build the Principal a completed sign-in earns.

    The subject is the verified email, not the LinkedIn `sub`. That is what a
    customer record can be matched against, and it is what an auditor reading the
    log needs to see. It is namespaced so it can never collide with an operator
    account issued by auth.py.
    """
    email = _email_of(claims)
    trust = trust_for(claims, customer_lookup=customer_lookup)
    identifier = email or str(claims.get("sub", ""))
    return auth.Principal(
        subject=f"linkedin:{identifier}",
        roles=roles_for("linkedin", trust),
        tenant_id=tenant_id,
    )


def display_name(claims: dict) -> str:
    for key in ("name", "given_name"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "there"
