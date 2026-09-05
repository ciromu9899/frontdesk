"""What differs between the United States and the United Kingdom.

Both markets are served in English, which makes it tempting to treat them as one.
They are not, and the differences are not cosmetic:

- **05/09/2026 is two different days.** A US reader sees 5 September; a UK reader
  sees 9 May. A booking confirmation that gets this wrong is a booking the
  customer does not turn up to.
- **999 is not 911.** A patient-facing assistant that tells somebody in Britain
  with stroke symptoms to dial 911 has done real harm. This is the reason this
  file exists at all.
- **Amounts are in dollars or in pounds**, and business systems reject the wrong one.
- **The regulator has a different name**, and a persona that cites the wrong one
  reads as though it was written for somewhere else - which it was.

So the region is one setting, and everything that follows from it is derived here
rather than written into seven persona files that would drift apart.

    FRONTDESK_REGION = us (default) | uk
"""

from __future__ import annotations

import os

DEFAULT = "us"


REGIONS = {
    "us": {
        "name": "the United States",
        "adjective": "US",
        "currency": "USD",
        "currency_symbol": "$",
        "date_format": "MM/DD/YYYY",
        "date_example": "09/05/2026 for September 5",
        "emergency_number": "911",
        "emergency_phrase": "call 911",
        "health_regulation": "HIPAA",
        "privacy_regulation": "state privacy law",
        "urgent_care": "the nurse line or urgent care",
        "timezone_note": "US time zones, with the zone named (ET, CT, MT, PT)",
        "spelling": "US spelling",
        "second_language": "Spanish",
        "number_format": "a point for decimals and a comma for thousands (1,234.50)",
    },
    "uk": {
        "name": "the United Kingdom",
        "adjective": "UK",
        "currency": "GBP",
        "currency_symbol": "£",
        "date_format": "DD/MM/YYYY",
        "date_example": "05/09/2026 for 5 September",
        "emergency_number": "999",
        "emergency_phrase": "call 999",
        "health_regulation": "UK GDPR and the Data Protection Act 2018",
        "privacy_regulation": "UK GDPR",
        "urgent_care": "NHS 111 or an urgent treatment centre",
        "timezone_note": "UK time (GMT or BST), stating which",
        "spelling": "British spelling",
        "second_language": "Welsh",
        "number_format": "a point for decimals and a comma for thousands (1,234.50)",
    },
    # The euro markets. 112 is the emergency number across the EU, and the
    # decimal comma is not cosmetic: a salon quoting 1.234,50 EUR and a bot
    # writing 1,234.50 disagree by three orders of magnitude.
    "de": {
        "name": "Germany",
        "adjective": "German",
        "currency": "EUR",
        "currency_symbol": "\u20ac",
        "date_format": "DD.MM.YYYY",
        "date_example": "05.09.2026 f\u00fcr den 5. September",
        "emergency_number": "112",
        "emergency_phrase": "call 112",
        "health_regulation": "the GDPR and German health-data rules",
        "privacy_regulation": "the GDPR (DSGVO)",
        "urgent_care": "the out-of-hours medical service on 116117",
        "timezone_note": "Central European Time (CET or CEST), stating which",
        "spelling": "German spelling",
        "second_language": "English",
        "number_format": "a comma for decimals and a point for thousands (1.234,50)",
    },
    "nl": {
        "name": "the Netherlands",
        "adjective": "Dutch",
        "currency": "EUR",
        "currency_symbol": "\u20ac",
        "date_format": "DD-MM-YYYY",
        "date_example": "05-09-2026 voor 5 september",
        "emergency_number": "112",
        "emergency_phrase": "call 112",
        "health_regulation": "the GDPR and Dutch health-data rules",
        "privacy_regulation": "the GDPR (AVG)",
        "urgent_care": "the huisartsenpost, the out-of-hours GP service",
        "timezone_note": "Central European Time (CET or CEST), stating which",
        "spelling": "Dutch spelling",
        "second_language": "English",
        "number_format": "a comma for decimals and a point for thousands (1.234,50)",
    },
    "fr": {
        "name": "France",
        "adjective": "French",
        "currency": "EUR",
        "currency_symbol": "\u20ac",
        "date_format": "DD/MM/YYYY",
        "date_example": "05/09/2026 pour le 5 septembre",
        "emergency_number": "112",
        "emergency_phrase": "call 112, or 15 for SAMU",
        "health_regulation": "the GDPR and French health-data rules",
        "privacy_regulation": "the GDPR (RGPD)",
        "urgent_care": "a m\u00e9decin de garde, or SAMU on 15",
        "timezone_note": "Central European Time (CET or CEST), stating which",
        "spelling": "French spelling",
        "second_language": "English",
        "number_format": "a comma for decimals and a space for thousands (1 234,50)",
    },
}

