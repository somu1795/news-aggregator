#!/usr/bin/env python3
"""
Writes the header moon's eclipse table into app/static/sky.js, and the reference
values its tests check the page's own (cheaper) astronomy against. Both come from
JPL's DE421 ephemeris through Skyfield, which the app itself doesn't need.

For each partial or total eclipse of the sun or the moon in the years covered, the
table holds when it can be seen from somewhere on Earth, and the apparent geocentric
right ascension, declination and distance of the moon and of the sun across that
time, as polynomials: the page works out from them what a visitor sees from where
they are (sky.js, eclipse()).

    pip install skyfield                  # In a venv of its own; not one of the app's packages
    scripts/sky-data.py DE421_DIR         # The directory holding (or to download) de421.bsp

Run it again before the last year in the table (END below) to move the table on.
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
from skyfield import almanac, eclipselib
from skyfield.api import Loader, wgs84

REPO = Path(__file__).resolve().parent.parent
SKY_JS = REPO / "app" / "static" / "sky.js"
REFERENCE = REPO / "app" / "tests" / "page" / "sky-reference.json"
START, END = (2026, 11, 1), (2037, 1, 1)
EARTH_RADIUS, MOON_RADIUS, SUN_RADIUS, AU = 6378.137, 1737.4, 696000.0, 149597870.7
STEP = 60  # Seconds between samples while finding an eclipse's start and end
TOLERANCE = 0.2 / 3600  # Degrees: how far a polynomial may stray from DE421 (0.2")

load = Loader(sys.argv[1] if len(sys.argv) > 1 else ".")
eph = load("de421.bsp")
ts = load.timescale()
earth, sun, moon = eph["earth"], eph["sun"], eph["moon"]


def geocentric(body, t):
    """Apparent right ascension and declination of date (degrees) and distance (km)."""
    ra, dec, dist = earth.at(t).observe(body).apparent().radec(epoch="date")
    return ra._degrees, dec.degrees, dist.km


def separation(ra1, dec1, ra2, dec2):
    """Angle between two directions, all in degrees."""
    r = np.radians
    cos = np.sin(r(dec1)) * np.sin(r(dec2)) + np.cos(r(dec1)) * np.cos(r(dec2)) * np.cos(r(ra1 - ra2))
    return np.degrees(np.arccos(np.clip(cos, -1, 1)))


def window(around, seen):
    """The span about `around` (a Skyfield time) when seen(times) holds, sampled each STEP; None if it never does."""
    times = ts.tt_jd(around.tt + np.arange(-8 * 3600, 8 * 3600 + STEP, STEP) / 86400)
    ok = seen(times)
    if not ok.any():
        return None
    idx = np.flatnonzero(ok)
    return times[max(idx[0] - 1, 0)], times[min(idx[-1] + 1, len(times) - 1)]


def solar_seen(times):
    """Whether the sun is eclipsed for someone on Earth: the discs can touch, allowing for the moon's parallax."""
    mra, mdec, mdist = geocentric(moon, times)
    sra, sdec, sdist = geocentric(sun, times)
    reach = np.degrees(np.arcsin(MOON_RADIUS / mdist) + np.arcsin(SUN_RADIUS / sdist) + np.arcsin(EARTH_RADIUS / mdist))
    return separation(mra, mdec, sra, sdec) < reach


def umbra(mdist, sdist):
    """The umbra's angular radius at the moon (degrees), with Danjon's 1% for the atmosphere."""
    moon_parallax, sun_parallax = np.arcsin(EARTH_RADIUS / mdist), np.arcsin(EARTH_RADIUS / sdist)
    return np.degrees(1.01 * moon_parallax + sun_parallax - np.arcsin(SUN_RADIUS / sdist))


def lunar_seen(times):
    """Whether any of the moon is in the umbra."""
    mra, mdec, mdist = geocentric(moon, times)
    sra, sdec, sdist = geocentric(sun, times)
    return separation(mra, mdec, sra + 180, -sdec) < umbra(mdist, sdist) + np.degrees(np.arcsin(MOON_RADIUS / mdist))


