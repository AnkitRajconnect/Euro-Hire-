"""Tiny offline geocoder for European cities (no API calls, no cost).

Matches a free-text location ("Berlin, Germany", "Amsterdam (Hybrid)") to a
city with coordinates. Falls back to a country centroid. Extend CITIES freely.
"""
from __future__ import annotations

import re
import unicodedata

# name, ISO country, lat, lon, extra aliases
CITIES = [
    ("Berlin", "DE", 52.520, 13.405, []),
    ("Munich", "DE", 48.137, 11.575, ["münchen", "munchen"]),
    ("Hamburg", "DE", 53.551, 9.994, []),
    ("Frankfurt", "DE", 50.110, 8.682, ["frankfurt am main"]),
    ("Cologne", "DE", 50.938, 6.960, ["köln", "koln"]),
    ("Düsseldorf", "DE", 51.228, 6.774, ["dusseldorf"]),
    ("Stuttgart", "DE", 48.776, 9.183, []),
    ("Leipzig", "DE", 51.340, 12.375, []),
    ("Dresden", "DE", 51.050, 13.738, []),
    ("Amsterdam", "NL", 52.368, 4.904, []),
    ("Rotterdam", "NL", 51.924, 4.478, []),
    ("Utrecht", "NL", 52.091, 5.122, []),
    ("Eindhoven", "NL", 51.441, 5.470, []),
    ("The Hague", "NL", 52.078, 4.300, ["den haag"]),
    ("Brussels", "BE", 50.850, 4.352, ["bruxelles", "brussel"]),
    ("Antwerp", "BE", 51.219, 4.402, ["antwerpen"]),
    ("Ghent", "BE", 51.054, 3.722, ["gent"]),
    ("Leuven", "BE", 50.879, 4.701, []),
    ("Paris", "FR", 48.857, 2.352, []),
    ("Lyon", "FR", 45.764, 4.836, []),
    ("Marseille", "FR", 43.296, 5.370, []),
    ("Toulouse", "FR", 43.605, 1.444, []),
    ("Nantes", "FR", 47.218, -1.554, []),
    ("Lille", "FR", 50.629, 3.057, []),
    ("London", "GB", 51.507, -0.128, []),
    ("Manchester", "GB", 53.481, -2.243, []),
    ("Edinburgh", "GB", 55.953, -3.188, []),
    ("Bristol", "GB", 51.454, -2.587, []),
    ("Cambridge", "GB", 52.205, 0.119, []),
    ("Dublin", "IE", 53.350, -6.260, []),
    ("Cork", "IE", 51.898, -8.470, []),
    ("Madrid", "ES", 40.417, -3.704, []),
    ("Barcelona", "ES", 41.385, 2.173, []),
    ("Valencia", "ES", 39.470, -0.376, []),
    ("Lisbon", "PT", 38.722, -9.139, ["lisboa"]),
    ("Porto", "PT", 41.158, -8.629, []),
    ("Milan", "IT", 45.464, 9.190, ["milano"]),
    ("Rome", "IT", 41.903, 12.496, ["roma"]),
    ("Turin", "IT", 45.070, 7.687, ["torino"]),
    ("Zurich", "CH", 47.377, 8.541, ["zürich", "zuerich"]),
    ("Geneva", "CH", 46.204, 6.143, ["genève", "geneve"]),
    ("Lausanne", "CH", 46.520, 6.633, []),
    ("Basel", "CH", 47.560, 7.589, []),
    ("Vienna", "AT", 48.208, 16.374, ["wien"]),
    ("Graz", "AT", 47.071, 15.438, []),
    ("Stockholm", "SE", 59.329, 18.069, []),
    ("Gothenburg", "SE", 57.709, 11.975, ["göteborg", "goteborg"]),
    ("Malmö", "SE", 55.605, 13.004, ["malmo"]),
    ("Copenhagen", "DK", 55.676, 12.568, ["københavn", "kobenhavn"]),
    ("Aarhus", "DK", 56.163, 10.204, []),
    ("Oslo", "NO", 59.914, 10.752, []),
    ("Bergen", "NO", 60.391, 5.322, []),
    ("Helsinki", "FI", 60.170, 24.938, []),
    ("Espoo", "FI", 60.205, 24.656, []),
    ("Tampere", "FI", 61.498, 23.761, []),
    ("Warsaw", "PL", 52.230, 21.012, ["warszawa"]),
    ("Krakow", "PL", 50.065, 19.945, ["kraków", "cracow"]),
    ("Wrocław", "PL", 51.108, 17.039, ["wroclaw"]),
    ("Gdańsk", "PL", 54.352, 18.646, ["gdansk"]),
    ("Prague", "CZ", 50.076, 14.438, ["praha"]),
    ("Brno", "CZ", 49.195, 16.608, []),
    ("Budapest", "HU", 47.498, 19.040, []),
    ("Bucharest", "RO", 44.427, 26.103, ["bucuresti", "bucurești"]),
    ("Cluj-Napoca", "RO", 46.770, 23.590, ["cluj"]),
    ("Sofia", "BG", 42.698, 23.322, []),
    ("Athens", "GR", 37.984, 23.728, []),
    ("Belgrade", "RS", 44.787, 20.457, ["beograd"]),
    ("Zagreb", "HR", 45.815, 15.982, []),
    ("Ljubljana", "SI", 46.056, 14.506, []),
    ("Bratislava", "SK", 48.149, 17.107, []),
    ("Tallinn", "EE", 59.437, 24.754, []),
    ("Riga", "LV", 56.950, 24.105, []),
    ("Vilnius", "LT", 54.687, 25.280, []),
    ("Kyiv", "UA", 50.450, 30.523, ["kiev"]),
    ("Luxembourg", "LU", 49.612, 6.130, ["luxembourg city"]),
    ("Reykjavik", "IS", 64.147, -21.942, ["reykjavík"]),
    ("Valletta", "MT", 35.899, 14.514, []),
    ("Nicosia", "CY", 35.186, 33.382, []),
]

