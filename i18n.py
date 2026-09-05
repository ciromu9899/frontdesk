"""Everything the product says out loud.

Frontdesk ships to the US market, so English is the only shipped language. The
locale mechanism stays because US customer service reaches Spanish-speaking
households often enough to matter: adding "es" to MESSAGES and a matching persona
to personas/ is the whole job.

This covers both what the end user sees - confirmation prompts, tool notices,
errors - and what the tools hand back to the model, such as the reason an action
was declined. Letting the second drift out of English destabilises the language
the model replies in.
"""

from __future__ import annotations

BASE_LANGUAGE = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # I/O
        "you_prompt": "You> ",
        "cont_prompt": "...  ",
        "bot_prefix": "Bot> ",
        "thinking_header": "\n[thinking]",
        "error": "Error: {message}",
        "interrupted": "\n[interrupted]",
        "truncated": "\n[output truncated: reached max_tokens]",
        "history_trimmed":
            "  [dropped {count} earlier turns to stay within the context budget]\n",
        "context_too_long":
            "The conversation is too long for this model. Use /reset to start a "
            "fresh one, or raise --max-history-chars if the model can take it.",
        "max_steps_reached": "  [stopped after {count} tool calls]\n",
        # Tools
        "tool_run": "  [tool] {summary}",
        "label_ok": "result",
        "label_error": "failed",
        "tool_output": "  [{label}] {content}\n",
        "tool_broken_args": "  [tool] {name}: malformed arguments\n",
        "tool_broken_args_model":
            "The arguments were not valid JSON. Call the tool again with valid JSON.",
        "tool_declined": "  [tool] skipped\n",
        "tool_declined_model":
            "The user did not approve this action. Nothing was executed.",
        # Text the tools return to the model
        "tool_err_order_not_found": "Order {order_id} was not found.",
        "tool_err_reservation_not_found": "Reservation {reservation_id} was not found.",
        "tool_err_already_cancelled":
            "Reservation {reservation_id} is already cancelled and cannot be changed.",
        "tool_err_bad_date": "new_date must be in YYYY-MM-DD format.",
        "tool_err_need_field": "Either new_date or new_time is required.",
        "tool_note_already_cancelled": "It was already cancelled.",
        "tool_err_not_yours":
            "That record was not found on your account.",
        "tool_err_unknown": "Unknown tool: {name}",
        "tool_err_unexpected": "Unexpected error while running the tool: {message}",
        "tool_none": "no matches",
        # Confirmation prompt
        "approve_skipped": "  [auto-approved] {summary}",
        "approve_blocked": "  [blocked: cannot confirm] {summary}",
        "approve_ask": "  [confirm] {summary}",
        "approve_prompt": "  Run this? [y/N] ",
        "approve_remote_wait": "  [waiting for approval] {summary}",
        "approve_remote_granted": "  [approved by {approver}] {summary}",
        "approve_remote_refused": "  [{outcome}] {summary}",
        "sum_change_reservation": "Change reservation {reservation_id} to {date} {time}",
        "sum_cancel_reservation": "Cancel reservation {reservation_id}",
        "date_unchanged": "(date unchanged)",
        "time_unchanged": "(time unchanged)",
        # Startup / shutdown
        "startup": "— {descriptor} / persona={persona}",
        "startup_hint": "— /help for commands, /exit to quit\n",
        "goodbye": "Goodbye.",
        "config_error": "Configuration error: {message}",
        "cannot_start": "Cannot start: {message}",
        # Commands
        "history_empty": "  (no history)\n",
        "history_reset": "  History cleared.\n",
        "saved": "  Saved: {path}\n",
        "persona_list": "  Available: {names}\n",
        "persona_changed": "  Switched to persona {name}; history cleared.\n",
        "provider_current": "  Current: {name} (anthropic / openai / ollama / echo)\n",
        "provider_switched": "  Switched to {descriptor}.\n",
        "provider_failed": "  Switch failed: {message}\n",
        "model_current": "  Current: {model}\n",
        "model_changed": "  Model set to {model}.\n",
        "model_failed": "  Change failed: {message}\n",
        "effort_current": "  Current: {effort} ({levels})\n",
        "effort_changed": "  Effort set to {effort}.\n",
        "thinking_state": "  Show thinking: {state}\n",
        "tools_state": "  Tools: {state}",
        "tools_toggled": "  Tools: {state}\n",
        "tool_tag_read": "[read]   ",
        "tool_tag_confirm": "[confirm]",
        "store_reset": "  Demo data reset.\n",
        "unknown_command": "  Unknown command: {command} (see /help)\n",
        "info_line1": "  {descriptor}  persona={persona}",
        "info_line2":
            "  History: {turns} turns / thinking: {thinking} / auto-approve: {approve}\n",
        "on": "ON",
        "off": "OFF",
        "echo_reply": ("[dry run] received: {last}\n"
                       "persona {system} chars / {turns} turns / "
                       "{tools} tools / model={model}"),
        "help": """
Commands:
  /help              this help
  /reset             clear conversation history (persona is kept)
  /history           show the conversation so far
  /save [path]       save the conversation as JSON (default: ./transcript-<time>.json)
  /persona [name]    switch persona (no argument lists them)
  /provider [name]   switch anthropic / openai / ollama / echo
  /model [id]        switch model (no argument shows the current one)
  /effort [level]    low / medium / high / xhigh / max (Claude only)
  /thinking          toggle display of the thinking summary
  /tools [on|off]    enable or disable tool use (no argument lists tools)
  /store [reset]     show or reset the demo data
  /info              show current settings
  /exit, /quit       quit

Tip: end a line with \\ to continue on the next line.
""".strip(),
    },
}

