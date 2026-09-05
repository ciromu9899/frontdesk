"""Configuration diagnosis: what is missing and how to fix it, on one screen.

    python chat.py --doctor

The point is to turn "it does not work" into "run this". A one-line error will
never lead someone through generate a secret -> issue a token -> set the
environment variable.
"""

from __future__ import annotations

import os
import socket
import urllib.parse
from pathlib import Path

import audit
import auth
import config as cfg
import rag

ROOT = Path(__file__).resolve().parent

OK, WARN, INFO, BAD = "ok", "warn", "info", "bad"
_MARK = {OK: "[ok]", WARN: "[! ]", INFO: "[- ]", BAD: "[x ]"}


class Report:
    """One diagnostic result: how to show it, and whether it needs action."""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list]] = []
        self.blocking = 0

    def section(self, title: str) -> None:
        self.sections.append((title, []))

    def line(self, level: str, label: str, detail: str, fixes: list | None = None) -> None:
        self.sections[-1][1].append((level, label, detail, fixes or []))
        if level in (WARN, BAD):
            self.blocking += 1

    def render(self) -> str:
        out: list[str] = []
        for title, lines in self.sections:
            out.append("")
            out.append(title)
            for level, label, detail, fixes in lines:
                out.append(f"  {_MARK[level]} {label:<26} {detail}")
                for fix in fixes:
                    out.append(f"         -> {fix}")
        return "\n".join(out)


def _reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _auth(report: Report) -> None:
    report.section("Authentication")
    if os.environ.get("FRONTDESK_AUTH_MODE") == "disabled":
        report.line(WARN, "FRONTDESK_AUTH_MODE",
                    "disabled - development only, never in production",
                    ["set FRONTDESK_AUTH_MODE="])
        return

    secret = os.environ.get("FRONTDESK_AUTH_SECRET", "")
    if len(secret) >= 32:
        report.line(OK, "FRONTDESK_AUTH_SECRET", f"set ({len(secret)} chars)")
    else:
        problem = "too short (needs 32+ chars)" if secret else "not set"
        report.line(WARN, "FRONTDESK_AUTH_SECRET", problem,
                    ["python auth.py --new-secret",
                     "set FRONTDESK_AUTH_SECRET=<the value printed above>"])

    token = os.environ.get("FRONTDESK_ACCESS_TOKEN", "")
    if not token:
        report.line(WARN, "FRONTDESK_ACCESS_TOKEN", "not set",
                    ["python auth.py --subject you@example.com --roles admin --hours 8",
                     "set FRONTDESK_ACCESS_TOKEN=<the value printed above>"])
    elif len(secret) >= 32:
        try:
            principal = auth.authenticate_token(token, secret)
            roles = ", ".join(principal.roles) or "(none)"
            report.line(OK, "FRONTDESK_ACCESS_TOKEN", f"valid - {principal.subject} [{roles}]")
        except Exception as exc:
            report.line(WARN, "FRONTDESK_ACCESS_TOKEN", f"rejected - {exc}",
                        ["python auth.py --subject you@example.com --roles admin --hours 8"])
    else:
        report.line(INFO, "FRONTDESK_ACCESS_TOKEN", "set, but cannot verify without the secret")


def _region(report: Report) -> None:
    """Which market's conventions are in force.

    Worth a line of its own because getting it wrong is silent: 05/09/2026 reads
    as a valid date in both markets and means different days, and a healthcare
    persona will confidently give the wrong emergency number.
    """
    import regions

    fact = regions.facts()
    configured = os.environ.get("FRONTDESK_REGION", "").strip().lower()
    report.section("Region")
    if configured and configured not in regions.SUPPORTED:
        report.line(WARN, "FRONTDESK_REGION",
                    f"'{configured}' is not a region - falling back to {regions.DEFAULT}",
                    [f"set FRONTDESK_REGION to one of: {', '.join(regions.SUPPORTED)}"])
    else:
        report.line(OK, "FRONTDESK_REGION",
                    f"{regions.current()} - {fact['name']}")
    report.line(OK, "  conventions",
                f"{fact['currency']} ({fact['currency_symbol']}), "
                f"dates {fact['date_format']}, emergency {fact['emergency_number']}")


