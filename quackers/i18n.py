"""UI-language catalog for the program's own messages.

`STRINGS` holds the one line per locale (en/de/fr/es/ru) that the CLI itself
prints — banner, help headings, prompts, errors, goodbye.  Job titles,
job descriptions, company names and command arguments are never passed
through `t()`; they stay in their original language.

`parse_lang` resolves an ISO 639-1 code or a human name ("German" /
"Deutsch" / "german") to a code, reusing the alias map in
`services.multilingual`.
`persist_lang` / `load_lang` write to / read the `SettingsRow` table
under the same pattern as `config.runtime_settings`.
"""
from __future__ import annotations

import string
import unicodedata
from typing import Dict, Optional

#: UI language currently in effect (ISO 639-1 lowercase).
_LANG = "en"

#: Locale codes with a native translation in `STRINGS`.
TRANSLATED = ("en", "de", "fr", "es", "ru")

STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "banner_tagline": "local-first AI job hunting",
        "banner_jobs_never": "never",
        "banner_profile_none": "none — /cv <file.pdf|.docx|.json>",
        "banner_jobs": "{n} · last scan: {last}",
        "banner_ai_off": "off — {reason} (/env)",
        "banner_help": "/help for commands · or just type what you want",
        "help_commands": "Commands",
        "help_examples": "Examples",
        "help_ask": "Or just ask",
        "help_add_h": "Add -h to any command for its options, e.g. /jobs -h",
        "help_ai_off": "Plain-text questions need an AI provider ({reason}).",
        "prompt_placeholder": 'Try "find remote internships" or /help',
        "prompt_toolbar": "/help commands · Tab complete · Ctrl+C twice to exit",
        "builtin_help": "show commands and examples",
        "builtin_clear": "clear the screen and start a new AI conversation",
        "builtin_exit": "leave quackers",
        "unknown_command": "Unknown command /{cmd}. Type /help.",
        "ai_off": ("AI mode is off ({reason}). Use commands instead, "
                   "e.g. /jobs --new. Type /help for the list."),
        "thinking": "thinking…",
        "ai_error": "AI error: {exc}",
        "interrupted": "Interrupted.",
        "ctrlc_hint": "(press Ctrl+C again or type /exit to quit)",
        "bye": "Bye! ",
        "yes": "yes",
        "no": "no",
        "lang_changed": "Language changed to {language}.",
        "lang_unsupported": "Language not supported: {language}."
                            " Supported: {supported}.",
        "interface_lang": "Interface",
    },
    "de": {
        "banner_tagline": "lokal betriebene KI-Stellenjagd",
        "banner_jobs_never": "nie",
        "banner_profile_none": "keine — /cv <file.pdf|.docx|.json>",
        "banner_jobs": "{n} · letzter Scan: {last}",
        "banner_ai_off": "off — {reason} (/env)",
        "banner_help": "/help für Befehle · oder einfach schreiben, was du suchst",
        "help_commands": "Befehle",
        "help_examples": "Beispiele",
        "help_ask": "Oder einfach fragen",
        "help_add_h": "Füge -h zu jedem Befehl hinzu für Optionen, z. B. /jobs -h",
        "help_ai_off": "Freitext-Fragen benötigen einen KI-Anbieter ({reason}).",
        "prompt_placeholder": 'Probier „fernmeldestellen finden“ oder /help',
        "prompt_toolbar": "Befehle /help · Tabsetzung · Zweifach Ctrl+C zum Beenden",
        "builtin_help": "Befehle und Beispiele anzeigen",
        "builtin_clear": "Bildschirm leeren und neue KI-Unterhaltung starten",
        "builtin_exit": "quackers verlassen",
        "unknown_command": "Unbekannter Befehl /{cmd}. Gib /help ein.",
        "ai_off": ("KI-Modus ist aus ({reason}). Verwende stattdessen Befehle, z. B. "
                   "/jobs --new. Gib /help für die Liste ein."),
        "thinking": "denke nach…",
        "ai_error": "KI-Fehler: {exc}",
        "interrupted": "Unterbrochen.",
        "ctrlc_hint": "(nochmal Ctrl+C drücken oder /exit eintippen, um zu beenden)",
        "bye": "Auf Wiedersehen.",
        "yes": "ja",
        "no": "nein",
        "lang_changed": "Sprache wurde auf {language} geändert.",
        "lang_unsupported": "Sprache wird nicht unterstützt: {language}."
                            " Unterstützt: {supported}.",
        "interface_lang": "Oberfläche",
    },
    "fr": {
        "banner_tagline": "recherche d'emploi locale assistée par IA",
        "banner_jobs_never": "jamais",
        "banner_profile_none": "aucun — /cv <fichier.pdf|.docx|.json>",
        "banner_jobs": "{n} · dernier scan : {last}",
        "banner_ai_off": "off — {reason} (/env)",
        "banner_help": "/help pour les commandes · ou tapez simplement ce que vous cherchez",
        "help_commands": "Commandes",
        "help_examples": "Exemples",
        "help_ask": "Ou posez simplement une question",
        "help_add_h": "Ajoutez -h à toute commande pour voir ses options, ex. /jobs -h",
        "help_ai_off": "Les questions en texte libre nécessitent un fournisseur d'IA ({reason}).",
        "prompt_placeholder": 'Essayez « trouver des postes à distance » ou /help',
        "prompt_toolbar": "commandes /help · Tab pour compléter · deux fois Ctrl+C pour quitter",
        "builtin_help": "afficher les commandes et les exemples",
        "builtin_clear": "effacer l'écran et démarrer une nouvelle conversation IA",
        "builtin_exit": "quitter quackers",
        "unknown_command": "Commande inconnue /{cmd}. Tapez /help.",
        "ai_off": ("Mode IA désactivé ({reason}). Utilisez des commandes à la "
                   "place, ex. /jobs --new. Tapez /help pour la liste."),
        "thinking": "réflexion…",
        "ai_error": "Erreur IA : {exc}",
        "interrupted": "Interrompu.",
        "ctrlc_hint": "(appuyez sur Ctrl+C à nouveau ou tapez /exit pour quitter)",
        "bye": "Au revoir ! ",
        "yes": "oui",
        "no": "non",
        "lang_changed": "La langue a été changée en {language}.",
        "lang_unsupported": "Langue non prise en charge : {language}."
                            " Langues prises en charge : {supported}.",
        "interface_lang": "Interface",
    },
    "es": {
        "banner_tagline": "búsqueda de empleo local con IA",
        "banner_jobs_never": "nunca",
        "banner_profile_none": "ninguno — /cv <archivo.pdf|.docx|.json>",
        "banner_jobs": "{n} · último escaneo: {last}",
        "banner_ai_off": "off — {reason} (/env)",
        "banner_help": "/help para comandos · o escribe lo que quieras",
        "help_commands": "Comandos",
        "help_examples": "Ejemplos",
        "help_ask": "O simplemente pregunta",
        "help_add_h": "Añade -h a cualquier comando para ver sus opciones, p. ej. /jobs -h",
        "help_ai_off": "Las preguntas en texto plano requieren un proveedor de IA ({reason}).",
        "prompt_placeholder": 'Prueba «buscar prácticas a distancia» o /help',
        "prompt_toolbar": "comandos /help · Tab para completar · Ctrl+C dos veces para salir",
        "builtin_help": "mostrar comandos y ejemplos",
        "builtin_clear": "limpiar la pantalla y empezar una nueva conversación de IA",
        "builtin_exit": "salir de quackers",
        "unknown_command": "Comando desconocido /{cmd}. Escribe /help.",
        "ai_off": ("Modo IA desactivado ({reason}). Usa comandos en su lugar, "
                   "p. ej. /jobs --new. Escribe /help para la lista."),
        "thinking": "pensando…",
        "ai_error": "Error de IA: {exc}",
        "interrupted": "Interrumpido.",
        "ctrlc_hint": "(pulsa Ctrl+C otra vez o escribe /exit para salir)",
        "bye": "¡Adiós! ",
        "yes": "sí",
        "no": "no",
        "lang_changed": "Idioma cambiado a {language}.",
        "lang_unsupported": "Idioma no compatible: {language}."
                            " Idiomas compatibles: {supported}.",
        "interface_lang": "Interfaz",
    },
    "ru": {
        "banner_tagline": "поиск работы с ИИ локально",
        "banner_jobs_never": "никогда",
        "banner_profile_none": "нет — /cv <file.pdf|.docx|.json>",
        "banner_jobs": "{n} · последний скан: {last}",
        "banner_ai_off": "off — {reason} (/env)",
        "banner_help": "/help для списка команд · или просто введите, что нужно",
        "help_commands": "Команды",
        "help_examples": "Примеры",
        "help_ask": "Или просто спросите",
        "help_add_h": "Добавьте -h к любой команде для параметров, напр. /jobs -h",
        "help_ai_off": "Вопросы в свободной форме требуют поставщика ИИ ({reason}).",
        "prompt_placeholder": 'Попробуйте «найти удалённую стажировку» или /help',
        "prompt_toolbar": "команды /help · Tab — автодополнение · twice Ctrl+C — выход",
        "builtin_help": "показать команды и примеры",
        "builtin_clear": "очистить экран и начать новый диалог с ИИ",
        "builtin_exit": "выйти из quackers",
        "unknown_command": "Неизвестная команда /{cmd}. Введите /help.",
        "ai_off": ("Режим ИИ выключен ({reason}). Вместо этого используйте "
                   "команды, напр. /jobs --new. Список команд: /help."),
        "thinking": "думаю…",
        "ai_error": "Ошибка ИИ: {exc}",
        "interrupted": "Прервано.",
        "ctrlc_hint": "(нажмите Ctrl+C ещё раз или введите /exit, чтобы выйти)",
        "bye": "Пока! ",
        "yes": "да",
        "no": "нет",
        "lang_changed": "Язык изменён на {language}.",
        "lang_unsupported": "Язык не поддерживается: {language}."
                            " Поддерживаются: {supported}.",
        "interface_lang": "Интерфейс",
    },
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def t(key: str, **kwargs) -> str:
    """Translate `key` into the current UI language, falling back to English.

    `kwargs` values that are `None` are treated as absent and substituted
    with an empty string.  Unknown keys render as the key itself so that a
    forgotten catalog entry is visible to the user.
    """
    template = STRINGS.get(_LANG, {}).get(key) or STRINGS["en"].get(key, key)
    if kwargs:
        filled = {k: ("" if v is None else v) for k, v in kwargs.items()}
        try:
            return template.format_map(_SafeMap(filled))
        except Exception:
            return template
    return template


