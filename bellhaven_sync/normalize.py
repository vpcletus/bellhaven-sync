"""Normalization helpers. Pure functions, no I/O."""
import re
from difflib import SequenceMatcher

# Street-suffix and directional canonical forms (USPS-style short forms).
_TOKEN_MAP = {
    "street": "st", "str": "st",
    "avenue": "ave", "av": "ave",
    "road": "rd",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "parkway": "pkwy",
    "highway": "hwy",
    "pike": "pike", "pk": "pike", "pke": "pike",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st",
}

_PO_BOX = re.compile(r"^\s*p\.?\s*o\.?\s*box\b", re.I)


def norm_street(street: str) -> str:
    """'4850 Northwest Sylvania Avenue' -> '4850 nw sylvania ave'."""
    if not street:
        return ""
    s = street.lower()
    s = re.sub(r"[.,#]", " ", s)
    s = re.sub(r"\b(suite|ste|unit|apt)\s*\w+", " ", s)  # drop unit designators
    tokens = [_TOKEN_MAP.get(t, t) for t in s.split()]
    return " ".join(tokens)


def is_po_box(street: str) -> bool:
    return bool(street and _PO_BOX.match(street))


def norm_zip(z: str) -> str:
    return (z or "").strip()[:5]


def norm_city(c: str) -> str:
    return re.sub(r"\s+", " ", (c or "").strip().lower())


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


_NAME_NOISE = {"bellhaven", "the", "of", "at", "and", "senior", "living"}


def norm_name(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def name_core(name: str) -> str:
    """Name with brand/filler words removed: 'Bellhaven of Owosso' -> 'owosso'."""
    return " ".join(t for t in norm_name(name).split() if t not in _NAME_NOISE)


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def person(name: str) -> str:
    return norm_name(name)


# Website care offering -> CRM care_type vocabulary (values observed in the CRM).
CARE_MAP = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "independent living": "Independent Living",
}


def crm_care_types(offerings):
    out = []
    for o in offerings:
        mapped = CARE_MAP.get(o.strip().lower())
        if mapped and mapped not in out:
            out.append(mapped)
    return out
