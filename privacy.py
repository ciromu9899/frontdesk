"""Privacy-rights workflow for access, deletion, correction, and opt-out requests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import audit
import state

REQUEST_TYPES = {"access", "delete", "correct", "opt_out", "limit"}


def submit(tenant_id: str, subject: str, request_type: str) -> dict:
    if request_type not in REQUEST_TYPES:
        raise ValueError("unsupported privacy request type")
    if not tenant_id or not subject:
        raise ValueError("tenant and subject are required")
    request = state.create_privacy_request(tenant_id, request_type, subject)
    audit.record("privacy.requested", actor=subject, tenant_id=tenant_id,
                 details={"request_id": request["request_id"], "type": request_type})
    return request


def complete_delete(tenant_id: str, subject: str, request_id: str) -> dict:
    removed = state.complete_subject_deletion(tenant_id, subject, request_id)
    audit.record("privacy.deletion_completed", actor="privacy-officer",
                 tenant_id=tenant_id,
                 details={"request_id": request_id, "removed": removed,
                          "retained": "security audit records"})
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Frontdesk privacy-rights workflow")
    parser.add_argument("--tenant", required=True); parser.add_argument("--subject", required=True)
    parser.add_argument("--submit", choices=sorted(REQUEST_TYPES))
    parser.add_argument("--export", type=Path)
    parser.add_argument("--complete-delete", metavar="REQUEST_ID")
    parser.add_argument("--confirm", action="store_true"); args = parser.parse_args()
    if args.submit:
        print(json.dumps(submit(args.tenant, args.subject, args.submit), indent=2)); return 0
    if args.export:
        payload = state.export_subject(args.tenant, args.subject)
        args.export.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(args.export); return 0
    if args.complete_delete:
        if not args.confirm:
            parser.error("--confirm is required because deletion cannot be undone")
        print(json.dumps(complete_delete(args.tenant, args.subject, args.complete_delete), indent=2)); return 0
    parser.error("choose --submit, --export, or --complete-delete")


if __name__ == "__main__":
    raise SystemExit(main())
