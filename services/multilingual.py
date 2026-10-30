"""Multilingual helpers: search languages, localized job titles, language
detection and required-language extraction.

Everything here is deterministic and offline.  ``langdetect`` is used for
:func:`detect_language` when it happens to be installed; otherwise a small
stop-word model is used.  Nothing is ever guessed: when the evidence is
weak the functions return ``None`` / ``[]``.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

#: Languages the dictionaries below cover.
SUPPORTED = ("en", "de", "fr", "nl", "es", "it", "pl", "pt", "ru")

#: Location substring -> local language(s).  Lower-case.
LOCATION_LANGUAGES: Dict[str, tuple[str, ...]] = {
    # German-speaking
    "germany": ("de",), "deutschland": ("de",), "berlin": ("de",), "munich": ("de",),
    "münchen": ("de",), "hamburg": ("de",), "frankfurt": ("de",), "cologne": ("de",),
    "köln": ("de",), "stuttgart": ("de",), "düsseldorf": ("de",), "dusseldorf": ("de",),
    "leipzig": ("de",), "dresden": ("de",), "hannover": ("de",), "nürnberg": ("de",),
    "nuremberg": ("de",), "bremen": ("de",), "bonn": ("de",), "potsdam": ("de",),
    "karlsruhe": ("de",), "aachen": ("de",), "mannheim": ("de",), "heidelberg": ("de",),
    "austria": ("de",), "österreich": ("de",), "vienna": ("de",), "wien": ("de",),
    "graz": ("de",), "zurich": ("de",), "zürich": ("de",), "basel": ("de",), "bern": ("de",),
    # multilingual countries
    "switzerland": ("de", "fr"), "schweiz": ("de", "fr"), "suisse": ("fr", "de"),
    "geneva": ("fr",), "genève": ("fr",), "lausanne": ("fr",),
    "belgium": ("nl", "fr"), "belgië": ("nl", "fr"), "belgique": ("fr", "nl"),
    "brussels": ("fr", "nl"), "antwerp": ("nl",), "ghent": ("nl",),
    "luxembourg": ("fr", "de"),
    # others
    "france": ("fr",), "paris": ("fr",), "lyon": ("fr",), "toulouse": ("fr",),
    "marseille": ("fr",), "nantes": ("fr",), "lille": ("fr",),
    "netherlands": ("nl",), "nederland": ("nl",), "amsterdam": ("nl",),
    "rotterdam": ("nl",), "utrecht": ("nl",), "eindhoven": ("nl",), "the hague": ("nl",),
    "spain": ("es",), "españa": ("es",), "madrid": ("es",), "barcelona": ("es",),
    "valencia": ("es",), "seville": ("es",),
    "italy": ("it",), "italia": ("it",), "milan": ("it",), "milano": ("it",),
    "rome": ("it",), "roma": ("it",), "turin": ("it",), "torino": ("it",),
    "poland": ("pl",), "polska": ("pl",), "warsaw": ("pl",), "warszawa": ("pl",),
    "krakow": ("pl",), "kraków": ("pl",), "wroclaw": ("pl",), "wrocław": ("pl",),
    "portugal": ("pt",), "lisbon": ("pt",), "lisboa": ("pt",), "porto": ("pt",),
    "brazil": ("pt",), "são paulo": ("pt",),
}

#: Level words handled separately from the role part of a title.
_LEVELS = ("working student", "intern", "apprentice", "junior")

#: Canonical English term -> local equivalents (first = preferred in queries).
TITLE_TERMS: Dict[str, Dict[str, tuple[str, ...]]] = {
    "intern": {
        "de": ("Praktikum", "Praktikant"), "fr": ("stage", "stagiaire"),
        "nl": ("stage", "stagiair"), "es": ("prácticas", "becario"),
        "it": ("stage", "tirocinio"), "pl": ("staż", "praktyki"),
        "pt": ("estágio", "estagiário"), "ru": ("стажировка", "стажер"),
    },
    "working student": {"de": ("Werkstudent",), "nl": ("werkstudent",)},
    "apprentice": {
        "de": ("Ausbildung", "Auszubildender"), "fr": ("alternance", "apprenti"),
        "es": ("aprendiz",), "it": ("apprendista",),
    },
    "software developer": {
        "de": ("Softwareentwickler",), "fr": ("développeur logiciel",),
        "nl": ("softwareontwikkelaar",), "es": ("desarrollador de software",),
        "it": ("sviluppatore software",), "pl": ("programista",),
        "pt": ("desenvolvedor de software",), "ru": ("разработчик",),
    },
    "software development": {
        "de": ("Softwareentwicklung",), "fr": ("développement logiciel",),
        "nl": ("softwareontwikkeling",), "es": ("desarrollo de software",),
        "it": ("sviluppo software",), "pt": ("desenvolvimento de software",),
    },
    "software engineer": {
        "de": ("Softwareingenieur",), "fr": ("ingénieur logiciel",),
        "es": ("ingeniero de software",), "it": ("ingegnere software",),
        "pl": ("inżynier oprogramowania",), "pt": ("engenheiro de software",),
        "ru": ("инженер-программист",),
    },
    "data analyst": {
        "de": ("Datenanalyst",), "fr": ("analyste de données",), "nl": ("data-analist",),
        "es": ("analista de datos",), "it": ("analista dati",), "pl": ("analityk danych",),
        "pt": ("analista de dados",), "ru": ("аналитик данных",),
    },
    "data scientist": {"es": ("científico de datos",), "pt": ("cientista de dados",)},
    "web developer": {
        "de": ("Webentwickler",), "fr": ("développeur web",), "nl": ("webontwikkelaar",),
        "es": ("desarrollador web",), "it": ("sviluppatore web",),
        "pt": ("desenvolvedor web",),
    },
    "computer science": {
        "de": ("Informatik",), "fr": ("informatique",), "nl": ("informatica",),
        "es": ("informática",), "it": ("informatica",), "pl": ("informatyka",),
        "pt": ("informática",),
    },
}

#: Gender / inclusive-language markers removed before comparing titles.
_GENDER_RE = re.compile(
    r"\(\s*(?:m|w|f|d|x|h|div|all genders?|gn\*?)(?:\s*[/|,]\s*(?:m|w|f|d|x|h|div))*\s*\)"
    r"|(?<!\w)(?:m|w|f|h)\s*/\s*(?:w|f|m|h)(?:\s*/\s*(?:d|x|div))?(?!\w)"
    r"|\*in(?:nen)?\b|:in(?:nen)?\b",
    re.I,
)

#: Local term (lower-case) -> canonical English, built from TITLE_TERMS plus
#: frequent inflected / feminine forms.
LOCAL_TO_CANONICAL: Dict[str, str] = {}
for _canon, _by_lang in TITLE_TERMS.items():
    for _terms in _by_lang.values():
        for _t in _terms:
            if _t.lower() != _canon:
                LOCAL_TO_CANONICAL.setdefault(_t.lower(), _canon)
LOCAL_TO_CANONICAL.update({
    "praktikantin": "intern", "pflichtpraktikum": "intern", "praxissemester": "intern",
    "werkstudentin": "working student", "werkstudierende": "working student",
    "studentische hilfskraft": "working student",
    "softwareentwicklerin": "software developer", "entwickler": "developer",
    "entwicklerin": "developer", "datenanalystin": "data analyst",
    "webentwicklerin": "web developer", "développeuse": "developer",
    "développeur": "developer", "desarrolladora": "developer",
    "desarrollador": "developer", "sviluppatore": "developer",
    "becaria": "intern", "estagiária": "intern", "stażysta": "intern",
})
# "stage" is also an English word; only translate it with a role next to it.
LOCAL_TO_CANONICAL.pop("stage", None)

#: Connector words that carry no meaning for title matching.
_CONNECTORS_RE = re.compile(
    r"(?<!\w)(?:im bereich|bereich|für|fuer|in der|im|als|de|des|du|en|del|di|da|do)(?!\w)"
)

LANGUAGE_NAMES: Dict[str, tuple[str, ...]] = {
    "en": ("english", "englisch", "anglais", "engels", "inglés", "ingles", "inglese",
           "angielski", "inglês", "английский"),
    "de": ("german", "deutsch", "allemand", "duits", "alemán", "aleman", "tedesco",
           "niemiecki", "alemão", "немецкий"),
    "fr": ("french", "französisch", "franzoesisch", "français", "francais", "frans",
           "francés", "frances", "francese", "francuski", "francês", "французский"),
    "nl": ("dutch", "niederländisch", "néerlandais", "nederlands", "neerlandés",
           "olandese", "niderlandzki", "holandês", "нидерландский"),
    "es": ("spanish", "spanisch", "espagnol", "spaans", "español", "espanol",
           "spagnolo", "hiszpański", "espanhol", "испанский"),
    "it": ("italian", "italienisch", "italien", "italiaans", "italiano", "włoski",
           "итальянский"),
    "pl": ("polish", "polnisch", "polonais", "pools", "polaco", "polacco",
           "polonês", "польский"),
    "pt": ("portuguese", "portugiesisch", "portugais", "portugees", "portugués",
           "portoghese", "portugalski", "português", "португальский"),
    "ru": ("russian", "russisch", "russe", "ruso", "russo", "rosyjski", "русский"),
}
_NAME_TO_CODE = {n: code for code, names in LANGUAGE_NAMES.items() for n in names}
_NAMES_ALT = "|".join(sorted(map(re.escape, _NAME_TO_CODE), key=len, reverse=True))

#: Words that turn a language mention into a *requirement*.
_REQ_CUE_RE = re.compile(
    r"fluent|fluency|proficien\w*|native|mother tongue|business[- ]level|working "
    r"knowledge|required|requirement|must|skills?\b|knowledge|command of|speak\w*|"
    r"written|spoken|level|\b[abc][12]\b|kenntnisse|fließend|fliessend|"
    r"verhandlungssicher|muttersprach\w*|sehr gut\w*|\bgute?\b|sicher\w*|beherrsch\w*|"
    r"courant|maîtrise|maitrise|bilingue|vloeiend|beheersing|fluido|dominio|nivel|"
    r"ottim\w*|conoscenza|biegł\w*|znajomość|fluente|domínio|свободн\w*|владени\w*|знани\w*",
    re.I,
)
#: German compounds: "Deutschkenntnisse", "Deutsch- und Englischkenntnisse".
_KENNTNISSE_RE = re.compile(
    r"(?<!\w)(" + _NAMES_ALT + r")(?:kenntnisse|-\s*(?:und|oder|sowie|&)\s+(\w+?)kenntnisse)",
    re.I,
)

# ---------------------------------------------------------------- languages


def _word_in(needle: str, text: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", text) is not None


def languages_for_locations(locations: Iterable[str]) -> List[str]:
    """Local language(s) of the configured locations, in order of appearance."""
    out: List[str] = []
    for loc in locations or []:
        low = (loc or "").strip().lower()
        for needle, langs in LOCATION_LANGUAGES.items():
            if low and _word_in(needle, low):
                out += [lang for lang in langs if lang not in out]
    return out


def search_languages(cfg) -> List[str]:
    """Languages to search in: English (unless ``include_english`` is off),
    the locations' local languages (when ``auto_detect_local_language``)
    and the explicit ``search_languages``.  Unknown codes are dropped."""
    langs: List[str] = []
    if getattr(cfg, "include_english", True):
        langs.append("en")
    if getattr(cfg, "auto_detect_local_language", True):
        langs += languages_for_locations(getattr(cfg, "locations", None) or [])
    langs += [(x or "").strip().lower() for x in getattr(cfg, "search_languages", None) or []]
    out: List[str] = []
    for lang in langs:
        if lang in SUPPORTED and lang not in out:
            out.append(lang)
    return out or ["en"]


# ---------------------------------------------------------------- titles


def strip_gender_markers(title: str) -> str:
    t = _GENDER_RE.sub(" ", title or "")
    return re.sub(r"\s+", " ", t).strip(" -|,")


def canonicalize_title(title: str) -> str:
    """Map a local-language job title onto English role words.

    "Werkstudent Softwareentwicklung (m/w/d)" -> "working student software
    development".  Unknown words are kept, so an English title comes back
    unchanged apart from case and gender markers.
    """
    t = strip_gender_markers(title).lower()
    if not t:
        return ""
    for local in sorted(LOCAL_TO_CANONICAL, key=len, reverse=True):
        if local in t:
            t = re.sub(r"(?<!\w)" + re.escape(local) + r"(?!\w)", LOCAL_TO_CANONICAL[local], t)
    t = _CONNECTORS_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" -|,")


def localized_titles(title: str, lang: str, *, limit: int = 2) -> List[str]:
    """Local-language variants of an English (or local) title.

    Only terms in :data:`TITLE_TERMS` are translated.  When nothing in the
    title is known for ``lang``, ``[]`` is returned rather than a bad guess.
    """
    if lang == "en":
        return []
    canon = canonicalize_title(title)
    if not canon:
        return []
    level = next((lvl for lvl in _LEVELS[:3] if _word_in(lvl, canon)), None)
    role = canon
    for lvl in ("working student", "internship", "intern", "apprentice", "junior"):
        role = re.sub(r"(?<!\w)" + lvl + r"(?!\w)", " ", role)
    role = re.sub(r"\s+", " ", role).strip()

    role_local, translated = role, False
    for canon_role in sorted(TITLE_TERMS, key=len, reverse=True):
        if canon_role in _LEVELS or lang not in TITLE_TERMS[canon_role]:
            continue
        if _word_in(canon_role, role_local):
            role_local = role_local.replace(canon_role, TITLE_TERMS[canon_role][lang][0])
            translated = True
    level_terms: tuple[str, ...] = ()
    if level:
        level_terms = TITLE_TERMS[level].get(lang, ())
        if not level_terms and level == "working student":
            level_terms = TITLE_TERMS["intern"].get(lang, ())
    if not level_terms and not translated:
        return []
    out: List[str] = []
    for lvl in level_terms or ("",):
        q = f"{lvl} {role_local}".strip()
        if q.lower() != (title or "").strip().lower() and q not in out:
            out.append(q)
    return out[:limit]


def query_language(query: str) -> str:
    """Language tag for a planner query: a known local term marks it as
    that language; everything else counts as English."""
    low = (query or "").lower()
    for canon, by_lang in TITLE_TERMS.items():
        for lang, terms in by_lang.items():
            for term in terms:
                tl = term.lower()
                if tl != canon and tl != "stage" and _word_in(tl, low):
                    return lang
    for word in ("werkstudent", "praktikant", "wir suchen", "studentische", "neu"):
        if _word_in(word, low):
            return "de"
    return "en"


# ---------------------------------------------------------------- detection

_STOPWORDS: Dict[str, frozenset] = {
    "en": frozenset("the and of to in for with you we our are is on as be will this "
                    "your have an at or from team".split()),
    "de": frozenset("der die das und mit für wir sie ist ein eine zu im den von auf "
                    "bei sich als auch oder werden dein deine ihr ihre unser".split()),
    "fr": frozenset("le la les et des pour avec vous nous est une un dans du en sur "
                    "au par votre notre sont".split()),
    "nl": frozenset("de het en een van voor met je wij jij zijn op ons naar bij "
                    "jouw onze".split()),
    "es": frozenset("el la los las y de para con usted nosotros es una un en del por "
                    "tu su somos".split()),
    "it": frozenset("il lo la gli le e di per con noi è una un nel del della sono "
                    "tuo tua".split()),
    "pl": frozenset("i w na z do się jest dla oraz że nie od jako przez twoje nasz".split()),
    "pt": frozenset("o a os as e de para com você nós é uma um em do da por seu sua "
                    "somos".split()),
    "ru": frozenset("и в на с для мы вы это по не от как что или".split()),
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str, *, min_words: int = 12) -> Optional[str]:
    """ISO 639-1 code of ``text``, or ``None`` when it is too short or
    ambiguous.  Uses ``langdetect`` when installed (fixed seed)."""
    t = (text or "").strip()
    words = [w.lower() for w in _WORD_RE.findall(t)]
    if len(words) < min_words:
        return None
    try:  # optional dependency
        from langdetect import DetectorFactory, detect_langs  # type: ignore

        DetectorFactory.seed = 0
        best = detect_langs(t[:5000])[0]
        return best.lang.split("-")[0] if best.prob >= 0.80 else None
    except ImportError:
        pass
    except Exception:
        return None
    counts = {lang: sum(1 for w in words if w in sw) for lang, sw in _STOPWORDS.items()}
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    (best_lang, best), (_, second) = ranked[0], ranked[1]
    if best < 3 or best / len(words) < 0.08 or best < second * 1.5:
        return None
    return best_lang


# ---------------------------------------------------------------- requirements


def extract_required_languages(text: str) -> List[str]:
    """Languages an applicant must speak, as sorted ISO codes.

    A language name only counts next to a requirement cue ("fluent German",
    "Deutschkenntnisse", "English (C1)", "maîtrise de l'anglais").  A posting
    that is merely *written* in German does not produce ``de``.
    """
    t = text or ""
    found: set[str] = set()
    for m in _KENNTNISSE_RE.finditer(t):
        for name in (m.group(1), m.group(2)):
            code = _NAME_TO_CODE.get((name or "").lower())
            if code:
                found.add(code)
    for m in re.finditer(r"(?<!\w)(" + _NAMES_ALT + r")(?!\w)", t, re.I):
        code = _NAME_TO_CODE[m.group(1).lower()]
        left = re.split(r"[.!?;\n]", t[max(0, m.start() - 45):m.start()])[-1]
        right = re.split(r"[.!?;\n]", t[m.end():m.end() + 45])[0]
        if _REQ_CUE_RE.search(left) or _REQ_CUE_RE.search(right):
            found.add(code)
    return sorted(found)


def profile_language_codes(languages: Sequence) -> List[str]:
    """ISO codes from a profile's ``languages`` list (strings or dicts)."""
    out: set[str] = set()
    for item in languages or []:
        text = " ".join(str(v) for v in item.values() if v) if isinstance(item, dict) else str(item)
        low = text.lower()
        for name, code in _NAME_TO_CODE.items():
            if re.search(r"(?<!\w)" + re.escape(name), low):
                out.add(code)
        m = re.fullmatch(r"\s*([a-z]{2})\s*(?:[-(:,].*)?", low)
        if m and m.group(1) in SUPPORTED:
            out.add(m.group(1))
    return sorted(out)


def language_fit(required: Sequence[str], spoken: Sequence[str]) -> tuple[str, List[str]]:
    """Return ("great" | "poor" | "unknown", missing codes).

    ``unknown`` when the job demands languages but the profile lists none —
    we do not assume the candidate cannot speak them.
    """
    req = sorted(set(required or []))
    if not req:
        return "great", []
    if not spoken:
        return "unknown", req
    missing = [r for r in req if r not in set(spoken)]
    return ("poor" if missing else "great"), missing


__all__ = [
    "SUPPORTED", "languages_for_locations", "search_languages", "strip_gender_markers",
    "canonicalize_title", "localized_titles", "query_language", "detect_language",
    "extract_required_languages", "profile_language_codes", "language_fit",
]
