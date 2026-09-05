"""The buyer's approval page, built on the PayPal JavaScript SDK v6.

A signed link opens exactly one order - one Frontdesk created - and the SDK is
left to deal with the buyer's existing PayPal session. Passwords, one-time codes
and PayPal cookies never pass through here. The server captures only an order
the buyer approved on PayPal itself.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import config
import regions

TOKEN_CONTEXT = b"frontdesk-paypal-checkout-v1."
TOKEN_TTL_SECONDS = 60 * 60
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{8,32}$")

HTML = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Complete your PayPal payment</title>
  <link rel="stylesheet" href="/checkout.css">
  <script defer src="/checkout.js"></script>
</head>
<body>
  <main>
    <section class="card" aria-labelledby="title">
      <p class="eyebrow">Secure checkout</p>
      <h1 id="title">Complete your payment</h1>
      <p class="copy">Sign in and approve on PayPal. Your payment is completed immediately after approval. Frontdesk never sees your PayPal password or payment credentials.</p>
      <div class="amount"><span>Total</span><strong id="amount">Loading...</strong></div>
      <paypal-button id="paypal-button" type="pay" hidden></paypal-button>
      <p id="status" class="status" role="status" aria-live="polite">Preparing PayPal...</p>
    </section>
  </main>
</body>
</html>
"""

JAVASCRIPT = b"""const amount = document.querySelector('#amount');
const button = document.querySelector('#paypal-button');
const status = document.querySelector('#status');

main().catch((error) => {
  console.error('PayPal checkout initialization failed', error?.message || 'unknown');
  show('This checkout link is invalid or unavailable. Ask Frontdesk for a new link.', true);
});

async function main() {
  const token = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  if (!token) throw new Error('Missing checkout token');

  const config = await requestJson('/api/checkout-config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token}),
  });
  amount.textContent = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: config.currency,
  }).format(Number(config.amount));

  await loadScript(config.sdkUrl);
  const sdk = await window.paypal.createInstance({
    clientId: config.clientId,
    components: ['paypal-payments'],
    pageType: 'checkout',
    locale: 'en-US',
  });
  const methods = await sdk.findEligibleMethods({
    currencyCode: config.currency,
    amount: config.amount,
    paymentFlow: 'ONE_TIME_PAYMENT',
  });
  if (!methods.isEligible('paypal')) throw new Error('PayPal is not eligible');

  const session = sdk.createPayPalOneTimePaymentSession({
    onApprove: async () => {
      show('PayPal approved the payment. Completing the charge...');
      const result = await requestJson('/api/capture', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token}),
      });
      button.hidden = true;
      const paid = new Intl.NumberFormat('en-US', {
        style: 'currency', currency: result.amount.currency_code,
      }).format(Number(result.amount.value));
      show(`Payment complete: ${paid}. Confirmation: ${result.captureId}`);
    },
    onCancel: () => show('Payment approval was canceled. No charge was completed.'),
    onError: (error) => {
      console.error('PayPal approval failed', error?.message || 'unknown');
      show('PayPal could not approve this payment. Try again or contact support.', true);
    },
  });

  let busy = false;
  button.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    button.setAttribute('disabled', '');
    show('Opening PayPal...');
    try {
      await session.start(
        {presentationMode: 'auto'},
        Promise.resolve({orderId: config.orderId}),
      );
    } catch (error) {
      console.error('PayPal approval could not start', error?.message || 'unknown');
      show('PayPal could not open. Allow pop-ups and try again.', true);
    } finally {
      busy = false;
      button.removeAttribute('disabled');
    }
  });
  button.hidden = false;
  show('Continue with PayPal. If you are already signed in, PayPal may skip the password step.');
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    script.addEventListener('load', resolve, {once: true});
    script.addEventListener('error', reject, {once: true});
    document.head.append(script);
  });
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}

function show(message, error = false) {
  status.textContent = message;
  status.classList.toggle('error', error);
}
"""

CSS = b""":root{font-family:Inter,system-ui,sans-serif;color:#142c45;background:#eef4f8}*{box-sizing:border-box}body{margin:0}main{min-height:100vh;display:grid;place-items:center;padding:24px}.card{width:min(100%,520px);padding:clamp(28px,7vw,52px);background:#fff;border:1px solid #d7e2ea;border-radius:24px;box-shadow:0 20px 60px rgb(25 60 85/12%)}.eyebrow{margin:0 0 8px;color:#0866ff;font-size:.78rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase}h1{margin:0;font-size:clamp(1.9rem,6vw,2.7rem);line-height:1.15}.copy{margin:18px 0;color:#50677a;line-height:1.7}.amount{display:flex;justify-content:space-between;align-items:center;margin:28px 0;padding:18px 0;border-block:1px solid #e2ebf0}.amount strong{font-size:1.35rem}.status{min-height:48px;margin:18px 0 0;padding:12px 14px;border-radius:10px;background:#eef7ff;color:#164d77;font-size:.9rem;line-height:1.5}.status.error{background:#fff0f0;color:#9b2424}paypal-button{min-height:48px}paypal-button[hidden]{display:none}
"""


