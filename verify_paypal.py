"""Check that PayPal sandbox is actually reachable.

Passing stubs prove nothing about the real API, so run this once after the
credentials are in place.

    python verify_paypal.py

It goes as far as: get a token -> create an order -> read the order back. On its
own this script moves no money. If SDK v6 checkout is configured, the buyer
approving at the printed URL will cause the checkout server to capture.

It refuses to run against live by default. To check live anyway, pass
--allow-live explicitly - and even then it stops after creating the order.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

import config as cfg
import paypal


def main() -> int:
    parser = argparse.ArgumentParser(description="PayPal sandbox connectivity check")
    parser.add_argument("--amount", default="1.00",
                        help="amount of the order to create (default 1.00)")
    parser.add_argument("--allow-live", action="store_true",
                        help="run even when PAYPAL_ENV=live (refused by default)")
    parser.add_argument("--order", metavar="ORDER_ID",
                        help="only read back an existing order, to re-check after approval")
    args = parser.parse_args()

    # Pull credentials out of .env. Real environment variables still win.
    cfg.load_dotenv()

    env = os.environ.get("PAYPAL_ENV", "sandbox").lower()
    auto_capture = bool(os.environ.get("FRONTDESK_CHECKOUT_BASE_URL", "").strip())
    print(f"environment : {env}  ({paypal.base_url()})")

    if env == "live" and not args.allow_live:
        print("\nStopped: this would run against live.")
        print("Check in sandbox instead (PAYPAL_ENV=sandbox).")
        print("To go ahead against live anyway, pass --allow-live.")
        return 2

    if not paypal.is_configured():
        print("\nNo credentials configured. Set these and run again:")
        print("  PAYPAL_CLIENT_ID")
        print("  PAYPAL_CLIENT_SECRET")
        print("  PAYPAL_ENV=sandbox")
        print("\nPut them in .env or in the environment. .env is never distributed.")
        return 2

    # With --order, only read the order back. Use this after the buyer approves.
    if args.order:
        try:
            fetched = paypal.get_order(args.order)
        except paypal.PayPalError as exc:
            print()
            print(f"read back: failed - {exc}")
            return 1
        status = fetched["status"]
        print()
        print(f"order {args.order} : {status}")
        if status == "APPROVED":
            print("The buyer has approved. The order can now be captured.")
            if auto_capture:
                print("Give the SDK v6 page's automatic capture a moment, then check again.")
            else:
                print("Capture it through Frontdesk's confirmation gate.")
        elif status == "CREATED":
            print("Not approved yet. Open the approval URL as the sandbox buyer")
            print("(Personal / sb-xxxxx@personal.example.com).")
        elif status == "COMPLETED":
            print("Already captured.")
        return 0

    # 1. Authentication
    try:
        token = paypal._access_token()
    except paypal.PayPalError as exc:
        print(f"\n[1/3] authenticate : failed - {exc}")
        return 1
    print(f"[1/3] authenticate : ok ({len(token)} character token)")

    # 2. Create the order. No money moves.
    try:
        order = paypal.create_order(
            amount=args.amount,
            description="Frontdesk connectivity check",
            reference_id=f"frontdesk-verify-{uuid.uuid4().hex[:12]}",
        )
    except paypal.PayPalError as exc:
        print(f"[2/3] create order : failed - {exc}")
        return 1
    print(f"[2/3] create order : ok - {order['order_id']} / {order['status']} / "
          f"${order['amount']} {order['currency']}")

    # 3. Read it back
    try:
        fetched = paypal.get_order(order["order_id"])
    except paypal.PayPalError as exc:
        print(f"[3/3] read back    : failed - {exc}")
        return 1
    print(f"[3/3] read back    : ok - {fetched['status']}")

    print("\nThe real API is reachable. No money has moved.")
    if order.get("approval_url"):
        print("\nTo exercise the approval flow as well, open this URL in a browser")
        print("and approve it as the sandbox buyer:")
        print(f"  {order['approval_url']}")
        if auto_capture:
            print("\nOn this SDK v6 page, approval is captured immediately afterwards.")
            print("Use sandbox test accounts only.")
        else:
            print("\nOnce approved the status becomes APPROVED and it can be captured.")
            print("Capture it through Frontdesk's confirmation gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
