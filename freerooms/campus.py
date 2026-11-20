"""One spelling per campus, shared by rooms and study places of both universities.

ETH calls its sites "Zürich Zentrum" / "Zürich Hönggerberg" (Rauminfo) or ZZ / ZH / AC (study
spaces), UZH says "Zentrum", "Irchel", "Oerlikon". We store the short name only. ETH Zentrum
and UZH Zentrum are next to each other, so both are simply "Zentrum"; the `uni` column tells
them apart where that matters.
"""

from __future__ import annotations

ZENTRUM, HOENGG, IRCHEL, OERLIKON, BASEL = "Zentrum", "Hönggerberg", "Irchel", "Oerlikon", "Basel"
CAMPUSES = (ZENTRUM, HOENGG, IRCHEL, OERLIKON, BASEL)   # display order

_ALIASES = {
    "zürich zentrum": ZENTRUM, "zuerich zentrum": ZENTRUM, "zentrum": ZENTRUM, "zz": ZENTRUM,
    "uzh zentrum": ZENTRUM, "eth zentrum": ZENTRUM,
    "zürich hönggerberg": HOENGG, "zuerich hoenggerberg": HOENGG, "hönggerberg": HOENGG,
    "hoenggerberg": HOENGG, "zh": HOENGG, "eth hönggerberg": HOENGG,
    "irchel": IRCHEL, "uzh irchel": IRCHEL,
    "oerlikon": OERLIKON, "uzh oerlikon": OERLIKON,
    "basel": BASEL, "ac": BASEL,
    # UZH's Botanischer Garten (Zollikerstrasse) is ~1.5 km from Zentrum; no campus chip of its own.
    "botanischer garten": ZENTRUM,
}


def canonical(name: str | None) -> str | None:
    """The shared campus name for any spelling a source uses; unknown names pass through unchanged."""
    if name is None:
        return None
    return _ALIASES.get(name.strip().lower(), name.strip() or None)
