"""
Which region tab a visitor starts on: their country (Cloudflare's CF-IPCountry
header, an ISO 3166 code) mapped to a region of sources.json. Only the
starting tab depends on it; nothing is stored.
"""

from typing import Optional

from config import ALL_VIEW, CATALOG

_COUNTRIES = {
    "africa": "DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN GW KE LS LR LY MG MW ML MR MU "
              "YT MA MZ NA NE NG RE RW SH ST SN SC SL SO ZA SS SD TZ TG TN UG EH ZM ZW",
    "europe": "AL AD AT AX BY BE BA BG HR CY CZ DK EE FO FI FR DE GI GR GG HU IS IE IM IT JE XK LV LI LT LU MT MD MC "
              "ME NL MK NO PL PT RO RU SM RS SK SI ES SJ SE CH UA GB VA",
    "asia": "AF AM AZ BH BD BT BN KH CN GE HK IN ID IR IQ IL JP JO KZ KW KG LA LB MO MY MV MN MM NP KP OM PK PS PH QA "
            "SA SG KR LK SY TW TJ TH TL TR TM AE UZ VN YE IO CC CX",
    "americas": "AI AG AR AW BS BB BZ BM BO BQ BR VG CA KY CL CO CR CU CW DM DO EC SV FK GF GL GD GP GT GY HT HN JM MQ "
                "MX MS NI PA PY PE PR BL KN LC MF PM VC SX SR TT TC US UY VE VI UM GS",
    "oceania": "AS AU CK FJ PF GU KI MH FM NR NC NZ NU NF MP PW PG PN WS SB TK TO TV VU WF",
}
_REGION_BY_COUNTRY = {code: region for region, codes in _COUNTRIES.items() for code in codes.split()}


def region_for_country(country: Optional[str]) -> str:
    """The region tab for a country code; "all" when unknown (or Tor's "T1", "XX") or not in sources.json."""
    region = _REGION_BY_COUNTRY.get((country or "").upper())
    return region if region in CATALOG.view_ids else ALL_VIEW
