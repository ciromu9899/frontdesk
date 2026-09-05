"""Generate the sample images (SVG) used in the product copy. US, English only.

The terminal images come from actually running chat.py and capturing stdout. The
frame, the prompts, the tool notices and the confirmation dialog are all real
output; only the model's own prose is scripted, so that the images can be
regenerated without an API key.

    python docs/make_images.py
"""

from __future__ import annotations

import builtins
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
IMAGES = DOCS / "images"
sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import chat  # noqa: E402
import config as cfg  # noqa: E402
import paypal  # noqa: E402
import tools as tools_module  # noqa: E402
from providers import Chunk  # noqa: E402
from tools import ToolCall  # noqa: E402

# --------------------------------------------------------------------------
# PayPal stub
#
# Generating images must not run real payments, so only the HTTP layer is
# replaced.
# The JSON returned matches the response shape of PayPal Orders API v2.
# --------------------------------------------------------------------------

_PP_ORDER = "5O190127TN364715T"
_PP_CAPTURE = "3C679366HH908993F"
_pp_status = {"value": "CREATED"}


def _fake_paypal(method, path, headers, body):
    if path == "/v1/oauth2/token":
        return {"access_token": "stub", "expires_in": 3600}
    if method == "POST" and path == "/v2/checkout/orders":
        return {"id": _PP_ORDER, "status": "CREATED", "links": [
            {"rel": "approve",
             "href": f"https://www.sandbox.paypal.com/checkoutnow?token={_PP_ORDER}"},
        ]}
    if method == "GET" and path.endswith(_PP_ORDER):
        return {"id": _PP_ORDER, "status": _pp_status["value"], "purchase_units": [
            {"amount": {"currency_code": "USD", "value": "189.50"}, "payments": {}}]}
    if method == "POST" and path.endswith("/capture"):
        return {"id": _PP_ORDER, "status": "COMPLETED", "purchase_units": [
            {"payments": {"captures": [
                {"id": _PP_CAPTURE, "status": "COMPLETED",
                 "amount": {"currency_code": "USD", "value": "189.50"}}]}}]}
    if method == "POST" and "/refund" in path:
        return {"id": "1JU08902781691411", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "189.50"}}
    raise AssertionError(f"unexpected PayPal request: {method} {path}")


paypal._request = _fake_paypal
os.environ.setdefault("PAYPAL_CLIENT_ID", "stub")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "stub")


# --------------------------------------------------------------------------
# Capturing a real run
# --------------------------------------------------------------------------


class _Tty(io.StringIO):
    """A stand-in stdin whose isatty() is True, so the confirmation gate runs."""

    def isatty(self) -> bool:
        return True


