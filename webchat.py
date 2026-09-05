"""Accessible customer web chat with durable tenant-scoped conversations."""

from __future__ import annotations

import argparse
import hmac
import html
import io
import json
import os
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import chat
import config as cfg
import feedback
import state
import handoffs
from channels import identity, linkedin, roles_for


MAX_BODY = 32_768
_SESSIONS: dict[str, chat.Session] = {}
_LOCK = threading.Lock()


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a web-chat tuning value without accepting runaway limits."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _embed_ancestors() -> str:
    allowed = []
    for value in os.environ.get("FRONTDESK_EMBED_ORIGINS", "'self'").split():
        if value == "'self'":
            allowed.append(value); continue
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username and
                not parsed.password and not parsed.path and not parsed.query and not parsed.fragment):
            allowed.append(value)
    return " ".join(allowed) or "'self'"


def _public_origin(host: str) -> str:
    configured = os.environ.get("FRONTDESK_PUBLIC_ORIGIN", "").rstrip("/")
    if configured:
        parsed = urllib.parse.urlsplit(configured)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username and
                not parsed.password and not parsed.path and not parsed.query and not parsed.fragment):
            return configured
    local = host.split(":", 1)[0] in {"localhost", "127.0.0.1", "::1"}
    return ("http://" if local else "https://") + host


# The languages the customer surface is offered in. Adding one here and to
# _LABELS is the whole change; nothing else in this file names a language.
SUPPORTED_LANGS = ("en", "es", "de", "nl", "fr")
# Shown in the language switcher. A speaker looking for their own language
# looks for its own name, not the English one.
LANGUAGE_NAMES = {"en": "English", "es": "Español", "de": "Deutsch",
                  "nl": "Nederlands", "fr": "Français"}


def _lang(value: str) -> str:
    """Settle any supplied language down to one we actually serve."""
    code = (value or "").strip().lower().split("-")[0]
    return code if code in SUPPORTED_LANGS else "en"


def _web_persona(lang: str) -> str:
    configured = os.environ.get("FRONTDESK_WEB_PERSONA", "").strip()
    if configured and configured in cfg.available_personas():
        return configured
    # A translated persona file is picked up by config.load_persona when one
    # exists; otherwise the English persona is used with an instruction to
    # answer in the customer's language.
    return "ecommerce-es" if lang == "es" else "ecommerce"