class _SafeMap(dict):
    """`format_map` mapping that never raises: absent keys -> ''."""

    def __missing__(self, _key: str) -> str:
        return ""


def get_language() -> str:
    return _LANG


def set_language(code: str) -> None:
    """Validate and install `code`. Unknown codes fall back to English."""
    global _LANG
    _LANG = code if code in STRINGS else "en"


def is_translated(code: str) -> bool:
    return code in TRANSLATED


def parse_lang(raw: str) -> Optional[str]:
    """Resolve "de" / "De" / "DE" / "German" / "deutsch" to a code.

    ISO 639-1 codes are matched case-insensitively against the keys of
    `services.multilingual.LANGUAGE_LABELS`; anything else is matched
    against that <language> English name (case-insensitive, with diacritics
    and spaces normalised).  Returns None when nothing is recognized.
    """
    s = (raw or "").strip()
    if not s:
        return None
    from services.multilingual import LANGUAGE_LABELS
    if s.lower() in LANGUAGE_LABELS:
        return s.lower()
    flat = _strip_accents(s).lower()
    for code, name in LANGUAGE_LABELS.items():
        if flat == _strip_accents(name).lower():
            return code
    return None


def all_supported() -> list:
    """Every selectable code (everything in `LANGUAGE_LABELS`)."""
    from services.multilingual import LANGUAGE_LABELS
    return sorted(set(LANGUAGE_LABELS) | set(TRANSLATED))


