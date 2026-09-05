"""Guarded REST connector for a real order and reservation backend."""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import resilience


class ConnectorError(Exception):
    """Remote backend configuration, protocol, or transport failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class BackendConfig:
    base_url: str
    token: str
    timeout: float = 10.0
    allow_http: bool = False
    max_retries: int = 2
    retry_base_delay: float = 0.25
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "BackendConfig | None":
        base_url = os.environ.get("FRONTDESK_BACKEND_URL", "").strip()
        if not base_url:
            return None
        return cls(
            base_url=base_url.rstrip("/"),
            token=os.environ.get("FRONTDESK_BACKEND_TOKEN", ""),
            timeout=float(os.environ.get("FRONTDESK_BACKEND_TIMEOUT", "10")),
            allow_http=os.environ.get("FRONTDESK_BACKEND_ALLOW_HTTP") == "1",
            max_retries=int(os.environ.get("FRONTDESK_BACKEND_MAX_RETRIES", "2")),
            retry_base_delay=float(os.environ.get("FRONTDESK_BACKEND_RETRY_BASE", "0.25")),
            circuit_failure_threshold=int(os.environ.get(
                "FRONTDESK_BACKEND_CIRCUIT_FAILURES", "5")),
            circuit_recovery_timeout=float(os.environ.get(
                "FRONTDESK_BACKEND_CIRCUIT_RESET", "30")),
        )

    @classmethod
    def for_tenant(cls, tenant_id: str) -> "BackendConfig | None":
        """Load an exact tenant profile without storing credentials in the file."""
        profile_path = os.environ.get("FRONTDESK_TENANT_BACKENDS_FILE", "").strip()
        if profile_path:
            path = Path(profile_path)
            try:
                if path.stat().st_size > 1_000_000:
                    raise ConnectorError("The tenant backend profile exceeds 1 MB.")
                payload = json.loads(path.read_text(encoding="utf-8"))
                profile = payload.get("tenants", {}).get(tenant_id)
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                raise ConnectorError("The tenant backend profile is invalid.") from exc
            if profile is None:
                return None
            if not isinstance(profile, dict):
                raise ConnectorError("The tenant backend entry is invalid.")
            token_env = profile.get("token_env", "")
            if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", token_env):
                raise ConnectorError("Tenant backend token_env must name a safe environment variable.")
            return cls(
                base_url=str(profile.get("base_url", "")).rstrip("/"),
                token=os.environ.get(token_env, ""),
                timeout=float(profile.get("timeout", 10)),
                allow_http=profile.get("allow_http") is True,
                max_retries=int(profile.get("max_retries", 2)),
                retry_base_delay=float(profile.get("retry_base_delay", 0.25)),
                circuit_failure_threshold=int(profile.get("circuit_failure_threshold", 5)),
                circuit_recovery_timeout=float(profile.get("circuit_recovery_timeout", 30)),
            )
        if tenant_id != "default" and os.environ.get("FRONTDESK_BACKEND_URL", "").strip():
            raise ConnectorError(
                "A shared backend is configured for a non-default tenant. "
                "Set FRONTDESK_TENANT_BACKENDS_FILE with an exact tenant profile."
            )
        return cls.from_env() if tenant_id == "default" else None

    def validate(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (self.allow_http and local):
            raise ConnectorError("The backend URL must use HTTPS (HTTP is allowed only for an explicit localhost test).")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConnectorError("The backend URL is invalid.")
        if not self.token:
            raise ConnectorError("FRONTDESK_BACKEND_TOKEN is required for the live backend.")
        if self.timeout <= 0 or self.timeout > 60:
            raise ConnectorError("Backend timeout must be between 0 and 60 seconds.")
        try:
            resilience.RetryPolicy(
                max_retries=self.max_retries, base_delay=self.retry_base_delay).validate()
            resilience.CircuitBreaker(
                self.circuit_failure_threshold, self.circuit_recovery_timeout)
        except ValueError as exc:
            raise ConnectorError(f"Backend resilience policy is invalid: {exc}") from None


class RestBackend:
    def __init__(self, config: BackendConfig, tenant_id: str = "default"):
        config.validate()
        if not tenant_id or len(tenant_id) > 160 or "\r" in tenant_id or "\n" in tenant_id:
            raise ConnectorError("The tenant id is invalid.")
        self.config = config
        self.tenant_id = tenant_id
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._retry_policy = resilience.RetryPolicy(
            max_retries=config.max_retries, base_delay=config.retry_base_delay)
        self._circuit = resilience.CircuitBreaker(
            config.circuit_failure_threshold, config.circuit_recovery_timeout)

    def request(
        self, method: str, path: str, *, query: dict | None = None,
        body: dict | None = None, idempotency_key: str | None = None,
    ) -> dict:
        if not path.startswith("/") or ".." in path:
            raise ConnectorError("Unsafe backend path.")
        url = self.config.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({key: value for key, value in query.items() if value != ""})
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": "Frontdesk/1.0",
            "X-Frontdesk-Tenant-ID": self.tenant_id,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD"}:
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        def perform() -> dict:
            with self._opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ConnectorError("Backend response exceeded 2 MB.")
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise ConnectorError("Backend response must be application/json.")
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ConnectorError("Backend response must be a JSON object.")
                return decoded

        retry_safe = method in {"GET", "HEAD"} or bool(headers.get("Idempotency-Key"))
        try:
            return resilience.execute(
                perform, retry_safe=retry_safe, policy=self._retry_policy,
                breaker=self._circuit)
        except resilience.CircuitOpenError:
            raise ConnectorError("Backend circuit is open after repeated failures.") from None
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ConnectorError("Backend redirects are not allowed.") from None
            raise ConnectorError(f"Backend returned HTTP {exc.code}.") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise ConnectorError(f"Backend request failed: {getattr(exc, 'reason', exc)}") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ConnectorError("Backend returned invalid JSON.") from None

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/orders/{urllib.parse.quote(order_id, safe='')}")

    def search_reservations(self, customer: str) -> dict:
        return self.request("GET", "/reservations", query={"customer": customer})

    def get_reservation(self, reservation_id: str) -> dict:
        return self.request("GET", f"/reservations/{urllib.parse.quote(reservation_id, safe='')}")

    def list_services(self) -> dict:
        return self.request("GET", "/services")

    def find_appointment_slots(self, service_id: str, appointment_date: str,
                               *, staff_id: str = "", location_id: str = "") -> dict:
        return self.request("GET", "/availability", query={
            "service_id": service_id,
            "date": appointment_date,
            "staff_id": staff_id,
            "location_id": location_id,
        })

    def create_appointment(self, appointment: dict, request_id: str) -> dict:
        return self.request(
            "POST", "/appointments", body=appointment, idempotency_key=request_id,
        )

    def find_customer_by_email(self, email: str) -> dict | None:
        """Resolve a verified email to a customer record, or None.

        A 404 is the expected answer for "no such customer", so it is not an
        error; every other failure is, because a backend that is down must not
        read as "this person is nobody".
        """
        try:
            found = self.request("GET", "/customers", query={"email": email})
        except ConnectorError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return found or None

    def change_reservation(self, reservation_id: str, updates: dict, request_id: str) -> dict:
        return self.request(
            "PATCH", f"/reservations/{urllib.parse.quote(reservation_id, safe='')}",
            body=updates, idempotency_key=request_id,
        )

    def cancel_reservation(self, reservation_id: str, request_id: str) -> dict:
        return self.request(
            "DELETE", f"/reservations/{urllib.parse.quote(reservation_id, safe='')}",
            idempotency_key=request_id,
        )


def live_backend(tenant_id: str = "default") -> RestBackend | None:
    config = BackendConfig.for_tenant(tenant_id)
    return RestBackend(config, tenant_id) if config else None
