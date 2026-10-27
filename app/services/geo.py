"""
Which region tab a visitor starts on: their country (Cloudflare's CF-IPCountry
header, an ISO 3166 code) mapped to the region that lists it (regions.yaml
countries). Only the starting tab depends on it; nothing is stored.
"""

from typing import Optional

from catalog import ALL_VIEW, get_catalog


def region_for_country(country: Optional[str]) -> str:
    """The region tab for a country code; "all" when unknown (or Tor's "T1", "XX") or listed by no region."""
    code = (country or "").upper()
    return next((region.id for region in get_catalog().regions if code in region.countries), ALL_VIEW)