def display_for(code: str) -> str:
    """English name of a language for the confirmation message."""
    from services.multilingual import LANGUAGE_LABELS
    return LANGUAGE_LABELS.get(code, code)


def supported_display() -> str:
    """`"de, en, fr, …"` for help / error text; alphabetical."""
    return ", ".join(sorted(all_supported()))


# ---------------------------------------------------------------------------
# Persistence (SettingsRow, mirrors config.runtime_settings save / load)
# ---------------------------------------------------------------------------

UI_LANGUAGE_KEY = "ui_language"


def persist_lang(code: str) -> None:
    from database.models import SettingsRow
    from quackers import store

    with store.with_session() as session:
        row = session.query(SettingsRow).filter_by(key=UI_LANGUAGE_KEY).first()
        if row is None:
            session.add(SettingsRow(key=UI_LANGUAGE_KEY, value=code))
        else:
            row.value = code
        session.commit()


def load_lang() -> Optional[str]:
    """Return the persisted interface language code, or None."""
    from database.models import SettingsRow
    from quackers import store

    with store.with_session() as session:
        row = session.query(SettingsRow).filter_by(key=UI_LANGUAGE_KEY).first()
        if row is None or not row.value:
            return None
    code = row.value.strip().lower()
    return code if code in TRANSLATED else None