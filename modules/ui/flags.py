"""Country «icons» for the console: a 3x2-pixel flag drawn with upper-half blocks (▀, fg = top pixel, bg = bottom).

Windows has no colour flag emoji (🇩🇪 is drawn as two letters), but Windows Terminal draws 24-bit colour, so a flag is
three `▀` characters with their own colours. Only in the emoji icon set (Windows Terminal and other modern terminals,
`modules.ui.icons.EMOJI`); the plain set (old conhost) shows no flag, only the country code next to it.
Two pixels in height cannot hold three horizontal stripes: those flags alternate the stripes over the three columns.
"""

from __future__ import annotations

from modules.ui import icons

Fragment = tuple[str, str]

_C = {
    "K": "#000000", "W": "#ffffff", "R": "#d80027", "Y": "#ffce00", "G": "#009246", "B": "#0039a6",
    "N": "#012169", "L": "#0057b7", "U": "#ffd700", "O": "#ff8200", "S": "#ed2939", "T": "#00afca",
    "M": "#9e3039", "C": "#74acdf", "V": "#006aa7", "F": "#003580", "D": "#6ab2e7",
}

# Stripes in order: "h" top to bottom, "v" left to right.
_STRIPES = {
    "DE": ("h", "KRY"), "RU": ("h", "WBR"), "NL": ("h", "RWN"), "AT": ("h", "RWR"), "HU": ("h", "RWG"),
    "BG": ("h", "WGR"), "LT": ("h", "YGR"), "EE": ("h", "BKW"), "LV": ("h", "MWM"), "AM": ("h", "RBO"),
    "AZ": ("h", "LRG"), "CO": ("h", "YBR"), "ES": ("h", "RYR"), "IN": ("h", "OWG"), "EG": ("h", "RWK"),
    "IR": ("h", "GWR"), "IQ": ("h", "RWK"), "LU": ("h", "RWD"), "AR": ("h", "CWC"), "BO": ("h", "RYG"),
    "GH": ("h", "RYG"), "SK": ("h", "WBR"), "SI": ("h", "WBR"), "RS": ("h", "RBW"), "HR": ("h", "RWB"),
    "UZ": ("h", "LWG"), "TJ": ("h", "RWG"), "TH": ("h", "RWN"), "YE": ("h", "RWK"), "KE": ("h", "KRG"),
    "PL": ("h", "WR"), "UA": ("h", "LU"), "ID": ("h", "RW"), "MC": ("h", "RW"), "SG": ("h", "SW"),
    "BY": ("h", "RG"), "KZ": ("h", "T"), "KG": ("h", "R"), "SA": ("h", "G"),
    "FR": ("v", "NWR"), "IT": ("v", "GWR"), "IE": ("v", "GWO"), "BE": ("v", "KYR"), "MX": ("v", "GWR"),
    "NG": ("v", "GWG"), "PE": ("v", "RWR"), "CA": ("v", "RWR"), "RO": ("v", "BYR"), "MD": ("v", "BYR"),
    "CI": ("v", "OWG"), "ML": ("v", "GYR"), "TD": ("v", "BYR"),
}

# Flags that are not plain stripes: (top row, bottom row), one colour letter per column.
_ART = {
    "US": ("NRR", "RWW"), "GB": ("NRN", "NRN"), "JP": ("WRW", "WRW"), "KR": ("WRW", "WBW"), "CN": ("YRR", "RRR"),
    "TR": ("WRR", "RRR"), "VN": ("RYR", "RYR"), "CH": ("RWR", "RWR"), "DK": ("RWR", "RWR"), "SE": ("VUV", "VUV"),
    "NO": ("RNR", "RNR"), "FI": ("WFW", "WFW"), "IS": ("BRB", "BRB"), "BR": ("GYG", "GBG"), "AU": ("RNN", "NNN"),
    "NZ": ("RNN", "NNN"), "IL": ("WLW", "WLW"), "AE": ("RGW", "RWK"), "PK": ("WGG", "WGG"), "HK": ("RRR", "RWR"),
    "TW": ("NRR", "RRR"), "GR": ("LWL", "WLW"), "PT": ("GRR", "GRR"), "CZ": ("NWW", "NRR"), "PH": ("WNN", "WRR"),
    "MY": ("NRR", "RWW"), "CL": ("BWW", "RRR"), "GE": ("WRW", "WRW"),
}


def _columns(code: str) -> list[tuple[str, str]] | None:
    """(top, bottom) colour letters of the three columns, None for a country without a drawing."""
    if code in _ART:
        top, bottom = _ART[code]
        return list(zip(top, bottom))
    if code not in _STRIPES:
        return None
    kind, stripes = _STRIPES[code]
    if kind == "v":
        return [(s, s) for s in (stripes * 3)[:3]] if len(stripes) == 1 else [(s, s) for s in stripes[:3]]
    if len(stripes) == 1:
        return [(stripes, stripes)] * 3
    if len(stripes) == 2:
        return [(stripes[0], stripes[1])] * 3
    first, middle, last = stripes[:3]
    return [(first, middle), (middle, last), (first, middle)]


def flag(country: str | None) -> tuple[Fragment, ...]:
    """prompt_toolkit fragments of the country's mini flag (three `▀`), () when it has none or emoji are off."""
    if not icons.EMOJI or not country:
        return ()
    columns = _columns(country.strip().upper()[:2])
    if columns is None:
        return ()
    return tuple((f"fg:{_C[top]} bg:{_C[bottom]}", "▀") for top, bottom in columns)


FLAG_COLUMNS = 4  # three pixels and a space


def flag_prefix(country: str | None) -> tuple[Fragment, ...]:
    """The flag and a space before a text; blanks of the same width for a country without a flag (rows stay
    aligned), () when emoji are off."""
    if not icons.EMOJI:
        return ()
    drawn = flag(country)
    return (drawn + (("", " "),)) if drawn else (("", " " * FLAG_COLUMNS),)


__all__ = ["FLAG_COLUMNS", "flag", "flag_prefix"]