def fit(start, end):
    """Polynomials in tau (-1 at start, 1 at end) for the moon's RA, Dec, distance and the sun's RA, Dec; the sun's distance in AU."""
    times = ts.tt_jd(np.linspace(start.tt, end.tt, 97))
    tau = np.linspace(-1, 1, 97)
    mra, mdec, mdist = geocentric(moon, times)
    sra, sdec, sdist = geocentric(sun, times)
    series = [np.degrees(np.unwrap(np.radians(mra))), mdec, mdist, np.degrees(np.unwrap(np.radians(sra))), sdec]
    polys = []
    for i, values in enumerate(series):
        for degree in range(2, 9):
            coefs = np.polynomial.polynomial.polyfit(tau, values, degree)
            rounded = [round(c, 1 if i == 2 else 6) for c in coefs]
            error = np.abs(np.polynomial.polynomial.polyval(tau, rounded) - values).max()
            if error < (1 if i == 2 else TOLERANCE):  # A km for the distance
                break
        else:
            raise SystemExit(f"No polynomial fits series {i} from {start.utc_iso()}")
        polys.append(rounded)
    return polys, round(float(sdist.mean() / AU), 5)


def unix(t):
    """Seconds since 1970 (UTC), as the page's Date.now() counts them."""
    return round(t.utc_datetime().timestamp())


def from_unix(seconds):
    return ts.utc(1970, 1, 1, 0, 0, seconds)


def eclipses():
    t0, t1 = ts.utc(*START), ts.utc(*END)
    table = []
    times, kinds, _ = eclipselib.lunar_eclipses(t0, t1, eph)
    for t, kind in zip(times, kinds):
        if kind == 0:  # Penumbral only: hardly visible
            continue
        span = window(t, lunar_seen)
        polys, sun_au = fit(*span)
        table.append(["lunar", unix(span[0]), unix(span[1]), sun_au, *polys])
    times, phases = almanac.find_discrete(t0, t1, almanac.moon_phases(eph))
    for t in times[phases == 0]:  # New moons
        span = window(t, solar_seen)
        if span:
            polys, sun_au = fit(*span)
            table.append(["solar", unix(span[0]), unix(span[1]), sun_au, *polys])
    return sorted(table, key=lambda e: e[1])


def write_table(table):
    rows = ",\n".join("        " + json.dumps(row, separators=(",", ":")) for row in table)
    block = ("    // Eclipses (generated by scripts/sky-data.py from JPL's DE421; don't edit): kind, start and end (Unix\n"
             "    // seconds, when it's seen from somewhere), the sun's distance (AU), then polynomials in time (-1 start, 1 end)\n"
             "    // for the moon's right ascension, declination (degrees, of date), distance (km), and the sun's right ascension\n"
             f"    // and declination. Until {END[0]}.\n"
             f"    var ECLIPSES = [\n{rows}\n    ];\n")
    js = SKY_JS.read_text()
    js, n = re.subn(r"    // Eclipses \(generated.*?\n    \];\n", lambda _: block, js, flags=re.S)
    if n != 1:
        raise SystemExit("sky.js has no eclipse table to replace")
    SKY_JS.write_text(js)


# --- Reference values for the tests (tests/page/sky.test.js) ---

PLACES = {"London": (51.5, -0.1), "Bengaluru": (13.0, 77.6), "Quito": (-0.2, -78.5), "Sydney": (-33.9, 151.2), "Tromso": (69.6, 19.0)}


def topocentric(place, body, t):
    """Altitude and azimuth (degrees) of a body from a place."""
    alt, az, _ = (earth + wgs84.latlon(*place)).at(t).observe(body).apparent().altaz()
    return alt.degrees, az.degrees


def disc_covered(r, cover, d):
    """How much of a disc of radius r is covered by one of radius `cover`, their centres d apart (radians)."""
    if d >= r + cover:
        return 0.0
    if d <= abs(r - cover):
        return 1.0 if cover >= r else float((cover / r) ** 2)
    a = r ** 2 * np.arccos((d * d + r * r - cover * cover) / (2 * d * r)) + cover ** 2 * np.arccos((d * d + cover * cover - r * r) / (2 * d * cover))
    a -= 0.5 * np.sqrt((-d + r + cover) * (d + r - cover) * (d - r + cover) * (d + r + cover))
    return float(a / (np.pi * r * r))


