"""
Builds data/places.json and data/homes.json. Run by hand, not in the service:

    python build_places.py <countries.json> <cities15000.txt> <admin1CodesASCII.txt> <headlines.jsonl>

countries.json is the world-countries package (ODbL); the others are GeoNames (CC BY).
headlines.jsonl has one {"title", "source"} per line: an outlet's country is the one
at least half of its located headlines name, given 8 or more of them.
"""

import collections
import json
import sys
from pathlib import Path

from places import Places

MIN_CITY_POPULATION = 300_000
STATE_COUNTRIES = {"US", "IN", "AU", "CA", "NG", "KE", "ZA", "PK", "GB", "BR", "MX", "PH", "MY", "ID", "DE", "IT", "ES", "FR", "CN"}
HOME_MIN_HITS, HOME_MIN_SHARE = 8, 0.5
OUTRANKS = 5  # A name used in two countries goes to one only if that one is this many times bigger
COUNTRY_WEIGHT, CAPITAL_WEIGHT, STATE_WEIGHT, ALIAS_WEIGHT = 1e12, 1e9, 5e6, 1e13

# Ordinary words, sports clubs' towns and names shared across countries
LEFT_OUT = {
    "Nice", "Bath", "Reading", "Mobile", "Orange", "Split", "Male", "Best", "Bar", "March", "Sale", "Hollywood",
    "Santa", "Mercury", "Georgia", "Mary", "Most", "Hope", "Victoria", "Wellington", "Birmingham", "Newcastle",
    "Cambridge", "York", "Boston", "Lincoln", "Madison", "Jackson", "Charlotte", "Austin", "Memphis", "Marks",
    "Union", "Federal", "Central", "North", "South", "East", "West", "Northern", "Southern", "Eastern", "Western",
    "Capital", "Coast", "Lake", "River", "Islands", "Center", "Centre", "Enterprise", "Liberty", "Independence",
    "Real", "Sucre", "Hamilton", "Kingston", "Bergen", "Essex", "Kent", "Norfolk", "Suffolk", "Bristol", "Oxford",
    "Leeds", "Everton", "Sevilla", "Bilbao",
}
# Leaders, institutions and names the country data lacks
ALIASES = {
    "Trump": "US", "Biden": "US", "Hegseth": "US", "Rubio": "US", "Vance": "US", "Republican": "US",
    "Republicans": "US", "Democrats": "US", "Modi": "IN", "Putin": "RU", "Kremlin": "RU", "Zelensky": "UA",
    "Zelenskyy": "UA", "Netanyahu": "IL", "Starmer": "GB", "Macron": "FR", "Merz": "DE", "Xi": "CN", "Kim Jong": "KP",
    "Khamenei": "IR", "Tinubu": "NG", "Ruto": "KE", "Ramaphosa": "ZA", "Albanese": "AU", "Carney": "CA", "Lula": "BR",
    "Bolsonaro": "BR", "Marcos": "PH", "Anwar": "MY", "Prabowo": "ID", "Erdogan": "TR", "Sheinbaum": "MX",
    "Scotland": "GB", "Scottish": "GB", "Wales": "GB", "Welsh": "GB", "England": "GB", "Britain": "GB", "British": "GB",
    "Gaza": "PS", "Palestinian": "PS", "Palestinians": "PS", "West Bank": "PS", "Hong Kong": "HK", "Taiwan": "TW",
    "Kashmir": "IN", "Holland": "NL", "Dutch": "NL", "N. Korea": "KP", "N. Korean": "KP", "North Korean": "KP",
    "S. Korea": "KR", "South Korean": "KR", "Seoul": "KR", "Pyongyang": "KP", "Aussie": "AU", "Kiwi": "NZ",
    "Kiwis": "NZ", "Turkey": "TR", "Niger Republic": "NE",
}
ABBREVIATIONS = {"US": "US", "U.S.": "US", "USA": "US", "UK": "GB", "U.K.": "GB", "UAE": "AE", "DRC": "CD", "PNG": "PG", "NZ": "NZ"}
NOT_NAMES = {"English", "Korean"}
# They cover the world, so a headline naming no place says nothing about their country
GLOBAL_OUTLETS = {
    "AP News", "BBC", "Bloomberg", "Business Insider", "CNBC", "CNN", "Forbes", "Fortune", "MSN", "Newsweek", "Reuters",
    "The Guardian", "The Independent", "The New York Times", "The Washington Post", "Yahoo", "Yahoo News",
}