MESSAGES["es"] = {
    **MESSAGES["en"],
    "you_prompt": "Tú> ", "bot_prefix": "Asistente> ",
    "error": "Error: {message}", "interrupted": "\n[interrumpido]",
    "history_trimmed": "  [se eliminaron {count} turnos anteriores]\n",
    "max_steps_reached": "  [detenido después de {count} llamadas a herramientas]\n",
    "tool_run": "  [herramienta] {summary}", "label_ok": "resultado",
    "label_error": "falló", "tool_output": "  [{label}] {content}\n",
    "tool_broken_args_model": "Los argumentos no eran JSON válido.",
    "tool_declined": "  [herramienta] omitida\n",
    "tool_declined_model": "La persona no aprobó esta acción. No se ejecutó nada.",
    "tool_err_order_not_found": "No se encontró el pedido {order_id}.",
    "tool_err_reservation_not_found": "No se encontró la reservación {reservation_id}.",
    "tool_err_already_cancelled": "La reservación {reservation_id} ya está cancelada.",
    "tool_err_bad_date": "new_date debe tener el formato AAAA-MM-DD.",
    "tool_err_need_field": "Se requiere new_date o new_time.",
    "tool_note_already_cancelled": "Ya estaba cancelada.",
    "tool_err_not_yours": "No se encontró ese registro en tu cuenta.",
    "tool_err_unknown": "Herramienta desconocida: {name}",
    "tool_err_unexpected": "Error inesperado al ejecutar la herramienta: {message}",
    "tool_none": "sin resultados",
    "approve_skipped": "  [aprobación automática] {summary}",
    "approve_blocked": "  [bloqueado: no se puede confirmar] {summary}",
    "approve_ask": "  [confirmar] {summary}", "approve_prompt": "  ¿Ejecutar? [s/N] ",
    "approve_remote_wait": "  [esperando aprobación] {summary}",
    "approve_remote_granted": "  [aprobado por {approver}] {summary}",
    "approve_remote_refused": "  [{outcome}] {summary}",
    "sum_change_reservation": "Cambiar la reservación {reservation_id} a {date} {time}",
    "sum_cancel_reservation": "Cancelar la reservación {reservation_id}",
    "date_unchanged": "(fecha sin cambios)",
    "time_unchanged": "(hora sin cambios)", "goodbye": "Adiós.",
    "cannot_start": "No se puede iniciar: {message}",
    "config_error": "Error de configuración: {message}",
    "echo_reply": "[prueba] recibido: {last}",
}

# German, Dutch and French cover the strings a customer can end up reading: a
# tool refusal the model relays, and the confirmation wording around an action.
# Everything else falls back to English by design; see the module docstring.
MESSAGES["de"] = {
    "tool_err_already_cancelled": "Termin {reservation_id} ist bereits storniert und kann nicht geändert werden.",
    "tool_err_bad_date": "new_date muss im Format YYYY-MM-DD angegeben werden.",
    "tool_err_need_field": "Entweder new_date oder new_time ist erforderlich.",
    "tool_err_not_yours": "Dieser Eintrag wurde in Ihrem Konto nicht gefunden.",
    "tool_err_order_not_found": "Bestellung {order_id} wurde nicht gefunden.",
    "tool_err_reservation_not_found": "Termin {reservation_id} wurde nicht gefunden.",
    "tool_err_unexpected": "Unerwarteter Fehler beim Ausführen des Werkzeugs: {message}",
    "tool_err_unknown": "Unbekanntes Werkzeug: {name}",
    "tool_broken_args_model": "Die Argumente waren kein gültiges JSON. Rufen Sie das Werkzeug erneut mit gültigem JSON auf.",
    "tool_declined_model": "Die Aktion wurde nicht freigegeben. Es wurde nichts ausgeführt.",
    "tool_note_already_cancelled": "Er war bereits storniert.",
    "tool_none": "keine Treffer",
    "context_too_long": "Das Gespräch ist für dieses Modell zu lang. Beginnen Sie mit /reset ein neues.",
    "approve_remote_wait": "  [warte auf Freigabe] {summary}",
    "approve_remote_granted": "  [freigegeben von {approver}] {summary}",
    "approve_remote_refused": "  [{outcome}] {summary}",
}