def _providers(report: Report) -> None:
    report.section("Model provider")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        report.line(OK, "anthropic", "credentials present")
    else:
        report.line(INFO, "anthropic", "not configured",
                    ["set ANTHROPIC_API_KEY=sk-ant-...    (or run: ant auth login)"])

    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    report.line(OK if has_openai else INFO, "openai",
                "credentials present" if has_openai else "not configured")

    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    parsed = urllib.parse.urlparse(base)
    alive = _reachable(parsed.hostname or "localhost", parsed.port or 11434)
    report.line(OK if alive else INFO, "ollama",
                ("reachable" if alive else "not reachable") + f" at {base}")

    report.line(OK, "echo", "always available (dry run, no API calls)")


def _knowledge(report: Report) -> None:
    report.section("Knowledge base")
    documents = []
    if rag.KNOWLEDGE_DIR.exists():
        documents = [p for p in rag.KNOWLEDGE_DIR.glob("**/*")
                     if p.suffix.lower() in {".md", ".txt", ".html"}]
    status = rag.index_status()
    if not documents:
        report.line(INFO, "knowledge/", "no documents - search_knowledge finds nothing",
                    ["put approved .md / .txt / .html files in knowledge/"])
    elif not status.get("ready"):
        reason = status.get("reason")
        detail = ("stale - the index format changed" if reason == "reindex_required"
                  else f"not built ({len(documents)} documents waiting)")
        report.line(WARN, "RAG index", detail, ["python rag.py --build"])
    else:
        report.line(OK, "RAG index",
                    f"{status.get('chunks', 0)} chunks "
                    f"from {status.get('files', len(documents))} documents")


def _backend(report: Report) -> None:
    report.section("Order / reservation backend")
    url = os.environ.get("FRONTDESK_BACKEND_URL", "").strip()
    if not url:
        report.line(INFO, "FRONTDESK_BACKEND_URL", "not set - bundled demo data is used")
        return
    insecure = not url.lower().startswith("https://")
    if insecure and os.environ.get("FRONTDESK_BACKEND_ALLOW_HTTP") != "1":
        report.line(BAD, "FRONTDESK_BACKEND_URL", "not HTTPS - requests will be refused",
                    ["use an https:// endpoint"])
        return
    has_token = bool(os.environ.get("FRONTDESK_BACKEND_TOKEN"))
    report.line(OK if has_token else WARN, "FRONTDESK_BACKEND_URL",
                url if has_token else url + "   (no FRONTDESK_BACKEND_TOKEN)",
                [] if has_token else ["set FRONTDESK_BACKEND_TOKEN=<bearer token>"])


def _channels(report: Report) -> None:
    report.section("Channels (Slack / Teams / Meta / WhatsApp / email / LinkedIn)")
    try:
        import channels
    except Exception as exc:
        report.line(BAD, "channels", f"import failed - {exc}")
        return
    for name, channel in channels.available().items():
        if channel.configured():
            trust = "workspace" if name in ("slack", "teams") else "public"
            roles = ", ".join(channels.roles_for(name, trust))
            report.line(OK, name, f"configured - trust={trust}, roles={roles}")
        else:
            report.line(INFO, name, "not configured",
                        ["see docs/customer-guide.md#slack-meta-whatsapp-and-email"])
    _linkedin(report)


