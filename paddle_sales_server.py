"""Loopback sandbox test storefront; never enable this server for live sales."""
import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from paddle_sales import CommerceError, SandboxAPI, Store


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>FrontDesk Sandbox</title>
<style>body{font:18px/1.6 system-ui;background:#faf7f2;color:#302c33;max-width:720px;margin:40px auto;padding:20px}button,a{padding:14px;margin:8px;display:inline-block}button{background:#68455f;color:white;border:0;border-radius:12px}button:focus-visible,a:focus-visible{outline:3px solid #28614e}button:disabled{opacity:.5}</style>
<h1>FrontDesk — test purchase</h1><p>Sandbox only. No real payment. Self-hosted software; an AI runtime is required. Not a signed desktop installer.</p>
<p>Review the recurring price, trial and billing terms in Paddle before confirming the test purchase. Payment alone does not prove delivery: wait for verified server events.</p>
<button id="buy" disabled>Open test checkout</button><button id="check">Check purchase status</button>
<button id="download" disabled>Download ZIP</button><a href="/portal" target="_blank" rel="noopener">Manage / cancel subscription (email sign-in)</a>
<p id="status" role="status">Loading sandbox checkout…</p>
<p>The receipt session lasts 24 hours and is held in this browser tab. Recovery on another device is not implemented yet.</p>
<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script><script src="/app.js"></script></html>"""

SCRIPT = """let token=sessionStorage.getItem('frontdesk-sandbox-receipt');
const status=document.getElementById('status'),buy=document.getElementById('buy'),download=document.getElementById('download');
async function call(path,method='GET'){
 const r=await fetch(path,{method,headers:{'Authorization':'Bearer '+(token||''),'X-FrontDesk-Request':'sandbox'}});
 const data=await r.json();if(!r.ok)throw Error(data.error||'Request failed');return data;
}
async function check(){const s=await call('/status');status.textContent=JSON.stringify(s);download.disabled=!s.download_allowed;}
async function safe(fn){try{await fn();}catch(e){status.textContent=e.message;}}
buy.onclick=()=>safe(async()=>{buy.disabled=true;try{
 if(!token){token=(await call('/session','POST')).token;sessionStorage.setItem('frontdesk-sandbox-receipt',token);}
 const t=await call('/checkout','POST');Paddle.Checkout.open({transactionId:t.transaction_id});
 }finally{buy.disabled=false;}});
document.getElementById('check').onclick=()=>safe(check);
download.onclick=()=>safe(async()=>{const r=await fetch('/download',{headers:{Authorization:'Bearer '+token}});
 if(!r.ok)throw Error('Download unavailable; refresh purchase status.');const url=URL.createObjectURL(await r.blob());
 const a=document.createElement('a');a.href=url;a.download='frontdesk.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);});
safe(async()=>{const c=await call('/config');Paddle.Environment.set('sandbox');Paddle.Initialize({token:c.client_token,eventCallback:e=>{if(e.name==='checkout.completed')safe(check);}});buy.disabled=false;status.textContent='Ready for sandbox testing.';});
"""


def handler(store, secret, client_token, portal_url, package, package_hash, origin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No URLs, bearer tokens or payer data in access logs.

        def reply(self, code, content, content_type="application/json"):
            body = json.dumps(content).encode() if content_type == "application/json" else content
            self.send_response(code)
            for key, value in {"Content-Type": content_type, "Content-Length": str(len(body)),
                               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                               "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}.items():
                self.send_header(key, value)
            if content_type == "application/zip":
                self.send_header("Content-Disposition", 'attachment; filename="frontdesk.zip"')
            self.end_headers()
            self.wfile.write(body)

        def token(self):
            value = self.headers.get("Authorization", "")
            if not value.startswith("Bearer "):
                raise CommerceError("Authentication required")
            return value[7:]

        def do_POST(self):
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 <= length <= 1_000_000:
                    raise CommerceError("Invalid body length")
                if self.path == "/webhook":
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise CommerceError("Incomplete body")
                    self.reply(200, store.webhook(body, self.headers.get("Paddle-Signature", ""), secret))
                    return
                if self.headers.get("Origin") != origin or self.headers.get("X-FrontDesk-Request") != "sandbox":
                    raise CommerceError("Origin rejected")
                if length:
                    raise CommerceError("This operation takes no customer-supplied identifiers")
                if self.path == "/session":
                    self.reply(200, {"token": store.start()})
                elif self.path == "/checkout":
                    self.reply(200, {"transaction_id": store.checkout(self.token())})
                else:
                    self.reply(404, {"error": "Not found"})
            except CommerceError as exc:
                self.reply(400, {"error": str(exc)})
            except Exception:
                self.reply(503, {"error": "Processing unavailable; do not repeat payment. Contact support."})

        def do_GET(self):
            try:
                if self.path == "/":
                    self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
                elif self.path == "/app.js":
                    self.reply(200, SCRIPT.encode(), "text/javascript; charset=utf-8")
                elif self.path == "/config":
                    self.reply(200, {"client_token": client_token, "environment": "sandbox"})
                elif self.path == "/status":
                    self.reply(200, store.status(self.token()))
                elif self.path == "/download":
                    self.reply(200, store.download(self.token(), package, package_hash), "application/zip")
                elif self.path == "/portal":
                    self.send_response(303)
                    self.send_header("Location", portal_url)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                else:
                    self.reply(404, {"error": "Not found"})
            except CommerceError as exc:
                self.reply(403, {"error": str(exc)})
            except Exception:
                self.reply(503, {"error": "Service unavailable"})
    return Handler


def main():
    parser = argparse.ArgumentParser(description="FrontDesk Paddle sandbox storefront")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--price-id", required=True)
    parser.add_argument("--portal-url", required=True, help="Sandbox email-login portal URL, never a tokenized session URL")
    parser.add_argument("--port", type=int, default=8782)
    args = parser.parse_args()
    portal = urlsplit(args.portal_url)
    if (portal.scheme != "https" or portal.hostname != "sandbox-customer-portal.paddle.com"
            or portal.username or portal.password or portal.port or portal.query or portal.fragment
            or not re.fullmatch(r"/cpl_[a-z0-9]{26}/?", portal.path)):
        parser.error("A sandbox portal email-login URL is required")
    client = os.environ.get("SHELLIE_PADDLE_SANDBOX_CLIENT_TOKEN", "")
    secret = os.environ.get("SHELLIE_PADDLE_SANDBOX_WEBHOOK_SECRET", "")
    if not client.startswith("test_") or not secret:
        parser.error("Sandbox client token and webhook secret must be supplied by the environment")
    store = Store(args.database, args.price_id, SandboxAPI(os.environ.get("SHELLIE_PADDLE_SANDBOX_API_KEY", "")))
    origin = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(
        store, secret, client, args.portal_url, args.package, args.sha256, origin))
    print("Sandbox only: " + origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
