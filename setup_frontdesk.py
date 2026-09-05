"""First run: turn an unpacked FrontDesk into a working front desk.

    python setup_frontdesk.py

Installing the files is the easy half. What stopped a buyer was everything after
it: generate two secrets, decide a market and an industry, get a local model,
build the knowledge index, and know which of eleven environment variables
actually matter. This asks a handful of questions, writes `.env`, and leaves a
deployment that answers customers.

Nothing here is required to run FrontDesk. Every answer maps to a setting that
can be edited by hand afterwards, and the file it writes says which.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DEFAULT_MODEL = "qwen3:8b"
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

INDUSTRIES = [
    ("salon", "Salon, barber, nails, beauty, spa"),
    ("hospitality", "Hotel, short stay, restaurant, travel"),
    ("ecommerce", "Online shop, retail, D2C"),
    ("homeservices", "Repairs, cleaning, moving, trades"),
    ("realestate", "Estate agency, lettings, property management"),
    ("automotive", "Car sales, servicing, hire"),
    ("education", "School, language centre, gym, studio"),
    ("events", "Events, photography, hire"),
    ("saas-support", "Software or IT support"),
    ("healthcare", "Healthcare reception (information and booking only)"),
    ("fintech", "Finance or insurance reception (information only)"),
    ("legal", "Law firm reception (no legal advice)"),
    ("professional", "Accountancy or consulting reception"),
    ("recruiting", "Hiring (role information and applications only)"),
]

MARKETS = [
    ("us", "United States", "USD", "en"),
    ("uk", "United Kingdom", "GBP", "en"),
    ("de", "Germany", "EUR", "de"),
    ("nl", "Netherlands", "EUR", "nl"),
    ("fr", "France", "EUR", "fr"),
]

# Industries whose tools are gated. Anything else needs no industry flag.
GATED = {"salon"}


class SetupError(RuntimeError):
    """Something the buyer has to fix before the setup can finish."""


def say(message: str = "") -> None:
    print(message)


def ask(question: str, options: list[tuple], default_index: int = 0) -> tuple:
    say()
    say(question)
    for index, option in enumerate(options, start=1):
        marker = " (default)" if index - 1 == default_index else ""
        say(f"  {index:2d}. {option[1]}{marker}")
    while True:
        raw = input(f"Choose 1-{len(options)} [{default_index + 1}]: ").strip()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        say("Please enter one of the numbers listed.")


def ask_text(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{question}{suffix}: ").strip() or default


def ask_yes(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{question} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


# --------------------------------------------------------------------------
# Checks


def check_python() -> None:
    if sys.version_info < (3, 11):
        raise SetupError(
            f"FrontDesk needs Python 3.11 or newer; this is {sys.version.split()[0]}. "
            "Install a newer Python and run this again.")
    say(f"[ok] Python {sys.version.split()[0]}")


def ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=4) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [str(model.get("name", "")) for model in payload.get("models", [])]


def ensure_model(model: str, offer_pull: bool = True) -> bool:
    """Is a local model ready to answer? Pull it if the buyer agrees."""
    installed = ollama_models()
    if not installed and not shutil.which("ollama"):
        say("[!] Ollama is not installed. FrontDesk keeps conversations on this "
            "machine by running the model locally.")
        say("    Install it from https://ollama.com/download, then run this again.")
        return False
    if model in installed:
        say(f"[ok] Local model {model} is ready")
        return True
    if not installed:
        say("[!] Ollama is installed but not running. Start it, then run this again.")
        return False
    say(f"[!] The model {model} is not downloaded yet (about 5 GB).")
    if not offer_pull or not ask_yes("Download it now?"):
        say(f"    Later, run: ollama pull {model}")
        return False
    say(f"    Downloading {model}. This takes a while on a slow connection.")
    try:
        subprocess.run(["ollama", "pull", model], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        say(f"[!] The download did not finish ({exc}). Run: ollama pull {model}")
        return False
    say(f"[ok] {model} downloaded")
    return True


# --------------------------------------------------------------------------
# Writing the configuration


def build_env(answers: dict) -> str:
    lines = [
        "# FrontDesk configuration, written by setup_frontdesk.py.",
        "# Edit by hand at any time; every value is a documented setting.",
        "# Full list: docs/reference.md",
        "",
        "# --- identity and signing -------------------------------------------",
        "# Two independent secrets. Rotate with docs/secret-rotation-runbook.md.",
        f"FRONTDESK_AUTH_SECRET={answers['auth_secret']}",
        f"FRONTDESK_FEEDBACK_SECRET={answers['feedback_secret']}",
        "",
        "# --- what this deployment is ----------------------------------------",
        f"FRONTDESK_REGION={answers['region']}",
        f"FRONTDESK_WEB_PERSONA={answers['industry']}",
    ]
    if answers["industry"] in GATED:
        lines.append(f"FRONTDESK_INDUSTRY={answers['industry']}")
    lines += [
        "",
        "# --- the model ------------------------------------------------------",
        "# Local by default: customer conversations stay on this machine.",
        "FRONTDESK_WEB_PROVIDER=ollama",
        f"OLLAMA_MODEL={answers['model']}",
    ]
    if answers.get("public_url"):
        lines += [
            "",
            "# --- where customers reach you ---------------------------------",
            "# Used for the rating link sent with email replies.",
            f"FRONTDESK_PUBLIC_URL={answers['public_url']}",
        ]
    if answers.get("email_host"):
        lines += [
            "",
            "# --- shared mailbox ---------------------------------------------",
            f"FRONTDESK_EMAIL_IMAP_HOST={answers['email_host']}",
            f"FRONTDESK_EMAIL_SMTP_HOST={answers.get('smtp_host', answers['email_host'])}",
            f"FRONTDESK_EMAIL_USER={answers['email_user']}",
            "# An app password limited to mail, never the main account password.",
            f"FRONTDESK_EMAIL_PASSWORD={answers['email_password']}",
            f"FRONTDESK_EMAIL_ADDRESS={answers['email_address']}",
        ]
    else:
        lines += [
            "",
            "# --- shared mailbox (not configured) ---------------------------",
            "# Fill these in to answer a mailbox; see docs/customer-guide.md#email",
            "# FRONTDESK_EMAIL_IMAP_HOST=imap.example.com",
            "# FRONTDESK_EMAIL_USER=help@example.com",
            "# FRONTDESK_EMAIL_PASSWORD=",
            "# FRONTDESK_EMAIL_ADDRESS=help@example.com",
        ]
    return "\n".join(lines) + "\n"


def write_env(content: str) -> None:
    if ENV_PATH.exists():
        backup = ENV_PATH.with_suffix(".env.backup")
        shutil.copy2(ENV_PATH, backup)
        say(f"[ok] Existing .env kept as {backup.name}")
    ENV_PATH.write_text(content, encoding="utf-8")
    try:                                   # best effort; POSIX only
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    say(f"[ok] Wrote {ENV_PATH.name} (it holds secrets - do not share it)")


def build_index() -> None:
    say()
    say("Building the knowledge index from knowledge/ ...")
    result = subprocess.run([sys.executable, str(ROOT / "rag.py"), "--build"],
                            capture_output=True, text=True)
    say("[ok] " + (result.stdout.strip().splitlines() or ["index built"])[-1]
        if result.returncode == 0 else "[!] " + result.stderr.strip()[:200])


def run_doctor() -> int:
    say()
    say("Checking the configuration ...")
    say("-" * 66)
    result = subprocess.run([sys.executable, str(ROOT / "chat.py"), "--doctor"],
                            env={**os.environ, "FRONTDESK_SETUP": "1"})
    say("-" * 66)
    return result.returncode


# --------------------------------------------------------------------------


def interactive() -> dict:
    say("FrontDesk setup")
    say("=" * 66)
    say("A few questions, then this writes your configuration and checks it.")
    check_python()

    industry = ask("What kind of business is this for?", INDUSTRIES, 0)[0]
    market = ask("Which market do you serve?", [(m[0], f"{m[1]} ({m[2]})") for m in MARKETS], 0)
    region = market[0]

    say()
    model = ask_text("Local model to use", DEFAULT_MODEL)
    ready = ensure_model(model)

    say()
    public_url = ask_text(
        "Public address customers will reach, if you have one "
        "(blank for local only)", "")

    email_host = email_user = email_password = email_address = ""
    say()
    if ask_yes("Answer a shared mailbox by email as well?", False):
        email_host = ask_text("  IMAP host (for example imap.gmail.com)")
        smtp_host = ask_text("  SMTP host", email_host)
        email_address = ask_text("  The address customers write to")
        email_user = ask_text("  Mailbox username", email_address)
        say("  Use an app password limited to mail, not the main account password.")
        email_password = ask_text("  Mailbox password")
    else:
        smtp_host = ""

    return {
        "industry": industry, "region": region, "model": model,
        "auth_secret": secrets.token_urlsafe(32),
        "feedback_secret": secrets.token_urlsafe(32),
        "public_url": public_url, "email_host": email_host, "smtp_host": smtp_host,
        "email_user": email_user, "email_password": email_password,
        "email_address": email_address, "model_ready": ready,
    }


def finish(answers: dict) -> int:
    build_index()
    for key, value in (("FRONTDESK_AUTH_SECRET", answers["auth_secret"]),
                       ("FRONTDESK_FEEDBACK_SECRET", answers["feedback_secret"]),
                       ("FRONTDESK_REGION", answers["region"]),
                       ("FRONTDESK_WEB_PERSONA", answers["industry"])):
        os.environ[key] = value
    if answers["industry"] in GATED:
        os.environ["FRONTDESK_INDUSTRY"] = answers["industry"]
    run_doctor()

    say()
    say("Done. To start answering customers:")
    say()
    say("    python webchat.py --port 8766")
    say()
    say("Then open http://127.0.0.1:8766/ .")
    if answers.get("email_host"):
        say()
        say("To answer the mailbox as well, in a second window:")
        say()
        say("    python email_channel.py")
    say()
    say("Before customers use it:")
    say("  * Put your own service list, prices and policies in knowledge/,")
    say("    then run: python rag.py --build")
    say("  * Put an HTTPS reverse proxy in front of the web chat.")
    say("  * Read docs/customer-guide.md for channels and responsibilities.")
    if not answers.get("model_ready"):
        say("  * Download the model before going live; answers need it.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Set FrontDesk up for first use")
    parser.add_argument("--industry", choices=[code for code, _ in INDUSTRIES])
    parser.add_argument("--region", choices=[code for code, *_ in MARKETS])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--public-url", default="")
    parser.add_argument("--no-pull", action="store_true",
                        help="do not offer to download the model")
    arguments = parser.parse_args()

    try:
        if arguments.industry and arguments.region:
            check_python()
            answers = {
                "industry": arguments.industry, "region": arguments.region,
                "model": arguments.model,
                "auth_secret": secrets.token_urlsafe(32),
                "feedback_secret": secrets.token_urlsafe(32),
                "public_url": arguments.public_url,
                "email_host": "", "smtp_host": "", "email_user": "",
                "email_password": "", "email_address": "",
                "model_ready": ensure_model(arguments.model,
                                            offer_pull=not arguments.no_pull),
            }
        else:
            answers = interactive()
        write_env(build_env(answers))
        return finish(answers)
    except SetupError as exc:
        say(f"\n[x] {exc}")
        return 2
    except (KeyboardInterrupt, EOFError):
        say("\nStopped. Nothing was written.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
