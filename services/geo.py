"""Worldwide geography used by search planning, location matching and
language selection.

Offline tables: countries (ISO-3166 alpha-2, aliases, main languages) and
major cities -> country.  Nothing here restricts a search; the tables only
help to *recognise* a place.  An unknown place is passed through as typed
and treated as "not verifiable", never as a mismatch.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# (canonical lower-case name, ISO2, aliases, main languages ISO 639-1)
_COUNTRY_ROWS: Tuple[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("germany", "de", ("deutschland", "ger", "deu"), ("de",)),
    ("austria", "at", ("österreich", "aut"), ("de",)),
    ("switzerland", "ch", ("schweiz", "suisse", "svizzera", "che", "swz"), ("de", "fr", "it")),
    ("united kingdom", "gb", ("uk", "u.k.", "great britain", "britain", "england", "scotland",
                              "wales", "northern ireland", "gbr"), ("en",)),
    ("ireland", "ie", ("éire", "irl", "ire"), ("en",)),
    ("france", "fr", ("fra",), ("fr",)),
    ("belgium", "be", ("belgië", "belgique", "bel"), ("nl", "fr")),
    ("netherlands", "nl", ("the netherlands", "holland", "nederland", "nld", "net"), ("nl",)),
    ("luxembourg", "lu", ("lux",), ("fr", "de")),
    ("spain", "es", ("españa", "esp"), ("es",)),
    ("portugal", "pt", ("prt",), ("pt",)),
    ("italy", "it", ("italia", "ita"), ("it",)),
    ("poland", "pl", ("polska", "pol"), ("pl",)),
    ("czech republic", "cz", ("czechia", "česko"), ("cs",)),
    ("slovakia", "sk", (), ("sk",)),
    ("hungary", "hu", ("magyarország",), ("hu",)),
    ("romania", "ro", ("românia",), ("ro",)),
    ("bulgaria", "bg", (), ("bg",)),
    ("greece", "gr", ("hellas", "ελλάδα"), ("el",)),
    ("croatia", "hr", ("hrvatska",), ("hr",)),
    ("serbia", "rs", ("srbija",), ("sr",)),
    ("slovenia", "si", (), ("sl",)),
    ("denmark", "dk", ("danmark", "den"), ("da",)),
    ("sweden", "se", ("sverige", "swe"), ("sv",)),
    ("norway", "no", ("norge", "nor"), ("no",)),
    ("finland", "fi", ("suomi", "fin"), ("fi",)),
    ("iceland", "is", (), ("is",)),
    ("estonia", "ee", (), ("et",)),
    ("latvia", "lv", (), ("lv",)),
    ("lithuania", "lt", (), ("lt",)),
    ("ukraine", "ua", ("україна",), ("uk",)),
    ("russia", "ru", ("россия", "russian federation"), ("ru",)),
    ("belarus", "by", (), ("ru",)),
    ("kazakhstan", "kz", ("казахстан",), ("ru", "kk")),
    ("uzbekistan", "uz", ("o'zbekiston", "узбекистан"), ("uz", "ru")),
    ("georgia", "ge", (), ("ka",)),
    ("armenia", "am", (), ("hy",)),
    ("azerbaijan", "az", (), ("az",)),
    ("turkey", "tr", ("türkiye", "turkiye"), ("tr",)),
    ("israel", "il", (), ("he", "en")),
    ("united arab emirates", "ae", ("uae", "u.a.e.", "emirates"), ("ar", "en")),
    ("saudi arabia", "sa", ("ksa",), ("ar",)),
    ("qatar", "qa", (), ("ar", "en")),
    ("kuwait", "kw", (), ("ar",)),
    ("bahrain", "bh", (), ("ar", "en")),
    ("oman", "om", (), ("ar",)),
    ("jordan", "jo", (), ("ar",)),
    ("lebanon", "lb", (), ("ar", "fr")),
    ("egypt", "eg", (), ("ar",)),
    ("morocco", "ma", ("maroc",), ("ar", "fr")),
    ("tunisia", "tn", (), ("ar", "fr")),
    ("algeria", "dz", (), ("ar", "fr")),
    ("nigeria", "ng", (), ("en",)),
    ("ghana", "gh", (), ("en",)),
    ("kenya", "ke", (), ("en", "sw")),
    ("ethiopia", "et", (), ("am", "en")),
    ("south africa", "za", ("rsa",), ("en",)),
    ("united states", "us", ("usa", "us", "u.s.", "u.s.a.", "united states of america",
                             "america"), ("en",)),
    ("canada", "ca", (), ("en", "fr")),
    ("mexico", "mx", ("méxico",), ("es",)),
    ("brazil", "br", ("brasil",), ("pt",)),
    ("argentina", "ar", (), ("es",)),
    ("chile", "cl", (), ("es",)),
    ("colombia", "co", (), ("es",)),
    ("peru", "pe", ("perú",), ("es",)),
    ("uruguay", "uy", (), ("es",)),
    ("costa rica", "cr", (), ("es",)),
    ("japan", "jp", ("日本", "nippon"), ("ja",)),
    ("china", "cn", ("中国", "prc", "mainland china"), ("zh",)),
    ("hong kong", "hk", ("香港",), ("zh", "en")),
    ("taiwan", "tw", ("台灣",), ("zh",)),
    ("south korea", "kr", ("korea", "republic of korea", "한국"), ("ko",)),
    ("india", "in", ("bharat",), ("en", "hi")),
    ("pakistan", "pk", (), ("en", "ur")),
    ("bangladesh", "bd", (), ("bn", "en")),
    ("sri lanka", "lk", (), ("en", "si")),
    ("singapore", "sg", (), ("en", "zh")),
    ("malaysia", "my", (), ("ms", "en")),
    ("indonesia", "id", (), ("id",)),
    ("thailand", "th", (), ("th",)),
    ("vietnam", "vn", ("viet nam",), ("vi",)),
    ("philippines", "ph", (), ("en",)),
    ("australia", "au", (), ("en",)),
    ("new zealand", "nz", ("aotearoa",), ("en",)),
)

# Major cities -> ISO2.  Names are lower-case; common local spellings too.
_CITIES: Dict[str, str] = {
    # Germany / DACH
    "berlin": "de", "munich": "de", "münchen": "de", "muenchen": "de", "hamburg": "de",
    "frankfurt": "de", "frankfurt am main": "de", "cologne": "de", "köln": "de",
    "stuttgart": "de", "düsseldorf": "de", "dusseldorf": "de", "leipzig": "de",
    "dresden": "de", "hannover": "de", "nuremberg": "de", "nürnberg": "de", "bremen": "de",
    "bonn": "de", "potsdam": "de", "karlsruhe": "de", "aachen": "de", "mannheim": "de",
    "heidelberg": "de", "essen": "de", "dortmund": "de",
    "vienna": "at", "wien": "at", "graz": "at", "salzburg": "at", "linz": "at",
    "zurich": "ch", "zürich": "ch", "geneva": "ch", "genève": "ch", "basel": "ch",
    "bern": "ch", "lausanne": "ch",
    # rest of Europe
    "london": "gb", "manchester": "gb", "birmingham": "gb", "edinburgh": "gb",
    "glasgow": "gb", "bristol": "gb", "leeds": "gb", "liverpool": "gb", "cambridge": "gb",
    "oxford": "gb", "belfast": "gb", "dublin": "ie", "cork": "ie",
    "paris": "fr", "lyon": "fr", "marseille": "fr", "toulouse": "fr", "nantes": "fr",
    "lille": "fr", "bordeaux": "fr",
    "brussels": "be", "bruxelles": "be", "antwerp": "be", "ghent": "be",
    "amsterdam": "nl", "rotterdam": "nl", "utrecht": "nl", "eindhoven": "nl",
    "the hague": "nl", "den haag": "nl",
    "madrid": "es", "barcelona": "es", "valencia": "es", "seville": "es", "sevilla": "es",
    "bilbao": "es", "malaga": "es", "málaga": "es",
    "lisbon": "pt", "lisboa": "pt", "porto": "pt",
    "rome": "it", "roma": "it", "milan": "it", "milano": "it", "turin": "it",
    "torino": "it", "naples": "it", "napoli": "it", "bologna": "it", "florence": "it",
    "warsaw": "pl", "warszawa": "pl", "krakow": "pl", "kraków": "pl", "wroclaw": "pl",
    "wrocław": "pl", "gdansk": "pl", "gdańsk": "pl", "poznan": "pl", "poznań": "pl",
    "prague": "cz", "praha": "cz", "brno": "cz", "budapest": "hu", "bucharest": "ro",
    "sofia": "bg", "athens": "gr", "zagreb": "hr", "belgrade": "rs", "ljubljana": "si",
    "copenhagen": "dk", "københavn": "dk", "stockholm": "se", "gothenburg": "se",
    "oslo": "no", "helsinki": "fi", "reykjavik": "is", "tallinn": "ee", "riga": "lv",
    "vilnius": "lt", "kyiv": "ua", "kiev": "ua", "moscow": "ru", "москва": "ru",
    "saint petersburg": "ru", "st. petersburg": "ru", "almaty": "kz", "astana": "kz",
    "tashkent": "uz", "ташкент": "uz", "tbilisi": "ge", "yerevan": "am", "baku": "az",
    "istanbul": "tr", "ankara": "tr", "izmir": "tr",
    # Middle East / Africa
    "dubai": "ae", "abu dhabi": "ae", "sharjah": "ae", "riyadh": "sa", "jeddah": "sa",
    "doha": "qa", "kuwait city": "kw", "manama": "bh", "muscat": "om", "amman": "jo",
    "beirut": "lb", "tel aviv": "il", "jerusalem": "il", "haifa": "il", "cairo": "eg",
    "alexandria": "eg", "casablanca": "ma", "rabat": "ma", "tunis": "tn", "algiers": "dz",
    "lagos": "ng", "abuja": "ng", "accra": "gh", "nairobi": "ke", "addis ababa": "et",
    "johannesburg": "za", "cape town": "za", "durban": "za", "pretoria": "za",
    # Americas
    "new york": "us", "new york city": "us", "nyc": "us", "los angeles": "us",
    "san francisco": "us", "seattle": "us", "boston": "us", "chicago": "us",
    "austin": "us", "houston": "us", "dallas": "us", "miami": "us", "atlanta": "us",
    "denver": "us", "washington dc": "us", "philadelphia": "us",
    "san diego": "us", "san jose": "us", "phoenix": "us",
    "toronto": "ca", "vancouver": "ca", "montreal": "ca", "montréal": "ca", "calgary": "ca",
    "ottawa": "ca", "edmonton": "ca", "mexico city": "mx", "ciudad de méxico": "mx",
    "guadalajara": "mx", "monterrey": "mx", "são paulo": "br", "sao paulo": "br",
    "rio de janeiro": "br", "buenos aires": "ar", "santiago": "cl", "bogotá": "co",
    "bogota": "co", "medellín": "co", "medellin": "co", "lima": "pe", "montevideo": "uy",
    # Asia-Pacific
    "tokyo": "jp", "東京": "jp", "osaka": "jp", "kyoto": "jp", "yokohama": "jp",
    "nagoya": "jp", "fukuoka": "jp", "beijing": "cn", "shanghai": "cn", "shenzhen": "cn",
    "guangzhou": "cn", "hangzhou": "cn", "taipei": "tw", "seoul": "kr",
    "busan": "kr", "bangalore": "in", "bengaluru": "in", "mumbai": "in", "delhi": "in",
    "new delhi": "in", "hyderabad": "in", "chennai": "in", "pune": "in", "kolkata": "in",
    "karachi": "pk", "lahore": "pk", "dhaka": "bd", "colombo": "lk",
    "kuala lumpur": "my", "jakarta": "id", "bangkok": "th", "ho chi minh city": "vn",
    "hanoi": "vn", "manila": "ph", "sydney": "au", "melbourne": "au", "brisbane": "au",
    "perth": "au", "adelaide": "au", "canberra": "au", "auckland": "nz",
    "wellington": "nz",
}

#: Words meaning "no geographic restriction".
ANYWHERE_RE = re.compile(
    r"(?<!\w)(?:anywhere|worldwide|world[- ]wide|globally|any country|any location|"
    r"all countries|every country|any place)(?!\w)", re.I)

_BY_NAME: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {}
_BY_ISO: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {}
for _name, _iso, _aliases, _langs in _COUNTRY_ROWS:
    _row = (_name, _iso, _langs)
    _BY_ISO[_iso] = _row
    for _alias in (_name, *_aliases):
        _BY_NAME.setdefault(_alias, _row)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().strip(".,()"))


def country(text: str) -> Optional[str]:
    """Canonical country name for a name / alias / ISO2 code, else None."""
    k = _key(text)
    if not k:
        return None
    row = _BY_NAME.get(k) or (_BY_ISO.get(k) if len(k) == 2 else None)
    return row[0] if row else None


def city_country(text: str) -> Optional[str]:
    """Canonical country of a known city, else None."""
    iso = _CITIES.get(_key(text))
    return _BY_ISO[iso][0] if iso else None


def is_city(text: str) -> bool:
    return _key(text) in _CITIES


def languages_of(country_name: str) -> Tuple[str, ...]:
    row = _BY_NAME.get(_key(country_name))
    return row[2] if row else ()


def resolve(place: str) -> Tuple[Optional[str], Optional[str]]:
    """(city, country) for a free-text place — "Toronto", "Dubai, UAE",
    "Japan", "Sydney NSW".  Unknown parts stay None."""
    text = (place or "").strip()
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r"[,/|()–—]|\s-\s", text) if p.strip()]
    city_name: Optional[str] = None
    ctry: Optional[str] = None
    for p in parts:
        c = country(p)
        if c and not ctry:
            ctry = c
            continue
        if is_city(p) and not city_name:
            city_name = _key(p)
    if not city_name and not ctry:
        # a known city / country *inside* a longer string ("Sydney NSW")
        low = _key(text)
        for name in sorted(_CITIES, key=len, reverse=True):
            if len(name) > 3 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", low):
                city_name = name
                break
        if not city_name:
            for name in sorted(_BY_NAME, key=len, reverse=True):
                if len(name) > 3 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", low):
                    ctry = _BY_NAME[name][0]
                    break
    if city_name and not ctry:
        ctry = city_country(city_name)
    return city_name, ctry


def place_languages(place: str) -> List[str]:
    """Main languages spoken at a place (city or country)."""
    _, ctry = resolve(place)
    return list(languages_of(ctry)) if ctry else []


def countries_of(places: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in places or []:
        _, c = resolve(p)
        if c and c not in out:
            out.append(c)
    return out


def same_place(job_location: str, requested: str) -> bool:
    """True when ``job_location`` lies in ``requested`` (a city or a country).

    "Sydney NSW" is in "Australia"; "Toronto, ON" is in "Toronto" and in
    "Canada".  Unknown places never match here (callers decide what an
    unknown means).
    """
    req_city, req_country = resolve(requested)
    job_city, job_country = resolve(job_location)
    if req_city:
        # same city name in another country ("London, ON, Canada") is not it
        return (bool(job_city) and job_city == req_city
                and (not job_country or not req_country or job_country == req_country))
    if req_country:
        return bool(job_country) and job_country == req_country
    return False


def is_anywhere(text: str) -> bool:
    return bool(ANYWHERE_RE.search(text or ""))


__all__ = ["country", "city_country", "is_city", "languages_of", "resolve",
           "place_languages", "countries_of", "same_place", "is_anywhere", "ANYWHERE_RE"]