def _linkedin(report: Report) -> None:
    """LinkedIn is checked apart from the others because it is a different thing.

    Slack and Meta carry messages. LinkedIn carries identity: it is what lets
    somebody on a public channel stop being a handle. So the useful report is not
    "connected" but "can a person actually get out of the guest tier here".
    """
    from channels import identity, linkedin

    missing = [name for name in (
        "FRONTDESK_LINKEDIN_CLIENT_ID",
        "FRONTDESK_LINKEDIN_CLIENT_SECRET",
        "FRONTDESK_LINKEDIN_REDIRECT_URI",
    ) if not os.environ.get(name, "").strip()]
    secret = os.environ.get("FRONTDESK_LINKEDIN_STATE_SECRET", "")

    configured_any = any(os.environ.get(name, "").strip() for name in (
        "FRONTDESK_LINKEDIN_CLIENT_ID", "FRONTDESK_LINKEDIN_CLIENT_SECRET",
        "FRONTDESK_LINKEDIN_REDIRECT_URI", "FRONTDESK_LINKEDIN_STATE_SECRET"))
    if not configured_any:
        report.line(INFO, "linkedin sign-in",
                    "optional and not configured - guest knowledge chat remains available",
                    ["configure it before enabling private account actions"])
        return
    if missing or len(secret) < 32:
        needed = missing + (
            [] if len(secret) >= 32 else ["FRONTDESK_LINKEDIN_STATE_SECRET (32+ chars)"])
        report.line(BAD, "linkedin sign-in", "partially configured",
                    [f"set {', '.join(needed)}",
                     "see docs/customer-guide.md#optional-step-up-linkedin-sign-in"])
        return

    try:
        linkedin.redirect_uri()
    except linkedin.LinkedInError as exc:
        report.line(BAD, "linkedin sign-in", str(exc),
                    ["it must match the redirect URI registered with LinkedIn exactly"])
        return

    domains = linkedin.workspace_domains()
    detail = "configured - a public sender can verify and reach their own records"
    report.line(OK, "linkedin sign-in", detail)
    if domains:
        report.line(OK, "  workspace domains", ", ".join(domains))
    live = identity.purge_expired()
    if live:
        report.line(INFO, "  verifications", f"dropped {live} expired")


def _mobile(report: Report) -> None:
    """Whether an irreversible action can be approved when nobody is at a desk.

    Without this, the gate is not broken - it refuses, which is the safe answer.
    But every gated action arriving on a channel refuses, so it is worth saying
    out loud rather than leaving somebody to work out why nothing ever runs.
    """
    import approvals

    report.section("Approvals from a phone")
    if not approvals.enabled():
        report.line(INFO, "remote approval", "off - gated actions on a channel are declined",
                    ["set FRONTDESK_REMOTE_APPROVAL=1",
                     "see docs/customer-guide.md#remote-approval"])
        return
    secret = os.environ.get("FRONTDESK_AUTH_SECRET", "")
    if len(secret) < 32:
        report.line(BAD, "remote approval", "on, but there is no secret to sign links with",
                    ["python auth.py --new-secret",
                     "set FRONTDESK_AUTH_SECRET=<the value printed above>"])
        return
    waiting = len(approvals.pending())
    report.line(OK, "remote approval", "on - a phone can answer the confirmation gate",
                ["python webhooks.py --pair --subject you@example.com --roles operator"])
    if waiting:
        report.line(INFO, "  waiting now", f"{waiting} action(s)")


def _audit_section(report: Report) -> None:
    report.section("Audit log")
    path = audit.AUDIT_PATH
    if not path.exists() and not audit.segments(path):
        report.line(INFO, "data/audit.jsonl", "no events yet")
        return
    ok, count, _ = audit.verify(path)
    rotated = len(audit.segments(path))
    detail = f"{count} events"
    if rotated:
        detail += f", {rotated} rotated segments"
    report.line(OK if ok else BAD, "hash chain",
                ("verified" if ok else "BROKEN - the log was altered") + f" ({detail})")


def run() -> int:
    """Run the diagnosis and print it. Returns 1 if anything needs action."""
    cfg.load_dotenv()
    report = Report()
    _region(report)
    _auth(report)
    _providers(report)
    _knowledge(report)
    _backend(report)
    _channels(report)
    _mobile(report)
    _audit_section(report)

    print("Frontdesk configuration check")
    print(report.render())
    print()
    if report.blocking:
        print(f"{report.blocking} item(s) need attention. Follow the -> lines above.")
    else:
        print("Everything needed is in place.")
    print()
    print("No credentials to hand? This always works:")
    print("  set FRONTDESK_AUTH_MODE=disabled")
    print("  python chat.py --provider echo")
    return 1 if report.blocking else 0
