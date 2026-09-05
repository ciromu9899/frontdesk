"""Tenant-isolated Shopify, Zendesk, HubSpot, and outbound email integrations."""

from __future__ import annotations

import json
import os
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from pathlib import Path

import resilience


class IntegrationError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _profiles(tenant_id: str) -> dict:
    location = os.environ.get("FRONTDESK_INTEGRATIONS_FILE", "").strip()
    if not location:
        return {}
    path = Path(location)
    try:
        if path.stat().st_size > 1_000_000:
            raise IntegrationError("Integration profiles exceed 1 MB.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        profile = payload.get("tenants", {}).get(tenant_id, {})
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise IntegrationError("Integration profiles are invalid.") from exc
    if not isinstance(profile, dict):
        raise IntegrationError("The tenant integration profile is invalid.")
    return profile


def _secret(profile: dict, key: str) -> str:
    env_name = profile.get(key, "")
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env_name):
        raise IntegrationError(f"{key} must name a safe environment variable.")
    value = os.environ.get(env_name, "")
    if not value:
        raise IntegrationError(f"The environment variable named by {key} is empty.")
    return value


class RestIntegration:
    def __init__(self, tenant_id: str, name: str):
        profile = _profiles(tenant_id).get(name)
        if not isinstance(profile, dict):
            raise IntegrationError(f"{name} is not configured for this tenant.")
        self.tenant_id = tenant_id; self.name = name; self.profile = profile
        self.base_url = str(profile.get("base_url", "")).rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise IntegrationError(f"{name} base_url must be a clean HTTPS origin.")
        self.token = _secret(profile, "token_env")
        self.timeout = max(1.0, min(float(profile.get("timeout", 15)), 60.0))
        try:
            self._retry_policy = resilience.RetryPolicy(
                max_retries=int(profile.get("max_retries", 2)),
                base_delay=float(profile.get("retry_base_delay", 0.25)),
                max_delay=float(profile.get("retry_max_delay", 5.0)),
            )
            self._retry_policy.validate()
            self._circuit = resilience.CircuitBreaker(
                int(profile.get("circuit_failure_threshold", 5)),
                float(profile.get("circuit_recovery_timeout", 30)),
            )
        except (TypeError, ValueError) as exc:
            raise IntegrationError(f"{name} resilience policy is invalid: {exc}") from None
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, path: str, *, query: dict | None = None,
                body: dict | None = None, headers: dict | None = None,
                retry_safe: bool = False) -> dict:
        if not path.startswith("/") or ".." in path:
            raise IntegrationError("Unsafe integration path.")
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = {"Accept": "application/json", "Content-Type": "application/json",
                           "Authorization": f"Bearer {self.token}",
                           "User-Agent": "Frontdesk/1.3", "X-Frontdesk-Tenant-ID": self.tenant_id}
        request_headers.update(headers or {})
        request_headers = {key: value for key, value in request_headers.items() if value}
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        def perform() -> dict:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise IntegrationError("Integration response exceeded 2 MB.")
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(decoded, dict):
                    raise IntegrationError("Integration response must be a JSON object.")
                return decoded

        idempotent = any(
            key.lower() == "idempotency-key" and bool(value)
            for key, value in request_headers.items())
        safe_to_repeat = retry_safe or method in {"GET", "HEAD"} or idempotent
        try:
            return resilience.execute(
                perform, retry_safe=safe_to_repeat, policy=self._retry_policy,
                breaker=self._circuit)
        except resilience.CircuitOpenError:
            raise IntegrationError(
                f"{self.name} circuit is open after repeated failures.") from None
        except urllib.error.HTTPError as exc:
            raise IntegrationError(f"{self.name} returned HTTP {exc.code}.") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise IntegrationError(f"Could not reach {self.name}: {getattr(exc, 'reason', exc)}") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise IntegrationError(f"{self.name} returned invalid JSON.") from None


class Shopify(RestIntegration):
    def __init__(self, tenant_id: str):
        super().__init__(tenant_id, "shopify")

    def find_order(self, order_name: str) -> dict:
        query = """query FrontdeskOrder($query: String!) {
          orders(first: 10, query: $query) {
            nodes { id name email createdAt displayFinancialStatus displayFulfillmentStatus }
          }
        }"""
        payload = self.request("POST", "/admin/api/2026-07/graphql.json",
                               body={"query": query, "variables": {"query": f"name:{order_name}"}},
                               headers={"Authorization": "", "X-Shopify-Access-Token": self.token},
                               retry_safe=True)
        nodes = ((payload.get("data") or {}).get("orders") or {}).get("nodes", [])
        return {"orders": nodes if isinstance(nodes, list) else [],
                "errors": payload.get("errors", [])}


class Zendesk(RestIntegration):
    def __init__(self, tenant_id: str):
        super().__init__(tenant_id, "zendesk")

    def get_ticket(self, ticket_id: str) -> dict:
        return self.request("GET", f"/api/v2/tickets/{urllib.parse.quote(ticket_id, safe='')}")

    def create_ticket(self, subject: str, description: str, requester_email: str,
                      idempotency_key: str = "") -> dict:
        return self.request("POST", "/api/v2/tickets", body={"ticket": {
            "subject": subject[:200], "comment": {"body": description[:10_000]},
            "requester": {"email": requester_email}}},
            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())})


class HubSpot(RestIntegration):
    def __init__(self, tenant_id: str):
        super().__init__(tenant_id, "hubspot")

    def find_contact(self, email: str) -> dict:
        return self.request("POST", "/crm/v3/objects/contacts/search", body={
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname"], "limit": 10},
            retry_safe=True)

    def create_ticket(self, subject: str, content: str,
                      idempotency_key: str = "") -> dict:
        return self.request("POST", "/crm/v3/objects/tickets", body={"properties": {
            "subject": subject[:200], "content": content[:10_000],
            "hs_pipeline": str(self.profile.get("pipeline", "0")),
            "hs_pipeline_stage": str(self.profile.get("stage", "1"))}},
            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())})


def send_email(tenant_id: str, recipient: str, subject: str, text: str) -> None:
    profile = _profiles(tenant_id).get("email")
    if not isinstance(profile, dict):
        raise IntegrationError("email is not configured for this tenant.")
    host = str(profile.get("smtp_host", "")); port = int(profile.get("smtp_port", 465))
    if not host or port not in range(1, 65536):
        raise IntegrationError("The SMTP endpoint is invalid.")
    username = _secret(profile, "username_env"); password = _secret(profile, "password_env")
    sender = str(profile.get("from", username))
    if (recipient.count("@") != 1 or sender.count("@") != 1 or
            any(character in recipient + sender + subject for character in "\r\n")):
        raise IntegrationError("Email headers are invalid.")
    message = EmailMessage(); message["From"] = sender; message["To"] = recipient
    message["Subject"] = subject[:200]; message.set_content(text[:50_000])
    context = ssl.create_default_context()
    try:
        if profile.get("starttls") is True:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.starttls(context=context); smtp.login(username, password); smtp.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as smtp:
                smtp.login(username, password); smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise IntegrationError(f"Email delivery failed: {exc}") from None