COUNTRIES = {
    "DE": ("Germany", 51.17, 10.45, ["deutschland"]),
    "NL": ("Netherlands", 52.13, 5.29, ["nederland", "holland"]),
    "BE": ("Belgium", 50.50, 4.47, ["belgique", "belgie", "belgië"]),
    "FR": ("France", 46.23, 2.21, []),
    "GB": ("United Kingdom", 54.00, -2.00, ["uk", "england", "scotland", "wales", "great britain"]),
    "IE": ("Ireland", 53.41, -8.24, []),
    "ES": ("Spain", 40.46, -3.75, ["españa", "espana"]),
    "PT": ("Portugal", 39.40, -8.22, []),
    "IT": ("Italy", 41.87, 12.57, ["italia"]),
    "CH": ("Switzerland", 46.82, 8.23, ["schweiz", "suisse"]),
    "AT": ("Austria", 47.52, 14.55, ["österreich", "osterreich"]),
    "SE": ("Sweden", 60.13, 18.64, ["sverige"]),
    "DK": ("Denmark", 56.26, 9.50, ["danmark"]),
    "NO": ("Norway", 60.47, 8.47, ["norge"]),
    "FI": ("Finland", 61.92, 25.75, ["suomi"]),
    "PL": ("Poland", 51.92, 19.15, ["polska"]),
    "CZ": ("Czechia", 49.82, 15.47, ["czech republic"]),
    "HU": ("Hungary", 47.16, 19.50, []),
    "RO": ("Romania", 45.94, 24.97, []),
    "BG": ("Bulgaria", 42.73, 25.49, []),
    "GR": ("Greece", 39.07, 21.82, []),
    "RS": ("Serbia", 44.02, 21.01, []),
    "HR": ("Croatia", 45.10, 15.20, []),
    "SI": ("Slovenia", 46.15, 14.99, []),
    "SK": ("Slovakia", 48.67, 19.70, []),
    "EE": ("Estonia", 58.60, 25.01, []),
    "LV": ("Latvia", 56.88, 24.60, []),
    "LT": ("Lithuania", 55.17, 23.88, []),
    "UA": ("Ukraine", 48.38, 31.17, []),
    "LU": ("Luxembourg", 49.82, 6.13, []),
    "IS": ("Iceland", 64.96, -19.02, []),
    "MT": ("Malta", 35.94, 14.38, []),
    "CY": ("Cyprus", 35.13, 33.43, []),

    "AL": ("Albania", 41.15, 20.17, ["shqipëria", "shqiperia"]),
    "BA": ("Bosnia and Herzegovina", 43.92, 17.68, ["bosnia", "herzegovina"]),
    "ME": ("Montenegro", 42.71, 19.37, ["crna gora"]),
    "MK": ("North Macedonia", 41.61, 21.75, ["macedonia"]),
    "MD": ("Moldova", 47.01, 28.86, ["republic of moldova"]),
    "TR": ("Turkey", 39.00, 35.00, ["Türkiye", "turkiye"]),
    "XK": ("Kosovo", 42.60, 20.90, ["kosova"]),
    "AD": ("Andorra", 42.55, 1.60, []),
    "LI": ("Liechtenstein", 47.14, 9.52, []),
    "MC": ("Monaco", 43.74, 7.42, []),
    "SM": ("San Marino", 43.94, 12.46, []),
    "VA": ("Vatican City", 41.90, 12.45, ["holy see"]),
    "BY": ("Belarus", 53.71, 27.95, []),
    "AM": ("Armenia", 40.18, 44.51, []),
    "GE": ("Georgia", 42.32, 43.36, []),
    "AZ": ("Azerbaijan", 40.14, 47.58, []),
}

