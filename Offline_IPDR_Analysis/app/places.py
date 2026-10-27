"""Pakistan cities and ISPs, including the short names people type.

A city is matched inside loc, the street address. An ISP is matched inside
client_asn, the provider name stored for the subscriber. The needles are the
words that actually appear in those columns, not only the abbreviation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    kind: str
    name: str
    aliases: tuple[str, ...]
    needles: tuple[str, ...]


def _city(name: str, *aliases: str) -> Place:
    needles = (name.lower(),) + tuple(alias.lower() for alias in aliases)
    return Place("city", name, (name.lower(),) + tuple(alias.lower() for alias in aliases), needles)


def _isp(name: str, needles: tuple[str, ...], *aliases: str) -> Place:
    spoken = (name.lower(),) + tuple(alias.lower() for alias in aliases)
    stored = tuple(dict.fromkeys(item.lower() for item in needles))
    return Place("isp", name, spoken, stored)


CITIES: tuple[Place, ...] = (
    _city("Islamabad", "isb"),
    _city("Rawalpindi", "rwp", "pindi"),
    _city("Lahore", "lhr"),
    _city("Karachi", "khi"),
    _city("Faisalabad", "fsd", "lyallpur"),
    _city("Multan", "mux"),
    _city("Peshawar", "pew"),
    _city("Quetta", "qta"),
    _city("Hyderabad", "hyd"),
    _city("Gujranwala", "grw"),
    _city("Sialkot", "skt"),
    _city("Bahawalpur", "bwp"),
    _city("Gujrat", "gjt"),
    _city("Abbottabad", "atd"),
    _city("Muzaffarabad", "mzd"),
    _city("Dera Ismail Khan", "di khan", "d i khan", "d.i. khan", "d.i.khan", "dik"),
    _city("Dera Ghazi Khan", "dg khan", "d g khan", "d.g. khan", "d.g.khan", "dgk"),
    _city("Rahim Yar Khan", "ryk"),
    _city("Toba Tek Singh", "tts"),
    _city("Mandi Bahauddin", "mb din"),
    _city("Nankana Sahib"),
    _city("Shaheed Benazirabad", "nawabshah", "benazirabad"),
    _city("Kambar Shahdadkot", "shahdadkot", "kambar"),
    _city("Mirpur Khas"),
    _city("Tando Allahyar"),
    _city("Tando Muhammad Khan"),
    _city("Naushahro Feroze"),
    _city("North Waziristan", "miranshah"),
    _city("South Waziristan", "wana"),
    _city("Attock"),
    _city("Jhelum"),
    _city("Chakwal"),
    _city("Talagang"),
    _city("Murree"),
    _city("Sargodha"),
    _city("Bhakkar"),
    _city("Khushab"),
    _city("Mianwali"),
    _city("Jhang"),
    _city("Chiniot"),
    _city("Hafizabad"),
    _city("Wazirabad"),
    _city("Narowal"),
    _city("Kasur"),
    _city("Sheikhupura"),
    _city("Sahiwal"),
    _city("Okara"),
    _city("Pakpattan"),
    _city("Vehari"),
    _city("Lodhran"),
    _city("Khanewal"),
    _city("Rajanpur"),
    _city("Layyah"),
    _city("Muzaffargarh"),
    _city("Kot Addu"),
    _city("Taunsa"),
    _city("Bahawalnagar"),
    _city("Wah Cantt", "wah"),
    _city("Taxila"),
    _city("Gujar Khan"),
    _city("Kahuta"),
    _city("Mardan"),
    _city("Swabi"),
    _city("Charsadda"),
    _city("Nowshera"),
    _city("Kohat"),
    _city("Hangu"),
    _city("Karak"),
    _city("Bannu"),
    _city("Lakki Marwat"),
    _city("Tank"),
    _city("Swat", "mingora", "saidu sharif"),
    _city("Shangla"),
    _city("Buner"),
    _city("Malakand"),
    _city("Upper Dir", "dir"),
    _city("Lower Dir", "timergara"),
    _city("Upper Chitral", "chitral"),
    _city("Lower Chitral"),
    _city("Bajaur", "khar"),
    _city("Upper Kohistan", "dassu"),
    _city("Lower Kohistan", "pattan"),
    _city("Kolai Palas"),
    _city("Mansehra"),
    _city("Torghar", "judba"),
    _city("Batagram"),
    _city("Haripur"),
    _city("Khyber", "landi kotal"),
    _city("Mohmand", "ghalanai"),
    _city("Orakzai", "kalaya"),
    _city("Kurram", "parachinar"),
    _city("Sukkur"),
    _city("Ghotki"),
    _city("Khairpur"),
    _city("Jacobabad"),
    _city("Kashmore", "kandhkot"),
    _city("Shikarpur"),
    _city("Larkana"),
    _city("Sanghar"),
    _city("Dadu"),
    _city("Jamshoro"),
    _city("Matiari"),
    _city("Badin"),
    _city("Thatta"),
    _city("Sujawal"),
    _city("Umerkot"),
    _city("Tharparkar", "mithi"),
    _city("Korangi"),
    _city("Malir"),
    _city("Keamari"),
    _city("Pishin"),
    _city("Killa Abdullah"),
    _city("Chaman"),
    _city("Chagai"),
    _city("Nushki"),
    _city("Kharan"),
    _city("Washuk"),
    _city("Loralai"),
    _city("Duki"),
    _city("Barkhan"),
    _city("Musakhel"),
    _city("Killa Saifullah"),
    _city("Zhob"),
    _city("Sherani"),
    _city("Sibi"),
    _city("Harnai"),
    _city("Ziarat"),
    _city("Kohlu"),
    _city("Dera Bugti"),
    _city("Jaffarabad", "dera allah yar"),
    _city("Nasirabad", "dera murad jamali"),
    _city("Kachhi", "bolan", "dhadar"),
    _city("Jhal Magsi", "gandawah"),
    _city("Sohbatpur"),
    _city("Kalat"),
    _city("Surab"),
    _city("Mastung"),
    _city("Khuzdar"),
    _city("Awaran"),
    _city("Lasbela", "hub", "uthal"),
    _city("Kech", "turbat"),
    _city("Gwadar"),
    _city("Panjgur"),
    _city("Gilgit"),
    _city("Hunza"),
    _city("Nagar"),
    _city("Ghizer", "gahkuch"),
    _city("Skardu"),
    _city("Kharmang"),
    _city("Shigar"),
    _city("Ghanche", "khaplu"),
    _city("Astore"),
    _city("Diamer", "chilas"),
    _city("Hattian Bala"),
    _city("Neelum", "athmuqam"),
    _city("Mirpur"),
    _city("Bhimber"),
    _city("Kotli"),
    _city("Poonch", "rawalakot"),
    _city("Bagh"),
    _city("Haveli", "forward kahuta"),
    _city("Sudhanoti", "pallandri"),
)


# needles are the substrings stored in client_asn. aliases are what a person types.
ISPS: tuple[Place, ...] = (
    _isp(
        "PTCL",
        ("pakistan telecommunication", "ptcl", "flash fiber", "flashfiber"),
        "ptcl",
        "pakistan telecommunications limited",
        "pakistan telecommunication company limited",
        "flashfiber",
        "flash fiber",
    ),
    _isp("Nayatel", ("nayatel", "naya tel"), "naya tel"),
    _isp(
        "Transworld",
        ("transworld", "trans world", "twa"),
        "twa",
        "trans world",
        "transworld associates",
        "trans world enterprise",
    ),
    _isp(
        "Jazz",
        ("jazz", "mobilink", "pmcl", "pakistan mobile communication", "warid"),
        "pmcl",
        "mobilink",
        "warid",
    ),
    _isp("Zong", ("zong", "cmpak", "china mobile"), "cmpak", "china mobile"),
    _isp("Telenor", ("telenor",),),
    _isp(
        "Ufone",
        ("ufone", "ptml", "pak telecom"),
        "ptml",
        "pak telecom mobile",
    ),
    _isp("Onic", ("onic", "ufone", "ptml"),),
    _isp("StormFiber", ("stormfiber", "storm fiber", "cybernet"), "storm fiber", "cybernet"),
    _isp("Wateen", ("wateen",),),
    _isp("WorldCall", ("worldcall", "world call"), "world call"),
    _isp("Optix", ("optix",),),
    _isp("Multinet", ("multinet",),),
    _isp("Qubee", ("qubee",),),
    _isp("Wi-tribe", ("witribe", "wi tribe", "wi-tribe"), "witribe", "wi tribe"),
    _isp("SCO", ("special communications", "sco"), "special communications organization", "scom"),
    _isp(
        "NTC",
        ("national telecommunication corporation", "ntc"),
        "national telecommunication corporation",
    ),
    _isp("Satcomm", ("satcomm", "sat comm"), "sat comm"),
    _isp("Comsats", ("comsats",),),
    _isp("Supernet", ("supernet",),),
    _isp("CubeXS", ("cubexs", "cube xs"), "cube xs"),
    _isp("Telecard", ("telecard",),),
    _isp("BrainNET", ("brainnet", "brain net"), "brain net", "fiberlink"),
    _isp("LINKdotNET", ("linkdotnet", "link dot net"), "link dot net"),
    _isp("DVCom", ("dvcom",),),
    _isp("Redtone", ("redtone",),),
    _isp("Vision Telecom", ("vision telecom",),),
)


_PREPOSITION = r"(?:from|in|near|within|at|of)"
_ALIAS_INDEX: list[tuple[str, Place]] = sorted(
    ((alias, place) for place in (*CITIES, *ISPS) for alias in place.aliases),
    key=lambda item: len(item[0]),
    reverse=True,
)


_AREA_WORD = r"(?:(?!in\b|of\b|from\b|near\b|within\b|at\b|by\b|for\b|on\b|with\b|and\b)[a-z][a-z0-9-]{1,30})"
_AREA_OF = re.compile(
    rf"(?i)\b(?:in\s+the\s+)?(?:area|locality|neighborhood|neighbourhood|vicinity)\s+of\s+"
    rf"({_AREA_WORD}(?:\s+{_AREA_WORD}){{0,3}})"
)
_NAME_AREA = re.compile(
    rf"(?i)\b({_AREA_WORD}(?:\s+{_AREA_WORD}){{0,2}})\s+"
    rf"(?:area|locality|neighborhood|neighbourhood)\b"
)
_AREA_STOP = {
    "the", "this", "that", "these", "those", "same", "city", "user", "users",
    "area", "in", "of", "near", "and", "all",
}


def mentioned_areas(question: str) -> list[str]:
    """A neighbourhood inside a city, as in 'in the area of Gulberg in Lahore'."""
    folded = re.sub(r"\s+", " ", question.lower().replace(".", " "))
    city_names = {alias for alias, place in _ALIAS_INDEX if place.kind == "city"}
    found: list[str] = []

    def add(raw: str) -> None:
        name = re.sub(r"\s+", " ", raw).strip(" -")
        words = name.split()
        if not name or name in city_names or any(word in _AREA_STOP for word in words):
            return
        if name not in found:
            found.append(name)

    for match in _AREA_OF.finditer(folded):
        add(match.group(1))
    for match in _NAME_AREA.finditer(folded):
        add(match.group(1))
    return found


_STREET_STOP = {
    "the", "a", "an", "of", "area", "locality", "neighborhood", "neighbourhood", "vicinity",
}


def street_before_city(question: str) -> list[str]:
    """Words of a neighbourhood that sit between 'in' and a city.

    'in lake city lahore' is the address, not an application. The stored cell
    often has other text between those words, so the match is lake, then city,
    then lahore.
    """
    folded = re.sub(r"\s+", " ", question.lower().replace(".", " ").replace(",", " ")).strip()
    occupied = [False] * len(folded)
    best: list[str] = []
    best_at = -1
    for alias, place in _ALIAS_INDEX:
        if place.kind != "city":
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        for match in re.finditer(pattern, folded):
            if any(occupied[match.start():match.end()]):
                continue
            for index in range(match.start(), match.end()):
                occupied[index] = True
            preps = list(re.finditer(rf"\b{_PREPOSITION}\s+", folded[: match.start()]))
            if not preps:
                continue
            middle = folded[preps[-1].end(): match.start()].strip()
            words = [word for word in middle.split() if word not in _STREET_STOP]
            if words and match.start() >= best_at:
                best = words + [place.name.lower()]
                best_at = match.start()
    return best


def street_predicate(words: list[str]) -> str:
    clean = [word.replace("'", "") for word in words if word.replace("'", "")]
    return "LOWER(loc) LIKE '%" + "%".join(clean) + "%'"


def area_predicate(area: str) -> str:
    """Match the neighbourhood words in order, with anything between them.

    'DHA Lahore' is stored as 'DHA Phase 5 Lahore' and 'DHA, Lahore', so the
    predicate is LIKE '%dha%lahore%', not LIKE '%dha lahore%'.
    """
    words = [word for word in re.split(r"\s+", area.lower().replace("'", "").strip()) if word]
    return "LOWER(loc) LIKE '%" + "%".join(words) + "%'"


def without_areas(question: str) -> str:
    """Remove a named neighbourhood so it is not guessed as an application."""
    text = question
    for area in mentioned_areas(question):
        text = re.sub(
            rf"(?i)\b(?:in\s+the\s+)?(?:area|locality|neighborhood|neighbourhood|vicinity)\s+of\s+{re.escape(area)}\b",
            " ",
            text,
        )
        text = re.sub(
            rf"(?i)\b{re.escape(area)}\s+(?:area|locality|neighborhood|neighbourhood)\b",
            " ",
            text,
        )
    return text


@dataclass(frozen=True)
class Province:
    name: str
    aliases: tuple[str, ...]
    cities: tuple[str, ...]


def _province(name: str, aliases: tuple[str, ...], *cities: str) -> Province:
    return Province(name, (name.lower(),) + tuple(alias.lower() for alias in aliases), cities)


PROVINCES: tuple[Province, ...] = (
    _province(
        "Punjab",
        (),
        "Rawalpindi", "Lahore", "Faisalabad", "Multan", "Gujranwala", "Sialkot", "Bahawalpur",
        "Gujrat", "Dera Ghazi Khan", "Rahim Yar Khan", "Toba Tek Singh", "Mandi Bahauddin",
        "Nankana Sahib", "Attock", "Jhelum", "Chakwal", "Talagang", "Murree", "Sargodha",
        "Bhakkar", "Khushab", "Mianwali", "Jhang", "Chiniot", "Hafizabad", "Wazirabad",
        "Narowal", "Kasur", "Sheikhupura", "Sahiwal", "Okara", "Pakpattan", "Vehari",
        "Lodhran", "Khanewal", "Rajanpur", "Layyah", "Muzaffargarh", "Kot Addu", "Taunsa",
        "Bahawalnagar", "Wah Cantt", "Taxila", "Gujar Khan", "Kahuta",
    ),
    _province(
        "Sindh",
        ("sind",),
        "Karachi", "Hyderabad", "Shaheed Benazirabad", "Kambar Shahdadkot", "Mirpur Khas",
        "Tando Allahyar", "Tando Muhammad Khan", "Naushahro Feroze", "Sukkur", "Ghotki",
        "Khairpur", "Jacobabad", "Kashmore", "Shikarpur", "Larkana", "Sanghar", "Dadu",
        "Jamshoro", "Matiari", "Badin", "Thatta", "Sujawal", "Umerkot", "Tharparkar",
        "Korangi", "Malir", "Keamari",
    ),
    _province(
        "Khyber Pakhtunkhwa",
        ("kpk", "kp", "nwfp"),
        "Peshawar", "Abbottabad", "Dera Ismail Khan", "North Waziristan", "South Waziristan",
        "Mardan", "Swabi", "Charsadda", "Nowshera", "Kohat", "Hangu", "Karak", "Bannu",
        "Lakki Marwat", "Tank", "Swat", "Shangla", "Buner", "Malakand", "Upper Dir",
        "Lower Dir", "Upper Chitral", "Lower Chitral", "Bajaur", "Upper Kohistan",
        "Lower Kohistan", "Kolai Palas", "Mansehra", "Torghar", "Batagram", "Haripur",
        "Khyber", "Mohmand", "Orakzai", "Kurram",
    ),
    _province(
        "Balochistan",
        ("baluchistan",),
        "Quetta", "Pishin", "Killa Abdullah", "Chaman", "Chagai", "Nushki", "Kharan",
        "Washuk", "Loralai", "Duki", "Barkhan", "Musakhel", "Killa Saifullah", "Zhob",
        "Sherani", "Sibi", "Harnai", "Ziarat", "Kohlu", "Dera Bugti", "Jaffarabad",
        "Nasirabad", "Kachhi", "Jhal Magsi", "Sohbatpur", "Kalat", "Surab", "Mastung",
        "Khuzdar", "Awaran", "Lasbela", "Kech", "Gwadar", "Panjgur",
    ),
    _province(
        "Gilgit-Baltistan",
        ("gilgit baltistan", "gb"),
        "Gilgit", "Hunza", "Nagar", "Ghizer", "Skardu", "Kharmang", "Shigar", "Ghanche",
        "Astore", "Diamer",
    ),
    _province(
        "Azad Kashmir",
        ("ajk", "azad jammu and kashmir"),
        "Muzaffarabad", "Hattian Bala", "Neelum", "Mirpur", "Bhimber", "Kotli", "Poonch",
        "Bagh", "Haveli", "Sudhanoti",
    ),
    _province("Islamabad Capital Territory", ("ict",), "Islamabad"),
)


def mentioned_provinces(question: str) -> list[Province]:
    """A province named anywhere, as in 'fixed-line users from Punjab'."""
    folded = re.sub(r"\s+", " ", question.lower().replace(".", " ").replace("-", " "))
    found: list[Province] = []
    for province in PROVINCES:
        for alias in sorted(province.aliases, key=len, reverse=True):
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded):
                found.append(province)
                break
    return found


def province_predicate(province: Province) -> str:
    """Match the province name or any of its district headquarters in the street address."""
    needles: list[str] = []
    for needle in (province.name, *province.aliases, *province.cities):
        text = needle.lower().replace("'", "").strip()
        if text and text not in needles:
            needles.append(text)
    return "(" + " OR ".join(f"LOWER(loc) LIKE '%{needle}%'" for needle in needles) + ")"


def mentioned_cities(question: str) -> list[Place]:
    """A city named anywhere, as in 'Multan PTCL WhatsApp users'."""
    folded = re.sub(r"\s+", " ", question.lower().replace(".", " "))
    found: list[Place] = []
    seen: set[str] = set()
    for alias, place in _ALIAS_INDEX:
        if place.kind != "city" or place.name in seen:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded):
            found.append(place)
            seen.add(place.name)
    return found


def mentioned_isps(question: str) -> list[Place]:
    """An ISP named anywhere, as in 'WhatsApp Nayatel users'."""
    folded = re.sub(r"\s+", " ", question.lower().replace(".", " "))
    found: list[Place] = []
    seen: set[str] = set()
    for alias, place in _ALIAS_INDEX:
        if place.kind != "isp" or place.name in seen:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded):
            found.append(place)
            seen.add(place.name)
    return found


def asks_same_city(question: str) -> bool:
    return bool(re.search(r"\b(?:same|that|this)\s+city\b", question.lower()))


def named_in(question: str) -> list[Place]:
    """Cities and ISPs named after from, in, near, within, or at."""
    folded = question.lower().replace(".", " ")
    folded = re.sub(r"\s+", " ", folded)
    found: list[Place] = []
    seen: set[str] = set()
    occupied = [False] * len(folded)
    for alias, place in _ALIAS_INDEX:
        if place.name in seen:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        for match in re.finditer(pattern, folded):
            if any(occupied[match.start():match.end()]):
                continue
            prefix = folded[: match.start()]
            if not re.search(rf"\b{_PREPOSITION}\s+$", prefix):
                continue
            found.append(place)
            seen.add(place.name)
            for index in range(match.start(), match.end()):
                occupied[index] = True
            break
    return found


def column_predicate(column: str, place: Place) -> str:
    """Match any stored form of this city or ISP. The value is a substring, not the whole cell."""
    parts = []
    seen: set[str] = set()
    for needle in place.needles:
        text = needle.lower().replace("'", "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(f"LOWER({column}) LIKE '%{text}%'")
    return "(" + " OR ".join(parts) + ")"


def without_named(question: str) -> str:
    """Remove a named city or ISP so it is not guessed as an application."""
    text = question
    for place in named_in(question):
        for alias in sorted(place.aliases, key=len, reverse=True):
            text = re.sub(rf"(?i)(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", " ", text)
    return text