def build_terms(countries, cities_path, admin1_path):
    terms = {}  # Name: [country, lat, lon, weight]; country None when two places claim it equally

    def add(name, country, weight, lat=None, lon=None):
        name = name.strip()
        if len(name) < 4 or name in LEFT_OUT or name in NOT_NAMES:
            return
        old = terms.get(name)
        if old is None or old[0] == country:
            if old is None or weight > old[3]:
                terms[name] = [country, lat, lon, weight]
        elif weight > old[3] * OUTRANKS:
            terms[name] = [country, lat, lon, weight]
        elif weight * OUTRANKS > old[3]:
            old[0], old[3] = None, max(old[3], weight)

    for c in countries:
        code = c["cca2"]
        names = [c["name"]["common"], c["name"]["official"]]
        names += [a for a in c.get("altSpellings", []) if (len(a) > 3 and " " not in a) or a.istitle()]
        for name in names:
            add(name, code, COUNTRY_WEIGHT)
        demonyms = c.get("demonyms", {}).get("eng", {})
        for key in "fm":
            if demonyms.get(key):
                add(demonyms[key], code, COUNTRY_WEIGHT)
        for capital in c.get("capital", []):
            add(capital, code, CAPITAL_WEIGHT)
    for line in open(admin1_path, encoding="utf8"):
        fields = line.rstrip("\n").split("\t")
        code = fields[0].split(".")[0]
        if code in STATE_COUNTRIES:
            add(fields[2], code, STATE_WEIGHT)
    cities = {}
    for line in open(cities_path, encoding="utf8"):
        fields = line.split("\t")
        name, lat, lon, code, population = fields[2], float(fields[4]), float(fields[5]), fields[8], int(fields[14] or 0)
        cities.setdefault((code, name), (lat, lon))
        if population >= MIN_CITY_POPULATION:
            add(name, code, population, lat, lon)
    for name, code in ALIASES.items():
        terms[name] = [code, None, None, ALIAS_WEIGHT]
    capitals = {}
    for c in countries:
        for capital in c.get("capital", [])[:1]:
            if (c["cca2"], capital) in cities:
                capitals[c["cca2"]] = list(cities[(c["cca2"], capital)])
        if c["cca2"] not in capitals and len(c.get("latlng", [])) == 2:
            capitals[c["cca2"]] = c["latlng"]
    return {t: v[:3] for t, v in terms.items() if v[0]}, capitals


def build_homes(places, rows):
    per_outlet = collections.defaultdict(collections.Counter)
    for row in rows:
        if row["source"] in GLOBAL_OUTLETS:
            continue
        for hit in places.detect(row["title"])[:1]:
            per_outlet[row["source"]][hit.country] += 1
    homes = {}
    for outlet, counts in per_outlet.items():
        country, top = counts.most_common(1)[0]
        if sum(counts.values()) >= HOME_MIN_HITS and top / sum(counts.values()) >= HOME_MIN_SHARE:
            homes[outlet] = country
    return homes


def main(countries_path, cities_path, admin1_path, headlines_path):
    countries = json.load(open(countries_path, encoding="utf8"))
    terms, capitals = build_terms(countries, cities_path, admin1_path)
    data = Path(__file__).parent / "data"
    (data / "places.json").write_text(json.dumps(
        {"terms": terms, "abbreviations": ABBREVIATIONS, "capitals": capitals}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    places = Places(terms, ABBREVIATIONS, capitals, {})
    homes = build_homes(places, map(json.loads, open(headlines_path, encoding="utf8")))
    (data / "homes.json").write_text(json.dumps(homes, ensure_ascii=False, sort_keys=True, indent=0))
    print(f"{len(terms)} names, {len(capitals)} capitals, {len(homes)} outlets")


if __name__ == "__main__":
    main(*sys.argv[1:])