EUROPE = set(COUNTRIES)
EU_BLOC = EUROPE - {"GB", "CH", "NO", "IS", "UA", "RS"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _word_re(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z])" + re.escape(_norm(term)) + r"(?![a-z])")


_CITY_INDEX = []
for name, cc, lat, lon, aliases in CITIES:
    for term in [name, *aliases]:
        _CITY_INDEX.append((_word_re(term), name, cc, lat, lon))
# Longer / more specific names first ("frankfurt am main" before "frankfurt")
_CITY_INDEX.sort(key=lambda t: -len(t[0].pattern))

_COUNTRY_INDEX = []
for cc, (name, lat, lon, aliases) in COUNTRIES.items():
    for term in [name, *aliases]:
        _COUNTRY_INDEX.append((_word_re(term), cc))


def locate_all(text: str | None, country_hint: str | None = None):
    """Return every distinct European city explicitly named in a location string.

    This handles ATS locations such as "Paris, London or Berlin" without
    pretending the vacancy is only in the first city. Country-only locations
    remain represented as a country centroid.
    """
    if not text and not country_hint:
        return []
    t = _norm(text or "")
    found = []
    seen = set()
    for rx, name, cc, lat, lon in _CITY_INDEX:
        if country_hint and country_hint != cc:
            continue
        if rx.search(t) and (name, cc) not in seen:
            found.append({"city": name, "country": cc, "lat": lat, "lon": lon})
            seen.add((name, cc))
    if found:
        return found
    place = locate(text, country_hint)
    return [place] if place else []


def locate(text: str | None, country_hint: str | None = None):
    """Return dict(city, country, lat, lon) or None. country_hint is an ISO code."""
    if not text and not country_hint:
        return None
    t = _norm(text or "")
    for rx, name, cc, lat, lon in _CITY_INDEX:
        if rx.search(t):
            if country_hint and country_hint != cc:
                # e.g. "Cambridge, MA" with hint US must not become Cambridge, UK
                continue
            return {"city": name, "country": cc, "lat": lat, "lon": lon}
    cc = country_hint if country_hint in COUNTRIES else None
    if not cc:
        for rx, code in _COUNTRY_INDEX:
            if rx.search(t):
                cc = code
                break
    if cc:
        _, lat, lon, _ = COUNTRIES[cc]
        return {"city": None, "country": cc, "lat": lat, "lon": lon}
    return None
