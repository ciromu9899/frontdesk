"""Frontdesk authentication and role-based authorization.

Tokens are compact HMAC-SHA256 signed JSON documents. The signing secret and
access token are supplied through environment variables at runtime; neither is
stored by this module.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass


ROLE_PERMISSIONS = {
    # The floor granted to a sender arriving over public social media. A handle
    # is a claim, not an identity, so nothing customer-specific is reachable -
    # public information only.
    "guest": {"knowledge:read"},
    "viewer": {"knowledge:read", "audit:read"},
    "support": {"knowledge:read", "orders:read", "reservations:read",
                "appointments:create", "tickets:write"},
    "operator": {
        "knowledge:read", "orders:read", "reservations:read", "reservations:write",
        "appointments:create", "tickets:write"
    },
    # Kept as a compatibility alias for existing tokens. FrontDesk 1.2 has no
    # payment tools, so this role intentionally grants public knowledge only.
    "finance": {"knowledge:read"},
    "admin": {"*"},
}

# Subject prefixes that mean "this principal is a customer who proved an email",
# as opposed to an operator holding a token this system issued. See
# Principal.customer_email.
IDENTITY_PROVIDERS = ("linkedin",)


class AuthError(Exception):
    """Authentication or authorization failure."""


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: tuple[str, ...]
    tenant_id: str = "default"

    def can(self, permission: str) -> bool:
        granted: set[str] = set()
        for role in self.roles:
            granted.update(ROLE_PERMISSIONS.get(role, set()))
        return "*" in granted or permission in granted

    @property
    def customer_email(self) -> str:
        """The customer this principal *is*, when it is a customer at all.

        An operator issued a token by auth.py acts on behalf of the business and
        sees every record. Somebody who proved an email through a channel sign-in
        is a customer, and must see only their own - so a subject of the form
        ``<provider>:<email>`` carries that email, and tools scope to it.

        Returns "" for an operator, which is what tells the tools not to scope.
        """
        provider, separator, rest = self.subject.partition(":")
        if not separator or provider not in IDENTITY_PROVIDERS:
            return ""
        return rest.strip().lower() if rest.count("@") == 1 else ""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    """Decode, rejecting any spelling _b64encode would not have produced.

    The decoder ignores the unused bits in the final character, so a signature
    has sixteen valid encodings rather than one. Requiring the round trip keeps
    a token's text and its meaning in step - two tokens that differ as strings
    now always differ as credentials.
    """
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64encode(decoded) != value:
        raise ValueError("base64 is not in canonical form")
    return decoded


def signing_secret() -> str:
    secret = os.environ.get("FRONTDESK_AUTH_SECRET", "")
    if len(secret) < 32:
        raise AuthError("FRONTDESK_AUTH_SECRET must contain at least 32 characters.")
    return secret


def issue_token(
    principal: Principal, secret: str, expires_in: int = 8 * 60 * 60
) -> str:
    now = int(time.time())
    payload = {
        "sub": principal.subject,
        "roles": list(principal.roles),
        "tenant_id": principal.tenant_id,
        "iat": now,
        "exp": now + expires_in,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{encoded}.{_b64encode(signature.digest())}"


def authenticate_token(token: str | None, secret: str | None = None) -> Principal:
    if not token:
        raise AuthError("An access token is required.")
    secret = secret or signing_secret()
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(supplied_signature)):
            raise AuthError("The access token signature is invalid.")
        payload = json.loads(_b64decode(encoded))
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        raise AuthError("The access token is malformed.") from None

    try:
        expires_at = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        raise AuthError("The access token has invalid claims.") from None
    if expires_at <= int(time.time()):
        raise AuthError("The access token has expired.")
    subject = str(payload.get("sub", "")).strip()
    roles = tuple(str(role) for role in payload.get("roles", []))
    if not subject or not roles or any(role not in ROLE_PERMISSIONS for role in roles):
        raise AuthError("The access token has invalid claims.")
    return Principal(subject, roles, str(payload.get("tenant_id", "default")))


def require(principal: Principal, permission: str) -> None:
    if not principal.can(permission):
        raise AuthError(f"Permission denied: {permission}.")


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _main() -> int:
    parser = argparse.ArgumentParser(description="Issue a signed Frontdesk access token")
    parser.add_argument("--new-secret", action="store_true",
                        help="generate and print one signing secret")
    parser.add_argument("--subject", help="subject of the token (not needed with --new-secret)")
    parser.add_argument("--roles", default="support", help="comma-separated roles")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--hours", type=int, default=8)
    args = parser.parse_args()
    if getattr(args, "new_secret", False):
        import secrets
        print(secrets.token_urlsafe(32))
        return 0
    if not args.subject:
        parser.error("--subject is required (or use --new-secret to generate a secret)")
    roles = tuple(part.strip() for part in args.roles.split(",") if part.strip())
    invalid = [role for role in roles if role not in ROLE_PERMISSIONS]
    if invalid:
        parser.error(f"unknown roles: {', '.join(invalid)}")
    print(issue_token(Principal(args.subject, roles, args.tenant), signing_secret(), args.hours * 3600))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
