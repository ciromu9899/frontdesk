"""Configuration loading.

Precedence: command line, then environment, then the .env file, then defaults.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PERSONA_DIR = ROOT / "personas"
# Transport-specific safety profiles are not customer industries. Keep them
# loadable by exact name without inflating the industry selector.
SYSTEM_PERSONAS = {"github-support"}


def application_data_dir() -> Path:
    """Return a durable, per-user data directory for packaged Windows builds."""
    configured = os.environ.get("FRONTDESK_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ShellieSoftwareTools" / "FrontDesk" / "data"
    return ROOT / "data"


DATA_DIR = application_data_dir()

# Default model per provider.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4o-mini",
    "ollama": "qwen3:8b",
    "llamacpp": "frontdesk-local",
    "echo": "dry-run",  # dry run; calls no API
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def load_dotenv(path: Path | None = None) -> None:
    """Load .env into the environment. Existing variables are left alone."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def detect_provider() -> str:
    """Use the local provider unless an operator explicitly selects another one.

    A credential left in the process environment must not silently change where
    customer conversations are sent. Cloud providers remain available through
    ``--provider`` and the channel-specific provider settings.
    """
    return "ollama"


@dataclass
class Config:
    provider: str = "auto"
    model: str | None = None
    persona: str = "default"
    effort: str = "medium"
    max_tokens: int = 64000
    temperature: float | None = None
    show_thinking: bool = False
    base_url: str | None = None
    no_color: bool = False
    use_tools: bool = True
    auto_approve: bool = False
    max_steps: int = 8
    # Conversation history ceiling, in characters. Anything over it is dropped
    # from the oldest end. Zero disables trimming.
    max_history_chars: int = 200_000
    ui_lang: str = "en"

    def resolve(self) -> "Config":
        """Settle provider, model and base_url into the values actually used."""
        if self.provider == "auto":
            self.provider = detect_provider()
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(
                f"Unknown provider: {self.provider} (expected {'/'.join(DEFAULT_MODELS)})"
            )
        env_model = os.environ.get(f"{self.provider.upper()}_MODEL")
        self.model = self.model or env_model or DEFAULT_MODELS[self.provider]
        if self.base_url is None:
            self.base_url = os.environ.get(f"{self.provider.upper()}_BASE_URL")
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(
                f"effort must be one of {'/'.join(EFFORT_LEVELS)}: {self.effort}"
            )
        from i18n import LANGUAGES  # imported late to avoid a circular import

        if self.ui_lang not in LANGUAGES:
            raise ValueError(f"ui-lang must be one of {'/'.join(LANGUAGES)}: {self.ui_lang}")
        return self


def available_personas() -> list[str]:
    if not PERSONA_DIR.exists():
        return []
    suffixes = tuple(f"-{code}" for code in _ANSWER_IN)
    return sorted(p.stem for p in PERSONA_DIR.glob("*.md")
                  if not p.stem.endswith(suffixes) and p.stem not in SYSTEM_PERSONAS)


_ANSWER_IN = {
    "es": ("Las instrucciones anteriores están en inglés; se aplican igual. "
           "Responde siempre en español, incluidas las negativas."),
    "de": ("Die obigen Anweisungen sind auf Englisch; sie gelten unverändert. "
           "Antworte immer auf Deutsch, auch bei Ablehnungen."),
    "nl": ("De bovenstaande instructies staan in het Engels; ze gelden onverkort. "
           "Antwoord altijd in het Nederlands, ook bij weigeringen."),
    "fr": ("Les instructions ci-dessus sont en anglais ; elles s'appliquent telles "
           "quelles. Réponds toujours en français, y compris pour les refus."),
}


def load_persona(name: str, region: str | None = None, lang: str = "en") -> str:
    """Load a persona and settle it into one market.

    The conventions paragraph is prepended rather than written into each file:
    seven copies of "dates as MM/DD/YYYY" is seven chances to disagree, and a new
    persona cannot forget what it never had to remember. Facts that differ - the
    emergency number, the regulator - are filled from {region.*} placeholders.
    """
    import regions

    path = PERSONA_DIR / f"{name}.md"
    reinforce = ""
    base_lang = lang.split("-")[0]
    if base_lang != "en" and not name.endswith(f"-{base_lang}"):
        # A translated persona is used when one exists. Where it does not, the
        # English persona is used with an instruction to answer in the customer's
        # language: one file per industry per language is one more place for the
        # boundaries an industry must hold to drift apart.
        translated = PERSONA_DIR / f"{name}-{base_lang}.md"
        if translated.exists():
            path = translated
        elif path.exists():
            reinforce = "\n\n" + _ANSWER_IN.get(base_lang, "")
    if not path.exists():
        names = ", ".join(sorted(set(available_personas()) | SYSTEM_PERSONAS)) or "(none)"
        print(f"Persona '{name}' not found. Available: {names}", file=sys.stderr)
        raise SystemExit(2)
    body = regions.apply(path.read_text(encoding="utf-8").strip(), region)
    return regions.preamble(region, lang) + "\n\n" + body + reinforce