_LABELS = {
    "en": {"title": "Customer support", "intro": "Ask about services, appointments, or policies.",
           "label": "Type your message", "send": "Send", "sending": "Sending…",
           "error": "We could not reply. Please try again.", "skip": "Skip to chat",
           "language": "Language", "linkedin": "Sign in for private account actions",
           "human": "Talk to a person", "humanDone": "A human follow-up was requested.",
           "humanActive": "A human teammate has taken over. Your message is in the shared inbox.",
           "rate": "Was this helpful?", "thanks": "Thanks for your feedback.",
           "privacyLink": "Privacy rights",
           "privacy": "Do not share passwords, full card numbers, Social Security numbers, medical records, or financial-account credentials."},
    "es": {"title": "Ayuda al cliente", "intro": "Pregunta sobre servicios, citas o políticas.",
           "label": "Escribe tu mensaje", "send": "Enviar", "sending": "Enviando…",
           "error": "No pudimos responder. Inténtalo de nuevo.", "skip": "Ir al chat",
           "language": "Idioma", "linkedin": "Iniciar sesión para acciones privadas",
           "human": "Hablar con una persona", "humanDone": "Se solicitó ayuda humana.",
           "humanActive": "Una persona se ha hecho cargo. Tu mensaje está en la bandeja compartida.",
           "rate": "¿Te resultó útil?", "thanks": "Gracias por tus comentarios.",
           "privacyLink": "Derechos de privacidad",
           "privacy": "No compartas contraseñas, números completos de tarjeta, números de Seguro Social, expedientes médicos ni credenciales de cuentas financieras."},
    "de": {"title": "Kundenservice", "intro": "Fragen Sie nach Leistungen, Terminen oder Richtlinien.",
           "label": "Ihre Nachricht", "send": "Senden", "sending": "Wird gesendet…",
           "error": "Wir konnten nicht antworten. Bitte versuchen Sie es erneut.",
           "skip": "Zum Chat springen",
           "language": "Sprache", "linkedin": "Anmelden für private Kontoaktionen",
           "human": "Mit einer Person sprechen", "humanDone": "Eine Rückmeldung durch eine Person wurde angefordert.",
           "humanActive": "Eine Mitarbeiterin oder ein Mitarbeiter hat übernommen. Ihre Nachricht liegt im gemeinsamen Postfach.",
           "rate": "War das hilfreich?", "thanks": "Danke für Ihre Rückmeldung.",
           "privacyLink": "Datenschutzrechte",
           "privacy": "Teilen Sie hier keine Passwörter, vollständigen Kartennummern, Ausweis- oder Sozialversicherungsnummern, Gesundheitsdaten oder Bankzugangsdaten."},
    "nl": {"title": "Klantenservice", "intro": "Stel een vraag over behandelingen, afspraken of voorwaarden.",
           "label": "Typ uw bericht", "send": "Versturen", "sending": "Bezig met versturen…",
           "error": "We konden niet antwoorden. Probeer het opnieuw.",
           "skip": "Naar de chat",
           "language": "Taal", "linkedin": "Inloggen voor persoonlijke accountacties",
           "human": "Met een medewerker spreken", "humanDone": "Er is om een medewerker gevraagd.",
           "humanActive": "Een medewerker heeft het overgenomen. Uw bericht staat in de gedeelde inbox.",
           "rate": "Was dit nuttig?", "thanks": "Bedankt voor uw reactie.",
           "privacyLink": "Privacyrechten",
           "privacy": "Deel hier geen wachtwoorden, volledige kaartnummers, BSN, medische gegevens of bankgegevens."},
    "fr": {"title": "Service client", "intro": "Posez une question sur les prestations, les rendez-vous ou les conditions.",
           "label": "Votre message", "send": "Envoyer", "sending": "Envoi en cours…",
           "error": "Nous n'avons pas pu répondre. Merci de réessayer.",
           "skip": "Aller au chat",
           "language": "Langue", "linkedin": "Se connecter pour les actions liées au compte",
           "human": "Parler à une personne", "humanDone": "Une reprise par une personne a été demandée.",
           "humanActive": "Un membre de l'équipe a pris le relais. Votre message est dans la boîte partagée.",
           "rate": "Cette réponse vous a-t-elle aidé ?", "thanks": "Merci pour votre retour.",
           "privacyLink": "Vos droits",
           "privacy": "Ne communiquez pas de mots de passe, de numéros de carte complets, de numéro de sécurité sociale, de données de santé ni d'identifiants bancaires."},
}


def _labels(lang: str) -> dict[str, str]:
    return _LABELS.get(_lang(lang), _LABELS["en"])


