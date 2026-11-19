// Earth's face for the moon page (static/earth.js): land, where the sun is overhead, day and night
const test = require("node:test");
const assert = require("node:assert/strict");
const Earth = require("../../static/earth.js");

test("land and sea on the grid, nothing off it", () => {
    assert.equal(Earth.isLand(51.5, -0.1), 1);
    assert.equal(Earth.isLand(13, 77.6), 1);
    assert.equal(Earth.isLand(30, -40), 0);
    assert.equal(Earth.isLand(-70, 0), null);
});

test("the sun is overhead at the Tropic of Capricorn at the December solstice", () => {
    const [lat, lon] = Earth.subsolar(Date.parse("2026-12-21T12:00:00Z"));
    assert.ok(Math.abs(lat + 23.44) < 0.3, String(lat));
    assert.ok(Math.abs(lon) < 2, String(lon));
});

test("the face centred on London: land by day at midsummer noon, by night at midwinter midnight", () => {
    const at = (utc) => Earth.face({ size: 41, lat: 51.5, lon: -0.1, rot: 0, time: Date.parse(utc) })[20][20];
    assert.equal(at("2026-06-21T12:00:00Z"), "land");
    assert.equal(at("2026-12-21T00:00:00Z"), "land-night");
});

test("north is up when rot is 0, and a disc of the size asked for", () => {
    const options = { size: 41, lat: 0, lon: 0, rot: 0, time: Date.parse("2026-06-21T12:00:00Z") };
    const grid = Earth.face(options);
    assert.equal(grid.length, 41);
    assert.equal(grid[0][0], null);  // A corner: outside the disc
    assert.ok(Earth.cellLatLon(options, 20, 2)[0] > 60 && Earth.cellLatLon(options, 20, 38)[0] < -60);
    const turned = Object.assign({}, options, { rot: Math.PI / 2 });  // North to the right
    assert.ok(Earth.cellLatLon(turned, 38, 20)[0] > 60);
});

test("redrawing a face is cheap once its place is known: under a quarter of drawing a new place", () => {
    // Compared with each other, not with a fixed time, and each the middle of several runs: on a busy machine
    // (the test files run side by side) single timings jump about
    const options = { size: 104, lat: 51.5, lon: -0.1, rot: 0.3, time: Date.parse("2026-11-19T00:00:00Z") };
    const ms = (run) => { const start = process.hrtime.bigint(); run(); return Number(process.hrtime.bigint() - start) / 1e6; };
    const middle = (values) => values.sort((a, b) => a - b)[values.length >> 1];
    const newPlace = middle([[13, 77.6], [-33.9, 151.2], [40.7, -74], [-0.2, -78.5], [51.5, -0.1]].map(([lat, lon]) =>
        ms(() => Earth.face(Object.assign({}, options, { lat: lat, lon: lon })))));
    const redraw = middle([0, 1, 2, 3, 4].map((batch) => ms(() => {
        for (let i = 0; i < 10; i++) Earth.face(Object.assign({}, options, { rot: 0.3 + i * 0.01, time: options.time + (batch * 10 + i) * 480000 }));
    }) / 10));
    assert.ok(redraw * 4 < newPlace, `a redraw ${redraw.toFixed(2)} ms, a new place ${newPlace.toFixed(2)} ms`);
});

test("a turned face shows what the exact projection does, give or take a pixel at the edges", () => {
    const options = { size: 104, lat: 13, lon: 77.6, rot: 0.7, time: Date.parse("2026-06-21T06:00:00Z") };
    const face = Earth.face(options);
    let same = 0, all = 0;
    for (let y = 0; y < 104; y++) for (let x = 0; x < 104; x++) {
        const at = Earth.cellLatLon(options, x, y);
        if (!at || face[y][x] === null) continue;
        all++;
        const land = Earth.isLand(at[0], at[1]) === 1 || at[0] > 80 || at[0] < -64;
        if (/land|ice/.test(face[y][x]) === land) same++;
    }
    assert.ok(same / all > 0.95, (100 * same / all).toFixed(1) + "% alike");
});