MESSAGES["nl"] = {
    "tool_err_already_cancelled": "Afspraak {reservation_id} is al geannuleerd en kan niet worden gewijzigd.",
    "tool_err_bad_date": "new_date moet de notatie YYYY-MM-DD hebben.",
    "tool_err_need_field": "Geef new_date of new_time op.",
    "tool_err_not_yours": "Dat gegeven is niet op uw account gevonden.",
    "tool_err_order_not_found": "Bestelling {order_id} is niet gevonden.",
    "tool_err_reservation_not_found": "Afspraak {reservation_id} is niet gevonden.",
    "tool_err_unexpected": "Onverwachte fout bij het uitvoeren van de tool: {message}",
    "tool_err_unknown": "Onbekende tool: {name}",
    "tool_broken_args_model": "De argumenten waren geen geldige JSON. Roep de tool opnieuw aan met geldige JSON.",
    "tool_declined_model": "De actie is niet goedgekeurd. Er is niets uitgevoerd.",
    "tool_note_already_cancelled": "Die was al geannuleerd.",
    "tool_none": "geen resultaten",
    "context_too_long": "Dit gesprek is te lang voor dit model. Begin met /reset een nieuw gesprek.",
    "approve_remote_wait": "  [wacht op goedkeuring] {summary}",
    "approve_remote_granted": "  [goedgekeurd door {approver}] {summary}",
    "approve_remote_refused": "  [{outcome}] {summary}",
}

MESSAGES["fr"] = {
    "tool_err_already_cancelled": "Le rendez-vous {reservation_id} est déjà annulé et ne peut pas être modifié.",
    "tool_err_bad_date": "new_date doit être au format YYYY-MM-DD.",
    "tool_err_need_field": "Indiquez new_date ou new_time.",
    "tool_err_not_yours": "Cet enregistrement est introuvable sur votre compte.",
    "tool_err_order_not_found": "La commande {order_id} est introuvable.",
    "tool_err_reservation_not_found": "Le rendez-vous {reservation_id} est introuvable.",
    "tool_err_unexpected": "Erreur inattendue lors de l'exécution de l'outil : {message}",
    "tool_err_unknown": "Outil inconnu : {name}",
    "tool_broken_args_model": "Les arguments n'étaient pas du JSON valide. Rappelez l'outil avec du JSON valide.",
    "tool_declined_model": "L'action n'a pas été approuvée. Rien n'a été exécuté.",
    "tool_note_already_cancelled": "Il était déjà annulé.",
    "tool_none": "aucun résultat",
    "context_too_long": "Cette conversation est trop longue pour ce modèle. Utilisez /reset pour en commencer une nouvelle.",
    "approve_remote_wait": "  [en attente d'approbation] {summary}",
    "approve_remote_granted": "  [approuvé par {approver}] {summary}",
    "approve_remote_refused": "  [{outcome}] {summary}",
}


LANGUAGES = tuple(MESSAGES)

# Answers counted as approval.
YES_ANSWERS = {"en": ("y", "yes"), "es": ("s", "si", "sí")}
YES_ANSWERS["de"] = ("j", "ja")
YES_ANSWERS["nl"] = ("j", "ja")
YES_ANSWERS["fr"] = ("o", "oui")


def t(lang: str, key: str, **kwargs: object) -> str:
    """Look up a message. An unknown key returns its own name so the gap is visible."""
    table = MESSAGES.get(lang) or MESSAGES[BASE_LANGUAGE]
    template = table.get(key) or MESSAGES[BASE_LANGUAGE].get(key) or f"<{key}>"
    return template.format(**kwargs) if kwargs else template


def is_yes(lang: str, answer: str) -> bool:
    return answer.strip().lower() in YES_ANSWERS.get(lang, YES_ANSWERS[BASE_LANGUAGE])