class _Input:
    """A stand-in input(), echoing what was typed exactly as a terminal would."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        print(prompt, end="")
        answer = self.answers.pop(0) if self.answers else ""
        print(answer)
        return answer


def capture(persona: str, exchanges: list[tuple[str, list]],
            answers: list[str] | None = None) -> list[str]:
    """Drive a real Session and collect the lines it prints.

    exchanges is a list of (what the user says, the model responses for that turn).
    Only the model's side - its prose and its tool calls - is scripted; everything
    else on screen is genuine output.
    """
    tools_module.reset_store()
    config = cfg.Config(provider="anthropic", persona=persona).resolve()
    principal = auth.Principal("demo-operator", ("admin",))
    session = chat.Session(config, chat.Style(enabled=False), principal)

    # provider.name and .model stay real; only the stream is substituted.
    steps: list = []

    def scripted(system, history, tools):
        yield from (steps.pop(0) if steps else [Chunk("final")])

    session.provider.stream = scripted

    buffer = io.StringIO()
    original_stdin, original_input = sys.stdin, builtins.input
    sys.stdin = _Tty()
    builtins.input = _Input(answers or [])
    try:
        with redirect_stdout(buffer):
            print(chat.t("en", "startup", descriptor=session.descriptor(), persona=persona))
            print(chat.t("en", "startup_hint"))
            for user_text, script in exchanges:
                steps[:] = script
                print(f"{chat.t('en', 'you_prompt')}{user_text}")
                session.ask(user_text)
    finally:
        sys.stdin = original_stdin
        builtins.input = original_input
    tools_module.reset_store()
    return buffer.getvalue().rstrip("\n").split("\n")


# --------------------------------------------------------------------------
# SVG rendering
# --------------------------------------------------------------------------

CHAR_W = 8.4
LINE_H = 22
PAD = 22
TITLE_H = 38
FONT = "Consolas, 'DejaVu Sans Mono', monospace"

PALETTE = {
    "shell": "#e6edf3",
    "meta": "#6e7681",
    "user": "#4ec9d9",
    "bot": "#5ed97a",
    "tool": "#8b949e",
    "confirm": "#e3b341",
    "text": "#c9d1d9",
}


def classify(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("$"):
        return "shell"
    if stripped.startswith("—"):
        return "meta"
    if line.startswith("You>"):
        return "user"
    if line.startswith("Bot>"):
        return "bot"
    if "[confirm]" in line or "Run this" in line:
        return "confirm"
    if any(tag in line for tag in ("[tool]", "[result]", "[failed]")):
        return "tool"
    return "text"


def display_width(text: str) -> int:
    return sum(2 if ord(char) > 0x2E80 else 1 for char in text)


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(line: str, columns: int = 112) -> str:
    """Wrap long lines so the image does not stretch sideways."""
    if display_width(line) <= columns:
        return line
    out, width = [], 0
    for char in line:
        step = 2 if ord(char) > 0x2E80 else 1
        if width + step > columns - 1:
            break
        out.append(char)
        width += step
    return "".join(out) + "…"


def render_terminal(lines: list[str], title: str, path: Path) -> None:
    lines = [clip(line) for line in lines]
    columns = max((display_width(line) for line in lines), default=40)
    width = int(PAD * 2 + columns * CHAR_W) + 16
    height = int(TITLE_H + PAD + len(lines) * LINE_H + PAD)

    # Colours are set as attributes rather than in <style>, because <style> is
    # dropped by some paste targets (Office and friends), leaving everything black.
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#0d1117" '
        'stroke="#30363d"/>',
        f'<line x1="0" y1="{TITLE_H}" x2="{width}" y2="{TITLE_H}" stroke="#30363d"/>',
    ]
    for index, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        parts.append(f'<circle cx="{20 + index * 18}" cy="19" r="5.5" fill="{color}"/>')
    parts.append(
        f'<text x="{width / 2}" y="23" text-anchor="middle" font-size="12" '
        f'fill="#8b949e">{escape(title)}</text>'
    )

    y = TITLE_H + PAD + 12
    previous = "text"
    for line in lines:
        kind = classify(line)
        # When a line wraps, its continuation keeps the same colour - URLs, mostly
        if kind == "text" and previous == "bot" and line.strip():
            kind = "bot"
        previous = kind if line.strip() else "text"
        weight = "700" if kind in ("user", "bot") else "400"
        parts.append(
            f'<text x="{PAD}" y="{y:.0f}" font-size="14" fill="{PALETTE[kind]}" '
            f'font-weight="{weight}" xml:space="preserve">{escape(line)}</text>'
        )
        y += LINE_H
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}  ({width}x{height})")


# --------------------------------------------------------------------------
# Architecture diagram
# --------------------------------------------------------------------------


def render_architecture(path: Path) -> None:
    box = ('<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" '
           'stroke="{stroke}" stroke-width="1.5"/>')
    label = ('<text x="{x}" y="{y}" text-anchor="middle" font-size="{size}" '
             'fill="{fill}" font-weight="{weight}" '
             'font-family="Segoe UI, sans-serif">{text}</text>')
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="880" height="430" '
        'viewBox="0 0 880 430">',
        '<rect width="880" height="430" fill="#ffffff"/>',
        '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#57606a"/>'
        "</marker></defs>",
    ]

    # Customers / employees
    parts.append(box.format(x=30, y=170, w=140, h=70, fill="#f6f8fa", stroke="#8c959f"))
    parts.append(label.format(x=100, y=200, size=15, fill="#24292f", weight=600,
                              text="Customers"))
    parts.append(label.format(x=100, y=222, size=12, fill="#57606a", weight=400,
                              text="24/7, no queue"))

    # Frontdesk
    parts.append(box.format(x=250, y=110, w=250, h=190, fill="#eef6ff", stroke="#2f6feb"))
    parts.append(label.format(x=375, y=140, size=17, fill="#0a3069", weight=700,
                              text="Frontdesk"))
    for offset, text in enumerate(["Personas (behavior contracts)",
                                   "Tools + confirmation gate",
                                   "Vendor-neutral history"]):
        parts.append(box.format(x=270, y=160 + offset * 44, w=210, h=34,
                                fill="#ffffff", stroke="#a5c9ff"))
        parts.append(label.format(x=375, y=182 + offset * 44, size=12,
                                  fill="#0a3069", weight=400, text=text))

    # LLM providers
    parts.append(box.format(x=580, y=40, w=270, h=110, fill="#f6f8fa", stroke="#8c959f"))
    parts.append(label.format(x=715, y=68, size=14, fill="#24292f", weight=600,
                              text="LLM providers (switchable)"))
    for offset, text in enumerate(["Claude", "OpenAI", "Ollama"]):
        parts.append(box.format(x=600 + offset * 84, y=84, w=74, h=44,
                                fill="#ffffff", stroke="#c9d1d9"))
        parts.append(label.format(x=637 + offset * 84, y=111, size=13,
                                  fill="#24292f", weight=400, text=text))

    # Business systems
    parts.append(box.format(x=580, y=180, w=270, h=90, fill="#f6f8fa", stroke="#8c959f"))
    parts.append(label.format(x=715, y=208, size=14, fill="#24292f", weight=600,
                              text="Business systems"))
    parts.append(label.format(x=715, y=232, size=12, fill="#57606a", weight=400,
                              text="Orders, reservations, CRM, KB"))
    parts.append(label.format(x=715, y=252, size=11.5, fill="#8250df", weight=600,
                              text="Writes only after approval"))

    # Human agents
    parts.append(box.format(x=580, y=300, w=270, h=90, fill="#f6f8fa", stroke="#8c959f"))
    parts.append(label.format(x=715, y=328, size=14, fill="#24292f", weight=600,
                              text="Human agents"))
    parts.append(label.format(x=715, y=352, size=12, fill="#57606a", weight=400,
                              text="Receive only what needs them,"))
    parts.append(label.format(x=715, y=372, size=12, fill="#57606a", weight=400,
                              text="with a summary of what was tried"))

    arrows = [
        (170, 205, 250, 205, "requests", 210, 197),
        (500, 170, 580, 110, "inference", 542, 132),
        (500, 205, 580, 215, "tool calls", 540, 200),
        (500, 250, 580, 335, "handoff", 545, 300),
    ]
    for x1, y1, x2, y2, text, tx, ty in arrows:
        parts.append(
            f'<path d="M{x1},{y1} L{x2},{y2}" stroke="#57606a" stroke-width="1.6" '
            'fill="none" marker-end="url(#a)"/>'
        )
        parts.append(label.format(x=tx, y=ty, size=11.5, fill="#57606a", weight=400,
                                  text=text))

    parts.append(label.format(x=100, y=270, size=11.5, fill="#57606a", weight=400,
                              text="← answers, confirmations"))
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}  (880x430)")


# --------------------------------------------------------------------------
# PayPal payment flow diagram, for the product copy
# --------------------------------------------------------------------------

SANS = "Segoe UI, Helvetica Neue, Arial, sans-serif"


def render_paypal_flow(path: Path) -> None:
    """The payment flow in four steps, drawing attention to where card details stop and where money moves."""
    W, H = 1040, 650
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="{SANS}">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    ]

    def text(x, y, s, size=13, fill="#24292f", weight=400, anchor="start", family=None):
        family = f' font-family="{family}"' if family else ""
        return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
                f'font-weight="{weight}" text-anchor="{anchor}"{family}'
                f' xml:space="preserve">{escape(s)}</text>')

    def rect(x, y, w, h, fill, stroke, width=1.5, rx=10, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="{width}"{d}/>')

    # Header
    out.append(text(38, 54, "Taking a payment with PayPal", 27, "#0a3069", 700))
    out.append(text(38, 84, "Frontdesk completes the sale end to end — "
                            "without ever handling card data.", 15, "#57606a"))

    # Step cards
    steps = [
        ("1", "Create order", "Frontdesk → PayPal", "#2f6feb",
         ["Sends the amount and what", "it's for. Returns a link for", "the customer to approve."]),
        ("2", "Customer approves", "Customer → PayPal", "#2f6feb",
         ["Approval happens on PayPal's", "own page. Card details never", "reach Frontdesk."]),
        ("3", "Verify", "Frontdesk → PayPal", "#2f6feb",
         ["Frontdesk checks that the", "order really is APPROVED", "before going further."]),
        ("4", "Charge", "Frontdesk → PayPal", "#bf8700",
         ["Runs only after an explicit", "yes. Idempotent, so a retry", "cannot double-charge."]),
    ]
    card_y, card_h, card_w, gap = 112, 196, 220, 28
    for index, (num, title, actor, accent, body) in enumerate(steps):
        x = 38 + index * (card_w + gap)
        highlighted = index == 3
        out.append(rect(x, card_y, card_w, card_h,
                        "#fffdf5" if highlighted else "#ffffff",
                        "#bf8700" if highlighted else "#d0d7de",
                        2 if highlighted else 1.5))
        out.append(f'<circle cx="{x + 32}" cy="{card_y + 36}" r="16" fill="{accent}"/>')
        out.append(text(x + 32, card_y + 41, num, 15, "#ffffff", 700, "middle"))
        out.append(text(x + 58, card_y + 42, title, 16, "#0a3069", 700))
        out.append(text(x + 20, card_y + 76, actor, 11.5, accent, 600))
        for line_index, line in enumerate(body):
            out.append(text(x + 20, card_y + 104 + line_index * 19, line, 12.5, "#24292f"))
        if index < 3:  # Arrows between steps
            ax = x + card_w + 6
            ay = card_y + card_h / 2
            out.append(f'<path d="M{ax},{ay} l14,0" stroke="#8c959f" stroke-width="2"/>'
                       f'<path d="M{ax + 12},{ay - 5} l6,5 l-6,5 z" fill="#8c959f"/>')

    # The band marking where money moves
    band_y = 326
    out.append(rect(38, band_y, 220 * 3 + 28 * 3 - 28, 36, "#f6f8fa", "#8c959f", 1.2,
                    8, dash="5 4"))
    out.append(text(38 + (220 * 3 + 28 * 2) / 2, band_y + 24,
                    "No money has moved yet", 13.5, "#57606a", 600, "middle"))
    out.append(rect(782, band_y, 220, 36, "#fff4d6", "#bf8700", 1.6, 8))
    out.append(text(892, band_y + 24, "Money moves here", 13.5, "#7a5200", 700, "middle"))

    # The two guarantees
    guarantees = [
        (38, "#8250df", "Card data never enters the agent",
         ["Frontdesk holds an order ID, not a card. Nothing",
          "in the conversation can leak a payment credential,",
          "because the credential was never there."]),
        (532, "#bf8700", "A charge needs a human yes",
         ["Creating an order is free to do. Capturing it is not,",
          "so it sits behind the confirmation gate — and is",
          "declined outright when no one can approve."]),
    ]
    g_y = 388
    for gx, accent, title, lines in guarantees:
        out.append(rect(gx, g_y, 470, 96, "#ffffff", "#d0d7de", 1.5))
        out.append(f'<rect x="{gx}" y="{g_y}" width="4" height="96" rx="2" fill="{accent}"/>')
        out.append(text(gx + 20, g_y + 28, title, 14.5, "#0a3069", 700))
        for line_index, line in enumerate(lines):
            out.append(text(gx + 20, g_y + 50 + line_index * 17, line, 12, "#57606a"))

    # Real output; one line, for credibility
    t_y = 508
    out.append(text(38, t_y - 8, "What the operator actually sees", 12, "#57606a", 600))
    out.append(rect(38, t_y, 964, 106, "#0d1117", "#30363d", 1.5))
    terminal_lines = [
        ("  [confirm] Charge PayPal order 5O190127TN364715T (money moves now)", "#e3b341"),
        ("  Run this? [y/N] y", "#e3b341"),
        ('  [result] {"status": "COMPLETED", "captures": [{"capture_id": "3C679366HH908993F"…',
         "#8b949e"),
    ]
    for line_index, (line, color) in enumerate(terminal_lines):
        out.append(text(58, t_y + 32 + line_index * 24, line, 13.5, color, 400,
                        family="Consolas, monospace"))

    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}  ({W}x{H})")


# --------------------------------------------------------------------------
# Demo scripts. Only the model's own prose is scripted.
# --------------------------------------------------------------------------

REFUND_SCRIPT = [
    [Chunk("tool_call", tool_call=ToolCall("t1", "get_order_status",
                                           {"order_id": "A-88003"})),
     Chunk("final")],
    [Chunk("tool_call", tool_call=ToolCall("t2", "refund_paypal_capture",
                                           {"capture_id": "3C679366HH908993F",
                                            "amount": "189.50",
                                            "note": "Return within 30 days"})),
     Chunk("final")],
    [Chunk("text", "Refunded $189.50 to your PayPal account — it posts in 3-5 business "
                   "days. A prepaid return label is on its way to your email."),
     Chunk("final")],
]

LOOKUP_SCRIPT = [
    [Chunk("tool_call", tool_call=ToolCall("t1", "get_order_status",
                                           {"order_id": "A-88001"})),
     Chunk("final")],
    [Chunk("text", "Your headphones are in transit, arriving 08/21/2026."),
     Chunk("final")],
]

DENY_SCRIPT = [
    [Chunk("tool_call", tool_call=ToolCall("t1", "cancel_reservation",
                                           {"reservation_id": "R-2001"})),
     Chunk("final")],
    [Chunk("text", "I have not cancelled it. Let me know if you'd like to proceed."),
     Chunk("final")],
]


# -- PayPal: create -> buyer approves -> capture, behind the gate ----------

PAYPAL_BUY = [
    ("I'd like to buy the standing desk converter", [
        [Chunk("tool_call", tool_call=ToolCall(
            "p1", "create_paypal_order",
            {"amount": "189.50", "description": "Standing desk converter",
             "reference_id": "INV-4471"})),
         Chunk("final")],
        [Chunk("text", "That's $189.50. Approve on PayPal, then tell me when you're done:\n"
                       "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"),
         Chunk("final")],
    ]),
    ("approved it", [
        [Chunk("tool_call", tool_call=ToolCall(
            "p2", "get_paypal_order_status", {"order_id": _PP_ORDER})),
         Chunk("final")],
        [Chunk("tool_call", tool_call=ToolCall(
            "p3", "capture_paypal_order", {"order_id": _PP_ORDER})),
         Chunk("final")],
        [Chunk("text", "Charged $189.50. Capture ID 3C679366HH908993F — you'll see "
                       "\"PAYPAL *DESKCO\" on your statement."),
         Chunk("final")],
    ]),
]

# -- PayPal: what happens when a refund is not approved --------------------

PAYPAL_REFUND_DENY = [
    ("Refund my order", [
        [Chunk("tool_call", tool_call=ToolCall(
            "r1", "refund_paypal_capture",
            {"capture_id": _PP_CAPTURE, "note": "Customer request"})),
         Chunk("final")],
        [Chunk("text", "I haven't issued the refund. Nothing has been charged back."),
         Chunk("final")],
    ]),
]


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    print("generating sample images...")

    refund_ok = capture("ecommerce",
                        [("I want to return the standing desk converter, order A-88003",
                          REFUND_SCRIPT)],
                        answers=["y"])
    render_terminal(["$ frontdesk --persona ecommerce", ""] + refund_ok,
                    "frontdesk — a refund, issued only after approval",
                    IMAGES / "demo-refund.svg")

    lookup = capture("ecommerce", [("Where is order A-88001?", LOOKUP_SCRIPT)])
    render_terminal(["$ frontdesk --persona ecommerce", ""] + lookup,
                    "frontdesk — order lookup", IMAGES / "demo-en.svg")

    _pp_status["value"] = "APPROVED"
    pay = capture("ecommerce", PAYPAL_BUY, answers=["y"])
    render_terminal(["$ frontdesk --persona ecommerce", ""] + pay,
                    "frontdesk — PayPal checkout, charged only after approval",
                    IMAGES / "demo-paypal.svg")

    refund = capture("ecommerce", PAYPAL_REFUND_DENY, answers=["n"])
    render_terminal(["$ frontdesk --persona ecommerce", ""] + refund,
                    "frontdesk — refund not approved, so nothing moved",
                    IMAGES / "demo-paypal-refund-declined.svg")

    render_paypal_flow(IMAGES / "paypal-flow.svg")
    render_architecture(IMAGES / "architecture.svg")
    print("done.")


if __name__ == "__main__":
    main()
