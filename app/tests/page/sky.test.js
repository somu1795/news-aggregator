// The header moon's astronomy (static/sky.js) against JPL's DE421 ephemeris (sky-reference.json,
// written by scripts/sky-data.py): positions, rise and set times, eclipses; and the meteor showers.
const test = require("node:test");
const assert = require("node:assert/strict");
const Sky = require("../../static/sky.js");
const REF = require("./sky-reference.json");

const PLACES = { London: [51.5, -0.1], Bengaluru: [13.0, 77.6], Quito: [-0.2, -78.5], Sydney: [-33.9, 151.2], Tromso: [69.6, 19.0] };
const RAD = Math.PI / 180;
const angle = (a, b) => Math.abs(((a - b) % 360 + 540) % 360 - 180);

test("rise and set times are within a few minutes of JPL's", () => {
    assert.ok(REF.riseSet.length >= 15);
    for (const r of REF.riseSet) {
        const found = Sky.riseSet(r.body, (r.time - 3 * 3600) * 1000, PLACES[r.place])[r.event];
        const minutes = Math.abs(found / 1000 - r.time) / 60;
        assert.ok(minutes < (r.body === "sun" ? 2 : 4), `${r.place} ${r.body}${r.event} off by ${minutes.toFixed(1)} min`);
    }
});

test("the sun, moon and planets are where JPL puts them, within a degree or so", () => {
    for (const r of REF.positions) {
        const place = PLACES[r.place], time = r.time * 1000;
        const check = (name, seen, within) => {
            const [alt, az] = r[name];
            assert.ok(Math.abs(seen.alt - alt) < within, `${r.place} ${name} altitude ${seen.alt.toFixed(2)} vs ${alt}`);
            if (alt > -80 && alt < 80) assert.ok(angle(seen.az, az) < within / Math.cos(alt * RAD), `${r.place} ${name} azimuth ${seen.az.toFixed(2)} vs ${az}`);
        };
        const sky = Sky.sunAndMoon(time);
        check("sun", Sky.horizontal(sky.sun, time, place), 0.5);
        check("moon", Sky.horizontal(sky.moon, time, place), 1.5);  // Seen from Earth's centre: up to a degree of parallax
        for (const name of ["Venus", "Jupiter", "Mars", "Saturn"]) check(name.toLowerCase(), Sky.horizontal(Sky.planetPosition(name, time), time, place), 1);
    }
});

test("solar eclipses: how much of the sun is covered, where it's up, is JPL's within 3%", () => {
    let seen = 0;
    for (const r of REF.solar) {
        if (Math.abs(r.sunAlt) < 1) continue;  // At the horizon: either answer will do
        const found = Sky.eclipse(r.time * 1000, PLACES[r.place]);
        if (r.sunAlt < 0) { assert.equal(found, null, `${r.place} ${r.time}: the sun is down`); continue; }
        if (r.covered === 0) { assert.ok(!found || found.covered < 0.03, `${r.place} ${r.time}: no eclipse there`); continue; }
        if (r.covered < 0.03) continue;
        seen++;
        assert.equal(found && found.kind, "solar", `${r.place} ${r.time}: ${r.covered} covered`);
        assert.ok(Math.abs(found.covered - r.covered) < 0.03, `${r.place} ${r.time}: ${found.covered.toFixed(3)} vs ${r.covered}`);
    }
    assert.ok(seen >= 15);
});

test("the total eclipse of 22 July 2028 is total over Sydney, and drawn with the moon over the sun", () => {
    const greatest = REF.solar.at(-1);
    const found = Sky.eclipse(greatest.time * 1000, PLACES.Sydney);
    assert.ok(found.covered > 0.97, String(found.covered));
    assert.ok(Math.hypot(found.at[0], found.at[1]) < 0.2 && found.size > 1, JSON.stringify(found));
    assert.equal(Sky.eclipse(greatest.time * 1000, PLACES.London), null);  // Night there
});

test("lunar eclipses: how much of the moon is in Earth's shadow, where it's up, is JPL's within 3%", () => {
    let seen = 0;
    for (const r of REF.lunar) {
        for (const [name, alt] of Object.entries(r.moonAlt)) {
            if (Math.abs(alt) < 1.5) continue;
            const found = Sky.eclipse(r.time * 1000, PLACES[name]);
            if (alt < 0 || r.covered === 0) { assert.equal(found, null, `${name} ${r.time}`); continue; }
            seen++;
            assert.equal(found.kind, "lunar");
            assert.ok(Math.abs(found.covered - r.covered) < 0.03, `${name} ${r.time}: ${found.covered.toFixed(3)} vs ${r.covered}`);
            assert.equal(found.type, r.covered === 1 ? "total" : "partial");
        }
    }
    assert.ok(seen >= 20);
});

test("the table covers eclipses to 2036, in order, and nothing is eclipsed between them", () => {
    assert.ok(Sky.ECLIPSES.length > 30);
    assert.ok(Sky.ECLIPSES.at(-1)[1] > Date.UTC(2036, 0, 1) / 1000);
    Sky.ECLIPSES.forEach((e, i) => { if (i) assert.ok(e[1] > Sky.ECLIPSES[i - 1][2]); });
    assert.equal(Sky.eclipse(Date.UTC(2026, 10, 16, 12), PLACES.Bengaluru), null);
});

test("a meteor shower shows near its peak, in a dark sky, with its radiant up", () => {
    const shower = (iso, place) => (Sky.meteorShower(Date.parse(iso), PLACES[place]) || {}).name || null;
    assert.equal(shower("2026-11-17T22:30:00Z", "Bengaluru"), "Leonids");  // 4 am: Leo high, the sky dark
    assert.equal(shower("2026-11-17T13:30:00Z", "Bengaluru"), null);  // 7 pm: the radiant not up yet
    assert.equal(shower("2026-11-25T22:30:00Z", "Bengaluru"), null);  // A week after the peak
    assert.equal(shower("2026-12-14T01:00:00Z", "London"), "Geminids");
    assert.equal(shower("2026-08-12T23:00:00Z", "Sydney"), null);  // The Perseids' radiant never rises far there
    assert.equal(shower("2026-08-13T01:00:00Z", "London"), "Perseids");
});

test("the brightest planet in a dark sky", () => {
    const planet = (iso, place) => (Sky.brightPlanet(Date.parse(iso), PLACES[place]) || {}).name || null;
    assert.equal(planet("2026-11-16T12:00:00Z", "London"), null);  // Daylight
    assert.equal(planet("2026-11-16T19:45:00Z", "Bengaluru"), "Jupiter");  // 1:15 am, Jupiter in the east
});
