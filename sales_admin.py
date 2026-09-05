"""Administrative commands for the isolated Shellie sales ledger."""

from __future__ import annotations

import argparse
import json
import os

import sales


def main() -> int:
    parser = argparse.ArgumentParser(description="Shellie FrontDesk sales administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")

    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("order_id")

    approve = subparsers.add_parser("approve-tax")
    approve.add_argument("order_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--jurisdiction", required=True)
    approve.add_argument("--note", default="")

    refund = subparsers.add_parser("refund")
    refund.add_argument("capture_id")
    refund.add_argument("--amount")
    refund.add_argument("--currency", default="USD")
    refund.add_argument("--reason", default="Customer request")
    refund.add_argument("--confirm", action="store_true")
    refund.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "status":
            result = sales.status()
        elif args.command == "receipt":
            result = sales.receipt(args.order_id)
        elif args.command == "approve-tax":
            result = sales.approve_tax(args.order_id, actor=args.actor,
                                       jurisdiction=args.jurisdiction, note=args.note)
        else:
            if not args.confirm:
                print("Refund not sent. Repeat with --confirm after reviewing the capture and amount.")
                return 2
            if os.environ.get("SHELLIE_PAYPAL_ENV", "sandbox").lower() == "live" and not args.allow_live:
                print("Live refund not sent. Repeat with --allow-live after final approval.")
                return 2
            result = sales.refund(args.capture_id, amount=args.amount,
                                  currency=args.currency, reason=args.reason)
    except sales.SalesError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