class CheckoutError(RuntimeError):
    """The checkout configuration is wrong, or a signature failed to verify."""


def create_checkout_url(order_id: str, amount: str, currency: str,
                        now: int | None = None) -> str | None:
    """The signed page URL when checkout is configured, otherwise ``None``."""
    base_url = os.environ.get("FRONTDESK_CHECKOUT_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    _validate_base_url(base_url)
    secret = _checkout_secret()
    issued_at = int(time.time() if now is None else now)
    claims = {
        "amount": amount,
        "currency": currency,
        "exp": issued_at + TOKEN_TTL_SECONDS,
        "order_id": order_id,
        "v": 1,
    }
    payload = _b64encode(json.dumps(
        claims, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    signature = _b64encode(hmac.new(
        secret, TOKEN_CONTEXT + payload.encode("ascii"), hashlib.sha256
    ).digest())
    return f"{base_url}/#{payload}.{signature}"


def verify_checkout_token(token: str, now: int | None = None) -> dict:
    """Verify signature, expiry and order format, then return the claims."""
    if not isinstance(token, str) or len(token) > 4096 or token.count(".") != 1:
        raise CheckoutError("Invalid checkout token.")
    payload, supplied_signature = token.split(".", 1)
    try:
        signed_payload = payload.encode("ascii")
    except UnicodeEncodeError:
        raise CheckoutError("Invalid checkout token encoding.") from None
    expected_signature = _b64encode(hmac.new(
        _checkout_secret(), TOKEN_CONTEXT + signed_payload, hashlib.sha256
    ).digest())
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise CheckoutError("Invalid checkout token signature.")
    try:
        claims = json.loads(_b64decode(payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise CheckoutError("Invalid checkout token payload.") from None
    current_time = int(time.time() if now is None else now)
    if claims.get("v") != 1 or not isinstance(claims.get("exp"), int):
        raise CheckoutError("Invalid checkout token claims.")
    if claims["exp"] < current_time:
        raise CheckoutError("Checkout token has expired.")
    if claims["exp"] > current_time + TOKEN_TTL_SECONDS + 60:
        raise CheckoutError("Checkout token expiry is invalid.")
    if not ORDER_ID_RE.fullmatch(str(claims.get("order_id", ""))):
        raise CheckoutError("Invalid PayPal order ID.")
    if (claims.get("currency") not in regions.SUPPORTED_CURRENCIES
            or not re.fullmatch(r"\d{1,5}\.\d{2}", str(claims.get("amount", "")))):
        raise CheckoutError("Invalid checkout amount.")
    return claims


def _checkout_secret() -> bytes:
    value = os.environ.get("FRONTDESK_CHECKOUT_SECRET", "")
    if len(value) < 32:
        raise CheckoutError("FRONTDESK_CHECKOUT_SECRET must contain at least 32 characters.")
    return value.encode("utf-8")


def _validate_base_url(value: str) -> None:
    parsed = urlparse(value)
    sandbox = os.environ.get("PAYPAL_ENV", "sandbox").lower() != "live"
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise CheckoutError("FRONTDESK_CHECKOUT_BASE_URL must be an origin URL without path, query, or fragment.")
    if parsed.scheme != "https" and not (sandbox and local_http):
        raise CheckoutError("FRONTDESK_CHECKOUT_BASE_URL must use HTTPS outside local sandbox testing.")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")


class CheckoutHandler(BaseHTTPRequestHandler):
    server_version = "FrontdeskCheckout/1"

    def log_message(self, format: str, *args: object) -> None:
        # A signing token in the fragment reaches neither the HTTP request nor any log.
        super().log_message(format, *args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("", "/"):
            self._send(HTTPStatus.OK, HTML, "text/html; charset=utf-8")
        elif path == "/checkout.js":
            self._send(HTTPStatus.OK, JAVASCRIPT, "text/javascript; charset=utf-8")
        elif path == "/checkout.css":
            self._send(HTTPStatus.OK, CSS, "text/css; charset=utf-8")
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/checkout-config", "/api/capture"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Expected JSON."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if size < 1 or size > 8192:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Invalid request size."})
            return
        raw_body = self.rfile.read(size)
        if not self._same_origin():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin."})
            return
        try:
            body = json.loads(raw_body)
            claims = verify_checkout_token(body.get("token", ""))
        except (CheckoutError, json.JSONDecodeError, AttributeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid or expired checkout link."})
            return
        if path == "/api/capture":
            try:
                result = capture_approved_order(claims)
            except CheckoutError:
                self._json(HTTPStatus.CONFLICT, {"error": "The PayPal order is not approved or could not be verified."})
                return
            self._json(HTTPStatus.OK, result)
            return
        client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
        if not client_id:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "PayPal is not configured."})
            return
        sandbox = os.environ.get("PAYPAL_ENV", "sandbox").lower() != "live"
        self._json(HTTPStatus.OK, {
            "amount": claims["amount"],
            "clientId": client_id,
            "currency": claims["currency"],
            "environment": "sandbox" if sandbox else "production",
            "orderId": claims["order_id"],
            "sdkUrl": ("https://www.sandbox.paypal.com/web-sdk/v6/core"
                       if sandbox else "https://www.paypal.com/web-sdk/v6/core"),
        })

    def _same_origin(self) -> bool:
        base_url = os.environ.get("FRONTDESK_CHECKOUT_BASE_URL", "")
        expected = urlparse(base_url)
        origin = self.headers.get("Origin", "")
        return origin == f"{expected.scheme}://{expected.netloc}"

    def _json(self, status: HTTPStatus, value: dict) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "; ".join((
            "default-src 'self'",
            "script-src 'self' https://*.paypal.com https://*.paypalobjects.com",
            "connect-src 'self' https://*.paypal.com https://*.paypalobjects.com",
            "frame-src https://*.paypal.com https://*.paypalobjects.com",
            "img-src 'self' data: https://*.paypal.com https://*.paypalobjects.com",
            "style-src 'self' 'unsafe-inline'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        )))
        self.end_headers()
        self.wfile.write(body)


def capture_approved_order(claims: dict) -> dict:
    """Match the signed claims against the PayPal order and capture only an order that was approved."""
    import audit
    import paypal

    order_id = claims["order_id"]
    expected_amount = {"currency_code": claims["currency"], "value": claims["amount"]}
    try:
        order = paypal.get_order(order_id)
    except paypal.PayPalError as exc:
        raise CheckoutError("Could not verify the PayPal order.") from exc
    units = order.get("purchase_units", [])
    if len(units) != 1 or units[0].get("amount") != expected_amount:
        raise CheckoutError("PayPal order amount does not match the checkout link.")

    # If the same signed link is opened twice, return the existing capture rather
        # than charging the buyer again.
    if order.get("status") == "COMPLETED":
        completed = next(
            (item for item in units[0].get("captures", [])
             if item.get("status") == "COMPLETED"),
            None,
        )
        if not completed:
            raise CheckoutError("Completed PayPal order has no completed capture.")
        return {
            "amount": expected_amount,
            "captureId": completed.get("capture_id"),
            "orderId": order_id,
            "status": "COMPLETED",
        }
    if order.get("status") != "APPROVED":
        raise CheckoutError("PayPal order is not approved.")

    try:
        captured = paypal.capture_order(order_id)
    except paypal.PayPalError as exc:
        audit.record(
            "paypal.checkout_capture_failed", actor="customer-checkout",
            details={"order_id": order_id, "reason": str(exc)},
        )
        raise CheckoutError("Could not capture the PayPal order.") from exc
    captures = captured.get("captures", [])
    completed = next(
        (item for item in captures
         if item.get("status") == "COMPLETED" and item.get("amount") == expected_amount),
        None,
    )
    if captured.get("status") != "COMPLETED" or not completed:
        audit.record(
            "paypal.checkout_capture_unverified", actor="customer-checkout",
            details={"order_id": order_id, "status": captured.get("status")},
        )
        raise CheckoutError("PayPal capture could not be verified.")
    result = {
        "amount": completed["amount"],
        "captureId": completed.get("capture_id"),
        "orderId": order_id,
        "status": "COMPLETED",
    }
    audit.record(
        "paypal.checkout_capture_completed", actor="customer-checkout",
        details={
            "amount": completed["amount"],
            "capture_id": completed.get("capture_id"),
            "order_id": order_id,
        },
    )
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(description="Frontdesk PayPal SDK v6 checkout server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    config.load_dotenv()
    base_url = os.environ.get("FRONTDESK_CHECKOUT_BASE_URL", "")
    try:
        _validate_base_url(base_url)
        _checkout_secret()
        if not os.environ.get("PAYPAL_CLIENT_ID"):
            raise CheckoutError("PAYPAL_CLIENT_ID is required.")
    except CheckoutError as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer((args.host, args.port), CheckoutHandler)
    print(f"Frontdesk PayPal checkout listening on http://{args.host}:{args.port}")
    print(f"Public checkout URL: {base_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