def obscuration(place, t):
    """The fraction of the sun's disc the moon covers, seen from a place."""
    observer = (earth + wgs84.latlon(*place)).at(t)
    m, s = observer.observe(moon).apparent(), observer.observe(sun).apparent()
    rm, rs = np.arcsin(MOON_RADIUS / m.distance().km), np.arcsin(SUN_RADIUS / s.distance().km)
    return disc_covered(rs, rm, m.separation_from(s).radians)


def reference(table):
    ref = {"riseSet": [], "positions": [], "solar": [], "lunar": []}
    day0, day1 = ts.utc(2026, 11, 16), ts.utc(2026, 11, 17)
    for name, place in PLACES.items():
        for body, key in ((sun, "sun"), (moon, "moon")):
            # The top edge at the horizon, allowing for refraction (34'): Skyfield's positions are seen from the
            # place, so the moon's parallax is already in them (sky.js, from Earth's centre, allows for it instead)
            radius = 0.2666 if key == "sun" else 0.2590
            f = almanac.risings_and_settings(eph, body, wgs84.latlon(*place), horizon_degrees=-0.5667, radius_degrees=radius)
            times, rising = almanac.find_discrete(day0, day1, f)
            for t, up in zip(times, rising):
                ref["riseSet"].append({"place": name, "body": key, "event": "rise" if up else "set", "time": unix(t)})
        for hour in (0, 6, 12, 18):
            t = ts.utc(2026, 11, 16, hour)
            entry = {"place": name, "time": unix(t)}
            for key in ("sun", "moon", "venus", "jupiter barycenter", "mars", "saturn barycenter"):
                entry[key.split()[0]] = [round(v, 2) for v in topocentric(place, eph[key], t)]
            ref["positions"].append(entry)
    for row in table:
        if row[0] != "solar":
            continue
        start, end = from_unix(row[1]), from_unix(row[2])
        for name, place in PLACES.items():
            for frac in (0.25, 0.5, 0.75):
                t = ts.tt_jd(start.tt + (end.tt - start.tt) * frac)
                ref["solar"].append({"place": name, "time": unix(t), "covered": round(obscuration(place, t), 3),
                                     "sunAlt": round(topocentric(place, sun, t)[0], 2)})
    # The total eclipse over Sydney: its greatest moment there, to the minute
    times = ts.utc(2028, 7, 22, 2, range(0, 180))
    best = max(times, key=lambda t: obscuration(PLACES["Sydney"], t))
    ref["solar"].append({"place": "Sydney", "time": unix(best), "covered": round(obscuration(PLACES["Sydney"], best), 3),
                         "sunAlt": round(topocentric(PLACES["Sydney"], sun, best)[0], 2)})
    for row in table:
        if row[0] != "lunar":
            continue
        for frac in (0.25, 0.5, 0.75):
            t = from_unix(round(row[1] + (row[2] - row[1]) * frac))
            mra, mdec, mdist = geocentric(moon, t)
            sra, sdec, sdist = geocentric(sun, t)
            size, shadow = np.radians(np.degrees(np.arcsin(MOON_RADIUS / mdist))), np.radians(umbra(mdist, sdist))
            apart = np.radians(separation(mra, mdec, sra + 180, -sdec))
            entry = {"time": unix(t), "covered": round(disc_covered(size, shadow, apart), 3), "moonAlt": {}}
            for name, place in PLACES.items():
                entry["moonAlt"][name] = round(topocentric(place, moon, t)[0], 2)
            ref["lunar"].append(entry)
    REFERENCE.write_text(json.dumps(ref, indent=1) + "\n")


table = eclipses()
write_table(table)
reference(table)
for row in table:
    print(row[0], from_unix(row[1]).utc_strftime("%Y-%m-%d %H:%M"),
          round((row[2] - row[1]) / 3600, 1), "h", sum(len(p) for p in row[4:]), "coefficients")
