"""
Which region tab a visitor starts on: their country (Cloudflare's CF-IPCountry
header, an ISO 3166 code) mapped to the region that lists it (regions.yaml
countries); also how the header's moon is drawn: which way up for the hemisphere,
and tilted for the visitor's rough position when Cloudflare sends it (CF-IPLatitude,
CF-IPLongitude). Only these depend on it; nothing is stored.
"""

import math
from typing import Optional

from catalog import ALL_VIEW, get_catalog


def region_for_country(country: Optional[str]) -> str:
    """The region tab for a country code; "all" when unknown (or Tor's "T1", "XX") or listed by no region."""
    code = (country or "").upper()
    return next((region.id for region in get_catalog().regions if code in region.countries), ALL_VIEW)


# Countries where most people live well south of the equator, so the moon looks mirrored
# (lit on the left while waxing). Near the equator either way looks about as wrong.
SOUTHERN_HEMISPHERE = frozenset("""
    AO AR AS AU BO BR BW BI CK CL FJ FK ID KM LS MG MU MW MZ NA NC NF NR NU NZ PE PF PG PN PY
    RE SB SC SH SZ TF TL TO TV TZ UY VU WF WS YT ZA ZM ZW
""".split())


def in_southern_hemisphere(country: Optional[str]) -> bool:
    """Whether a visitor from this country (code as above) sees the moon from the south; unknown is north."""
    return (country or "").upper() in SOUTHERN_HEMISPHERE


def rough_position(latitude: Optional[str], longitude: Optional[str]) -> Optional[list[float]]:
    """[latitude, longitude] rounded to a tenth of a degree (about 11 km); None when either is missing or not a place."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180):
        return None
    return [round(lat, 1), round(lon, 1)]
