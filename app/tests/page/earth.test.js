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
