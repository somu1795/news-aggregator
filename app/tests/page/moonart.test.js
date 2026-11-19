// The pixel moon and the phase names (static/moonart.js), shared by the header icon and the moon page
const test = require("node:test");
const assert = require("node:assert/strict");
const MoonArt = require("../../static/moonart.js");

const cells = (grid) => grid.flatMap((row, y) => row.map((kind, x) => ({ x: x, y: y, kind: kind }))).filter((c) => c.kind !== null);

test("a full moon has no shaded pixels, a new moon only shaded ones", () => {
    assert.ok(cells(MoonArt.disc({ size: 24, e: 180, limb: -90 })).every((c) => c.kind !== "shade"));
    assert.ok(cells(MoonArt.disc({ size: 24, e: 0, limb: -90 })).every((c) => c.kind === "shade"));
});

test("at first quarter, lit on the right, the right half is lit and the left half shaded", () => {
    const disc = cells(MoonArt.disc({ size: 24, e: 90, limb: -90 }));
    assert.ok(disc.filter((c) => c.x >= 13).every((c) => c.kind === "lit" || c.kind === "crater"));
    assert.ok(disc.filter((c) => c.x <= 10).every((c) => c.kind === "shade"));
});

test("a disc is as many pixels square as asked for, craters only where given", () => {
    const grid = MoonArt.disc({ size: 12, e: 180, limb: -90 });
    assert.equal(grid.length, 12);
    assert.ok(grid.every((row) => row.length === 12));
    assert.ok(cells(grid).every((c) => c.kind === "lit"));
    assert.ok(cells(MoonArt.disc({ size: 24, e: 180, limb: -90, craters: MoonArt.CRATERS })).some((c) => c.kind === "crater"));
});

test("phase names", () => {
    const names = { 0: "new moon", 45: "waxing crescent", 92: "first quarter", 135: "waxing gibbous", 180: "full moon",
        225: "waning gibbous", 270: "last quarter", 315: "waning crescent" };
    for (const [e, name] of Object.entries(names)) assert.equal(MoonArt.phaseName(Number(e)), name);
});

test("how much of the disc is lit", () => {
    assert.equal(MoonArt.litPercent(0), 0);
    assert.equal(MoonArt.litPercent(180), 100);
    assert.equal(MoonArt.litPercent(90), 50);
});