_PRIVACY = {
    "en": ("Privacy rights", "Return to chat", (
        "This conversation is operated by the organisation that deployed Frontdesk. That organisation decides why messages are used, how long they are kept, and which external services are enabled.",
        "A self-hosted setup with Ollama processes conversations inside the operator's own deployment. Optional sign-in and enabled integrations may send data to their providers.",
        "Depending on applicable law you may request access, deletion, correction, objection or restriction through the operating organisation's verified privacy contact. Frontdesk records the request and requires identity verification before disclosing or deleting anything.",
        "Frontdesk carries no advertising trackers and honours Global Privacy Control signals.")),
    "es": ("Derechos de privacidad", "Volver al chat", (
        "Esta conversación es operada por la organización que implementó Frontdesk. Esa organización determina para qué se usan los mensajes, cuánto tiempo se conservan y qué servicios externos están habilitados.",
        "La configuración autohospedada con Ollama procesa las conversaciones dentro de la implementación del cliente. El inicio de sesión opcional y las integraciones externas habilitadas pueden enviar datos a sus respectivos proveedores.",
        "Según la ley aplicable, puedes solicitar acceso, eliminación, corrección, oposición o limitación a través del contacto de privacidad verificado de la organización operadora. Frontdesk registra la solicitud y exige verificación de identidad antes de divulgar o eliminar datos.",
        "Frontdesk no incluye rastreadores publicitarios y respeta las señales de Global Privacy Control.")),
    "de": ("Ihre Datenschutzrechte", "Zurück zum Chat", (
        "Dieser Chat wird von der Organisation betrieben, die Frontdesk einsetzt. Diese Organisation entscheidet, wofür Nachrichten verwendet werden, wie lange sie gespeichert bleiben und welche externen Dienste aktiviert sind. Sie ist die verantwortliche Stelle im Sinne der DSGVO.",
        "Bei einem selbst gehosteten Betrieb mit Ollama werden die Gespräche innerhalb der Installation des Betreibers verarbeitet. Optionale Anmeldung und aktivierte Integrationen können Daten an deren Anbieter übermitteln.",
        "Nach Artikel 15 bis 21 DSGVO können Sie Auskunft, Löschung, Berichtigung, Widerspruch oder Einschränkung verlangen. Wenden Sie sich dazu an die Datenschutzkontaktstelle der betreibenden Organisation. Frontdesk protokolliert die Anfrage und verlangt eine Identitätsprüfung, bevor Daten offengelegt oder gelöscht werden.",
        "Frontdesk enthält keine Werbe-Tracker und beachtet Global-Privacy-Control-Signale.")),
    "nl": ("Uw privacyrechten", "Terug naar de chat", (
        "Dit gesprek wordt uitgevoerd door de organisatie die Frontdesk heeft ingericht. Die organisatie bepaalt waarvoor berichten worden gebruikt, hoe lang ze worden bewaard en welke externe diensten zijn ingeschakeld. Zij is de verwerkingsverantwoordelijke onder de AVG.",
        "Bij een zelf gehoste opstelling met Ollama worden gesprekken binnen de installatie van de organisatie verwerkt. Optioneel inloggen en ingeschakelde koppelingen kunnen gegevens naar hun aanbieders sturen.",
        "Op grond van artikel 15 tot en met 21 AVG kunt u inzage, verwijdering, correctie, bezwaar of beperking vragen via het geverifieerde privacycontact van de organisatie. Frontdesk legt het verzoek vast en vraagt om identiteitscontrole voordat gegevens worden verstrekt of verwijderd.",
        "Frontdesk bevat geen advertentietrackers en respecteert Global Privacy Control-signalen.")),
    "fr": ("Vos droits sur vos données", "Retour au chat", (
        "Cette conversation est exploitée par l'organisation qui a déployé Frontdesk. C'est elle qui décide de l'usage des messages, de leur durée de conservation et des services externes activés. Elle est le responsable de traitement au sens du RGPD.",
        "Dans une installation auto-hébergée avec Ollama, les conversations sont traitées au sein du déploiement de l'organisation. La connexion facultative et les intégrations activées peuvent transmettre des données à leurs fournisseurs.",
        "En vertu des articles 15 à 21 du RGPD, vous pouvez demander l'accès, l'effacement, la rectification, l'opposition ou la limitation auprès du contact « données personnelles » vérifié de l'organisation. Frontdesk enregistre la demande et exige une vérification d'identité avant toute communication ou suppression.",
        "Frontdesk n'intègre aucun traceur publicitaire et respecte les signaux Global Privacy Control.")),
}


def _privacy_page(lang: str) -> bytes:
    lang = _lang(lang)
    title, back, paragraphs = _PRIVACY.get(lang, _PRIVACY["en"])
    body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f"<title>{html.escape(title)}</title></head>"
            f"<body><main><h1>{html.escape(title)}</h1>{body}"
            f'<p><a href="/?lang={lang}">{html.escape(back)}</a></p></main></body></html>'
            ).encode("utf-8")