SUPPORTED = tuple(REGIONS)

# Every currency used by a configured region lives here so model prompts, demo
# data, and purchaser-owned connectors share one authoritative value.
SUPPORTED_CURRENCIES = tuple(dict.fromkeys(
    fact["currency"] for fact in REGIONS.values()))


def current() -> str:
    """The configured region, falling back to the default rather than failing."""
    value = os.environ.get("FRONTDESK_REGION", DEFAULT).strip().lower()
    return value if value in REGIONS else DEFAULT


def facts(region: str | None = None) -> dict:
    return REGIONS[region if region in REGIONS else current()]


def currency(region: str | None = None) -> str:
    return facts(region)["currency"]


def symbol(region: str | None = None) -> str:
    return facts(region)["currency_symbol"]


def preamble(region: str | None = None, lang: str = "en") -> str:
    """The conventions paragraph prepended to every persona.

    Kept out of the persona files themselves so that a new persona cannot forget
    it, and so that adding a third market does not mean editing seven files.
    """
    fact = facts(region)
    if lang == "de":
        return (
            f"Du bist die Rezeption f\u00fcr Kundinnen und Kunden in Deutschland. "
            f"Antworte auf Deutsch. Schreibe Datumsangaben als {fact['date_format']} "
            f"({fact['date_example']}). Betr\u00e4ge sind in {fact['currency']} und werden "
            f"mit {fact['currency_symbol']} geschrieben, mit Komma als Dezimaltrennzeichen "
            f"(1.234,50 \u20ac). Nenne bei Uhrzeiten die Zeitzone (MEZ oder MESZ). "
            f"Sieze die Kundschaft durchgehend; wechsle nie zum Du."
        )
    if lang == "nl":
        return (
            f"Je bent de receptie voor klanten in Nederland. Antwoord in het "
            f"Nederlands. Schrijf datums als {fact['date_format']} "
            f"({fact['date_example']}). Bedragen zijn in {fact['currency']} en worden "
            f"geschreven met {fact['currency_symbol']}, met een komma als decimaalteken "
            f"(1.234,50 \u20ac). Noem bij tijden de tijdzone (CET of CEST). "
            f"Spreek de klant consequent met u aan; wissel nooit naar je of jij."
        )
    if lang == "fr":
        return (
            f"Vous \u00eates l'accueil pour des clients en France. R\u00e9pondez en "
            f"fran\u00e7ais. \u00c9crivez les dates au format {fact['date_format']} "
            f"({fact['date_example']}). Les montants sont en {fact['currency']} et "
            f"s'\u00e9crivent avec {fact['currency_symbol']}, avec une virgule pour les "
            f"d\u00e9cimales (1 234,50 \u20ac). Pr\u00e9cisez le fuseau horaire pour toute heure. "
            f"Vouvoyez toujours le client ; ne passez jamais au tutoiement."
        )
    if lang == "es":
        return (
            f"Atiendes a clientes en Estados Unidos. Responde en español. "
            f"Escribe las fechas como {fact['date_format']} ({fact['date_example']}). "
            f"Los importes están en {fact['currency']} y usan {fact['currency_symbol']}. "
            f"Indica la zona horaria al dar una hora."
        )
    return (
        f"You are serving customers in {fact['name']}. "
        f"Respond in English, using {fact['spelling']}. "
        f"Write dates as {fact['date_format']} ({fact['date_example']}). "
        f"Amounts are in {fact['currency']}, written with {fact['currency_symbol']}. "
        f"Give times in {fact['timezone_note']}."
    )


def apply(text: str, region: str | None = None) -> str:
    """Fill the {region.*} placeholders a persona may use.

    A persona that needs a fact rather than a convention - the emergency number,
    the regulator - writes {region.emergency_phrase} and gets the right one.
    An unknown placeholder is left alone: silently deleting part of a system
    prompt would be worse than leaving something visibly wrong.
    """
    fact = facts(region)
    for key, value in fact.items():
        text = text.replace("{region." + key + "}", str(value))
    return text