_RATING_LABELS = {
    "en": ("Your feedback", "How was the answer?", "Not helpful", "Very helpful",
           "Thank you.", "Comment (optional)", "Send"),
    "es": ("Tu opinión", "¿Cómo estuvo la respuesta?", "Nada útil", "Muy útil",
           "Gracias.", "Comentario (opcional)", "Enviar"),
    "de": ("Ihre Rückmeldung", "Wie war die Antwort?", "Nicht hilfreich", "Sehr hilfreich",
           "Vielen Dank.", "Kommentar (optional)", "Senden"),
    "nl": ("Uw feedback", "Hoe was het antwoord?", "Niet nuttig", "Heel nuttig",
           "Bedankt.", "Opmerking (optioneel)", "Versturen"),
    "fr": ("Votre avis", "Cette réponse vous convient-elle ?", "Pas utile", "Très utile",
           "Merci.", "Commentaire (facultatif)", "Envoyer"),
}


def _rating_page(lang: str, message: str = "", token: str = "",
                 nonce: str = "") -> bytes:
    """Accessible no-script rating form suitable for links sent by email.

    No script, large targets and a plain form: this is opened from an email on a
    phone, in a browser that never loaded the chat page.
    """
    lang = _lang(lang)
    title, ask, low, high, thanks, note, send = _RATING_LABELS.get(
        lang, _RATING_LABELS["en"])
    body = f"<h1>{html.escape(title)}</h1>"
    if message:
        body += f"<p>{html.escape(message)}</p>"
    if token:
        buttons = "".join(
            f'<button name="score" value="{score}" type="submit" '
            f'aria-label="{score}/5">{score}</button>' for score in range(1, 6))
        body += (
            '<form method="post" action="/feedback">'
            f'<input type="hidden" name="t" value="{html.escape(token)}">'
            f"<fieldset><legend>{html.escape(ask)}</legend>"
            f'<p class="scale"><span>{html.escape(low)}</span>{buttons}'
            f"<span>{html.escape(high)}</span></p>"
            f'<label for="comment">{html.escape(note)}</label>'
            '<textarea id="comment" name="comment" maxlength="1000"></textarea>'
            f'<p><button type="submit" name="score" value="0" hidden>{html.escape(send)}</button></p>'
            "</fieldset></form>")
    else:
        body += f"<p>{html.escape(thanks)}</p>"
    style = ("body{margin:0;background:#f5f7f6;color:#142d2a;"
             "font:16px/1.55 system-ui,sans-serif}main{max-width:640px;margin:auto;"
             "padding:24px}fieldset{border:1px solid #d5dfdd;border-radius:12px;"
             "background:#fff;padding:16px}.scale{display:flex;gap:8px;"
             "align-items:center;flex-wrap:wrap}button{min-width:52px;min-height:52px;"
             "font:inherit;font-weight:700;border:1px solid #08645b;background:#fff;"
             "color:#08645b;border-radius:10px;cursor:pointer}button:hover{"
             "background:#08645b;color:#fff}textarea{width:100%;min-height:80px;"
             "margin-top:8px;padding:10px;font:inherit;border:1px solid #82938f;"
             "border-radius:8px}:focus-visible{outline:4px solid #ffbf47;"
             "outline-offset:3px}")
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title>"
            f'<style nonce="{nonce}">{style}</style></head>'
            f"<body><main>{body}</main></body></html>").encode("utf-8")


def _page(lang: str, csrf: str, nonce: str, *, embedded: bool = False) -> bytes:
    labels = _labels(lang)
    safe = {key: html.escape(value) for key, value in labels.items()}
    language_links = " ".join(
        f'<a href="/?lang={code}" hreflang="{code}">{html.escape(LANGUAGE_NAMES[code])}</a>'
        for code in SUPPORTED_LANGS if code != lang)
    embed_class = " embedded" if embedded else ""
    document = f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe['title']} · Frontdesk</title>
<style nonce="{nonce}">:root{{--bg:#f5f7f6;--panel:#fff;--ink:#142d2a;--muted:#536966;--brand:#08645b;--focus:#ffbf47}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,sans-serif}}
.skip{{position:absolute;left:-9999px}}.skip:focus{{left:12px;top:12px;background:#fff;padding:10px;z-index:2}}
main{{max-width:760px;margin:auto;padding:24px}}header{{display:flex;justify-content:space-between;gap:16px;align-items:start}}
.chat{{background:var(--panel);border:1px solid #d5dfdd;border-radius:16px;box-shadow:0 5px 24px #153d3814;overflow:hidden}}
#log{{height:min(58vh,520px);overflow:auto;padding:20px}}.message{{max-width:82%;padding:11px 14px;border-radius:14px;margin:10px 0;white-space:pre-wrap}}
.user{{margin-left:auto;background:#d9f0ec}}.bot{{background:#eef1f0}}form{{border-top:1px solid #d5dfdd;padding:16px}}
label{{font-weight:650;display:block}}textarea{{width:100%;min-height:90px;margin:7px 0;padding:12px;font:inherit;border:1px solid #82938f;border-radius:8px}}
button{{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:11px 20px;font:inherit;font-weight:700;cursor:pointer}}button.secondary{{background:#fff;color:var(--brand);border:1px solid var(--brand)}}
a{{color:#075c54}}.linkedin{{display:inline-block;margin:.25rem 0 1rem;font-weight:700}}:focus-visible{{outline:4px solid var(--focus);outline-offset:3px}}.muted{{color:var(--muted)}}
.actions{{display:flex;gap:8px;flex-wrap:wrap}}#rating[hidden]{{display:none}}#rating button{{min-width:44px;padding:8px}}
@media(max-width:520px){{main{{padding:12px}}#log{{height:55vh}}.message{{max-width:94%}}header{{display:block}}}}
body.embedded main{{padding:8px}}body.embedded header h1{{font-size:1.25rem}}body.embedded #log{{height:52vh}}
@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important}}}}</style></head><body class="{embed_class.strip()}">
<a class="skip" href="#message">{safe['skip']}</a><main><header><div><h1>{safe['title']}</h1><p>{safe['intro']}</p></div>
<nav aria-label="{safe['language']}">{language_links}</nav></header>
<a class="linkedin" href="/linkedin/start">{safe['linkedin']}</a>
<section class="chat" aria-label="Chat"><div id="log" role="log" aria-live="polite" aria-relevant="additions"></div>
<form id="form"><label for="message">{safe['label']}</label><textarea id="message" maxlength="4000" required aria-describedby="privacy"></textarea>
<p id="privacy" class="muted">{safe['privacy']} <a href="/privacy?lang={lang}">{safe['privacyLink']}</a></p><div class="actions"><button id="send" type="submit">{safe['send']}</button><button id="human" type="button" class="secondary">{safe['human']}</button></div><div id="rating" hidden><p>{safe['rate']}</p><div class="actions">{' '.join(f'<button type="button" data-rating="{rating}" aria-label="{rating} out of 5">{rating}</button>' for rating in range(1,6))}</div></div><p id="status" role="status" class="muted"></p></form></section></main>
<script nonce="{nonce}">const csrf={json.dumps(csrf)},lang={json.dumps(lang)},L={json.dumps(labels, ensure_ascii=False)};
const form=document.querySelector('#form'),box=document.querySelector('#message'),log=document.querySelector('#log'),button=document.querySelector('#send'),status=document.querySelector('#status'),human=document.querySelector('#human'),rating=document.querySelector('#rating');
function add(text,kind){{const p=document.createElement('p');p.className='message '+kind;p.textContent=text;log.appendChild(p);log.scrollTop=log.scrollHeight;}}
let lastAgentMessage=0;async function pollAgent(){{try{{const response=await fetch('/api/messages?after='+lastAgentMessage,{{credentials:'same-origin'}});if(!response.ok)return;const data=await response.json();for(const message of data.messages){{add(message.body,'bot');lastAgentMessage=Math.max(lastAgentMessage,message.created_at);}}}}catch(e){{}}}}
form.addEventListener('submit',async e=>{{e.preventDefault();const text=box.value.trim();if(!text)return;add(text,'user');box.value='';button.disabled=true;status.textContent=L.sending;
try{{const response=await fetch('/api/chat',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-CSRF':csrf}},body:JSON.stringify({{message:text,lang}})}});const data=await response.json();if(!response.ok)throw new Error();add(data.reply,'bot');rating.hidden=false;}}
catch(e){{add(L.error,'bot');}}finally{{button.disabled=false;status.textContent='';box.focus();}}}});
human.addEventListener('click',async()=>{{human.disabled=true;const response=await fetch('/api/handoff',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-CSRF':csrf}},body:JSON.stringify({{lang}})}});if(response.ok)status.textContent=L.humanDone;else human.disabled=false;}});
rating.addEventListener('click',async e=>{{const value=Number(e.target.dataset.rating);if(!value)return;const response=await fetch('/api/csat',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-CSRF':csrf}},body:JSON.stringify({{rating:value}})}});if(response.ok){{rating.hidden=true;status.textContent=L.thanks;}}}});
setInterval(pollAgent,3000);
box.addEventListener('keydown',e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();form.requestSubmit();}}}});</script></body></html>"""
    return document.encode("utf-8")


class WebChatHandler(BaseHTTPRequestHandler):
    server_version = "FrontdeskWebChat/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _cookies(self) -> SimpleCookie:
        return SimpleCookie(self.headers.get("Cookie", ""))

    def _cookie(self, name: str) -> str:
        item = self._cookies().get(name)
        return item.value if item else ""

    def _secure(self) -> str:
        host = self.headers.get("Host", "").split(":")[0]
        return "" if host in {"localhost", "127.0.0.1", "::1"} else "; Secure"

    def _send(self, status: int, body: bytes, content_type: str,
              headers: list[tuple[str, str]] | None = None, nonce: str = "",
              embedded: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if not embedded:
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.headers.get("Sec-GPC") == "1":
            self.send_header("X-Frontdesk-GPC", "honored")
        frame_ancestors = _embed_ancestors() if embedded else "'none'"
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'nonce-%s'; script-src 'nonce-%s'; connect-src 'self'; form-action 'self'; frame-ancestors %s" % (nonce, nonce, frame_ancestors))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/api/messages":
            session_key = self._cookie("fd_web_session")
            if not session_key:
                self._json(400, {"error": "session required"}); return
            principal = self._principal(session_key)
            try:
                after = float(urllib.parse.parse_qs(query).get("after", ["0"])[-1])
            except ValueError:
                after = 0
            messages = [{"body": row["body"], "created_at": row["created_at"]}
                        for row in state.list_messages(principal.tenant_id, session_key)
                        if row["sender_kind"] == "agent" and row["created_at"] > after]
            self._json(200, {"messages": messages}); return
        if path == "/embed.js":
            source = _public_origin(self.headers.get("Host", "localhost"))
            script = ("(()=>{const s=document.currentScript,o=s.dataset.origin||" + json.dumps(source) + ";"
                      "const b=document.createElement('button');b.textContent=s.dataset.label||'Chat';"
                      "b.setAttribute('aria-label',b.textContent);b.style.cssText='position:fixed;right:20px;bottom:20px;z-index:2147483646;padding:12px 18px;border:0;border-radius:999px;background:#08645b;color:white;font:600 16px system-ui';"
                      "const f=document.createElement('iframe');f.title='Customer support chat';f.src=o+'/widget?lang='+(s.dataset.lang||'en');"
                      "f.style.cssText='display:none;position:fixed;right:20px;bottom:76px;width:min(390px,calc(100vw - 24px));height:min(650px,calc(100vh - 100px));border:0;border-radius:16px;box-shadow:0 12px 44px #0004;z-index:2147483647;background:white';"
                      "b.onclick=()=>{const open=f.style.display!=='none';f.style.display=open?'none':'block';b.setAttribute('aria-expanded',String(!open));};"
                      "document.body.append(f,b);})();").encode("utf-8")
            self._send(200, script, "application/javascript; charset=utf-8", embedded=True)
            return
        if path == "/linkedin/start":
            self._linkedin_start()
            return
        if path == "/feedback":
            nonce = secrets.token_urlsafe(16)
            parameters = urllib.parse.parse_qs(query)
            lang = _lang(parameters.get("lang", [""])[-1])
            self._feedback_get(parameters, lang, nonce)
            return
        if path == "/privacy":
            nonce = secrets.token_urlsafe(16)
            lang = _lang(urllib.parse.parse_qs(query).get("lang", [""])[-1])
            self._send(200, _privacy_page(lang), "text/html; charset=utf-8", nonce=nonce); return
        if path not in {"/", "/widget"}:
            self._send(404, b"not found", "text/plain; charset=utf-8"); return
        lang = _lang(urllib.parse.parse_qs(query).get("lang", [""])[-1])
        session_id = self._cookie("fd_web_session") or secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(24); nonce = secrets.token_urlsafe(16)
        secure = self._secure()
        headers = [("Set-Cookie", f"fd_web_session={session_id}; Path=/; HttpOnly; SameSite=Strict{secure}"),
                   ("Set-Cookie", f"fd_web_csrf={csrf}; Path=/; HttpOnly; SameSite=Strict{secure}")]
        embedded = path == "/widget"
        self._send(200, _page(lang, csrf, nonce, embedded=embedded),
                   "text/html; charset=utf-8", headers, nonce, embedded=embedded)

    def _linkedin_start(self) -> None:
        session_id = self._cookie("fd_web_session") or secrets.token_urlsafe(24)
        tenant = os.environ.get("FRONTDESK_WEB_TENANT_ID", "web:default")
        try:
            location = linkedin.authorization_url(
                "web", session_id, session_id, tenant_id=tenant)
        except linkedin.LinkedInError:
            self._send(503, b"LinkedIn sign-in is unavailable.",
                       "text/plain; charset=utf-8")
            return
        secure = self._secure()
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Set-Cookie",
                         f"fd_web_session={session_id}; Path=/; HttpOnly; SameSite=Strict{secure}")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/feedback":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            nonce = secrets.token_urlsafe(16)
            parameters = urllib.parse.parse_qs(parsed.query)
            lang = _lang(parameters.get("lang", [""])[-1])
            if length < 1 or length > MAX_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           _rating_page(lang, "That feedback was too long.", nonce=nonce),
                           "text/html; charset=utf-8", nonce=nonce)
                return
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace"))
            self._feedback_record((form.get("t") or [""])[0],
                                  (form.get("score") or [""])[0],
                                  (form.get("comment") or [""])[0][:1000],
                                  lang, nonce)
            return
        if path not in {"/api/chat", "/api/handoff", "/api/csat"}:
            self._json(404, {"error": "not found"}); return
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = -1
        if length < 1 or length > MAX_BODY:
            self._json(413, {"error": "invalid body"}); return
        supplied = self.headers.get("X-CSRF", "")
        if not supplied or not hmac.compare_digest(supplied, self._cookie("fd_web_csrf")):
            self._json(403, {"error": "csrf"}); return
        try: payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid json"}); return
        session_key = self._cookie("fd_web_session")
        if not session_key:
            self._json(400, {"error": "session required"}); return
        principal = self._principal(session_key)
        if path == "/api/csat":
            try:
                result = state.record_csat(principal.tenant_id, session_key,
                                           int(payload.get("rating", 0)),
                                           str(payload.get("comment", "")))
            except (TypeError, ValueError):
                self._json(400, {"error": "rating must be 1-5"}); return
            self._json(201, result); return
        if path == "/api/handoff":
            ticket = handoffs.request(
                "Customer requested a human from web chat.", requested_by=principal.subject,
                tenant_id=principal.tenant_id, channel="web", thread_key=session_key,
                reason="customer_requested")
            state.record_metric(principal.tenant_id, "handoff_requested",
                                conversation_key=session_key)
            self._json(201, {"handoff_id": ticket["id"], "status": "open"}); return
        text = str(payload.get("message", "")).strip()[:4000]
        lang = _lang(str(payload.get("lang", "")))
        if not text:
            self._json(400, {"error": "message required"}); return
        try:
            state.append_message(principal.tenant_id, session_key, "customer", text,
                                 sender_id=principal.subject, channel="web")
            started = time.monotonic()
            reply = self._ask(session_key, text, lang)
            state.append_message(principal.tenant_id, session_key, "assistant", reply,
                                 sender_id="frontdesk", channel="web")
            state.record_metric(principal.tenant_id, "assistant_reply",
                                conversation_key=session_key,
                                value=time.monotonic() - started,
                                dimensions={"language": lang})
        except Exception:
            self._json(503, {"error": "unavailable"}); return
        self._json(200, {"reply": reply, "conversation_key": session_key,
                         "authenticated": "guest" not in principal.roles})

    def _feedback_get(self, query: dict, lang: str, nonce: str) -> None:
        token = (query.get("t") or [""])[0]
        score = (query.get("score") or [""])[0]
        if not token:
            self._send(HTTPStatus.BAD_REQUEST,
                       _rating_page(lang, "Missing rating link.", nonce=nonce),
                       "text/html; charset=utf-8", nonce=nonce)
            return
        if score:
            self._feedback_record(token, score, "", lang, nonce)
            return
        try:
            feedback.verify(token)
        except feedback.FeedbackError as exc:
            self._send(HTTPStatus.BAD_REQUEST,
                       _rating_page(lang, str(exc), nonce=nonce),
                       "text/html; charset=utf-8", nonce=nonce)
            return
        self._send(HTTPStatus.OK, _rating_page(lang, token=token, nonce=nonce),
                   "text/html; charset=utf-8", nonce=nonce)

    def _feedback_record(self, token: str, score: str, comment: str,
                         lang: str, nonce: str) -> None:
        try:
            feedback.submit(token, int(score), comment)
        except (feedback.FeedbackError, ValueError) as exc:
            self._send(HTTPStatus.BAD_REQUEST,
                       _rating_page(lang, str(exc), nonce=nonce),
                       "text/html; charset=utf-8", nonce=nonce)
            return
        self._send(HTTPStatus.OK, _rating_page(lang, nonce=nonce),
                   "text/html; charset=utf-8", nonce=nonce)

    def _principal(self, session_key: str) -> auth.Principal:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return auth.authenticate_token(authorization[7:])
        tenant = os.environ.get("FRONTDESK_WEB_TENANT_ID", "web:default")
        record = identity.recall("web", session_key, tenant_id=tenant)
        if record is not None:
            return auth.Principal(
                str(record.get("subject", "")) or f"web:{session_key}",
                roles_for("web", str(record.get("trust", "public"))), tenant)
        return auth.Principal(f"web:{session_key}", ("guest",), tenant)

    def _ask(self, session_key: str, text: str, lang: str) -> str:
        if not session_key:
            raise ValueError("session cookie required")
        principal = self._principal(session_key)
        current_thread = state.get_thread(principal.tenant_id, session_key)
        if current_thread and current_thread.get("status") == "in_progress":
            return _labels(lang)["humanActive"]
        key = f"web:{session_key}"
        cache_key = f"{principal.tenant_id}:{key}:{lang}"
        with _LOCK:
            session = _SESSIONS.get(cache_key)
            if session is None:
                configuration = cfg.Config(
                    provider=os.environ.get("FRONTDESK_WEB_PROVIDER", "auto"),
                    persona=_web_persona(lang),
                    ui_lang=lang, use_tools=True,
                    max_tokens=_bounded_env_int(
                        "FRONTDESK_CHAT_MAX_TOKENS", 256, 64, 4096),
                    max_history_chars=_bounded_env_int(
                        "FRONTDESK_CHAT_HISTORY_CHARS", 60_000, 8_000, 200_000)).resolve()
                session = chat.Session(configuration, chat.Style(False), principal,
                                       out=io.StringIO(), context={"channel": "web",
                                       "thread_key": session_key, "tenant_id": principal.tenant_id})
                saved = state.load_session(principal.tenant_id, key)
                if saved:
                    session.session_id = saved["session_id"]
                    session.history = chat.Session.deserialize_history(saved["history"])
                _SESSIONS[cache_key] = session
            elif (session.principal.subject, session.principal.roles) != (
                    principal.subject, principal.roles):
                session.principal = principal
        reply = session.ask(text)
        state.save_session(principal.tenant_id, key, session.durable_payload())
        return reply or _labels(lang)["error"]

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Frontdesk customer web chat")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
    parser.add_argument("--port", type=int, default=8766); args = parser.parse_args()
    cfg.load_dotenv()
    server = ThreadingHTTPServer((args.host, args.port), WebChatHandler)
    print(f"Frontdesk Web Chat: http://{args.host}:{args.port}")
    print("LinkedIn sign-in: configured" if linkedin.configured() else
          "LinkedIn sign-in: optional and not configured (guest chat remains available)")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
